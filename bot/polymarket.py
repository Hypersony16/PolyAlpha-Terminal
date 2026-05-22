import json
import re
import time
from typing import Any, Dict, Optional, List, Tuple

import httpx


GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_PRICE_URL = "https://clob.polymarket.com/price"
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


def _norm_outcome(x: Any) -> str:
    s = str(x or "").strip().lower()
    if s in ("yes", "up", "higher", "above", "green"):
        return "up"
    if s in ("no", "down", "lower", "below", "red"):
        return "down"
    if "up" in s or "above" in s or "higher" in s:
        return "up"
    if "down" in s or "below" in s or "lower" in s:
        return "down"
    return s


def _extract_outcome_map(outcomes, values) -> Dict[str, Any]:
    outcomes = parse_json_field(outcomes)
    values = parse_json_field(values)

    result = {}
    if not isinstance(outcomes, list) or not isinstance(values, list):
        return result

    for outcome, value in zip(outcomes, values):
        key = _norm_outcome(outcome)
        if key:
            result[key] = value
    return result


def _extract_prices(outcomes, prices) -> Dict[str, float]:
    raw = _extract_outcome_map(outcomes, prices)
    result = {}
    for k, v in raw.items():
        val = _to_float(v)
        if val is not None:
            result[k] = max(0.01, min(0.99, val))
    return result


def _extract_token_ids(outcomes, token_ids) -> Dict[str, str]:
    raw = _extract_outcome_map(outcomes, token_ids)
    return {k: str(v) for k, v in raw.items() if v is not None and str(v)}


def _looks_like_btc_15m(text: str) -> bool:
    t = text.lower()
    if not any(x in t for x in ["btc", "bitcoin"]):
        return False
    if not any(x in t for x in ["15m", "15 min", "15-minute", "15 minute"]):
        return False
    if not any(x in t for x in ["up or down", "up/down", "up-down", "up", "down"]):
        return False
    return True


def _market_sort_key(m: Dict[str, Any]):
    volume = _to_float(m.get("volume") or m.get("volumeNum"), 0.0) or 0.0
    liquidity = _to_float(m.get("liquidity") or m.get("liquidityNum"), 0.0) or 0.0
    end = str(m.get("endDate") or m.get("end_date") or "")
    return (liquidity + volume, end, str(m.get("id") or ""))


def _parse_target_price(m: Dict[str, Any]) -> Optional[float]:
    """
    Attempts to find the 'price to beat'/target from Gamma text.
    This is critical for BTC Up/Down markets.
    """
    fields = [
        m.get("question"),
        m.get("title"),
        m.get("description"),
        m.get("rules"),
        m.get("resolutionSource"),
        m.get("slug"),
    ]

    raw = " ".join(str(x or "") for x in fields)

    # Prefer numbers near "price to beat", "target", "above", "below".
    patterns = [
        r"(?:price\s*to\s*beat|target|strike|above|below)[^\d$]{0,40}\$?\s*([0-9]{2,3}[, ]?[0-9]{3}(?:\.[0-9]+)?)",
        r"\$?\s*([0-9]{2,3}[, ]?[0-9]{3}(?:\.[0-9]+)?)[^\n]{0,40}(?:price\s*to\s*beat|target|strike|above|below)",
        r"btc[^\d$]{0,60}\$?\s*([0-9]{2,3}[, ]?[0-9]{3}(?:\.[0-9]+)?)",
    ]

    for pat in patterns:
        for match in re.finditer(pat, raw, flags=re.I):
            val = _to_float(match.group(1).replace(",", "").replace(" ", ""))
            if val and 10000 < val < 200000:
                return val

    # Last fallback: all BTC-looking prices; choose first plausible.
    nums = re.findall(r"\$?\s*([0-9]{2,3}[, ]?[0-9]{3}(?:\.[0-9]+)?)", raw)
    for n in nums:
        val = _to_float(n.replace(",", "").replace(" ", ""))
        if val and 10000 < val < 200000:
            return val

    return None


async def _clob_price(client: httpx.AsyncClient, token_id: str, side: str = "BUY") -> Optional[float]:
    if not token_id:
        return None

    try:
        r = await client.get(CLOB_PRICE_URL, params={"token_id": token_id, "side": side})
        r.raise_for_status()
        data = r.json()
        price = data.get("price") if isinstance(data, dict) else None
        return _to_float(price)
    except Exception:
        return None


async def _fill_clob_prices(market: Dict[str, Any]):
    """
    Adds buy prices from CLOB if Gamma outcomePrices are missing or stale.
    For paper execution, buy price is what matters.
    """
    up_token = market.get("up_token_id")
    down_token = market.get("down_token_id")

    async with httpx.AsyncClient(timeout=6) as client:
        up_clob = await _clob_price(client, up_token, "BUY") if up_token else None
        down_clob = await _clob_price(client, down_token, "BUY") if down_token else None

    if up_clob is not None:
        market["up_price"] = max(0.01, min(0.99, float(up_clob)))
        market["up_price_source"] = "clob"
    if down_clob is not None:
        market["down_price"] = max(0.01, min(0.99, float(down_clob)))
        market["down_price_source"] = "clob"

    # If only one side exists, infer the other.
    if market.get("up_price") is not None and market.get("down_price") is None:
        market["down_price"] = max(0.01, min(0.99, 1.0 - float(market["up_price"])))
        market["down_price_source"] = "inferred"
    if market.get("down_price") is not None and market.get("up_price") is None:
        market["up_price"] = max(0.01, min(0.99, 1.0 - float(market["down_price"])))
        market["up_price_source"] = "inferred"


