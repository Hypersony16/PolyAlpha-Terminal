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
MIN_EDGE = 0.04
MIN_CONFIDENCE = "Medium"
MIN_EV_PER_DOLLAR = 0.02
MIN_MODEL_PROB = 0.56
MAX_ENTRY_PRICE = 0.78
MIN_LIQUIDITY = 0.0
MIN_TIME_LEFT_SECONDS = 120

MAX_OPEN_TRADES = 1
ONE_TRADE_PER_WINDOW = True
RESOLVE_GRACE_SECONDS = 75  # wait for official Polymarket settlement, avoids false local outcomes

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
            closed_at TEXT,
            window_start TEXT,
            market_slug TEXT,
            market_question TEXT,
            target_price REAL,
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
            resolution_source TEXT,
            official_outcome TEXT,
            note TEXT
        )
    """)

    for ddl in [
        "ALTER TABLE paper_auto_trades ADD COLUMN window_start TEXT",
        "ALTER TABLE paper_auto_trades ADD COLUMN entry_price REAL DEFAULT 0.5",
        "ALTER TABLE paper_auto_trades ADD COLUMN shares REAL DEFAULT 0",
        "ALTER TABLE paper_auto_trades ADD COLUMN result TEXT",
        "ALTER TABLE paper_auto_trades ADD COLUMN payout_usd REAL DEFAULT 0",
        "ALTER TABLE paper_auto_trades ADD COLUMN ev_usd REAL DEFAULT 0",
        "ALTER TABLE paper_auto_trades ADD COLUMN market_prob REAL DEFAULT 0.5",
        "ALTER TABLE paper_auto_trades ADD COLUMN note TEXT",
        "ALTER TABLE paper_auto_trades ADD COLUMN market_slug TEXT",
        "ALTER TABLE paper_auto_trades ADD COLUMN market_question TEXT",
        "ALTER TABLE paper_auto_trades ADD COLUMN target_price REAL",
        "ALTER TABLE paper_auto_trades ADD COLUMN resolution_source TEXT",
        "ALTER TABLE paper_auto_trades ADD COLUMN official_outcome TEXT",
    ]:
        try:
            cur.execute(ddl)
        except Exception:
            pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS paper_calibration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            window_start TEXT,
            market_slug TEXT,
            market_question TEXT,
            target_price REAL,
            side TEXT NOT NULL,
            model_prob REAL NOT NULL,
            market_prob REAL NOT NULL,
            edge REAL NOT NULL,
            entry_price REAL NOT NULL,
            odds_source TEXT,
            confidence TEXT,
            result TEXT,
            correct INTEGER,
            pnl_usd REAL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_paper_auto_user_status
        ON paper_auto_trades(user_id, status)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_paper_auto_user_window
        ON paper_auto_trades(user_id, window_start)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_paper_calib_user_created
        ON paper_calibration(user_id, created_at)
    """)

    conn.commit()
    conn.close()


def paper_enabled(user_id: int) -> bool:
    return get_user_setting(user_id, "paper_auto_enabled", "0") == "1"


def set_paper_enabled(user_id: int, enabled: bool):
    set_user_setting(user_id, "paper_auto_enabled", "1" if enabled else "0")


def real_odds_only(user_id: int) -> bool:
    # Default ON: no more fallback_50 trades.
    return get_user_setting(user_id, "paper_real_odds_only", "1") == "1"


def set_real_odds_only(user_id: int, enabled: bool):
    set_user_setting(user_id, "paper_real_odds_only", "1" if enabled else "0")


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
    raw = get_user_setting(user_id, "paper_auto_balance", str(DEFAULT_BALANCE))
    try:
        return float(raw)
    except Exception:
        return DEFAULT_BALANCE


def set_balance(user_id: int, balance: float):
    set_user_setting(user_id, "paper_auto_balance", f"{balance:.6f}")


