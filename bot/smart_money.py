"""PolyAlpha Smart Money Engine for Polymarket.
Read-only: ranking, consensus, portfolio comparison and whale-style intelligence.
"""
from __future__ import annotations

import asyncio, math
from dataclasses import dataclass
from typing import Any, Optional
import httpx

from bot.wallet_ranker import rank_wallet_metrics
from bot.alpha_store import save_wallet_score, save_positions, save_consensus, save_discovered_wallet, save_alpha_scan_run

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
DEFAULT_SMART_WALLETS: list[str] = []

@dataclass
class LeaderboardWallet:
    wallet: str
    rank: int = 0
    pnl: float = 0.0
    volume: float = 0.0
    username: str = ""
    raw: dict[str, Any] | None = None


def short_wallet(wallet: str) -> str:
    return (wallet[:6] + "…" + wallet[-4:]) if wallet and len(wallet) > 12 else (wallet or "unknown")

def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None or v == "": return d
        return float(v)
    except Exception: return d

def _s(v: Any, d: str = "") -> str:
    return d if v is None else str(v)

def _norm(w: str) -> str:
    return w.strip().lower()

@dataclass
class WalletScore:
    wallet: str; score: float; roi: float; pnl: float; volume: float; trades: int; winrate: float; open_value: float
    consistency: float = 0.0; recent_score: float = 0.0; drawdown: float = 0.0; label: str = ""

@dataclass
class Position:
    wallet: str; market: str; title: str; outcome: str; size: float; value: float; avg_price: float; current_price: float
    token_id: str = ""; condition_id: str = ""

@dataclass
class ConsensusSignal:
    title: str; outcome: str; market: str; score: float; wallets: int; total_value: float; avg_wallet_score: float; avg_price: float; best_wallets: list[str]
    token_id: str = ""; fair_value: float = 0.0; edge: float = 0.0; confidence: str = "Medium"

