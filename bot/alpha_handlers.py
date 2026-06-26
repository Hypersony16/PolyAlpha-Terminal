"""Telegram UI + commands for PolyAlpha Terminal v2.3.
Read-only smart-money intelligence layer built on PolyScalpBot.

UI improvements:
- Cleaner section separation
- Score breakdown per wallet
- Better consensus display with conviction/edge
- Proper whale feed
- Improved compare (shared + missing + risky)
- Fast cached responses by default
"""
from __future__ import annotations

import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes

from bot.alpha_store import (
    add_alpha_wallet,
    ensure_alpha_tables,
    get_alpha_setting,
    latest_consensus,
    latest_alpha_scans,
    discovered_wallet_count,
    list_alpha_wallets,
    latest_whale_alerts,
    remove_alpha_wallet,
    set_alpha_setting,
    top_saved_wallet_scores,
)
from bot.smart_money import SmartMoneyEngine, short_wallet
from bot.time_utils import timestamp_with_seconds


# ── helpers ──────────────────────────────────────────────────────────────────

def _wallets() -> list[str]:
    return [w for w, _ in list_alpha_wallets(limit=500)]


def _money(x: float) -> str:
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.1f}k"
    return f"${x:,.0f}"


def _pct(x: float) -> str:
    sign = "+" if x > 0 else ""
    return f"{sign}{x:.1f}%"


def _esc(s: object) -> str:
    return html.escape(str(s or ""))


def _profile_url(wallet: str) -> str:
    w = str(wallet or "").strip().lower()
    return f"https://polymarket.com/profile/{w}" if w.startswith("0x") else "https://polymarket.com"


def _wallet_link(wallet: str, label: str | None = None) -> str:
    w = str(wallet or "").strip().lower()
    text = label or short_wallet(w)
    return f'<a href="{_profile_url(w)}">{_esc(text)}</a>' if w.startswith("0x") else _esc(text)

def _market_url(market: str, title: str = "") -> str:
    m = str(market or "").strip()
    # Slugs usually look like btc-updown-15m-1779635700 or will-x-win.
    if m and " " not in m and len(m) < 180 and not m.startswith("0x"):
        return f"https://polymarket.com/event/{m}"
    q = (title or m or "polymarket").replace(" ", "%20")[:180]
    return f"https://polymarket.com/search?query={q}"


def _market_link(market: str, title: str, label: str = "Open market") -> str:
    return f'<a href="{_market_url(market, title)}">{_esc(label)}</a>'


def _grade_emoji(score: float) -> str:
    if score >= 80:
        return "🟢"
    if score >= 65:
        return "🔵"
    if score >= 50:
        return "🟡"
    if score >= 35:
        return "🟠"
    return "🔴"


def _conf_emoji(conf: str) -> str:
    return {"High": "🔥", "Medium": "⚡", "Low": "📉"}.get(conf, "•")


def _divider() -> str:
    return "─" * 28


# ── menus ─────────────────────────────────────────────────────────────────────

def alpha_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Actionable Alpha", callback_data="alpha_actionable"),
         InlineKeyboardButton("🖥 Terminal", callback_data="alpha_terminal")],
        [InlineKeyboardButton("🔥 Consensus", callback_data="alpha_consensus"),
         InlineKeyboardButton("🏆 Top Wallets", callback_data="alpha_topwallets")],
        [InlineKeyboardButton("📡 Feed", callback_data="alpha_feed"),
         InlineKeyboardButton("🧬 Compare", callback_data="alpha_compare")],
        [InlineKeyboardButton("🐋 Whales", callback_data="alpha_whales"),
         InlineKeyboardButton("🧪 Quality Lab", callback_data="alpha_quality")],
        [InlineKeyboardButton("👛 Portfolio", callback_data="alpha_portfolio"),
         InlineKeyboardButton("🔎 Scan Now", callback_data="alpha_scan")],
        [InlineKeyboardButton("📋 Wallets", callback_data="alpha_wallets"),
         InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ])


def back_to_alpha() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Alpha Menu", callback_data="alpha")]])


# ── send/edit helper ───────────────────────────────────────────────────────────

async def _send_or_edit(update: Update, text: str, kb=None):
    kb = kb or alpha_menu()
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        try:
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            await q.message.reply_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


# ── /alpha ─────────────────────────────────────────────────────────────────────

