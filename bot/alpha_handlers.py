"""Telegram UI + commands for PolyAlpha Terminal v3.0.
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
from bot.wallet_history import (
    actionable_signals,
    alpha_score_from_signal,
    heatmap_data,
    market_url,
    research_market,
    signal_quality_summary,
    ensure_history_tables,
)
from bot.time_utils import timestamp_with_seconds
from bot.intelligence_v4 import (
    ensure_v4_tables, latest_position_events, wallet_history_summary,
    build_wallet_clusters, signal_backtest_summary,
)


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

def _bankroll() -> float:
    try:
        return max(1.0, float(get_alpha_setting("bankroll") or 100.0))
    except Exception:
        return 100.0


def _kelly_binary(prob: float, price: float, bankroll: float | None = None) -> dict:
    """Conservative Kelly for binary shares that pay $1 if right.
    Full Kelly fraction of bankroll: (p - price) / (1 - price).
    We use 25% Kelly and hard cap at 2% to avoid overbetting noisy signals.
    """
    bankroll = bankroll or _bankroll()
    p = max(0.0, min(0.999, float(prob or 0)))
    c = max(0.001, min(0.999, float(price or 0)))
    edge = p - c
    full = max(0.0, edge / max(0.001, 1.0 - c))
    quarter = full * 0.25
    capped = min(quarter, 0.02)
    dollars = max(0.0, bankroll * capped)
    if edge < 0.04:
        verdict = "NO TRADE"
        dollars = 0.0
        capped = 0.0
    elif dollars < 1.0:
        verdict = "WATCH"
    else:
        verdict = "TRADE SMALL" if capped <= 0.01 else "TRADE"
    return {"prob": p, "price": c, "edge": edge, "full_kelly": full, "safe_kelly": capped, "dollars": dollars, "verdict": verdict}


def _kelly_line(prob: float, price: float) -> str:
    k = _kelly_binary(prob, price)
    return (
        f"Kelly: <b>{k['verdict']}</b> · safe {(k['safe_kelly']*100):.2f}% "
        f"= <b>${k['dollars']:.2f}</b> on ${_bankroll():.0f} bankroll"
    )


def _esc(s: object) -> str:
    return html.escape(str(s or ""))


def _profile_url(wallet: str) -> str:
    w = str(wallet or "").strip().lower()
    return f"https://polymarket.com/profile/{w}" if w.startswith("0x") else "https://polymarket.com"


def _wallet_link(wallet: str, label: str | None = None) -> str:
    w = str(wallet or "").strip().lower()
    text = label or short_wallet(w)
    return f'<a href="{_profile_url(w)}">{_esc(text)}</a>' if w.startswith("0x") else _esc(text)




def _market_link(market: str, label: str = "Open market") -> str:
    return f'<a href="{market_url(market)}">{_esc(label)}</a>'

def _alpha_tier(score: float) -> str:
    if score >= 85:
        return "⭐⭐⭐⭐⭐ Elite"
    if score >= 75:
        return "⭐⭐⭐⭐ Strong"
    if score >= 65:
        return "⭐⭐⭐ Moderate"
    if score >= 55:
        return "⭐⭐ Watch"
    return "⭐ Ignore"

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
    """Minimal v3.2 UI: fewer buttons, clearer flow."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖥 Terminal", callback_data="alpha_terminal"),
         InlineKeyboardButton("🎯 Picks", callback_data="alpha_actionable")],
        [InlineKeyboardButton("🔎 Scan", callback_data="alpha_scan"),
         InlineKeyboardButton("📡 Feed", callback_data="alpha_feed")],
        [InlineKeyboardButton("🏆 Wallets", callback_data="alpha_topwallets"),
         InlineKeyboardButton("👛 My Wallet", callback_data="alpha_portfolio")],
        [InlineKeyboardButton("🧪 Quality", callback_data="alpha_quality"),
         InlineKeyboardButton("🧠 V4 Intel", callback_data="alpha_v4")],
        [InlineKeyboardButton("⬅️ Home", callback_data="home")],
    ])


