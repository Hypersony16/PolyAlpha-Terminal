"""Telegram commands for PolyAlpha Terminal.

Drop into bot/ and call register_alpha_handlers(app) from your existing register_handlers(app).
"""
from __future__ import annotations

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.alpha_store import (
    add_alpha_wallet,
    ensure_alpha_tables,
    get_alpha_setting,
    list_alpha_wallets,
    remove_alpha_wallet,
    set_alpha_setting,
)
from bot.smart_money import SmartMoneyEngine, short_wallet


def _wallets() -> list[str]:
    return [w for w, _ in list_alpha_wallets(limit=250)]


def _fmt_money(x: float) -> str:
    return f"${x:,.2f}"


async def alpha_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_alpha_tables()
    text = (
        "📊 <b>PolyAlpha Terminal</b>\n\n"
        "Read-only smart-money scanner for Polymarket.\n\n"
        "Commands:\n"
        "/alpha_addwallet &lt;0x...&gt; optional label\n"
        "/alpha_wallets\n"
        "/topwallets\n"
        "/consensus\n"
        "/mywallet &lt;0x...&gt;\n"
        "/compare\n"
        "/terminal\n\n"
        "Trading is disabled in this package. Add execution later only with confirmations."
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def alpha_addwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /alpha_addwallet 0xWallet optional label")
        return
    wallet = context.args[0].strip()
    label = " ".join(context.args[1:]).strip()
    if not wallet.startswith("0x") or len(wallet) < 20:
        await update.message.reply_text("That does not look like an EVM wallet. Use 0x...")
        return
    add_alpha_wallet(wallet, label)
    await update.message.reply_text(f"✅ Added smart wallet {short_wallet(wallet)}" + (f" — {label}" if label else ""))


async def alpha_removewallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /alpha_removewallet 0xWallet")
        return
    count = remove_alpha_wallet(context.args[0])
    await update.message.reply_text("✅ Removed." if count else "Wallet not found.")


async def alpha_wallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_alpha_wallets(limit=100)
    if not rows:
        await update.message.reply_text("No smart wallets yet. Add one with /alpha_addwallet 0xWallet label")
        return
    lines = ["🧠 <b>Tracked Smart Wallets</b>"]
    for i, (wallet, label) in enumerate(rows, 1):
        lines.append(f"{i}. <code>{short_wallet(wallet)}</code>" + (f" — {label}" if label else ""))
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def topwallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = _wallets()
    if not wallets:
        await update.message.reply_text("Add smart wallets first: /alpha_addwallet 0xWallet label")
        return
    await update.message.reply_text("Scanning wallet quality…")
    scores = await SmartMoneyEngine(wallets).score_wallets(top_n=15)
    if not scores:
        await update.message.reply_text("No wallet scores returned yet. Try again after adding active wallets.")
        return
    lines = ["🏆 <b>Top Smart Wallets</b>"]
    for i, s in enumerate(scores, 1):
        lines.append(
            f"\n{i}. <code>{short_wallet(s.wallet)}</code> — <b>{s.score}/100</b>\n"
            f"ROI: {s.roi}% | Win: {s.winrate}% | Trades: {s.trades}\n"
            f"PnL: {_fmt_money(s.pnl)} | Vol: {_fmt_money(s.volume)} | Open: {_fmt_money(s.open_value)}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def consensus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = _wallets()
    if not wallets:
        await update.message.reply_text("Add smart wallets first: /alpha_addwallet 0xWallet label")
        return
    await update.message.reply_text("Scanning consensus positions…")
    signals = await SmartMoneyEngine(wallets).consensus(min_wallets=2, top_n=10)
    if not signals:
        await update.message.reply_text("No strong consensus found yet. Add more wallets or lower min_wallets in code.")
        return
    lines = ["🔥 <b>Smart Money Consensus</b>"]
    for i, s in enumerate(signals, 1):
        lines.append(
            f"\n{i}. <b>{s.title[:90]}</b>\n"
            f"Outcome: <b>{s.outcome}</b> | Score: <b>{s.score}/100</b>\n"
            f"Wallets: {s.wallets} | Value: {_fmt_money(s.total_value)} | Avg price: {s.avg_price}\n"
            f"Best: " + ", ".join(short_wallet(w) for w in s.best_wallets)
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def mywallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        current = get_alpha_setting("my_wallet")
        await update.message.reply_text(f"Current wallet: {current or 'not set'}\nUsage: /mywallet 0xYourWallet")
        return
    wallet = context.args[0].strip().lower()
    if not wallet.startswith("0x") or len(wallet) < 20:
        await update.message.reply_text("That does not look like an EVM wallet. Use 0x...")
        return
    set_alpha_setting("my_wallet", wallet)
    await update.message.reply_text(f"✅ Your wallet set to {short_wallet(wallet)}")


async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    my_wallet = get_alpha_setting("my_wallet")
    wallets = _wallets()
    if not my_wallet:
        await update.message.reply_text("Set your wallet first: /mywallet 0xYourWallet")
        return
    if not wallets:
        await update.message.reply_text("Add smart wallets first: /alpha_addwallet 0xWallet label")
        return
    await update.message.reply_text("Comparing your wallet with smart-money consensus…")
    data = await SmartMoneyEngine(wallets).compare_wallet(my_wallet, wallets)
    lines = [
        "🧬 <b>Your Wallet Comparison</b>",
        f"Wallet: <code>{short_wallet(my_wallet)}</code>",
        f"Overlap: <b>{data['overlap_pct']}%</b> ({data['overlap_count']} consensus matches)",
        "",
        "⚠️ <b>Missing Consensus Trades</b>",
    ]
    missing = data.get("missing", [])[:8]
    if not missing:
        lines.append("No missing consensus trades found.")
    for i, s in enumerate(missing, 1):
        lines.append(f"\n{i}. <b>{s.title[:85]}</b>\n{s.outcome} | Score {s.score}/100 | Wallets {s.wallets}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def terminal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = _wallets()
    my_wallet = get_alpha_setting("my_wallet")
    if not wallets:
        await update.message.reply_text("Add smart wallets first: /alpha_addwallet 0xWallet label")
        return
    await update.message.reply_text("Loading PolyAlpha Terminal…")
    engine = SmartMoneyEngine(wallets)
    signals = await engine.consensus(min_wallets=2, top_n=5)
    top = signals[0] if signals else None
    lines = ["📊 <b>POLYALPHA TERMINAL</b>", f"Smart wallets: <b>{len(wallets)}</b>"]
    if my_wallet:
        lines.append(f"Your wallet: <code>{short_wallet(my_wallet)}</code>")
    if top:
        lines += [
            "",
            "🚀 <b>Top Signal</b>",
            f"<b>{top.title[:95]}</b>",
            f"Outcome: <b>{top.outcome}</b>",
            f"Consensus Score: <b>{top.score}/100</b>",
            f"Wallets: {top.wallets} | Value: {_fmt_money(top.total_value)} | Avg price: {top.avg_price}",
        ]
    else:
        lines.append("\nNo consensus signal found yet.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def sell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔒 Trading execution is disabled in this MVP.\n"
        "Next step: add CLOB sell preview + confirmation code + max slippage."
    )


def register_alpha_handlers(app):
    ensure_alpha_tables()
    app.add_handler(CommandHandler("alpha", alpha_start_cmd))
    app.add_handler(CommandHandler("alpha_addwallet", alpha_addwallet_cmd))
    app.add_handler(CommandHandler("alpha_removewallet", alpha_removewallet_cmd))
    app.add_handler(CommandHandler("alpha_wallets", alpha_wallets_cmd))
    app.add_handler(CommandHandler("topwallets", topwallets_cmd))
    app.add_handler(CommandHandler("consensus", consensus_cmd))
    app.add_handler(CommandHandler("mywallet", mywallet_cmd))
    app.add_handler(CommandHandler("compare", compare_cmd))
    app.add_handler(CommandHandler("terminal", terminal_cmd))
    app.add_handler(CommandHandler("sell", sell_cmd))
