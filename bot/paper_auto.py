from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Any, List
import time

from bot.db import get_conn, get_user_setting, set_user_setting


DEFAULT_BALANCE = 100.0
TAKER_FEE_RATE = 0.00   # Polymarket fees may be market-specific; keep configurable.
SLIPPAGE_RATE = 0.005   # 0.5% simulated slippage
MAX_POSITION_USD = 5.0
MIN_EDGE = 0.06
MIN_CONFIDENCE = "Medium"


CONF_RANK = {"Low": 0, "Medium": 1, "High": 2}


def ensure_paper_auto_tables():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS paper_auto_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            closed_at TEXT,
            side TEXT NOT NULL,
            entry_btc REAL NOT NULL,
            exit_btc REAL,
            open_price REAL NOT NULL,
            model_prob REAL NOT NULL,
            market_prob REAL NOT NULL,
            edge REAL NOT NULL,
            confidence TEXT NOT NULL,
            stake_usd REAL NOT NULL,
            fee_usd REAL NOT NULL,
            slippage_usd REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            pnl_usd REAL DEFAULT 0,
            note TEXT
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_paper_auto_user_status
        ON paper_auto_trades(user_id, status)
    """)

    conn.commit()
    conn.close()


def paper_enabled(user_id: int) -> bool:
    return get_user_setting(user_id, "paper_auto_enabled", "0") == "1"


def set_paper_enabled(user_id: int, enabled: bool):
    set_user_setting(user_id, "paper_auto_enabled", "1" if enabled else "0")


def get_balance(user_id: int) -> float:
    raw = get_user_setting(user_id, "paper_balance", "")
    if raw == "":
        set_user_setting(user_id, "paper_balance", str(DEFAULT_BALANCE))
        return DEFAULT_BALANCE
    try:
        return float(raw)
    except Exception:
        set_user_setting(user_id, "paper_balance", str(DEFAULT_BALANCE))
        return DEFAULT_BALANCE


def set_balance(user_id: int, balance: float):
    set_user_setting(user_id, "paper_balance", str(round(float(balance), 6)))


def reset_account(user_id: int):
    ensure_paper_auto_tables()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM paper_auto_trades WHERE user_id = ?", (str(user_id),))
    conn.commit()
    conn.close()
    set_balance(user_id, DEFAULT_BALANCE)
    set_paper_enabled(user_id, False)


def open_trade_count(user_id: int) -> int:
    ensure_paper_auto_tables()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM paper_auto_trades
        WHERE user_id = ? AND status = 'open'
    """, (str(user_id),))
    count = cur.fetchone()[0]
    conn.close()
    return int(count or 0)


def should_enter(model: Dict[str, Any]) -> bool:
    if model["edge"] < MIN_EDGE:
        return False
    if CONF_RANK.get(model["confidence"], 0) < CONF_RANK.get(MIN_CONFIDENCE, 1):
        return False
    if model["window"]["phase"] in ("Early", "Danger"):
        return False
    if model["suggested_size_pct"] <= 0:
        return False
    return True


def calc_stake(balance: float, model: Dict[str, Any]) -> float:
    stake = balance * max(0.005, min(0.05, model["suggested_size_pct"] / 100.0))
    return round(max(1.0, min(MAX_POSITION_USD, stake, balance)), 2)