def alpha_advanced_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Consensus", callback_data="alpha_consensus"),
         InlineKeyboardButton("📚 Research", callback_data="alpha_research")],
        [InlineKeyboardButton("🗺 Heatmap", callback_data="alpha_heatmap"),
         InlineKeyboardButton("🐋 Whales", callback_data="alpha_whales")],
        [InlineKeyboardButton("🧬 Compare", callback_data="alpha_compare"),
         InlineKeyboardButton("📊 Alpha Score", callback_data="alpha_alpha_score")],
        [InlineKeyboardButton("⬅️ Main Alpha Menu", callback_data="alpha")],
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

    # Decision-first top signal: prefer positive-edge actionable alpha over raw consensus.
    act_top = actionable_signals(1, min_alpha=0)
    if act_top:
        t = act_top[0]
        edge_str = f"{(t.get('edge') or 0):+.3f}"
        alpha = t.get("alpha_score") or 0
        lines += [
            "",
            "🚀 <b>TOP ACTIONABLE ALPHA</b>",
            f"<b>{_esc(t.get('title', '')[:90])}</b>",
            f"Outcome: <b>{_esc(t.get('outcome'))}</b>  |  Alpha: <b>{alpha:.0f}/100</b>  |  {_alpha_tier(alpha)}",
            f"Wallets: <b>{t.get('wallets')}</b>  ·  Value: <b>{_money(t.get('total_value') or 0)}</b>",
            f"Price: <code>{(t.get('avg_price') or 0):.3f}</code>  →  Fair: <code>{(t.get('fair_value') or 0):.3f}</code>  ·  Edge: <b>{edge_str}</b>",
            f"{_market_link(t.get('market',''), '🔗 Open market')}",
        ]
    elif cached:
        t = cached[0]
        edge_str = f"{(t.get('edge') or 0):+.3f}"
        lines += [
            "",
            "🔥 <b>TOP CONSENSUS SIGNAL</b>",
            f"<b>{_esc(t.get('title', '')[:90])}</b>",
            f"Outcome: <b>{_esc(t.get('outcome'))}</b>  |  Signal: <b>{(t.get('score') or 0):.0f}/100</b>  |  {_conf_emoji(t.get('confidence',''))} {_esc(t.get('confidence'))}",
            f"Wallets: <b>{t.get('wallets')}</b>  ·  Value: <b>{_money(t.get('total_value') or 0)}</b>",
            f"Avg price: <code>{(t.get('avg_price') or 0):.3f}</code>  ·  Fair: <code>{(t.get('fair_value') or 0):.3f}</code>  ·  Edge: <b>{edge_str}</b>",
            "⚠️ No positive-edge actionable signal found yet.",
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
    saved = top_saved_wallet_scores(8)
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



# ── /actionable /alpha_score /quality /research /heatmap ──────────────────────

async def actionable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # v3.2: this is the main "what should I look at" page. Strict, short, decision-first.
    rows = actionable_signals(limit=5, min_alpha=70)
    lines = [
        "🎯 <b>POLYALPHA PICKS</b>",
        "<code>strict · positive edge · Kelly-sized</code>",
        _divider(),
    ]
    if not rows:
        lines += [
            "",
            "No trade-quality signal right now.",
            "This is good: the bot should wait instead of forcing weak trades.",
            "Run /scan_wallets or /alpha_scan_all later.",
        ]
    for i, r in enumerate(rows, 1):
        alpha = r.get("alpha_score") or 0
        edge = r.get("edge") or 0
        price = float(r.get('avg_price') or 0)
        fair = float(r.get('fair_value') or 0)
        comps = r.get("alpha_components") or {}
        k = _kelly_binary(fair, price)
        decision = k["verdict"]
        if alpha < 75:
            decision = "WATCH" if decision != "NO TRADE" else decision
        lines.append(
            f"\n<b>{i}. {_esc(r.get('title','')[:82])}</b>\n"
            f"Side: <b>{_esc(r.get('outcome'))}</b> · Decision: <b>{decision}</b> · Alpha <b>{alpha:.0f}/100</b>\n"
            f"Price <code>{price:.3f}</code> → Fair <code>{fair:.3f}</code> · Edge <b>{edge:+.3f}</b>\n"
            f"Smart money: <b>{r.get('wallets')}</b> wallets · <b>{_money(r.get('total_value') or 0)}</b> · avg score {(r.get('avg_wallet_score') or 0):.0f}\n"
            f"{_kelly_line(fair, price)}\n"
            f"Why: C{comps.get('consensus',0):.0f} Q{comps.get('wallet_quality',0):.0f} ${comps.get('capital',0):.0f} E{comps.get('edge',0):.0f}\n"
            f"{_market_link(r.get('market',''), '🔗 Market')} · <code>{_esc(r.get('market',''))}</code>"
        )
    lines.append(f"\n{_divider()}\nSet bankroll: <code>/bankroll 250</code> · Details: <code>/research slug</code>")
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def alpha_score_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = actionable_signals(limit=10, min_alpha=0)
    lines = ["📈 <b>ALPHA SCORE RANKING</b>", "<code>signal score components</code>", _divider()]
    if not rows:
        lines.append("\nNo signals yet. Run /scan_wallets.")
    for i, r in enumerate(rows[:10], 1):
        comps = r.get("alpha_components") or {}
        lines.append(
            f"\n<b>{i}. {_esc(r.get('title','')[:65])}</b>\n"
            f"{_esc(r.get('outcome'))} · Alpha <b>{(r.get('alpha_score') or 0):.0f}/100</b> · Edge {(r.get('edge') or 0):+.3f}\n"
            f"C:{comps.get('consensus',0):.0f} Q:{comps.get('wallet_quality',0):.0f} $:{comps.get('capital',0):.0f} E:{comps.get('edge',0):.0f} Penalty:{comps.get('penalty',0):.0f}"
        )
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def quality_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = signal_quality_summary()
    lines = [
        "🧪 <b>QUALITY LAB</b>",
        "<code>is the smart-money cache tradable?</code>",
        _divider(),
        f"Wallet scores: <b>{q['wallets']}</b> · Avg score: <b>{q['avg_wallet_score']}/100</b>",
        f"Elite 80+: <b>{q['elite']}</b> · Strong 65-80: <b>{q['strong']}</b> · Good 50-65: <b>{q['good']}</b> · Weak: <b>{q['weak']}</b>",
        "",
        f"Consensus cached: <b>{q['consensus']}</b>",
        f"Actionable positive-edge: <b>{q['actionable']}</b>",
        f"Negative-edge hidden from /actionable: <b>{q['negative_edge']}</b>",
        f"Old/outright markets hidden: <b>{q.get('stale_or_outright_hidden', 0)}</b>",
        f"Avg signal wallets: <b>{q['avg_signal_wallets']}</b>",
        f"Avg signal value: <b>{_money(q['avg_signal_value'])}</b>",
        "",
        "Recommended: /alpha_scan_all for more wallets, then /actionable.",
    ]
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def heatmap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = heatmap_data()
    lines = ["🗺 <b>SMART MONEY HEATMAP</b>", "<code>where high-score wallets cluster</code>", _divider()]
    if not rows:
        lines.append("\nNo heatmap yet. Run /scan_wallets.")
    max_val = max([x.get('value', 0) for x in rows] or [1])
    for r in rows:
        bar_len = max(1, int((r.get('value',0) / max(1, max_val)) * 10))
        bar = "█" * bar_len + "░" * (10 - bar_len)
        lines.append(
            f"\n<b>{_esc(r['category'])}</b> [{bar}]\n"
            f"Signals: {r['signals']} · Positive: {r['positive']} · Value: {_money(r['value'])} · Avg alpha: {r['avg_alpha']}/100"
        )
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def research_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args or []).strip()
    rows = research_market(query, limit=1)
    if not rows:
        await _send_or_edit(update, "📚 <b>Research</b>\n\nNo signal found. Try /research BTC or /actionable", alpha_menu())
        return
    r = rows[0]
    alpha = r.get('alpha_score') or 0
    comps = r.get('alpha_components') or {}
    edge = r.get('edge') or 0
    lines = [
        "📚 <b>MARKET RESEARCH</b>",
        _divider(),
        f"<b>{_esc(r.get('title',''))}</b>",
        f"Outcome: <b>{_esc(r.get('outcome'))}</b>",
        f"Category: <b>{_esc(r.get('category'))}</b>",
        f"Alpha Score: <b>{alpha:.0f}/100</b> — {_alpha_tier(alpha)}",
        "",
        f"Smart wallets: <b>{r.get('wallets')}</b>",
        f"Total value: <b>{_money(r.get('total_value') or 0)}</b>",
        f"Avg wallet score: <b>{(r.get('avg_wallet_score') or 0):.0f}/100</b>",
        f"Conviction: <b>{(r.get('weighted_conviction') or 0):.0f}/100</b>",
        "",
        f"Market price: <code>{(r.get('avg_price') or 0):.3f}</code>",
        f"Estimated fair: <code>{(r.get('fair_value') or 0):.3f}</code>",
        f"Edge: <b>{edge:+.3f}</b>",
        "",
        f"Score parts: consensus {comps.get('consensus',0):.0f}, wallet quality {comps.get('wallet_quality',0):.0f}, capital {comps.get('capital',0):.0f}, edge {comps.get('edge',0):.0f}, penalty {comps.get('penalty',0):.0f}",
        "",
        f"{_market_link(r.get('market',''), '🔗 Open market')}",
        f"<code>{_esc(r.get('market',''))}</code>",
    ]
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
            f"Conviction: <b>{conv:.0f}/100</b>"
            + ("\n⚠️ <i>Negative edge: hidden from /actionable</i>" if edge <= 0 else "")
            + f"\n{_market_link(srow.get('market',''), '🔗 Open market')} · <code>{_esc(srow.get('market',''))}</code>"
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


