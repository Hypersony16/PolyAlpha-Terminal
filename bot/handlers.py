import io
import logging
from typing import Optional

from telegram import Update, InputFile
from telegram.ext import CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

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
from bot.polymarket import discover_btc_15m_market, fetch_public_profile, fetch_market_resolution
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
    resolve_open_trades,
    due_open_market_slugs,
    set_paper_enabled,
    reset_account,
    paper_auto_summary,
    set_max_bet,
    get_max_bet,
    get_last_skip_reason,
    set_real_odds_only,
)
from bot.time_utils import timestamp_with_seconds
from bot.analytics_export import export_user_analytics, import_user_analytics, analytics_text
from bot.scalp import get_strategy_mode, set_strategy_mode, scalp_analytics
from bot.performance import strategy_breakdown_text
from bot.alpha_handlers import register_alpha_handlers


# ---------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------

def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


async def _touch(update: Update):
    user = update.effective_user
    if user:
        touch_active_user(user.id, user.username or "", user.first_name or "")


async def _safe_edit(message, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode,
                                 disable_web_page_preview=True)
    except Exception:
        pass


# ---------------------------------------------------------------------
# Home / start
# ---------------------------------------------------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    user = update.effective_user
    name = user.first_name if user else "trader"
    is_admin = _is_admin(user.id) if user else False
    text = (
        f"👋 <b>Welcome, {name}!</b>\n\n"
        "PolyScalpBot — Polymarket BTC 15m scalping assistant.\n\n"
        "Use the menu below to navigate."
    )
    await update.message.reply_text(text, reply_markup=main_menu(is_admin), parse_mode="HTML")


async def home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    is_admin = _is_admin(user.id) if user else False
    text = "🏠 <b>PolyScalpBot</b>\n\nChoose an option:"
    await _safe_edit(query.message, text, main_menu(is_admin))


# ---------------------------------------------------------------------
# BTC / Market
# ---------------------------------------------------------------------

async def btc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    query = update.callback_query
    await query.answer()
    try:
        market = await discover_btc_15m_market()
        model = await build_btc_model(market)
    except Exception as e:
        await _safe_edit(query.message, f"⚠️ Error fetching BTC data: {e}", btc_menu())
        return

    price = model.get("price", 0)
    signal = model.get("signal", "?")
    edge = model.get("edge", 0)
    confidence = model.get("confidence", "?")
    up_price = model.get("up_price", 0)
    down_price = model.get("down_price", 0)
    time_left = model.get("time_left_seconds", 0)
    phase = model.get("phase", "?")
    rsi = model.get("rsi")
    ema9 = model.get("ema9")
    ema21 = model.get("ema21")

    signal_emoji = "🟢" if signal == "UP" else "🔴" if signal == "DOWN" else "⚪"
    text = (
        f"₿ <b>BTC 15m Signal</b>\n\n"
        f"Price: <b>{format_btc_price(price)}</b>\n"
        f"Signal: {signal_emoji} <b>{signal}</b>\n"
        f"Edge: <b>{edge:.1%}</b> | Confidence: <b>{confidence}</b>\n\n"
        f"UP: {up_price:.3f} | DOWN: {down_price:.3f}\n"
        f"Time left: {time_left // 60}m {time_left % 60}s | Phase: {phase}\n"
    )
    if rsi is not None:
        text += f"RSI: {rsi:.1f}"
    if ema9 is not None and ema21 is not None:
        text += f" | EMA9: {ema9:.0f} | EMA21: {ema21:.0f}"
    text += f"\n\n<i>{timestamp_with_seconds()}</i>"

    await _safe_edit(query.message, text, btc_menu())


async def market_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    query = update.callback_query
    await query.answer()
    try:
        market = await discover_btc_15m_market()
    except Exception as e:
        await _safe_edit(query.message, f"⚠️ Error: {e}", main_menu())
        return

    slug = market.get("slug", "N/A")
    question = market.get("question", "N/A")
    up_price = market.get("up_price", 0)
    down_price = market.get("down_price", 0)
    time_left = market.get("time_left_seconds", 0)
    liquidity = market.get("liquidity", 0)

    text = (
        f"📈 <b>Polymarket BTC 15m</b>\n\n"
        f"<i>{question}</i>\n\n"
        f"UP: <b>{up_price:.3f}</b> | DOWN: <b>{down_price:.3f}</b>\n"
        f"Time left: {time_left // 60}m {time_left % 60}s\n"
        f"Liquidity: ${liquidity:,.0f}\n"
        f"Slug: <code>{slug}</code>\n\n"
        f"<i>{timestamp_with_seconds()}</i>"
    )
    await _safe_edit(query.message, text, btc_menu())


# ---------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------