async def alpha_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_alpha_tables()
    n_tracked = len(_wallets())
    n_disc = discovered_wallet_count()
    cached = latest_consensus(1)
    top_signal = ""
    if cached:
        t = cached[0]
        top_signal = (
            f"\n🚀 <b>Top Signal:</b> {_esc(t.get('title', '')[:60])}\n"
            f"   {_esc(t.get('outcome'))} | {(t.get('score') or 0):.0f}/100 | {t.get('wallets')} wallets"
        )

    text = (
        "🧠 <b>POLYALPHA TERMINAL</b>\n"
        "<code>Smart Money Intelligence for Polymarket</code>\n"
        f"{_divider()}\n"
        f"Tracked wallets: <b>{n_tracked}</b>  ·  Discovered: <b>{n_disc}</b>"
        f"{top_signal}\n"
        f"{_divider()}\n"
        "What this does:\n"
        "• Ranks profitable Polymarket traders by ROI, PnL, win rate\n"
        "• Detects where smart money overlaps → consensus signals\n"
        "• Compares your wallet vs smart-money positions\n"
        "• Tracks whale moves in real time\n\n"
        "<i>Trading is read-only. /buy /sell are disabled.</i>"
    )
    await _send_or_edit(update, text, alpha_menu())


# ── /actionable / /alpha_score ─────────────────────────────────────────────────

async def actionable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show only the most actionable cached smart-money ideas.

    This intentionally uses cache only, so it is fast. /consensus_refresh or /scan_wallets rebuilds data.
    """
    cached = latest_consensus(30)
    if not cached:
        await _send_or_edit(
            update,
            "🚀 <b>ACTIONABLE ALPHA</b>\n\nNo cached signals yet. Run: <code>/scan_wallets OVERALL MONTH PNL 50</code>",
            alpha_menu(),
        )
        return

    # Strict filter first, then graceful fallback so the screen never looks broken.
    strict = [
        s for s in cached
        if (s.get("score") or 0) >= 60
        and (s.get("wallets") or 0) >= 3
        and (s.get("total_value") or 0) >= 1000
        and (s.get("edge") or 0) >= 0.03
        and (s.get("confidence") or "") in ("High", "Medium")
    ]
    used_fallback = False
    signals = strict[:8]
    if not signals:
        used_fallback = True
        signals = sorted(cached, key=lambda x: ((x.get("score") or 0), (x.get("total_value") or 0)), reverse=True)[:8]

    lines = [
        "🚀 <b>ACTIONABLE ALPHA</b>",
        "<code>clean shortlist · cached · no live scan latency</code>",
        _divider(),
    ]
    if used_fallback:
        lines.append("<i>No strict high-quality signals yet, showing best cached candidates.</i>")

    for i, srow in enumerate(signals, 1):
        title = srow.get("title", "")
        market = srow.get("market", "")
        outcome = srow.get("outcome", "")
        score = srow.get("score") or 0
        edge = srow.get("edge") or 0
        fair = srow.get("fair_value") or 0
        px = srow.get("avg_price") or 0
        conf = srow.get("confidence", "")
        lines.append(
            f"\n<b>{i}. {_conf_emoji(conf)} {_esc(title[:72])}</b>\n"
            f"Outcome: <b>{_esc(outcome)}</b>  ·  Signal: <b>{score:.0f}/100</b>  ·  {_esc(conf)}\n"
            f"Wallets: <b>{srow.get('wallets')}</b>  ·  Value: <b>{_money(srow.get('total_value') or 0)}</b>  ·  Avg score: <b>{(srow.get('avg_wallet_score') or 0):.0f}</b>\n"
            f"Price <code>{px:.3f}</code> → fair <code>{fair:.3f}</code>  ·  edge <b>{edge:+.3f}</b>\n"
            f"{_market_link(market, title, 'Open market')}  ·  <code>{_esc(market)}</code>"
        )

    lines.append(f"\n{_divider()}")
    lines.append("Use /consensus_refresh after scans. Use /quality to see why signals pass/fail.")
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def quality_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quality diagnostics for wallet scores and consensus cache."""
    scores = top_saved_wallet_scores(250)
    signals = latest_consensus(50)
    if not scores and not signals:
        await _send_or_edit(update, "🧪 <b>QUALITY LAB</b>\n\nNo data yet. Run /scan_wallets first.", alpha_menu())
        return

    elite = [s for s in scores if (s.get("score") or 0) >= 80]
    strong = [s for s in scores if 65 <= (s.get("score") or 0) < 80]
    good = [s for s in scores if 50 <= (s.get("score") or 0) < 65]
    weak = [s for s in scores if (s.get("score") or 0) < 50]
    strict_signals = [s for s in signals if (s.get("score") or 0) >= 60 and (s.get("wallets") or 0) >= 3 and (s.get("total_value") or 0) >= 1000]

    avg_score = sum((s.get("score") or 0) for s in scores) / max(1, len(scores))
    avg_pnl = sum((s.get("pnl") or 0) for s in scores) / max(1, len(scores))
    avg_roi = sum((s.get("roi") or 0) for s in scores) / max(1, len(scores))

    lines = [
        "🧪 <b>QUALITY LAB</b>",
        "<code>is the smart-money cache actually useful?</code>",
        _divider(),
        f"Wallet scores: <b>{len(scores)}</b>  ·  Avg score: <b>{avg_score:.1f}/100</b>",
        f"Elite 80+: <b>{len(elite)}</b>  ·  Strong 65-80: <b>{len(strong)}</b>  ·  Good 50-65: <b>{len(good)}</b>  ·  Weak: <b>{len(weak)}</b>",
        f"Avg ROI: <b>{_pct(avg_roi)}</b>  ·  Avg PnL: <b>{_money(avg_pnl)}</b>",
        "",
        f"Consensus signals cached: <b>{len(signals)}</b>",
        f"Strict actionable signals: <b>{len(strict_signals)}</b>",
    ]

    if signals:
        wallets_counts = [s.get("wallets") or 0 for s in signals]
        values = [s.get("total_value") or 0 for s in signals]
        lines += [
            f"Avg signal wallets: <b>{sum(wallets_counts)/max(1,len(wallets_counts)):.1f}</b>",
            f"Avg signal value: <b>{_money(sum(values)/max(1,len(values)))}</b>",
        ]

    lines += ["", "<b>Recommended next action</b>"]
    if len(strict_signals) == 0:
        lines.append("Run /alpha_scan_all or lower quality floors only if results stay empty.")
    elif len(elite) + len(strong) < 10:
        lines.append("Scan more leaderboards: /alpha_scan_all")
    else:
        lines.append("Use /actionable and compare against your wallet.")

    await _send_or_edit(update, "\n".join(lines), alpha_menu())

