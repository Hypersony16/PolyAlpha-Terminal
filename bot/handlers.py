from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from bot.menus import main_menu
from bot.btc import build_btc_model
from bot.polymarket import get_active_btc_market
from bot.paper_auto import (
    get_paper_status,
    toggle_paper_auto,
    reset_paper_balance,
)
from bot.db import get_db


# =========================
# START / HOME
# =========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🚀 PolyScalpBot\n"
        "Advanced BTC 15m Polymarket scalping system\n\n"
        "Features:\n"
        "• BTC 15m analysis\n"
        "• Edge detection\n"
        "• Paper auto trading\n"
        "• Live statistics\n"
        "• Risk engine\n"
        "• Market phase model\n"
    )

    await update.message.reply_text(
        text,
        reply_markup=main_menu()
    )


# =========================
# BTC ANALYSIS
# =========================

async def btc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    market = await get_active_btc_market()
    model = await build_btc_model(market)

    text = (
        f"₿ BTC 15M EDGE ALERT\n"
        f"🕒 {model['timestamp']}\n\n"
        f"BTC: ${model['btc_price']:,.2f}\n\n"
        f"Signal: {'🟢 UP' if model['signal']=='UP' else '🔴 DOWN'}\n"
        f"Model: {model['model_prob']*100:.1f}%\n"
        f"Market: {model['market_prob']*100:.1f}%\n"
        f"Edge: +{model['edge']*100:.1f} pts\n"
        f"Confidence: {model['confidence']}\n"
        f"Time left: {model['time_left']}\n"
        f"Size: {model['size_pct']:.2f}% bankroll"
    )

    await update.message.reply_text(text)


async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await btc_cmd(update, context)


async def bestbet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await btc_cmd(update, context)


async def market_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    market = await get_active_btc_market()

    text = (
        "📈 ACTIVE MARKET\n\n"
        f"Question:\n{market.get('question','N/A')}\n\n"
        f"Volume: ${market.get('volume',0)}\n"
        f"Liquidity: ${market.get('liquidity',0)}\n"
        f"End: {market.get('endDate','N/A')}"
    )

    await update.message.reply_text(text)


# =========================
# PAPER AUTO
# =========================

async def paper_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await paper_auto_cmd(update, context)


async def paper_auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    s = get_paper_status()

    lines = [
        "🤖 AUTO PAPER TRADING",
        "$100 virtual balance / BTC 15m only",
        f"🕒 {s['time']}",
        "",
        f"Status: {'ON' if s['enabled'] else 'OFF'}",
        f"Balance: ${s['balance']:.2f}",
        f"Total trades: {s['total_trades']}",
        f"Open trades: {s['open_trades']}",
        f"Closed trades: {s['closed_trades']}",
        f"Total PnL: ${s['pnl']:.2f}",
        f"Win rate: {s['win_rate']:.1f}%",
        "",
        "Rules",
        "• Paper only, no real money",
        "• Polymarket binary payout simulation",
        "• Correct side pays $1/share",
        "• Max 1 trade per 15m market",
        "• Entry only in Prime/Late phase",
        "• Min edge 6%",
        "• Max position $5",
        "• 0.5% slippage assumption",
        "",
        "Recent",
    ]

    for row in s["recent"]:
        side, stake, entry_price, shares, edge, confidence, status, pnl, ev, result, created_at = row

        pnl_txt = (
            f"${float(pnl or 0):.2f}"
            if status == "closed"
            else "open"
        )

        result_txt = f" | {result}" if result else ""

        lines.append(
            f"• {side} ${float(stake):.2f} @ {float(entry_price):.3f} "
            f"| {float(shares):.2f} sh "
            f"| EV ${float(ev or 0):.2f} "
            f"| {pnl_txt}{result_txt}"
        )

    keyboard = [
        [
            InlineKeyboardButton("▶️ Start", callback_data="paper_start"),
            InlineKeyboardButton("⏸ Stop", callback_data="paper_stop"),
        ],
        [
            InlineKeyboardButton("📊 Balance", callback_data="paper_balance"),
            InlineKeyboardButton("♻️ Reset $100", callback_data="paper_reset"),
        ],
        [
            InlineKeyboardButton("⬅️ Home", callback_data="home"),
        ]
    ]

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def paper_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await paper_auto_cmd(update, context)


async def paper_reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    reset_paper_balance()

    await update.message.reply_text(
        "♻️ Paper balance reset to $100"
    )


# =========================
# WALLET
# =========================

async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "👛 Wallet tracking coming soon"
    )


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    s = get_paper_status()

    await update.message.reply_text(
        f"💰 Current paper balance: ${s['balance']:.2f}"
    )


# =========================
# SETTINGS
# =========================

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "⚙️ Settings panel coming soon"
    )


# =========================
# CALLBACKS
# =========================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if query.data == "home":

        await query.message.reply_text(
            "🏠 Main Menu",
            reply_markup=main_menu()
        )

    elif query.data == "paper_start":

        toggle_paper_auto(True)

        await query.message.reply_text(
            "✅ Paper auto trading ENABLED"
        )

    elif query.data == "paper_stop":

        toggle_paper_auto(False)

        await query.message.reply_text(
            "⛔ Paper auto trading DISABLED"
        )

    elif query.data == "paper_reset":

        reset_paper_balance()

        await query.message.reply_text(
            "♻️ Reset paper balance to $100"
        )

    elif query.data == "paper_balance":

        s = get_paper_status()

        await query.message.reply_text(
            f"💰 Balance: ${s['balance']:.2f}"
        )


# =========================
# TEXT ROUTER
# =========================

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text.lower()

    if text in ["btc", "/btc"]:
        await btc_cmd(update, context)

    elif text in ["analyze", "/analyze"]:
        await analyze_cmd(update, context)

    elif text in ["paper", "/paper"]:
        await paper_auto_cmd(update, context)

    else:
        await update.message.reply_text(
            "Unknown command. Use /start"
        )


# =========================
# REGISTER
# =========================

def register_handlers(app):

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("menu", start_cmd))
    app.add_handler(CommandHandler("home", start_cmd))

    app.add_handler(CommandHandler("btc", btc_cmd))
    app.add_handler(CommandHandler("analyze", analyze_cmd))
    app.add_handler(CommandHandler("bestbet", bestbet_cmd))
    app.add_handler(CommandHandler("market", market_cmd))

    app.add_handler(CommandHandler("paper", paper_cmd))
    app.add_handler(CommandHandler("paperauto", paper_auto_cmd))
    app.add_handler(CommandHandler("paperstats", paper_stats_cmd))
    app.add_handler(CommandHandler("paperreset", paper_reset_cmd))

    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))

    app.add_handler(CommandHandler("settings", settings_cmd))

    app.add_handler(CallbackQueryHandler(button_callback))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router
        )
    )

    print("✅ handlers loaded")