def open_auto_trade(user_id: int, model: Dict[str, Any]) -> Dict[str, Any]:
    ensure_paper_auto_tables()

    balance = get_balance(user_id)
    if balance < 1.0:
        return {"opened": False, "reason": "paper balance too low"}

    if open_trade_count(user_id) >= 1:
        return {"opened": False, "reason": "already has open paper trade"}

    if not should_enter(model):
        return {"opened": False, "reason": "entry filters not met"}

    stake = calc_stake(balance, model)
    fee = round(stake * TAKER_FEE_RATE, 4)
    slippage = round(stake * SLIPPAGE_RATE, 4)
    cost = stake + fee + slippage

    if cost > balance:
        return {"opened": False, "reason": "not enough paper balance"}

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO paper_auto_trades (
            user_id, created_at, side, entry_btc, open_price, model_prob,
            market_prob, edge, confidence, stake_usd, fee_usd, slippage_usd, status, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
    """, (
        str(user_id),
        datetime.utcnow().isoformat(),
        model["signal"],
        float(model["price"]),
        float(model["open"]),
        float(model["model_prob"]),
        float(model["market_prob"]),
        float(model["edge"]),
        model["confidence"],
        stake,
        fee,
        slippage,
        f"phase={model['window']['phase']}",
    ))
    conn.commit()
    conn.close()

    set_balance(user_id, balance - cost)
    return {"opened": True, "stake": stake, "cost": cost, "side": model["signal"]}


def resolve_open_trades(user_id: int, current_btc: float) -> List[Dict[str, Any]]:
    ensure_paper_auto_tables()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, side, entry_btc, open_price, stake_usd, fee_usd, slippage_usd, created_at
        FROM paper_auto_trades
        WHERE user_id = ? AND status = 'open'
        ORDER BY id ASC
    """, (str(user_id),))
    rows = cur.fetchall()

    resolved = []
    balance = get_balance(user_id)

    for trade_id, side, entry_btc, open_price, stake, fee, slippage, created_at in rows:
        # Resolve after the 15m window is effectively over or after 16 minutes max.
        created_dt = datetime.fromisoformat(created_at)
        age_sec = (datetime.utcnow() - created_dt).total_seconds()
        if age_sec < 60 and current_btc != 0:
            # Allow quick paper feedback only after at least 60s.
            continue

        result = "UP" if float(current_btc) >= float(open_price) else "DOWN"
        won = result == side
        payout = float(stake) if won else 0.0
        pnl = payout - float(stake) - float(fee) - float(slippage)
        balance += payout

        cur.execute("""
            UPDATE paper_auto_trades
            SET closed_at = ?, exit_btc = ?, status = 'closed', pnl_usd = ?, note = ?
            WHERE id = ?
        """, (
            datetime.utcnow().isoformat(),
            float(current_btc),
            float(pnl),
            f"result={result}",
            trade_id,
        ))

        resolved.append({
            "id": trade_id,
            "side": side,
            "result": result,
            "won": won,
            "stake": float(stake),
            "pnl": float(pnl),
            "entry_btc": float(entry_btc),
            "exit_btc": float(current_btc),
        })

    conn.commit()
    conn.close()
    set_balance(user_id, balance)
    return resolved


def paper_auto_summary(user_id: int) -> Dict[str, Any]:
    ensure_paper_auto_tables()
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM paper_auto_trades WHERE user_id = ?", (str(user_id),))
    total = int(cur.fetchone()[0] or 0)

    cur.execute("SELECT COUNT(*) FROM paper_auto_trades WHERE user_id = ? AND status = 'open'", (str(user_id),))
    open_count = int(cur.fetchone()[0] or 0)

    cur.execute("""
        SELECT COUNT(*), COALESCE(SUM(pnl_usd),0)
        FROM paper_auto_trades
        WHERE user_id = ? AND status = 'closed'
    """, (str(user_id),))
    closed_count, pnl = cur.fetchone()

    cur.execute("""
        SELECT side, stake_usd, edge, confidence, status, pnl_usd, created_at
        FROM paper_auto_trades
        WHERE user_id = ?
        ORDER BY id DESC LIMIT 5
    """, (str(user_id),))
    recent = cur.fetchall()

    cur.execute("""
        SELECT COUNT(*)
        FROM paper_auto_trades
        WHERE user_id = ? AND status = 'closed' AND pnl_usd > 0
    """, (str(user_id),))
    wins = int(cur.fetchone()[0] or 0)

    conn.close()

    closed_count = int(closed_count or 0)
    win_rate = (wins / closed_count) if closed_count else 0.0

    return {
        "enabled": paper_enabled(user_id),
        "balance": get_balance(user_id),
        "total": total,
        "open": open_count,
        "closed": closed_count,
        "pnl": float(pnl or 0.0),
        "wins": wins,
        "win_rate": win_rate,
        "recent": recent,
    }