def reset_account(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM paper_auto_trades WHERE user_id = ?", (str(user_id),))
    cur.execute("DELETE FROM paper_calibration WHERE user_id = ?", (str(user_id),))
    conn.commit()
    conn.close()

    set_balance(user_id, DEFAULT_BALANCE)
    set_last_skip_reason(user_id, "reset account")



def due_open_market_slugs(user_id: int) -> List[str]:
    """
    Return open trade market slugs that are past the real window end + grace.
    Jobs will fetch official Polymarket resolution for these slugs.
    """
    ensure_paper_auto_tables()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT market_slug, window_start
        FROM paper_auto_trades
        WHERE user_id = ? AND status = 'open' AND market_slug IS NOT NULL AND market_slug != ''
    """, (str(user_id),))
    rows = cur.fetchall()
    conn.close()

    now_dt = datetime.now(timezone.utc)
    due = []
    for slug, window_start in rows:
        window_end = _window_end_from_start(window_start)
        if window_end is None:
            continue
        if now_dt >= window_end + timedelta(seconds=RESOLVE_GRACE_SECONDS):
            due.append(str(slug))
    return due


def open_trade_count(user_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM paper_auto_trades
        WHERE user_id = ? AND status = 'open'
    """, (str(user_id),))
    value = int(cur.fetchone()[0])
    conn.close()
    return value


def already_traded_market(user_id: int, market_slug: str) -> bool:
    if not market_slug:
        return False
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM paper_auto_trades
        WHERE user_id = ? AND market_slug = ?
        LIMIT 1
    """, (str(user_id), str(market_slug)))
    exists = cur.fetchone() is not None
    conn.close()
    return exists


def already_traded_window(user_id: int, window_start: str) -> bool:
    if not ONE_TRADE_PER_WINDOW:
        return False

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM paper_auto_trades
        WHERE user_id = ? AND window_start = ?
        LIMIT 1
    """, (str(user_id), str(window_start)))
    exists = cur.fetchone() is not None
    conn.close()
    return exists


def get_entry_price(model: Dict[str, Any]) -> float:
    price = model.get("entry_price")
    if price is None:
        side = str(model.get("signal", "")).upper()
        price = model.get("market_up" if side == "UP" else "market_down", model.get("market_prob", 0.5))

    try:
        price = float(price)
    except Exception:
        price = 0.5

    return max(0.01, min(0.99, price))


def estimate_ev_usd(stake: float, entry_price: float, model_prob: float, slippage_usd: float) -> float:
    entry_price = max(0.01, min(0.99, float(entry_price)))
    shares = stake / entry_price
    expected_payout = model_prob * shares
    return expected_payout - stake - slippage_usd


def should_enter(user_id: int, model: Dict[str, Any]) -> tuple[bool, str]:
    edge = float(model.get("edge", 0))
    ev = float(model.get("ev_per_dollar", 0))
    model_prob = float(model.get("model_prob", 0))
    market_prob = float(model.get("market_prob", 0.5))
    odds_source = str(model.get("odds_source", "fallback_50"))
    phase = model.get("window", {}).get("phase", "?")
    entry_price = get_entry_price(model)

    # Priority 1: real odds only. No more fake 50/50 paper profit unless explicitly disabled.
    if real_odds_only(user_id) and (odds_source == "fallback_50" or entry_price == 0.5 and abs(market_prob - 0.5) < 0.001):
        return False, "waiting for real Polymarket odds"

    if phase not in ("Prime", "Late"):
        return False, f"bad phase: {phase}"

    if entry_price > MAX_ENTRY_PRICE:
        return False, "entry price too expensive"

    if model_prob < MIN_MODEL_PROB:
        return False, "model probability too low"

    if edge < MIN_EDGE:
        return False, "edge too low"

    if ev < MIN_EV_PER_DOLLAR:
        return False, "EV too low"

    if CONF_RANK.get(model.get("confidence"), 0) < CONF_RANK.get(MIN_CONFIDENCE, 1):
        return False, "confidence too low"

    # Optional market liquidity check when present.
    market = model.get("market") or {}
    try:
        liquidity = float(market.get("liquidity") or 0)
        if MIN_LIQUIDITY > 0 and liquidity < MIN_LIQUIDITY:
            return False, "liquidity too low"
    except Exception:
        pass

    stake_test = MIN_POSITION_USD
    slippage_test = stake_test * SLIPPAGE_RATE
    ev_test = estimate_ev_usd(stake_test, entry_price, model_prob, slippage_test)

    if ev_test <= 0:
        return False, "negative binary EV after slippage"

    return True, "ok"


