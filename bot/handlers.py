import logging

from telegram import Update
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes

from bot.menus import (
    main_menu,
    btc_menu,
    settings_menu,
    paper_auto_menu,
)

from bot.btc import build_btc_model, format_btc_price
from bot.polymarket import discover_btc_15m_market
from bot.paper_auto import (
    set_paper_enabled,
    reset_account,
    paper_auto_summary,
)
from bot.time_utils import timestamp_with_seconds


def signal_emoji(signal: str):
    return "🟢" if signal == "UP" else "🔴"


async def get_market_and_model():
    market = await discover_btc_15m_market()
    model = await build_btc_model(market)
    return market, model


async def send(update: Update, text: str, reply_markup=None, parse_mode="HTML"):
    if update.callback_query:
        await update.callback_query.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )


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

    await send(update, text, reply_markup=main_menu())


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
        f"⏱ Time left: <b>{model['window']['left_label']}</b>"
    )

    await send(update, text, reply_markup=btc_menu())


async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    market, model = await get_market_and_model()

    text = (
        "🧠 <b>STRATEGY LAB</b>\n\n"
        f"Signal: <b>{signal_emoji(model['signal'])} {model['signal']}</b>\n"
        f"Model probability: <b>{model['model_prob'] * 100:.1f}%</b>\n"
        f"Market probability: <b>{model['market_prob'] * 100:.1f}%</b>\n"
        f"Edge: <b>{model['edge'] * 100:.1f} pts</b>\n"
        f"Confidence: <b>{model['confidence']}</b>\n"
        f"Phase: <b>{model['window']['phase']}</b>\n"
        f"Volatility: <b>{model.get('vol_regime', 'Unknown')}</b>"
    )

    await send(update, text, reply_markup=btc_menu())


async def market_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    market, model = await get_market_and_model()

    if not market:
        await send(update, "❌ No active BTC 15m Polymarket market found.")
        return

    text = (
        "📈 <b>LIVE BTC 15M MARKET</b>\n\n"
        f"{market.get('question', 'BTC 15m')}\n\n"
        f"🟢 UP: <b>{market.get('up_price', 'n/a')}</b>\n"
        f"🔴 DOWN: <b>{market.get('down_price', 'n/a')}</b>\n"
        f"💧 Liquidity: <b>{market.get('liquidity', 0)}</b>\n"
        f"📊 Volume: <b>{market.get('volume', 0)}</b>"
    )

    await send(update, text, reply_markup=btc_menu())


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = paper_auto_summary(update.effective_user.id)

    text = (
        "📊 <b>STATS</b>\n\n"
        f"Balance: <b>${s['balance']:.2f}</b>\n"
        f"Total trades: <b>{s['total']}</b>\n"
        f"Open trades: <b>{s['open']}</b>\n"
        f"Closed trades: <b>{s['closed']}</b>\n"
        f"Wins: <b>{s['wins']}</b>\n"
        f"Win rate: <b>{s['win_rate'] * 100:.1f}%</b>\n"
        f"PnL: <b>${s['pnl']:.2f}</b>"
    )

    await send(update, text, reply_markup=main_menu())


async def paper_auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = paper_auto_summary(update.effective_user.id)
    enabled = "ON" if s["enabled"] else "OFF"

    lines = [
        "🤖 <b>AUTO PAPER TRADING</b>",
        "<code>$100 virtual balance / BTC 15m</code>",
        f"🕒 {timestamp_with_seconds()}",
        "",
        f"Status: <b>{enabled}</b>",
        f"Balance: <b>${s['balance']:.2f}</b>",
        f"Total trades: <b>{s['total']}</b>",
        f"Open trades: <b>{s['open']}</b>",
        f"Closed trades: <b>{s['closed']}</b>",
        f"Total PnL: <b>${s['pnl']:.2f}</b>",
        f"Win rate: <b>{s['win_rate'] * 100:.1f}%</b>",
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
                side, stake, entry_price, shares, edge, confidence, status, pnl, ev, result, created_at = row
                pnl_txt = f"${float(pnl or 0):.2f}" if status == "closed" else "open"
                result_txt = f" | {result}" if result else ""

                lines.append(
                    f"• {side} ${float(stake):.2f} @ {float(entry_price):.3f} "
                    f"| {float(shares):.2f} sh | EV ${float(ev or 0):.2f} | {pnl_txt}{result_txt}"
                )
            except Exception:
                pass

    await send(update, "\n".join(lines), reply_markup=paper_auto_menu())


async def paper_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_paper_enabled(update.effective_user.id, True)
    await send(update, "▶️ Auto paper trading enabled.", reply_markup=paper_auto_menu())


async def paper_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_paper_enabled(update.effective_user.id, False)
    await send(update, "⏸ Auto paper trading disabled.", reply_markup=paper_auto_menu())


async def paper_reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_account(update.effective_user.id)
    await send(update, "♻️ Paper account reset to $100.", reply_markup=paper_auto_menu())


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚙️ <b>SETTINGS</b>\n\n"
        "• BTC 15m only\n"
        "• Binary Polymarket simulation\n"
        "• Paper trading only\n"
        "• Real execution disabled\n"
        "• No Binance dependency"
    )

    await send(update, text, reply_markup=settings_menu())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    try:
        if data in ["home", "refresh"]:
            await start(update, context)

        elif data in ["btc", "bestbet"]:
            await btc_cmd(update, context)

        elif data == "market":
            await market_cmd(update, context)

        elif data in ["strategy", "analyze"]:
            await analyze_cmd(update, context)

        elif data in ["stats", "accuracy"]:
            await stats_cmd(update, context)

        elif data == "paper_auto":
            await paper_auto_cmd(update, context)

        elif data in ["paper_auto_start", "paper_start"]:
            await paper_start_cmd(update, context)

        elif data in ["paper_auto_stop", "paper_stop"]:
            await paper_stop_cmd(update, context)

        elif data in ["paper_auto_reset", "paper_reset"]:
            await paper_reset_cmd(update, context)

        elif data in ["paper_auto_balance", "paper_balance"]:
            await paper_auto_cmd(update, context)

        elif data == "settings":
            await settings_cmd(update, context)

        elif data == "wallets":
            await send(update, "👛 Wallet tracker is currently not active in this clean build.")

        elif data == "alerts":
            await send(update, "🔔 Alerts are handled by the background jobs.")

        else:
            await send(update, f"Unknown button: {data}")

    except Exception as e:
        logging.exception("button handler error")
        await query.message.reply_text(f"Error: {e}")


def register_handlers(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("btc", btc_cmd))
    app.add_handler(CommandHandler("market", market_cmd))
    app.add_handler(CommandHandler("bestbet", btc_cmd))
    app.add_handler(CommandHandler("analyze", analyze_cmd))
    app.add_handler(CommandHandler("strategy", analyze_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("paper", paper_auto_cmd))
    app.add_handler(CommandHandler("paper_auto", paper_auto_cmd))
    app.add_handler(CommandHandler("paper_start", paper_start_cmd))
    app.add_handler(CommandHandler("paper_stop", paper_stop_cmd))
    app.add_handler(CommandHandler("paper_reset", paper_reset_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))

    app.add_handler(CallbackQueryHandler(button_handler))
