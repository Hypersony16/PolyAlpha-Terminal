from __future__ import annotations
import math
from statistics import pstdev
from typing import Any


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def compute_consistency(pnls: list[float]) -> float:
    if len(pnls) < 5:
        return 20.0
    wins = sum(1 for x in pnls if x > 0)
    wr = wins / len(pnls)
    vol = pstdev(pnls) if len(pnls) > 1 else 0.0
    return clamp((wr * 70.0) - min(35.0, vol * 10.0) + 20.0, 0.0, 100.0)


def max_drawdown(pnls: list[float]) -> float:
    equity = 0.0; peak = 0.0; dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    return abs(dd)


def rank_wallet_metrics(roi: float, winrate: float, trades: int, volume: float, pnl: float, open_value: float, pnls: list[float]) -> tuple[float, float, float, float]:
    consistency = compute_consistency(pnls)
    dd = max_drawdown(pnls)
    recent = sum(pnls[:50]) if pnls else 0.0
    roi_score = clamp(roi, -50, 300) / 300 * 25 if roi > 0 else 0
    wr_score = clamp(winrate, 0, 80) / 80 * 20
    trade_score = clamp(math.log10(max(1, trades)) / 3, 0, 1) * 15
    vol_score = clamp(math.log10(max(1, volume)) / 6, 0, 1) * 10
    cons_score = consistency / 100 * 15
    recent_score = clamp((recent / max(1, abs(pnl) + 1)) * 50 + 50, 0, 100) / 100 * 10
    dd_penalty = clamp(dd / max(1, abs(pnl) + volume * 0.03), 0, 1) * 10
    score = clamp(roi_score + wr_score + trade_score + vol_score + cons_score + recent_score - dd_penalty, 0, 100)
    if trades < 25: score *= 0.65
    if volume < 500: score *= 0.75
    return round(score, 1), round(consistency, 1), round(recent_score * 10, 1), round(dd, 2)
