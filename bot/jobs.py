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
    set_user_setting,
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
from bot.polymarket import discover_btc_15m_market, fetch_public_profile
from bot.wallet import fetch_wallet_total_value
from bot.trades import fetch_wallet_trades, parse_trade_notification, detect_wallet_intelligence_message, score_wallet_from_rows
from bot.time_utils import timestamp_with_seconds


async def get_btc_bundle():
    cached = cache.get("btc_bundle")
    if cached:
        return cached
    market = await discover_btc_15m_market()
    model = await build_btc_model(market)
    payload = (market, model)
    cache.set("btc_bundle", payload, ttl_seconds=3)
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


async def wallet_label_html(wallet: str, nickname: str) -> str:
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

            threshold = get_edge_threshold(user_id)
            notify = get_notify_mode(user_id)
            edge = float(model["edge"])

            if notify == "quiet":
                threshold = max(threshold, 0.08)
                if model["confidence"] != "High":
                    continue

            if edge < threshold:
                continue

            # Reuse legacy alert dedupe with BTC-coded values.
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
                f"Market: {model['market_prob']*100:.1f}%\n"
                f"Edge: +{edge*100:.1f} pts\n"
                f"Confidence: {model['confidence']}\n"
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
            own = get_own_wallet(user_id).strip()
            targets = list(wallets)
            if own:
                targets.append({"address": own, "nickname": "My Wallet"})

            for item in targets:
                wallet = item.get("address", "")
                nickname = item.get("nickname", "")
                if not wallet:
                    continue
                try:
                    value = await fetch_wallet_total_value(wallet)
                    prev_key = f"wallet_last_value:{wallet.lower()}"
                    prev_raw = get_user_setting(user_id, prev_key, "")
                    log_wallet_snapshot(user_id, wallet, value)
                    set_user_setting(user_id, prev_key, str(value))

                    if prev_raw:
                        change = value - float(prev_raw)
                        if abs(change) >= 1.0 and get_notify_mode(user_id) != "quiet":
                            label = await wallet_label_html(wallet, nickname)
                            sign = "+" if change >= 0 else ""
                            await context.bot.send_message(
                                chat_id=int(chat_id),
                                text=(
                                    f"👛 <b>Wallet Value Update</b>\n"
                                    f"Wallet: {label}\n"
                                    f"Value: ${value:.2f}\n"
                                    f"Change: {sign}${change:.2f}"
                                ),
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
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

            for item in get_tracked_wallets(user_id):
                wallet = item.get("address", "")
                nickname = item.get("nickname", "")
                if not wallet:
                    continue

                try:
                    trades = await fetch_wallet_trades(wallet, limit=100)
                except Exception:
                    continue

                trades_sorted = sorted(trades, key=lambda t: float(t.get("timestamp", 0) or 0))
                for trade in trades_sorted:
                    tx_hash = str(trade.get("transactionHash", "")).strip()
                    if not tx_hash or trade_exists(user_id, wallet, tx_hash):
                        continue

                    log_tracked_trade(user_id, wallet, trade)
                    recent = get_recent_tracked_trades(user_id, wallet, limit=20)
                    profile = score_wallet_from_rows(recent)
                    size = float(trade.get("size", 0) or 0)

                    if get_notify_mode(user_id) == "quiet" and profile.get("score", 0) < 55 and size < 25:
                        continue

                    wallet_label = nickname if nickname else wallet
                    wallet_link = await wallet_label_html(wallet, nickname)
                    text = parse_trade_notification(trade, wallet_label, profile)
                    html_text = text.replace(f"Wallet: {wallet_label}", f"Wallet: {wallet_link}")

                    buttons = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("📋 Copy Preview", callback_data=f"copy_{tx_hash[:18]}"),
                            InlineKeyboardButton("🙈 Ignore", callback_data=f"ignore_{tx_hash[:18]}")
                        ]
                    ])

                    await context.bot.send_message(
                        chat_id=int(chat_id),
                        text=html_text,
                        parse_mode="HTML",
                        reply_markup=buttons,
                        disable_web_page_preview=True,
                    )

                    if get_notify_mode(user_id) != "quiet":
                        intel = detect_wallet_intelligence_message(recent[:5], wallet_label, profile)
                        if intel:
                            await context.bot.send_message(
                                chat_id=int(chat_id),
                                text=intel.replace(f"Wallet: {wallet_label}", f"Wallet: {wallet_link}"),
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
    except Exception as e:
        logging.exception(f"wallet_trades_job failed: {e}")


async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        users = get_active_users(500)
        market, model = await get_btc_bundle()

        for user_row in users:
            user_id = int(user_row[0])
            chat_id = get_user_setting(user_id, "alerts_chat_id", "")
            if not chat_id:
                continue

            current_date = timestamp_with_seconds().split(" ")[0]
            if get_user_setting(user_id, "last_daily_summary_date", "") == current_date:
                continue

            text = (
                f"🌙 <b>Daily BTC Summary</b>\n"
                f"BTC: ${model['price']:,.2f}\n"
                f"Last signal: {model['signal']}\n"
                f"Edge: +{model['edge']*100:.1f} pts\n"
                f"Confidence: {model['confidence']}\n"
                f"Tracked wallets: {len(get_tracked_wallets(user_id))}"
            )
            await context.bot.send_message(chat_id=int(chat_id), text=text, parse_mode="HTML")
            set_user_setting(user_id, "last_daily_summary_date", current_date)
    except Exception as e:
        logging.exception(f"daily_summary_job failed: {e}")


async def live_dashboard_job(context: ContextTypes.DEFAULT_TYPE):
    if not live_dashboards_enabled():
        return

    try:
        from bot.handlers import build_btc_text, build_market_text, build_wallet_text
        refresh_s = dashboard_refresh_seconds()
        now = time_module.time()

        for user_row in get_active_users(500):
            user_id = int(user_row[0])
            for kind, builder in (("bestbet", build_btc_text), ("market", build_market_text), ("wallet", build_wallet_text)):
                ref = get_dashboard_ref(kind, user_id)
                if not ref:
                    continue
                if now - get_dashboard_last_refresh(kind, user_id) < refresh_s:
                    continue
                try:
                    text = await builder(user_id)
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
