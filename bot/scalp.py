
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

from bot.db import get_conn, get_user_setting, set_user_setting
from bot.paper_auto import (
    ensure_paper_auto_tables, get_balance, set_balance, get_max_bet,
    _market_slug_from_model, _market_question_from_model, _target_price_from_model,
    open_trade_count, already_traded_market, already_traded_window, get_entry_price,
    calc_stake, estimate_ev_usd, set_last_skip_reason, should_enter, SLIPPAGE_RATE,
)

SCALP_TP_ABS = 0.08          # buy 0.20 -> take profit around 0.28
SCALP_TP_PCT = 0.22          # +22% relative
SCALP_STOP_ABS = 0.055       # stop if price moves against us by 5.5c
SCALP_TRAIL_ABS = 0.04       # once profitable, exit if it drops 4c from peak
SCALP_MIN_HOLD_SECONDS = 20
SCALP_MAX_HOLD_SECONDS = 240
SCALP_ENTRY_MAX = 0.60       # scalps should not buy expensive contracts
SCALP_ENTRY_MIN = 0.30
SCALP_MIN_EDGE = 0.08
SCALP_MIN_TIME_LEFT = 300
SCALP_MAX_TIME_LEFT = 720

MODES = ("resolution", "scalp", "hybrid")


def get_strategy_mode(user_id: int) -> str:
    mode = get_user_setting(user_id, "paper_strategy_mode", "hybrid")
    return mode if mode in MODES else "hybrid"


def set_strategy_mode(user_id: int, mode: str):
    if mode not in MODES:
        mode = "hybrid"
    set_user_setting(user_id, "paper_strategy_mode", mode)


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _dt(value: str):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _current_price_for_side(market: Dict[str, Any], side: str) -> Optional[float]:
    if not market:
        return None
    key = "up_price" if side == "UP" else "down_price"
    val = market.get(key)
    try:
        val = float(val)
        if 0.001 <= val <= 0.999:
            return val
    except Exception:
        return None
    return None


def ensure_scalp_columns():
    ensure_paper_auto_tables()
    conn = get_conn()
    cur = conn.cursor()
    for ddl in [
        "ALTER TABLE paper_auto_trades ADD COLUMN trade_mode TEXT DEFAULT 'resolution'",
        "ALTER TABLE paper_auto_trades ADD COLUMN exit_reason TEXT",
        "ALTER TABLE paper_auto_trades ADD COLUMN peak_price REAL",
        "ALTER TABLE paper_auto_trades ADD COLUMN lowest_price REAL",
        "ALTER TABLE paper_auto_trades ADD COLUMN max_favorable REAL DEFAULT 0",
        "ALTER TABLE paper_auto_trades ADD COLUMN max_adverse REAL DEFAULT 0",
        "ALTER TABLE paper_auto_trades ADD COLUMN hold_seconds REAL DEFAULT 0",
    ]:
        try:
            cur.execute(ddl)
        except Exception:
            pass
    conn.commit()
    conn.close()


def choose_trade_mode(user_id: int, model: Dict[str, Any]) -> str:
    configured = get_strategy_mode(user_id)
    if configured in ("resolution", "scalp"):
        return configured

    entry = get_entry_price(model)
    edge = float(model.get("edge", 0))
    left = float(model.get("window", {}).get("left_sec", model.get("window", {}).get("seconds_left", 0)) or 0)
    conf = str(model.get("confidence", ""))

    # Hybrid: scalp cheaper/volatile/mid-window entries; hold only cleaner high-quality setups.
    if SCALP_ENTRY_MIN <= entry <= SCALP_ENTRY_MAX and edge >= SCALP_MIN_EDGE and SCALP_MIN_TIME_LEFT <= left <= SCALP_MAX_TIME_LEFT:
        return "scalp"

    if 0.45 <= entry <= 0.65 and edge >= 0.15 and conf == "High":
        return "resolution"

    return "resolution"


def should_enter_scalp(user_id: int, model: Dict[str, Any]) -> tuple[bool, str]:
    ok, reason = should_enter(user_id, model)
    if not ok:
        return False, reason

    entry = get_entry_price(model)
    edge = float(model.get("edge", 0))
    left = float(model.get("window", {}).get("left_sec", model.get("window", {}).get("seconds_left", 0)) or 0)

    if entry < SCALP_ENTRY_MIN:
        return False, "scalp entry too cheap"
    if entry > SCALP_ENTRY_MAX:
        return False, "scalp entry too expensive"
    if edge < SCALP_MIN_EDGE:
        return False, "scalp edge too low"
    if left < SCALP_MIN_TIME_LEFT:
        return False, "too close for scalp"
    if left > SCALP_MAX_TIME_LEFT:
        return False, "too early for scalp"

    return True, "ok"