def calc_stake(user_id: int, balance: float, model: Dict[str, Any]) -> float:
    max_bet = get_max_bet(user_id)
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
    market_slug = _market_slug_from_model(model)
    market_question = _market_question_from_model(model)
    target_price = _target_price_from_model(model)

    # Prefer market slug for de-dupe because it matches the real Polymarket contract.
    if market_slug and already_traded_market(user_id, market_slug):
        set_last_skip_reason(user_id, "already traded this market id", model)
        return {"opened": False, "reason": "already traded this market id"}

    if already_traded_window(user_id, window_start):
        set_last_skip_reason(user_id, "already traded this 15m window", model)
        return {"opened": False, "reason": "already traded this 15m window"}

    ok, reason = should_enter(user_id, model)
    if not ok:
        set_last_skip_reason(user_id, reason, model)
        return {"opened": False, "reason": reason}

    stake = calc_stake(user_id, balance, model)
    if stake > balance:
        set_last_skip_reason(user_id, "not enough paper balance", model)
        return {"opened": False, "reason": "not enough paper balance"}

    side = model["signal"]
    entry_price = get_entry_price(model)
    shares = stake / entry_price

    fee = 0.0
    slippage = stake * SLIPPAGE_RATE
    cost = stake + fee + slippage
    ev_usd = estimate_ev_usd(stake, entry_price, float(model["model_prob"]), slippage)

    now = datetime.utcnow().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO paper_auto_trades (
            user_id, created_at, window_start, market_slug, market_question, target_price, side,
            entry_btc, open_price, entry_price, shares,
            model_prob, market_prob, edge, confidence,
            stake_usd, fee_usd, slippage_usd, status, ev_usd, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
    """, (
        str(user_id),
        now,
        window_start,
        market_slug,
        market_question,
        float(target_price),
        side,
        float(model["price"]),
        float(target_price),
        float(entry_price),
        float(shares),
        float(model["model_prob"]),
        float(model["market_prob"]),
        float(model["edge"]),
        str(model["confidence"]),
        float(stake),
        float(fee),
        float(slippage),
        float(ev_usd),
        f"phase={model['window']['phase']}; odds={model.get('odds_source','?')}; market={market_slug}; target={target_price}; binary_payout=1_if_correct",
    ))
    conn.commit()
    conn.close()

    set_balance(user_id, balance - cost)
    set_last_skip_reason(user_id, "opened trade", model)

    return {
        "opened": True,
        "side": side,
        "stake": stake,
        "entry_price": entry_price,
        "shares": shares,
        "cost": cost,
        "ev": ev_usd,
        "balance_after": get_balance(user_id),
        "market_slug": market_slug,
        "market_question": market_question,
        "target_price": target_price,
        "window_start": window_start,
    }

def _log_calibration(user_id: int, row: dict, cur=None):
    own_conn = None
    if cur is None:
        own_conn = get_conn()
        cur = own_conn.cursor()

    cur.execute("""
        INSERT INTO paper_calibration (
            user_id, created_at, window_start, side,
            model_prob, market_prob, edge, entry_price,
            odds_source, confidence, result, correct, pnl_usd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(user_id),
        datetime.utcnow().isoformat(),
        row.get("window_start"),
        row.get("side"),
        float(row.get("model_prob", 0)),
        float(row.get("market_prob", 0)),
        float(row.get("edge", 0)),
        float(row.get("entry_price", 0.5)),
        row.get("odds_source", ""),
        row.get("confidence", ""),
        row.get("result", ""),
        1 if row.get("correct") else 0,
        float(row.get("pnl_usd", 0)),
    ))

    if own_conn is not None:
        own_conn.commit()
        own_conn.close()


