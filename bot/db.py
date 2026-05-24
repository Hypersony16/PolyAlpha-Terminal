import json
import sqlite3
from datetime import datetime

from bot.config import DB_PATH


def get_conn():
    return sqlite3.connect(DB_PATH)


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
            created_at TEXT NOT NULL,
            wallet TEXT NOT NULL,
            total_value REAL NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tracked_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            wallet TEXT NOT NULL,
            created_at TEXT NOT NULL,
            transaction_hash TEXT NOT NULL,
            side TEXT NOT NULL,
            outcome TEXT,
            title TEXT,
            size REAL,
            price REAL,
            trade_timestamp REAL,
            UNIQUE(user_id, wallet, transaction_hash)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            city TEXT NOT NULL,
            market_date TEXT NOT NULL,
            best_temp INTEGER NOT NULL,
            edge REAL NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS copytrades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            source_wallet TEXT NOT NULL,
            created_at TEXT NOT NULL,
            transaction_hash TEXT NOT NULL,
            side TEXT NOT NULL,
            outcome TEXT,
            title TEXT,
            size_usdc REAL,
            price REAL,
            status TEXT DEFAULT 'queued',
            note TEXT,
            UNIQUE(user_id, transaction_hash)
        )
    """)

    conn.commit()
    conn.close()


# ----- user/activity -----
def touch_active_user(user_id: int, username: str | None, first_name: str | None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO active_users (user_id, username, first_name, last_seen)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_seen=excluded.last_seen
    """, (str(user_id), username or "", first_name or "", datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def get_active_users(limit: int = 500):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, username, first_name, last_seen
        FROM active_users
        ORDER BY last_seen DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


# ----- settings -----
def get_user_setting(user_id: int, key: str, default: str | None = None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM user_settings WHERE user_id = ? AND key = ?", (str(user_id), key))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default


def set_user_setting(user_id: int, key: str, value: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_settings (user_id, key, value)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value
    """, (str(user_id), key, str(value)))
    conn.commit()
    conn.close()


def get_user_json(user_id: int, key: str, default):
    raw = get_user_setting(user_id, key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def set_user_json(user_id: int, key: str, value):
    set_user_setting(user_id, key, json.dumps(value))


# Compatibility aliases for Claude naming
def get_setting(user_id: int, key: str, default: str | None = None):
    return get_user_setting(user_id, key, default)


def set_setting(user_id: int, key: str, value: str):
    set_user_setting(user_id, key, value)


def get_global(key: str, default: str = ""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM global_settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default


def set_global(key: str, value: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO global_settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, str(value)))
    conn.commit()
    conn.close()


def touch_user(user_id: int, username: str | None, first_name: str | None):
    touch_active_user(user_id, username, first_name)


# ----- per-user prefs -----
def get_unit(user_id: int) -> str:
    unit = get_user_setting(user_id, "unit", "C")
    return "F" if unit == "F" else "C"


def set_unit(user_id: int, unit: str):
    set_user_setting(user_id, "unit", "F" if unit == "F" else "C")


def get_city(user_id: int) -> str:
    return get_user_setting(user_id, "selected_city", get_user_setting(user_id, "city", "munich"))


def set_city(user_id: int, city: str):
    set_user_setting(user_id, "selected_city", city)
    set_user_setting(user_id, "city", city)


# ----- wallets -----
def get_tracked_wallets(user_id: int):
    wallets = get_user_json(user_id, "tracked_wallets", [])
    return wallets if isinstance(wallets, list) else []


def set_tracked_wallets(user_id: int, wallets: list[dict]):
    set_user_json(user_id, "tracked_wallets", wallets)


def add_tracked_wallet(user_id: int, address: str, nickname: str = ""):
    wallets = get_tracked_wallets(user_id)
    address_l = address.lower()
    for item in wallets:
        if item.get("address", "").lower() == address_l:
            if nickname:
                item["nickname"] = nickname
            set_tracked_wallets(user_id, wallets)
            return
    wallets.append({"address": address_l, "nickname": nickname})
    set_tracked_wallets(user_id, wallets)


def remove_tracked_wallet(user_id: int, address: str):
    address_l = address.lower()
    wallets = [w for w in get_tracked_wallets(user_id) if w.get("address", "").lower() != address_l]
    set_tracked_wallets(user_id, wallets)


def update_wallet_nickname(user_id: int, address: str, nickname: str):
    wallets = get_tracked_wallets(user_id)
    address_l = address.lower()
    for item in wallets:
        if item.get("address", "").lower() == address_l:
            item["nickname"] = nickname
            break
    set_tracked_wallets(user_id, wallets)


# Claude alias
def rename_tracked_wallet(user_id: int, address: str, nickname: str):
    update_wallet_nickname(user_id, address, nickname)


def get_own_wallet(user_id: int):
    return get_user_setting(user_id, "own_wallet", "")


def set_own_wallet(user_id: int, address: str):
    set_user_setting(user_id, "own_wallet", address.lower())


# ----- signals -----
def log_signal(user_id: int, city: str, weather_data_or_date, best_or_high=None, *args):
    """Supports both old style log_signal(user_id, city, weather_data, best)
    and Claude style log_signal(user_id, city, market_date, consensus_high, source, temp, model, market, edge, confidence)."""
    if isinstance(weather_data_or_date, dict):
        weather_data = weather_data_or_date
        best = best_or_high
        market_date = weather_data["date"]
        consensus_high = weather_data["consensus_high"]
        model_source = weather_data["source"]
        best_temp = best["temp"]
        model_prob = best["model_prob"]
        market_prob = best["market_prob"]
        edge = best["edge"]
        confidence = best["confidence"]
    else:
        market_date = weather_data_or_date
        consensus_high = best_or_high
        model_source, best_temp, model_prob, market_prob, edge, confidence = args

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO signals (
            user_id, created_at, city, market_date, consensus_high, model_source,
            best_temp, model_prob, market_prob, edge, confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(user_id), datetime.utcnow().isoformat(), city, market_date, float(consensus_high), model_source, int(best_temp), float(model_prob), float(market_prob), float(edge), confidence))
    conn.commit()
    conn.close()


def get_signal_summary(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM signals WHERE user_id = ?", (str(user_id),))
    count = cur.fetchone()[0]
    cur.execute("SELECT AVG(edge) FROM signals WHERE user_id = ?", (str(user_id),))
    avg_edge = cur.fetchone()[0]
    cur.execute("SELECT AVG(model_prob) FROM signals WHERE user_id = ?", (str(user_id),))
    avg_model_prob = cur.fetchone()[0]
    cur.execute("""
        SELECT city, market_date, best_temp, model_prob, market_prob, edge, confidence
        FROM signals WHERE user_id = ? ORDER BY id DESC LIMIT 1
    """, (str(user_id),))
    latest = cur.fetchone()
    cur.execute("""
        SELECT city, best_temp, COUNT(*) AS c
        FROM signals WHERE user_id = ? GROUP BY city, best_temp ORDER BY c DESC LIMIT 5
    """, (str(user_id),))
    top_temps = cur.fetchall()
    conn.close()
    return {
        "count": count,
        "avg_edge": float(avg_edge) if avg_edge is not None else 0.0,
        "avg_model_prob": float(avg_model_prob) if avg_model_prob is not None else 0.0,
        "latest": latest,
        "top_temps": top_temps,
    }


# ----- alert dedupe -----
def was_alert_sent_recently(user_id: int, city: str, market_date: str, best_temp: int, edge: float) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM alerts_sent
        WHERE user_id = ? AND city = ? AND market_date = ? AND best_temp = ? AND edge <= (? + 0.20)
        LIMIT 1
    """, (str(user_id), city, market_date, best_temp, edge))
    row = cur.fetchone()
    conn.close()
    return row is not None


def mark_alert_sent(user_id: int, city: str, market_date: str, best_temp: int, edge: float):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO alerts_sent (user_id, created_at, city, market_date, best_temp, edge)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (str(user_id), datetime.utcnow().isoformat(), city, market_date, best_temp, edge))
    conn.commit()
    conn.close()


# Claude aliases
def alert_sent_recently(user_id: int, city: str, market_date: str, best_temp: int, edge: float) -> bool:
    return was_alert_sent_recently(user_id, city, market_date, best_temp, edge)


# ----- wallet snapshots -----
def log_wallet_snapshot(user_id: int, wallet: str, total_value: float):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO wallet_snapshots (user_id, created_at, wallet, total_value)
        VALUES (?, ?, ?, ?)
    """, (str(user_id), datetime.utcnow().isoformat(), wallet.lower(), float(total_value)))
    conn.commit()
    conn.close()


def get_latest_wallet_snapshot(user_id: int, wallet: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT created_at, total_value FROM wallet_snapshots
        WHERE user_id = ? AND wallet = ? ORDER BY id DESC LIMIT 1
    """, (str(user_id), wallet.lower()))
    row = cur.fetchone()
    conn.close()
    return row


