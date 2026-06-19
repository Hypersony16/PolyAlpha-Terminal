"""Telegram UI + commands for PolyAlpha Terminal.
Read-only smart-money intelligence layer built on PolyScalpBot.
"""
from __future__ import annotations

import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes

from bot.alpha_store import (
    add_alpha_wallet, ensure_alpha_tables, get_alpha_setting, latest_consensus, latest_alpha_scans, discovered_wallet_count,
    list_alpha_wallets, remove_alpha_wallet, set_alpha_setting, top_saved_wallet_scores,
)
from bot.smart_money import SmartMoneyEngine, short_wallet
from bot.time_utils import timestamp_with_seconds


def _wallets() -> list[str]:
    return [w for w, _ in list_alpha_wallets(limit=500)]

def _money(x: float) -> str:
    return f"${x:,.2f}"

def _pct(x: float) -> str:
    return f"{x:.1f}%"

def _esc(s: object) -> str:
    return html.escape(str(s or ""))


def alpha_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖥 Terminal", callback_data="alpha_terminal"), InlineKeyboardButton("🔎 Auto Scan", callback_data="alpha_scan")],
        [InlineKeyboardButton("🔥 Consensus", callback_data="alpha_consensus"), InlineKeyboardButton("📡 Feed", callback_data="alpha_feed")],
        [InlineKeyboardButton("🏆 Top Wallets", callback_data="alpha_topwallets"), InlineKeyboardButton("🐋 Whales", callback_data="alpha_whales")],
        [InlineKeyboardButton("👛 My Portfolio", callback_data="alpha_portfolio"), InlineKeyboardButton("🧬 Compare", callback_data="alpha_compare")],
        [InlineKeyboardButton("➕ Add Wallet", callback_data="alpha_add_hint"), InlineKeyboardButton("📋 Wallet List", callback_data="alpha_wallets")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="alpha_settings"), InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ])


def alpha_back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="alpha_terminal")],[InlineKeyboardButton("⬅️ Alpha Menu", callback_data="alpha")]])


async def _send_or_edit(update: Update, text: str, kb=None):
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        try:
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=kb or alpha_menu(), disable_web_page_preview=True)
        except Exception:
            await q.message.reply_text(text, parse_mode="HTML", reply_markup=kb or alpha_menu(), disable_web_page_preview=True)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb or alpha_menu(), disable_web_page_preview=True)


async def alpha_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_alpha_tables()
    count = len(_wallets())
    text = (
        "🧠 <b>POLYALPHA TERMINAL</b>\n"
        "<code>smart money intelligence for Polymarket</code>\n\n"
        f"Tracked smart wallets: <b>{count}</b> | Discovered: <b>{discovered_wallet_count()}</b>\n\n"
        "Use this as the terminal layer on top of PolyScalpBot:\n"
        "• rank profitable wallets\n"
        "• detect consensus positions\n"
        "• compare your wallet vs smart money\n"
        "• monitor future whale alerts\n\n"
        "Trading execution is <b>disabled</b>; buy/sell architecture is prepared only."
    )
    await _send_or_edit(update, text, alpha_menu())


async def alpha_help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 <b>PolyAlpha Commands</b>\n\n"
        "/alpha - open Alpha Terminal menu\n"
        "/terminal - smart money dashboard\n"
        "/topwallets - score tracked wallets\n"
        "/consensus - consensus signals\n"
        "/topsignals - same as consensus\n"
        "/topmarkets - market ranking\n"
        "/mywallet 0x... - set your wallet\n"
        "/portfolio - analyze your wallet\n"
        "/compare - compare your wallet vs smart money\n"
        "/alpha_addwallet 0x... label - add tracked wallet\n"
        "/alpha_removewallet 0x... - remove wallet\n"
        "/alpha_wallets - list tracked wallets\n"
        "/whales - whale/intelligence monitor\n\n"
        "Future trading commands are intentionally disabled: /buy /sell /close /closeall"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def alpha_addwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /alpha_addwallet 0xWallet optional label")
        return
    wallet = context.args[0].strip().lower()
    label = " ".join(context.args[1:]).strip()
    if not wallet.startswith("0x") or len(wallet) < 20:
        await update.message.reply_text("Invalid wallet. Use a 0x EVM wallet address.")
        return
    add_alpha_wallet(wallet, label)
    await update.message.reply_text(f"✅ Added smart wallet {short_wallet(wallet)}" + (f" — {label}" if label else ""))