def resolve_open_trades(user_id: int, current_price: float, official_resolutions: Dict[str, Dict[str, Any]] | None = None) -> List[Dict[str, Any]]:
    """
    Resolve only with official Polymarket outcome.

    This is critical:
    Do NOT settle by local BTC > target. Polymarket can settle using its own oracle/snapshot,
    so local BTC comparison can produce false UP/DOWN.
    """
    ensure_paper_auto_tables()
    official_resolutions = official_resolutions or {}

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, created_at, window_start, market_slug, market_question, target_price,
               side, entry_btc, open_price, entry_price, shares, stake_usd,
               fee_usd, slippage_usd, model_prob, market_prob, edge, confidence, note
        FROM paper_auto_trades
        WHERE user_id = ? AND status = 'open'
    """, (str(user_id),))
    rows = cur.fetchall()

    resolved = []
    now_dt_aware = datetime.now(timezone.utc)
    now_dt = datetime.utcnow()

    for row in rows:
        (
            trade_id, created_at, window_start, market_slug, market_question, target_price,
            side, entry_btc, open_price, entry_price, shares, stake, fee, slippage,
            model_prob, market_prob, edge, confidence, note
        ) = row

        window_end = _window_end_from_start(window_start)
        if window_end is not None:
            if now_dt_aware < window_end + timedelta(seconds=RESOLVE_GRACE_SECONDS):
                continue
        else:
            # old malformed row safety
            continue

        resolution = official_resolutions.get(str(market_slug or ""), {})
        if not resolution or not resolution.get("resolved") or resolution.get("outcome") not in ("UP", "DOWN"):
            # Do not close. Wait for official result.
            continue

        result = str(resolution["outcome"])
        target = float(target_price if target_price is not None else open_price)
        won = (result == side)
        payout = float(shares) if won else 0.0
        pnl = payout - float(stake) - float(fee) - float(slippage)
        resolution_source = str(resolution.get("source") or "gamma_official")

        # Update balance inside same DB connection to avoid SQLite lock.
        cur.execute(
            "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
            (str(user_id), "paper_auto_balance")
        )
        bal_row = cur.fetchone()
        try:
            current_balance = float(bal_row[0]) if bal_row else DEFAULT_BALANCE
        except Exception:
            current_balance = DEFAULT_BALANCE
        balance = current_balance + payout
        cur.execute("""
            INSERT INTO user_settings (user_id, key, value)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value
        """, (str(user_id), "paper_auto_balance", f"{balance:.6f}"))

        cur.execute("""
            UPDATE paper_auto_trades
            SET closed_at = ?, exit_btc = ?, status = 'closed',
                result = ?, official_outcome = ?, resolution_source = ?,
                payout_usd = ?, pnl_usd = ?
            WHERE id = ?
        """, (
            now_dt.isoformat(),
            float(current_price),
            result,
            result,
            resolution_source,
            float(payout),
            float(pnl),
            trade_id,
        ))

        odds_source = ""
        if note and "odds=" in note:
            try:
                odds_source = note.split("odds=", 1)[1].split(";", 1)[0]
            except Exception:
                odds_source = ""

        _log_calibration(user_id, {
            "window_start": window_start,
            "market_slug": market_slug,
            "side": side,
            "model_prob": model_prob,
            "market_prob": market_prob,
            "edge": edge,
            "entry_price": entry_price,
            "odds_source": odds_source,
            "confidence": confidence,
            "result": result,
            "correct": won,
            "pnl_usd": pnl,
        }, cur=cur)

        resolved.append({
            "id": trade_id,
            "market_slug": market_slug or "",
            "market_question": market_question or "",
            "resolution_source": resolution_source,
            "official_outcome": result,
            "side": side,
            "result": result,
            "won": won,
            "stake": float(stake),
            "entry_price": float(entry_price),
            "shares": float(shares),
            "payout": float(payout),
            "pnl": float(pnl),
            "entry_btc": float(entry_btc),
            "exit_btc": float(current_price),
            "target_price": target,
            "window_start": window_start,
        })

    conn.commit()
    conn.close()
    return resolved

def calibration_summary(user_id: int) -> Dict[str, Any]:
    ensure_paper_auto_tables()
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*), COALESCE(SUM(correct),0), COALESCE(AVG(model_prob),0),
               COALESCE(AVG(market_prob),0), COALESCE(AVG(edge),0),
               COALESCE(SUM(pnl_usd),0)
        FROM paper_calibration
        WHERE user_id = ?
    """, (str(user_id),))
    total, correct, avg_model, avg_market, avg_edge, pnl = cur.fetchone()

    buckets = []
    for lo, hi in [(0.50,0.60),(0.60,0.70),(0.70,0.80),(0.80,0.90),(0.90,1.01)]:
        cur.execute("""
            SELECT COUNT(*), COALESCE(SUM(correct),0), COALESCE(AVG(model_prob),0)
            FROM paper_calibration
            WHERE user_id = ? AND model_prob >= ? AND model_prob < ?
        """, (str(user_id), lo, hi))
        n, c, avgp = cur.fetchone()
        if n:
            buckets.append({
                "range": f"{int(lo*100)}-{int((hi if hi < 1 else 1)*100)}%",
                "n": int(n),
                "hit": float(c)/float(n),
                "avg_model": float(avgp),
            })

    cur.execute("""
        SELECT side, COUNT(*), COALESCE(SUM(correct),0), COALESCE(SUM(pnl_usd),0)
        FROM paper_calibration
        WHERE user_id = ?
        GROUP BY side
    """, (str(user_id),))
    by_side = [
        {"side": r[0], "n": int(r[1]), "hit": float(r[2])/float(r[1]) if r[1] else 0, "pnl": float(r[3])}
        for r in cur.fetchall()
    ]

    conn.close()

    total = int(total or 0)
    correct = int(correct or 0)
    return {
        "total": total,
        "correct": correct,
        "accuracy": (correct / total) if total else 0.0,
        "avg_model": float(avg_model or 0),
        "avg_market": float(avg_market or 0),
        "avg_edge": float(avg_edge or 0),
        "pnl": float(pnl or 0),
        "buckets": buckets,
        "by_side": by_side,
    }