# ── bankroll / Kelly sizing ───────────────────────────────────────────────────

async def bankroll_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        try:
            val = float(context.args[0].replace("$", "").replace(",", ""))
            if val <= 0:
                raise ValueError
            set_alpha_setting("bankroll", str(val))
            await update.message.reply_text(f"✅ Bankroll set to ${val:,.2f}. Use /picks or /kelly.")
            return
        except Exception:
            await update.message.reply_text("Usage: /bankroll 250")
            return
    await update.message.reply_text(f"Current bankroll: ${_bankroll():,.2f}\nSet with: /bankroll 250")


async def kelly_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /kelly price fair [bankroll]
    if len(context.args or []) >= 2:
        try:
            price = float(context.args[0])
            fair = float(context.args[1])
            bankroll = float(context.args[2]) if len(context.args) >= 3 else _bankroll()
            k = _kelly_binary(fair, price, bankroll)
            await update.message.reply_text(
                f"📐 Kelly sizing\n"
                f"Price: {price:.3f} · Fair probability: {fair:.3f}\n"
                f"Edge: {k['edge']:+.3f}\n"
                f"Full Kelly: {k['full_kelly']*100:.2f}%\n"
                f"Safe Kelly: {k['safe_kelly']*100:.2f}%\n"
                f"Stake: ${k['dollars']:.2f} on ${bankroll:.2f} bankroll\n"
                f"Verdict: {k['verdict']}"
            )
            return
        except Exception:
            pass
    rows = actionable_signals(limit=3, min_alpha=70)
    if not rows:
        await update.message.reply_text("No Kelly-ready picks right now. Use /scan_wallets then /picks.")
        return
    lines = ["📐 <b>KELLY SIZING</b>", f"Bankroll: <b>${_bankroll():,.2f}</b>", _divider()]
    for r in rows:
        price = float(r.get('avg_price') or 0)
        fair = float(r.get('fair_value') or 0)
        lines.append(f"\n<b>{_esc(r.get('title','')[:70])}</b>\n{_esc(r.get('outcome'))} · {_kelly_line(fair, price)}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚙️ <b>SETTINGS</b>\n"
        f"Bankroll: <b>${_bankroll():,.2f}</b>\n"
        "Mode: read-only intelligence\n"
        "UI: simplified v3.2\n\n"
        "Commands you actually need:\n"
        "/picks · /scan_wallets · /terminal · /topwallets · /feed · /bankroll"
    )
    await _send_or_edit(update, text, alpha_menu())


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
    if data == "alpha_terminal":
        return await terminal_cmd(update, context)
    if data == "alpha_consensus":
        return await consensus_cmd(update, context)
    if data == "alpha_actionable":
        return await actionable_cmd(update, context)
    if data == "alpha_quality":
        return await quality_cmd(update, context)
    if data == "alpha_heatmap":
        return await heatmap_cmd(update, context)
    if data == "alpha_research":
        return await research_cmd(update, context)
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
    if data == "alpha_v4":
        return await v4_cmd(update, context)
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

