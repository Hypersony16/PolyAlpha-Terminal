import logging

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
)
from bot.time_utils import timestamp_with_seconds


def is_admin_user(user_id: int | None) -> bool:
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


def header(title: str, subtitle: str = "") -> str:
    if subtitle:
        return f"<b>{title}</b>\n<code>{subtitle}</code>"
    return f"<b>{title}</b>"


def progress_bar(p: float, width: int = 12) -> str:
    p = max(0.0, min(1.0, p))
    filled = int(round(p * width))
    return "█" * filled + "░" * (width - filled)


async def get_btc_context():
    market = await discover_btc_15m_market()
    model = await build_btc_model(market)
    return market, model


def signal_emoji(signal: str) -> str:
    return "🟢" if signal == "UP" else "🔴"


async def build_home_text(user_id: int):
    market, model = await get_btc_context()
    record_prediction(user_id, model)

    wallets = get_tracked_wallets(user_id)
    own_wallet = get_own_wallet(user_id).strip()

    market_status = "Live" if market else "No active market found"

    return (
        f"{header('🏠 POLYSCALP COMMAND CENTER', 'BTC 15m only')}\n"
        f"🕒 {timestamp_with_seconds()}\n\n"
        f"BTC: <b>{format_btc_price(model['price'])}</b>\n"
        f"Signal: <b>{signal_emoji(model['signal'])} {model['signal']}</b>\n"
        f"Edge: <b>+{model['edge'] * 100:.1f} pts</b>\n"
        f"Confidence: <b>{model['confidence']}</b>\n"
        f"Time left: <b>{model['window']['left_label']}</b>\n"
        f"Market: <b>{market_status}</b>\n\n"
        f"👛 Own wallet: {'set' if own_wallet else 'not set'}\n"
        f"📡 Tracked wallets: {len(wallets)}\n"
        f"🔔 Alerts: {'ON' if get_alerts_enabled(user_id) == '1' else 'OFF'}\n"
        f"👀 View: {get_view_mode(user_id).title()}"
    )


async def build_btc_text(user_id: int):
    market, model = await get_btc_context()
    record_prediction(user_id, model)

    up = model["model_up"]
    down = model["model_down"]

    market_line = "Fallback model only"
    if market:
        market_line = market.get("question", "BTC 15m")

    return (
        f"{header('₿ BTC 15M SIGNAL', 'Up/Down scalp model')}\n"
        f"🕒 {timestamp_with_seconds()}\n\n"
        f"💰 BTC: <b>{format_btc_price(model['price'])}</b>\n"
        f"🎯 Signal: <b>{signal_emoji(model['signal'])} {model['signal']}</b>\n"
        f"🧠 Confidence: <b>{model['confidence']}</b>\n"
        f"⚡ Edge: <b>+{model['edge'] * 100:.1f} pts</b>\n"
        f"💼 Size: <b>{model['suggested_size_pct']:.2f}% bankroll</b>\n"
        f"⏱ Left: <b>{model['window']['left_label']}</b>\n"
        f"Phase: {model['window']['phase']}\n\n"
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
            f"No active BTC 15m Polymarket market found.\n\n"
            f"BTC: {format_btc_price(model['price'])}\n"
            f"Signal: {signal_emoji(model['signal'])} {model['signal']}"
        )

    return (
        f"{header('📈 LIVE BTC 15M MARKET', 'Polymarket only')}\n"
        f"🕒 {timestamp_with_seconds()}\n\n"
        f"{market.get('question', 'BTC 15m')}\n"
        f"Slug: <code>{market.get('slug', '')}</code>\n\n"
        f"🟢 UP price: <b>{market.get('up_price', 'n/a')}</b>\n"
        f"🔴 DOWN price: <b>{market.get('down_price', 'n/a')}</b>\n"
        f"💧 Liquidity: {market.get('liquidity', 0):,.0f}\n"
        f"📊 Volume: {market.get('volume', 0):,.0f}"
    )


