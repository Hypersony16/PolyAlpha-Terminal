import logging
from datetime import time

from telegram.ext import ApplicationBuilder

from bot.config import TELEGRAM_TOKEN, BERLIN_TZ
from bot.db import init_db, get_user_setting, set_user_setting
from bot.handlers import register_handlers
from bot.jobs import alerts_job, wallet_job, wallet_trades_job, daily_summary_job, live_dashboard_job, paper_auto_job, cache_clear_job, market_tick_job, alpha_discovery_job
from bot.stats import ensure_stats_tables
from bot.paper_auto import ensure_paper_auto_tables


logging.basicConfig(level=logging.INFO)


async def global_error_handler(update, context):
    logging.exception("Unhandled bot error", exc_info=context.error)
    try:
        if update and getattr(update, "effective_chat", None):
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Error handled. Bot is still running.",
            )
    except Exception:
        pass


def ensure_defaults():
    defaults = {
        "live_dashboards_enabled": "1",
        "dashboard_refresh_seconds": "5",
    }
    for key, value in defaults.items():
        if get_user_setting(0, key) is None:
            set_user_setting(0, key, value)


def main():
    init_db()
    ensure_stats_tables()
    ensure_paper_auto_tables()
    ensure_defaults()

    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    register_handlers(app)
    app.add_error_handler(global_error_handler)

    if app.job_queue is not None:
        app.job_queue.run_repeating(alerts_job, interval=20, first=6)
        app.job_queue.run_repeating(wallet_job, interval=60, first=15)
        app.job_queue.run_repeating(wallet_trades_job, interval=4, first=5)
        app.job_queue.run_repeating(live_dashboard_job, interval=5, first=8)
        app.job_queue.run_repeating(market_tick_job, interval=1, first=2)
        app.job_queue.run_repeating(paper_auto_job, interval=10, first=12)
        app.job_queue.run_repeating(cache_clear_job, interval=300, first=300)
        app.job_queue.run_repeating(daily_summary_job, interval=86400, first=3600)
        app.job_queue.run_repeating(alpha_discovery_job, interval=21600, first=90)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