# Claude aliases
def log_snapshot(user_id: int, wallet: str, value: float):
    log_wallet_snapshot(user_id, wallet, value)


def get_latest_snapshot(user_id: int, wallet: str):
    return get_latest_wallet_snapshot(user_id, wallet)


def get_snapshot_history(user_id: int, wallet: str, limit: int = 48):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT created_at, total_value FROM wallet_snapshots
        WHERE user_id = ? AND wallet = ? ORDER BY id DESC LIMIT ?
    """, (str(user_id), wallet.lower(), limit))
    rows = cur.fetchall()
    conn.close()
    return rows


# ----- tracked trades -----
def trade_exists(user_id: int, wallet: str, transaction_hash: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM tracked_trades WHERE user_id = ? AND wallet = ? AND transaction_hash = ? LIMIT 1
    """, (str(user_id), wallet.lower(), transaction_hash))
    row = cur.fetchone()
    conn.close()
    return row is not None


def log_tracked_trade(user_id: int, wallet: str, trade: dict):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO tracked_trades (
            user_id, wallet, created_at, transaction_hash, side, outcome, title, size, price, trade_timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(user_id), wallet.lower(), datetime.utcnow().isoformat(), str(trade.get("transactionHash", "")),
        str(trade.get("side", "")), str(trade.get("outcome", "")), str(trade.get("title", "")),
        float(trade.get("size", 0) or 0), float(trade.get("price", 0) or 0), float(trade.get("timestamp", 0) or 0),
    ))
    conn.commit()
    conn.close()


