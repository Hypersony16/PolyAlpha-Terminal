import logging
import time as time_module

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from bot.cache import cache
from bot.market_tick import set_latest_tick, clear_latest_tick
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
from bot.paper_auto import paper_enabled, resolve_open_trades, due_open_market_slugs
from bot.scalp import open_scalp_or_resolution_trade, resolve_scalp_trades, get_strategy_mode


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

    bundle = {"market": market, "model": model}
    cache.set("btc_bundle", bundle, ttl=4)
    return bundle


async def alerts_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        bundle = await get_btc_bundle()
        model = bundle["model"]
        market = bundle["market"]
    except Exception as e:
        logging.warning(f"alerts_job: failed to get bundle: {e}")
        return

    signal = model.get("signal", "")
    edge = float(model.get("edge", 0) or 0)
    confidence = model.get("confidence", "Low")
    price = model.get("price", 0)
    time_left = model.get("time_left_seconds", 0)

    if signal not in ("UP", "DOWN"):
        return
    if edge < 0.12:
        return
    if confidence == "Low":
        return
    if time_left < 120:
        return

    alert_key = f"signal_{signal}_{model.get('window_start', '')}"
    users = get_active_users()

    for user_id in users:
        alerts_on = get_user_setting(user_id, "alerts_enabled", "1") == "1"
        if not alerts_on:
            continue
        if was_alert_sent_recently(user_id, alert_key, within_seconds=600):
            continue

        signal_emoji = "🟢" if signal == "UP" else "🔴"
        text = (
            f"{signal_emoji} <b>Alert: {signal}</b>\n\n"
            f"BTC: ${price:,.2f}\n"
            f"Edge: {edge:.1%} | Confidence: {confidence}\n"
            f"Time left: {time_left // 60}m {time_left % 60}s\n\n"
            f"<i>{timestamp_with_seconds()}</i>"
        )
        try:
            await context.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
            mark_alert_sent(user_id, alert_key)
        except Exception as e:
            logging.warning(f"alerts_job: failed to send to {user_id}: {e}")


async def wallet_job(context: ContextTypes.DEFAULT_TYPE):
    users = get_active_users()
    for user_id in users:
        own_wallet = get_own_wallet(user_id)
        if not own_wallet:
            continue
        try:
            value = await fetch_wallet_total_value(own_wallet)
            log_wallet_snapshot(user_id, own_wallet, value)
        except Exception as e:
            logging.debug(f"wallet_job: {user_id}: {e}")


async def wallet_trades_job(context: ContextTypes.DEFAULT_TYPE):
    users = get_active_users()
    for user_id in users:
        wallets = get_tracked_wallets(user_id)
        if not wallets:
            continue
        for wallet, nickname in wallets:
            try:
                trades = await fetch_wallet_trades(wallet, limit=20)
                for trade in trades:
                    tx_hash = str(trade.get("transactionHash") or trade.get("id") or "")
                    if not tx_hash or trade_exists(tx_hash):
                        continue

                    side = str(trade.get("side", "")).upper()
                    outcome = str(trade.get("outcome") or trade.get("outcomeName") or "")
                    title = str(trade.get("title") or trade.get("marketTitle") or trade.get("question") or "")
                    size = float(trade.get("size") or trade.get("usdcSize") or 0)
                    price = float(trade.get("price") or 0)
                    ts = float(trade.get("timestamp") or trade.get("createdAt") or 0)

                    if not title:
                        continue

                    log_tracked_trade(user_id, wallet, tx_hash, side, outcome, title, size, price, ts)

                    notification = parse_trade_notification(trade)
                    if notification:
                        label = f" ({nickname})" if nickname else ""
                        text = f"👛 <b>Wallet{label}</b>\n{notification}"
                        try:
                            await context.bot.send_message(
                                chat_id=user_id, text=text, parse_mode="HTML"
                            )
                        except Exception:
                            pass
            except Exception as e:
                logging.debug(f"wallet_trades_job: {user_id} {wallet}: {e}")


async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE):
    users = get_active_users()
    for user_id in users:
        try:
            from bot.paper_auto import paper_auto_summary
            summary = paper_auto_summary(user_id)
            if summary["total_trades"] == 0:
                continue
            text = (
                f"📊 <b>Daily Summary</b>\n\n"
                f"Balance: ${summary['balance']:.2f}\n"
                f"Trades: {summary['total_trades']} | PnL: ${summary['total_pnl']:.2f}\n"
                f"Win rate: {summary['winrate']:.1f}%\n\n"
                f"<i>{timestamp_with_seconds()}</i>"
            )
            await context.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
        except Exception as e:
            logging.debug(f"daily_summary_job: {user_id}: {e}")


