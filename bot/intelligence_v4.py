"""PolyAlpha Intelligence v4 helpers.

Adds historical wallet tracking, position-change detection, clustering, and
lightweight signal backtesting from cached SQLite data. Designed to be safe:
read-only for Polymarket and no trading execution.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from bot.db import get_conn
from bot.alpha_store import ensure_alpha_tables, now_iso, cached_positions, latest_consensus, top_saved_wallet_scores
from bot.wallet_history import category_for_title, alpha_score_from_signal


def ensure_v4_tables() -> None:
    ensure_alpha_tables()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS wallet_position_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            market TEXT NOT NULL,
            title TEXT,
            outcome TEXT,
            action TEXT NOT NULL,
            old_value REAL DEFAULT 0,
            new_value REAL DEFAULT 0,
            delta_value REAL DEFAULT 0,
            old_size REAL DEFAULT 0,
            new_size REAL DEFAULT 0,
            avg_price REAL DEFAULT 0,
            current_price REAL DEFAULT 0,
            token_id TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_wpe_wallet_time ON wallet_position_events(wallet, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_wpe_market_time ON wallet_position_events(market, created_at)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS wallet_cluster_scores (
            cluster TEXT PRIMARY KEY,
            wallets INTEGER DEFAULT 0,
            avg_score REAL DEFAULT 0,
            total_value REAL DEFAULT 0,
            signals INTEGER DEFAULT 0,
            top_market TEXT,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signal_backtest_stats (
            bucket TEXT PRIMARY KEY,
            signals INTEGER DEFAULT 0,
            positive_edge INTEGER DEFAULT 0,
            avg_alpha REAL DEFAULT 0,
            avg_edge REAL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit(); conn.close()


def _pos_key(p: Any) -> tuple[str, str, str]:
    return (str(getattr(p, 'market', '') or '').lower(), str(getattr(p, 'outcome', '') or '').lower(), str(getattr(p, 'token_id', '') or '').lower())


def record_position_changes(wallet: str, new_positions: list[Any], min_delta: float = 25.0) -> int:
    """Compare fresh API positions vs last cached wallet_positions and save add/reduce/close events.

    This runs before save_positions(). It is intentionally conservative: tiny deltas are ignored to
    avoid Telegram/feed noise from price movement or rounding.
    """
    ensure_v4_tables()
    w = wallet.lower().strip()
    conn = get_conn()
    rows = conn.execute(
        """SELECT market,title,outcome,size,value,avg_price,current_price,token_id
           FROM wallet_positions WHERE wallet=?""", (w,)
    ).fetchall()
    old: dict[tuple[str,str,str], dict[str,Any]] = {}
    for r in rows:
        d = {"market": r[0], "title": r[1], "outcome": r[2], "size": float(r[3] or 0), "value": float(r[4] or 0),
             "avg_price": float(r[5] or 0), "current_price": float(r[6] or 0), "token_id": r[7] or ""}
        old[(str(d['market']).lower(), str(d['outcome']).lower(), str(d['token_id']).lower())] = d
    new: dict[tuple[str,str,str], Any] = {_pos_key(p): p for p in new_positions}
    t = now_iso(); count = 0

    def insert(p_market, p_title, p_outcome, action, old_v, new_v, old_s, new_s, avg_p, cur_p, token):
        nonlocal count
        delta = float(new_v or 0) - float(old_v or 0)
        conn.execute(
            """INSERT INTO wallet_position_events(wallet,market,title,outcome,action,old_value,new_value,delta_value,
               old_size,new_size,avg_price,current_price,token_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (w, p_market, p_title, p_outcome, action, old_v, new_v, delta, old_s, new_s, avg_p, cur_p, token, t),
        )
        count += 1

    for k, p in new.items():
        nv = float(getattr(p, 'value', 0) or 0)
        ns = float(getattr(p, 'size', 0) or 0)
        ov = old.get(k, {}).get('value', 0.0)
        os = old.get(k, {}).get('size', 0.0)
        if k not in old and nv >= min_delta:
            insert(p.market, p.title, p.outcome, 'OPEN', 0.0, nv, 0.0, ns, p.avg_price, p.current_price, p.token_id)
        elif k in old:
            diff = nv - ov
            if abs(diff) >= min_delta:
                action = 'ADD' if diff > 0 else 'REDUCE'
                insert(p.market, p.title, p.outcome, action, ov, nv, os, ns, p.avg_price, p.current_price, p.token_id)
    for k, d in old.items():
        if k not in new and float(d.get('value') or 0) >= min_delta:
            insert(d['market'], d.get('title',''), d.get('outcome',''), 'CLOSE', d.get('value',0), 0.0, d.get('size',0), 0.0, d.get('avg_price',0), d.get('current_price',0), d.get('token_id',''))
    conn.commit(); conn.close()
    return count


def latest_position_events(limit: int = 12) -> list[dict[str, Any]]:
    ensure_v4_tables(); conn = get_conn()
    rows = conn.execute(
        """SELECT wallet,market,title,outcome,action,old_value,new_value,delta_value,current_price,created_at
           FROM wallet_position_events ORDER BY id DESC LIMIT ?""", (limit,)
    ).fetchall(); conn.close()
    keys = ['wallet','market','title','outcome','action','old_value','new_value','delta_value','current_price','created_at']
    return [dict(zip(keys, r)) for r in rows]


