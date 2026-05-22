from __future__ import annotations

from datetime import datetime
from typing import Dict, Any, List

from bot.db import get_conn, get_user_setting, set_user_setting


DEFAULT_BALANCE = 100.0

# Paper execution assumptions.
# For real CLOB execution later these must be replaced by real orderbook fill prices.
SLIPPAGE_RATE = 0.005          # 0.5% simulated entry slippage
DEFAULT_MAX_POSITION_USD = 2.0
MIN_POSITION_USD = 1.0
MIN_EDGE = 0.02
MIN_CONFIDENCE = "Low"
MIN_EV_PER_DOLLAR = 0.005

# Avoid spam/overtrading.
MAX_OPEN_TRADES = 1
ONE_TRADE_PER_WINDOW = True

# FIX: Resolve at 900s (full window end), not 840s (1 min early).
# Paper trades must wait for the actual 15m Polymarket settlement.
RESOLVE_AFTER_SECONDS = 900

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
            window_start TEXT,
            side TEXT NOT NULL,
            entry_btc REAL NOT NULL,
            exit_btc REAL,
            open_price REAL NOT NULL,
            entry_price REAL DEFAULT 0.5,
            shares REAL DEFAULT 0,
            model_prob REAL NOT NULL,
            market_prob REAL NOT NULL,
            edge REAL NOT NULL,
            confidence TEXT NOT NULL,
            stake_usd REAL NOT NULL,
            fee_usd REAL NOT NULL,
            slippage_usd REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            result TEXT,
            payout_usd REAL DEFAULT 0,
            pnl_usd REAL DEFAULT 0,
            ev_usd REAL DEFAULT 0,
            note TEXT
        )
    """)

    # Safe migrations for older SQLite table.
    for column, ddl in {
        "window_start": "ALTER TABLE paper_auto_trades ADD COLUMN window_start TEXT",
        "entry_price": "ALTER TABLE paper_auto_trades ADD COLUMN entry_price REAL DEFAULT 0.5",
        "shares": "ALTER TABLE paper_auto_trades ADD COLUMN shares REAL DEFAULT 0",
        "result": "ALTER TABLE paper_auto_trades ADD COLUMN result TEXT",
        "payout_usd": "ALTER TABLE paper_auto_trades ADD COLUMN payout_usd REAL DEFAULT 0",
        "ev_usd": "ALTER TABLE paper_auto_trades ADD COLUMN ev_usd REAL DEFAULT 0",
    }.items():
        try:
            cur.execute(ddl)
        except Exception:
            pass

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_paper_auto_user_status
        ON paper_auto_trades(user_id, status)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_paper_auto_user_window
        ON paper_auto_trades(user_id, window_start)
    """)

    conn.commit()
    conn.close()


def paper_enabled(user_id: int) -> bool:
    return get_user_setting(user_id, "paper_auto_enabled", "0") == "1"


def set_paper_enabled(user_id: int, enabled: bool):
    set_user_setting(user_id, "paper_auto_enabled", "1" if enabled else "0")


def set_last_skip_reason(user_id: int, reason: str, model: Dict[str, Any] | None = None):
    details = reason
    if model:
        try:
            details = (
                f"{reason} | odds={model.get('odds_source','?')} "
                f"phase={model.get('window',{}).get('phase','?')} "
                f"edge={float(model.get('edge',0))*100:.1f}% "
                f"EV={float(model.get('ev_per_dollar',0))*100:.1f}% "
                f"conf={model.get('confidence','?')} "
                f"mkt={float(model.get('market_prob',0))*100:.1f}% "
                f"model={float(model.get('model_prob',0))*100:.1f}%"
            )
        except Exception:
            details = reason
    set_user_setting(user_id, "paper_last_skip_reason", details)


def get_last_skip_reason(user_id: int) -> str:
    return get_user_setting(user_id, "paper_last_skip_reason", "No check yet")




def get_max_bet(user_id: int) -> float:
    raw = get_user_setting(user_id, "paper_max_bet", str(DEFAULT_MAX_POSITION_USD))
    try:
        val = float(raw)
    except Exception:
        val = DEFAULT_MAX_POSITION_USD
    return max(1.0, min(5.0, val))


def set_max_bet(user_id: int, amount: float):
    amount = max(1.0, min(5.0, float(amount)))
    set_user_setting(user_id, "paper_max_bet", str(amount))


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