class PolyAlphaClient:
    def __init__(self, timeout: int = 18): self.timeout = timeout
    async def _get(self, url: str, params: Optional[dict] = None) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent":"PolyAlphaTerminal/2.0"}) as c:
            r = await c.get(url, params=params); r.raise_for_status(); return r.json()

    async def fetch_leaderboard(self, category: str = "OVERALL", time_period: str = "MONTH", order_by: str = "PNL", limit: int = 100) -> list[LeaderboardWallet]:
        """Fetch Polymarket's public trader leaderboard and extract wallet addresses.
        Uses flexible parsing because the public API has changed field names before.
        """
        category = (category or "OVERALL").upper()
        time_period = (time_period or "MONTH").upper()
        order_by = (order_by or "PNL").upper()
        candidates = [
            f"{DATA_API}/leaderboard",
            f"{DATA_API}/rankings",
            f"{DATA_API}/traders/leaderboard",
        ]
        last_error = None
        for url in candidates:
            try:
                data = await self._get(url, {"category": category, "timePeriod": time_period, "orderBy": order_by, "limit": limit})
                rows = data if isinstance(data, list) else data.get("leaderboard") or data.get("rankings") or data.get("data") or data.get("results") or []
                out: list[LeaderboardWallet] = []
                for idx, r in enumerate(rows, 1):
                    if not isinstance(r, dict):
                        continue
                    wallet = _s(r.get("proxyWallet") or r.get("wallet") or r.get("address") or r.get("user") or r.get("profileAddress") or r.get("funder"))
                    if not wallet.startswith("0x"):
                        continue
                    out.append(LeaderboardWallet(
                        wallet=_norm(wallet),
                        rank=int(_f(r.get("rank") or idx, idx)),
                        pnl=_f(r.get("pnl") or r.get("profit") or r.get("totalPnl") or r.get("amount")),
                        volume=_f(r.get("volume") or r.get("vol") or r.get("totalVolume")),
                        username=_s(r.get("name") or r.get("username") or r.get("pseudonym")),
                        raw=r,
                    ))
                if out:
                    return out
            except Exception as e:
                last_error = e
                continue
        if last_error:
            raise last_error
        return []

    async def fetch_wallet_value(self, wallet: str) -> float:
        try:
            data = await self._get(f"{DATA_API}/value", {"user": wallet})
            if isinstance(data, list) and data: return _f(data[0].get("value"))
            if isinstance(data, dict): return _f(data.get("value"))
        except Exception: pass
        return 0.0
    async def fetch_positions(self, wallet: str, limit: int = 250) -> list[Position]:
        raw: Any = []
        for params in ({"user":wallet,"limit":limit},{"address":wallet,"limit":limit}):
            try:
                raw = await self._get(f"{DATA_API}/positions", params)
                if raw: break
            except Exception: continue
        rows = raw if isinstance(raw, list) else raw.get("positions", []) if isinstance(raw, dict) else []
        out: list[Position] = []
        for p in rows:
            if not isinstance(p, dict): continue
            title = _s(p.get("title") or p.get("marketTitle") or p.get("question") or p.get("market"))
            outcome = _s(p.get("outcome") or p.get("outcomeName") or p.get("side") or p.get("asset"))
            market = _s(p.get("market") or p.get("marketSlug") or p.get("slug") or p.get("conditionId") or title)
            size = _f(p.get("size") or p.get("shares") or p.get("quantity") or p.get("balance"))
            value = _f(p.get("value") or p.get("currentValue") or p.get("cashPnl") or p.get("costBasis") or (size * _f(p.get("curPrice") or p.get("currentPrice") or p.get("price"))))
            avg_price = _f(p.get("avgPrice") or p.get("averagePrice") or p.get("price") or p.get("initialValue"))
            cur = _f(p.get("curPrice") or p.get("currentPrice") or p.get("price") or avg_price)
            token_id = _s(p.get("asset") or p.get("tokenId") or p.get("clobTokenId"))
            condition_id = _s(p.get("conditionId") or p.get("condition_id"))
            if title or market: out.append(Position(wallet, market, title or market, outcome or "YES", size, value, avg_price, cur, token_id, condition_id))
        return out
    async def fetch_activity(self, wallet: str, limit: int = 500) -> list[dict]:
        for endpoint, params in (("activity", {"user":wallet,"limit":limit}), ("trades", {"user":wallet,"limit":limit})):
            try:
                data = await self._get(f"{DATA_API}/{endpoint}", params)
                if isinstance(data, list): return [x for x in data if isinstance(x, dict)]
                if isinstance(data, dict):
                    rows = data.get("activity") or data.get("trades") or data.get("data") or []
                    if isinstance(rows, list): return [x for x in rows if isinstance(x, dict)]
            except Exception: continue
        return []