# ── /terminal ──────────────────────────────────────────────────────────────────

async def terminal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_alpha_tables()
    wallets = _wallets()
    my_wallet = get_alpha_setting("my_wallet")
    scans = latest_alpha_scans(1)
    last_scan = scans[0].get("created_at", "never") if scans else "never"
    n_disc = discovered_wallet_count()

    if not wallets:
        await _send_or_edit(
            update,
            "🖥 <b>POLYALPHA TERMINAL</b>\n\nNo data yet.\nRun: <code>/scan_wallets OVERALL MONTH PNL 50</code>",
            alpha_menu(),
        )
        return

    scores = top_saved_wallet_scores(3)
    cached = latest_consensus(5)
    whale_alerts = latest_whale_alerts(3)

    lines = [
        "🖥 <b>POLYALPHA TERMINAL</b>",
        "<code>Bloomberg-style Polymarket Intelligence</code>",
        _divider(),
        f"Smart wallets: <b>{len(wallets)}</b>  ·  Discovered: <b>{n_disc}</b>",
        f"Last scan: <code>{last_scan[:19]}</code>",
    ]
    if my_wallet:
        lines.append(f"Your wallet: <code>{short_wallet(my_wallet)}</code>")

    # Top signal
    if cached:
        t = cached[0]
        edge_str = f"{(t.get('edge') or 0):+.3f}"
        lines += [
            "",
            "🔥 <b>TOP CONSENSUS SIGNAL</b>",
            f"<b>{_esc(t.get('title', '')[:90])}</b>",
            f"Outcome: <b>{_esc(t.get('outcome'))}</b>  |  Signal: <b>{(t.get('score') or 0):.0f}/100</b>  |  {_conf_emoji(t.get('confidence',''))} {_esc(t.get('confidence'))}",
            f"Wallets: <b>{t.get('wallets')}</b>  ·  Value: <b>{_money(t.get('total_value') or 0)}</b>",
            f"Avg price: <code>{(t.get('avg_price') or 0):.3f}</code>  ·  Fair: <code>{(t.get('fair_value') or 0):.3f}</code>  ·  Edge: <b>{edge_str}</b>",
            f"{_market_link(t.get('market',''), t.get('title',''), 'Open market')}  ·  <code>{_esc(t.get('market',''))}</code>",
        ]
    else:
        lines += ["", "No consensus cache yet. Run /scan_wallets."]

    # Top wallets summary
    if scores:
        lines += ["", "🏆 <b>TOP SMART WALLETS</b>"]
        for s in scores:
            w = s.get("wallet", "")
            g = _grade_emoji(s.get("score") or 0)
            lines.append(
                f"{g} {_wallet_link(w)} — <b>{(s.get('score') or 0):.1f}/100</b>"
                f"  ROI {_pct(s.get('roi') or 0)}"
            )

    # Whale alerts
    if whale_alerts:
        lines += ["", "🐋 <b>RECENT WHALE ACTIVITY</b>"]
        for a in whale_alerts[:2]:
            lines.append(
                f"• {short_wallet(a.get('wallet',''))} — {_esc(a.get('outcome'))} on {_esc(a.get('market','')[:45])}\n"
                f"  Value: <b>{_money(a.get('value') or 0)}</b>  ·  Score: {(a.get('score') or 0):.0f}/100"
            )

    lines.append(f"\n<i>{timestamp_with_seconds()}</i>")
    lines.append("Use /scan_wallets to refresh  ·  /consensus for all signals")
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


# ── /topwallets ────────────────────────────────────────────────────────────────

