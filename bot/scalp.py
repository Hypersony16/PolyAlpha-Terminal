
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

SCALP_TP_ABS = 0.12          # buy 0.20 -> take profit around 0.28
SCALP_TP_PCT = 0.30          # +22% relative
SCALP_STOP_ABS = 0.04       # stop if price moves against us by 5.5c
SCALP_TRAIL_ABS = 0.05       # once profitable, exit if it drops 4c from peak
SCALP_MIN_HOLD_SECONDS = 20
SCALP_MAX_HOLD_SECONDS = 240
SCALP_ENTRY_MAX = 0.60       # scalps should not buy expensive contracts
SCALP_ENTRY_MIN = 0.35
SCALP_MIN_EDGE = 0.12
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
        pass
    return None


def _should_scalp_enter(model: Dict[str, Any], user_id: int) -> tuple[bool, str, str]:
    signal = str(model.get("signal", "")).upper()
    edge = float(model.get("edge", 0) or 0)
    time_left = int(model.get("time_left_seconds", 0) or 0)

    if signal not in ("UP", "DOWN"):
        return False, "", "No clear signal"
    if edge < SCALP_MIN_EDGE:
        return False, "", f"Edge too low for scalp: {edge:.3f}"
    if time_left < SCALP_MIN_TIME_LEFT:
        return False, "", f"Too little time for scalp: {time_left}s"
    if time_left > SCALP_MAX_TIME_LEFT:
        return False, "", f"Too much time for scalp: {time_left}s"

    entry_price = get_entry_price(model, signal)
    if entry_price > SCALP_ENTRY_MAX:
        return False, "", f"Entry too high for scalp: {entry_price:.3f}"
    if entry_price < SCALP_ENTRY_MIN:
        return False, "", f"Entry too low for scalp: {entry_price:.3f}"

    return True, signal, "OK"


def open_scalp_or_resolution_trade(user_id: int, model: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ensure_paper_auto_tables()
    mode = get_strategy_mode(user_id)

    if open_trade_count(user_id) >= 1:
        set_last_skip_reason(user_id, "Max open trades reached")
        return None

    market_slug = _market_slug_from_model(model)
    window_start = str(model.get("window_start", ""))

    if already_traded_market(user_id, market_slug):
        set_last_skip_reason(user_id, f"Already traded market: {market_slug}")
        return None

    if window_start and already_traded_window(user_id, window_start):
        set_last_skip_reason(user_id, f"Already traded window: {window_start}")
        return None

    if mode == "scalp":
        ok, side, reason = _should_scalp_enter(model, user_id)
        trade_mode = "scalp"
    elif mode == "resolution":
        ok, side, reason = should_enter(model, user_id)
        trade_mode = "resolution"
    else:  # hybrid
        ok_scalp, side_scalp, reason_scalp = _should_scalp_enter(model, user_id)
        ok_res, side_res, reason_res = should_enter(model, user_id)
        if ok_scalp:
            ok, side, reason, trade_mode = ok_scalp, side_scalp, reason_scalp, "scalp"
        elif ok_res:
            ok, side, reason, trade_mode = ok_res, side_res, reason_res, "resolution"
        else:
            ok, side, reason, trade_mode = False, "", reason_res, "resolution"

    if not ok:
        set_last_skip_reason(user_id, reason)
        return None

    stake = calc_stake(user_id, model)
    if stake <= 0:
        set_last_skip_reason(user_id, "Insufficient balance")
        return None

    entry_price = get_entry_price(model, side)
    shares = round(stake / entry_price, 4) if entry_price > 0 else 0
    edge = float(model.get("edge", 0) or 0)
    confidence = str(model.get("confidence", "Low"))
    model_prob = float(model.get("model_prob", 0) or 0)
    market_question = _market_question_from_model(model)
    target_price = _target_price_from_model(model)
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
        (str(user_id), _now_iso(), market_slug, market_question, side,
         entry_price, target_price, stake, shares, model_prob, edge, confidence,
         window_start, window_end_ts, trade_mode),
    )
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()

    return {
        "trade_id": trade_id,
        "side": side,
        "entry_price": entry_price,
        "stake": stake,
        "shares": shares,
        "trade_mode": trade_mode,
        "market_slug": market_slug,
    }


def resolve_scalp_trades(user_id: int, market: Dict[str, Any]) -> List[Dict[str, Any]]:
    ensure_paper_auto_tables()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, side, entry_price, stake_usd, shares, created_at, trade_mode
           FROM paper_auto_trades
           WHERE user_id = ? AND status = 'open' AND trade_mode = 'scalp'""",
        (str(user_id),),
    )
    rows = cur.fetchall()
    conn.close()

    resolved = []
    now = datetime.utcnow()

    for row in rows:
        trade_id, side, entry_price, stake, shares, created_at_str, trade_mode = row
        created_at = _dt(created_at_str)
        if not created_at:
            continue

        hold_seconds = (now - created_at.replace(tzinfo=None)).total_seconds()
        if hold_seconds < SCALP_MIN_HOLD_SECONDS:
            continue

        current_price = _current_price_for_side(market, side)
        if current_price is None:
            continue

        price_change = current_price - entry_price
        should_exit = False
        exit_reason = ""

        if price_change >= SCALP_TP_ABS or (entry_price > 0 and price_change / entry_price >= SCALP_TP_PCT):
            should_exit = True
            exit_reason = "take_profit"
        elif price_change <= -SCALP_STOP_ABS:
            should_exit = True
            exit_reason = "stop_loss"
        elif hold_seconds >= SCALP_MAX_HOLD_SECONDS:
            should_exit = True
            exit_reason = "max_hold"

        if not should_exit:
            continue

        exit_price = max(0.01, min(0.99, current_price))
        payout = shares * exit_price
        pnl = round(payout - stake, 4)

        balance = get_balance(user_id)
        set_balance(user_id, balance + payout)

        conn = get_conn()
        conn.execute(
            """UPDATE paper_auto_trades
               SET status='closed', exit_price=?, pnl_usd=?, closed_at=?
               WHERE id=?""",
            (exit_price, pnl, _now_iso(), trade_id),
        )
        conn.commit()
        conn.close()

        resolved.append({
            "trade_id": trade_id,
            "side": side,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "stake": stake,
            "pnl": pnl,
            "exit_reason": exit_reason,
            "hold_seconds": int(hold_seconds),
        })

    return resolved


def scalp_analytics(user_id: int) -> Dict[str, Any]:
    ensure_paper_auto_tables()
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """SELECT COUNT(*),
                  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END),
                  COALESCE(SUM(pnl_usd), 0),
                  COALESCE(AVG(pnl_usd), 0),
                  COALESCE(AVG(entry_price), 0)
           FROM paper_auto_trades
           WHERE user_id = ? AND status = 'closed' AND trade_mode = 'scalp'""",
        (str(user_id),),
    )
    row = cur.fetchone()
    conn.close()

    n = row[0] or 0
    wins = row[1] or 0
    total_pnl = row[2] or 0.0
    avg_pnl = row[3] or 0.0
    avg_entry = row[4] or 0.0

    return {
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "winrate": round(wins / n * 100, 1) if n else 0.0,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(avg_pnl, 4),
        "avg_entry": round(avg_entry, 3),
    }