def already_traded_window(user_id: int, window_start: str) -> bool:
    if not ONE_TRADE_PER_WINDOW:
        return False

    ensure_paper_auto_tables()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM paper_auto_trades
        WHERE user_id = ? AND window_start = ?
        LIMIT 1
    """, (str(user_id), window_start))
    row = cur.fetchone()
    conn.close()
    return row is not None


def get_entry_price(model: Dict[str, Any]) -> float:
    """
    Real Polymarket binary entry price.
    Uses model['entry_price'] from real market odds.
    """
    price = model.get("entry_price")
    if price is None:
        side = str(model.get("signal", "")).upper()
        if side == "UP":
            price = model.get("market_up", model.get("market_prob", 0.5))
        else:
            price = model.get("market_down", model.get("market_prob", 0.5))

    try:
        price = float(price)
    except Exception:
        price = 0.5

    return max(0.01, min(0.99, price))


def estimate_ev_usd(stake: float, entry_price: float, model_prob: float, slippage_usd: float, fee_usd: float = 0.0) -> float:
    """
    Binary market EV:
    shares = stake / entry_price
    expected payout = model_prob * shares * $1
    EV = expected payout - stake - slippage - fees
    """
    shares = stake / entry_price
    expected_payout = model_prob * shares
    return expected_payout - stake - slippage_usd - fee_usd


def should_enter(model: Dict[str, Any]) -> tuple[bool, str]:
    """
    Paper-test filters.
    Still prefers real Polymarket odds, but no longer silently blocks for hours.
    If odds are fallback_50, it can paper-test only with stronger model probability.
    """
    edge = float(model.get("edge", 0))
    ev = float(model.get("ev_per_dollar", 0))
    model_prob = float(model.get("model_prob", 0))
    odds_source = model.get("odds_source", "fallback_50")
    phase = model.get("window", {}).get("phase", "?")

    if odds_source == "fallback_50":
        # Allow rare test trades even if Polymarket odds failed, but require stronger signal.
        if model_prob < 0.62 or edge < 0.10:
            return False, "missing real odds and fallback signal not strong enough"

    if edge < MIN_EDGE:
        return False, "edge too low"

    if ev < MIN_EV_PER_DOLLAR:
        return False, "EV too low"

    if CONF_RANK.get(model.get("confidence"), 0) < CONF_RANK.get(MIN_CONFIDENCE, 0):
        return False, "confidence too low"

    # Allow Prime and Late. Avoid final seconds only.
    if phase in ("Early", "Danger"):
        return False, f"bad phase: {phase}"

    entry_price = get_entry_price(model)
    stake_test = MIN_POSITION_USD
    slippage_test = stake_test * SLIPPAGE_RATE
    ev_test = estimate_ev_usd(stake_test, entry_price, model_prob, slippage_test)

    if ev_test <= -0.03:
        return False, "negative binary EV after slippage"

    return True, "ok"


def calc_stake(user_id: int, balance: float, model: Dict[str, Any]) -> float:
    max_bet = get_max_bet(user_id)

    # EV + Kelly-lite. Still capped by user setting.
    suggested = max(0.005, min(0.05, float(model.get("suggested_size_pct", 1.0)) / 100.0))
    stake = balance * suggested

    return round(max(MIN_POSITION_USD, min(max_bet, stake, balance)), 2)


def open_auto_trade(user_id: int, model: Dict[str, Any]) -> Dict[str, Any]:
    ensure_paper_auto_tables()

    balance = get_balance(user_id)
    if balance < MIN_POSITION_USD:
        set_last_skip_reason(user_id, "paper balance too low", model)
        return {"opened": False, "reason": "paper balance too low"}

    if open_trade_count(user_id) >= MAX_OPEN_TRADES:
        set_last_skip_reason(user_id, "already has open paper trade", model)
        return {"opened": False, "reason": "already has open paper trade"}

    window_start = model["window"]["start"].isoformat()
    if already_traded_window(user_id, window_start):
        set_last_skip_reason(user_id, "already traded this 15m window", model)
        return {"opened": False, "reason": "already traded this 15m window"}

    ok, reason = should_enter(model)
    if not ok:
        set_last_skip_reason(user_id, reason, model)
        return {"opened": False, "reason": reason}

    side = str(model["signal"]).upper()
    entry_price = get_entry_price(model)
    stake = calc_stake(user_id, balance, model)

    fee = 0.0
    slippage = round(stake * SLIPPAGE_RATE, 4)
    cost = stake + fee + slippage

    if cost > balance:
        stake = round(max(MIN_POSITION_USD, balance / (1 + SLIPPAGE_RATE)), 2)
        slippage = round(stake * SLIPPAGE_RATE, 4)
        cost = stake + slippage

    if cost > balance:
        set_last_skip_reason(user_id, "not enough paper balance", model)
        return {"opened": False, "reason": "not enough paper balance"}

    shares = stake / entry_price
    ev = estimate_ev_usd(stake, entry_price, float(model["model_prob"]), slippage, fee)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO paper_auto_trades (
            user_id, created_at, window_start, side, entry_btc, open_price,
            entry_price, shares, model_prob, market_prob, edge, confidence,
            stake_usd, fee_usd, slippage_usd, status, ev_usd, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
    """, (
        str(user_id),
        datetime.utcnow().isoformat(),
        window_start,
        side,
        float(model["price"]),
        float(model["open"]),
        float(entry_price),
        float(shares),
        float(model["model_prob"]),
        float(model["market_prob"]),
        float(model["edge"]),
        model["confidence"],
        float(stake),
        float(fee),
        float(slippage),
        float(ev),
        f"phase={model['window']['phase']}; odds={model.get('odds_source','?')}; target={model.get('target_price', model.get('open'))}; binary_payout=1_if_correct",
    ))
    conn.commit()
    conn.close()

    set_balance(user_id, balance - cost)
    set_last_skip_reason(user_id, "opened trade", model)
    return {
        "opened": True,
        "stake": stake,
        "cost": cost,
        "side": side,
        "entry_price": entry_price,
        "shares": shares,
        "ev": ev,
    }