async def topwallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    saved = top_saved_wallet_scores(15)
    if not saved:
        await _send_or_edit(
            update,
            "🏆 <b>Top Wallets</b>\n\nNo scored wallets yet.\nRun: <code>/scan_wallets OVERALL MONTH PNL 50</code>",
            alpha_menu(),
        )
        return

    lines = [
        "🏆 <b>TOP SMART WALLETS</b>",
        "<code>Ranked by ROI · PnL · Win Rate · Consistency</code>",
        _divider(),
    ]
    for i, x in enumerate(saved, 1):
        w = x.get("wallet") or ""
        score = x.get("score") or 0
        g = _grade_emoji(score)
        comps = x.get("components") or {}
        # Why: top contributing components
        reasons = []
        if comps.get("roi_score", comps.get("roi_bonus", 0)) >= 10:
            reasons.append(f"ROI {_pct(x.get('roi') or 0)}")
        if comps.get("wr_score", 0) >= 6:
            reasons.append(f"WR {(x.get('winrate') or 0):.0f}%")
        if comps.get("pnl_score", 0) >= 6:
            reasons.append(f"PnL {_money(x.get('pnl') or 0)}")
        if comps.get("rank_score", 0) >= 20:
            reasons.append("top-ranked")
        why = ", ".join(reasons) if reasons else "leaderboard rank"

        lines.append(
            f"\n<b>{i}. {g} {_wallet_link(w)}</b>  —  <b>{score:.1f}/100</b> ({x.get('label','') or 'wallet'})\n"
            f"ROI: <b>{_pct(x.get('roi') or 0)}</b>  ·  WR: {(x.get('winrate') or 0):.0f}%  ·  PnL: {_money(x.get('pnl') or 0)}\n"
            f"Vol: {_money(x.get('volume') or 0)}  ·  Open: {_money(x.get('open_value') or 0)}  ·  Trades: {int(x.get('trades') or 0)}\n"
            f"<i>Why: {_esc(why)}</i>\n"
            f"<code>{_esc(w)}</code>"
        )
    lines.append(f"\n{_divider()}")
    lines.append("Cache from latest scan  ·  /scan_wallets to refresh")
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


# ── /consensus ─────────────────────────────────────────────────────────────────

async def consensus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cached = latest_consensus(12)
    if not cached:
        await _send_or_edit(
            update,
            "🔥 <b>Consensus</b>\n\nNo consensus data yet.\nRun: <code>/scan_wallets OVERALL MONTH PNL 50</code>",
            alpha_menu(),
        )
        return

    lines = [
        "🔥 <b>SMART MONEY CONSENSUS</b>",
        "<code>Overlapping positions from top-scored wallets only</code>",
        _divider(),
    ]
    for i, srow in enumerate(cached, 1):
        conf = srow.get("confidence", "Medium")
        score = srow.get("score") or 0
        edge = srow.get("edge") or 0
        conv = srow.get("weighted_conviction") or 0
        lines.append(
            f"\n<b>{i}. {_conf_emoji(conf)} {_esc(srow.get('title','')[:80])}</b>\n"
            f"Outcome: <b>{_esc(srow.get('outcome'))}</b>  |  Signal: <b>{score:.0f}/100</b>  |  {conf}\n"
            f"Smart wallets: <b>{srow.get('wallets')}</b>  ·  Avg score: <b>{(srow.get('avg_wallet_score') or 0):.0f}</b>  ·  Value: <b>{_money(srow.get('total_value') or 0)}</b>\n"
            f"Market px: <code>{(srow.get('avg_price') or 0):.3f}</code>  ·  Fair: <code>{(srow.get('fair_value') or 0):.3f}</code>  ·  Edge: <b>{edge:+.3f}</b>\n"
            f"Conviction: <b>{conv:.0f}/100</b>\n"
            f"{_market_link(srow.get('market',''), srow.get('title',''), 'Open market')}  ·  <code>{_esc(srow.get('market',''))}</code>"
        )
    lines.append(f"\n{_divider()}")
    lines.append("Use <code>/consensus_refresh</code> to rebuild live  ·  <code>/topsignals</code> for same view")
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


# ── /consensus_refresh ─────────────────────────────────────────────────────────

async def consensus_refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("🔄 Rebuilding consensus from cached positions…")

    engine = SmartMoneyEngine()
    # Fast: rebuild from DB positions without new API calls
    signals = engine.consensus_from_cache(top_n=15)

    if not signals:
        await _send_or_edit(update, "No consensus found in cached data. Run /scan_wallets first.", alpha_menu())
        return

    lines = [
        "🔥 <b>LIVE CONSENSUS REFRESH</b>",
        f"Built from cached positions — {len(signals)} signals",
        _divider(),
    ]
    for i, sig in enumerate(signals, 1):
        lines.append(
            f"\n<b>{i}. {_conf_emoji(sig.confidence)} {_esc(sig.title[:80])}</b>\n"
            f"{_esc(sig.outcome)}  |  Signal {sig.score:.0f}/100  |  {sig.wallets} wallets  |  {sig.confidence}\n"
            f"Value: {_money(sig.total_value)}  ·  Edge: {sig.edge:+.3f}  ·  Conviction: {sig.weighted_conviction:.0f}/100"
        )
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


