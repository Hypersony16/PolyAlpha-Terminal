import logging

from telegram import Update
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from bot.menus import (
    main_menu,
    btc_menu,
    settings_menu,
    paper_auto_menu,
)

from bot.btc import (
    build_btc_model,
    format_btc_price,
)

from bot.polymarket import (
    discover_btc_15m_market,
)

from bot.paper_auto import (
    set_paper_enabled,
    reset_account,
    paper_auto_summary,
)

from bot.time_utils import (
    timestamp_with_seconds,
)


# =========================================
# HELPERS
# =========================================

def signal_emoji(signal: str):
    return "🟢" if signal == "UP" else "🔴"


async def get_market_and_model():
    market = await discover_btc_15m_market()
    model = await build_btc_model(market)
    return market, model


# =========================================
# HOME
# =========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    market, model = await get_market_and_model()

    text = (
        "🚀 <b>POLYSCALPBOT</b>\n"
        "BTC 15m Prediction Engine\n\n"

        f"💰 BTC: <b>{format_btc_price(model['price'])}</b>\n"
        f"🎯 Signal: <b>{signal_emoji(model['signal'])} {model['signal']}</b>\n"
        f"⚡ Edge: <b>+{model['edge'] * 100:.1f} pts</b>\n"
        f"🧠 Confidence: <b>{model['confidence']}</b>\n"
        f"⏱ Time left: <b>{model['window']['left_label']}</b>\n\n"

        f"🕒 {timestamp_with_seconds()}"
    )

    await update.message.reply_text(
        text,
        reply_markup=main_menu(),
        parse_mode="HTML",
    )


# =========================================
# BTC
# =========================================

async def btc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    market, model = await get_market_and_model()

    text = (
        "₿ <b>BTC 15M SIGNAL</b>\n\n"

        f"💰 Price: <b>{format_btc_price(model['price'])}</b>\n"
        f"🎯 Signal: <b>{signal_emoji(model['signal'])} {model['signal']}</b>\n"
        f"🧠 Confidence: <b>{model['confidence']}</b>\n"
        f"⚡ Edge: <b>+{model['edge'] * 100:.1f} pts</b>\n"
        f"📊 Model: <b>{model['model_prob'] * 100:.1f}%</b>\n"
        f"📈 Market: <b>{model['market_prob'] * 100:.1f}%</b>\n"
        f"💼 Size: <b>{model['suggested_size_pct']:.2f}%</b>\n"
        f"⏱ Time left: <b>{model['window']['left_label']}</b>\n"
    )

    await update.message.reply_text(
        text,
        reply_markup=btc_menu(),
        parse_mode="HTML",
    )


# =========================================
# ANALYZE
# =========================================

async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await btc_cmd(update, context)


# =========================================
# MARKET
# =========================================

async def market_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    market, model = await get_market_and_model()

    if not market:

        await update.message.reply_text(
            "❌ No active BTC 15m market found."
        )

        return

    text = (
        "📈 <b>LIVE MARKET</b>\n\n"

        f"{market.get('question', 'BTC 15m')}\n\n"

        f"🟢 UP: <b>{market.get('up_price', 0)}</b>\n"
        f"🔴 DOWN: <b>{market.get('down_price', 0)}</b>\n"
        f"💧 Liquidity: <b>{market.get('liquidity', 0)}</b>\n"
        f"📊 Volume: <b>{market.get('volume', 0)}</b>\n"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )


# =========================================
# SETTINGS
# =========================================

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = (
        "⚙️ <b>SETTINGS</b>\n\n"

        "• BTC 15m only\n"
        "• Binary market model\n"
        "• Paper trading enabled\n"
        "• Real execution disabled\n"
    )

    await update.message.reply_text(
        text,
        reply_markup=settings_menu(),
        parse_mode="HTML",
    )