async def live_dashboard_job(context: ContextTypes.DEFAULT_TYPE):
    if not live_dashboards_enabled():
        return

    users = get_active_users()
    refresh_secs = dashboard_refresh_seconds()

    for user_id in users:
        for kind in ["btc", "market"]:
            ref = get_dashboard_ref(kind, user_id)
            if not ref:
                continue
            last = get_dashboard_last_refresh(kind, user_id)
            if time_module.time() - last < refresh_secs:
                continue

            try:
                bundle = await get_btc_bundle()
                model = bundle["model"]
                market = bundle["market"]

                if kind == "btc":
                    from bot.btc import format_btc_price
                    from bot.menus import btc_menu
                    price = model.get("price", 0)
                    signal = model.get("signal", "?")
                    edge = model.get("edge", 0)
                    confidence = model.get("confidence", "?")
                    up_price = model.get("up_price", 0)
                    down_price = model.get("down_price", 0)
                    time_left = model.get("time_left_seconds", 0)
                    phase = model.get("phase", "?")
                    signal_emoji = "🟢" if signal == "UP" else "🔴" if signal == "DOWN" else "⚪"
                    text = (
                        f"₿ <b>BTC 15m Signal</b>\n\n"
                        f"Price: <b>{format_btc_price(price)}</b>\n"
                        f"Signal: {signal_emoji} <b>{signal}</b>\n"
                        f"Edge: <b>{edge:.1%}</b> | Confidence: <b>{confidence}</b>\n\n"
                        f"UP: {up_price:.3f} | DOWN: {down_price:.3f}\n"
                        f"Time left: {time_left // 60}m {time_left % 60}s | Phase: {phase}\n\n"
                        f"<i>{timestamp_with_seconds()}</i>"
                    )
                    kb = btc_menu()
                else:
                    slug = market.get("slug", "N/A")
                    up_price = market.get("up_price", 0)
                    down_price = market.get("down_price", 0)
                    time_left = market.get("time_left_seconds", 0)
                    text = (
                        f"📈 <b>Polymarket BTC 15m</b>\n\n"
                        f"UP: <b>{up_price:.3f}</b> | DOWN: <b>{down_price:.3f}</b>\n"
                        f"Time left: {time_left // 60}m {time_left % 60}s\n\n"
                        f"<i>{timestamp_with_seconds()}</i>"
                    )
                    from bot.menus import btc_menu
                    kb = btc_menu()

                await context.bot.edit_message_text(
                    chat_id=ref["chat_id"],
                    message_id=ref["message_id"],
                    text=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
                set_dashboard_last_refresh(kind, user_id)
            except BadRequest:
                pass
            except Exception as e:
                logging.debug(f"live_dashboard_job: {user_id} {kind}: {e}")


async def paper_auto_job(context: ContextTypes.DEFAULT_TYPE):
    users = get_active_users()
    for user_id in users:
        if not paper_enabled(user_id):
            continue
        try:
            bundle = await get_btc_bundle()
            model = bundle["model"]
            market = bundle["market"]

            # Resolve scalp trades first
            resolved_scalps = resolve_scalp_trades(user_id, market)
            for r in resolved_scalps:
                pnl_emoji = "✅" if r["pnl"] >= 0 else "❌"
                text = (
                    f"{pnl_emoji} <b>Scalp Closed</b>\n"
                    f"Side: {r['side']} | Entry: {r['entry_price']:.3f} → Exit: {r['exit_price']:.3f}\n"
                    f"PnL: ${r['pnl']:.4f} | Reason: {r.get('exit_reason', '?')}\n"
                    f"Hold: {r.get('hold_seconds', 0)}s"
                )
                try:
                    await context.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
                except Exception:
                    pass

            # Resolve resolution trades
            resolved = resolve_open_trades(user_id, model)
            for r in resolved:
                pnl_emoji = "✅" if r["pnl"] >= 0 else "❌"
                text = (
                    f"{pnl_emoji} <b>Trade Resolved</b>\n"
                    f"Side: {r['side']} | Entry: {r['entry_price']:.3f} → Exit: {r['exit_price']:.3f}\n"
                    f"PnL: ${r['pnl']:.4f}"
                )
                try:
                    await context.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
                except Exception:
                    pass

            # Try to open a new trade
            result = open_scalp_or_resolution_trade(user_id, model)
            if result:
                text = (
                    f"📝 <b>Trade Opened</b>\n"
                    f"Mode: {result.get('trade_mode', '?')} | Side: {result['side']}\n"
                    f"Entry: {result['entry_price']:.3f} | Stake: ${result['stake']:.2f}"
                )
                try:
                    await context.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
                except Exception:
                    pass

        except Exception as e:
            logging.debug(f"paper_auto_job: {user_id}: {e}")


async def cache_clear_job(context: ContextTypes.DEFAULT_TYPE):
    cache.clear()
    clear_market_cache()


async def market_tick_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        market = await discover_btc_15m_market()
        model = await build_btc_model(market)
        set_latest_tick(market, model)
    except Exception as e:
        logging.debug(f"market_tick_job: {e}")
        clear_latest_tick()


async def alpha_discovery_job(context: ContextTypes.DEFAULT_TYPE):
    """Background smart-wallet discovery. Safe: logs errors and never crashes the bot."""
    try:
        from bot.alpha_store import ensure_alpha_tables, get_alpha_setting, latest_alpha_scans
        from bot.smart_money import SmartMoneyEngine
        ensure_alpha_tables()
        enabled = get_alpha_setting("auto_leaderboard_scan", "1") == "1"
        if not enabled:
            return
        # Avoid scanning too aggressively. Job interval already controls this, but this guard helps after restarts.
        scans = latest_alpha_scans(1)
        if scans:
            import datetime as _dt
            last = scans[0].get("created_at") or ""
            try:
                t = _dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
                if (_dt.datetime.now(_dt.timezone.utc) - t).total_seconds() < 5 * 3600:
                    return
            except Exception:
                pass
        res = await SmartMoneyEngine([]).discover_multi_leaderboards(limit_per_board=50, score_top=75)
        logging.info(f"alpha_discovery_job: {res}")
    except Exception as e:
        logging.warning(f"alpha_discovery_job failed: {e}")