def paper_auto_summary(user_id: int) -> Dict[str, Any]:
    ensure_paper_auto_tables()

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*), COALESCE(SUM(pnl_usd),0),
               SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='closed' AND pnl_usd > 0 THEN 1 ELSE 0 END)
        FROM paper_auto_trades
        WHERE user_id = ?
    """, (str(user_id),))
    total, pnl, open_count, closed_count, wins = cur.fetchone()

    cur.execute("""
        SELECT side, stake_usd, entry_price, shares, edge, confidence, status,
               pnl_usd, ev_usd, result, created_at, market_slug
        FROM paper_auto_trades
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 5
    """, (str(user_id),))
    recent = cur.fetchall()

    conn.close()

    total = int(total or 0)
    open_count = int(open_count or 0)
    closed_count = int(closed_count or 0)
    wins = int(wins or 0)
    win_rate = (wins / closed_count) if closed_count else 0.0

    return {
        "enabled": paper_enabled(user_id),
        "real_odds_only": real_odds_only(user_id),
        "balance": get_balance(user_id),
        "total": total,
        "open": open_count,
        "closed": closed_count,
        "wins": wins,
        "win_rate": win_rate,
        "pnl": float(pnl or 0),
        "recent": recent,
        "calibration": calibration_summary(user_id),
    }
