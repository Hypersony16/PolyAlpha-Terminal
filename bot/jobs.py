import logging
import time as time_module

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from bot.cache import cache
from bot.dashboard import (
    get_dashboard_ref,
    get_dashboard_last_refresh,
    set_dashboard_last_refresh,
    dashboard_refresh_seconds,
    live_dashboards_enabled,
)
from bot.db import (
    get_active_users,
    get_user_setting,
    get_tracked_wallets,
    get_own_wallet,
    log_wallet_snapshot,
    was_alert_sent_recently,
    mark_alert_sent,
    trade_exists,
    log_tracked_trade,
    get_recent_tracked_trades,
)
from bot.btc import build_btc_model
from bot.polymarket import discover_btc_15m_market, fetch_public_profile, clear_market_cache, fetch_market_resolution
from bot.wallet import fetch_wallet_total_value
from bot.trades import (
    fetch_wallet_trades,
    parse_trade_notification,
    detect_wallet_intelligence_message,
    score_wallet_from_rows,
)
from bot.time_utils import timestamp_with_seconds
from bot.stats import log_latency, resolve_due_predictions
from bot.paper_auto import paper_enabled, open_auto_trade, resolve_open_trades, due_open_market_slugs


async def get_btc_bundle():
    cached = cache.get("btc_bundle")
    if cached:
        return cached

    started = time_module.time()
    ok = True
    try:
        market = await discover_btc_15m_market()
    except Exception:
        market = {}
        ok = False
    log_latency("polymarket_market", started, ok)

    started = time_module.time()
    ok = True
    try:
        model = await build_btc_model(market)
        resolve_due_predictions(model["price"])
    except Exception:
        ok = False
        raise
    finally:
        log_latency("btc_model", started, ok)

    payload = (market, model)
    cache.set("btc_bundle", payload, ttl_seconds=1)
    return payload


def get_alerts_enabled(user_id: int) -> str:
    return get_user_setting(user_id, "alerts_enabled", "0") or "0"


def get_edge_threshold(user_id: int) -> float:
    try:
        return float(get_user_setting(user_id, "edge_threshold", "0.05"))
    except Exception:
        return 0.05


def get_notify_mode(user_id: int) -> str:
    return get_user_setting(user_id, "notify_mode", "normal") or "normal"


async def wallet_label_html(wallet: str, nickname: str = "") -> str:
    label = nickname if nickname else wallet
    try:
        profile = await fetch_public_profile(wallet)
        username = profile.get("username") or profile.get("name")
        if username:
            return f'<a href="https://polymarket.com/@{username}">{label}</a>'
    except Exception:
        pass
    return label


