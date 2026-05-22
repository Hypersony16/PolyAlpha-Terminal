import logging
from typing import Optional

from telegram import Update
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes

from bot.cache import cache
from bot.config import ADMIN_USER_IDS
from bot.db import (
    touch_active_user,
    get_active_users,
    get_user_setting,
    set_user_setting,
    get_tracked_wallets,
    add_tracked_wallet,
    remove_tracked_wallet,
    update_wallet_nickname,
    get_own_wallet,
    set_own_wallet,
    get_latest_wallet_snapshot,
    log_wallet_snapshot,
    get_recent_tracked_trades,
    get_signal_summary,
)
from bot.menus import (
    main_menu,
    btc_menu,
    wallet_menu,
    alerts_menu,
    settings_menu,
    admin_menu,
    paper_auto_menu,
    copy_size_menu,
)
from bot.btc import build_btc_model, format_btc_price
from bot.polymarket import discover_btc_15m_market, fetch_public_profile
from bot.wallet import fetch_wallet_total_value
from bot.trades import score_wallet_from_rows
from bot.maker import maker_snapshot
from bot.stats import (
    record_prediction,
    resolve_due_predictions,
    prediction_accuracy,
    latency_summary,
    log_paper_trade,
    paper_summary,
)
from bot.paper_auto import (
    set_paper_enabled,
    reset_account,
    paper_auto_summary,
    set_max_bet,
    get_max_bet,
)
from bot.time_utils import timestamp_with_seconds


# ---------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------

def is_admin_user(user_id: Optional[int]) -> bool:
    return user_id is not None and user_id in ADMIN_USER_IDS


def get_view_mode(user_id: int) -> str:
    return get_user_setting(user_id, "view_mode", "normal") or "normal"


def get_notify_mode(user_id: int) -> str:
    return get_user_setting(user_id, "notify_mode", "normal") or "normal"


def get_alerts_enabled(user_id: int) -> str:
    return get_user_setting(user_id, "alerts_enabled", "0") or "0"


def get_edge_threshold(user_id: int) -> float:
    try:
        return float(get_user_setting(user_id, "edge_threshold", "0.05"))
    except Exception:
        return 0.05


def header(title: str, subtitle: str = "") -> str:
    if subtitle:
        return f"<b>{title}</b>\n<code>{subtitle}</code>"
    return f"<b>{title}</b>"


def signal_emoji(signal: str) -> str:
    return "🟢" if str(signal).upper() == "UP" else "🔴"


def progress_bar(p: float, width: int = 12) -> str:
    p = max(0.0, min(1.0, float(p or 0)))
    filled = int(round(p * width))
    return "█" * filled + "░" * (width - filled)


async def safe_send(update: Update, text: str, reply_markup=None, parse_mode: str = "HTML"):
    """
    Works for BOTH slash commands and inline button callbacks.
    This fixes: 'NoneType' object has no attribute reply_text.
    """
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
    elif update.message:
        await update.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )


async def get_btc_context():
    market = await discover_btc_15m_market()
    model = await build_btc_model(market)
    return market, model


async def wallet_profile_link(wallet: str, nickname: str = "") -> str:
    label = nickname if nickname else wallet
    try:
        profile = await fetch_public_profile(wallet)
        username = profile.get("username") or profile.get("name")
        if username:
            return f'<a href="https://polymarket.com/@{username}">{label}</a>'
    except Exception:
        pass
    return label


def touch_user(update: Update):
    if not update.effective_user:
        return
    touch_active_user(
        update.effective_user.id,
        update.effective_user.username,
        update.effective_user.first_name,
    )
    if update.effective_chat:
        set_user_setting(update.effective_user.id, "alerts_chat_id", str(update.effective_chat.id))


# ---------------------------------------------------------------------
# Text builders
# ---------------------------------------------------------------------