# ── /feed ─────────────────────────────────────────────────────────────────────

async def feed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cached = latest_consensus(12)
    whale_alerts = latest_whale_alerts(15)

    lines = ["📡 <b>SMART MONEY FEED</b>", _divider()]

    # Whale position changes
    if whale_alerts:
        lines.append("\n🐋 <b>WHALE POSITIONS</b>")
        for a in whale_alerts[:8]:
            w = a.get("wallet", "")
            score = a.get("score") or 0
            lines.append(
                f"\n{_grade_emoji(score)} {_wallet_link(w)} — score <b>{score:.0f}/100</b>\n"
                f"Market: {_esc(a.get('market','')[:60])}\n"
                f"Outcome: <b>{_esc(a.get('outcome'))}</b>  ·  Value: <b>{_money(a.get('value') or 0)}</b>\n"
                f"<i>{_esc(a.get('created_at', '')[:16])}</i>"
            )
    else:
        lines.append("\n<i>No whale alerts yet. Run /scan_wallets to populate.</i>")

    # Consensus feed
    if cached:
        lines += ["", "🔥 <b>CONSENSUS SIGNALS</b>"]
        for srow in cached[:6]:
            conf = srow.get("confidence", "")
            lines.append(
                f"\n{_conf_emoji(conf)} <b>{_esc(srow.get('title','')[:70])}</b>\n"
                f"{_esc(srow.get('outcome'))}  ·  {srow.get('wallets')} wallets  ·  {_money(srow.get('total_value') or 0)}"
            )

    lines.append(f"\n{_divider()}\n<i>{timestamp_with_seconds()}</i>")
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


# ── /whales ────────────────────────────────────────────────────────────────────

async def whales_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    whale_alerts = latest_whale_alerts(20)
    scores = top_saved_wallet_scores(5)
    high_score_wallets = [s for s in scores if (s.get("score") or 0) >= 60]

    lines = ["🐋 <b>WHALE TRACKER</b>", "<code>High-score wallets with large positions</code>", _divider()]

    if whale_alerts:
        for a in whale_alerts[:12]:
            w = a.get("wallet", "")
            score = a.get("score") or 0
            lines.append(
                f"\n{_grade_emoji(score)} {_wallet_link(w)} — <b>{score:.0f}/100</b>\n"
                f"<b>{_esc(a.get('outcome'))}</b> on {_esc(a.get('market','')[:55])}\n"
                f"Size: <b>{_money(a.get('value') or 0)}</b>  ·  px {(a.get('price') or 0):.3f}\n"
                f"<i>{_esc(a.get('created_at','')[:16])}</i>\n"
                f"<code>{_esc(w)}</code>"
            )
    elif high_score_wallets:
        lines.append("\nNo whale alerts yet. Current top wallets:")
        for s in high_score_wallets:
            w = s.get("wallet", "")
            lines.append(
                f"\n{_grade_emoji(s.get('score') or 0)} {_wallet_link(w)} — <b>{(s.get('score') or 0):.1f}/100</b>\n"
                f"Open: {_money(s.get('open_value') or 0)}  ·  Vol: {_money(s.get('volume') or 0)}"
            )
    else:
        lines.append("\nNo data yet. Run /scan_wallets.")

    await _send_or_edit(update, "\n".join(lines), alpha_menu())


# ── /wallet / /portfolio ───────────────────────────────────────────────────────

async def portfolio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallet = (context.args[0].strip().lower() if context.args else None) or get_alpha_setting("my_wallet")
    if not wallet:
        await _send_or_edit(update, "👛 <b>Portfolio</b>\n\nSet wallet first:\n<code>/mywallet 0xYourWallet</code>", alpha_menu())
        return
    if update.message:
        await update.message.reply_text("Loading portfolio…")
    positions = await SmartMoneyEngine().client.fetch_positions(wallet)
    total = sum(max(p.value, p.size * p.current_price) for p in positions)
    lines = [
        "👛 <b>PORTFOLIO</b>",
        f"Wallet: <code>{_esc(wallet)}</code>",
        f"<a href=\"{_profile_url(wallet)}\">View on Polymarket</a>",
        _divider(),
        f"Positions: <b>{len(positions)}</b>  ·  Exposure: <b>{_money(total)}</b>",
    ]
    for p in sorted(positions, key=lambda x: max(x.value, x.size * x.current_price), reverse=True)[:12]:
        val = max(p.value, p.size * p.current_price)
        pnl_est = (p.current_price - p.avg_price) * p.size if p.avg_price and p.size else 0
        pnl_str = f"  est. {_pct(pnl_est / max(0.01, p.avg_price * p.size) * 100)}" if pnl_est else ""
        lines.append(
            f"\n• <b>{_esc(p.title[:65])}</b>\n"
            f"  {_esc(p.outcome)}  ·  {_money(val)}{pnl_str}\n"
            f"  avg {p.avg_price:.3f}  →  cur {p.current_price:.3f}"
        )
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