async def build_strategy_text(user_id: int):
    market, model = await get_btc_context()
    maker = maker_snapshot(model, market)

    return (
        f"{header('🧠 STRATEGY LAB', '15m BTC model')}\n"
        f"🕒 {timestamp_with_seconds()}\n\n"
        f"<b>Directional model</b>\n"
        f"Signal: {signal_emoji(model['signal'])} {model['signal']}\n"
        f"Model probability: {model['model_prob'] * 100:.1f}%\n"
        f"Market probability: {model['market_prob'] * 100:.1f}%\n"
        f"Edge: {model['edge'] * 100:.1f} pts\n"
        f"Kelly-lite size: {model['suggested_size_pct']:.2f}%\n\n"
        f"<b>Math inputs</b>\n"
        f"RSI: {model.get('rsi', 0):.1f}\n"
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
        f"Verdict: {maker['verdict']}"
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
        f"Copy mode: preview only"
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
        "<b>Paper stats</b>",
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
        f"Total trades: {s['total']}",
        f"Open trades: {s['open']}",
        f"Closed trades: {s['closed']}",
        f"Total PnL: <b>${s['pnl']:.2f}</b>",
        f"Win rate: {s['win_rate'] * 100:.1f}%",
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

    if s["recent"]:
        lines.append("")
        lines.append("<b>Recent</b>")

        for row in s["recent"]:
            side, stake, entry_price, shares, edge, confidence, status, pnl, ev, result, created_at = row
            pnl_txt = f"${float(pnl or 0):.2f}" if status == "closed" else "open"
            result_txt = f" | {result}" if result else ""

            lines.append(
                f"• {side} ${float(stake):.2f} @ {float(entry_price):.3f} "
                f"| {float(shares):.2f} sh | EV ${float(ev or 0):.2f} | {pnl_txt}{result_txt}"
            )

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    touch_active_user(
        user_id,
        update.effective_user.username,
        update.effective_user.first_name,
    )

    set_user_setting(user_id, "alerts_chat_id", str(update.effective_chat.id))

    await update.message.reply_text(
        await build_home_text(user_id),
        reply_markup=main_menu(is_admin_user(user_id)),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def btc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    touch_active_user(
        user_id,
        update.effective_user.username,
        update.effective_user.first_name,
    )

    await update.message.reply_text(
        await build_btc_text(user_id),
        reply_markup=btc_menu(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def market_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    await update.message.reply_text(
        await build_market_text(user_id),
        reply_markup=btc_menu(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def bestbet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await btc_cmd(update, context)


async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    await update.message.reply_text(
        await build_strategy_text(user_id),
        reply_markup=btc_menu(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    await update.message.reply_text(
        await build_accuracy_text(user_id),
        reply_markup=main_menu(is_admin_user(user_id)),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def wallet_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    try:
        address = context.args[0]
        nickname = " ".join(context.args[1:]).strip()

        add_tracked_wallet(user_id, address, nickname)

        try:
            value = await fetch_wallet_total_value(address)
            log_wallet_snapshot(user_id, address, value)
            msg = f"Wallet added: {nickname or address}\nValue: ${value:.2f}"
        except Exception:
            msg = f"Wallet added: {nickname or address}\nValue snapshot unavailable."

        await update.message.reply_text(msg)

    except Exception:
        await update.message.reply_text("Usage: /wallet_add 0x... Nickname")


async def wallet_remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        remove_tracked_wallet(update.effective_user.id, context.args[0])
        await update.message.reply_text("Wallet removed.")
    except Exception:
        await update.message.reply_text("Usage: /wallet_remove 0x...")


async def wallet_name_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        update_wallet_nickname(
            update.effective_user.id,
            context.args[0],
            " ".join(context.args[1:]).strip(),
        )

        await update.message.reply_text("Wallet renamed.")

    except Exception:
        await update.message.reply_text("Usage: /wallet_name 0x... Nickname")


async def own_wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        address = context.args[0]
        set_own_wallet(update.effective_user.id, address)
        await update.message.reply_text("Own wallet set.")
    except Exception:
        await update.message.reply_text("Usage: /own_wallet 0x...")


async def wallet_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    await update.message.reply_text(
        await build_wallet_text(user_id),
        reply_markup=wallet_menu(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def alerts_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_setting(update.effective_user.id, "alerts_enabled", "1")
    await update.message.reply_text("Alerts enabled.")


async def alerts_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_setting(update.effective_user.id, "alerts_enabled", "0")
    await update.message.reply_text("Alerts disabled.")


async def set_edge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = float(context.args[0])
        set_user_setting(update.effective_user.id, "edge_threshold", str(value / 100.0))
        await update.message.reply_text(f"Edge threshold set to {value:.1f}%")
    except Exception:
        await update.message.reply_text("Usage: /set_edge 5")


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    await update.message.reply_text(
        await build_settings_text(user_id),
        reply_markup=settings_menu(is_admin_user(user_id)),
        parse_mode="HTML",
    )


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_admin_user(user_id):
        await update.message.reply_text("Admin only.")
        return

    await update.message.reply_text(
        build_admin_text(),
        reply_markup=admin_menu(),
        parse_mode="HTML",
    )


async def pnl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    summary = get_signal_summary(update.effective_user.id)

    await update.message.reply_text(
        f"{header('📊 SIGNAL LOG')}\n"
        f"Signals: {summary['count']}\n"
        f"Avg edge: {summary['avg_edge'] * 100:.2f} pts",
        parse_mode="HTML",
    )


async def paper_trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    market, model = await get_btc_context()

    size = 1.0

    try:
        if context.args:
            size = max(1.0, min(25.0, float(context.args[0])))
    except Exception:
        size = 1.0

    log_paper_trade(user_id, model, size)

    await update.message.reply_text(
        f"🧪 Manual paper trade logged\n"
        f"Signal: {model['signal']}\n"
        f"Size: ${size:.2f}\n"
        f"Edge: {model['edge'] * 100:.1f} pts"
    )


async def paper_auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    await update.message.reply_text(
        build_paper_auto_text(user_id),
        reply_markup=paper_auto_menu(),
        parse_mode="HTML",
    )


async def paper_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    set_paper_enabled(user_id, True)
    await update.message.reply_text("🤖 Auto paper trading started.")


async def paper_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    set_paper_enabled(user_id, False)
    await update.message.reply_text("⏸ Auto paper trading stopped.")


async def paper_reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reset_account(user_id)
    await update.message.reply_text("♻️ Paper account reset to $100.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    try:
        if query.data in ("home", "refresh"):
            await query.message.reply_text(
                await build_home_text(user_id),
                reply_markup=main_menu(is_admin_user(user_id)),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        elif query.data in ("btc", "bestbet"):
            await query.message.reply_text(
                await build_btc_text(user_id),
                reply_markup=btc_menu(),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        elif query.data == "market":
            await query.message.reply_text(
                await build_market_text(user_id),
                reply_markup=btc_menu(),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        elif query.data in ("strategy", "analyze"):
            await query.message.reply_text(
                await build_strategy_text(user_id),
                reply_markup=btc_menu(),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        elif query.data == "accuracy":
            await query.message.reply_text(
                await build_accuracy_text(user_id),
                reply_markup=main_menu(is_admin_user(user_id)),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        elif query.data == "wallets":
            await query.message.reply_text(
                await build_wallet_text(user_id),
                reply_markup=wallet_menu(),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        elif query.data == "alerts":
            await query.message.reply_text(
                build_alerts_text(user_id),
                reply_markup=alerts_menu(),
                parse_mode="HTML",
            )

        elif query.data == "settings":
            await query.message.reply_text(
                await build_settings_text(user_id),
                reply_markup=settings_menu(is_admin_user(user_id)),
                parse_mode="HTML",
            )

        elif query.data == "admin":
            if is_admin_user(user_id):
                await query.message.reply_text(
                    build_admin_text(),
                    reply_markup=admin_menu(),
                    parse_mode="HTML",
                )
            else:
                await query.message.reply_text("Admin only.")

        elif query.data == "paper_auto":
            await query.message.reply_text(
                build_paper_auto_text(user_id),
                reply_markup=paper_auto_menu(),
                parse_mode="HTML",
            )

        elif query.data == "paper_auto_start":
            set_paper_enabled(user_id, True)
            await query.message.reply_text("▶️ Auto paper trading started.", reply_markup=paper_auto_menu())

        elif query.data == "paper_auto_stop":
            set_paper_enabled(user_id, False)
            await query.message.reply_text("⏸ Auto paper trading stopped.", reply_markup=paper_auto_menu())

        elif query.data == "paper_auto_reset":
            reset_account(user_id)
            await query.message.reply_text("♻️ Paper account reset to $100.", reply_markup=paper_auto_menu())

        elif query.data == "paper_auto_balance":
            await query.message.reply_text(
                build_paper_auto_text(user_id),
                reply_markup=paper_auto_menu(),
                parse_mode="HTML",
            )

        elif query.data == "wallet_add_hint":
            await query.message.reply_text("Use /wallet_add 0x... Nickname")

        elif query.data == "wallet_remove_hint":
            await query.message.reply_text("Use /wallet_remove 0x...")

        elif query.data == "wallet_name_hint":
            await query.message.reply_text("Use /wallet_name 0x... Nickname")

        elif query.data == "own_wallet_hint":
            await query.message.reply_text("Use /own_wallet 0x...")

        elif query.data == "alerts_on":
            set_user_setting(user_id, "alerts_enabled", "1")
            await query.message.reply_text("Alerts enabled.", reply_markup=alerts_menu())

        elif query.data == "alerts_off":
            set_user_setting(user_id, "alerts_enabled", "0")
            await query.message.reply_text("Alerts disabled.", reply_markup=alerts_menu())

        elif query.data.startswith("edge_"):
            pct = float(query.data.split("_")[1])
            set_user_setting(user_id, "edge_threshold", str(pct / 100.0))
            await query.message.reply_text(f"Edge threshold set to {pct:.0f}%", reply_markup=alerts_menu())

        elif query.data == "notify_quiet":
            set_user_setting(user_id, "notify_mode", "quiet")
            await query.message.reply_text("Notify mode: Quiet", reply_markup=settings_menu(is_admin_user(user_id)))

        elif query.data == "notify_normal":
            set_user_setting(user_id, "notify_mode", "normal")
            await query.message.reply_text("Notify mode: Normal", reply_markup=settings_menu(is_admin_user(user_id)))

        elif query.data.startswith("view_"):
            mode = query.data.replace("view_", "")
            set_user_setting(user_id, "view_mode", mode)
            await query.message.reply_text(f"View mode: {mode.title()}", reply_markup=settings_menu(is_admin_user(user_id)))

        elif query.data == "clear_cache":
            cache.clear()
            await query.message.reply_text("Cache cleared.", reply_markup=admin_menu())

        elif query.data == "live_on":
            set_user_setting(0, "live_dashboards_enabled", "1")
            await query.message.reply_text("Live dashboards enabled.", reply_markup=admin_menu())

        elif query.data == "live_off":
            set_user_setting(0, "live_dashboards_enabled", "0")
            await query.message.reply_text("Live dashboards disabled.", reply_markup=admin_menu())

        elif query.data.startswith("dash_"):
            seconds = query.data.split("_")[1]
            set_user_setting(0, "dashboard_refresh_seconds", seconds)
            await query.message.reply_text(f"Dashboard refresh: {seconds}s", reply_markup=admin_menu())

        elif query.data.startswith("copy_"):
            await query.message.reply_text(
                "Copy-trade preview selected. Choose size:",
                reply_markup=copy_size_menu(query.data.replace("copy_", "")),
            )

        elif query.data.startswith("copy_size_"):
            amount = query.data.split("_")[-1]
            await query.message.reply_text(
                f"✅ Preview only: would copy this trade with ${amount}.\n"
                f"Real execution is disabled."
            )

        elif query.data.startswith("ignore_"):
            await query.message.reply_text("Ignored.")

        else:
            await query.message.reply_text("Unknown action.")

    except Exception as e:
        logging.exception("button handler failed")
        await query.message.reply_text(f"Handled error: {e}")


def register_handlers(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("btc", btc_cmd))
    app.add_handler(CommandHandler("market", market_cmd))
    app.add_handler(CommandHandler("bestbet", bestbet_cmd))
    app.add_handler(CommandHandler("analyze", analyze_cmd))
    app.add_handler(CommandHandler("strategy", analyze_cmd))
    app.add_handler(CommandHandler("refresh", refresh_cmd))

    app.add_handler(CommandHandler("wallet_add", wallet_add_cmd))
    app.add_handler(CommandHandler("wallet_remove", wallet_remove_cmd))
    app.add_handler(CommandHandler("wallet_name", wallet_name_cmd))
    app.add_handler(CommandHandler("wallet_list", wallet_list_cmd))
    app.add_handler(CommandHandler("own_wallet", own_wallet_cmd))

    app.add_handler(CommandHandler("pnl", pnl_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("accuracy", stats_cmd))

    app.add_handler(CommandHandler("paper", paper_trade_cmd))
    app.add_handler(CommandHandler("paper_auto", paper_auto_cmd))
    app.add_handler(CommandHandler("paper_start", paper_start_cmd))
    app.add_handler(CommandHandler("paper_stop", paper_stop_cmd))
    app.add_handler(CommandHandler("paper_reset", paper_reset_cmd))

    app.add_handler(CommandHandler("alerts_on", alerts_on_cmd))
    app.add_handler(CommandHandler("alerts_off", alerts_off_cmd))
    app.add_handler(CommandHandler("set_edge", set_edge_cmd))

    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))

    app.add_handler(CallbackQueryHandler(button_handler))
