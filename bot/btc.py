import math
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import httpx


BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

_PRICE_CACHE = {"ts": 0.0, "value": None}
_KLINES_CACHE = {"ts": 0.0, "value": None}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


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


def market_phase(left_sec: int) -> str:
    if left_sec > 720:
        return "Early"
    if left_sec > 180:
        return "Prime"
    if left_sec > 60:
        return "Late"
    return "Danger"


async def fetch_btc_price() -> float:
    now = time.time()
    if _PRICE_CACHE["value"] is not None and now - _PRICE_CACHE["ts"] < 2.0:
        return float(_PRICE_CACHE["value"])

    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.get(BINANCE_PRICE_URL, params={"symbol": "BTCUSDT"})
        r.raise_for_status()
        data = r.json()

    price = float(data["price"])
    _PRICE_CACHE.update({"ts": now, "value": price})
    return price


async def fetch_btc_klines(limit: int = 64) -> list:
    now = time.time()
    if _KLINES_CACHE["value"] is not None and now - _KLINES_CACHE["ts"] < 10.0:
        return _KLINES_CACHE["value"]

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            BINANCE_KLINES_URL,
            params={"symbol": "BTCUSDT", "interval": "1m", "limit": limit},
        )
        r.raise_for_status()
        data = r.json()

    _KLINES_CACHE.update({"ts": now, "value": data})
    return data


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, x))))


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _kelly_fraction(p: float, market_prob: float) -> float:
    # Binary market approximate Kelly. Cap hard for Telegram signal safety.
    q = 1.0 - p
    b = (1.0 / max(market_prob, 0.01)) - 1.0
    if b <= 0:
        return 0.0
    f = (b * p - q) / b
    return _clamp(f * 0.25, 0.0, 0.05)  # quarter Kelly, max 5%


async def build_btc_model(market: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    price = await fetch_btc_price()
    candles = await fetch_btc_klines()
    window = current_15m_window()

    closes = [float(c[4]) for c in candles]
    opens = [float(c[1]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]

    current_open = opens[-1]
    if len(candles) >= 16:
        # approximate 15m window open from 15 one-minute candles ago
        current_open = opens[-min(15, len(candles))]

    ret_1m = math.log(closes[-1] / closes[-2]) if len(closes) > 2 else 0.0
    ret_5m = math.log(closes[-1] / closes[-6]) if len(closes) > 6 else 0.0
    ret_15m = math.log(closes[-1] / closes[-16]) if len(closes) > 16 else 0.0
    distance = math.log(price / current_open)

    returns = []
    for i in range(max(1, len(closes) - 30), len(closes)):
        if i > 0:
            returns.append(math.log(closes[i] / closes[i - 1]))

    realized_vol_1m = (sum((r - (sum(returns) / len(returns))) ** 2 for r in returns) / max(1, len(returns) - 1)) ** 0.5 if len(returns) > 2 else 0.0008
    vol_cal = historical_volatility_calibration(candles)
    remaining_min = max(1.0, window["left_sec"] / 60.0)
    sigma_remaining = max(0.00015, realized_vol_1m * math.sqrt(remaining_min))

    # Probability current market closes UP vs opening price.
    z = distance / sigma_remaining
    base_up_prob = _normal_cdf(z)

    # Momentum adjustment. Kept intentionally small to avoid overfitting.
    mom_score = (ret_1m * 8.0) + (ret_5m * 3.0) + (ret_15m * 1.5)
    momentum_adj = (_sigmoid(mom_score * 130.0) - 0.5) * 0.16
    model_up = _clamp(base_up_prob + momentum_adj, 0.03, 0.97)
    model_down = 1.0 - model_up

    up_market = None
    down_market = None
    if market:
        up_market = market.get("up_price")
        down_market = market.get("down_price")

    if up_market is None:
        up_market = 0.50
    if down_market is None:
        down_market = 1.0 - up_market

    up_edge = model_up - float(up_market)
    down_edge = model_down - float(down_market)

    if up_edge >= down_edge:
        side = "UP"
        model_prob = model_up
        market_prob = float(up_market)
        edge = up_edge
    else:
        side = "DOWN"
        model_prob = model_down
        market_prob = float(down_market)
        edge = down_edge

    confidence_score = abs(edge) * 1.2 + abs(distance) / max(sigma_remaining, 1e-9) * 0.08
    if window["phase"] == "Danger":
        confidence_score *= 0.65
    elif window["phase"] == "Early":
        confidence_score *= 0.8

    if confidence_score >= 0.16:
        confidence = "High"
    elif confidence_score >= 0.075:
        confidence = "Medium"
    else:
        confidence = "Low"

    kelly = _kelly_fraction(model_prob, market_prob)
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
        "open": current_open,
        "distance_pct": distance * 100.0,
        "ret_1m_pct": ret_1m * 100.0,
        "ret_5m_pct": ret_5m * 100.0,
        "vol_1m_pct": realized_vol_1m * 100.0,
        "vol_regime": vol_cal["regime"],
        "sigma_remaining_pct": sigma_remaining * 100.0,
        "model_up": model_up,
        "model_down": model_down,
        "market_up": float(up_market),
        "market_down": float(down_market),
        "signal": side,
        "model_prob": model_prob,
        "market_prob": market_prob,
        "edge": edge,
        "confidence": confidence,
        "confidence_score": confidence_score,
        "suggested_size_pct": suggested_size_pct,
        "maker_combined_bid": maker_combined_bid,
        "maker_edge": maker_edge,
        "market": market or {},
    }


def format_btc_price(price: float) -> str:
    return f"${price:,.2f}"


def format_pct(x: float) -> str:
    return f"{x * 100.0:.1f}%"


def historical_volatility_calibration(candles: list) -> dict:
    closes = [float(c[4]) for c in candles]
    returns = []
    for i in range(1, len(closes)):
        returns.append(math.log(closes[i] / closes[i - 1]))
    if not returns:
        return {"regime": "Unknown", "vol_1m": 0.0}
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1)
    vol = var ** 0.5
    if vol < 0.00035:
        regime = "Low"
    elif vol < 0.0009:
        regime = "Normal"
    else:
        regime = "High"
    return {"regime": regime, "vol_1m": vol}