def _normalize_market(chosen: Dict[str, Any]) -> Dict[str, Any]:
    prices = _extract_prices(chosen.get("outcomes", []), chosen.get("outcomePrices", []))
    token_ids = _extract_token_ids(chosen.get("outcomes", []), chosen.get("clobTokenIds", []))

    up_price = prices.get("up")
    down_price = prices.get("down")

    if up_price is None:
        up_price = _to_float(chosen.get("up_price") or chosen.get("yes_price"))
    if down_price is None:
        down_price = _to_float(chosen.get("down_price") or chosen.get("no_price"))

    if up_price is not None and down_price is None:
        down_price = max(0.01, min(0.99, 1.0 - float(up_price)))
    if down_price is not None and up_price is None:
        up_price = max(0.01, min(0.99, 1.0 - float(down_price)))

    return {
        "id": str(chosen.get("id") or ""),
        "condition_id": str(chosen.get("conditionId") or chosen.get("condition_id") or ""),
        "question": str(chosen.get("question") or chosen.get("title") or "BTC 15m Up/Down"),
        "slug": str(chosen.get("slug") or ""),
        "url": "https://polymarket.com/event/" + str(chosen.get("slug") or ""),
        "up_price": up_price,
        "down_price": down_price,
        "up_price_source": "gamma" if up_price is not None else "missing",
        "down_price_source": "gamma" if down_price is not None else "missing",
        "up_token_id": token_ids.get("up"),
        "down_token_id": token_ids.get("down"),
        "target_price": _parse_target_price(chosen),
        "up_bid": _to_float(chosen.get("bestBid")),
        "up_ask": _to_float(chosen.get("bestAsk")),
        "down_bid": None,
        "down_ask": None,
        "volume": _to_float(chosen.get("volume") or chosen.get("volumeNum"), 0.0) or 0.0,
        "liquidity": _to_float(chosen.get("liquidity") or chosen.get("liquidityNum"), 0.0) or 0.0,
        "raw": chosen,
    }


async def discover_btc_15m_market() -> Dict[str, Any]:
    """
    BTC 15m market discovery with real odds.
    Uses Gamma first and CLOB fallback for BUY prices.
    Returns {} if missing, never raises for missing markets.
    """
    now = time.time()
    if _MARKET_CACHE["value"] and now - _MARKET_CACHE["ts"] < 2.5:
        return _MARKET_CACHE["value"]

    candidates: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(GAMMA_EVENTS_URL, params={"active": "true", "closed": "false", "limit": 250})
            r.raise_for_status()
            events = r.json()
            if isinstance(events, list):
                for ev in events:
                    ev_text = " ".join(str(ev.get(k, "")) for k in ["title", "slug", "ticker", "description"])
                    markets = parse_json_field(ev.get("markets", []))
                    if isinstance(markets, list):
                        for m in markets:
                            text = ev_text + " " + " ".join(str(m.get(k, "")) for k in ["question", "slug", "title", "description"])
                            if _looks_like_btc_15m(text):
                                # inherit useful text for target parsing
                                if ev.get("description") and not m.get("description"):
                                    m["description"] = ev.get("description")
                                candidates.append(m)
        except Exception:
            pass

        for search in ["BTC Up or Down 15m", "Bitcoin Up or Down 15m", "btc 15m"]:
            try:
                r = await client.get(GAMMA_MARKETS_URL, params={"active": "true", "closed": "false", "limit": 100, "search": search})
                r.raise_for_status()
                markets = r.json()
                if isinstance(markets, list):
                    for m in markets:
                        text = " ".join(str(m.get(k, "")) for k in ["question", "slug", "title", "description"])
                        if _looks_like_btc_15m(text):
                            candidates.append(m)
            except Exception:
                continue

    if not candidates:
        _MARKET_CACHE.update({"ts": now, "value": {}})
        return {}

    uniq = {}
    for m in candidates:
        key = str(m.get("id") or m.get("conditionId") or m.get("slug") or id(m))
        uniq[key] = m

    chosen = sorted(uniq.values(), key=_market_sort_key, reverse=True)[0]
    market = _normalize_market(chosen)

    # CLOB improves odds if Gamma missing.
    if market.get("up_price") is None or market.get("down_price") is None:
        await _fill_clob_prices(market)

    _MARKET_CACHE.update({"ts": now, "value": market})
    return market


async def fetch_public_profile(wallet: str):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(PUBLIC_PROFILE_URL, params={"address": wallet})
        r.raise_for_status()
        return r.json()
