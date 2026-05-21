import math
from datetime import datetime, timedelta, timezone

import httpx


_PRICE_CACHE = {"value": None, "ts": 0}


def clamp(value, low, high):
    return max(low, min(high, value))


async def fetch_btc_price():
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
            r.raise_for_status()
            data = r.json()
            return float(data["data"]["amount"])
        except Exception:
            pass

        try:
            r = await client.get("https://api.kraken.com/0/public/Ticker?pair=XBTUSD")
            r.raise_for_status()
            data = r.json()
            return float(data["result"]["XXBTZUSD"]["c"][0])
        except Exception:
            pass

        try:
            r = await client.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")
            r.raise_for_status()
            data = r.json()
            return float(data["bitcoin"]["usd"])
        except Exception:
            pass

    raise Exception("All BTC price providers failed")


async def fetch_candles():
    url = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60"

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()

    candles = sorted(data, key=lambda x: x[0])
    return candles[-120:]


def ema(values, period):
    if not values:
        return 0

    k = 2 / (period + 1)
    current = values[0]

    for value in values[1:]:
        current = value * k + current * (1 - k)

    return current


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50

    gains = []
    losses = []

    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]

        if diff >= 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def historical_volatility_calibration(candles):
    closes = [float(c[4]) for c in candles]
    returns = []

    for i in range(1, len(closes)):
        returns.append(math.log(closes[i] / closes[i - 1]))

    if not returns:
        return {"regime": "Unknown", "vol_1m": 0.0}

    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1)
    vol = variance ** 0.5

    if vol < 0.00035:
        regime = "Low"
    elif vol < 0.0009:
        regime = "Normal"
    else:
        regime = "High"

    return {"regime": regime, "vol_1m": vol}


def sigmoid(x):
    return 1 / (1 + math.exp(-clamp(x, -20, 20)))


async def build_btc_model(market=None):
    candles = await fetch_candles()
    closes = [float(c[4]) for c in candles]

    price = await fetch_btc_price()

    if len(closes) >= 16:
        open_price = closes[-15]
    else:
        open_price = closes[0]

    returns = []
    for i in range(1, len(closes)):
        returns.append(math.log(closes[i] / closes[i - 1]))

    if len(returns) > 2:
        mean_return = sum(returns) / len(returns)
        realized_vol_1m = (
            sum((r - mean_return) ** 2 for r in returns)
            / max(1, len(returns) - 1)
        ) ** 0.5
    else:
        realized_vol_1m = 0.0008

    vol_cal = historical_volatility_calibration(candles)

    ema_fast = ema(closes[-25:], 9)
    ema_slow = ema(closes[-50:], 21)

    trend_strength = (ema_fast - ema_slow) / price

    momentum_5m = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0
    momentum_15m = (closes[-1] - closes[-16]) / closes[-16] if len(closes) >= 16 else 0

    current_rsi = rsi(closes)

    now = datetime.now(timezone.utc)
    minute_bucket = (now.minute // 15) * 15

    window_start = now.replace(
        minute=minute_bucket,
        second=0,
        microsecond=0,
    )

    elapsed_sec = (now - window_start).total_seconds()
    left_sec = max(0, 900 - elapsed_sec)
    time_progress = elapsed_sec / 900

    sigma_remaining = realized_vol_1m * math.sqrt(max(left_sec / 60, 1))

    score = 0.0
    score += trend_strength * 180
    score += momentum_5m * 120
    score += momentum_15m * 80
    score += ((current_rsi - 50) / 50) * 0.8

    if time_progress > 0.75:
        score *= 1.15

    if vol_cal["regime"] == "High":
        score *= 0.9

    prob_up = sigmoid(score)
    prob_down = 1 - prob_up

    signal = "UP" if prob_up >= prob_down else "DOWN"
    model_prob = max(prob_up, prob_down)

    market_prob = 0.5

    if market:
        try:
            if signal == "UP":
                market_prob = float(market.get("up_price") or market.get("yes_price") or 0.5)
            else:
                market_prob = float(market.get("down_price") or market.get("no_price") or 0.5)
        except Exception:
            market_prob = 0.5

    edge = abs(model_prob - market_prob)

    if edge >= 0.18:
        confidence = "High"
    elif edge >= 0.08:
        confidence = "Medium"
    else:
        confidence = "Low"

    suggested_size_pct = clamp(edge * 100 * 0.7, 0, 5)

    if time_progress < 0.15:
        phase = "Early"
    elif time_progress < 0.65:
        phase = "Prime"
    elif time_progress < 0.90:
        phase = "Late"
    else:
        phase = "Danger"

    return {
        "price": price,
        "open": open_price,
        "signal": signal,
        "prob_up": prob_up,
        "prob_down": prob_down,
        "model_up": prob_up,
        "model_down": prob_down,
        "model_prob": model_prob,
        "market_prob": market_prob,
        "edge": edge,
        "confidence": confidence,
        "trend_strength": trend_strength,
        "momentum_5m": momentum_5m,
        "momentum_15m": momentum_15m,
        "rsi": current_rsi,
        "vol_1m_pct": realized_vol_1m * 100,
        "vol_regime": vol_cal["regime"],
        "sigma_remaining_pct": sigma_remaining * 100,
        "suggested_size_pct": suggested_size_pct,
        "window": {
            "start": window_start,
            "elapsed_sec": elapsed_sec,
            "left_sec": left_sec,
            "left_label": str(timedelta(seconds=int(left_sec))),
            "phase": phase,
        },
        "market": market or {},
    }


def format_btc_price(price: float) -> str:
    return f"${price:,.2f}"


def format_pct(x: float) -> str:
    return f"{x * 100:.1f}%"
