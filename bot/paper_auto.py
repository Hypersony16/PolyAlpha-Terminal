from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List

from bot.db import get_conn, get_user_setting, set_user_setting


DEFAULT_BALANCE = 100.0

# Paper execution assumptions.
SLIPPAGE_RATE = 0.005
DEFAULT_MAX_POSITION_USD = 1.0
MIN_POSITION_USD = 1.0

# Stage 2: real-odds mode and reduced frequency.
MIN_EDGE = 0.12
MIN_CONFIDENCE = "Medium"
MIN_EV_PER_DOLLAR = 0.07
MAX_ENTRY_PRICE = 0.60
MIN_ENTRY_PRICE = 0.35
MIN_MODEL_PROB = 0.62
MIN_LIQUIDITY = 250.0
MIN_TIME_LEFT_SECONDS = 300

MAX_OPEN_TRADES = 1
ONE_TRADE_PER_WINDOW = True
RESOLVE_GRACE_SECONDS = 3  # near-instant official poll after expiry

CONF_RANK = {"Low": 0, "Medium": 1, "High": 2}


def _parse_iso_dt(value: str):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _window_end_from_start(window_start: str):
    dt = _parse_iso_dt(window_start)
    if not dt:
        return None
    return dt + timedelta(seconds=900)


def _market_slug_from_model(model: Dict[str, Any]) -> str:
    market = model.get("market") or {}
    return str(model.get("market_slug") or market.get("slug") or "")


def _market_question_from_model(model: Dict[str, Any]) -> str:
    market = model.get("market") or {}
    return str(model.get("market_question") or market.get("question") or "BTC Up or Down 15m")


def _target_price_from_model(model: Dict[str, Any]) -> float:
    return float(model.get("target_price", model.get("open", model["price"])))


def ensure_paper_auto_tables():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS paper_auto_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            market_slug TEXT NOT NULL,
            market_question TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            target_price REAL NOT NULL,
            stake_usd REAL NOT NULL,
            shares REAL NOT NULL,
            model_prob REAL NOT NULL,
            edge REAL NOT NULL,
            confidence TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            exit_price REAL,
            pnl_usd REAL,
            closed_at TEXT,
            window_start TEXT,
            window_end_ts REAL,
            trade_mode TEXT DEFAULT 'resolution',
            skip_reason TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS paper_calibration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            market_slug TEXT NOT NULL,
            predicted_prob REAL NOT NULL,
            actual_result INTEGER NOT NULL,
            edge REAL NOT NULL,
            confidence TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def get_balance(user_id: int) -> float:
    raw = get_user_setting(user_id, "paper_balance", str(DEFAULT_BALANCE))
    try:
        return float(raw)
    except Exception:
        return DEFAULT_BALANCE


def set_balance(user_id: int, value: float):
    set_user_setting(user_id, "paper_balance", str(round(value, 4)))


def get_max_bet(user_id: int) -> float:
    raw = get_user_setting(user_id, "paper_max_bet", str(DEFAULT_MAX_POSITION_USD))
    try:
        return max(MIN_POSITION_USD, float(raw))
    except Exception:
        return DEFAULT_MAX_POSITION_USD


def set_max_bet(user_id: int, value: float):
    set_user_setting(user_id, "paper_max_bet", str(round(max(MIN_POSITION_USD, value), 2)))


def paper_enabled(user_id: int) -> bool:
    return get_user_setting(user_id, "paper_enabled", "0") == "1"


def set_paper_enabled(user_id: int, value: bool):
    set_user_setting(user_id, "paper_enabled", "1" if value else "0")


def get_last_skip_reason(user_id: int) -> str:
    return get_user_setting(user_id, "paper_last_skip", "") or ""


def set_last_skip_reason(user_id: int, reason: str):
    set_user_setting(user_id, "paper_last_skip", reason)


def set_real_odds_only(user_id: int, value: bool):
    set_user_setting(user_id, "paper_real_odds_only", "1" if value else "0")


def get_real_odds_only(user_id: int) -> bool:
    return get_user_setting(user_id, "paper_real_odds_only", "1") == "1"


def open_trade_count(user_id: int) -> int:
    ensure_paper_auto_tables()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM paper_auto_trades WHERE user_id = ? AND status = 'open'",
        (str(user_id),),
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0


