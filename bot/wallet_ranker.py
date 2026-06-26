"""Wallet scoring engine for PolyAlpha Terminal.

Scoring breakdown (100 pts total):
  ROI score      — 30 pts  (key differentiator, avoids identical scores)
  Win rate       — 20 pts
  PnL absolute   — 15 pts  (proven real earnings)
  Trade count    — 10 pts  (sample size credibility)
  Volume         — 10 pts  (market presence)
  Consistency    — 10 pts  (not just lucky streaks)
  Recency        — 5  pts  (still active and winning recently)
  Penalties:
    - drawdown   — up to -12 pts
    - low trades — score * 0.55
    - negative PnL  — score * 0.40
    - zero/unknown pnl — score cap 38
"""
from __future__ import annotations
import math
from statistics import pstdev
from typing import Any


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def compute_consistency(pnls: list[float]) -> float:
    """0-100 consistency score; rewards stable positive streaks."""
    if len(pnls) < 3:
        return 15.0
    wins = sum(1 for x in pnls if x > 0)
    wr = wins / len(pnls)
    # Penalise high variance
    vol = pstdev(pnls) if len(pnls) > 1 else 0.0
    mean_abs = sum(abs(x) for x in pnls) / len(pnls) or 1.0
    norm_vol = min(vol / mean_abs, 3.0)  # coefficient of variation capped at 3
    consistency = (wr * 75.0) - (norm_vol * 10.0) + 15.0
    return clamp(consistency, 0.0, 100.0)


def max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    return abs(dd)


def rank_wallet_metrics(
    roi: float,
    winrate: float,
    trades: int,
    volume: float,
    pnl: float,
    open_value: float,
    pnls: list[float],
) -> tuple[float, float, float, float]:
    """Return (score, consistency, recent_score, drawdown).

    Designed so wallets with meaningfully different ROI/winrate
    receive meaningfully different scores — not all 31.5.
    """
    consistency = compute_consistency(pnls)
    dd = max_drawdown(pnls)
    # Recent momentum: last 20 trades
    recent_pnl = sum(pnls[:20]) if pnls else 0.0

    # --- Component scores ---

    # ROI: 0–30 pts. Logarithmic to avoid one outlier dominating.
    # +5% ROI → ~12pts; +30% → ~22pts; +100% → ~28pts; +300% → 30pts
    if roi > 0:
        roi_score = clamp(math.log1p(roi / 10.0) / math.log1p(30.0) * 30.0, 0.0, 30.0)
    else:
        roi_score = 0.0

    # Winrate: 0–20 pts. 50%=0pts, 60%=10pts, 70%=17pts, 80%+=20pts
    wr_score = clamp((winrate - 50.0) / 30.0 * 20.0, 0.0, 20.0) if winrate > 50 else 0.0

    # Absolute PnL: 0–15 pts (proven real money made)
    pnl_score = clamp(math.log10(max(1.0, pnl)) / 5.0 * 15.0, 0.0, 15.0) if pnl > 0 else 0.0

    # Trade count: 0–10 pts. Need >=10 for any score; 100+=8pts; 500+=10pts
    trade_score = clamp(math.log10(max(1.0, trades - 9)) / math.log10(491.0) * 10.0, 0.0, 10.0) if trades >= 10 else 0.0

    # Volume: 0–10 pts (market presence)
    vol_score = clamp(math.log10(max(1.0, volume)) / 6.0 * 10.0, 0.0, 10.0)

    # Consistency: 0–10 pts
    cons_score = consistency / 100.0 * 10.0

    # Recency: 0–5 pts (positive recent momentum)
    if recent_pnl > 0 and pnl > 0:
        recency = clamp(recent_pnl / pnl, 0.0, 1.0) * 5.0
    else:
        recency = 0.0

    raw = roi_score + wr_score + pnl_score + trade_score + vol_score + cons_score + recency

    # --- Penalties ---
    # Drawdown penalty: up to -12 pts
    dd_ratio = dd / max(1.0, pnl + 1.0) if pnl > 0 else 1.0
    dd_penalty = clamp(dd_ratio * 12.0, 0.0, 12.0)
    raw -= dd_penalty

    score = clamp(raw, 0.0, 100.0)

    # Structural penalties (multiplicative)
    if trades < 15:
        score *= 0.50   # too few trades, unreliable
    elif trades < 30:
        score *= 0.75
    if volume < 200:
        score *= 0.70
    if pnl <= 0:
        score *= 0.40   # losing traders penalised hard
    elif pnl < 10:
        score = min(score, 38.0)  # unknown/trivial pnl: cap below 40

    return round(score, 1), round(consistency, 1), round(recency * 10.0, 1), round(dd, 2)


def score_components(
    roi: float,
    winrate: float,
    trades: int,
    volume: float,
    pnl: float,
    open_value: float,
    pnls: list[float],
) -> dict[str, float]:
    """Return per-component breakdown for /topwallets display."""
    consistency = compute_consistency(pnls)
    dd = max_drawdown(pnls)
    recent_pnl = sum(pnls[:20]) if pnls else 0.0

    roi_score = clamp(math.log1p(roi / 10.0) / math.log1p(30.0) * 30.0, 0.0, 30.0) if roi > 0 else 0.0
    wr_score = clamp((winrate - 50.0) / 30.0 * 20.0, 0.0, 20.0) if winrate > 50 else 0.0
    pnl_score = clamp(math.log10(max(1.0, pnl)) / 5.0 * 15.0, 0.0, 15.0) if pnl > 0 else 0.0
    trade_score = clamp(math.log10(max(1.0, trades - 9)) / math.log10(491.0) * 10.0, 0.0, 10.0) if trades >= 10 else 0.0
    vol_score = clamp(math.log10(max(1.0, volume)) / 6.0 * 10.0, 0.0, 10.0)
    cons_score = consistency / 100.0 * 10.0
    recency = clamp(recent_pnl / pnl, 0.0, 1.0) * 5.0 if recent_pnl > 0 and pnl > 0 else 0.0
    dd_ratio = dd / max(1.0, pnl + 1.0) if pnl > 0 else 1.0
    dd_penalty = clamp(dd_ratio * 12.0, 0.0, 12.0)

    return {
        "roi_score": round(roi_score, 1),
        "wr_score": round(wr_score, 1),
        "pnl_score": round(pnl_score, 1),
        "trade_score": round(trade_score, 1),
        "vol_score": round(vol_score, 1),
        "cons_score": round(cons_score, 1),
        "recency": round(recency, 1),
        "dd_penalty": round(dd_penalty, 1),
    }