async def build_home_text(user_id: int):
    market, model = await get_btc_context()
    record_prediction(user_id, model)

    wallets = get_tracked_wallets(user_id)
    own_wallet = get_own_wallet(user_id).strip()
    market_status = "Live" if market else "No active market found"

    return (
        f"{header('🚀 POLYSCALPBOT', 'BTC 15m Prediction Engine')}\n\n"
        f"💰 BTC: <b>{format_btc_price(model['price'])}</b>\n"
        f"🎯 Signal: <b>{signal_emoji(model['signal'])} {model['signal']}</b>\n"
        f"⚡ Edge: <b>+{model['edge'] * 100:.1f} pts</b>\n"
        f"🧠 Confidence: <b>{model['confidence']}</b>\n"
        f"⏱ Time left: <b>{model['window']['left_label']}</b>\n"
        f"📈 Market: <b>{market_status}</b>\n\n"
        f"👛 Own wallet: {'set' if own_wallet else 'not set'}\n"
        f"📡 Tracked wallets: {len(wallets)}\n"
        f"🔔 Alerts: {'ON' if get_alerts_enabled(user_id) == '1' else 'OFF'}\n\n"
        f"🕒 {timestamp_with_seconds()}"
    )


async def build_btc_text(user_id: int):
    market, model = await get_btc_context()
    record_prediction(user_id, model)

    up = model["model_up"]
    down = model["model_down"]
    market_line = market.get("question", "BTC 15m") if market else "Fallback model only"

    return (
        f"{header('₿ BTC 15M SIGNAL', 'Up/Down binary market model')}\n"
        f"🕒 {timestamp_with_seconds()}\n\n"
        f"💰 BTC: <b>{format_btc_price(model['price'])}</b>\n"
        f"🎯 Signal: <b>{signal_emoji(model['signal'])} {model['signal']}</b>\n"
        f"🧠 Confidence: <b>{model['confidence']}</b>\n"
        f"⚡ Edge: <b>+{model['edge'] * 100:.1f} pts</b>\n"
        f"📊 Model: <b>{model['model_prob'] * 100:.1f}%</b>\n"
        f"📈 Market: <b>{model['market_prob'] * 100:.1f}%</b> ({model.get('odds_source','?')})\n"
        f"💵 EV: <b>{model.get('ev_per_dollar', 0) * 100:.1f}% per $1</b>\n"
        f"🎯 Target: <b>{format_btc_price(model.get('target_price', model.get('open', 0)))}</b>\n"
        f"💼 Size: <b>{model['suggested_size_pct']:.2f}% bankroll</b>\n"
        f"⏱ Time left: <b>{model['window']['left_label']}</b>\n"
        f"Phase: <b>{model['window']['phase']}</b>\n\n"
        f"<b>Model distribution</b>\n"
        f"UP   {progress_bar(up)} {up * 100:.1f}%\n"
        f"DOWN {progress_bar(down)} {down * 100:.1f}%\n\n"
        f"<b>Market</b>\n{market_line}"
    )


async def build_market_text(user_id: int):
    market, model = await get_btc_context()

    if not market:
        return (
            f"{header('📈 LIVE MARKET', 'BTC 15m only')}\n"
            f"🕒 {timestamp_with_seconds()}\n\n"
            f"No active BTC 15m Polymarket market found right now.\n\n"
            f"BTC: <b>{format_btc_price(model['price'])}</b>\n"
            f"Signal: <b>{signal_emoji(model['signal'])} {model['signal']}</b>"
        )

    return (
        f"{header('📈 LIVE BTC 15M MARKET', 'Polymarket BTC only')}\n"
        f"🕒 {timestamp_with_seconds()}\n\n"
        f"{market.get('question', 'BTC 15m')}\n"
        f"Slug: <code>{market.get('slug', '')}</code>\n\n"
        f"🟢 UP price: <b>{market.get('up_price', 'n/a')}</b>\n"
        f"🔴 DOWN price: <b>{market.get('down_price', 'n/a')}</b>\n"
        f"🎯 Target: <b>{format_btc_price(float(market.get('target_price') or 0)) if market.get('target_price') else 'n/a'}</b>\n"
        f"Source: <code>{market.get('up_price_source','?')}/{market.get('down_price_source','?')}</code>\n"
        f"💧 Liquidity: {float(market.get('liquidity', 0) or 0):,.0f}\n"
        f"📊 Volume: {float(market.get('volume', 0) or 0):,.0f}"
    )


