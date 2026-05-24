import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

import httpx


GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_PRICE_URL = "https://clob.polymarket.com/price"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
PUBLIC_PROFILE_URL = "https://gamma-api.polymarket.com/public-profile"

_MARKET_CACHE = {"ts": 0.0, "value": {}}


def parse_json_field(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _to_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _clamp_price(value, default=None):
    v = _to_float(value, default)
    if v is None:
        return None
    return max(0.01, min(0.99, float(v)))


def _norm_outcome(x: Any) -> str:
    s = str(x or "").strip().lower()
    if s in ("yes", "up", "higher", "above", "green"):
        return "up"
    if s in ("no", "down", "lower", "below", "red"):
        return "down"
    if "up" in s or "above" in s or "higher" in s or "yes" == s:
        return "up"
    if "down" in s or "below" in s or "lower" in s or "no" == s:
        return "down"
    return s


def _extract_list(value):
    value = parse_json_field(value)
    return value if isinstance(value, list) else []


def _extract_outcome_map(outcomes, values) -> Dict[str, Any]:
    outcomes = _extract_list(outcomes)
    values = _extract_list(values)

    result = {}
    if not outcomes or not values:
        return result

    for outcome, value in zip(outcomes, values):
        key = _norm_outcome(outcome)
        if key in ("up", "down"):
            result[key] = value

    return result


def _extract_prices(m: Dict[str, Any]) -> Dict[str, float]:
    prices = _extract_outcome_map(m.get("outcomes"), m.get("outcomePrices"))
    result = {}
    for k, v in prices.items():
        val = _clamp_price(v)
        if val is not None:
            result[k] = val

    # Fallback names sometimes used by transformed payloads.
    for key, out_key in [
        ("up_price", "up"),
        ("yes_price", "up"),
        ("down_price", "down"),
        ("no_price", "down"),
    ]:
        if out_key not in result:
            val = _clamp_price(m.get(key))
            if val is not None:
                result[out_key] = val

    # If only one side exists infer binary opposite.
    if "up" in result and "down" not in result:
        result["down"] = _clamp_price(1.0 - result["up"])
    if "down" in result and "up" not in result:
        result["up"] = _clamp_price(1.0 - result["down"])

    return result


def _extract_token_ids(m: Dict[str, Any]) -> Dict[str, str]:
    ids = _extract_outcome_map(m.get("outcomes"), m.get("clobTokenIds"))

    # Some Gamma payloads use nested tokens array.
    if not ids:
        tokens = _extract_list(m.get("tokens"))
        for t in tokens:
            if not isinstance(t, dict):
                continue
            outcome = _norm_outcome(t.get("outcome") or t.get("name"))
            token_id = t.get("token_id") or t.get("tokenId") or t.get("id")
            if outcome in ("up", "down") and token_id:
                ids[outcome] = str(token_id)

    # Some payloads have direct keys.
    direct = [
        ("up_token_id", "up"),
        ("yes_token_id", "up"),
        ("down_token_id", "down"),
        ("no_token_id", "down"),
    ]
    for key, out_key in direct:
        if out_key not in ids and m.get(key):
            ids[out_key] = str(m.get(key))

    return {k: str(v) for k, v in ids.items() if v is not None and str(v)}


def _text_blob(m: Dict[str, Any], ev: Dict[str, Any] | None = None) -> str:
    parts = []
    for obj in [ev or {}, m]:
        for k in [
            "question", "title", "slug", "description", "rules", "resolutionSource",
            "seriesSlug", "eventSlug", "ticker"
        ]:
            if obj.get(k):
                parts.append(str(obj.get(k)))
    return " ".join(parts)


def _is_btc_updown(text: str) -> bool:
    t = text.lower()
    has_btc = any(x in t for x in ["btc", "bitcoin"])
    has_direction = (
        "up or down" in t or "up/down" in t or "up-down" in t or
        (" up " in f" {t} " and " down" in t) or
        "higher or lower" in t
    )
    return has_btc and has_direction


def _is_15m(text: str, m: Dict[str, Any], ev: Dict[str, Any] | None = None) -> bool:
    t = text.lower()
    if any(x in t for x in ["15m", "15 min", "15-minute", "15 minute", "15mins"]):
        return True

    # Polymarket crypto up/down markets usually have event/market end every 15 minutes.
    # If title just says "BTC Up or Down" but start-end timestamps are 15m, accept it.
    start_raw = (
        m.get("startDate") or m.get("start_date") or
        (ev or {}).get("startDate") or (ev or {}).get("start_date")
    )
    end_raw = (
        m.get("endDate") or m.get("end_date") or
        (ev or {}).get("endDate") or (ev or {}).get("end_date")
    )
    try:
        if start_raw and end_raw:
            start = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
            mins = abs((end - start).total_seconds()) / 60
            if 13 <= mins <= 17:
                return True
    except Exception:
        pass

    # Slug may include current round time only and no literal 15m.
    if "up-or-down" in t and any(x in t for x in ["crypto", "bitcoin", "btc"]):
        return True

    return False


def _looks_like_btc_15m_market(m: Dict[str, Any], ev: Dict[str, Any] | None = None) -> bool:
    text = _text_blob(m, ev)
    return _is_btc_updown(text) and _is_15m(text, m, ev)


def _parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _market_sort_key(m: Dict[str, Any]):
    now = datetime.now(timezone.utc)

    end_dt = _parse_datetime(m.get("endDate") or m.get("end_date"))
    start_dt = _parse_datetime(m.get("startDate") or m.get("start_date"))

    active_score = 0
    if start_dt and end_dt and start_dt <= now <= end_dt:
        active_score = 10_000_000
    elif end_dt and end_dt > now:
        active_score = 5_000_000

    liquidity = _to_float(m.get("liquidity") or m.get("liquidityNum"), 0.0) or 0.0
    volume = _to_float(m.get("volume") or m.get("volumeNum"), 0.0) or 0.0
    # Prefer active/current market, then liquidity/volume, then later end date.
    end_ts = end_dt.timestamp() if end_dt else 0
    return (active_score, liquidity + volume, end_ts)


def _parse_target_price(m: Dict[str, Any], ev: Dict[str, Any] | None = None) -> Optional[float]:
    raw = _text_blob(m, ev)

    patterns = [
        r"(?:price\s*to\s*beat|target|strike|above|below|final\s*price)[^\d$]{0,60}\$?\s*([0-9]{2,3}[, ]?[0-9]{3}(?:\.[0-9]+)?)",
        r"\$?\s*([0-9]{2,3}[, ]?[0-9]{3}(?:\.[0-9]+)?)[^\n]{0,60}(?:price\s*to\s*beat|target|strike|above|below|final\s*price)",
        r"btc[^\d$]{0,80}\$?\s*([0-9]{2,3}[, ]?[0-9]{3}(?:\.[0-9]+)?)",
        r"bitcoin[^\d$]{0,80}\$?\s*([0-9]{2,3}[, ]?[0-9]{3}(?:\.[0-9]+)?)",
    ]

    for pat in patterns:
        for match in re.finditer(pat, raw, flags=re.I):
            val = _to_float(match.group(1).replace(",", "").replace(" ", ""))
            if val and 10000 < val < 200000:
                return val

    nums = re.findall(r"\$?\s*([0-9]{2,3}[, ]?[0-9]{3}(?:\.[0-9]+)?)", raw)
    for n in nums:
        val = _to_float(n.replace(",", "").replace(" ", ""))
        if val and 10000 < val < 200000:
            return val
    return None


async def _clob_price(client: httpx.AsyncClient, token_id: str, side: str = "BUY") -> Optional[float]:
    if not token_id:
        return None
    token_id = str(token_id)

    # Endpoint 1: /price
    try:
        r = await client.get(CLOB_PRICE_URL, params={"token_id": token_id, "side": side})
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                price = data.get("price") or data.get("mid") or data.get("bestAsk") or data.get("bestBid")
                val = _clamp_price(price)
                if val is not None:
                    return val
    except Exception:
        pass

    # Endpoint 2: /book fallback: use best ask for BUY.
    try:
        r = await client.get(CLOB_BOOK_URL, params={"token_id": token_id})
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                asks = data.get("asks") or []
                bids = data.get("bids") or []
                book_side = asks if side.upper() == "BUY" else bids
                best = None
                for row in book_side:
                    if isinstance(row, dict):
                        p = _to_float(row.get("price"))
                    elif isinstance(row, (list, tuple)) and row:
                        p = _to_float(row[0])
                    else:
                        p = None
                    if p is None:
                        continue
                    if best is None:
                        best = p
                    elif side.upper() == "BUY":
                        best = min(best, p)
                    else:
                        best = max(best, p)
                val = _clamp_price(best)
                if val is not None:
                    return val
    except Exception:
        pass

    return None


async def _fill_clob_prices(market: Dict[str, Any]):
    up_token = market.get("up_token_id")
    down_token = market.get("down_token_id")

    headers = {
        "User-Agent": "PolyScalpBot/1.0",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=8, headers=headers) as client:
        up_clob = await _clob_price(client, up_token, "BUY") if up_token else None
        down_clob = await _clob_price(client, down_token, "BUY") if down_token else None

    if up_clob is not None:
        market["up_price"] = up_clob
        market["up_price_source"] = "clob"
    if down_clob is not None:
        market["down_price"] = down_clob
        market["down_price_source"] = "clob"

    if market.get("up_price") is not None and market.get("down_price") is None:
        market["down_price"] = _clamp_price(1.0 - float(market["up_price"]))
        market["down_price_source"] = "inferred"
    if market.get("down_price") is not None and market.get("up_price") is None:
        market["up_price"] = _clamp_price(1.0 - float(market["down_price"]))
        market["up_price_source"] = "inferred"


def _normalize_market(chosen: Dict[str, Any], ev: Dict[str, Any] | None = None) -> Dict[str, Any]:
    prices = _extract_prices(chosen)
    token_ids = _extract_token_ids(chosen)

    up_price = prices.get("up")
    down_price = prices.get("down")

    slug = str(chosen.get("slug") or (ev or {}).get("slug") or "")
    event_slug = str((ev or {}).get("slug") or chosen.get("eventSlug") or slug)
    url_slug = event_slug or slug

    end_dt = chosen.get("endDate") or chosen.get("end_date") or (ev or {}).get("endDate") or (ev or {}).get("end_date")
    start_dt = chosen.get("startDate") or chosen.get("start_date") or (ev or {}).get("startDate") or (ev or {}).get("start_date")

    return {
        "id": str(chosen.get("id") or chosen.get("conditionId") or ""),
        "condition_id": str(chosen.get("conditionId") or chosen.get("condition_id") or ""),
        "question": str(chosen.get("question") or chosen.get("title") or (ev or {}).get("title") or "BTC 15m Up/Down"),
        "slug": slug,
        "event_slug": event_slug,
        "url": "https://polymarket.com/event/" + url_slug if url_slug else "",
        "start_date": str(start_dt or ""),
        "end_date": str(end_dt or ""),
        "up_price": up_price,
        "down_price": down_price,
        "up_price_source": "gamma" if up_price is not None else "missing",
        "down_price_source": "gamma" if down_price is not None else "missing",
        "up_token_id": token_ids.get("up"),
        "down_token_id": token_ids.get("down"),
        "target_price": _parse_target_price(chosen, ev),
        "volume": _to_float(chosen.get("volume") or chosen.get("volumeNum") or (ev or {}).get("volume"), 0.0) or 0.0,
        "liquidity": _to_float(chosen.get("liquidity") or chosen.get("liquidityNum") or (ev or {}).get("liquidity"), 0.0) or 0.0,
        "raw": chosen,
    }


def _extract_markets_from_events(events: Any) -> List[tuple[Dict[str, Any], Dict[str, Any]]]:
    found = []
    if not isinstance(events, list):
        return found

    for ev in events:
        if not isinstance(ev, dict):
            continue

        markets = parse_json_field(ev.get("markets") or ev.get("marketsData") or [])
        if isinstance(markets, list):
            for m in markets:
                if isinstance(m, dict):
                    found.append((m, ev))

        # Some endpoints return event itself like a market.
        if ev.get("question") or ev.get("outcomes"):
            found.append((ev, ev))

    return found


async def _gamma_get(client: httpx.AsyncClient, url: str, params: Dict[str, Any]):
    r = await client.get(url, params=params)
    r.raise_for_status()
    return r.json()


async def discover_btc_15m_market() -> Dict[str, Any]:
    """
    Find active/current BTC Up or Down 15m market and return real odds when available.
    Important: returns odds_source gamma/clob when real prices exist.
    """
    now_ts = time.time()
    if _MARKET_CACHE["value"] and now_ts - _MARKET_CACHE["ts"] < 2.0:
        return _MARKET_CACHE["value"]

    headers = {
        "User-Agent": "PolyScalpBot/1.0",
        "Accept": "application/json",
    }

    candidates: List[tuple[Dict[str, Any], Dict[str, Any]]] = []

    async with httpx.AsyncClient(timeout=10, headers=headers) as client:
        # Broad event searches: crypto up/down is often event title.
        searches = [
            "BTC Up or Down",
            "Bitcoin Up or Down",
            "BTC Up or Down 15m",
            "Bitcoin Up or Down 15m",
        ]

        # Events usually contain nested markets.
        for search in searches:
            try:
                data = await _gamma_get(client, GAMMA_EVENTS_URL, {
                    "active": "true",
                    "closed": "false",
                    "limit": 200,
                    "search": search,
                })
                candidates.extend(_extract_markets_from_events(data))
            except Exception:
                pass

        # Direct market search too.
        for search in searches + ["btc 15m", "bitcoin 15m"]:
            try:
                data = await _gamma_get(client, GAMMA_MARKETS_URL, {
                    "active": "true",
                    "closed": "false",
                    "limit": 200,
                    "search": search,
                })
                if isinstance(data, list):
                    for m in data:
                        if isinstance(m, dict):
                            candidates.append((m, {}))
            except Exception:
                pass

        # Final broad fallback: current crypto events without search.
        if not candidates:
            try:
                data = await _gamma_get(client, GAMMA_EVENTS_URL, {
                    "active": "true",
                    "closed": "false",
                    "limit": 250,
                })
                candidates.extend(_extract_markets_from_events(data))
            except Exception:
                pass

    filtered = []
    for m, ev in candidates:
        if _looks_like_btc_15m_market(m, ev):
            norm = _normalize_market(m, ev)
            filtered.append(norm)

    # Deduplicate.
    uniq = {}
    for m in filtered:
        key = m.get("condition_id") or m.get("id") or m.get("slug") or m.get("question")
        uniq[str(key)] = m

    if not uniq:
        empty = {
            "up_price": None,
            "down_price": None,
            "up_price_source": "missing",
            "down_price_source": "missing",
            "question": "No BTC 15m market found",
            "odds_debug": "no candidates matched BTC 15m filters",
        }
        _MARKET_CACHE.update({"ts": now_ts, "value": empty})
        return empty

    chosen = sorted(uniq.values(), key=_market_sort_key, reverse=True)[0]

    # Always try CLOB if token ids exist. This overwrites stale Gamma with executable BUY price.
    await _fill_clob_prices(chosen)

    # Final source flag used by btc.py
    if chosen.get("up_price") is not None and chosen.get("down_price") is not None:
        chosen["odds_source"] = f"{chosen.get('up_price_source','?')}/{chosen.get('down_price_source','?')}"
    else:
        chosen["odds_source"] = "missing"

    _MARKET_CACHE.update({"ts": now_ts, "value": chosen})
    return chosen


async def fetch_public_profile(wallet: str):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(PUBLIC_PROFILE_URL, params={"address": wallet})
        r.raise_for_status()
        return r.json()
