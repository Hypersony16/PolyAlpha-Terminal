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
            price = float(data["result"]["XXBTZUSD"]["c"][0])
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

    if _PRICE_CACHE["value"] is not None:
        return float(_PRICE_CACHE["value"])
    raise RuntimeError("Could not fetch BTC price from any source")


async def fetch_btc_klines(granularity: int = 900, limit: int = 20) -> list:
    now = time.time()
    if _KLINES_CACHE["value"] is not None and now - _KLINES_CACHE["ts"] < 30:
        return _KLINES_CACHE["value"]

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(
                COINBASE_CANDLES_URL,
                params={"granularity": granularity},
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                _KLINES_CACHE.update({"ts": now, "value": data})
                return data
        except Exception:
            pass

    return _KLINES_CACHE["value"] or []


def _compute_rsi(closes: list, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _compute_ema(closes: list, period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)


def _compute_macd(closes: list) -> Dict[str, Optional[float]]:
    ema12 = _compute_ema(closes, 12)
    ema26 = _compute_ema(closes, 26)
    if ema12 is None or ema26 is None:
        return {"macd": None, "signal": None, "hist": None}
    macd_line = ema12 - ema26
    return {"macd": round(macd_line, 2), "signal": None, "hist": None}


def _compute_bollinger(closes: list, period: int = 20) -> Dict[str, Optional[float]]:
    if len(closes) < period:
        return {"upper": None, "middle": None, "lower": None}
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = math.sqrt(variance)
    return {
        "upper": round(middle + 2 * std, 2),
        "middle": round(middle, 2),
        "lower": round(middle - 2 * std, 2),
    }


async def build_btc_model(market: Dict[str, Any]) -> Dict[str, Any]:
    price = await fetch_btc_price()
    klines = await fetch_btc_klines()

    window = current_15m_window()
    closes = []
    if klines:
        # Coinbase candles: [time, low, high, open, close, volume]
        for candle in reversed(klines):
            try:
                closes.append(float(candle[4]))
            except Exception:
                pass

    rsi = _compute_rsi(closes) if closes else None
    ema9 = _compute_ema(closes, 9) if closes else None
    ema21 = _compute_ema(closes, 21) if closes else None
    macd = _compute_macd(closes) if closes else {"macd": None, "signal": None, "hist": None}
    bb = _compute_bollinger(closes) if closes else {"upper": None, "middle": None, "lower": None}

    # Signal logic
    bullish_signals = 0
    bearish_signals = 0

    if rsi is not None:
        if rsi < 40:
            bullish_signals += 1
        elif rsi > 60:
            bearish_signals += 1

    if ema9 is not None and ema21 is not None:
        if ema9 > ema21:
            bullish_signals += 1
        else:
            bearish_signals += 1

    if bb["middle"] is not None:
        if price > bb["middle"]:
            bullish_signals += 1
        else:
            bearish_signals += 1

    if bullish_signals > bearish_signals:
        signal = "UP"
        model_prob = 0.55 + min(0.15, bullish_signals * 0.05)
    elif bearish_signals > bullish_signals:
        signal = "DOWN"
        model_prob = 0.55 + min(0.15, bearish_signals * 0.05)
    else:
        signal = "NEUTRAL"
        model_prob = 0.50

    # Market probability from Polymarket
    market_prob = float(market.get("up_price") or market.get("yes_price") or 0.5)
    market_prob = max(0.01, min(0.99, market_prob))

    if signal == "UP":
        edge = model_prob - market_prob
    elif signal == "DOWN":
        edge = (1 - model_prob) - (1 - market_prob)
    else:
        edge = 0.0

    if abs(edge) >= 0.15:
        confidence = "High"
    elif abs(edge) >= 0.08:
        confidence = "Medium"
    else:
        confidence = "Low"

    time_left = window["left_sec"]
    window_start_str = window["start"].isoformat()
    window_end_ts = window["start"].timestamp() + 900

    return {
        "price": price,
        "signal": signal,
        "model_prob": round(model_prob, 4),
        "market_prob": round(market_prob, 4),
        "edge": round(edge, 4),
        "confidence": confidence,
        "rsi": rsi,
        "ema9": ema9,
        "ema21": ema21,
        "macd": macd["macd"],
        "bb_upper": bb["upper"],
        "bb_middle": bb["middle"],
        "bb_lower": bb["lower"],
        "time_left_seconds": time_left,
        "window_start": window_start_str,
        "window_end_ts": window_end_ts,
        "phase": window["phase"],
        "market": market,
        "up_price": market_prob,
        "down_price": round(1 - market_prob, 4),
        "open": price,
    }


def format_btc_price(price: float) -> str:
    return f"${price:,.2f}"
