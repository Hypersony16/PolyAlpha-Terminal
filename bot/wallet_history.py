"""Wallet history, alpha scoring, research, and heatmap helpers for PolyAlpha Terminal v3.
Read-only intelligence layer. Uses cached wallet_positions and consensus_signals.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from bot.db import get_conn
from bot.alpha_store import ensure_alpha_tables, now_iso, cached_positions, latest_consensus, top_saved_wallet_scores
from bot.market_filters import is_quality_market, market_filter_reason


def ensure_history_tables() -> None:
    ensure_alpha_tables()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS wallet_position_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            market TEXT NOT NULL,
            title TEXT,
            outcome TEXT,
            value REAL DEFAULT 0,
            size REAL DEFAULT 0,
            avg_price REAL DEFAULT 0,
            current_price REAL DEFAULT 0,
            change_value REAL DEFAULT 0,
            action TEXT,
            token_id TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS wallet_daily_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            day TEXT NOT NULL,
            positions INTEGER DEFAULT 0,
            exposure REAL DEFAULT 0,
            markets INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE(wallet, day)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alpha_signal_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market TEXT NOT NULL,
            title TEXT,
            outcome TEXT,
            alpha_score REAL,
            wallets INTEGER,
            total_value REAL,
            edge REAL,
            confidence TEXT,
            category TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def category_for_title(title: str, market: str = "") -> str:
    s = f"{title} {market}".lower()
    if any(x in s for x in ["bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "xrp", "doge"]):
        return "Crypto"
    if any(x in s for x in ["election", "trump", "biden", "senate", "president", "fed", "rate cut", "politic"]):
        return "Politics/Economy"
    if any(x in s for x in ["fifa", "world cup", "win on", "spread", "o/u", "vs.", "nba", "nfl", "ufc", "mlb"]):
        return "Sports"
    if any(x in s for x in ["temperature", "rain", "weather", "snow", "hurricane"]):
        return "Weather"
    return "Other"


def market_url(slug_or_market: str) -> str:
    slug = str(slug_or_market or "").strip()
    if slug.startswith("http"):
        return slug
    if not slug:
        return "https://polymarket.com"
    return f"https://polymarket.com/event/{slug}"


def alpha_score_from_signal(sig: dict[str, Any]) -> tuple[float, dict[str, float]]:
    wallets = float(sig.get("wallets") or 0)
    value = float(sig.get("total_value") or 0)
    avg_score = float(sig.get("avg_wallet_score") or 0)
    edge = float(sig.get("edge") or 0)
    conviction = float(sig.get("weighted_conviction") or 0)
    confidence = str(sig.get("confidence") or "")

    consensus_pts = min(25.0, wallets * 5.0)
    quality_pts = min(25.0, avg_score * 0.32)
    capital_pts = min(20.0, math.log10(max(value, 1.0)) * 3.0)
    edge_pts = min(20.0, max(0.0, edge) * 120.0)
    conviction_pts = min(10.0, conviction * 0.10)
    conf_pts = {"High": 5.0, "Medium": 2.5, "Low": 0.0}.get(confidence, 0.0)
    penalty = 0.0
    if edge <= 0:
        penalty += 25.0
    if wallets < 3:
        penalty += 25.0
    elif wallets == 3:
        penalty += 5.0
    if avg_score < 55:
        penalty += 15.0

    raw = consensus_pts + quality_pts + capital_pts + edge_pts + conviction_pts + conf_pts - penalty
    score = max(0.0, min(100.0, raw))
    comps = {
        "consensus": round(consensus_pts, 1),
        "wallet_quality": round(quality_pts, 1),
        "capital": round(capital_pts, 1),
        "edge": round(edge_pts, 1),
        "conviction": round(conviction_pts, 1),
        "confidence": round(conf_pts, 1),
        "penalty": round(penalty, 1),
    }
    return round(score, 1), comps


def actionable_signals(limit: int = 10, min_alpha: float = 55.0) -> list[dict[str, Any]]:
    ensure_history_tables()
    rows = latest_consensus(80)
    out: list[dict[str, Any]] = []
    for r in rows:
        # Decision-first: actionable must be fresh, liquid enough, positive edge, and decent overlap.
        if not is_quality_market(r.get("title", ""), r.get("market", "")):
            continue
        if float(r.get("edge") or 0) < 0.07:
            continue
        if int(r.get("wallets") or 0) < 3:
            continue
        if float(r.get("avg_wallet_score") or 0) < 58:
            continue
        if float(r.get("total_value") or 0) < 5000:
            continue
        a, comps = alpha_score_from_signal(r)
        if a < min_alpha:
            continue
        x = dict(r)
        x["alpha_score"] = a
        x["alpha_components"] = comps
        x["category"] = category_for_title(x.get("title", ""), x.get("market", ""))
        out.append(x)
    out.sort(key=lambda z: (z.get("alpha_score", 0), z.get("edge", 0), z.get("total_value", 0)), reverse=True)
    return out[:limit]


def signal_quality_summary() -> dict[str, Any]:
    ensure_history_tables()
    scores = top_saved_wallet_scores(500)
    cons = latest_consensus(200)
    act = actionable_signals(50, min_alpha=0)
    return {
        "wallets": len(scores),
        "avg_wallet_score": round(sum(float(x.get("score") or 0) for x in scores) / max(1, len(scores)), 1),
        "elite": sum(1 for x in scores if float(x.get("score") or 0) >= 80),
        "strong": sum(1 for x in scores if 65 <= float(x.get("score") or 0) < 80),
        "good": sum(1 for x in scores if 50 <= float(x.get("score") or 0) < 65),
        "weak": sum(1 for x in scores if float(x.get("score") or 0) < 50),
        "consensus": len(cons),
        "actionable": len(act),
        "negative_edge": sum(1 for x in cons if float(x.get("edge") or 0) <= 0),
        "stale_or_outright_hidden": sum(1 for x in cons if not is_quality_market(x.get("title", ""), x.get("market", ""))),
        "avg_signal_wallets": round(sum(float(x.get("wallets") or 0) for x in cons) / max(1, len(cons)), 1),
        "avg_signal_value": round(sum(float(x.get("total_value") or 0) for x in cons) / max(1, len(cons)), 2),
    }


def heatmap_data() -> list[dict[str, Any]]:
    ensure_history_tables()
    rows = latest_consensus(200)
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"signals": 0, "value": 0.0, "alpha": 0.0, "positive": 0})
    for r in rows:
        if not is_quality_market(r.get("title", ""), r.get("market", "")):
            continue
        cat = category_for_title(r.get("title", ""), r.get("market", ""))
        a, _ = alpha_score_from_signal(r)
        buckets[cat]["signals"] += 1
        buckets[cat]["value"] += float(r.get("total_value") or 0)
        buckets[cat]["alpha"] += a
        if float(r.get("edge") or 0) > 0:
            buckets[cat]["positive"] += 1
    out = []
    for cat, b in buckets.items():
        signals = max(1, int(b["signals"]))
        out.append({
            "category": cat,
            "signals": int(b["signals"]),
            "positive": int(b["positive"]),
            "value": round(b["value"], 2),
            "avg_alpha": round(b["alpha"] / signals, 1),
        })
    return sorted(out, key=lambda x: (x["avg_alpha"], x["value"]), reverse=True)


def research_market(query: str = "", limit: int = 1) -> list[dict[str, Any]]:
    ensure_history_tables()
    q = str(query or "").lower().strip()
    rows = [r for r in latest_consensus(200) if is_quality_market(r.get("title", ""), r.get("market", ""))]
    if q:
        rows = [r for r in rows if q in str(r.get("title", "")).lower() or q in str(r.get("market", "")).lower()]
    if not rows:
        rows = actionable_signals(10, min_alpha=0)
    scored = []
    for r in rows:
        a, comps = alpha_score_from_signal(r)
        x = dict(r)
        x["alpha_score"] = a
        x["alpha_components"] = comps
        x["category"] = category_for_title(x.get("title", ""), x.get("market", ""))
        scored.append(x)
    scored.sort(key=lambda x: x.get("alpha_score", 0), reverse=True)
    return scored[:limit]


def save_signal_snapshot(sig: dict[str, Any]) -> None:
    ensure_history_tables()
    conn = get_conn()
    conn.execute(
        """INSERT INTO alpha_signal_snapshots(market,title,outcome,alpha_score,wallets,total_value,edge,confidence,category,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (sig.get("market"), sig.get("title"), sig.get("outcome"), sig.get("alpha_score"), sig.get("wallets"),
         sig.get("total_value"), sig.get("edge"), sig.get("confidence"), sig.get("category"), now_iso())
    )
    conn.commit()
    conn.close()