# ── PolyAlpha Intelligence v4 ─────────────────────────────────────────────────

async def v4_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_v4_tables()
    events = latest_position_events(5)
    clusters = build_wallet_clusters()[:4]
    back = signal_backtest_summary()
    lines = [
        "🧠 <b>POLYALPHA INTELLIGENCE v4</b>",
        "<code>history · clusters · backtest · wallet flows</code>",
        _divider(),
        "<b>What changed:</b>",
        "• Wallet position history is now stored",
        "• Adds/reduces/closes are detected after scans",
        "• Wallets are grouped into market clusters",
        "• Signal buckets are tracked for future backtesting",
    ]
    if clusters:
        lines += ["", "👥 <b>Top clusters</b>"]
        for c in clusters:
            lines.append(f"• <b>{_esc(c['cluster'])}</b>: {c['wallets']} wallets · avg {c['avg_score']}/100 · {_money(c['total_value'])}")
    if events:
        lines += ["", "📈 <b>Latest wallet flow</b>"]
        for e in events:
            delta = float(e.get('delta_value') or 0)
            sign = "+" if delta > 0 else ""
            lines.append(f"• {e['action']} {_wallet_link(e['wallet'])} · {sign}{_money(delta)} · {_esc(e['outcome'])} · <code>{_esc(e['market'])}</code>")
    if back:
        lines += ["", "🎯 <b>Signal buckets</b>"]
        for b in back:
            lines.append(f"• {b['bucket']}: {b['signals']} signals · avg alpha {b['avg_alpha']} · avg edge {b['avg_edge']:+.3f}")
    lines += ["", "Use /changes · /clusters · /backtest · /history 0xWallet"]
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def changes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_v4_tables()
    events = latest_position_events(15)
    lines = ["📈 <b>SMART WALLET FLOW</b>", "<code>adds · reduces · closes from scans</code>", _divider()]
    if not events:
        lines.append("No position changes recorded yet. Run /scan_wallets twice over time.")
    for e in events:
        delta = float(e.get('delta_value') or 0)
        sign = "+" if delta > 0 else ""
        lines.append(
            f"\n<b>{_esc(e['action'])}</b> {_wallet_link(e['wallet'])}\n"
            f"{_esc((e.get('title') or '')[:65])}\n"
            f"Outcome: <b>{_esc(e.get('outcome'))}</b> · Δ {sign}{_money(delta)} · px {(float(e.get('current_price') or 0)):.3f}\n"
            f"{_market_link(e.get('market',''))} · <code>{_esc(e.get('market',''))}</code>"
        )
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def clusters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clusters = build_wallet_clusters()
    lines = ["👥 <b>WALLET CLUSTERS</b>", "<code>where smart money specializes</code>", _divider()]
    if not clusters:
        lines.append("No clusters yet. Run /scan_wallets first.")
    max_val = max([c['total_value'] for c in clusters] or [1])
    for c in clusters[:8]:
        bars = max(1, min(10, int((c['total_value']/max_val)*10)))
        lines.append(f"\n<b>{_esc(c['cluster'])}</b> [{'█'*bars}{'░'*(10-bars)}]\nWallets: {c['wallets']} · Avg score: {c['avg_score']}/100 · Value: {_money(c['total_value'])}\nTop market: <code>{_esc(c.get('top_market','')[:45])}</code>")
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = signal_backtest_summary()
    lines = ["🎯 <b>SIGNAL BACKTEST LAB</b>", "<code>early v4 diagnostics · not live trading proof</code>", _divider()]
    if not rows:
        lines.append("No signal buckets yet. Run /scan_wallets first.")
    else:
        for r in rows:
            pos_rate = 100 * (r['positive_edge'] / max(1, r['signals']))
            lines.append(f"• <b>{_esc(r['bucket'])}</b>: {r['signals']} signals · +edge {pos_rate:.0f}% · avg alpha {r['avg_alpha']} · avg edge {r['avg_edge']:+.3f}")
        lines += ["", "Next v4 step: compare these buckets to real resolved outcomes over time."]
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallet = context.args[0].strip().lower() if context.args else ""
    data = wallet_history_summary(wallet if wallet.startswith('0x') else None, limit=10)
    title = f"🧾 <b>WALLET HISTORY</b> {_wallet_link(wallet)}" if wallet.startswith('0x') else "🧾 <b>GLOBAL WALLET HISTORY</b>"
    lines = [title, "<code>latest tracked position changes</code>", _divider()]
    totals = data.get('totals') or {}
    if totals:
        lines.append("<b>Totals</b>")
        for action, d in totals.items():
            lines.append(f"• {action}: {d['count']} · {_money(d['value'])}")
    events = data.get('events') or []
    if not events:
        lines.append("No history yet. Run scans over time so changes can be detected.")
    for e in events:
        delta = float(e.get('delta_value') or 0); sign = "+" if delta > 0 else ""
        lines.append(f"\n<b>{_esc(e['action'])}</b> {sign}{_money(delta)} · {_esc(e.get('outcome'))}\n{_esc((e.get('title') or '')[:70])}\n<code>{_esc(e.get('market',''))}</code>")
    await _send_or_edit(update, "\n".join(lines), alpha_menu())