async def alerts_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        users = get_active_users(500)
        market, model = await get_btc_bundle()

        for user_row in users:
            user_id = int(user_row[0])
            chat_id = get_user_setting(user_id, "alerts_chat_id", "")
            if not chat_id or get_alerts_enabled(user_id) != "1":
                continue

            threshold = max(get_edge_threshold(user_id), 0.05)
            notify = get_notify_mode(user_id)
            edge = float(model.get("edge", 0))
            ev = float(model.get("ev_per_dollar", 0))

            # Never alert on fake fallback odds.
            if model.get("odds_source") == "fallback_50":
                continue

            if notify == "quiet":
                threshold = max(threshold, 0.10)
                if model.get("confidence") != "High":
                    continue

            if edge < threshold or ev < 0.04 or model.get("confidence") == "Low":
                continue

            # One alert per 15m window + side unless edge massively improves.
            best_temp = 1 if model["signal"] == "UP" else 0
            market_date = model["window"]["start"].isoformat()
            if was_alert_sent_recently(user_id, "btc15m", market_date, best_temp, edge):
                continue

            emoji = "🟢" if model["signal"] == "UP" else "🔴"
            text = (
                f"🚨 <b>BTC 15M EDGE ALERT</b>\n"
                f"🕒 {timestamp_with_seconds()}\n\n"
                f"BTC: ${model['price']:,.2f}\n"
                f"Signal: {emoji} <b>{model['signal']}</b>\n"
                f"Model: {model['model_prob']*100:.1f}%\n"
                f"Market: {model['market_prob']*100:.1f}% ({model.get('odds_source','?')})\n"
                f"Edge: +{edge*100:.1f} pts\n"
                f"EV: {ev*100:.1f}% per $1\n"
                f"Confidence: {model['confidence']}\n"
                f"Target: ${model.get('target_price', model.get('open', 0)):,.2f}\n"
                f"Time left: {model['window']['left_label']}\n"
                f"Size: {model['suggested_size_pct']:.2f}% bankroll"
            )

            await context.bot.send_message(
                chat_id=int(chat_id),
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            mark_alert_sent(user_id, "btc15m", market_date, best_temp, edge)

    except Exception as e:
        logging.exception(f"alerts_job failed: {e}")


async def wallet_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        users = get_active_users(500)
        for user_row in users:
            user_id = int(user_row[0])
            chat_id = get_user_setting(user_id, "alerts_chat_id", "")
            if not chat_id:
                continue

            wallets = get_tracked_wallets(user_id)
            own_wallet = get_own_wallet(user_id).strip()
            all_wallets = [w.get("address", "") for w in wallets if w.get("address")]
            if own_wallet:
                all_wallets.append(own_wallet)

            for wallet in all_wallets:
                try:
                    value = await fetch_wallet_total_value(wallet)
                    log_wallet_snapshot(user_id, wallet, value)
                except Exception:
                    continue
    except Exception as e:
        logging.exception(f"wallet_job failed: {e}")


async def wallet_trades_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        users = get_active_users(500)
        for user_row in users:
            user_id = int(user_row[0])
            chat_id = get_user_setting(user_id, "alerts_chat_id", "")
            if not chat_id:
                continue

            wallets = get_tracked_wallets(user_id)
            if not wallets:
                continue

            for item in wallets:
                wallet = item.get("address", "")
                nickname = item.get("nickname", "")
                if not wallet:
                    continue

                try:
                    trades = await fetch_wallet_trades(wallet, limit=20)
                except Exception:
                    continue

                for trade in trades:
                    tx_hash = str(
                        trade.get("transactionHash")
                        or trade.get("transaction_hash")
                        or trade.get("hash")
                        or trade.get("id")
                        or ""
                    )
                    if not tx_hash or trade_exists(user_id, wallet, tx_hash):
                        continue

                    parsed = parse_trade_notification(trade)
                    side = parsed.get("side", "")
                    outcome = parsed.get("outcome", "")
                    title = parsed.get("title", "")
                    size = parsed.get("size")
                    price = parsed.get("price")
                    ts = parsed.get("timestamp", 0)

                    log_tracked_trade(user_id, wallet, tx_hash, side, outcome, title, size, price, ts)

                    label = await wallet_label_html(wallet, nickname)
                    intelligence = detect_wallet_intelligence_message(trade)
                    score = score_wallet_from_rows(get_recent_tracked_trades(user_id, wallet, 30))

                    side_emoji = "🟢" if str(side).upper() in ("BUY", "YES", "UP") else "🔴"
                    msg = (
                        f"🐋 <b>Wallet trade detected</b>\n"
                        f"{label}\n\n"
                        f"{side_emoji} Side: <b>{side}</b>\n"
                        f"Outcome: <b>{outcome}</b>\n"
                        f"Market: {title}\n"
                        f"Size: {size}\n"
                        f"Price: {price}\n\n"
                        f"Smart score: {score.get('score', 0)}/100 — {score.get('label', 'Unknown')}"
                    )
                    if intelligence:
                        msg += f"\n\n{intelligence}"

                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("Copy preview", callback_data=f"copy_{tx_hash[:18]}"),
                         InlineKeyboardButton("Ignore", callback_data=f"ignore_{tx_hash[:18]}")]
                    ])

                    await context.bot.send_message(
                        chat_id=int(chat_id),
                        text=msg,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
    except Exception as e:
        logging.exception(f"wallet_trades_job failed: {e}")


async def live_dashboard_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        if not live_dashboards_enabled():
            return

        users = get_active_users(500)
        refresh_seconds = dashboard_refresh_seconds()
        now = time_module.time()

        for user_row in users:
            user_id = int(user_row[0])

            for kind in ("home", "btc", "paper"):
                ref = get_dashboard_ref(kind, user_id)
                if not ref:
                    continue

                last = get_dashboard_last_refresh(kind, user_id)
                if now - last < refresh_seconds:
                    continue

                try:
                    market, model = await get_btc_bundle()

                    if kind == "paper":
                        # Avoid importing handlers here; keep dashboard light.
                        text = (
                            f"🤖 <b>Paper Auto Live</b>\n"
                            f"🕒 {timestamp_with_seconds()}\n\n"
                            f"BTC: ${model['price']:,.2f}\n"
                            f"Signal: {model['signal']}\n"
                            f"Edge: {model['edge']*100:.1f} pts\n"
                            f"EV: {model.get('ev_per_dollar',0)*100:.1f}%\n"
                            f"Market: {model['market_prob']*100:.1f}%"
                        )
                    else:
                        text = (
                            f"₿ <b>BTC 15m Live</b>\n"
                            f"🕒 {timestamp_with_seconds()}\n\n"
                            f"BTC: ${model['price']:,.2f}\n"
                            f"Signal: {model['signal']}\n"
                            f"Model: {model['model_prob']*100:.1f}%\n"
                            f"Market: {model['market_prob']*100:.1f}% ({model.get('odds_source','?')})\n"
                            f"Edge: {model['edge']*100:.1f} pts\n"
                            f"EV: {model.get('ev_per_dollar',0)*100:.1f}%"
                        )

                    await context.bot.edit_message_text(
                        chat_id=int(ref["chat_id"]),
                        message_id=int(ref["message_id"]),
                        text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    set_dashboard_last_refresh(kind, user_id, now)
                except BadRequest as e:
                    if "message is not modified" in str(e).lower():
                        set_dashboard_last_refresh(kind, user_id, now)
                except Exception:
                    continue
    except Exception as e:
        logging.exception(f"live_dashboard_job failed: {e}")


async def paper_auto_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Automatic PAPER trading only. No real orders.
    """
    try:
        users = get_active_users(500)
        market, model = await get_btc_bundle()

        for user_row in users:
            user_id = int(user_row[0])
            chat_id = get_user_setting(user_id, "alerts_chat_id", "")
            if not chat_id or not paper_enabled(user_id):
                continue

            # Resolve with official Polymarket outcome only.
            due_slugs = due_open_market_slugs(user_id)
            official_resolutions = {}
            for slug in due_slugs:
                try:
                    official_resolutions[slug] = await fetch_market_resolution(slug)
                except Exception as e:
                    logging.exception(f"official resolution fetch failed for {slug}: {e}")
                    official_resolutions[slug] = {"resolved": False, "outcome": None, "source": "fetch_error", "slug": slug}

            # If a trade is due but official result is still not available, don't close it.
            # Send a low-frequency debug message so you know it is waiting, not broken.
            if due_slugs and not any(v.get("resolved") for v in official_resolutions.values()):
                last_debug = get_user_setting(user_id, "paper_resolution_debug_sent", "0")
                try:
                    last_debug_f = float(last_debug)
                except Exception:
                    last_debug_f = 0.0
                now_debug = time_module.time()
                if now_debug - last_debug_f > 300:
                    waiting_slug = due_slugs[0]
                    waiting_info = official_resolutions.get(waiting_slug, {})
                    await context.bot.send_message(
                        chat_id=int(chat_id),
                        text=(
                            f"⏳ <b>Waiting for official Polymarket result</b>\n"
                            f"Market: <code>{waiting_slug}</code>\n"
                            f"Source: <code>{waiting_info.get('source','unknown')}</code>\n"
                            f"Debug: <code>{str(waiting_info.get('debug',''))[:250]}</code>\n"
                            f"No local BTC guess will be used."
                        ),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    from bot.db import set_user_setting
                    set_user_setting(user_id, "paper_resolution_debug_sent", str(now_debug))

            resolved = resolve_open_trades(user_id, model["price"], official_resolutions)
            for item in resolved:
                emoji = "✅" if item["won"] else "❌"
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text=(
                        f"{emoji} <b>Paper trade closed</b>\n"
                        f"Market: <code>{item.get('market_slug','')}</code>\n"
                        f"Side: {item['side']} | Official result: {item['result']}\n"
                        f"Resolution: <code>{item.get('resolution_source','')}</code>\n"
                        f"Stake: ${item['stake']:.2f}\n"
                        f"Entry price: ${item['entry_price']:.3f}\n"
                        f"Shares: {item['shares']:.2f}\n"
                        f"Payout: ${item['payout']:.2f}\n"
                        f"PnL: ${item['pnl']:.2f}\n"
                        f"Target: ${item.get('target_price',0):,.2f}\n"
                        f"Entry BTC: ${item['entry_btc']:,.2f}\n"
                        f"Exit BTC: ${item['exit_btc']:,.2f}"
                    ),
                    parse_mode="HTML",
                )

            opened = open_auto_trade(user_id, model)
            if opened.get("opened"):
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text=(
                        f"🧪 <b>Auto paper trade opened</b>\n"
                        f"Market: <code>{opened.get('market_slug','')}</code>\n"
                        f"Side: {opened['side']}\n"
                        f"Stake: ${opened['stake']:.2f}\n"
                        f"Entry price: ${opened['entry_price']:.3f}\n"
                        f"Shares: {opened['shares']:.2f}\n"
                        f"Cost incl. slippage/fees: ${opened['cost']:.2f}\n"
                        f"Expected EV: ${opened['ev']:.2f}\n"
                        f"Odds: {model.get('odds_source','?')} @ ${opened['entry_price']:.3f}\n"
                        f"Target: ${model.get('target_price', model.get('open', 0)):,.2f}\n"
                        f"Edge: {model['edge']*100:.1f} pts\n"
                        f"Confidence: {model['confidence']}\n"
                        f"Time left: {model['window']['left_label']}\n"
                        f"Settlement: official Polymarket outcome only\n"
                        f"Mode: stricter consistency filter"
                    ),
                    parse_mode="HTML",
                )

    except Exception as e:
        logging.exception(f"paper_auto_job failed: {e}")


async def cache_clear_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        cache.clear()
        clear_market_cache()
        logging.info("background cache cleared")
    except Exception as e:
        logging.exception(f"cache_clear_job failed: {e}")


async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        users = get_active_users(500)
        market, model = await get_btc_bundle()

        for user_row in users:
            user_id = int(user_row[0])
            chat_id = get_user_setting(user_id, "alerts_chat_id", "")
            if not chat_id:
                continue

            await context.bot.send_message(
                chat_id=int(chat_id),
                text=(
                    f"📌 <b>Daily BTC Bot Summary</b>\n"
                    f"🕒 {timestamp_with_seconds()}\n\n"
                    f"BTC: ${model['price']:,.2f}\n"
                    f"Current signal: {model['signal']}\n"
                    f"Model: {model['model_prob']*100:.1f}%\n"
                    f"Market: {model['market_prob']*100:.1f}% ({model.get('odds_source','?')})\n"
                    f"Edge: {model['edge']*100:.1f} pts\n"
                    f"EV: {model.get('ev_per_dollar',0)*100:.1f}%"
                ),
                parse_mode="HTML",
            )
    except Exception as e:
        logging.exception(f"daily_summary_job failed: {e}")