def open_scalp_or_resolution_trade(user_id: int, model: Dict[str, Any]) -> Dict[str, Any]:
    """
    Open either scalp or resolution trade.
    Stores trade_mode so the job can exit scalps before settlement.
    """
    ensure_scalp_columns()

    mode = choose_trade_mode(user_id, model)
    if mode == "resolution":
        from bot.paper_auto import open_auto_trade
        opened = open_auto_trade(user_id, model)
        if opened.get("opened"):
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "UPDATE paper_auto_trades SET trade_mode='resolution' WHERE user_id=? AND market_slug=? AND status='open'",
                (str(user_id), str(opened.get("market_slug", "")))
            )
            conn.commit()
            conn.close()
            opened["trade_mode"] = "resolution"
        return opened

    # Scalp open logic
    balance = get_balance(user_id)
    if open_trade_count(user_id) >= 1:
        set_last_skip_reason(user_id, "already has open paper trade", model)
        return {"opened": False, "reason": "already has open paper trade"}

    window_start = model["window"]["start"].isoformat()
    market_slug = _market_slug_from_model(model)
    if market_slug and already_traded_market(user_id, market_slug):
        set_last_skip_reason(user_id, "already traded this market id", model)
        return {"opened": False, "reason": "already traded this market id"}
    if already_traded_window(user_id, window_start):
        set_last_skip_reason(user_id, "already traded this 15m window", model)
        return {"opened": False, "reason": "already traded this 15m window"}

    ok, reason = should_enter_scalp(user_id, model)
    if not ok:
        set_last_skip_reason(user_id, reason, model)
        return {"opened": False, "reason": reason}

    side = model["signal"]
    entry_price = get_entry_price(model)
    stake = calc_stake(user_id, balance, model)
    if stake > balance:
        return {"opened": False, "reason": "not enough balance"}

    shares = stake / entry_price
    slippage = stake * SLIPPAGE_RATE
    cost = stake + slippage
    ev_usd = estimate_ev_usd(stake, entry_price, float(model["model_prob"]), slippage)

    market_question = _market_question_from_model(model)
    target_price = _target_price_from_model(model)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO paper_auto_trades (
            user_id, created_at, window_start, market_slug, market_question, target_price, side,
            entry_btc, open_price, entry_price, shares,
            model_prob, market_prob, edge, confidence,
            stake_usd, fee_usd, slippage_usd, status, ev_usd, note,
            trade_mode, peak_price, lowest_price, max_favorable, max_adverse
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'open', ?, ?, 'scalp', ?, ?, 0, 0)
    """, (
        str(user_id), _now_iso(), window_start, market_slug, market_question, float(target_price), side,
        float(model["price"]), float(target_price), float(entry_price), float(shares),
        float(model["model_prob"]), float(model["market_prob"]), float(model["edge"]), str(model["confidence"]),
        float(stake), float(slippage), float(ev_usd),
        f"mode=scalp; phase={model['window']['phase']}; odds={model.get('odds_source','?')}; market={market_slug}; target={target_price}; scalp_tp_abs={SCALP_TP_ABS}; scalp_stop_abs={SCALP_STOP_ABS}",
        float(entry_price), float(entry_price),
    ))
    conn.commit()
    conn.close()

    set_balance(user_id, balance - cost)
    set_last_skip_reason(user_id, "opened scalp trade", model)

    return {
        "opened": True,
        "trade_mode": "scalp",
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


def resolve_scalp_trades(user_id: int, market: Dict[str, Any], model: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Exit scalp trades before expiry by live odds movement.
    Paper PnL = sell value - original stake - slippage.
    """
    ensure_scalp_columns()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, created_at, market_slug, side, entry_price, shares, stake_usd,
               slippage_usd, peak_price, lowest_price, entry_btc
        FROM paper_auto_trades
        WHERE user_id=? AND status='open' AND COALESCE(trade_mode,'resolution')='scalp'
    """, (str(user_id),))
    rows = cur.fetchall()

    closed = []
    now = datetime.utcnow()

    for row in rows:
        trade_id, created_at, slug, side, entry_price, shares, stake, slippage, peak, low, entry_btc = row
        if str(slug or "") != str((market or {}).get("slug") or ""):
            continue

        live_price = _current_price_for_side(market, side)
        if live_price is None:
            continue

        entry_price = float(entry_price)
        shares = float(shares)
        stake = float(stake)
        slippage = float(slippage or 0)
        peak = max(float(peak or entry_price), float(live_price))
        low = min(float(low or entry_price), float(live_price))
        favorable = peak - entry_price
        adverse = entry_price - low

        created_dt = _dt(created_at) or now
        if created_dt.tzinfo is not None:
            hold_seconds = (datetime.now(timezone.utc) - created_dt.astimezone(timezone.utc)).total_seconds()
        else:
            hold_seconds = (now - created_dt).total_seconds()

        reason = None
        target_abs = entry_price + SCALP_TP_ABS
        target_pct = entry_price * (1.0 + SCALP_TP_PCT)
        target = max(target_abs, target_pct)

        if hold_seconds >= SCALP_MIN_HOLD_SECONDS and live_price >= target:
            reason = "take_profit"
        elif hold_seconds >= SCALP_MIN_HOLD_SECONDS and live_price <= entry_price - SCALP_STOP_ABS:
            reason = "stop_loss"
        elif hold_seconds >= SCALP_MIN_HOLD_SECONDS and peak >= entry_price + 0.05 and live_price <= peak - SCALP_TRAIL_ABS:
            reason = "trailing_stop"
        elif hold_seconds >= SCALP_MAX_HOLD_SECONDS:
            reason = "time_exit"
        elif float(model.get("window", {}).get("left_sec", model.get("window", {}).get("seconds_left", 999)) or 999) < 75:
            reason = "expiry_risk_exit"

        cur.execute("""
            UPDATE paper_auto_trades
            SET peak_price=?, lowest_price=?, max_favorable=?, max_adverse=?, hold_seconds=?
            WHERE id=?
        """, (peak, low, favorable, adverse, float(hold_seconds), trade_id))

        if not reason:
            continue

        sell_value = shares * float(live_price)
        pnl = sell_value - stake - slippage

        # credit sell value
        cur.execute("SELECT value FROM user_settings WHERE user_id=? AND key='paper_auto_balance'", (str(user_id),))
        bal_row = cur.fetchone()
        try:
            bal = float(bal_row[0]) if bal_row else 100.0
        except Exception:
            bal = 100.0
        new_balance = bal + sell_value
        cur.execute("""
            INSERT INTO user_settings (user_id, key, value)
            VALUES (?, 'paper_auto_balance', ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value
        """, (str(user_id), f"{new_balance:.6f}"))

        cur.execute("""
            UPDATE paper_auto_trades
            SET closed_at=?, status='closed', exit_btc=?, result=?, payout_usd=?, pnl_usd=?, exit_reason=?
            WHERE id=?
        """, (
            _now_iso(), float(model.get("price", 0)), f"SCALP_{reason.upper()}",
            float(sell_value), float(pnl), reason, trade_id
        ))

        closed.append({
            "id": trade_id,
            "market_slug": slug or "",
            "trade_mode": "scalp",
            "side": side,
            "exit_reason": reason,
            "stake": stake,
            "entry_price": entry_price,
            "exit_price": float(live_price),
            "shares": shares,
            "payout": float(sell_value),
            "pnl": float(pnl),
            "entry_btc": float(entry_btc or 0),
            "exit_btc": float(model.get("price", 0)),
            "hold_seconds": float(hold_seconds),
            "mfe": float(favorable),
            "mae": float(adverse),
        })

    conn.commit()
    conn.close()
    return closed


def scalp_analytics(user_id: int) -> str:
    ensure_scalp_columns()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*), COALESCE(SUM(pnl_usd),0), COALESCE(AVG(pnl_usd),0),
               COALESCE(AVG(hold_seconds),0), COALESCE(AVG(max_favorable),0),
               COALESCE(AVG(max_adverse),0),
               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)
        FROM paper_auto_trades
        WHERE user_id=? AND COALESCE(trade_mode,'resolution')='scalp' AND status='closed'
    """, (str(user_id),))
    n, pnl, avg_pnl, avg_hold, avg_mfe, avg_mae, wins = cur.fetchone()
    conn.close()
    n = int(n or 0)
    wins = int(wins or 0)
    wr = wins / n if n else 0
    return (
        f"Scalp closed: {n}\\n"
        f"Scalp winrate: {wr*100:.1f}%\\n"
        f"Scalp PnL: ${float(pnl or 0):.2f}\\n"
        f"Avg scalp PnL: ${float(avg_pnl or 0):.3f}\\n"
        f"Avg hold: {float(avg_hold or 0):.0f}s\\n"
        f"Avg MFE: {float(avg_mfe or 0)*100:.1f}c\\n"
        f"Avg MAE: {float(avg_mae or 0)*100:.1f}c"
    )