def get_recent_tracked_trades(user_id: int, wallet: str, limit: int = 10):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT created_at, transaction_hash, side, outcome, title, size, price, trade_timestamp
        FROM tracked_trades WHERE user_id = ? AND wallet = ? ORDER BY trade_timestamp DESC, id DESC LIMIT ?
    """, (str(user_id), wallet.lower(), limit))
    rows = cur.fetchall()
    conn.close()
    return rows


# Claude aliases
def log_trade(user_id: int, wallet: str, trade: dict):
    log_tracked_trade(user_id, wallet, trade)


def get_recent_trades(user_id: int, wallet: str, limit: int = 20):
    return get_recent_tracked_trades(user_id, wallet, limit)


def get_all_trades_for_wallet(user_id: int, wallet: str):
    return get_recent_tracked_trades(user_id, wallet, 1000)


def get_trade_summary(user_id: int, wallet: str):
    rows = get_recent_tracked_trades(user_id, wallet, 1000)
    total_size = sum(float(r[5] or 0) for r in rows)
    by_side = {}
    for r in rows:
        side = str(r[2])
        by_side.setdefault(side, [0, 0.0])
        by_side[side][0] += 1
        by_side[side][1] += float(r[5] or 0)
    return {"total_count": len(rows), "total_size": total_size, "by_side": [(k, v[0], v[1]) for k, v in by_side.items()]}


# ----- copy trades log -----
def log_copytrade(user_id: int, source_wallet: str, trade: dict, size_usdc: float, status: str = "queued", note: str = ""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO copytrades (
            user_id, source_wallet, created_at, transaction_hash, side, outcome, title, size_usdc, price, status, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(user_id), source_wallet.lower(), datetime.utcnow().isoformat(), str(trade.get("transactionHash", "")),
        str(trade.get("side", "")), str(trade.get("outcome", "")), str(trade.get("title", "")),
        float(size_usdc), float(trade.get("price", 0) or 0), status, note,
    ))
    conn.commit()
    conn.close()