# ── /compare ───────────────────────────────────────────────────────────────────

async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    my_wallet = get_alpha_setting("my_wallet")
    if not my_wallet:
        await _send_or_edit(update, "🧬 <b>Compare</b>\n\nSet your wallet first:\n<code>/mywallet 0xYourWallet</code>", alpha_menu())
        return
    if update.message:
        await update.message.reply_text("Comparing your wallet against smart-money consensus…")

    data = await SmartMoneyEngine().compare_wallet(my_wallet)
    pct = data.get("overlap_pct", 0)
    n_overlap = data.get("overlap_count", 0)

    align_bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))

    lines = [
        "🧬 <b>WALLET ALIGNMENT</b>",
        f"Your wallet: <code>{my_wallet}</code>",
        _divider(),
        f"Alignment: <b>{pct:.0f}%</b>  [{align_bar}]",
        f"Matching signals: <b>{n_overlap}</b>",
    ]

    # Shared positions (you agree with smart money)
    shared = data.get("shared") or []
    if shared:
        lines += ["", "✅ <b>SHARED WITH SMART MONEY</b>"]
        for s in shared[:5]:
            lines.append(
                f"• {_esc(s.title[:60])}  —  {_esc(s.outcome)}\n"
                f"  {s.wallets} wallets  ·  Score {s.score:.0f}/100  ·  Edge {s.edge:+.3f}"
            )

    # Missing high-consensus positions
    missing = data.get("missing") or []
    if missing:
        lines += ["", "⚠️ <b>HIGH-CONSENSUS POSITIONS YOU'RE MISSING</b>"]
        for i, s in enumerate(missing[:6], 1):
            lines.append(
                f"\n{i}. <b>{_esc(s.title[:70])}</b>\n"
                f"   {_esc(s.outcome)}  ·  Score {s.score:.0f}/100  ·  {s.wallets} wallets  ·  Edge {s.edge:+.3f}"
            )

    # Risky solo positions
    risky = data.get("risky") or []
    if risky:
        lines += ["", "🔴 <b>POSITIONS YOU HOLD ALONE (no smart-wallet overlap)</b>"]
        for p in risky[:4]:
            val = max(p.value, p.size * p.current_price)
            lines.append(f"• {_esc(p.title[:60])}  —  {_esc(p.outcome)}  ·  {_money(val)}")

    await _send_or_edit(update, "\n".join(lines), alpha_menu())


# ── /mywallet ─────────────────────────────────────────────────────────────────

async def mywallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        current = get_alpha_setting("my_wallet")
        text = (
            f"👛 Current wallet: <code>{current or 'not set'}</code>\n\n"
            "To set: <code>/mywallet 0xYourWallet</code>"
        )
        await update.message.reply_text(text, parse_mode="HTML")
        return
    wallet = context.args[0].strip().lower()
    if not wallet.startswith("0x") or len(wallet) < 20:
        await update.message.reply_text("Invalid wallet. Use a 0x EVM address.")
        return
    set_alpha_setting("my_wallet", wallet)
    await update.message.reply_text(
        f"✅ Wallet set to:\n<code>{wallet}</code>\n\n"
        f"<a href=\"{_profile_url(wallet)}\">View on Polymarket</a>\n\n"
        "Now use /portfolio or /compare",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ── /scan_wallets ──────────────────────────────────────────────────────────────

async def scan_wallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        f"🔎 <b>Smart Wallet Scan Starting</b>\n"
        f"Board: <code>{category}/{time_period}/{order_by}</code>  ·  Limit: <b>{limit}</b>\n"
        "Fetching leaderboard → scoring → building consensus…",
        parse_mode="HTML",
    )
    engine = SmartMoneyEngine()
    res = await engine.discover_from_leaderboards(category, time_period, order_by, limit=limit, score_top=min(75, limit))

    if res.get("status") != "ok":
        await update.message.reply_text(
            f"⚠️ Scan failed: <code>{_esc(res.get('error'))}</code>",
            parse_mode="HTML",
        )
        return

    scores = res.get("scores") or []
    top3 = scores[:3]
    lines = [
        "✅ <b>Scan Complete</b>",
        _divider(),
        f"Wallets found: <b>{res['wallets_found']}</b>",
        f"Scored: <b>{res['wallets_scored']}</b>",
        f"Consensus signals: <b>{res.get('consensus', 0)}</b>",
        f"Whale alerts: <b>{res.get('whale_alerts', 0)}</b>",
    ]
    if top3:
        lines += ["", "🏆 <b>Top wallets from this scan:</b>"]
        for s in top3:
            lines.append(
                f"• {_wallet_link(s.wallet)} — <b>{s.score:.1f}/100</b>"
                f"  ROI {_pct(s.roi)}  PnL {_money(s.pnl)}"
            )
    lines.append("\nNext: /topwallets · /consensus · /terminal")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=alpha_menu(),
                                    disable_web_page_preview=True)


