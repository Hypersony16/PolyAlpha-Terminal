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


def log_latency(source: str, started_ts: float, ok: bool):
    latency_ms = (time.time() - started_ts) * 1000
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO latency_logs(created_at, source, latency_ms, ok) VALUES (?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), source, round(latency_ms, 2), 1 if ok else 0),
        )
        conn.commit()
    except Exception:
        pass
    conn.close()


def record_prediction(user_id: int, window_start: str, window_end_ts: float, signal: str,
                      btc_price: float, open_price: float, model_up: float, model_down: float,
                      edge: float, confidence: str, market_slug: str = ""):
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO btc_predictions
               (user_id, created_at, window_start, window_end_ts, signal, btc_price, open_price,
                model_up, model_down, edge, confidence, market_slug)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(user_id), datetime.utcnow().isoformat(), window_start, window_end_ts,
             signal, btc_price, open_price, model_up, model_down, edge, confidence, market_slug),
        )
        conn.commit()
    except Exception:
        pass
    conn.close()


def resolve_due_predictions(current_price: float):
    now_ts = time.time()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, signal, open_price FROM btc_predictions WHERE resolved = 0 AND window_end_ts <= ?",
            (now_ts,),
        )
        rows = cur.fetchall()
        for row in rows:
            pred_id, signal, open_price = row
            if signal == "UP":
                result = "WIN" if current_price > open_price else "LOSS"
            else:
                result = "WIN" if current_price < open_price else "LOSS"
            conn.execute(
                "UPDATE btc_predictions SET resolved = 1, result = ?, close_price = ? WHERE id = ?",
                (result, current_price, pred_id),
            )
        conn.commit()
    except Exception:
        pass
    conn.close()


def prediction_accuracy(user_id: int, hours: int = 24) -> Dict[str, Any]:
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT COUNT(*), SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END)
               FROM btc_predictions WHERE user_id = ? AND resolved = 1 AND created_at >= ?""",
            (str(user_id), cutoff),
        )
        row = cur.fetchone()
        total = row[0] or 0
        wins = row[1] or 0
    except Exception:
        total, wins = 0, 0
    conn.close()
    return {
        "total": total,
        "wins": wins,
        "losses": total - wins,
        "accuracy": round(wins / total * 100, 1) if total else 0.0,
    }


def latency_summary(hours: int = 1) -> Dict[str, Any]:
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT source, AVG(latency_ms), MIN(latency_ms), MAX(latency_ms),
                      COUNT(*), SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END)
               FROM latency_logs WHERE created_at >= ?
               GROUP BY source""",
            (cutoff,),
        )
        rows = cur.fetchall()
    except Exception:
        rows = []
    conn.close()
    result = {}
    for row in rows:
        source, avg_ms, min_ms, max_ms, count, ok_count = row
        result[source] = {
            "avg_ms": round(avg_ms or 0, 1),
            "min_ms": round(min_ms or 0, 1),
            "max_ms": round(max_ms or 0, 1),
            "count": count,
            "ok_rate": round((ok_count / count * 100) if count else 0, 1),
        }
    return result


def log_paper_trade(user_id: int, signal: str, entry_price: float, model_prob: float,
                    edge: float, confidence: str, size_usdc: float):
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO paper_trades
               (user_id, created_at, signal, entry_price, model_prob, edge, confidence, size_usdc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(user_id), datetime.utcnow().isoformat(), signal, entry_price,
             model_prob, edge, confidence, size_usdc),
        )
        conn.commit()
    except Exception:
        pass
    conn.close()


def paper_summary(user_id: int) -> Dict[str, Any]:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(size_usdc), 0) FROM paper_trades WHERE user_id = ?",
            (str(user_id),),
        )
        row = cur.fetchone()
        total = row[0] or 0
        total_size = row[1] or 0.0
    except Exception:
        total, total_size = 0, 0.0
    conn.close()
    return {"total": total, "total_size": round(total_size, 2)}