async def build_strategy_text(user_id: int):
    market, model = await get_btc_context()
    maker = maker_snapshot(model, market)

    return (
        f"{header('🧠 STRATEGY LAB', '15m BTC model + maker scanner')}\n"
        f"🕒 {timestamp_with_seconds()}\n\n"
        f"<b>Directional model</b>\n"
        f"Signal: {signal_emoji(model['signal'])} <b>{model['signal']}</b>\n"
        f"Model probability: <b>{model['model_prob'] * 100:.1f}%</b>\n"
        f"Market probability: <b>{model['market_prob'] * 100:.1f}%</b> ({model.get('odds_source','?')})\n"
        f"Edge: <b>{model['edge'] * 100:.1f} pts</b>\n"
        f"EV per $1: <b>{model.get('ev_per_dollar', 0) * 100:.1f}%</b>\n"
        f"Kelly-lite size: <b>{model['suggested_size_pct']:.2f}%</b>\n\n"
        f"<b>Math inputs</b>\n"
        f"5m momentum: {model.get('momentum_5m', 0) * 100:.3f}%\n"
        f"15m momentum: {model.get('momentum_15m', 0) * 100:.3f}%\n"
        f"Vol regime: {model.get('vol_regime', 'Unknown')}\n"
        f"Phase: {model['window']['phase']}\n\n"
        f"<b>Maker scanner</b>\n"
        f"YES bid: {maker['yes_bid']:.3f}\n"
        f"NO bid: {maker['no_bid']:.3f}\n"
        f"Combined: {maker['combined']:.3f}\n"
        f"Merge edge: {maker['merge_edge']:.2f}¢\n"
        f"Fill risk: {maker['risk']}\n"
        f"Verdict: <b>{maker['verdict']}</b>"
    )


async def build_wallet_text(user_id: int):
    own = get_own_wallet(user_id).strip()
    wallets = get_tracked_wallets(user_id)

    parts = [
        header("👛 WALLET CENTER", "private per-user space"),
        f"🕒 {timestamp_with_seconds()}",
        "",
    ]

    if own:
        snap = get_latest_wallet_snapshot(user_id, own)
        value = f"${float(snap[1]):.2f}" if snap else "no snapshot"
        parts.append(f"<b>Own wallet</b>\n{await wallet_profile_link(own, 'My Wallet')} — {value}\n")
    else:
        parts.append("Own wallet not set. Use <code>/own_wallet 0x...</code>\n")

    if not wallets:
        parts.append("No tracked wallets. Use <code>/wallet_add 0x... Nickname</code>")
        return "\n".join(parts)

    parts.append("<b>Tracked wallets</b>")
    for item in wallets:
        address = item.get("address", "")
        nickname = item.get("nickname", "")
        recent = get_recent_tracked_trades(user_id, address, limit=20)
        score = score_wallet_from_rows(recent)
        snap = get_latest_wallet_snapshot(user_id, address)
        value = f"${float(snap[1]):.2f}" if snap else "no snapshot"
        parts.append(
            f"• {await wallet_profile_link(address, nickname)} — {value}\n"
            f"  {score.get('label', 'Unknown')} ({score.get('score', 0)}/100)"
        )

    return "\n".join(parts)


def build_alerts_text(user_id: int):
    return (
        f"{header('🔔 ALERT CONTROL')}\n"
        f"🕒 {timestamp_with_seconds()}\n\n"
        f"Status: <b>{'ON' if get_alerts_enabled(user_id) == '1' else 'OFF'}</b>\n"
        f"Mode: {get_notify_mode(user_id).title()}\n"
        f"Min edge: {get_edge_threshold(user_id) * 100:.1f}%"
    )


