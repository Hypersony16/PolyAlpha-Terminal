from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Tuple

from bot.db import get_conn


def ensure_stats_tables():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS btc_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end_ts REAL NOT NULL,
            signal TEXT NOT NULL,
            btc_price REAL NOT NULL,
            open_price REAL NOT NULL,
            model_up REAL NOT NULL,
            model_down REAL NOT NULL,
            edge REAL NOT NULL,
            confidence TEXT NOT NULL,
            market_slug TEXT,
            resolved INTEGER DEFAULT 0,
            result TEXT,
            close_price REAL
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_btc_predictions_user_time
        ON btc_predictions(user_id, created_at)
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS latency_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL,
            latency_ms REAL NOT NULL,
            ok INTEGER NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            signal TEXT NOT NULL,
            entry_price REAL NOT NULL,
            model_prob REAL NOT NULL,
            edge REAL NOT NULL,
            confidence TEXT NOT NULL,
            size_usdc REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
        )
    """)

    conn.commit()
    conn.close()


def log_latency(source: str, started_ts: float, ok: bool = True):
    latency_ms = (time.time() - started_ts) * 1000.0
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO latency_logs (created_at, source, latency_ms, ok)
        VALUES (?, ?, ?, ?)
    """, (datetime.utcnow().isoformat(), source, latency_ms, 1 if ok else 0))
    conn.commit()
    conn.close()


def record_prediction(user_id: int, model: Dict[str, Any]):
    ensure_stats_tables()

    window_start = model["window"]["start"].isoformat()
    window_end_ts = model["window"]["start"].timestamp() + 900.0
    signal = model["signal"]
    market_slug = (model.get("market") or {}).get("slug", "")

    conn = get_conn()
    cur = conn.cursor()

    # avoid spam: one prediction per user/window/signal every ~90 sec
    cur.execute("""
        SELECT id FROM btc_predictions
        WHERE user_id = ? AND window_start = ? AND signal = ?
        ORDER BY id DESC LIMIT 1
    """, (str(user_id), window_start, signal))
    row = cur.fetchone()
    if row:
        conn.close()
        return

    cur.execute("""
        INSERT INTO btc_predictions (
            user_id, created_at, window_start, window_end_ts, signal, btc_price, open_price,
            model_up, model_down, edge, confidence, market_slug
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(user_id),
        datetime.utcnow().isoformat(),
        window_start,
        window_end_ts,
        signal,
        float(model["price"]),
        float(model["open"]),
        float(model["model_up"]),
        float(model["model_down"]),
        float(model["edge"]),
        str(model["confidence"]),
        market_slug,
    ))

    conn.commit()
    conn.close()


def resolve_due_predictions(current_price: float):
    ensure_stats_tables()
    now_ts = time.time()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, signal, open_price FROM btc_predictions
        WHERE resolved = 0 AND window_end_ts <= ?
        LIMIT 200
    """, (now_ts,))
    rows = cur.fetchall()

    for pred_id, signal, open_price in rows:
        result = "UP" if current_price >= float(open_price) else "DOWN"
        cur.execute("""
            UPDATE btc_predictions
            SET resolved = 1, result = ?, close_price = ?
            WHERE id = ?
        """, (result, float(current_price), pred_id))

    conn.commit()
    conn.close()


def prediction_accuracy(user_id: int, hours: int) -> Dict[str, Any]:
    ensure_stats_tables()
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT signal, result, edge, confidence
        FROM btc_predictions
        WHERE user_id = ? AND created_at >= ? AND resolved = 1
    """, (str(user_id), since))
    rows = cur.fetchall()
    conn.close()

    total = len(rows)
    right = sum(1 for signal, result, *_ in rows if str(signal).upper() == str(result).upper())
    acc = (right / total) if total else 0.0

    high_rows = [r for r in rows if str(r[3]).lower() == "high"]
    high_total = len(high_rows)
    high_right = sum(1 for signal, result, *_ in high_rows if str(signal).upper() == str(result).upper())
    high_acc = (high_right / high_total) if high_total else 0.0

    avg_edge = sum(float(r[2] or 0) for r in rows) / total if total else 0.0

    return {
        "hours": hours,
        "total": total,
        "right": right,
        "accuracy": acc,
        "high_total": high_total,
        "high_right": high_right,
        "high_accuracy": high_acc,
        "avg_edge": avg_edge,
    }


def latency_summary(minutes: int = 60) -> Dict[str, Any]:
    ensure_stats_tables()
    since = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT source, latency_ms, ok FROM latency_logs
        WHERE created_at >= ?
    """, (since,))
    rows = cur.fetchall()
    conn.close()

    by_source: Dict[str, List[float]] = {}
    failures = 0
    for source, latency, ok in rows:
        if not ok:
            failures += 1
        by_source.setdefault(source, []).append(float(latency))

    out = {"total": len(rows), "failures": failures, "sources": {}}
    for source, vals in by_source.items():
        vals_sorted = sorted(vals)
        p50 = vals_sorted[int(len(vals_sorted) * 0.50)] if vals_sorted else 0
        p90 = vals_sorted[int(len(vals_sorted) * 0.90)-1] if len(vals_sorted) > 1 else p50
        out["sources"][source] = {
            "count": len(vals),
            "avg_ms": sum(vals) / len(vals),
            "p50_ms": p50,
            "p90_ms": p90,
        }
    return out


def log_paper_trade(user_id: int, model: Dict[str, Any], size_usdc: float = 1.0):
    ensure_stats_tables()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO paper_trades (
            user_id, created_at, signal, entry_price, model_prob, edge, confidence, size_usdc, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(user_id),
        datetime.utcnow().isoformat(),
        model["signal"],
        float(model["price"]),
        float(model["model_prob"]),
        float(model["edge"]),
        model["confidence"],
        float(size_usdc),
        "open",
    ))
    conn.commit()
    conn.close()


def paper_summary(user_id: int) -> Dict[str, Any]:
    ensure_stats_tables()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*), AVG(edge), SUM(size_usdc)
        FROM paper_trades WHERE user_id = ?
    """, (str(user_id),))
    count, avg_edge, volume = cur.fetchone()
    conn.close()
    return {
        "count": int(count or 0),
        "avg_edge": float(avg_edge or 0.0),
        "volume": float(volume or 0.0),
    }
