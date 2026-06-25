import json
import sqlite3
from datetime import datetime

from bot.config import DB_PATH


def get_conn():
    # Railway + APScheduler can hit SQLite concurrently.
    # WAL + busy_timeout prevents "database is locked" from normal short writes.
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS global_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS active_users (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_seen TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            city TEXT NOT NULL,
            market_date TEXT NOT NULL,
            consensus_high REAL NOT NULL,
            model_source TEXT NOT NULL,
            best_temp INTEGER NOT NULL,
            model_prob REAL NOT NULL,
            market_prob REAL NOT NULL,
            edge REAL NOT NULL,
            confidence TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS wallet_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            wallet TEXT NOT NULL,
            value REAL NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tracked_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            wallet TEXT NOT NULL,
            nickname TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, wallet)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tracked_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            wallet TEXT NOT NULL,
            created_at TEXT NOT NULL,
            transaction_hash TEXT NOT NULL UNIQUE,
            side TEXT NOT NULL,
            outcome TEXT NOT NULL,
            title TEXT NOT NULL,
            size REAL NOT NULL,
            price REAL NOT NULL,
            trade_timestamp REAL NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            alert_key TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def get_user_setting(user_id, key: str, default=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
        (str(user_id), key),
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default


def set_user_setting(user_id, key: str, value: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO user_settings(user_id, key, value) VALUES (?, ?, ?)",
        (str(user_id), key, str(value)),
    )
    conn.commit()
    conn.close()


def touch_active_user(user_id: int, username: str = "", first_name: str = ""):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO active_users(user_id, username, first_name, last_seen) VALUES (?, ?, ?, ?)",
        (str(user_id), username or "", first_name or "", datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_active_users():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM active_users")
    rows = cur.fetchall()
    conn.close()
    return [int(r[0]) for r in rows if str(r[0]).isdigit()]


def get_tracked_wallets(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT wallet, nickname FROM tracked_wallets WHERE user_id = ? ORDER BY id ASC",
        (str(user_id),),
    )
    rows = cur.fetchall()
    conn.close()
    return [(r[0], r[1] or "") for r in rows]


def add_tracked_wallet(user_id: int, wallet: str, nickname: str = ""):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO tracked_wallets(user_id, wallet, nickname, created_at) VALUES (?, ?, ?, ?)",
        (str(user_id), wallet.lower().strip(), nickname.strip(), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def remove_tracked_wallet(user_id: int, wallet: str):
    conn = get_conn()
    conn.execute(
        "DELETE FROM tracked_wallets WHERE user_id = ? AND wallet = ?",
        (str(user_id), wallet.lower().strip()),
    )
    conn.commit()
    conn.close()


def update_wallet_nickname(user_id: int, wallet: str, nickname: str):
    conn = get_conn()
    conn.execute(
        "UPDATE tracked_wallets SET nickname = ? WHERE user_id = ? AND wallet = ?",
        (nickname.strip(), str(user_id), wallet.lower().strip()),
    )
    conn.commit()
    conn.close()


def get_own_wallet(user_id: int):
    return get_user_setting(user_id, "own_wallet", None)


def set_own_wallet(user_id: int, wallet: str):
    set_user_setting(user_id, "own_wallet", wallet.lower().strip())


def log_wallet_snapshot(user_id: int, wallet: str, value: float):
    conn = get_conn()
    conn.execute(
        "INSERT INTO wallet_snapshots(user_id, wallet, value, created_at) VALUES (?, ?, ?, ?)",
        (str(user_id), wallet.lower().strip(), value, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_latest_wallet_snapshot(user_id: int, wallet: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT value, created_at FROM wallet_snapshots WHERE user_id = ? AND wallet = ? ORDER BY id DESC LIMIT 1",
        (str(user_id), wallet.lower().strip()),
    )
    row = cur.fetchone()
    conn.close()
    return row


def trade_exists(transaction_hash: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM tracked_trades WHERE transaction_hash = ?", (transaction_hash,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def log_tracked_trade(user_id: int, wallet: str, transaction_hash: str, side: str,
                      outcome: str, title: str, size: float, price: float, trade_timestamp: float):
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO tracked_trades
               (user_id, wallet, created_at, transaction_hash, side, outcome, title, size, price, trade_timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(user_id), wallet.lower().strip(), datetime.utcnow().isoformat(),
             transaction_hash, side, outcome, title, size, price, trade_timestamp),
        )
        conn.commit()
    except Exception:
        pass
    conn.close()


def get_recent_tracked_trades(user_id: int, wallet: str, limit: int = 50):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT created_at, transaction_hash, side, outcome, title, size, price, trade_timestamp
           FROM tracked_trades WHERE user_id = ? AND wallet = ?
           ORDER BY trade_timestamp DESC LIMIT ?""",
        (str(user_id), wallet.lower().strip(), limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def was_alert_sent_recently(user_id: int, alert_key: str, within_seconds: int = 300) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT sent_at FROM alert_log WHERE user_id = ? AND alert_key = ? ORDER BY id DESC LIMIT 1",
        (str(user_id), alert_key),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return False
    try:
        sent_at = datetime.fromisoformat(row[0])
        elapsed = (datetime.utcnow() - sent_at).total_seconds()
        return elapsed < within_seconds
    except Exception:
        return False


def mark_alert_sent(user_id: int, alert_key: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO alert_log(user_id, alert_key, sent_at) VALUES (?, ?, ?)",
        (str(user_id), alert_key, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_signal_summary(user_id: int, limit: int = 10):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT created_at, city, market_date, edge, confidence FROM signals WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (str(user_id), limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows
