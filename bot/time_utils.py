from datetime import datetime, timedelta, time

from bot.config import BERLIN_TZ


def berlin_now() -> datetime:
    return datetime.now(BERLIN_TZ)


def timestamp_with_seconds() -> str:
    return berlin_now().strftime("%Y-%m-%d %H:%M:%S %Z")


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "closed"

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours}h {minutes}m {secs}s"


def time_until_market_close(date_str: str) -> str:
    market_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    close_dt = datetime.combine(
        market_date + timedelta(days=1),
        time(0, 0, 0),
        tzinfo=BERLIN_TZ,
    )
    now = berlin_now()
    diff_seconds = int((close_dt - now).total_seconds())
    return format_duration(diff_seconds)