def already_traded_market(user_id: int, market_slug: str) -> bool:
    ensure_paper_auto_tables()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM paper_auto_trades WHERE user_id = ? AND market_slug = ? AND status = 'open'",
        (str(user_id), market_slug),
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


def already_traded_window(user_id: int, window_start: str) -> bool:
    if not window_start:
        return False
    ensure_paper_auto_tables()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM paper_auto_trades WHERE user_id = ? AND window_start = ?",
        (str(user_id), window_start),
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


def get_entry_price(model: Dict[str, Any], side: str) -> float:
    if side == "UP":
        price = float(model.get("up_price") or model.get("market_prob") or 0.5)
    else:
        price = float(model.get("down_price") or (1 - float(model.get("market_prob") or 0.5)))
    return max(0.01, min(0.99, price))


def calc_stake(user_id: int, model: Dict[str, Any]) -> float:
    balance = get_balance(user_id)
    max_bet = get_max_bet(user_id)
    stake = min(max_bet, balance * 0.05)
    return max(MIN_POSITION_USD, round(stake, 2)) if balance >= MIN_POSITION_USD else 0.0


def estimate_ev_usd(stake: float, entry_price: float, edge: float) -> float:
    if entry_price <= 0:
        return 0.0
    payout = stake / entry_price
    cost = stake
    ev = payout * (entry_price + edge) - cost
    return round(ev, 4)


def should_enter(model: Dict[str, Any], user_id: int) -> tuple[bool, str, str]:
    """Returns (should_enter, side, reason)."""
    signal = str(model.get("signal", "")).upper()
    edge = float(model.get("edge", 0) or 0)
    confidence = str(model.get("confidence", "Low"))
    model_prob = float(model.get("model_prob", 0) or 0)
    market_prob = float(model.get("market_prob", 0) or 0)
    time_left = int(model.get("time_left_seconds", 0) or 0)
    liquidity = float(model.get("liquidity", 0) or 0)

    if signal not in ("UP", "DOWN"):
        return False, "", "No clear signal"
    if edge < MIN_EDGE:
        return False, "", f"Edge too low: {edge:.3f} < {MIN_EDGE}"
    if CONF_RANK.get(confidence, 0) < CONF_RANK.get(MIN_CONFIDENCE, 1):
        return False, "", f"Confidence too low: {confidence}"
    if model_prob < MIN_MODEL_PROB:
        return False, "", f"Model prob too low: {model_prob:.3f}"
    if time_left < MIN_TIME_LEFT_SECONDS:
        return False, "", f"Too little time left: {time_left}s"

    entry_price = get_entry_price(model, signal)
    if entry_price > MAX_ENTRY_PRICE:
        return False, "", f"Entry price too high: {entry_price:.3f}"
    if entry_price < MIN_ENTRY_PRICE:
        return False, "", f"Entry price too low: {entry_price:.3f}"

    if get_real_odds_only(user_id) and liquidity < MIN_LIQUIDITY:
        return False, "", f"Liquidity too low: {liquidity:.0f}"

    return True, signal, "OK"