async def build_settings_text(user_id: int):
    return (
        f"{header('⚙️ SETTINGS')}\n"
        f"🕒 {timestamp_with_seconds()}\n\n"
        f"View mode: {get_view_mode(user_id).title()}\n"
        f"Alert mode: {get_notify_mode(user_id).title()}\n"
        f"Market mode: BTC 15m only\n"
        f"Paper trading: virtual only\n"
        f"Copy mode: preview only\n"
        f"No Binance dependency"
    )


async def build_accuracy_text(user_id: int):
    market, model = await get_btc_context()
    resolve_due_predictions(model["price"])

    periods = [(1, "1h"), (10, "10h"), (24, "1d")]
    lines = [
        header("📊 PREDICTION ACCURACY", "BTC 15m model performance"),
        f"🕒 {timestamp_with_seconds()}",
        "",
    ]

    for hours, label in periods:
        s = prediction_accuracy(user_id, hours)
        lines.append(
            f"<b>{label}</b> — {s['right']}/{s['total']} right "
            f"({s['accuracy'] * 100:.1f}%)"
        )

    paper = paper_summary(user_id)
    lines.extend([
        "",
        "<b>Manual paper stats</b>",
        f"Logged paper trades: {paper['count']}",
        f"Paper volume: ${paper['volume']:.2f}",
        f"Avg paper edge: {paper['avg_edge'] * 100:.2f} pts",
    ])

    return "\n".join(lines)


def build_paper_auto_text(user_id: int):
    s = paper_auto_summary(user_id)
    status = "ON" if s["enabled"] else "OFF"

    lines = [
        header("🤖 AUTO PAPER TRADING", "$100 virtual balance / BTC 15m"),
        f"🕒 {timestamp_with_seconds()}",
        "",
        f"Status: <b>{status}</b>",
        f"Balance: <b>${s['balance']:.2f}</b>",
        f"Total trades: <b>{s['total']}</b>",
        f"Open trades: <b>{s['open']}</b>",
        f"Closed trades: <b>{s['closed']}</b>",
        f"Total PnL: <b>${s['pnl']:.2f}</b>",
        f"Win rate: <b>{s['win_rate'] * 100:.1f}%</b>",
        f"Max bet: <b>${get_max_bet(user_id):.2f}</b>",
        "",
        "<b>Rules</b>",
        "• Paper only, no real money",
        "• Correct side pays $1/share",
        "• Max 1 trade per 15m market",
        "• Entry only in Prime/Late phase",
        "• Min edge 6%",
        "• Max position $5",
        "• 0.5% slippage assumption",
    ]

    if s.get("recent"):
        lines.append("")
        lines.append("<b>Recent</b>")
        for row in s["recent"]:
            try:
                side, stake, entry_price, shares, edge, confidence, row_status, pnl, ev, result, created_at = row
                pnl_txt = f"${float(pnl or 0):.2f}" if row_status == "closed" else "open"
                result_txt = f" | {result}" if result else ""
                lines.append(
                    f"• {side} ${float(stake):.2f} @ {float(entry_price):.3f} "
                    f"| {float(shares):.2f} sh | EV ${float(ev or 0):.2f} | {pnl_txt}{result_txt}"
                )
            except Exception:
                continue

    return "\n".join(lines)


def build_admin_text():
    users = get_active_users(20)
    lines = [
        header("🛠 ADMIN"),
        f"🕒 {timestamp_with_seconds()}",
        "",
        f"Active users shown: {len(users)}",
    ]

    for user_id, username, first_name, last_seen in users[:10]:
        label = first_name or str(user_id)
        if username:
            label += f" (@{username})"
        lines.append(f"• {label} — {last_seen}")

    return "\n".join(lines)


# ---------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    await safe_send(
        update,
        await build_home_text(update.effective_user.id),
        reply_markup=main_menu(is_admin_user(update.effective_user.id)),
    )


async def btc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    await safe_send(update, await build_btc_text(update.effective_user.id), reply_markup=btc_menu())


