import math
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import httpx


COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker"
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"

_PRICE_CACHE = {"ts": 0.0, "value": None}
_KLINES_CACHE = {"ts": 0.0, "value": None}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def market_phase(left_sec: int) -> str:
    if left_sec > 720:
        return "Early"
    if left_sec > 180:
        return "Prime"
    if left_sec > 60:
        return "Late"
    return "Danger"


def current_15m_window() -> Dict[str, Any]:
    now = _now_utc()
    minute_floor = (now.minute // 15) * 15
    start = now.replace(minute=minute_floor, second=0, microsecond=0)
    elapsed = int((now - start).total_seconds())
    left = max(0, 900 - elapsed)
    return {
        "start": start,
        "elapsed_sec": elapsed,
        "left_sec": left,
        "left_label": f"{left // 60}m {left % 60}s",
        "phase": market_phase(left),
    }


async def fetch_btc_price() -> float:
    now = time.time()
    if _PRICE_CACHE["value"] is not None and now - _PRICE_CACHE["ts"] < 1.5:
        return float(_PRICE_CACHE["value"])

    async with httpx.AsyncClient(timeout=7) as client:
        try:
            r = await client.get(COINBASE_SPOT_URL)
            r.raise_for_status()
            data = r.json()
            price = float(data["data"]["amount"])
            _PRICE_CACHE.update({"ts": now, "value": price})
            return price
        except Exception:
            pass

        try:
            r = await client.get(KRAKEN_TICKER_URL, params={"pair": "XBTUSD"})
            r.raise_for_status()
            data = r.json()
            result = data.get("result", {})
            first_key = next(iter(result))
            price = float(result[first_key]["c"][0])
            _PRICE_CACHE.update({"ts": now, "value": price})
            return price
        except Exception:
            pass

        try:
            r = await client.get(COINGECKO_PRICE_URL, params={"ids": "bitcoin", "vs_currencies": "usd"})
            r.raise_for_status()
            data = r.json()
            price = float(data["bitcoin"]["usd"])
            _PRICE_CACHE.update({"ts": now, "value": price})
            return price
        except Exception:
            pass

    raise RuntimeError("All BTC price providers failed")


async def fetch_btc_klines(limit: int = 90) -> list:
    now = time.time()
    if _KLINES_CACHE["value"] is not None and now - _KLINES_CACHE["ts"] < 6.0:
        return _KLINES_CACHE["value"]

    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.get(COINBASE_CANDLES_URL, params={"granularity": 60})
        r.raise_for_status()
        data = r.json()

    data = sorted(data, key=lambda x: x[0])[-limit:]

    candles = []
    for c in data:
        candles.append([
            c[0],
            float(c[3]),  # open
            float(c[2]),  # high
            float(c[1]),  # low
            float(c[4]),  # close
            float(c[5]),  # volume
        ])

    _KLINES_CACHE.update({"ts": now, "value": candles})
    return candles


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, x))))


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _kelly_fraction(p: float, entry_price: float) -> float:
    """
    Kelly for binary contract:
    cost = entry_price, payout = 1.
    b = payout/cost - 1.
    Quarter Kelly, capped.
    """
    p = _clamp(p, 0.01, 0.99)
    entry_price = _clamp(entry_price, 0.01, 0.99)
    b = (1.0 / entry_price) - 1.0
    q = 1.0 - p
    if b <= 0:
        return 0.0
    f = (b * p - q) / b
    return _clamp(f * 0.25, 0.0, 0.05)


def _vol_regime(vol_1m: float) -> str:
    if vol_1m < 0.00035:
        return "Low"
    if vol_1m < 0.0009:
        return "Normal"
    return "High"


def _binary_ev(model_prob: float, entry_price: float) -> float:
    """
    Expected value per $1 staked.
    shares = 1 / entry_price
    expected payout = model_prob * shares
    EV = expected payout - 1
    """
    entry_price = _clamp(entry_price, 0.01, 0.99)
    return (model_prob / entry_price) - 1.0