def open_paper_trade(user_id: int, model: Dict[str, Any], side: str, trade_mode: str = "resolution") -> int | None:
    ensure_paper_auto_tables()
    stake = calc_stake(user_id, model)
    if stake <= 0:
        return None

    entry_price = get_entry_price(model, side)
    shares = round(stake / entry_price, 4) if entry_price > 0 else 0
    edge = float(model.get("edge", 0) or 0)
    confidence = str(model.get("confidence", "Low"))
    model_prob = float(model.get("model_prob", 0) or 0)
    market_slug = _market_slug_from_model(model)
    market_question = _market_question_from_model(model)
    target_price = _target_price_from_model(model)
    window_start = str(model.get("window_start", ""))
    window_end_ts = float(model.get("window_end_ts", 0) or 0)

    balance = get_balance(user_id)
    set_balance(user_id, balance - stake)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO paper_auto_trades
           (user_id, created_at, market_slug, market_question, side, entry_price, target_price,
            stake_usd, shares, model_prob, edge, confidence, status, window_start, window_end_ts, trade_mode)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
        (str(user_id), datetime.utcnow().isoformat(), market_slug, market_question, side,
         entry_price, target_price, stake, shares, model_prob, edge, confidence,
         window_start, window_end_ts, trade_mode),
    )
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def resolve_open_trades(user_id: int, model: Dict[str, Any]) -> List[Dict[str, Any]]:
    ensure_paper_auto_tables()
    now_ts = datetime.utcnow().timestamp()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, side, entry_price, stake_usd, shares, market_slug, window_end_ts
           FROM paper_auto_trades
           WHERE user_id = ? AND status = 'open' AND window_end_ts > 0 AND window_end_ts <= ?""",
        (str(user_id), now_ts + RESOLVE_GRACE_SECONDS),
    )
    rows = cur.fetchall()
    conn.close()

    resolved = []
    for row in rows:
        trade_id, side, entry_price, stake, shares, market_slug, window_end_ts = row
        result = _resolve_trade(user_id, trade_id, side, entry_price, stake, shares, model)
        if result:
            resolved.append(result)
    return resolved


def _resolve_trade(user_id: int, trade_id: int, side: str, entry_price: float,
                   stake: float, shares: float, model: Dict[str, Any]) -> Dict[str, Any] | None:
    signal = str(model.get("signal", "")).upper()
    market_prob = float(model.get("market_prob", 0.5) or 0.5)

    if side == "UP":
        exit_price = market_prob
        won = signal == "UP" or market_prob > 0.5
    else:
        exit_price = 1 - market_prob
        won = signal == "DOWN" or market_prob < 0.5

    exit_price = max(0.01, min(0.99, exit_price))
    payout = shares * exit_price if won else 0.0
    pnl = round(payout - stake, 4)

    balance = get_balance(user_id)
    set_balance(user_id, balance + payout)

    conn = get_conn()
    conn.execute(
        """UPDATE paper_auto_trades
           SET status='closed', exit_price=?, pnl_usd=?, closed_at=?
           WHERE id=?""",
        (exit_price, pnl, datetime.utcnow().isoformat(), trade_id),
    )
    conn.commit()
    conn.close()

    return {
        "trade_id": trade_id,
        "side": side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "stake": stake,
        "pnl": pnl,
        "won": won,
    }


def due_open_market_slugs(user_id: int) -> List[str]:
    ensure_paper_auto_tables()
    now_ts = datetime.utcnow().timestamp()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT DISTINCT market_slug FROM paper_auto_trades
           WHERE user_id = ? AND status = 'open' AND window_end_ts > 0 AND window_end_ts <= ?""",
        (str(user_id), now_ts + RESOLVE_GRACE_SECONDS),
    )
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows if r[0]]


def reset_account(user_id: int):
    ensure_paper_auto_tables()
    set_balance(user_id, DEFAULT_BALANCE)
    conn = get_conn()
    conn.execute("DELETE FROM paper_auto_trades WHERE user_id = ?", (str(user_id),))
    conn.execute("DELETE FROM paper_calibration WHERE user_id = ?", (str(user_id),))
    conn.commit()
    conn.close()


def paper_auto_summary(user_id: int) -> Dict[str, Any]:
    ensure_paper_auto_tables()
    balance = get_balance(user_id)
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """SELECT COUNT(*), SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END),
                  COALESCE(SUM(pnl_usd), 0),
                  COALESCE(SUM(stake_usd), 0)
           FROM paper_auto_trades WHERE user_id = ?""",
        (str(user_id),),
    )
    row = cur.fetchone()
    conn.close()

    total = row[0] or 0
    open_count = row[1] or 0
    closed = row[2] or 0
    wins = row[3] or 0
    total_pnl = row[4] or 0.0
    total_staked = row[5] or 0.0

    return {
        "balance": round(balance, 2),
        "total_trades": total,
        "open_trades": open_count,
        "closed_trades": closed,
        "wins": wins,
        "losses": closed - wins,
        "winrate": round(wins / closed * 100, 1) if closed else 0.0,
        "total_pnl": round(total_pnl, 2),
        "total_staked": round(total_staked, 2),
        "roi": round(total_pnl / max(1, total_staked) * 100, 1),
    }
