import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

import httpx


GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_EVENT_SLUG_URL = "https://gamma-api.polymarket.com/events/slug"
CLOB_PRICE_URL = "https://clob.polymarket.com/price"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
PUBLIC_PROFILE_URL = "https://gamma-api.polymarket.com/public-profile"

_MARKET_CACHE = {"ts": 0.0, "value": {}}


def clear_market_cache():
    _MARKET_CACHE.update({"ts": 0.0, "value": {}})


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


def _extract_list(value):
    value = parse_json_field(value)
    return value if isinstance(value, list) else []


def _round_down(ts: int, seconds: int) -> int:
    return (ts // seconds) * seconds


def _candidate_slugs(asset: str = "btc", duration: int = 15) -> List[str]:
    """
    Polymarket 15m crypto market slugs are deterministic:
    btc-updown-15m-<unix interval start>

    IMPORTANT:
    Try CURRENT first, then NEXT, then PREVIOUS.
    Old resolved markets can still exist in Gamma and may look valid, so never test old windows first.
    Example:
    at 11:27 UTC, current slug must be base 11:15 UTC, not 10:45 UTC.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    interval = duration * 60
    base = _round_down(now, interval)
    offsets = [0, 1, -1, 2, -2]
    return [f"{asset}-updown-{duration}m-{base + i * interval}" for i in offsets]


def _slug_start_ts(slug: str) -> Optional[int]:
    try:
        return int(str(slug).rstrip("/").split("-")[-1])
    except Exception:
        return None


def _is_current_or_next_slug(slug: str, duration: int = 15) -> bool:
    start = _slug_start_ts(slug)
    if start is None:
        return False
    now = int(datetime.now(timezone.utc).timestamp())
    interval = duration * 60
    # Accept current market or next market only.
    # Allow a small 10s grace around exact boundaries.
    return (start - 10) <= now <= (start + interval + 10) or (now < start <= now + interval + 10)


def _market_from_event(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    markets = data.get("markets")
    markets = parse_json_field(markets)
    if isinstance(markets, list) and markets:
        for m in markets:
            if isinstance(m, dict):
                return m
    if data.get("clobTokenIds") or data.get("outcomePrices"):
        return data
    return None


def _extract_token_ids(m: Dict[str, Any]) -> Dict[str, str]:
    raw = parse_json_field(m.get("clobTokenIds") or m.get("clob_token_ids") or [])
    if isinstance(raw, list) and len(raw) >= 2:
        return {"up": str(raw[0]), "down": str(raw[1])}

    outcomes = _extract_list(m.get("outcomes"))
    tokens = _extract_list(m.get("clobTokenIds"))
    result = {}
    for outcome, token in zip(outcomes, tokens):
        o = str(outcome).lower()
        if "up" in o or o == "yes":
            result["up"] = str(token)
        elif "down" in o or o == "no":
            result["down"] = str(token)

    # Direct fallbacks
    for key, side in [("up_token_id", "up"), ("yes_token_id", "up"), ("down_token_id", "down"), ("no_token_id", "down")]:
        if side not in result and m.get(key):
            result[side] = str(m.get(key))

    return result


def _extract_gamma_prices(m: Dict[str, Any]) -> Dict[str, Optional[float]]:
    raw = parse_json_field(m.get("outcomePrices") or m.get("outcome_prices") or [])
    if isinstance(raw, list) and len(raw) >= 2:
        return {"up": _clamp_price(raw[0]), "down": _clamp_price(raw[1])}

    outcomes = _extract_list(m.get("outcomes"))
    prices = _extract_list(m.get("outcomePrices"))
    result = {"up": None, "down": None}
    for outcome, price in zip(outcomes, prices):
        o = str(outcome).lower()
        if "up" in o or o == "yes":
            result["up"] = _clamp_price(price)
        elif "down" in o or o == "no":
            result["down"] = _clamp_price(price)

    for key, side in [("up_price", "up"), ("yes_price", "up"), ("down_price", "down"), ("no_price", "down")]:
        if result.get(side) is None and m.get(key) is not None:
            result[side] = _clamp_price(m.get(key))

    if result["up"] is not None and result["down"] is None:
        result["down"] = _clamp_price(1 - result["up"])
    if result["down"] is not None and result["up"] is None:
        result["up"] = _clamp_price(1 - result["down"])
    return result


async def _clob_price(client: httpx.AsyncClient, token_id: str, side: str = "BUY") -> Optional[float]:
    if not token_id:
        return None
    try:
        r = await client.get(CLOB_PRICE_URL, params={"token_id": str(token_id), "side": side})
        if r.status_code == 200:
            data = r.json()
            val = _clamp_price(data.get("price") if isinstance(data, dict) else None)
            if val is not None:
                return val
    except Exception:
        pass

    # Fallback orderbook
    try:
        r = await client.get(CLOB_BOOK_URL, params={"token_id": str(token_id)})
        if r.status_code == 200:
            data = r.json()
            asks = data.get("asks") or []
            bids = data.get("bids") or []
            rows = asks if side.upper() == "BUY" else bids
            vals = []
            for row in rows:
                if isinstance(row, dict):
                    p = _to_float(row.get("price"))
                elif isinstance(row, (list, tuple)) and row:
                    p = _to_float(row[0])
                else:
                    p = None
                if p is not None:
                    vals.append(p)
            if vals:
                best = min(vals) if side.upper() == "BUY" else max(vals)
                return _clamp_price(best)
    except Exception:
        pass
    return None


async def _event_by_slug(client: httpx.AsyncClient, slug: str) -> Optional[Dict[str, Any]]:
    try:
        r = await client.get(f"{GAMMA_EVENT_SLUG_URL}/{slug}")
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and data:
                return data
    except Exception:
        return None
    return None


async def _search_fallback(client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    # Secondary fallback only. Slug method should be primary.
    searches = ["btc updown", "btc up down", "bitcoin up down"]
    for q in searches:
        try:
            r = await client.get(GAMMA_EVENTS_URL, params={"active": "true", "closed": "false", "limit": 200, "search": q})
            if r.status_code != 200:
                continue
            events = r.json()
            if not isinstance(events, list):
                continue
            for ev in events:
                slug = str(ev.get("slug") or "")
                title = " ".join(str(ev.get(k) or "") for k in ["title", "slug", "ticker"]).lower()
                if ("btc-updown-15m" in slug) or ("bitcoin" in title and "up" in title and "down" in title and "15" in title):
                    return ev
        except Exception:
            continue
    return None


def _normalize(event: Dict[str, Any], slug: str) -> Dict[str, Any]:
    market = _market_from_event(event) or {}
    tokens = _extract_token_ids(market)
    prices = _extract_gamma_prices(market)

    start_date = event.get("startDate") or market.get("startDate") or ""
    end_date = event.get("endDate") or market.get("endDate") or ""
    question = market.get("question") or event.get("title") or event.get("question") or f"BTC Up or Down 15m {slug}"

    # Up/Down crypto markets use the price to beat in the title/rules less reliably.
    # If Gamma has final/reference fields, pass them through; btc.py can still use Coinbase open.
    target = (
        _to_float(event.get("priceToBeat")) or
        _to_float(event.get("targetPrice")) or
        _to_float(market.get("priceToBeat")) or
        _to_float(market.get("targetPrice"))
    )

    return {
        "id": str(market.get("id") or event.get("id") or ""),
        "condition_id": str(market.get("conditionId") or market.get("condition_id") or ""),
        "question": str(question),
        "slug": str(slug),
        "event_slug": str(event.get("slug") or slug),
        "url": f"https://polymarket.com/event/{slug}",
        "start_date": str(start_date or ""),
        "end_date": str(end_date or ""),
        "up_price": prices.get("up"),
        "down_price": prices.get("down"),
        "up_price_source": "gamma" if prices.get("up") is not None else "missing",
        "down_price_source": "gamma" if prices.get("down") is not None else "missing",
        "up_token_id": tokens.get("up"),
        "down_token_id": tokens.get("down"),
        "target_price": target,
        "liquidity": _to_float(market.get("liquidity") or event.get("liquidity") or 0, 0.0) or 0.0,
        "volume": _to_float(market.get("volume") or event.get("volume") or 0, 0.0) or 0.0,
        "raw": market,
    }


async def discover_btc_15m_market() -> Dict[str, Any]:
    now_ts = time.time()
    if _MARKET_CACHE["value"] and now_ts - _MARKET_CACHE["ts"] < 2.0:
        return _MARKET_CACHE["value"]

    headers = {"User-Agent": "PolyScalpBot/1.0", "Accept": "application/json"}
    debug = []

    async with httpx.AsyncClient(timeout=10, headers=headers) as client:
        found_event = None
        found_slug = None

        # Primary: deterministic CURRENT slug first.
        for slug in _candidate_slugs("btc", 15):
            debug.append(slug)
            if not _is_current_or_next_slug(slug, 15):
                continue
            ev = await _event_by_slug(client, slug)
            if ev and _market_from_event(ev):
                found_event = ev
                found_slug = slug
                break

        # Fallback: search only if slug method fails.
        # Only accept search result if its slug is current/next, never stale.
        if not found_event:
            ev = await _search_fallback(client)
            if ev:
                candidate_slug = str(ev.get("slug") or "")
                if _is_current_or_next_slug(candidate_slug, 15):
                    found_event = ev
                    found_slug = candidate_slug
                else:
                    debug.append("ignored stale search result: " + candidate_slug)

        if not found_event:
            value = {
                "up_price": None,
                "down_price": None,
                "up_price_source": "missing",
                "down_price_source": "missing",
                "odds_source": "missing/missing",
                "question": "No BTC 15m market found",
                "slug": "",
                "event_slug": "",
                "url": "",
                "target_price": None,
                "liquidity": 0.0,
                "volume": 0.0,
                "odds_debug": "tried slugs: " + ", ".join(debug[-5:]),
            }
            _MARKET_CACHE.update({"ts": now_ts, "value": value})
            return value

        market = _normalize(found_event, found_slug)

        # Always prefer executable CLOB BUY prices if token IDs exist.
        up_clob = await _clob_price(client, market.get("up_token_id"), "BUY") if market.get("up_token_id") else None
        down_clob = await _clob_price(client, market.get("down_token_id"), "BUY") if market.get("down_token_id") else None

        if up_clob is not None:
            market["up_price"] = up_clob
            market["up_price_source"] = "clob"
        if down_clob is not None:
            market["down_price"] = down_clob
            market["down_price_source"] = "clob"

        if market.get("up_price") is not None and market.get("down_price") is None:
            market["down_price"] = _clamp_price(1 - float(market["up_price"]))
            market["down_price_source"] = "inferred"
        if market.get("down_price") is not None and market.get("up_price") is None:
            market["up_price"] = _clamp_price(1 - float(market["down_price"]))
            market["up_price_source"] = "inferred"

        if market.get("up_price") is not None and market.get("down_price") is not None:
            market["odds_source"] = f"{market.get('up_price_source')}/{market.get('down_price_source')}"
        else:
            market["odds_source"] = "missing/missing"

        market["odds_debug"] = "slug=" + str(found_slug)
        _MARKET_CACHE.update({"ts": now_ts, "value": market})
        return market


async def fetch_public_profile(wallet: str):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(PUBLIC_PROFILE_URL, params={"address": wallet})
        r.raise_for_status()
        return r.json()


def _extract_official_outcome_from_obj(obj: Dict[str, Any]) -> Optional[str]:
    """
    Robustly detect Polymarket official resolved outcome from Gamma event/market payload.
    Returns UP/DOWN only if the payload clearly contains an official settlement.
    """
    if not isinstance(obj, dict):
        return None

    # Direct fields first.
    for key in [
        "winningOutcome", "winner", "winning_outcome", "resolvedOutcome",
        "resolved_outcome", "outcome", "result", "resolution"
    ]:
        value = obj.get(key)
        if value is None:
            continue
        s = str(value).strip().lower()
        if s in ("up", "yes", "higher", "above"):
            return "UP"
        if s in ("down", "no", "lower", "below"):
            return "DOWN"
        if "up" == s or "up " in s or " up" in s:
            return "UP"
        if "down" == s or "down " in s or " down" in s:
            return "DOWN"

    # Outcome objects / token objects sometimes carry winning flags.
    for list_key in ["outcomes", "tokens", "outcomeTokens"]:
        raw = parse_json_field(obj.get(list_key))
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                won_flag = (
                    item.get("winner") is True or
                    item.get("winning") is True or
                    item.get("isWinner") is True or
                    str(item.get("status", "")).lower() in ("winner", "won")
                )
                if won_flag:
                    name = str(item.get("name") or item.get("outcome") or item.get("title") or "").lower()
                    if "up" in name or name == "yes":
                        return "UP"
                    if "down" in name or name == "no":
                        return "DOWN"

    return None


def _event_is_final(obj: Dict[str, Any]) -> bool:
    if not isinstance(obj, dict):
        return False
    for key in ["resolved", "closed", "archived"]:
        if obj.get(key) is True:
            return True
    for key in ["status", "state"]:
        s = str(obj.get(key) or "").lower()
        if s in ("resolved", "closed", "finalized", "settled"):
            return True
    # If there is an explicit winning outcome, treat as final.
    return _extract_official_outcome_from_obj(obj) is not None


async def fetch_market_resolution(slug: str) -> Dict[str, Any]:
    """
    Official Polymarket/Gamma resolution by market slug.

    IMPORTANT:
    Paper trading must use this, not local BTC-vs-target comparison.
    Returns:
      {"resolved": True, "outcome": "UP"/"DOWN", ...}
      or {"resolved": False, "outcome": None, ...}
    """
    slug = str(slug or "").strip()
    if not slug:
        return {"resolved": False, "outcome": None, "source": "missing_slug", "slug": slug}

    headers = {"User-Agent": "PolyScalpBot/1.0", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=10, headers=headers) as client:
        ev = await _event_by_slug(client, slug)

    if not ev:
        return {"resolved": False, "outcome": None, "source": "gamma_event_missing", "slug": slug}

    market = _market_from_event(ev) or {}

    # Detect official outcome from event first, then nested market.
    outcome = _extract_official_outcome_from_obj(ev) or _extract_official_outcome_from_obj(market)
    final = _event_is_final(ev) or _event_is_final(market)

    # Some payloads have one of two outcome prices/volumes set to 1 after settlement.
    if outcome is None and final:
        prices = _extract_gamma_prices(market)
        up = prices.get("up")
        down = prices.get("down")
        if up is not None and down is not None:
            if up >= 0.99 and down <= 0.01:
                outcome = "UP"
            elif down >= 0.99 and up <= 0.01:
                outcome = "DOWN"

    return {
        "resolved": bool(final and outcome in ("UP", "DOWN")),
        "outcome": outcome if outcome in ("UP", "DOWN") else None,
        "source": "gamma_official",
        "slug": slug,
        "event_closed": bool(ev.get("closed") or ev.get("resolved") or ev.get("archived")),
        "event_status": str(ev.get("status") or ev.get("state") or ""),
        "question": str(market.get("question") or ev.get("title") or ev.get("question") or ""),
    }