def resolve_open_trades(user_id: int, current_btc: float) -> List[Dict[str, Any]]:
    """
    Resolve paper trades like Polymarket:
    - If chosen side wins, each share pays $1.
    - If chosen side loses, payout is $0.

    FIX: Resolves at RESOLVE_AFTER_SECONDS (900s = full window end),
    not 840s. This matches the real 15m Polymarket settlement window
    and avoids premature resolution before the market settles.
    """
    ensure_paper_auto_tables()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, side, entry_btc, open_price, entry_price, shares,
               stake_usd, fee_usd, slippage_usd, created_at, window_start
        FROM paper_auto_trades
        WHERE user_id = ? AND status = 'open'
        ORDER BY id ASC
    """, (str(user_id),))
    rows = cur.fetchall()

    resolved = []
    balance = get_balance(user_id)

    for trade_id, side, entry_btc, open_price, entry_price, shares, stake, fee, slippage, created_at, window_start in rows:
        # FIX: Wait for full 15m window to close (900s), not 840s (14 min).
        try:
            window_dt = datetime.fromisoformat(window_start)
            seconds_since_window = (datetime.utcnow() - window_dt.replace(tzinfo=None)).total_seconds()
        except Exception:
            created_dt = datetime.fromisoformat(created_at)
            seconds_since_window = (datetime.utcnow() - created_dt).total_seconds()

        if seconds_since_window < RESOLVE_AFTER_SECONDS:
            continue

        result = "UP" if float(current_btc) >= float(open_price) else "DOWN"
        won = result == str(side).upper()

        payout = float(shares) if won else 0.0
        pnl = payout - float(stake) - float(fee) - float(slippage)
        balance += payout

        cur.execute("""
            UPDATE paper_auto_trades
            SET closed_at = ?, exit_btc = ?, status = 'closed', result = ?,
                payout_usd = ?, pnl_usd = ?, note = ?
            WHERE id = ?
        """, (
            datetime.utcnow().isoformat(),
            float(current_btc),
            result,
            float(payout),
            float(pnl),
            f"binary_result={result}; won={won}",
            trade_id,
        ))

        resolved.append({
            "id": trade_id,
            "side": side,
            "result": result,
            "won": won,
            "stake": float(stake),
            "entry_price": float(entry_price),
            "shares": float(shares),
            "payout": float(payout),
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
        SELECT side, stake_usd, entry_price, shares, edge, confidence, status, pnl_usd, ev_usd, result, created_at
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