async def strategy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = user.id if user else 0

    try:
        market = await discover_btc_15m_market()
        model = await build_btc_model(market)
    except Exception as e:
        await _safe_edit(query.message, f"⚠️ Error: {e}", main_menu())
        return

    snap = maker_snapshot(model, market)
    mode = get_strategy_mode(user_id)
    skip_reason = get_last_skip_reason(user_id)

    text = (
        f"🧠 <b>Strategy</b>\n\n"
        f"Mode: <b>{mode}</b>\n"
        f"Signal: <b>{model.get('signal', '?')}</b> | Edge: <b>{model.get('edge', 0):.1%}</b>\n"
        f"Confidence: <b>{model.get('confidence', '?')}</b>\n\n"
        f"Maker YES bid: {snap['yes_bid']:.3f} | NO bid: {snap['no_bid']:.3f}\n"
        f"Combined: {snap['combined']:.3f} | Merge edge: {snap['merge_edge']:.2f}%\n"
        f"Verdict: <b>{snap['verdict']}</b> | Risk: {snap['risk']}\n"
    )
    if skip_reason:
        text += f"\nLast skip: <i>{skip_reason}</i>"
    text += f"\n\n<i>{timestamp_with_seconds()}</i>"

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Resolution", callback_data="mode_resolution"),
         InlineKeyboardButton("Scalp", callback_data="mode_scalp"),
         InlineKeyboardButton("Hybrid", callback_data="mode_hybrid")],
        [InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ])
    await _safe_edit(query.message, text, kb)


async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = user.id if user else 0
    mode = query.data.replace("mode_", "")
    set_strategy_mode(user_id, mode)
    await strategy_callback(update, context)


# ---------------------------------------------------------------------
# Stats / Accuracy
# ---------------------------------------------------------------------

async def accuracy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = user.id if user else 0

    acc = prediction_accuracy(user_id, hours=24)
    lat = latency_summary(hours=1)
    ps = paper_summary(user_id)

    text = (
        f"📊 <b>Stats (24h)</b>\n\n"
        f"Predictions: {acc['total']} | Wins: {acc['wins']} | Losses: {acc['losses']}\n"
        f"Accuracy: <b>{acc['accuracy']:.1f}%</b>\n\n"
        f"Paper trades: {ps['total']} | Staked: ${ps['total_size']:.2f}\n\n"
    )
    if lat:
        text += "<b>Latency (1h):</b>\n"
        for src, d in lat.items():
            text += f"  {src}: {d['avg_ms']:.0f}ms avg | {d['ok_rate']:.0f}% ok\n"

    text += f"\n<i>{timestamp_with_seconds()}</i>"

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="accuracy")],
        [InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ])
    await _safe_edit(query.message, text, kb)


# ---------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------

async def analytics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = user.id if user else 0

    text = analytics_text(user_id)
    text += f"\n\n<i>{timestamp_with_seconds()}</i>"

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Export", callback_data="analytics_export")],
        [InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ])
    await _safe_edit(query.message, text, kb)


async def analytics_export_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = user.id if user else 0

    try:
        filename, content = export_user_analytics(user_id)
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=InputFile(io.BytesIO(content), filename=filename),
            caption="📤 Analytics export",
        )
    except Exception as e:
        await query.message.reply_text(f"⚠️ Export failed: {e}")


# ---------------------------------------------------------------------
# Paper Auto
# ---------------------------------------------------------------------

async def paper_auto_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = user.id if user else 0

    summary = paper_auto_summary(user_id)
    skip = get_last_skip_reason(user_id)
    max_bet = get_max_bet(user_id)

    text = (
        f"🤖 <b>Paper Auto Trading</b>\n\n"
        f"Balance: <b>${summary['balance']:.2f}</b>\n"
        f"Trades: {summary['total_trades']} | Open: {summary['open_trades']}\n"
        f"Closed: {summary['closed_trades']} | Wins: {summary['wins']} | Losses: {summary['losses']}\n"
        f"Win rate: <b>{summary['winrate']:.1f}%</b>\n"
        f"Total PnL: <b>${summary['total_pnl']:.2f}</b> | ROI: {summary['roi']:.1f}%\n"
        f"Max bet: ${max_bet:.2f}\n"
    )
    if skip:
        text += f"\nLast skip: <i>{skip}</i>"
    text += f"\n\n<i>{timestamp_with_seconds()}</i>"

    await _safe_edit(query.message, text, paper_auto_menu())


async def paper_enable_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = user.id if user else 0
    set_paper_enabled(user_id, True)
    await paper_auto_callback(update, context)


async def paper_disable_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = user.id if user else 0
    set_paper_enabled(user_id, False)
    await paper_auto_callback(update, context)


async def paper_reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = user.id if user else 0
    reset_account(user_id)
    await paper_auto_callback(update, context)


async def paper_summary_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await paper_auto_callback(update, context)