# =========================================
# STATS
# =========================================

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    summary = paper_auto_summary(update.effective_user.id)

    text = (
        "📊 <b>STATS</b>\n\n"

        f"💰 Balance: <b>${summary['balance']:.2f}</b>\n"
        f"📈 Trades: <b>{summary['total']}</b>\n"
        f"🏆 Wins: <b>{summary['wins']}</b>\n"
        f"📉 Winrate: <b>{summary['win_rate'] * 100:.1f}%</b>\n"
        f"💵 PnL: <b>${summary['pnl']:.2f}</b>\n"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )


# =========================================
# PAPER AUTO
# =========================================

async def paper_auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    summary = paper_auto_summary(update.effective_user.id)

    enabled = "ON" if summary["enabled"] else "OFF"

    text = (
        "🤖 <b>AUTO PAPER TRADING</b>\n\n"

        f"Status: <b>{enabled}</b>\n"
        f"💰 Balance: <b>${summary['balance']:.2f}</b>\n"
        f"📈 Trades: <b>{summary['total']}</b>\n"
        f"🏆 Wins: <b>{summary['wins']}</b>\n"
        f"📉 Winrate: <b>{summary['win_rate'] * 100:.1f}%</b>\n"
        f"💵 PnL: <b>${summary['pnl']:.2f}</b>\n\n"

        "Rules\n"
        "• Binary payout simulation\n"
        "• Correct side pays $1/share\n"
        "• Max 1 trade per 15m market\n"
        "• Max position size $5\n"
        "• 0.5% slippage\n"
    )

    await update.message.reply_text(
        text,
        reply_markup=paper_auto_menu(),
        parse_mode="HTML",
    )


async def paper_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    set_paper_enabled(update.effective_user.id, True)

    await update.message.reply_text(
        "▶️ Auto paper trading enabled."
    )


async def paper_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    set_paper_enabled(update.effective_user.id, False)

    await update.message.reply_text(
        "⏸ Auto paper trading disabled."
    )


async def paper_reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    reset_account(update.effective_user.id)

    await update.message.reply_text(
        "♻️ Paper account reset to $100."
    )


# =========================================
# BUTTONS
# =========================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    data = query.data

    try:

        if data == "home":
            await start(update, context)

        elif data == "btc":
            await btc_cmd(update, context)

        elif data == "market":
            await market_cmd(update, context)

        elif data == "strategy":
            await analyze_cmd(update, context)

        elif data == "settings":
            await settings_cmd(update, context)

        elif data == "stats":
            await stats_cmd(update, context)

        elif data == "paper_auto":
            await paper_auto_cmd(update, context)

        elif data == "paper_auto_start":
            set_paper_enabled(update.effective_user.id, True)

            await query.message.reply_text(
                "▶️ Auto paper trading enabled."
            )

        elif data == "paper_auto_stop":
            set_paper_enabled(update.effective_user.id, False)

            await query.message.reply_text(
                "⏸ Auto paper trading disabled."
            )

        elif data == "paper_auto_reset":
            reset_account(update.effective_user.id)

            await query.message.reply_text(
                "♻️ Paper account reset."
            )

    except Exception as e:

        logging.exception(e)

        await query.message.reply_text(
            f"Error: {e}"
        )


# =========================================
# REGISTER
# =========================================

def register_handlers(app):

    app.add_handler(CommandHandler("start", start))

    app.add_handler(CommandHandler("btc", btc_cmd))
    app.add_handler(CommandHandler("analyze", analyze_cmd))
    app.add_handler(CommandHandler("market", market_cmd))

    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))

    app.add_handler(CommandHandler("paper_auto", paper_auto_cmd))
    app.add_handler(CommandHandler("paper_start", paper_start_cmd))
    app.add_handler(CommandHandler("paper_stop", paper_stop_cmd))
    app.add_handler(CommandHandler("paper_reset", paper_reset_cmd))

    app.add_handler(CallbackQueryHandler(button_handler))
