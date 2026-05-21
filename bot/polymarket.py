import json
import re
from typing import Any, Dict, Optional, List

import httpx


GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
PUBLIC_PROFILE_URL = "https://gamma-api.polymarket.com/public-profile"


def parse_json_field(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _to_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def _extract_prices(outcomes, prices) -> Dict[str, float]:
    outcomes = parse_json_field(outcomes)
    prices = parse_json_field(prices)
    result = {}
    if not isinstance(outcomes, list) or not isinstance(prices, list):
        return result
    for outcome, price in zip(outcomes, prices):
        key = str(outcome).strip().lower()
        val = _to_float(price)
        if val is not None:
            result[key] = val
    return result


def _looks_like_btc_15m(text: str) -> bool:
    t = text.lower()
    if not any(x in t for x in ["btc", "bitcoin"]):
        return False
    if not any(x in t for x in ["15m", "15 min", "15-minute", "15 minute"]):
        return False
    if not any(x in t for x in ["up or down", "up/down", "up-down", "up"]):
        return False
    return True


def _market_sort_key(m: Dict[str, Any]):
    # Prefer active open markets with liquidity/volume and newest-ish ids.
    volume = _to_float(m.get("volume") or m.get("volumeNum"), 0.0) or 0.0
    liquidity = _to_float(m.get("liquidity") or m.get("liquidityNum"), 0.0) or 0.0
    return (liquidity + volume, str(m.get("endDate") or m.get("end_date") or ""), str(m.get("id") or ""))


async def discover_btc_15m_market() -> Dict[str, Any]:
    """
    BTC 15m only. Returns {} if not found. Never raises for missing markets.
    """
    candidates: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=12) as client:
        # Events first usually contain nested markets.
        try:
            r = await client.get(GAMMA_EVENTS_URL, params={"active": "true", "closed": "false", "limit": 200})
            r.raise_for_status()
            events = r.json()
            if isinstance(events, list):
                for ev in events:
                    ev_text = " ".join(str(ev.get(k, "")) for k in ["title", "slug", "ticker", "description"])
                    markets = parse_json_field(ev.get("markets", []))
                    if isinstance(markets, list):
                        for m in markets:
                            text = ev_text + " " + " ".join(str(m.get(k, "")) for k in ["question", "slug", "title"])
                            if _looks_like_btc_15m(text):
                                candidates.append(m)
        except Exception:
            pass

        # Markets fallback.
        try:
            for search in ["bitcoin up or down 15m", "btc up or down 15m", "btc 15m"]:
                r = await client.get(GAMMA_MARKETS_URL, params={"active": "true", "closed": "false", "limit": 100, "search": search})
                r.raise_for_status()
                markets = r.json()
                if isinstance(markets, list):
                    for m in markets:
                        text = " ".join(str(m.get(k, "")) for k in ["question", "slug", "title", "description"])
                        if _looks_like_btc_15m(text):
                            candidates.append(m)
        except Exception:
            pass

    if not candidates:
        return {}

    # dedupe by id/slug
    uniq = {}
    for m in candidates:
        key = str(m.get("id") or m.get("conditionId") or m.get("slug") or id(m))
        uniq[key] = m
    chosen = sorted(uniq.values(), key=_market_sort_key, reverse=True)[0]

    prices = _extract_prices(chosen.get("outcomes", []), chosen.get("outcomePrices", []))
    up_price = prices.get("up") or prices.get("yes")
    down_price = prices.get("down") or prices.get("no")

    # bestBid/bestAsk can be top-level for single binary markets, but not always split by side.
    # Keep them optional; the model degrades gracefully.
    return {
        "id": str(chosen.get("id") or ""),
        "condition_id": str(chosen.get("conditionId") or chosen.get("condition_id") or ""),
        "question": str(chosen.get("question") or chosen.get("title") or "BTC 15m Up/Down"),
        "slug": str(chosen.get("slug") or ""),
        "url": "https://polymarket.com/event/" + str(chosen.get("slug") or ""),
        "up_price": up_price,
        "down_price": down_price,
        "up_bid": _to_float(chosen.get("bestBid")),
        "up_ask": _to_float(chosen.get("bestAsk")),
        "down_bid": None,
        "down_ask": None,
        "volume": _to_float(chosen.get("volume") or chosen.get("volumeNum"), 0.0) or 0.0,
        "liquidity": _to_float(chosen.get("liquidity") or chosen.get("liquidityNum"), 0.0) or 0.0,
        "raw": chosen,
    }


async def fetch_public_profile(wallet: str):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(PUBLIC_PROFILE_URL, params={"address": wallet})
        r.raise_for_status()
        return r.json()
