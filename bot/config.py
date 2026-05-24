import os
from zoneinfo import ZoneInfo

TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
DB_PATH: str = os.getenv("DB_PATH", "bot.db")
TIMEZONE = "Europe/Berlin"
BERLIN_TZ = ZoneInfo(TIMEZONE)

_raw_admins = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_IDS: set[int] = {
    int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()
}

# Cache TTLs (seconds)
WEATHER_CACHE_TTL   = 300   # 5 min
MARKET_CACHE_TTL    = 30    # 30s — market prices can move fast
WALLET_CACHE_TTL    = 60    # 1 min

# Dashboard
DEFAULT_DASHBOARD_REFRESH = 10