async def market_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    await safe_send(update, await build_market_text(update.effective_user.id), reply_markup=btc_menu())


async def bestbet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await btc_cmd(update, context)


async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    await safe_send(update, await build_strategy_text(update.effective_user.id), reply_markup=btc_menu())


async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    await safe_send(
        update,
        await build_accuracy_text(update.effective_user.id),
        reply_markup=main_menu(is_admin_user(update.effective_user.id)),
    )


async def paper_auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    await safe_send(update, build_paper_auto_text(update.effective_user.id), reply_markup=paper_auto_menu())


async def paper_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    set_paper_enabled(update.effective_user.id, True)
    await safe_send(update, "▶️ Auto paper trading enabled.", reply_markup=paper_auto_menu())


async def paper_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    set_paper_enabled(update.effective_user.id, False)
    await safe_send(update, "⏸ Auto paper trading disabled.", reply_markup=paper_auto_menu())


async def paper_reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    reset_account(update.effective_user.id)
    await safe_send(update, "♻️ Paper account reset to $100.", reply_markup=paper_auto_menu())


async def paper_trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Manual paper log only; auto paper has its own engine.
    touch_user(update)
    market, model = await get_btc_context()
    size = 1.0
    try:
        if context.args:
            size = max(1.0, min(25.0, float(context.args[0])))
    except Exception:
        size = 1.0
    log_paper_trade(update.effective_user.id, model, size)
    await safe_send(
        update,
        f"🧪 Manual paper trade logged\nSignal: {model['signal']}\nSize: ${size:.2f}\nEdge: {model['edge'] * 100:.1f} pts",
    )


async def wallet_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    try:
        address = context.args[0]
        nickname = " ".join(context.args[1:]).strip()
        add_tracked_wallet(update.effective_user.id, address, nickname)
        try:
            value = await fetch_wallet_total_value(address)
            log_wallet_snapshot(update.effective_user.id, address, value)
            msg = f"Wallet added: {nickname or address}\nValue: ${value:.2f}"
        except Exception:
            msg = f"Wallet added: {nickname or address}\nValue snapshot unavailable."
        await safe_send(update, msg)
    except Exception:
        await safe_send(update, "Usage: /wallet_add 0x... Nickname")


async def wallet_remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        remove_tracked_wallet(update.effective_user.id, context.args[0])
        await safe_send(update, "Wallet removed.")
    except Exception:
        await safe_send(update, "Usage: /wallet_remove 0x...")


async def wallet_name_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        update_wallet_nickname(update.effective_user.id, context.args[0], " ".join(context.args[1:]).strip())
        await safe_send(update, "Wallet renamed.")
    except Exception:
        await safe_send(update, "Usage: /wallet_name 0x... Nickname")


async def own_wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        set_own_wallet(update.effective_user.id, context.args[0])
        await safe_send(update, "Own wallet set.")
    except Exception:
        await safe_send(update, "Usage: /own_wallet 0x...")


async def wallet_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    await safe_send(update, await build_wallet_text(update.effective_user.id), reply_markup=wallet_menu())


async def alerts_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_setting(update.effective_user.id, "alerts_enabled", "1")
    await safe_send(update, "Alerts enabled.", reply_markup=alerts_menu())


async def alerts_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_setting(update.effective_user.id, "alerts_enabled", "0")
    await safe_send(update, "Alerts disabled.", reply_markup=alerts_menu())


async def set_edge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = float(context.args[0])
        set_user_setting(update.effective_user.id, "edge_threshold", str(value / 100.0))
        await safe_send(update, f"Edge threshold set to {value:.1f}%")
    except Exception:
        await safe_send(update, "Usage: /set_edge 5")


async def pnl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    summary = get_signal_summary(update.effective_user.id)
    await safe_send(
        update,
        f"{header('📊 SIGNAL LOG')}\nSignals: {summary['count']}\nAvg edge: {summary['avg_edge'] * 100:.2f} pts",
    )


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    await safe_send(update, await build_settings_text(update.effective_user.id), reply_markup=settings_menu(is_admin_user(update.effective_user.id)))


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        await safe_send(update, "Admin only.")
        return
    await safe_send(update, build_admin_text(), reply_markup=admin_menu())