async def performance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = user.id if user else 0

    text = strategy_breakdown_text(user_id)
    text += f"\n\n<i>{timestamp_with_seconds()}</i>"

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="performance")],
        [InlineKeyboardButton("⬅️ Paper Auto", callback_data="paper_auto")],
    ])
    await _safe_edit(query.message, text, kb)


# ---------------------------------------------------------------------
# Wallets
# ---------------------------------------------------------------------

async def wallets_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = user.id if user else 0

    wallets = get_tracked_wallets(user_id)
    own = get_own_wallet(user_id)

    lines = ["👛 <b>Tracked Wallets</b>"]
    if own:
        lines.append(f"\n⭐ Own: <code>{own[:10]}…</code>")
    if wallets:
        for i, (w, nick) in enumerate(wallets, 1):
            label = f" — {nick}" if nick else ""
            lines.append(f"{i}. <code>{w[:10]}…</code>{label}")
    else:
        lines.append("\nNo wallets tracked yet.")
        lines.append("Use /addwallet 0xWallet to add one.")

    text = "\n".join(lines)
    await _safe_edit(query.message, text, wallet_menu())


async def wallet_add_hint_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Use: /addwallet 0xWalletAddress optional_nickname")


async def wallet_remove_hint_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Use: /removewallet 0xWalletAddress")


async def wallet_name_hint_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Use: /namewallet 0xWalletAddress NewName")


async def own_wallet_hint_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Use: /setwallet 0xYourWalletAddress")


async def addwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    user = update.effective_user
    user_id = user.id if user else 0
    if not context.args:
        await update.message.reply_text("Usage: /addwallet 0xWallet optional_nickname")
        return
    wallet = context.args[0].strip().lower()
    nickname = " ".join(context.args[1:]).strip()
    if not wallet.startswith("0x") or len(wallet) < 10:
        await update.message.reply_text("Invalid wallet address.")
        return
    add_tracked_wallet(user_id, wallet, nickname)
    await update.message.reply_text(f"✅ Added wallet {wallet[:10]}…" + (f" ({nickname})" if nickname else ""))


async def removewallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    user = update.effective_user
    user_id = user.id if user else 0
    if not context.args:
        await update.message.reply_text("Usage: /removewallet 0xWallet")
        return
    wallet = context.args[0].strip().lower()
    remove_tracked_wallet(user_id, wallet)
    await update.message.reply_text(f"✅ Removed wallet {wallet[:10]}…")


async def namewallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    user = update.effective_user
    user_id = user.id if user else 0
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /namewallet 0xWallet NewName")
        return
    wallet = context.args[0].strip().lower()
    name = " ".join(context.args[1:]).strip()
    update_wallet_nickname(user_id, wallet, name)
    await update.message.reply_text(f"✅ Renamed wallet {wallet[:10]}… to {name}")


async def setwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    user = update.effective_user
    user_id = user.id if user else 0
    if not context.args:
        current = get_own_wallet(user_id)
        await update.message.reply_text(f"Your wallet: {current or 'not set'}\nUsage: /setwallet 0xWallet")
        return
    wallet = context.args[0].strip().lower()
    set_own_wallet(user_id, wallet)
    await update.message.reply_text(f"✅ Own wallet set to {wallet[:10]}…")


# ---------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------

async def alerts_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = user.id if user else 0

    enabled = get_user_setting(user_id, "alerts_enabled", "1") == "1"
    text = (
        f"🔔 <b>Alerts</b>\n\n"
        f"Status: {'✅ Enabled' if enabled else '❌ Disabled'}\n\n"
        "Alerts fire when edge > 12% and confidence is Medium or High."
    )
    await _safe_edit(query.message, text, alerts_menu())


async def alerts_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = user.id if user else 0
    current = get_user_setting(user_id, "alerts_enabled", "1") == "1"
    set_user_setting(user_id, "alerts_enabled", "0" if current else "1")
    await alerts_callback(update, context)


# ---------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = user.id if user else 0

    from bot.dashboard import live_dashboards_enabled, dashboard_refresh_seconds
    live = live_dashboards_enabled()
    refresh = dashboard_refresh_seconds()

    text = (
        f"⚙️ <b>Settings</b>\n\n"
        f"Live dashboards: {'✅' if live else '❌'}\n"
        f"Dashboard refresh: {refresh}s\n"
    )
    await _safe_edit(query.message, text, settings_menu())


async def settings_live_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    from bot.dashboard import live_dashboards_enabled, set_live_dashboards_enabled
    current = live_dashboards_enabled()
    set_live_dashboards_enabled(not current)
    await settings_callback(update, context)


async def settings_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Use: /setrefresh <seconds> (min 5)")


async def setrefresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setrefresh <seconds>")
        return
    try:
        seconds = max(5, int(context.args[0]))
        set_user_setting(0, "dashboard_refresh_seconds", str(seconds))
        await update.message.reply_text(f"✅ Dashboard refresh set to {seconds}s")
    except ValueError:
        await update.message.reply_text("Invalid number.")