# ── /alpha_scan_all ────────────────────────────────────────────────────────────

async def alpha_scan_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_alpha_tables()
    await update.message.reply_text(
        "🛰 <b>Multi-Board Discovery Scan</b>\n"
        "Scanning 6 leaderboards (OVERALL, CRYPTO, POLITICS · MONTH/ALL/WEEK · PNL/VOL).\n"
        "This takes 1–2 minutes…",
        parse_mode="HTML",
    )
    res = await SmartMoneyEngine().discover_multi_leaderboards(limit_per_board=75, score_top=100)
    scores = res.get("scores") or []
    lines = [
        "✅ <b>Multi-Board Scan Complete</b>" if res.get("status") == "ok" else "⚠️ <b>Scan Finished (with errors)</b>",
        _divider(),
        f"Unique wallets: <b>{res.get('wallets_found', 0)}</b>",
        f"Scored: <b>{res.get('wallets_scored', 0)}</b>",
        f"Consensus signals: <b>{res.get('consensus', 0)}</b>",
        f"Whale alerts: <b>{res.get('whale_alerts', 0)}</b>",
    ]
    if scores:
        lines += ["", "🏆 <b>Top wallets:</b>"]
        for s in scores[:5]:
            lines.append(f"• {_wallet_link(s.wallet)} — <b>{s.score:.1f}/100</b>  ROI {_pct(s.roi)}  PnL {_money(s.pnl)}")
    if res.get("errors"):
        lines.append(f"\n<i>{len(res['errors'])} board(s) failed but scan continued.</i>")
    lines.append("\nNext: /topwallets · /consensus · /terminal")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=alpha_menu(),
                                    disable_web_page_preview=True)


# ── /leaderboard ───────────────────────────────────────────────────────────────

async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scans = latest_alpha_scans(5)
    lines = [
        "📡 <b>SCANNER STATUS</b>",
        f"Discovered wallets: <b>{discovered_wallet_count()}</b>",
        _divider(),
    ]
    if not scans:
        lines.append("\nNo scans yet. Use /scan_wallets or /alpha_scan_all")
    for srow in scans:
        status_emoji = "✅" if srow.get("status") == "ok" else "⚠️"
        lines.append(
            f"\n{status_emoji} <b>{srow['source']}</b> {srow['category']}/{srow['time_period']}/{srow['order_by']}\n"
            f"Found: {srow['wallets_found']}  ·  Scored: {srow['wallets_scored']}"
            + (f"  ·  Top: {short_wallet(srow.get('top_wallet') or '')} {(srow.get('top_score') or 0):.1f}/100" if srow.get("top_wallet") else "")
            + f"\n<i>{_esc(srow['created_at'][:16])}</i>"
        )
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


# ── wallet management ──────────────────────────────────────────────────────────

async def alpha_addwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: <code>/alpha_addwallet 0xWallet optional label</code>", parse_mode="HTML")
        return
    wallet = context.args[0].strip().lower()
    label = " ".join(context.args[1:]).strip()
    if not wallet.startswith("0x") or len(wallet) < 20:
        await update.message.reply_text("Invalid wallet. Use a 0x EVM wallet address.")
        return
    add_alpha_wallet(wallet, label)
    await update.message.reply_text(
        f"✅ Added smart wallet {_wallet_link(wallet)}" + (f" — {_esc(label)}" if label else ""),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def alpha_removewallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /alpha_removewallet 0xWallet")
        return
    n = remove_alpha_wallet(context.args[0])
    await update.message.reply_text("✅ Removed wallet." if n else "Wallet not found.")


async def alpha_wallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_alpha_wallets(limit=100)
    scores = {s["wallet"]: s for s in top_saved_wallet_scores(100)}
    if not rows:
        text = "📋 <b>Tracked Smart Wallets</b>\n\nNone yet.\n<code>/alpha_addwallet 0xWallet label</code>"
    else:
        lines = [f"📋 <b>Tracked Smart Wallets</b>  ({len(rows)} total)"]
        for i, (w, label) in enumerate(rows[:20], 1):
            s = scores.get(w)
            score_str = f" — <b>{s['score']:.1f}/100</b>" if s else ""
            lines.append(
                f"\n{i}. {_wallet_link(w, label or None)}{score_str}\n"
                f"<code>{_esc(w)}</code>"
            )
        if len(rows) > 20:
            lines.append(f"\n<i>…and {len(rows) - 20} more</i>")
        text = "\n".join(lines)
    await _send_or_edit(update, text, alpha_menu())


async def alpha_help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 <b>PolyAlpha Commands</b>\n\n"
        "<b>Scanning</b>\n"
        "/scan_wallets [CAT] [PERIOD] [ORDER] [N] — leaderboard scan\n"
        "/alpha_scan_all — scan 6 boards at once\n\n"
        "<b>Intelligence</b>\n"
        "/terminal — dashboard\n"
        "/topwallets — ranked wallets with score breakdown\n"
        "/consensus — cached consensus signals\n"
        "/consensus_refresh — rebuild from cached positions\n"
        "/topsignals — same as /consensus\n"
        "/feed — whale activity + consensus feed\n"
        "/whales — whale position tracker\n\n"
        "<b>My Portfolio</b>\n"
        "/mywallet 0x... — set your wallet\n"
        "/portfolio — analyze your positions\n"
        "/compare — alignment vs smart money\n\n"
        "<b>Wallet Management</b>\n"
        "/alpha_addwallet 0x... label\n"
        "/alpha_removewallet 0x...\n"
        "/alpha_wallets — list tracked\n\n"
        "<b>Disabled</b>\n"
        "/buy /sell /close /closeall — real trading not implemented"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ── disabled trading ───────────────────────────────────────────────────────────

async def trade_disabled_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔒 Real trading is not implemented.\n"
        "/buy /sell /close /closeall require CONFIRM code and slippage checks — coming later."
    )


