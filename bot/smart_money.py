"""
PolyAlpha Smart Money Engine
Read-only Polymarket wallet/trader scanner for Telegram bots.

Drop this file into your existing PolyScalpBot `bot/` folder.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


# Starter wallets: replace/add your own proven wallets over time.
# Leave empty and use /alpha_addwallet to build your own list.
DEFAULT_SMART_WALLETS: list[str] = []


def short_wallet(wallet: str) -> str:
    if not wallet:
        return "unknown"
    wallet = wallet.strip()
    return wallet[:6] + "…" + wallet[-4:] if len(wallet) > 12 else wallet


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _s(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _norm_wallet(wallet: str) -> str:
    return wallet.strip().lower()


@dataclass
class WalletScore:
    wallet: str
    score: float
    roi: float
    pnl: float
    volume: float
    trades: int
    winrate: float
    open_value: float
    label: str = ""


@dataclass
class Position:
    wallet: str
    market: str
    title: str
    outcome: str
    size: float
    value: float
    avg_price: float
    current_price: float
    token_id: str = ""
    condition_id: str = ""


@dataclass
class ConsensusSignal:
    title: str
    outcome: str
    market: str
    score: float
    wallets: int
    total_value: float
    avg_wallet_score: float
    avg_price: float
    best_wallets: list[str]
    token_id: str = ""


class PolyAlphaClient:
    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    async def _get(self, url: str, params: Optional[dict] = None) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": "PolyAlphaTerminal/1.0"}) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json()

    async def fetch_wallet_value(self, wallet: str) -> float:
        data = await self._get(f"{DATA_API}/value", {"user": wallet})
        if isinstance(data, list) and data:
            return _f(data[0].get("value"))
        if isinstance(data, dict):
            return _f(data.get("value"))
        return 0.0

    async def fetch_positions(self, wallet: str, limit: int = 250) -> list[Position]:
        """Fetch open positions. Endpoint shape changes sometimes, so parsing is defensive."""
        candidates = [
            (f"{DATA_API}/positions", {"user": wallet, "limit": limit}),
            (f"{DATA_API}/positions", {"address": wallet, "limit": limit}),
        ]
        raw: Any = []
        for url, params in candidates:
            try:
                raw = await self._get(url, params)
                if raw:
                    break
            except Exception:
                continue

        rows = raw if isinstance(raw, list) else raw.get("positions", []) if isinstance(raw, dict) else []
        out: list[Position] = []
        for p in rows:
            if not isinstance(p, dict):
                continue
            title = _s(p.get("title") or p.get("marketTitle") or p.get("question") or p.get("market"))
            outcome = _s(p.get("outcome") or p.get("outcomeName") or p.get("side") or p.get("asset"))
            market = _s(p.get("market") or p.get("marketSlug") or p.get("slug") or p.get("conditionId") or title)
            size = _f(p.get("size") or p.get("shares") or p.get("quantity") or p.get("balance"))
            value = _f(p.get("value") or p.get("currentValue") or p.get("cashPnl") or p.get("costBasis"))
            avg_price = _f(p.get("avgPrice") or p.get("averagePrice") or p.get("price") or p.get("initialValue"))
            current_price = _f(p.get("curPrice") or p.get("currentPrice") or p.get("price"))
            token_id = _s(p.get("asset") or p.get("tokenId") or p.get("clobTokenId"))
            condition_id = _s(p.get("conditionId") or p.get("condition_id"))
            if title or market:
                out.append(Position(wallet, market, title or market, outcome, size, value, avg_price, current_price, token_id, condition_id))
        return out

    async def fetch_activity(self, wallet: str, limit: int = 500) -> list[dict]:
        candidates = [
            (f"{DATA_API}/activity", {"user": wallet, "limit": limit}),
            (f"{DATA_API}/trades", {"user": wallet, "limit": limit}),
        ]
        for url, params in candidates:
            try:
                data = await self._get(url, params)
                if isinstance(data, list):
                    return [x for x in data if isinstance(x, dict)]
                if isinstance(data, dict):
                    rows = data.get("activity") or data.get("trades") or data.get("data") or []
                    if isinstance(rows, list):
                        return [x for x in rows if isinstance(x, dict)]
            except Exception:
                continue
        return []


class SmartMoneyEngine:
    def __init__(self, wallets: Optional[list[str]] = None):
        self.client = PolyAlphaClient()
        self.wallets = [_norm_wallet(w) for w in (wallets or DEFAULT_SMART_WALLETS) if w]

    async def score_wallet(self, wallet: str) -> WalletScore:
        wallet = _norm_wallet(wallet)
        value_task = asyncio.create_task(self.client.fetch_wallet_value(wallet))
        activity_task = asyncio.create_task(self.client.fetch_activity(wallet, limit=500))
        positions_task = asyncio.create_task(self.client.fetch_positions(wallet, limit=250))

        value = await _safe(value_task, 0.0)
        activity = await _safe(activity_task, [])
        positions = await _safe(positions_task, [])

        trades = len(activity)
        volume = sum(_f(x.get("size") or x.get("amount") or x.get("usdcSize") or x.get("value")) for x in activity)
        pnl_values = [_f(x.get("pnl") or x.get("realizedPnl") or x.get("profit"), 0.0) for x in activity]
        pnl = sum(pnl_values)
        wins = sum(1 for x in pnl_values if x > 0)
        winrate = (wins / len(pnl_values) * 100.0) if pnl_values else 0.0
        cost_guess = max(1.0, volume * 0.25)
        roi = pnl / cost_guess * 100.0 if volume else 0.0

        open_value = sum(p.value for p in positions) or value

        # Balanced score: avoids ranking one lucky tiny wallet too high.
        score = 0.0
        score += min(35.0, max(0.0, roi) * 0.35)
        score += min(20.0, winrate * 0.20)
        score += min(20.0, math.log10(max(1.0, trades)) * 8.0)
        score += min(15.0, math.log10(max(1.0, volume)) * 3.0)
        score += min(10.0, math.log10(max(1.0, open_value)) * 2.5)
        if trades < 20:
            score *= 0.65
        if volume < 1000:
            score *= 0.75

        return WalletScore(wallet, round(score, 1), round(roi, 1), round(pnl, 2), round(volume, 2), trades, round(winrate, 1), round(open_value, 2))

    async def score_wallets(self, wallets: Optional[list[str]] = None, top_n: int = 25) -> list[WalletScore]:
        targets = [_norm_wallet(w) for w in (wallets or self.wallets) if w]
        results = await asyncio.gather(*(self.score_wallet(w) for w in targets), return_exceptions=True)
        scores = [r for r in results if isinstance(r, WalletScore)]
        return sorted(scores, key=lambda x: x.score, reverse=True)[:top_n]

    async def consensus(self, wallets: Optional[list[str]] = None, min_wallets: int = 2, top_n: int = 10) -> list[ConsensusSignal]:
        scores = await self.score_wallets(wallets, top_n=100)
        score_map = {s.wallet: s.score for s in scores}
        good_wallets = [s.wallet for s in scores if s.score >= 35]
        if not good_wallets:
            good_wallets = [s.wallet for s in scores[:20]]

        position_lists = await asyncio.gather(*(self.client.fetch_positions(w) for w in good_wallets), return_exceptions=True)
        buckets: dict[tuple[str, str], list[Position]] = {}
        for rows in position_lists:
            if isinstance(rows, Exception):
                continue
            for p in rows:
                if p.value <= 0 and p.size <= 0:
                    continue
                key = ((p.market or p.title).lower().strip(), p.outcome.lower().strip())
                buckets.setdefault(key, []).append(p)

        signals: list[ConsensusSignal] = []
        for (market, outcome), positions in buckets.items():
            unique_wallets = sorted(set(p.wallet for p in positions))
            if len(unique_wallets) < min_wallets:
                continue
            total_value = sum(max(p.value, p.size * p.current_price) for p in positions)
            avg_score = sum(score_map.get(w, 0.0) for w in unique_wallets) / max(1, len(unique_wallets))
            avg_price_vals = [p.current_price or p.avg_price for p in positions if (p.current_price or p.avg_price)]
            avg_price = sum(avg_price_vals) / len(avg_price_vals) if avg_price_vals else 0.0
            consensus_score = min(100.0, (len(unique_wallets) * 8.0) + (avg_score * 0.55) + min(25.0, math.log10(max(1, total_value)) * 5.0))
            best = sorted(unique_wallets, key=lambda w: score_map.get(w, 0), reverse=True)[:5]
            sample = positions[0]
            signals.append(ConsensusSignal(
                title=sample.title,
                outcome=sample.outcome,
                market=sample.market,
                score=round(consensus_score, 1),
                wallets=len(unique_wallets),
                total_value=round(total_value, 2),
                avg_wallet_score=round(avg_score, 1),
                avg_price=round(avg_price, 3),
                best_wallets=best,
                token_id=sample.token_id,
            ))
        return sorted(signals, key=lambda x: x.score, reverse=True)[:top_n]

    async def compare_wallet(self, my_wallet: str, smart_wallets: Optional[list[str]] = None) -> dict[str, Any]:
        my_positions = await self.client.fetch_positions(_norm_wallet(my_wallet))
        signals = await self.consensus(smart_wallets, min_wallets=2, top_n=25)
        mine = {((p.market or p.title).lower().strip(), p.outcome.lower().strip()) for p in my_positions}
        signal_keys = {((s.market or s.title).lower().strip(), s.outcome.lower().strip()) for s in signals}
        overlap = mine & signal_keys
        missing = [s for s in signals if ((s.market or s.title).lower().strip(), s.outcome.lower().strip()) not in mine]
        overlap_pct = (len(overlap) / max(1, len(signal_keys)) * 100.0) if signal_keys else 0.0
        return {"my_positions": my_positions, "signals": signals, "overlap_count": len(overlap), "overlap_pct": round(overlap_pct, 1), "missing": missing[:10]}


async def _safe(task: "asyncio.Task[Any]", default: Any) -> Any:
    try:
        return await task
    except Exception:
        return default
