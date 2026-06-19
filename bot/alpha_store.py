"""Small SQLite helpers for PolyAlpha wallet lists/settings."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from bot.db import get_conn


def ensure_alpha_tables() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alpha_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL UNIQUE,
            label TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alpha_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def add_alpha_wallet(wallet: str, label: str = "") -> None:
    ensure_alpha_tables()
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO alpha_wallets(wallet, label, created_at) VALUES (?, ?, ?)",
        (wallet.lower().strip(), label.strip(), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def remove_alpha_wallet(wallet: str) -> int:
    ensure_alpha_tables()
    conn = get_conn()
    cur = conn.execute("DELETE FROM alpha_wallets WHERE wallet = ?", (wallet.lower().strip(),))
    conn.commit()
    count = cur.rowcount
    conn.close()
    return count


def list_alpha_wallets(limit: int = 200) -> List[tuple[str, str]]:
    ensure_alpha_tables()
    conn = get_conn()
    rows = conn.execute("SELECT wallet, COALESCE(label, '') FROM alpha_wallets ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows]


def set_alpha_setting(key: str, value: str) -> None:
    ensure_alpha_tables()
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO alpha_settings(key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def get_alpha_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    ensure_alpha_tables()
    conn = get_conn()
    row = conn.execute("SELECT value FROM alpha_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default
