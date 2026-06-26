"""SQLite persistence for PolyAlpha Terminal.
Keeps existing PolyScalpBot tables intact and only adds alpha_* tables.
v2.3: added save_whale_alert, improved latest_consensus ordering.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from bot.db import get_conn
from bot.market_filters import is_quality_market


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_alpha_tables() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alpha_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL UNIQUE,
            label TEXT,
            notes TEXT,
            score REAL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alpha_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS wallet_scores (
            wallet TEXT PRIMARY KEY,
            label TEXT,
            score REAL,
            roi REAL,
            pnl REAL,
            volume REAL,
            trades INTEGER,
            winrate REAL,
            open_value REAL,
            consistency REAL,
            recent_score REAL,
            drawdown REAL,
            components_json TEXT,
            last_scanned TEXT,
            raw_json TEXT
        )
    """)
    # Add components_json column if upgrading from older schema
    try:
        cur.execute("ALTER TABLE wallet_scores ADD COLUMN components_json TEXT")
        conn.commit()
    except Exception:
        pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS wallet_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            market TEXT NOT NULL,
            title TEXT,
            outcome TEXT,
            size REAL,
            value REAL,
            avg_price REAL,
            current_price REAL,
            token_id TEXT,
            condition_id TEXT,
            first_seen TEXT,
            last_seen TEXT,
            UNIQUE(wallet, market, outcome, token_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS consensus_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market TEXT NOT NULL,
            title TEXT,
            outcome TEXT,
            score REAL,
            wallets INTEGER,
            total_value REAL,
            avg_wallet_score REAL,
            avg_price REAL,
            fair_value REAL,
            edge REAL,
            confidence TEXT,
            best_wallets TEXT,
            token_id TEXT,
            weighted_conviction REAL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    try:
        cur.execute("ALTER TABLE consensus_signals ADD COLUMN weighted_conviction REAL DEFAULT 0")
        conn.commit()
    except Exception:
        pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS whale_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            market TEXT NOT NULL,
            outcome TEXT,
            value REAL,
            price REAL,
            score REAL,
            created_at TEXT NOT NULL,
            sent INTEGER DEFAULT 0,
            UNIQUE(wallet, market, outcome, created_at)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            total_value REAL,
            positions INTEGER,
            exposure_json TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alpha_scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            category TEXT,
            time_period TEXT,
            order_by TEXT,
            wallets_found INTEGER DEFAULT 0,
            wallets_added INTEGER DEFAULT 0,
            wallets_scored INTEGER DEFAULT 0,
            top_wallet TEXT,
            top_score REAL,
            status TEXT,
            error TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alpha_wallet_discovery (
            wallet TEXT PRIMARY KEY,
            source TEXT,
            category TEXT,
            time_period TEXT,
            order_by TEXT,
            pnl REAL,
            volume REAL,
            rank INTEGER,
            raw_json TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
    """)
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
        CREATE TABLE IF NOT EXISTS market_rankings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market TEXT NOT NULL,
            title TEXT,
            score REAL,
            liquidity REAL,
            volume REAL,
            wallets INTEGER,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def add_alpha_wallet(wallet: str, label: str = "", notes: str = "") -> None:
    ensure_alpha_tables()
    w = wallet.lower().strip()
    conn = get_conn()
    conn.execute(
        """INSERT INTO alpha_wallets(wallet,label,notes,created_at,updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(wallet) DO UPDATE SET label=excluded.label, notes=excluded.notes, updated_at=excluded.updated_at""",
        (w, label.strip(), notes.strip(), now_iso(), now_iso()),
    )
    conn.commit()
    conn.close()


def remove_alpha_wallet(wallet: str) -> int:
    ensure_alpha_tables()
    conn = get_conn()
    cur = conn.execute("DELETE FROM alpha_wallets WHERE wallet=?", (wallet.lower().strip(),))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def list_alpha_wallets(limit: int = 500) -> list[tuple[str, str]]:
    ensure_alpha_tables()
    conn = get_conn()
    rows = conn.execute(
        "SELECT wallet, COALESCE(label,'') FROM alpha_wallets ORDER BY score DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows]


def set_alpha_setting(key: str, value: str) -> None:
    ensure_alpha_tables()
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO alpha_settings(key,value) VALUES(?,?)", (key, value))
    conn.commit()
    conn.close()


def get_alpha_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    ensure_alpha_tables()
    conn = get_conn()
    row = conn.execute("SELECT value FROM alpha_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def save_wallet_score(score: Any) -> None:
    ensure_alpha_tables()
    conn = get_conn()
    raw = json.dumps(getattr(score, "__dict__", {}), default=str)
    comps = json.dumps(getattr(score, "components", {}), default=str)
    conn.execute(
        """INSERT OR REPLACE INTO wallet_scores(wallet,label,score,roi,pnl,volume,trades,winrate,open_value,
           consistency,recent_score,drawdown,components_json,last_scanned,raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            score.wallet, getattr(score, "label", ""), score.score, score.roi, score.pnl,
            score.volume, score.trades, score.winrate, score.open_value,
            getattr(score, "consistency", 0), getattr(score, "recent_score", 0),
            getattr(score, "drawdown", 0), comps, now_iso(), raw,
        ),
    )
    conn.execute(
        "UPDATE alpha_wallets SET score=?, updated_at=? WHERE wallet=?",
        (score.score, now_iso(), score.wallet),
    )
    conn.commit()
    conn.close()


def top_saved_wallet_scores(limit: int = 25) -> list[dict[str, Any]]:
    ensure_alpha_tables()
    conn = get_conn()
    rows = conn.execute(
        """SELECT wallet,label,score,roi,pnl,volume,trades,winrate,open_value,
           consistency,recent_score,drawdown,components_json,last_scanned
           FROM wallet_scores ORDER BY score DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    keys = ["wallet", "label", "score", "roi", "pnl", "volume", "trades", "winrate",
            "open_value", "consistency", "recent_score", "drawdown", "components_json", "last_scanned"]
    result = []
    for r in rows:
        d = dict(zip(keys, r))
        try:
            d["components"] = json.loads(d.get("components_json") or "{}")
        except Exception:
            d["components"] = {}
        result.append(d)
    return result


def save_positions(wallet: str, positions: list[Any]) -> None:
    ensure_alpha_tables()
    conn = get_conn()
    t = now_iso()
    for p in positions:
        conn.execute(
            """INSERT INTO wallet_positions(wallet,market,title,outcome,size,value,avg_price,current_price,
               token_id,condition_id,first_seen,last_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(wallet,market,outcome,token_id) DO UPDATE SET
               size=excluded.size,value=excluded.value,avg_price=excluded.avg_price,
               current_price=excluded.current_price,last_seen=excluded.last_seen""",
            (wallet, p.market, p.title, p.outcome, p.size, p.value,
             p.avg_price, p.current_price, p.token_id, p.condition_id, t, t),
        )
    conn.commit()
    conn.close()


def save_consensus(signals: list[Any]) -> None:
    ensure_alpha_tables()
    conn = get_conn()
    t = now_iso()
    for s in signals:
        conn.execute(
            """INSERT INTO consensus_signals(market,title,outcome,score,wallets,total_value,avg_wallet_score,
               avg_price,fair_value,edge,confidence,best_wallets,token_id,weighted_conviction,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                s.market, s.title, s.outcome, s.score, s.wallets, s.total_value,
                s.avg_wallet_score, s.avg_price,
                getattr(s, "fair_value", 0), getattr(s, "edge", 0),
                getattr(s, "confidence", ""), json.dumps(s.best_wallets),
                s.token_id, getattr(s, "weighted_conviction", 0), t,
            ),
        )
    conn.commit()
    conn.close()


def latest_consensus(limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent unique consensus signals (deduplicated by market+outcome)."""
    ensure_alpha_tables()
    conn = get_conn()
    # Get recent signals, then deduplicate by market+outcome in Python keeping highest score
    rows = conn.execute(
        """SELECT market,title,outcome,score,wallets,total_value,avg_wallet_score,avg_price,
           fair_value,edge,confidence,weighted_conviction,created_at
           FROM consensus_signals ORDER BY id DESC LIMIT ?""",
        (limit * 5,),
    ).fetchall()
    conn.close()
    keys = ["market", "title", "outcome", "score", "wallets", "total_value",
            "avg_wallet_score", "avg_price", "fair_value", "edge", "confidence",
            "weighted_conviction", "created_at"]
    seen: set[tuple] = set()
    result = []
    for r in rows:
        d = dict(zip(keys, r))
        if not is_quality_market(d.get("title", ""), d.get("market", "")):
            continue
        key = (d.get("market", ""), d.get("outcome", ""))
        if key not in seen:
            seen.add(key)
            result.append(d)
        if len(result) >= limit:
            break
    return result


def cached_positions(limit: int = 5000) -> list[dict[str, Any]]:
    """Return latest saved positions for fast consensus/terminal views."""
    ensure_alpha_tables()
    conn = get_conn()
    rows = conn.execute(
        """SELECT wallet,market,title,outcome,size,value,avg_price,current_price,token_id,condition_id,last_seen
           FROM wallet_positions ORDER BY last_seen DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    keys = ["wallet", "market", "title", "outcome", "size", "value",
            "avg_price", "current_price", "token_id", "condition_id", "last_seen"]
    return [dict(zip(keys, r)) for r in rows]


def clear_old_consensus(keep_last: int = 400) -> None:
    ensure_alpha_tables()
    conn = get_conn()
    conn.execute(
        "DELETE FROM consensus_signals WHERE id NOT IN (SELECT id FROM consensus_signals ORDER BY id DESC LIMIT ?)",
        (keep_last,),
    )
    conn.commit()
    conn.close()


def save_whale_alert(wallet: str, market: str, outcome: str, value: float, price: float, score: float) -> None:
    ensure_alpha_tables()
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO whale_alerts(wallet,market,outcome,value,price,score,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (wallet.lower().strip(), market, outcome, value, price, score, now_iso()),
        )
        conn.commit()
    except Exception:
        pass
    conn.close()


def latest_whale_alerts(limit: int = 20) -> list[dict[str, Any]]:
    ensure_alpha_tables()
    conn = get_conn()
    rows = conn.execute(
        """SELECT wallet,market,outcome,value,price,score,created_at
           FROM whale_alerts ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    keys = ["wallet", "market", "outcome", "value", "price", "score", "created_at"]
    return [dict(zip(keys, r)) for r in rows]


def save_discovered_wallet(
    wallet: str,
    source: str = "leaderboard",
    category: str = "OVERALL",
    time_period: str = "MONTH",
    order_by: str = "PNL",
    pnl: float = 0.0,
    volume: float = 0.0,
    rank: int = 0,
    raw: Any = None,
    label: str = "",
) -> None:
    ensure_alpha_tables()
    w = wallet.lower().strip()
    if not w.startswith("0x"):
        return
    t = now_iso()
    raw_json = json.dumps(raw or {}, default=str)[:10000]
    conn = get_conn()
    conn.execute(
        """INSERT INTO alpha_wallet_discovery(wallet,source,category,time_period,order_by,pnl,volume,rank,raw_json,first_seen,last_seen)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(wallet) DO UPDATE SET source=excluded.source,category=excluded.category,
           time_period=excluded.time_period,order_by=excluded.order_by,pnl=excluded.pnl,
           volume=excluded.volume,rank=excluded.rank,raw_json=excluded.raw_json,last_seen=excluded.last_seen""",
        (w, source, category, time_period, order_by, pnl, volume, rank, raw_json, t, t),
    )
    conn.execute(
        """INSERT INTO alpha_wallets(wallet,label,notes,created_at,updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(wallet) DO UPDATE SET updated_at=excluded.updated_at""",
        (w, label or f"{source}:{category}:{time_period}", "auto-discovered", t, t),
    )
    conn.commit()
    conn.close()


def save_alpha_scan_run(
    source: str, category: str, time_period: str, order_by: str,
    wallets_found: int, wallets_added: int, wallets_scored: int,
    top_wallet: str = "", top_score: float = 0.0, status: str = "ok", error: str = "",
) -> None:
    ensure_alpha_tables()
    conn = get_conn()
    conn.execute(
        """INSERT INTO alpha_scan_runs(source,category,time_period,order_by,wallets_found,wallets_added,
           wallets_scored,top_wallet,top_score,status,error,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (source, category, time_period, order_by, wallets_found, wallets_added,
         wallets_scored, top_wallet, top_score, status, error[:1000], now_iso()),
    )
    conn.commit()
    conn.close()


def latest_alpha_scans(limit: int = 5) -> list[dict[str, Any]]:
    ensure_alpha_tables()
    conn = get_conn()
    rows = conn.execute(
        """SELECT source,category,time_period,order_by,wallets_found,wallets_added,wallets_scored,
           top_wallet,top_score,status,error,created_at
           FROM alpha_scan_runs ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    keys = ["source", "category", "time_period", "order_by", "wallets_found", "wallets_added",
            "wallets_scored", "top_wallet", "top_score", "status", "error", "created_at"]
    return [dict(zip(keys, r)) for r in rows]


def discovered_wallet_count() -> int:
    ensure_alpha_tables()
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) FROM alpha_wallet_discovery").fetchone()
    conn.close()
    return int(row[0] or 0)