def register_alpha_handlers(app):
    ensure_alpha_tables()
    ensure_history_tables()
    app.add_handler(CommandHandler("alpha", alpha_start_cmd))
    app.add_handler(CommandHandler("alpha_help", alpha_help_cmd))
    app.add_handler(CommandHandler("alpha_addwallet", alpha_addwallet_cmd))
    app.add_handler(CommandHandler("addwallet", alpha_addwallet_cmd))
    app.add_handler(CommandHandler("alpha_removewallet", alpha_removewallet_cmd))
    app.add_handler(CommandHandler("removewallet", alpha_removewallet_cmd))
    app.add_handler(CommandHandler("alpha_wallets", alpha_wallets_cmd))
    app.add_handler(CommandHandler("scan_wallets", scan_wallets_cmd))
    app.add_handler(CommandHandler("alpha_scan_all", alpha_scan_all_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("actionable", actionable_cmd))
    app.add_handler(CommandHandler("picks", actionable_cmd))
    app.add_handler(CommandHandler("signals", actionable_cmd))
    app.add_handler(CommandHandler("alpha_score", alpha_score_cmd))
    app.add_handler(CommandHandler("kelly", kelly_cmd))
    app.add_handler(CommandHandler("bankroll", bankroll_cmd))
    app.add_handler(CommandHandler("quality", quality_cmd))
    app.add_handler(CommandHandler("heatmap", heatmap_cmd))
    app.add_handler(CommandHandler("research", research_cmd))
    app.add_handler(CommandHandler("feed", feed_cmd))
    app.add_handler(CommandHandler("topwallets", topwallets_cmd))
    app.add_handler(CommandHandler("consensus", consensus_cmd))
    app.add_handler(CommandHandler("consensus_refresh", consensus_refresh_cmd))
    app.add_handler(CommandHandler("topsignals", consensus_cmd))
    app.add_handler(CommandHandler("topmarkets", topmarkets_cmd))
    app.add_handler(CommandHandler("terminal", terminal_cmd))
    app.add_handler(CommandHandler("v4", v4_cmd))
    app.add_handler(CommandHandler("changes", changes_cmd))
    app.add_handler(CommandHandler("clusters", clusters_cmd))
    app.add_handler(CommandHandler("backtest", backtest_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("mywallet", mywallet_cmd))
    app.add_handler(CommandHandler("setwallet", mywallet_cmd))
    app.add_handler(CommandHandler("portfolio", portfolio_cmd))
    app.add_handler(CommandHandler("wallet", portfolio_cmd))
    app.add_handler(CommandHandler("compare", compare_cmd))
    app.add_handler(CommandHandler("whales", whales_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("buy", trade_disabled_cmd))
    app.add_handler(CommandHandler("sell", trade_disabled_cmd))
    app.add_handler(CommandHandler("close", trade_disabled_cmd))
    app.add_handler(CommandHandler("closeall", trade_disabled_cmd))
    app.add_handler(CallbackQueryHandler(alpha_button, pattern="^alpha"))
