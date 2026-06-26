"""PolyAlpha Smart Money Engine for Polymarket.
Read-only: ranking, consensus, portfolio comparison and whale intelligence.

v2.3 improvements:
- Proper differentiated wallet scoring (no more identical 31.5/100)
- Score component breakdown per wallet
- Improved consensus engine with weighted conviction
- Faster background-scan architecture
- Whale alert detection on position changes
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from bot.wallet_ranker import rank_wallet_metrics, score_components
from bot.alpha_store import (
    save_wallet_score,
    save_positions,
    save_consensus,
    save_discovered_wallet,
    save_alpha_scan_run,
    clear_old_consensus,
    save_whale_alert,
    cached_positions,
    top_saved_wallet_scores,
)
from bot.market_filters import is_quality_market, market_filter_reason
from bot.intelligence_v4 import ensure_v4_tables, record_position_changes, build_wallet_clusters, signal_backtest_summary

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
DEFAULT_SMART_WALLETS: list[str] = []

SCORE_FLOOR_FOR_CONSENSUS = 50.0   # only decent wallets contribute to consensus
MIN_CONSENSUS_WALLETS = 3           # avoid 1-2 wallet noise
MIN_CONSENSUS_VALUE = 1000.0        # avoid tiny stale positions
MIN_ACTIONABLE_EDGE = 0.03          # 3 pts minimum positive edge


def short_wallet(wallet: str) -> str:
    return (wallet[:6] + "…" + wallet[-4:]) if wallet and len(wallet) > 12 else (wallet or "unknown")


def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return d
        return float(v)
    except Exception:
        return d


def _s(v: Any, d: str = "") -> str:
    return d if v is None else str(v)


def _norm(w: str) -> str:
    return w.strip().lower()


def _first_metric(raw: dict[str, Any] | None, names: tuple[str, ...], default: float = 0.0) -> float:
    if not isinstance(raw, dict):
        return default
    for name in names:
        if name in raw and raw.get(name) not in (None, ""):
            return _f(raw.get(name), default)
    lower = {str(k).lower(): v for k, v in raw.items()}
    for name in names:
        key = name.lower()
        if key in lower and lower.get(key) not in (None, ""):
            return _f(lower.get(key), default)
    return default


@dataclass
class LeaderboardWallet:
    wallet: str
    rank: int = 0
    pnl: float = 0.0
    volume: float = 0.0
    username: str = ""
    raw: dict[str, Any] | None = None


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
    consistency: float = 0.0
    recent_score: float = 0.0
    drawdown: float = 0.0
    label: str = ""
    components: dict[str, float] = field(default_factory=dict)

    def why(self) -> str:
        """Human-readable reason for wallet score."""
        c = self.components
        parts = []
        if c.get("roi_score", 0) >= 15:
            parts.append(f"ROI {self.roi:+.0f}%")
        if c.get("wr_score", 0) >= 8:
            parts.append(f"WR {self.winrate:.0f}%")
        if c.get("pnl_score", 0) >= 8:
            parts.append(f"PnL ${self.pnl:,.0f}")
        if c.get("trade_score", 0) >= 6:
            parts.append(f"{self.trades} trades")
        if c.get("cons_score", 0) >= 6:
            parts.append("consistent")
        if c.get("dd_penalty", 0) >= 5:
            parts.append(f"DD penalty -{c['dd_penalty']:.0f}pts")
        return ", ".join(parts) if parts else "leaderboard rank"

    def grade(self) -> str:
        if self.score >= 80:
            return "Elite"
        if self.score >= 65:
            return "Strong"
        if self.score >= 50:
            return "Good"
        if self.score >= 35:
            return "Moderate"
        return "Weak"


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
    fair_value: float = 0.0
    edge: float = 0.0
    confidence: str = "Medium"
    weighted_conviction: float = 0.0


def _rank_score_from_leaderboard(lb: LeaderboardWallet, total_limit: int = 100) -> WalletScore:
    """Score a wallet using leaderboard metadata only (fast, no extra API calls).

    Key improvement: uses leaderboard rank, PnL, volume, and open_value
    with non-linear scaling so scores spread across 1–95 range.
    """
    raw = lb.raw or {}
    pnl = _first_metric(raw, ("pnl", "profit", "totalPnl", "realizedPnl", "cashPnl", "amount"), lb.pnl)
    volume = _first_metric(raw, ("volume", "vol", "totalVolume", "tradedVolume", "tradeVolume", "userVolume"), lb.volume)
    open_value = _first_metric(raw, ("openValue", "positionValue", "portfolioValue", "currentValue", "value"), 0.0)
    trades = int(_first_metric(raw, ("trades", "tradeCount", "numTrades", "positions", "marketsTraded"), 0.0))
    winrate = _first_metric(raw, ("winRate", "winrate", "win_rate", "accuracy", "hitRate"), 0.0)
    if 0 < winrate <= 1.0:
        winrate *= 100.0

    # Synthetic trade count from rank/volume when API doesn't expose it
    if trades <= 0:
        trades = max(5, int(min(400, math.log10(max(10.0, volume + open_value + 1)) * 40)))

    roi = 0.0
    if volume > 0 and pnl > 0:
        roi = pnl / max(1.0, volume) * 100.0
    elif pnl > 0 and open_value > 0:
        roi = pnl / max(1.0, open_value) * 100.0

    # Rank quality: top-10 gets full 30pts; rank 100 gets 0pts. Non-linear.
    rank_norm = min(max(lb.rank - 1, 0), total_limit) / max(1, total_limit)
    rank_score = max(0.0, 30.0 * (1.0 - rank_norm ** 0.5))

    # PnL: logarithmic 0–25
    pnl_score = clamp(math.log10(max(1.0, pnl)) / 5.5 * 25.0, 0.0, 25.0) if pnl > 0 else 0.0

    # Volume: 0–15
    vol_score = clamp(math.log10(max(1.0, volume)) / 6.5 * 15.0, 0.0, 15.0)

    # Open value (skin-in-game): 0–10
    open_score = clamp(math.log10(max(1.0, open_value)) / 5.0 * 10.0, 0.0, 10.0)

    # Winrate bonus if available: 0–12
    wr_score = clamp((winrate - 50.0) / 30.0 * 12.0, 0.0, 12.0) if winrate > 50 else 0.0

    # ROI bonus: differentiation booster
    roi_bonus = clamp(math.log1p(roi / 10.0) / math.log1p(30.0) * 8.0, 0.0, 8.0) if roi > 0 else 0.0

    raw_score = rank_score + pnl_score + vol_score + open_score + wr_score + roi_bonus
    score = max(1.0, min(100.0, raw_score))

    # Penalties
    if pnl <= 0:
        score *= 0.45
    elif pnl < 50:
        score = min(score, 38.0)

    comps = {
        "rank_score": round(rank_score, 1),
        "pnl_score": round(pnl_score, 1),
        "vol_score": round(vol_score, 1),
        "open_score": round(open_score, 1),
        "wr_score": round(wr_score, 1),
        "roi_bonus": round(roi_bonus, 1),
        "roi_score": round(roi_bonus, 1),
    }

    ws = WalletScore(
        wallet=lb.wallet,
        score=round(score, 1),
        roi=round(roi, 1),
        pnl=round(pnl, 2),
        volume=round(volume, 2),
        trades=trades,
        winrate=round(winrate, 1),
        open_value=round(open_value, 2),
        consistency=round(rank_score, 1),
        recent_score=0.0,
        drawdown=0.0,
        label=lb.username or f"LB #{lb.rank}",
        components=comps,
    )
    return ws


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class PolyAlphaClient:
    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    async def _get(self, url: str, params: Optional[dict] = None) -> Any:
        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": "PolyAlphaTerminal/2.3"},
            follow_redirects=True,
        ) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            return r.json()

    async def fetch_leaderboard(
        self,
        category: str = "OVERALL",
        time_period: str = "MONTH",
        order_by: str = "PNL",
        limit: int = 100,
    ) -> list[LeaderboardWallet]:
        category = (category or "OVERALL").upper()
        time_period = (time_period or "MONTH").upper()
        order_by = (order_by or "PNL").upper()
        total_limit = max(1, min(int(limit or 50), 250))

        async def parse_rows(data: Any, start_rank: int = 1) -> list[LeaderboardWallet]:
            rows = (
                data
                if isinstance(data, list)
                else data.get("leaderboard")
                or data.get("rankings")
                or data.get("data")
                or data.get("results")
                or []
            )
            out: list[LeaderboardWallet] = []
            for idx, r in enumerate(rows, start_rank):
                if not isinstance(r, dict):
                    continue
                wallet = _s(
                    r.get("proxyWallet")
                    or r.get("profileAddress")
                    or r.get("wallet")
                    or r.get("address")
                    or r.get("user")
                    or r.get("funder")
                )
                if not wallet.lower().startswith("0x"):
                    continue
                out.append(
                    LeaderboardWallet(
                        wallet=_norm(wallet),
                        rank=int(_f(r.get("rank") or idx, idx)),
                        pnl=_f(r.get("pnl") or r.get("profit") or r.get("totalPnl") or r.get("amount")),
                        volume=_f(r.get("vol") or r.get("volume") or r.get("totalVolume")),
                        username=_s(r.get("userName") or r.get("username") or r.get("name") or r.get("pseudonym")),
                        raw=r,
                    )
                )
            return out

        out: list[LeaderboardWallet] = []
        last_error: Exception | None = None
        try:
            offset = 0
            while len(out) < total_limit and offset <= 1000:
                page_limit = min(50, total_limit - len(out))
                data = await self._get(
                    f"{DATA_API}/v1/leaderboard",
                    {
                        "category": category,
                        "timePeriod": time_period,
                        "orderBy": order_by,
                        "limit": page_limit,
                        "offset": offset,
                    },
                )
                page = await parse_rows(data, offset + 1)
                if not page:
                    break
                out.extend(page)
                if len(page) < page_limit:
                    break
                offset += page_limit
            seen: set[str] = set()
            deduped = []
            for w in out:
                if w.wallet not in seen:
                    seen.add(w.wallet)
                    deduped.append(w)
            if deduped:
                return deduped[:total_limit]
        except Exception as e:
            last_error = e

        for url in (f"{DATA_API}/leaderboard", f"{DATA_API}/rankings"):
            try:
                data = await self._get(
                    url,
                    {"category": category, "timePeriod": time_period, "orderBy": order_by, "limit": min(total_limit, 50)},
                )
                rows = await parse_rows(data, 1)
                if rows:
                    return rows[:total_limit]
            except Exception as e:
                last_error = e
                continue
        if last_error:
            raise last_error
        return []

    async def fetch_wallet_value(self, wallet: str) -> float:
        try:
            data = await self._get(f"{DATA_API}/value", {"user": wallet})
            if isinstance(data, list) and data:
                return _f(data[0].get("value"))
            if isinstance(data, dict):
                return _f(data.get("value"))
        except Exception:
            pass
        return 0.0

    async def fetch_positions(self, wallet: str, limit: int = 250) -> list[Position]:
        raw: Any = []
        for params in ({"user": wallet, "limit": limit}, {"address": wallet, "limit": limit}):
            try:
                raw = await self._get(f"{DATA_API}/positions", params)
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
            market = _s(
                p.get("market") or p.get("marketSlug") or p.get("slug") or p.get("conditionId") or title
            )
            size = _f(p.get("size") or p.get("shares") or p.get("quantity") or p.get("balance"))
            value = _f(
                p.get("value")
                or p.get("currentValue")
                or p.get("cashPnl")
                or p.get("costBasis")
                or (size * _f(p.get("curPrice") or p.get("currentPrice") or p.get("price")))
            )
            avg_price = _f(p.get("avgPrice") or p.get("averagePrice") or p.get("price") or p.get("initialValue"))
            cur = _f(p.get("curPrice") or p.get("currentPrice") or p.get("price") or avg_price)
            token_id = _s(p.get("asset") or p.get("tokenId") or p.get("clobTokenId"))
            condition_id = _s(p.get("conditionId") or p.get("condition_id"))
            if title or market:
                # Filter stale/settled games and low-quality long-term outright markets early.
                if not is_quality_market(title or market, market):
                    continue
                out.append(Position(wallet, market, title or market, outcome or "YES", size, value, avg_price, cur, token_id, condition_id))
        return out

    async def fetch_activity(self, wallet: str, limit: int = 500) -> list[dict]:
        for endpoint, params in (
            ("activity", {"user": wallet, "limit": limit}),
            ("trades", {"user": wallet, "limit": limit}),
        ):
            try:
                data = await self._get(f"{DATA_API}/{endpoint}", params)
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
        self.wallets = [_norm(w) for w in (wallets or DEFAULT_SMART_WALLETS) if w]

    async def score_wallet(self, wallet: str) -> WalletScore:
        w = _norm(wallet)
        value_task = asyncio.create_task(self.client.fetch_wallet_value(w))
        act_task = asyncio.create_task(self.client.fetch_activity(w, 500))
        pos_task = asyncio.create_task(self.client.fetch_positions(w, 250))
        value = await _safe(value_task, 0.0)
        activity = await _safe(act_task, [])
        positions = await _safe(pos_task, [])

        trades = len(activity)
        volume = sum(
            _f(x.get("size") or x.get("amount") or x.get("usdcSize") or x.get("value") or x.get("volume"))
            for x in activity
        )
        pnls = [_f(x.get("pnl") or x.get("realizedPnl") or x.get("profit"), 0.0) for x in activity]
        pnl = sum(pnls)
        wins = sum(1 for x in pnls if x > 0)
        winrate = wins / len(pnls) * 100 if pnls else 0.0
        roi = pnl / max(1.0, volume * 0.25) * 100 if volume else 0.0
        open_value = sum(max(p.value, p.size * p.current_price) for p in positions) or value

        score, consistency, recent, dd = rank_wallet_metrics(roi, winrate, trades, volume, pnl, open_value, pnls)
        comps = score_components(roi, winrate, trades, volume, pnl, open_value, pnls)

        ws = WalletScore(w, score, round(roi, 1), round(pnl, 2), round(volume, 2), trades,
                         round(winrate, 1), round(open_value, 2), consistency, recent, dd, components=comps)
        save_wallet_score(ws)
        save_positions(w, positions)
        return ws

    async def score_wallets(self, wallets: Optional[list[str]] = None, top_n: int = 25) -> list[WalletScore]:
        targets = [_norm(w) for w in (wallets or self.wallets) if w]
        results = []
        for i in range(0, len(targets), 15):
            chunk = targets[i:i + 15]
            res = await asyncio.gather(*(self.score_wallet(w) for w in chunk), return_exceptions=True)
            results += [r for r in res if isinstance(r, WalletScore)]
        return sorted(results, key=lambda x: x.score, reverse=True)[:top_n]

    def _build_consensus_from_positions(
        self,
        all_positions: list[dict],
        score_map: dict[str, float],
        min_wallets: int = MIN_CONSENSUS_WALLETS,
        min_value: float = MIN_CONSENSUS_VALUE,
        top_n: int = 20,
    ) -> list[ConsensusSignal]:
        """Build consensus signals from a list of position dicts (from cache or live)."""
        buckets: dict[tuple[str, str], list[dict]] = {}
        for p in all_positions:
            # Only include wallets above floor score
            w = p.get("wallet", "")
            ws = score_map.get(w, 0.0)
            if ws < SCORE_FLOOR_FOR_CONSENSUS:
                continue
            val = max(_f(p.get("value")), _f(p.get("size")) * _f(p.get("current_price")))
            if val <= 0:
                continue
            title = _s(p.get("title") or p.get("market"))
            outcome = _s(p.get("outcome") or "YES")
            market = _s(p.get("market") or title)
            if not is_quality_market(title, market):
                continue
            key = ((market or title).lower().strip(), outcome.lower().strip())
            buckets.setdefault(key, []).append({**p, "_val": val, "_ws": ws, "_title": title, "_outcome": outcome, "_market": market})

        signals: list[ConsensusSignal] = []
        for (market_key, outcome_key), rows in buckets.items():
            unique_wallets = sorted(set(r.get("wallet", "") for r in rows))
            if len(unique_wallets) < min_wallets:
                continue
            total_value = sum(r["_val"] for r in rows)
            if total_value < min_value:
                continue

            avg_ws = sum(score_map.get(w, 0) for w in unique_wallets) / max(1, len(unique_wallets))
            # Only show signals where avg wallet quality is meaningful
            if avg_ws < SCORE_FLOOR_FOR_CONSENSUS:
                continue

            prices = [_f(r.get("current_price") or r.get("avg_price")) for r in rows if (r.get("current_price") or r.get("avg_price"))]
            avg_price = sum(prices) / len(prices) if prices else 0.0

            # Weighted conviction: wallet_score * position_value
            weighted_sum = sum(score_map.get(r.get("wallet", ""), 0) * r["_val"] for r in rows)
            weighted_conviction = weighted_sum / max(1.0, total_value)

            # Fair value estimate: weighted by wallet score quality
            # Higher-quality wallets entering at lower prices → bullish signal
            fair = min(0.99, max(0.01,
                (avg_ws / 100.0) * 0.45
                + min(0.30, len(unique_wallets) / 80.0)
                + min(0.15, math.log10(max(1, total_value)) / 60.0)
                + (weighted_conviction / 100.0) * 0.10
            ))
            edge = round(fair - avg_price, 3) if avg_price else 0.0

            # Signal score: wallets × quality + value + edge
            signal_score = min(100.0,
                len(unique_wallets) * 5.0
                + avg_ws * 0.60
                + min(20.0, math.log10(max(1, total_value)) * 4.0)
                + max(0, edge * 40.0)
            )

            conf = "High" if signal_score >= 75 and len(unique_wallets) >= 4 else "Medium" if signal_score >= 50 else "Low"

            # Hard quality gate: no negative edge / no tiny fake edge in stored consensus.
            if edge < MIN_ACTIONABLE_EDGE:
                continue

            best = sorted(unique_wallets, key=lambda w: score_map.get(w, 0), reverse=True)[:5]
            sample = rows[0]
            signals.append(ConsensusSignal(
                title=sample["_title"] or market_key,
                outcome=sample["_outcome"] or outcome_key,
                market=sample["_market"] or market_key,
                score=round(signal_score, 1),
                wallets=len(unique_wallets),
                total_value=round(total_value, 2),
                avg_wallet_score=round(avg_ws, 1),
                avg_price=round(avg_price, 3),
                best_wallets=best,
                token_id=_s(sample.get("token_id")),
                fair_value=round(fair, 3),
                edge=edge,
                confidence=conf,
                weighted_conviction=round(weighted_conviction, 1),
            ))

        return sorted(signals, key=lambda x: (x.score, x.total_value), reverse=True)[:top_n]

    async def consensus(
        self,
        wallets: Optional[list[str]] = None,
        min_wallets: int = MIN_CONSENSUS_WALLETS,
        top_n: int = 20,
    ) -> list[ConsensusSignal]:
        """Build live consensus from wallet positions."""
        scores = await self.score_wallets(wallets, top_n=100)
        score_map = {s.wallet: s.score for s in scores}
        good = [s.wallet for s in scores if s.score >= SCORE_FLOOR_FOR_CONSENSUS]
        if not good:
            good = [s.wallet for s in scores[:20]]

        pos_lists: list[list[Position]] = []
        for i in range(0, len(good), 20):
            res = await asyncio.gather(
                *(self.client.fetch_positions(w) for w in good[i:i + 20]),
                return_exceptions=True,
            )
            pos_lists += [r for r in res if isinstance(r, list)]

        # Convert to dicts for unified processing
        pos_dicts = []
        for rows in pos_lists:
            for p in rows:
                pos_dicts.append({
                    "wallet": p.wallet, "market": p.market, "title": p.title,
                    "outcome": p.outcome, "size": p.size, "value": p.value,
                    "avg_price": p.avg_price, "current_price": p.current_price,
                    "token_id": p.token_id,
                })

        signals = self._build_consensus_from_positions(pos_dicts, score_map, min_wallets, MIN_CONSENSUS_VALUE, top_n)
        save_consensus(signals)
        return signals

    def consensus_from_cache(self, top_n: int = 20) -> list[ConsensusSignal]:
        """Build consensus from cached DB positions (fast, no API calls)."""
        scores = top_saved_wallet_scores(200)
        score_map = {r["wallet"]: _f(r.get("score")) for r in scores}
        pos_dicts = cached_positions(10000)
        return self._build_consensus_from_positions(pos_dicts, score_map, MIN_CONSENSUS_WALLETS, MIN_CONSENSUS_VALUE, top_n)

    async def discover_from_leaderboards(
        self,
        category: str = "OVERALL",
        time_period: str = "MONTH",
        order_by: str = "PNL",
        limit: int = 100,
        score_top: int = 50,
    ) -> dict[str, Any]:
        category = (category or "OVERALL").upper()
        time_period = (time_period or "MONTH").upper()
        order_by = (order_by or "PNL").upper()
        wallets_found: list[LeaderboardWallet] = []
        try:
            wallets_found = await self.client.fetch_leaderboard(category, time_period, order_by, limit)
            seen: set[str] = set()
            unique: list[LeaderboardWallet] = []
            for w in wallets_found:
                if w.wallet not in seen:
                    seen.add(w.wallet)
                    unique.append(w)
            wallets_found = unique

            for w in wallets_found:
                save_discovered_wallet(w.wallet, "leaderboard", category, time_period, order_by,
                                       w.pnl, w.volume, w.rank, w.raw or {}, w.username or f"LB {category} {time_period}")

            lb_scores = sorted(
                [_rank_score_from_leaderboard(w, total_limit=max(50, limit)) for w in wallets_found[:max(1, score_top)]],
                key=lambda x: x.score,
                reverse=True,
            )
            for sc in lb_scores:
                save_wallet_score(sc)

            # Fetch positions for top wallets only
            best_wallets = [s.wallet for s in lb_scores[:min(35, len(lb_scores))]]
            pos_results = await asyncio.gather(
                *(self.client.fetch_positions(w, 100) for w in best_wallets),
                return_exceptions=True,
            ) if best_wallets else []

            new_alerts = 0
            for w, rows in zip(best_wallets, pos_results):
                if isinstance(rows, list):
                    ensure_v4_tables()
                    record_position_changes(w, rows)
                    save_positions(w, rows)
                    # Whale alert: high-score wallet with large position
                    ws = next((s for s in lb_scores if s.wallet == w), None)
                    if ws and ws.score >= 60:
                        for p in rows:
                            val = max(p.value, p.size * p.current_price)
                            if val >= 5000:
                                save_whale_alert(w, p.market, p.outcome, val, p.current_price, ws.score)
                                new_alerts += 1

            # Rebuild consensus from cached positions (fast)
            score_map = {s.wallet: s.score for s in lb_scores}
            pos_dicts = cached_positions(8000)
            signals = self._build_consensus_from_positions(pos_dicts, score_map, MIN_CONSENSUS_WALLETS, MIN_CONSENSUS_VALUE, 20)
            save_consensus(signals)
            try:
                build_wallet_clusters(); signal_backtest_summary()
            except Exception:
                pass
            clear_old_consensus()

            top = lb_scores[0] if lb_scores else None
            save_alpha_scan_run("leaderboard", category, time_period, order_by,
                                len(wallets_found), len(wallets_found), len(lb_scores),
                                top.wallet if top else "", top.score if top else 0, "ok", "")
            return {
                "status": "ok",
                "wallets_found": len(wallets_found),
                "wallets_added": len(wallets_found),
                "wallets_scored": len(lb_scores),
                "top_wallet": top.wallet if top else "",
                "top_score": top.score if top else 0,
                "consensus": len(signals),
                "whale_alerts": new_alerts,
                "scores": lb_scores,
                "signals": signals,
            }
        except Exception as e:
            save_alpha_scan_run("leaderboard", category, time_period, order_by,
                                len(wallets_found), 0, 0, "", 0, "error", str(e))
            return {"status": "error", "error": str(e), "wallets_found": len(wallets_found),
                    "wallets_added": 0, "wallets_scored": 0, "scores": [], "signals": []}

    async def discover_multi_leaderboards(self, limit_per_board: int = 75, score_top: int = 100) -> dict[str, Any]:
        boards = [
            ("OVERALL", "MONTH", "PNL"),
            ("OVERALL", "ALL", "PNL"),
            ("CRYPTO", "MONTH", "PNL"),
            ("POLITICS", "MONTH", "PNL"),
            ("OVERALL", "WEEK", "PNL"),
            ("OVERALL", "MONTH", "VOL"),
        ]
        all_wallets: dict[str, LeaderboardWallet] = {}
        errors = []
        for cat, period, order in boards:
            try:
                rows = await self.client.fetch_leaderboard(cat, period, order, limit_per_board)
                for r in rows:
                    old = all_wallets.get(r.wallet)
                    if old is None or r.pnl > old.pnl:
                        all_wallets[r.wallet] = r
                    save_discovered_wallet(r.wallet, "leaderboard", cat, period, order,
                                           r.pnl, r.volume, r.rank, r.raw or {}, r.username or f"LB {cat} {period}")
            except Exception as e:
                errors.append(f"{cat}/{period}/{order}: {e}")

        candidates = sorted(all_wallets.values(), key=lambda x: (x.pnl, x.volume, -x.rank), reverse=True)
        scores = sorted(
            [_rank_score_from_leaderboard(w, total_limit=max(50, len(candidates))) for w in candidates[:max(1, score_top)]],
            key=lambda x: x.score,
            reverse=True,
        )
        for sc in scores:
            save_wallet_score(sc)

        best_wallets = [s.wallet for s in scores[:min(40, len(scores))]]
        pos_results = await asyncio.gather(
            *(self.client.fetch_positions(w, 100) for w in best_wallets),
            return_exceptions=True,
        ) if best_wallets else []

        new_alerts = 0
        for w, rows in zip(best_wallets, pos_results):
            if isinstance(rows, list):
                ensure_v4_tables()
                record_position_changes(w, rows)
                save_positions(w, rows)
                ws = next((s for s in scores if s.wallet == w), None)
                if ws and ws.score >= 60:
                    for p in rows:
                        val = max(p.value, p.size * p.current_price)
                        if val >= 5000:
                            save_whale_alert(w, p.market, p.outcome, val, p.current_price, ws.score)
                            new_alerts += 1

        score_map = {s.wallet: s.score for s in scores}
        pos_dicts = cached_positions(8000)
        signals = self._build_consensus_from_positions(pos_dicts, score_map, MIN_CONSENSUS_WALLETS, MIN_CONSENSUS_VALUE, 20)
        save_consensus(signals)
        try:
            build_wallet_clusters(); signal_backtest_summary()
        except Exception:
            pass
        clear_old_consensus()

        top = scores[0] if scores else None
        status = "ok" if candidates else "error"
        save_alpha_scan_run("multi_leaderboard", "MULTI", "MIXED", "PNL/VOL",
                            len(candidates), len(candidates), len(scores),
                            top.wallet if top else "", top.score if top else 0,
                            status, "; ".join(errors)[:1000])
        return {
            "status": status,
            "wallets_found": len(candidates),
            "wallets_added": len(candidates),
            "wallets_scored": len(scores),
            "top_wallet": top.wallet if top else "",
            "top_score": top.score if top else 0,
            "consensus": len(signals),
            "whale_alerts": new_alerts,
            "errors": errors,
            "scores": scores,
            "signals": signals,
        }

    async def compare_wallet(self, my_wallet: str, smart_wallets: Optional[list[str]] = None) -> dict[str, Any]:
        my_positions = await self.client.fetch_positions(_norm(my_wallet))

        # Use cached consensus for speed
        signals = self.consensus_from_cache(top_n=30)
        if not signals:
            # Fall back to live
            signals = await self.consensus(smart_wallets, min_wallets=2, top_n=30)

        mine = {((p.market or p.title).lower().strip(), p.outcome.lower().strip()) for p in my_positions}
        sigkeys = {((s.market or s.title).lower().strip(), s.outcome.lower().strip()) for s in signals}
        overlap = mine & sigkeys
        shared = [s for s in signals if ((s.market or s.title).lower().strip(), s.outcome.lower().strip()) in mine]
        missing = [s for s in signals if ((s.market or s.title).lower().strip(), s.outcome.lower().strip()) not in mine]

        # Find risky positions: I hold them but no smart wallet does
        smart_keys = sigkeys | {((s.market or s.title).lower().strip(), s.outcome.lower().strip()) for s in signals}
        risky = [p for p in my_positions if ((p.market or p.title).lower().strip(), p.outcome.lower().strip()) not in smart_keys]

        exposure: dict[str, float] = {}
        for p in my_positions:
            cat = (p.title.split()[0] if p.title else "Other")[:20]
            exposure[cat] = exposure.get(cat, 0) + max(p.value, p.size * p.current_price)

        return {
            "my_positions": my_positions,
            "signals": signals,
            "shared": shared,
            "overlap_count": len(overlap),
            "overlap_pct": round(len(overlap) / max(1, len(sigkeys)) * 100, 1),
            "missing": missing[:10],
            "risky": risky[:5],
            "exposure": exposure,
        }


async def _safe(task: "asyncio.Task[Any]", default: Any) -> Any:
    try:
        return await task
    except Exception:
        return default