async def copy_mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send(update, "Copy mode is currently SAFE PREVIEW only. Real execution is disabled.")


async def paper_max_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    FIX: No longer uses update.message.text (crashes on message-less updates).
    Instead derives the amount from the command name via the entity text or
    falls back to context.args. Works safely for both commands and callbacks.
    """
    touch_user(update)

    # Derive amount from the command text or entity.
    # The command will be /paper_1, /paper_2, or /paper_5.
    amount = None

    # Safe: try to read from message text (works for direct commands)
    try:
        if update.message and update.message.text:
            cmd_text = update.message.text.split()[0].lstrip("/").split("@")[0]
            # e.g. "paper_1" -> "1"
            parts = cmd_text.split("_")
            if len(parts) >= 2:
                amount = float(parts[-1])
    except Exception:
        amount = None

    # Fallback: try context.args
    if amount is None and context.args:
        try:
            amount = float(context.args[0])
        except Exception:
            pass

    if amount is None or amount not in (1, 2, 5):
        await safe_send(update, "Usage: /paper_1, /paper_2 or /paper_5", reply_markup=paper_auto_menu())
        return

    set_max_bet(update.effective_user.id, amount)
    await safe_send(update, f"✅ Max paper bet set to ${amount:.0f}.", reply_markup=paper_auto_menu())


# ---------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    touch_user(update)

    user_id = update.effective_user.id
    data = query.data

    try:
        if data in ("home", "refresh"):
            await start(update, context)

        elif data in ("btc", "bestbet"):
            await btc_cmd(update, context)

        elif data == "market":
            await market_cmd(update, context)

        elif data in ("strategy", "analyze"):
            await analyze_cmd(update, context)

        elif data in ("accuracy", "stats"):
            await stats_cmd(update, context)

        elif data == "wallets":
            await safe_send(update, await build_wallet_text(user_id), reply_markup=wallet_menu())

        elif data == "alerts":
            await safe_send(update, build_alerts_text(user_id), reply_markup=alerts_menu())

        elif data == "settings":
            await settings_cmd(update, context)

        elif data == "admin":
            await admin_cmd(update, context)

        elif data == "paper_auto":
            await paper_auto_cmd(update, context)

        elif data == "paper_auto_start":
            await paper_start_cmd(update, context)

        elif data == "paper_auto_stop":
            await paper_stop_cmd(update, context)

        elif data == "paper_auto_reset":
            await paper_reset_cmd(update, context)

        elif data == "paper_auto_balance":
            await paper_auto_cmd(update, context)

        elif data.startswith("paper_max_"):
            # FIX: parse the amount directly from callback_data, not update.message
            amount = float(data.split("_")[-1])
            set_max_bet(user_id, amount)
            await safe_send(update, f"✅ Max paper bet set to ${amount:.0f}.", reply_markup=paper_auto_menu())

        elif data == "wallet_add_hint":
            await safe_send(update, "Use /wallet_add 0x... Nickname")

        elif data == "wallet_remove_hint":
            await safe_send(update, "Use /wallet_remove 0x...")

        elif data == "wallet_name_hint":
            await safe_send(update, "Use /wallet_name 0x... Nickname")

        elif data == "own_wallet_hint":
            await safe_send(update, "Use /own_wallet 0x...")

        elif data == "alerts_on":
            set_user_setting(user_id, "alerts_enabled", "1")
            await safe_send(update, "Alerts enabled.", reply_markup=alerts_menu())

        elif data == "alerts_off":
            set_user_setting(user_id, "alerts_enabled", "0")
            await safe_send(update, "Alerts disabled.", reply_markup=alerts_menu())

        elif data.startswith("edge_"):
            pct = float(data.split("_")[1])
            set_user_setting(user_id, "edge_threshold", str(pct / 100.0))
            await safe_send(update, f"Edge threshold set to {pct:.0f}%", reply_markup=alerts_menu())

        elif data == "notify_quiet":
            set_user_setting(user_id, "notify_mode", "quiet")
            await safe_send(update, "Notify mode: Quiet", reply_markup=settings_menu(is_admin_user(user_id)))

        elif data == "notify_normal":
            set_user_setting(user_id, "notify_mode", "normal")
            await safe_send(update, "Notify mode: Normal", reply_markup=settings_menu(is_admin_user(user_id)))

        elif data.startswith("view_"):
            mode = data.replace("view_", "")
            set_user_setting(user_id, "view_mode", mode)
            await safe_send(update, f"View mode: {mode.title()}", reply_markup=settings_menu(is_admin_user(user_id)))

        elif data == "clear_cache":
            cache.clear()
            await safe_send(update, "Cache cleared.", reply_markup=admin_menu())

        elif data == "live_on":
            set_user_setting(0, "live_dashboards_enabled", "1")
            await safe_send(update, "Live dashboards enabled.", reply_markup=admin_menu())

        elif data == "live_off":
            set_user_setting(0, "live_dashboards_enabled", "0")
            await safe_send(update, "Live dashboards disabled.", reply_markup=admin_menu())

        elif data.startswith("dash_"):
            seconds = data.split("_")[1]
            set_user_setting(0, "dashboard_refresh_seconds", seconds)
            await safe_send(update, f"Dashboard refresh: {seconds}s", reply_markup=admin_menu())

        elif data.startswith("copy_"):
            await safe_send(update, "Copy-trade preview selected. Choose size:", reply_markup=copy_size_menu(data.replace("copy_", "")))

        elif data.startswith("copy_size_"):
            amount = data.split("_")[-1]
            await safe_send(update, f"✅ Preview only: would copy this trade with ${amount}.\nReal execution is disabled.")

        elif data.startswith("ignore_"):
            await safe_send(update, "Ignored.")

        else:
            await safe_send(update, f"Unknown button: {data}")

    except Exception as e:
        logging.exception("button handler failed")
        await safe_send(update, f"Error: {e}")


# ---------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------

def register_handlers(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("btc", btc_cmd))
    app.add_handler(CommandHandler("market", market_cmd))
    app.add_handler(CommandHandler("bestbet", bestbet_cmd))
    app.add_handler(CommandHandler("analyze", analyze_cmd))
    app.add_handler(CommandHandler("strategy", analyze_cmd))
    app.add_handler(CommandHandler("refresh", refresh_cmd))

    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("accuracy", stats_cmd))
    app.add_handler(CommandHandler("pnl", pnl_cmd))

    app.add_handler(CommandHandler("paper", paper_auto_cmd))
    app.add_handler(CommandHandler("paper_auto", paper_auto_cmd))
    app.add_handler(CommandHandler("paper_start", paper_start_cmd))
    app.add_handler(CommandHandler("paper_stop", paper_stop_cmd))
    app.add_handler(CommandHandler("paper_reset", paper_reset_cmd))
    app.add_handler(CommandHandler("paper_1", paper_max_cmd))
    app.add_handler(CommandHandler("paper_2", paper_max_cmd))
    app.add_handler(CommandHandler("paper_5", paper_max_cmd))

    app.add_handler(CommandHandler("wallet_add", wallet_add_cmd))
    app.add_handler(CommandHandler("wallet_remove", wallet_remove_cmd))
    app.add_handler(CommandHandler("wallet_name", wallet_name_cmd))
    app.add_handler(CommandHandler("wallet_list", wallet_list_cmd))
    app.add_handler(CommandHandler("own_wallet", own_wallet_cmd))

    app.add_handler(CommandHandler("alerts_on", alerts_on_cmd))
    app.add_handler(CommandHandler("alerts_off", alerts_off_cmd))
    app.add_handler(CommandHandler("set_edge", set_edge_cmd))

    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("copy_mode", copy_mode_cmd))

    app.add_handler(CallbackQueryHandler(button_handler))