# ── /topmarkets / /topsignals ─────────────────────────────────────────────────

async def topmarkets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await consensus_cmd(update, context)


# ── callback router ────────────────────────────────────────────────────────────

async def alpha_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data

    if data == "alpha":
        return await alpha_start_cmd(update, context)
    if data == "alpha_actionable":
        return await actionable_cmd(update, context)
    if data == "alpha_quality":
        return await quality_cmd(update, context)
    if data == "alpha_terminal":
        return await terminal_cmd(update, context)
    if data == "alpha_consensus":
        return await consensus_cmd(update, context)
    if data == "alpha_topwallets":
        return await topwallets_cmd(update, context)
    if data == "alpha_feed":
        return await feed_cmd(update, context)
    if data == "alpha_whales":
        return await whales_cmd(update, context)
    if data == "alpha_wallets":
        return await alpha_wallets_cmd(update, context)
    if data == "alpha_compare":
        return await compare_cmd(update, context)
    if data == "alpha_portfolio":
        return await portfolio_cmd(update, context)
    if data == "alpha_settings":
        return await _send_or_edit(
            update,
            "⚙️ <b>Alpha Settings</b>\n\n"
            "Real trading: <b>OFF</b>\n"
            "Consensus floor: 40/100 wallet score\n"
            "Min wallets for signal: 2\n"
            "Min signal value: $50\n"
            "Background scan: every 6h (when /scan_wallets or scheduler runs)",
            alpha_menu(),
        )
    if data == "alpha_add_hint":
        await update.callback_query.answer()
        return await update.callback_query.message.reply_text(
            "Use: <code>/alpha_addwallet 0xWallet optional label</code>",
            parse_mode="HTML",
        )
    if data == "alpha_scan":
        q = update.callback_query
        await q.answer()
        await q.message.reply_text(
            "🔎 <b>Starting Smart Wallet Scan</b>\n"
            "Board: OVERALL/MONTH/PNL  ·  Limit: 50",
            parse_mode="HTML",
        )
        res = await SmartMoneyEngine().discover_from_leaderboards("OVERALL", "MONTH", "PNL", limit=50, score_top=50)
        if res.get("status") != "ok":
            return await q.message.reply_text(
                f"⚠️ Scan failed: <code>{_esc(res.get('error'))}</code>",
                parse_mode="HTML",
                reply_markup=alpha_menu(),
            )
        scores = res.get("scores") or []
        lines = [
            "✅ <b>Scan Complete</b>",
            f"Wallets: <b>{res.get('wallets_found', 0)}</b>  ·  Consensus: <b>{res.get('consensus', 0)}</b>",
        ]
        for s in scores[:3]:
            lines.append(f"• {_wallet_link(s.wallet)} — <b>{s.score:.1f}/100</b>  {_pct(s.roi)}")
        lines.append("\nNext: /topwallets · /consensus")
        return await q.message.reply_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=alpha_menu(), disable_web_page_preview=True
        )


# ── registration ──────────────────────────────────────────────────────────────

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
    app.add_handler(CommandHandler("actionable", actionable_cmd))
    app.add_handler(CommandHandler("alpha_score", actionable_cmd))
    app.add_handler(CommandHandler("quality", quality_cmd))
    app.add_handler(CommandHandler("feed", feed_cmd))
    app.add_handler(CommandHandler("topwallets", topwallets_cmd))
    app.add_handler(CommandHandler("consensus", consensus_cmd))
    app.add_handler(CommandHandler("consensus_refresh", consensus_refresh_cmd))
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
