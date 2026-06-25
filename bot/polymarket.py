import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

import httpx


GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_EVENT_SLUG_URL = "https://gamma-api.polymarket.com/events/slug"
GAMMA_MARKET_SLUG_URL = "https://gamma-api.polymarket.com/markets/slug"
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
    slugs = []
    for offset in [0, interval, -interval, interval * 2, -interval * 2]:
        ts = base + offset
        slugs.append(f"{asset}-updown-{duration}m-{ts}")
    return slugs


async def _fetch_market_by_slug(slug: str, client: httpx.AsyncClient) -> Optional[Dict]:
    try:
        r = await client.get(GAMMA_MARKET_SLUG_URL, params={"slug": slug}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict) and data:
                return data
    except Exception:
        pass
    return None


async def _fetch_clob_price(token_id: str, client: httpx.AsyncClient) -> Optional[float]:
    if not token_id:
        return None
    try:
        r = await client.get(CLOB_PRICE_URL, params={"token_id": token_id, "side": "buy"}, timeout=5)
        if r.status_code == 200:
            data = r.json()
            price = _to_float(data.get("price"))
            if price is not None:
                return _clamp_price(price)
    except Exception:
        pass
    return None


async def discover_btc_15m_market() -> Dict[str, Any]:
    now = time.time()
    if _MARKET_CACHE["value"] and now - _MARKET_CACHE["ts"] < 5:
        return _MARKET_CACHE["value"]

    slugs = _candidate_slugs("btc", 15)

    async with httpx.AsyncClient(timeout=12) as client:
        for slug in slugs:
            market = await _fetch_market_by_slug(slug, client)
            if not market:
                continue

            active = market.get("active", True)
            closed = market.get("closed", False)
            if closed:
                continue

            outcomes = _extract_list(market.get("outcomes"))
            tokens = _extract_list(market.get("clobTokenIds") or market.get("tokens"))

            up_token_id = ""
            down_token_id = ""
            if tokens and len(tokens) >= 2:
                up_token_id = str(tokens[0]) if isinstance(tokens[0], str) else str(tokens[0].get("token_id", ""))
                down_token_id = str(tokens[1]) if isinstance(tokens[1], str) else str(tokens[1].get("token_id", ""))

            up_price = None
            down_price = None

            # Try CLOB prices first
            if up_token_id:
                up_price = await _fetch_clob_price(up_token_id, client)
            if down_token_id:
                down_price = await _fetch_clob_price(down_token_id, client)

            # Fall back to outcomePrices
            if up_price is None or down_price is None:
                outcome_prices = _extract_list(market.get("outcomePrices"))
                if len(outcome_prices) >= 2:
                    if up_price is None:
                        up_price = _clamp_price(outcome_prices[0])
                    if down_price is None:
                        down_price = _clamp_price(outcome_prices[1])

            if up_price is None:
                up_price = 0.5
            if down_price is None:
                down_price = round(1 - up_price, 4)

            end_date_iso = market.get("endDate") or market.get("endDateIso") or ""
            time_left_seconds = 0
            if end_date_iso:
                try:
                    end_dt = datetime.fromisoformat(str(end_date_iso).replace("Z", "+00:00"))
                    time_left_seconds = max(0, int((end_dt - datetime.now(timezone.utc)).total_seconds()))
                except Exception:
                    pass

            liquidity = _to_float(market.get("liquidity") or market.get("volume"), 0.0) or 0.0

            result = {
                "slug": slug,
                "question": market.get("question", ""),
                "up_price": round(up_price, 4),
                "down_price": round(down_price, 4),
                "yes_price": round(up_price, 4),
                "no_price": round(down_price, 4),
                "time_left_seconds": time_left_seconds,
                "liquidity": liquidity,
                "active": active,
                "up_token_id": up_token_id,
                "down_token_id": down_token_id,
                "raw": market,
            }
            _MARKET_CACHE.update({"ts": time.time(), "value": result})
            return result

    return {}


async def fetch_public_profile(wallet: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(PUBLIC_PROFILE_URL, params={"address": wallet})
            if r.status_code == 200:
                return r.json() or {}
        except Exception:
            pass
    return {}


async def fetch_market_resolution(slug: str) -> Optional[str]:
    """Returns 'UP', 'DOWN', or None if not yet resolved."""
    async with httpx.AsyncClient(timeout=10) as client:
        market = await _fetch_market_by_slug(slug, client)
        if not market:
            return None

        resolved = market.get("resolved", False)
        if not resolved:
            return None

        winner = market.get("winner") or market.get("resolvedOutcome") or ""
        if isinstance(winner, str):
            w = winner.upper()
            if "UP" in w or "YES" in w:
                return "UP"
            if "DOWN" in w or "NO" in w:
                return "DOWN"

        outcome_prices = _extract_list(market.get("outcomePrices"))
        if len(outcome_prices) >= 2:
            p0 = _to_float(outcome_prices[0], 0)
            p1 = _to_float(outcome_prices[1], 0)
            if p0 is not None and p1 is not None:
                return "UP" if p0 > p1 else "DOWN"

    return None