async def alpha_removewallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /alpha_removewallet 0xWallet")
        return
    n = remove_alpha_wallet(context.args[0])
    await update.message.reply_text("✅ Removed wallet." if n else "Wallet not found.")


async def alpha_wallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_alpha_wallets(limit=100)
    if not rows:
        text = "📋 <b>Tracked Smart Wallets</b>\n\nNo wallets yet. Use:\n<code>/alpha_addwallet 0xWallet label</code>"
    else:
        lines = ["📋 <b>Tracked Smart Wallets</b>"]
        for i, (w, label) in enumerate(rows, 1):
            lines.append(f"{i}. <code>{short_wallet(w)}</code>" + (f" — {_esc(label)}" if label else ""))
        text = "\n".join(lines)
    await _send_or_edit(update, text, alpha_menu())



async def scan_wallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-discover smart wallets from official Polymarket leaderboards, score them, and build consensus."""
    ensure_alpha_tables()
    args = [a.strip() for a in (context.args or [])]
    category = args[0].upper() if len(args) >= 1 else "OVERALL"
    time_period = args[1].upper() if len(args) >= 2 else "MONTH"
    order_by = args[2].upper() if len(args) >= 3 else "PNL"
    try:
        limit = int(args[3]) if len(args) >= 4 else 100
    except Exception:
        limit = 100
    limit = max(10, min(250, limit))

    await update.message.reply_text(
        "🔎 <b>Starting Smart Wallet Scan</b>\n"
        f"Board: <code>{category}/{time_period}/{order_by}</code>\n"
        f"Limit: <b>{limit}</b>\n\n"
        "This can take 20-90 seconds because wallets are scored and positions are checked.",
        parse_mode="HTML",
    )
    engine = SmartMoneyEngine([])
    res = await engine.discover_from_leaderboards(category, time_period, order_by, limit=limit, score_top=min(75, limit))
    if res.get("status") != "ok":
        await update.message.reply_text(f"⚠️ Scan failed: <code>{_esc(res.get('error'))}</code>", parse_mode="HTML")
        return
    lines = [
        "✅ <b>Smart Wallet Scan Complete</b>",
        f"Found wallets: <b>{res['wallets_found']}</b>",
        f"Added/saved: <b>{res['wallets_added']}</b>",
        f"Scored: <b>{res['wallets_scored']}</b>",
        f"Consensus signals: <b>{res.get('consensus', 0)}</b>",
    ]
    if res.get("top_wallet"):
        lines.append(f"Top wallet: <code>{short_wallet(res['top_wallet'])}</code> — <b>{res.get('top_score',0):.1f}/100</b>")
    lines.append("\nNext: /topwallets, /consensus, /terminal")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=alpha_menu())


async def alpha_scan_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_alpha_tables()
    await update.message.reply_text("🛰 <b>Starting multi-board discovery scan...</b>\nThis checks several PnL/volume boards and may take up to 2 minutes.", parse_mode="HTML")
    res = await SmartMoneyEngine([]).discover_multi_leaderboards(limit_per_board=75, score_top=100)
    lines = [
        "✅ <b>Multi-Board Scan Complete</b>" if res.get("status") == "ok" else "⚠️ <b>Multi-Board Scan Finished With Errors</b>",
        f"Unique wallets found: <b>{res.get('wallets_found',0)}</b>",
        f"Scored: <b>{res.get('wallets_scored',0)}</b>",
        f"Consensus signals: <b>{res.get('consensus',0)}</b>",
    ]
    if res.get("top_wallet"):
        lines.append(f"Top wallet: <code>{short_wallet(res['top_wallet'])}</code> — <b>{res.get('top_score',0):.1f}/100</b>")
    if res.get("errors"):
        lines.append("\nSome boards failed but scan continued.")
    lines.append("\nNext: /topwallets, /consensus, /terminal")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=alpha_menu())


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scans = latest_alpha_scans(6)
    lines = ["📡 <b>Leaderboard Scanner</b>", f"Discovered wallets: <b>{discovered_wallet_count()}</b>"]
    if not scans:
        lines.append("\nNo scans yet. Use /scan_wallets or /alpha_scan_all")
    for srow in scans:
        lines.append(
            f"\n• <b>{_esc(srow['status'])}</b> {srow['source']} {srow['category']}/{srow['time_period']}/{srow['order_by']}\n"
            f"Found {srow['wallets_found']} | Scored {srow['wallets_scored']} | Top {short_wallet(srow.get('top_wallet') or '')} {srow.get('top_score') or 0:.1f}\n"
            f"{_esc(srow['created_at'])}"
        )
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def feed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cached = latest_consensus(10)
    lines = ["📡 <b>Smart Money Feed</b>", "<code>latest consensus signals from scanned wallets</code>"]
    if not cached:
        lines.append("\nNo feed yet. Run /scan_wallets first.")
    for i, sig in enumerate(cached[:10], 1):
        lines.append(
            f"\n{i}. <b>{_esc(sig['title'])[:80]}</b>\n"
            f"Outcome: <b>{_esc(sig['outcome'])}</b> | Signal {sig['score']}/100 | {sig['wallets']} wallets\n"
            f"Value: {_money(sig['total_value'])} | Edge: {sig['edge']:+.3f} | {sig['confidence']}"
        )
    await _send_or_edit(update, "\n".join(lines), alpha_menu())

async def topwallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = _wallets()
    if not wallets:
        await _send_or_edit(update, "🏆 <b>Top Wallets</b>\n\nNo smart wallets saved yet. Run <code>/scan_wallets OVERALL MONTH PNL 50</code> first, or add manually with <code>/alpha_addwallet 0xWallet label</code>.", alpha_menu())
        return
    if update.message:
        await update.message.reply_text("Scanning wallet quality…")
    scores = await SmartMoneyEngine(wallets).score_wallets(top_n=15)
    if not scores:
        saved = top_saved_wallet_scores(15)
        lines = ["🏆 <b>Top Wallets</b>", "No fresh API scores. Saved cache:"]
        for i, s in enumerate(saved, 1):
            lines.append(f"{i}. <code>{short_wallet(s['wallet'])}</code> — {s['score']}/100")
        await _send_or_edit(update, "\n".join(lines), alpha_menu())
        return
    lines = ["🏆 <b>Top Smart Wallets</b>"]
    for i, s in enumerate(scores, 1):
        lines.append(
            f"\n{i}. <code>{short_wallet(s.wallet)}</code> — <b>{s.score}/100</b>\n"
            f"ROI: {_pct(s.roi)} | Win: {_pct(s.winrate)} | Trades: {s.trades}\n"
            f"PnL: {_money(s.pnl)} | Vol: {_money(s.volume)} | Open: {_money(s.open_value)}\n"
            f"Consistency: {s.consistency}/100 | DD: {_money(s.drawdown)}"
        )
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def consensus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = _wallets()
    if not wallets:
        cached = latest_consensus(10)
        if cached:
            lines = ["🔥 <b>Latest Saved Consensus</b>"]
            for i, s in enumerate(cached, 1):
                lines.append(f"\n{i}. <b>{_esc(s['title'])[:80]}</b>\n{s['outcome']} | Score {s['score']}/100 | Wallets {s['wallets']}")
            await _send_or_edit(update, "\n".join(lines), alpha_menu())
            return
        await _send_or_edit(update, "🔥 <b>Consensus</b>\n\nNo smart wallets saved yet. Run <code>/scan_wallets OVERALL MONTH PNL 50</code> first.", alpha_menu())
        return
    if update.message:
        await update.message.reply_text("Scanning consensus positions…")
    signals = await SmartMoneyEngine(wallets).consensus(min_wallets=2, top_n=10)
    if not signals:
        cached = latest_consensus(10)
        if not cached:
            await _send_or_edit(update, "No consensus found yet. Add more active wallets.", alpha_menu())
            return
        lines = ["🔥 <b>Latest Saved Consensus</b>"]
        for i, s in enumerate(cached, 1):
            lines.append(f"\n{i}. <b>{_esc(s['title'])[:80]}</b>\n{s['outcome']} | Score {s['score']}/100 | Wallets {s['wallets']}")
        await _send_or_edit(update, "\n".join(lines), alpha_menu())
        return
    lines = ["🔥 <b>Smart Money Consensus</b>", "<code>overlap among highest-scored wallets</code>"]
    for i, s in enumerate(signals, 1):
        lines.append(
            f"\n{i}. <b>{_esc(s.title)[:90]}</b>\n"
            f"Outcome: <b>{_esc(s.outcome)}</b> | Signal: <b>{s.score}/100</b> | {s.confidence}\n"
            f"Wallets: {s.wallets} | Value: {_money(s.total_value)} | Avg price: {s.avg_price}\n"
            f"Fair: {s.fair_value} | Edge: {s.edge:+.3f}\n"
            f"Best: " + ", ".join(short_wallet(w) for w in s.best_wallets)
        )
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def mywallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        current = get_alpha_setting("my_wallet")
        await update.message.reply_text(f"Current wallet: {current or 'not set'}\nUsage: /mywallet 0xYourWallet")
        return
    wallet = context.args[0].strip().lower()
    if not wallet.startswith("0x") or len(wallet) < 20:
        await update.message.reply_text("Invalid wallet. Use 0x...")
        return
    set_alpha_setting("my_wallet", wallet)
    await update.message.reply_text(f"✅ Your wallet set to {short_wallet(wallet)}")


async def portfolio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallet = context.args[0].strip().lower() if context.args else get_alpha_setting("my_wallet")
    if not wallet:
        await _send_or_edit(update, "👛 <b>Portfolio</b>\n\nSet wallet first: /mywallet 0xYourWallet", alpha_menu())
        return
    if update.message:
        await update.message.reply_text("Loading portfolio…")
    positions = await SmartMoneyEngine([]).client.fetch_positions(wallet)
    total = sum(max(p.value, p.size*p.current_price) for p in positions)
    lines = ["👛 <b>Portfolio Analyzer</b>", f"Wallet: <code>{short_wallet(wallet)}</code>", f"Positions: <b>{len(positions)}</b>", f"Exposure: <b>{_money(total)}</b>"]
    for p in sorted(positions, key=lambda x: max(x.value, x.size*x.current_price), reverse=True)[:10]:
        val = max(p.value, p.size*p.current_price)
        lines.append(f"\n• <b>{_esc(p.title)[:70]}</b>\n{_esc(p.outcome)} | {_money(val)} | px {p.current_price:.3f}")
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    my_wallet = get_alpha_setting("my_wallet")
    wallets = _wallets()
    if not my_wallet:
        await _send_or_edit(update, "🧬 <b>Compare</b>\n\nSet your wallet first: /mywallet 0xYourWallet", alpha_menu())
        return
    if not wallets:
        await _send_or_edit(update, "Add smart wallets first.", alpha_menu())
        return
    if update.message:
        await update.message.reply_text("Comparing against smart-money consensus…")
    data = await SmartMoneyEngine(wallets).compare_wallet(my_wallet, wallets)
    lines = ["🧬 <b>Wallet Alignment</b>", f"Wallet: <code>{short_wallet(my_wallet)}</code>", f"Alignment: <b>{data['overlap_pct']}%</b> ({data['overlap_count']} matches)"]
    lines.append("\n⚠️ <b>Missing High-Consensus Trades</b>")
    if not data.get("missing"):
        lines.append("No missing consensus trades found.")
    for i, s in enumerate(data.get("missing", [])[:8], 1):
        lines.append(f"\n{i}. <b>{_esc(s.title)[:80]}</b>\n{s.outcome} | Score {s.score}/100 | Wallets {s.wallets} | Edge {s.edge:+.3f}")
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def terminal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = _wallets(); my_wallet = get_alpha_setting("my_wallet")
    if not wallets:
        text = "🖥 <b>POLYALPHA TERMINAL</b>\n\nNo smart wallets yet. Run:\n<code>/scan_wallets OVERALL MONTH PNL 50</code>\nor add manually with:\n<code>/alpha_addwallet 0xWallet label</code>"
        await _send_or_edit(update, text, alpha_menu()); return
    if update.message:
        await update.message.reply_text("Loading terminal…")
    engine = SmartMoneyEngine(wallets)
    signals = await engine.consensus(min_wallets=2, top_n=5)
    scores = top_saved_wallet_scores(5)
    lines = ["🖥 <b>POLYALPHA TERMINAL</b>", "<code>Bloomberg-style Polymarket intelligence</code>", f"Smart wallets: <b>{len(wallets)}</b> | Discovered: <b>{discovered_wallet_count()}</b>"]
    if my_wallet: lines.append(f"Your wallet: <code>{short_wallet(my_wallet)}</code>")
    if signals:
        t = signals[0]
        lines += ["", "🚀 <b>Top Signal</b>", f"<b>{_esc(t.title)[:95]}</b>", f"Outcome: <b>{_esc(t.outcome)}</b>", f"Consensus: <b>{t.score}/100</b> | {t.confidence}", f"Wallets: {t.wallets} | Value: {_money(t.total_value)}", f"Avg price: {t.avg_price} | Fair: {t.fair_value} | Edge: {t.edge:+.3f}"]
    else:
        lines.append("\nNo fresh consensus yet.")
    if scores:
        lines.append("\n🏆 <b>Top Wallet Cache</b>")
        for s in scores[:3]: lines.append(f"• {short_wallet(s['wallet'])} — {s['score']}/100")
    lines.append(f"\n<i>{timestamp_with_seconds()}</i>")
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def whales_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scores = top_saved_wallet_scores(10)
    lines = ["🐋 <b>Whale Tracker</b>", "<code>read-only monitor; alert job can be added later</code>"]
    if not scores:
        lines.append("\nNo scored wallets yet. Run /topwallets first.")
    for s in scores:
        if (s.get("score") or 0) >= 70:
            lines.append(f"\n• <code>{short_wallet(s['wallet'])}</code> score {s['score']}/100 | open {_money(s['open_value'])} | vol {_money(s['volume'])}")
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def topmarkets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await consensus_cmd(update, context)


async def trade_disabled_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔒 Real trading is not implemented yet. Future /buy /sell /close /closeall will require CONFIRM code and slippage checks.")


async def alpha_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    fake_update = update
    if data == "alpha": return await alpha_start_cmd(fake_update, context)
    if data == "alpha_terminal": return await terminal_cmd(fake_update, context)
    if data == "alpha_scan":
        q = update.callback_query
        await q.answer()
        await q.message.reply_text(
            "🔎 <b>Starting Smart Wallet Scan</b>\n"
            "Board: <code>OVERALL/MONTH/PNL</code>\n"
            "Limit: <b>50</b>\n\n"
            "Using official <code>/v1/leaderboard</code>. This can take 20-90 seconds.",
            parse_mode="HTML",
        )
        res = await SmartMoneyEngine([]).discover_from_leaderboards("OVERALL", "MONTH", "PNL", limit=50, score_top=50)
        if res.get("status") != "ok":
            return await q.message.reply_text(f"⚠️ Scan failed: <code>{_esc(res.get('error'))}</code>", parse_mode="HTML", reply_markup=alpha_menu())
        lines = [
            "✅ <b>Smart Wallet Scan Complete</b>",
            f"Found wallets: <b>{res.get('wallets_found',0)}</b>",
            f"Saved wallets: <b>{res.get('wallets_added',0)}</b>",
            f"Scored: <b>{res.get('wallets_scored',0)}</b>",
            f"Consensus signals: <b>{res.get('consensus',0)}</b>",
        ]
        if res.get("top_wallet"):
            lines.append(f"Top wallet: <code>{short_wallet(res['top_wallet'])}</code> — <b>{res.get('top_score',0):.1f}/100</b>")
        lines.append("\nNext: /topwallets, /consensus, /terminal")
        return await q.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=alpha_menu())
    if data == "alpha_feed": return await feed_cmd(fake_update, context)
    if data == "alpha_consensus": return await consensus_cmd(fake_update, context)
    if data == "alpha_topwallets": return await topwallets_cmd(fake_update, context)
    if data == "alpha_wallets": return await alpha_wallets_cmd(fake_update, context)
    if data == "alpha_compare": return await compare_cmd(fake_update, context)
    if data == "alpha_portfolio": return await portfolio_cmd(fake_update, context)
    if data == "alpha_whales": return await whales_cmd(fake_update, context)
    if data == "alpha_settings": return await _send_or_edit(update, "⚙️ <b>Alpha Settings</b>\n\nReal trading: OFF\nWhale alerts: read-only MVP\nMin consensus wallets: 2", alpha_menu())
    if data == "alpha_add_hint":
        await update.callback_query.answer()
        return await update.callback_query.message.reply_text("Use: /alpha_addwallet 0xWallet optional label")


def register_alpha_handlers(app):
    ensure_alpha_tables()
    app.add_handler(CommandHandler("alpha", alpha_start_cmd))
    app.add_handler(CommandHandler("alpha_help", alpha_help_cmd))
    app.add_handler(CommandHandler("alpha_addwallet", alpha_addwallet_cmd))
    app.add_handler(CommandHandler("alpha_removewallet", alpha_removewallet_cmd))
    app.add_handler(CommandHandler("alpha_wallets", alpha_wallets_cmd))
    app.add_handler(CommandHandler("scan_wallets", scan_wallets_cmd))
    app.add_handler(CommandHandler("alpha_scan_all", alpha_scan_all_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("feed", feed_cmd))
    app.add_handler(CommandHandler("topwallets", topwallets_cmd))
    app.add_handler(CommandHandler("consensus", consensus_cmd))
    app.add_handler(CommandHandler("topsignals", consensus_cmd))
    app.add_handler(CommandHandler("topmarkets", topmarkets_cmd))
    app.add_handler(CommandHandler("terminal", terminal_cmd))
    app.add_handler(CommandHandler("mywallet", mywallet_cmd))
    app.add_handler(CommandHandler("portfolio", portfolio_cmd))
    app.add_handler(CommandHandler("wallet", portfolio_cmd))
    app.add_handler(CommandHandler("compare", compare_cmd))
    app.add_handler(CommandHandler("whales", whales_cmd))
    app.add_handler(CommandHandler("buy", trade_disabled_cmd))
    app.add_handler(CommandHandler("sell", trade_disabled_cmd))
    app.add_handler(CommandHandler("close", trade_disabled_cmd))
    app.add_handler(CommandHandler("closeall", trade_disabled_cmd))
    app.add_handler(CallbackQueryHandler(alpha_button, pattern="^alpha"))