def wallet_history_summary(wallet: str | None = None, limit: int = 8) -> dict[str, Any]:
    ensure_v4_tables(); conn = get_conn()
    params: tuple[Any, ...] = ()
    where = ''
    if wallet:
        where = 'WHERE wallet=?'; params = (wallet.lower().strip(),)
    rows = conn.execute(
        f"""SELECT wallet,market,title,outcome,action,old_value,new_value,delta_value,current_price,created_at
            FROM wallet_position_events {where} ORDER BY id DESC LIMIT ?""", params + (limit,)
    ).fetchall()
    totals = conn.execute(
        f"""SELECT action, COUNT(*), SUM(ABS(delta_value)) FROM wallet_position_events {where} GROUP BY action""", params
    ).fetchall()
    conn.close()
    keys = ['wallet','market','title','outcome','action','old_value','new_value','delta_value','current_price','created_at']
    return {"events": [dict(zip(keys, r)) for r in rows], "totals": {r[0]: {"count": r[1], "value": float(r[2] or 0)} for r in totals}}


def build_wallet_clusters() -> list[dict[str, Any]]:
    """Cluster current smart-wallet positions by market category."""
    ensure_v4_tables()
    scores = {s['wallet'].lower(): float(s.get('score') or 0) for s in top_saved_wallet_scores(500)}
    pos = cached_positions(10000)
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"wallets": set(), "value": 0.0, "score_sum": 0.0, "signals": 0, "markets": defaultdict(float)})
    for p in pos:
        w = str(p.get('wallet') or '').lower()
        sc = scores.get(w, 0.0)
        if sc < 50:
            continue
        cat = category_for_title(p.get('title',''), p.get('market',''))
        val = float(p.get('value') or 0)
        b = buckets[cat]
        if w not in b['wallets']:
            b['wallets'].add(w); b['score_sum'] += sc
        b['value'] += val; b['signals'] += 1; b['markets'][p.get('market','')] += val
    out = []
    conn = get_conn()
    for cat, b in buckets.items():
        n = len(b['wallets']); avg = b['score_sum'] / max(1, n)
        top_market = max(b['markets'].items(), key=lambda kv: kv[1])[0] if b['markets'] else ''
        row = {"cluster": cat, "wallets": n, "avg_score": round(avg,1), "total_value": round(b['value'],2), "signals": int(b['signals']), "top_market": top_market}
        out.append(row)
        conn.execute("""INSERT INTO wallet_cluster_scores(cluster,wallets,avg_score,total_value,signals,top_market,updated_at)
                      VALUES(?,?,?,?,?,?,?) ON CONFLICT(cluster) DO UPDATE SET wallets=excluded.wallets,avg_score=excluded.avg_score,total_value=excluded.total_value,signals=excluded.signals,top_market=excluded.top_market,updated_at=excluded.updated_at""",
                     (cat, row['wallets'], row['avg_score'], row['total_value'], row['signals'], top_market, now_iso()))
    conn.commit(); conn.close()
    return sorted(out, key=lambda x: (x['avg_score'], x['total_value']), reverse=True)


def signal_backtest_summary() -> list[dict[str, Any]]:
    """Lightweight backtest diagnostics from cached historical consensus snapshots.

    It is not a true realized-PnL backtest yet; it shows signal distribution by alpha bucket so we can
    later compare with outcomes as markets resolve.
    """
    ensure_v4_tables()
    rows = latest_consensus(300)
    buckets: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "pos": 0, "alpha": 0, "edge": 0})
    for r in rows:
        alpha, _ = alpha_score_from_signal(r)
        if alpha >= 85: b = '85+ Elite'
        elif alpha >= 75: b = '75-85 Strong'
        elif alpha >= 65: b = '65-75 Moderate'
        else: b = '<65 Weak'
        buckets[b]['n'] += 1; buckets[b]['pos'] += 1 if float(r.get('edge') or 0) > 0 else 0
        buckets[b]['alpha'] += alpha; buckets[b]['edge'] += float(r.get('edge') or 0)
    order = ['85+ Elite','75-85 Strong','65-75 Moderate','<65 Weak']
    out=[]; conn=get_conn(); t=now_iso()
    for b in order:
        d=buckets.get(b)
        if not d or d['n'] <= 0: continue
        row={"bucket":b,"signals":int(d['n']),"positive_edge":int(d['pos']),"avg_alpha":round(d['alpha']/d['n'],1),"avg_edge":round(d['edge']/d['n'],3)}
        out.append(row)
        conn.execute("""INSERT INTO signal_backtest_stats(bucket,signals,positive_edge,avg_alpha,avg_edge,updated_at)
                      VALUES(?,?,?,?,?,?) ON CONFLICT(bucket) DO UPDATE SET signals=excluded.signals,positive_edge=excluded.positive_edge,avg_alpha=excluded.avg_alpha,avg_edge=excluded.avg_edge,updated_at=excluded.updated_at""",
                     (b,row['signals'],row['positive_edge'],row['avg_alpha'],row['avg_edge'],t))
    conn.commit(); conn.close(); return out