class SmartMoneyEngine:
    def __init__(self, wallets: Optional[list[str]] = None):
        self.client = PolyAlphaClient(); self.wallets = [_norm(w) for w in (wallets or DEFAULT_SMART_WALLETS) if w]
    async def score_wallet(self, wallet: str) -> WalletScore:
        w = _norm(wallet)
        value_task = asyncio.create_task(self.client.fetch_wallet_value(w))
        act_task = asyncio.create_task(self.client.fetch_activity(w, 500))
        pos_task = asyncio.create_task(self.client.fetch_positions(w, 250))
        value, activity, positions = await _safe(value_task, 0.0), await _safe(act_task, []), await _safe(pos_task, [])
        trades = len(activity)
        volume = sum(_f(x.get("size") or x.get("amount") or x.get("usdcSize") or x.get("value")) for x in activity)
        pnls = [_f(x.get("pnl") or x.get("realizedPnl") or x.get("profit"), 0.0) for x in activity]
        pnl = sum(pnls); wins = sum(1 for x in pnls if x > 0)
        winrate = wins / len(pnls) * 100 if pnls else 0.0
        roi = pnl / max(1.0, volume * 0.25) * 100 if volume else 0.0
        open_value = sum(max(p.value, p.size * p.current_price) for p in positions) or value
        score, consistency, recent, dd = rank_wallet_metrics(roi, winrate, trades, volume, pnl, open_value, pnls)
        ws = WalletScore(w, score, round(roi,1), round(pnl,2), round(volume,2), trades, round(winrate,1), round(open_value,2), consistency, recent, dd)
        save_wallet_score(ws); save_positions(w, positions)
        return ws
    async def score_wallets(self, wallets: Optional[list[str]] = None, top_n: int = 25) -> list[WalletScore]:
        targets = [_norm(w) for w in (wallets or self.wallets) if w]
        # chunk to avoid API bursts
        results = []
        for i in range(0, len(targets), 15):
            chunk = targets[i:i+15]
            res = await asyncio.gather(*(self.score_wallet(w) for w in chunk), return_exceptions=True)
            results += [r for r in res if isinstance(r, WalletScore)]
        return sorted(results, key=lambda x: x.score, reverse=True)[:top_n]
    async def consensus(self, wallets: Optional[list[str]] = None, min_wallets: int = 2, top_n: int = 10) -> list[ConsensusSignal]:
        scores = await self.score_wallets(wallets, top_n=100)
        score_map = {s.wallet:s.score for s in scores}
        good = [s.wallet for s in scores if s.score >= 35] or [s.wallet for s in scores[:30]]
        pos_lists = []
        for i in range(0, len(good), 20):
            res = await asyncio.gather(*(self.client.fetch_positions(w) for w in good[i:i+20]), return_exceptions=True)
            pos_lists += [r for r in res if isinstance(r, list)]
        buckets: dict[tuple[str,str], list[Position]] = {}
        for rows in pos_lists:
            for p in rows:
                if p.value <= 0 and p.size <= 0: continue
                key = ((p.market or p.title).lower().strip(), p.outcome.lower().strip())
                buckets.setdefault(key, []).append(p)
        signals: list[ConsensusSignal] = []
        for (market, outcome), positions in buckets.items():
            wallets_u = sorted(set(p.wallet for p in positions))
            if len(wallets_u) < min_wallets: continue
            total_value = sum(max(p.value, p.size * p.current_price) for p in positions)
            avg_score = sum(score_map.get(w,0) for w in wallets_u) / max(1,len(wallets_u))
            prices = [p.current_price or p.avg_price for p in positions if (p.current_price or p.avg_price)]
            avg_price = sum(prices)/len(prices) if prices else 0.0
            fair = min(0.99, max(0.01, (avg_score/100)*0.55 + min(0.35, len(wallets_u)/80) + min(0.10, math.log10(max(1,total_value))/60)))
            edge = fair - avg_price if avg_price else 0.0
            score = min(100.0, len(wallets_u)*7 + avg_score*0.55 + min(25, math.log10(max(1,total_value))*5) + max(0, edge*50))
            conf = "High" if score >= 75 and len(wallets_u) >= 5 else "Medium" if score >= 55 else "Low"
            best = sorted(wallets_u, key=lambda w: score_map.get(w,0), reverse=True)[:5]
            sample = positions[0]
            signals.append(ConsensusSignal(sample.title, sample.outcome, sample.market, round(score,1), len(wallets_u), round(total_value,2), round(avg_score,1), round(avg_price,3), best, sample.token_id, round(fair,3), round(edge,3), conf))
        signals = sorted(signals, key=lambda x: (x.score, x.total_value), reverse=True)[:top_n]
        save_consensus(signals)
        return signals

    async def discover_from_leaderboards(self, category: str = "OVERALL", time_period: str = "MONTH", order_by: str = "PNL", limit: int = 100, score_top: int = 50) -> dict[str, Any]:
        """Auto-discover wallets from public leaderboards, save them, score best candidates, and refresh consensus."""
        category = (category or "OVERALL").upper()
        time_period = (time_period or "MONTH").upper()
        order_by = (order_by or "PNL").upper()
        wallets_found: list[LeaderboardWallet] = []
        try:
            wallets_found = await self.client.fetch_leaderboard(category, time_period, order_by, limit)
            seen = set()
            unique = []
            for w in wallets_found:
                if w.wallet in seen:
                    continue
                seen.add(w.wallet); unique.append(w)
            wallets_found = unique
            for w in wallets_found:
                save_discovered_wallet(w.wallet, "leaderboard", category, time_period, order_by, w.pnl, w.volume, w.rank, w.raw or {}, w.username or f"LB {category} {time_period}")
            to_score = [w.wallet for w in wallets_found[:max(1, score_top)]]
            scores = await self.score_wallets(to_score, top_n=score_top) if to_score else []
            # Build consensus immediately from the newly scored best wallets.
            consensus = await self.consensus([s.wallet for s in scores[:min(80, len(scores))]], min_wallets=2, top_n=20) if scores else []
            top = scores[0] if scores else None
            save_alpha_scan_run("leaderboard", category, time_period, order_by, len(wallets_found), len(wallets_found), len(scores), top.wallet if top else "", top.score if top else 0, "ok", "")
            return {"status":"ok", "wallets_found": len(wallets_found), "wallets_added": len(wallets_found), "wallets_scored": len(scores), "top_wallet": top.wallet if top else "", "top_score": top.score if top else 0, "consensus": len(consensus), "scores": scores, "signals": consensus}
        except Exception as e:
            save_alpha_scan_run("leaderboard", category, time_period, order_by, len(wallets_found), 0, 0, "", 0, "error", str(e))
            return {"status":"error", "error": str(e), "wallets_found": len(wallets_found), "wallets_added": 0, "wallets_scored": 0, "scores": [], "signals": []}

    async def discover_multi_leaderboards(self, limit_per_board: int = 75, score_top: int = 75) -> dict[str, Any]:
        """Scan several useful boards to avoid one-hit wonders and collect better smart-wallet candidates."""
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
                    # Keep best rank record per wallet.
                    old = all_wallets.get(r.wallet)
                    if old is None or (r.pnl > old.pnl):
                        all_wallets[r.wallet] = r
                    save_discovered_wallet(r.wallet, "leaderboard", cat, period, order, r.pnl, r.volume, r.rank, r.raw or {}, r.username or f"LB {cat} {period}")
            except Exception as e:
                errors.append(f"{cat}/{period}/{order}: {e}")
        candidates = sorted(all_wallets.values(), key=lambda x: (x.pnl, x.volume), reverse=True)
        to_score = [w.wallet for w in candidates[:max(1, score_top)]]
        scores = await self.score_wallets(to_score, top_n=score_top) if to_score else []
        signals = await self.consensus([s.wallet for s in scores[:min(80, len(scores))]], min_wallets=2, top_n=20) if scores else []
        top = scores[0] if scores else None
        status = "ok" if candidates else "error"
        save_alpha_scan_run("multi_leaderboard", "MULTI", "MIXED", "PNL/VOL", len(candidates), len(candidates), len(scores), top.wallet if top else "", top.score if top else 0, status, "; ".join(errors)[:1000])
        return {"status":status, "wallets_found": len(candidates), "wallets_added": len(candidates), "wallets_scored": len(scores), "top_wallet": top.wallet if top else "", "top_score": top.score if top else 0, "consensus": len(signals), "errors": errors, "scores": scores, "signals": signals}

    async def compare_wallet(self, my_wallet: str, smart_wallets: Optional[list[str]]=None) -> dict[str,Any]:
        my_positions = await self.client.fetch_positions(_norm(my_wallet))
        signals = await self.consensus(smart_wallets, min_wallets=2, top_n=30)
        mine = {((p.market or p.title).lower().strip(), p.outcome.lower().strip()) for p in my_positions}
        sigkeys = {((s.market or s.title).lower().strip(), s.outcome.lower().strip()) for s in signals}
        overlap = mine & sigkeys
        missing = [s for s in signals if ((s.market or s.title).lower().strip(), s.outcome.lower().strip()) not in mine]
        exposure: dict[str,float] = {}
        for p in my_positions:
            cat = (p.title.split()[0] if p.title else "Other")[:20]
            exposure[cat] = exposure.get(cat, 0) + max(p.value, p.size*p.current_price)
        return {"my_positions":my_positions,"signals":signals,"overlap_count":len(overlap),"overlap_pct":round(len(overlap)/max(1,len(sigkeys))*100,1),"missing":missing[:10],"exposure":exposure}

async def _safe(task: "asyncio.Task[Any]", default: Any) -> Any:
    try: return await task
    except Exception: return default