async def build_btc_model(market: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    price = await fetch_btc_price()
    candles = await fetch_btc_klines()
    window = current_15m_window()

    closes = [float(c[4]) for c in candles]
    opens = [float(c[1]) for c in candles]

    if not closes:
        raise RuntimeError("No BTC candle data returned")

    fallback_open = opens[-1]
    if len(candles) >= 16:
        fallback_open = opens[-min(15, len(candles))]

    target_price = None
    if market:
        try:
            target_price = float(market.get("target_price")) if market.get("target_price") else None
        except Exception:
            target_price = None

    # Real market target is preferred. Fallback to local candle open if missing.
    price_to_beat = target_price or fallback_open

    ret_1m = math.log(closes[-1] / closes[-2]) if len(closes) > 2 else 0.0
    ret_3m = math.log(closes[-1] / closes[-4]) if len(closes) > 4 else 0.0
    ret_5m = math.log(closes[-1] / closes[-6]) if len(closes) > 6 else 0.0
    ret_15m = math.log(closes[-1] / closes[-16]) if len(closes) > 16 else 0.0
    distance = math.log(price / price_to_beat) if price_to_beat else 0.0

    returns = []
    for i in range(max(1, len(closes) - 45), len(closes)):
        if i > 0:
            returns.append(math.log(closes[i] / closes[i - 1]))

    if len(returns) > 2:
        mean = sum(returns) / len(returns)
        realized_vol_1m = (sum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1)) ** 0.5
    else:
        realized_vol_1m = 0.0008

    remaining_min = max(1.0, window["left_sec"] / 60.0)
    sigma_remaining = max(0.00020, realized_vol_1m * math.sqrt(remaining_min))

    # Probability final BTC > target at settlement.
    z = distance / sigma_remaining
    base_up_prob = _normal_cdf(z)

    # Small calibrated momentum adjustment only. Prevents fake 97% spam.
    mom_score = (ret_1m * 5.0) + (ret_3m * 2.5) + (ret_5m * 1.5) + (ret_15m * 0.5)
    momentum_adj = (_sigmoid(mom_score * 80.0) - 0.5) * 0.10

    # Time danger reduces confidence, not always direction.
    raw_up = base_up_prob + momentum_adj
    model_up = _clamp(raw_up, 0.08, 0.92)
    model_down = 1.0 - model_up

    # Real Polymarket odds. If missing, mark as fallback and use 50/50.
    odds_source = "fallback_50"
    up_market = None
    down_market = None
    if market:
        up_market = market.get("up_price")
        down_market = market.get("down_price")
        if market.get("up_price_source") or market.get("down_price_source"):
            odds_source = f"{market.get('up_price_source','?')}/{market.get('down_price_source','?')}"

    if up_market is None and down_market is None:
        up_market = 0.50
        down_market = 0.50
    elif up_market is None:
        down_market = _clamp(float(down_market), 0.01, 0.99)
        up_market = 1.0 - down_market
    elif down_market is None:
        up_market = _clamp(float(up_market), 0.01, 0.99)
        down_market = 1.0 - up_market
    else:
        up_market = _clamp(float(up_market), 0.01, 0.99)
        down_market = _clamp(float(down_market), 0.01, 0.99)

    up_edge = model_up - up_market
    down_edge = model_down - down_market

    up_ev = _binary_ev(model_up, up_market)
    down_ev = _binary_ev(model_down, down_market)

    # Choose the better actual bet, not just direction.
    if up_ev >= down_ev:
        side = "UP"
        model_prob = model_up
        market_prob = up_market
        edge = up_edge
        ev_per_dollar = up_ev
        entry_price = up_market
    else:
        side = "DOWN"
        model_prob = model_down
        market_prob = down_market
        edge = down_edge
        ev_per_dollar = down_ev
        entry_price = down_market

    # Calibrated confidence.
    abs_edge = max(0.0, edge)
    if ev_per_dollar >= 0.12 and abs_edge >= 0.10 and model_prob >= 0.62:
        confidence = "High"
    elif ev_per_dollar >= 0.04 and abs_edge >= 0.05 and model_prob >= 0.56:
        confidence = "Medium"
    else:
        confidence = "Low"

    if window["phase"] == "Danger":
        confidence = "Low"

    kelly = _kelly_fraction(model_prob, entry_price)
    suggested_size_pct = round(kelly * 100.0, 2)

    maker_combined_bid = None
    maker_edge = None
    if market and market.get("up_bid") is not None and market.get("down_bid") is not None:
        maker_combined_bid = float(market["up_bid"]) + float(market["down_bid"])
        maker_edge = max(0.0, 1.0 - maker_combined_bid)

    return {
        "asset": "BTC",
        "price": price,
        "window": window,
        "open": price_to_beat,       # used as target/resolution price
        "target_price": price_to_beat,
        "target_source": "polymarket" if target_price else "fallback_open",
        "distance_pct": distance * 100.0,
        "ret_1m_pct": ret_1m * 100.0,
        "ret_5m_pct": ret_5m * 100.0,
        "momentum_5m": ret_5m,
        "momentum_15m": ret_15m,
        "rsi": 50.0,
        "vol_1m_pct": realized_vol_1m * 100.0,
        "vol_regime": _vol_regime(realized_vol_1m),
        "sigma_remaining_pct": sigma_remaining * 100.0,
        "model_up": model_up,
        "model_down": model_down,
        "market_up": up_market,
        "market_down": down_market,
        "entry_price": entry_price,
        "odds_source": odds_source,
        "signal": side,
        "model_prob": model_prob,
        "market_prob": market_prob,
        "edge": edge,
        "ev_per_dollar": ev_per_dollar,
        "confidence": confidence,
        "confidence_score": max(0.0, ev_per_dollar),
        "suggested_size_pct": suggested_size_pct,
        "maker_combined_bid": maker_combined_bid,
        "maker_edge": maker_edge,
        "market": market or {},
    }


def format_btc_price(price: float) -> str:
    return f"${price:,.2f}"


def format_pct(x: float) -> str:
    return f"{x * 100.0:.1f}%"