# ---------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not user or not _is_admin(user.id):
        await query.answer("Not authorized.", show_alert=True)
        return

    users = get_active_users()
    text = f"🛠 <b>Admin</b>\n\nActive users: {len(users)}"
    await _safe_edit(query.message, text, admin_menu())


async def admin_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not user or not _is_admin(user.id):
        return
    users = get_active_users()
    text = f"👥 <b>Active Users</b>\n\n" + "\n".join(str(u) for u in users[:50])
    await _safe_edit(query.message, text, admin_menu())


async def admin_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not user or not _is_admin(user.id):
        return
    lat = latency_summary(hours=24)
    lines = ["📊 <b>Admin Stats (24h)</b>"]
    for src, d in lat.items():
        lines.append(f"{src}: {d['avg_ms']:.0f}ms | {d['ok_rate']:.0f}% ok | {d['count']} calls")
    await _safe_edit(query.message, "\n".join(lines), admin_menu())


# ---------------------------------------------------------------------
# Message handler for file imports
# ---------------------------------------------------------------------

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _touch(update)
    user = update.effective_user
    user_id = user.id if user else 0
    doc = update.message.document
    if not doc or not doc.file_name:
        return
    if not doc.file_name.endswith(".json"):
        return
    try:
        file = await context.bot.get_file(doc.file_id)
        raw = await file.download_as_bytearray()
        result = import_user_analytics(user_id, bytes(raw))
        await update.message.reply_text(f"✅ Import complete: {result}")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Import failed: {e}")


# ---------------------------------------------------------------------
# Register all handlers
# ---------------------------------------------------------------------

def register_handlers(app):
    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("addwallet", addwallet_cmd))
    app.add_handler(CommandHandler("removewallet", removewallet_cmd))
    app.add_handler(CommandHandler("namewallet", namewallet_cmd))
    app.add_handler(CommandHandler("setwallet", setwallet_cmd))
    app.add_handler(CommandHandler("setrefresh", setrefresh_cmd))

    # Callback queries
    app.add_handler(CallbackQueryHandler(home_callback, pattern="^home$"))
    app.add_handler(CallbackQueryHandler(btc_callback, pattern="^btc$"))
    app.add_handler(CallbackQueryHandler(market_callback, pattern="^market$"))
    app.add_handler(CallbackQueryHandler(strategy_callback, pattern="^strategy$"))
    app.add_handler(CallbackQueryHandler(mode_callback, pattern="^mode_"))
    app.add_handler(CallbackQueryHandler(accuracy_callback, pattern="^accuracy$"))
    app.add_handler(CallbackQueryHandler(analytics_callback, pattern="^analytics$"))
    app.add_handler(CallbackQueryHandler(analytics_export_callback, pattern="^analytics_export$"))
    app.add_handler(CallbackQueryHandler(paper_auto_callback, pattern="^paper_auto$"))
    app.add_handler(CallbackQueryHandler(paper_enable_callback, pattern="^paper_enable$"))
    app.add_handler(CallbackQueryHandler(paper_disable_callback, pattern="^paper_disable$"))
    app.add_handler(CallbackQueryHandler(paper_reset_callback, pattern="^paper_reset$"))
    app.add_handler(CallbackQueryHandler(paper_summary_callback, pattern="^paper_summary$"))
    app.add_handler(CallbackQueryHandler(performance_callback, pattern="^performance$"))
    app.add_handler(CallbackQueryHandler(wallets_callback, pattern="^wallets$"))
    app.add_handler(CallbackQueryHandler(wallet_add_hint_callback, pattern="^wallet_add_hint$"))
    app.add_handler(CallbackQueryHandler(wallet_remove_hint_callback, pattern="^wallet_remove_hint$"))
    app.add_handler(CallbackQueryHandler(wallet_name_hint_callback, pattern="^wallet_name_hint$"))
    app.add_handler(CallbackQueryHandler(own_wallet_hint_callback, pattern="^own_wallet_hint$"))
    app.add_handler(CallbackQueryHandler(alerts_callback, pattern="^alerts$"))
    app.add_handler(CallbackQueryHandler(alerts_toggle_callback, pattern="^alerts_toggle$"))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^settings$"))
    app.add_handler(CallbackQueryHandler(settings_live_callback, pattern="^settings_live$"))
    app.add_handler(CallbackQueryHandler(settings_refresh_callback, pattern="^settings_refresh$"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin$"))
    app.add_handler(CallbackQueryHandler(admin_users_callback, pattern="^admin_users$"))
    app.add_handler(CallbackQueryHandler(admin_stats_callback, pattern="^admin_stats$"))

    # Document handler for imports
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))

    # Alpha handlers integration
    register_alpha_handlers(app)
