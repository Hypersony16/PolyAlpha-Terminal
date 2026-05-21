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

# ── City registry ────────────────────────────────────────────────────────────
# Each entry: (polymarket_slug_fragment, lat, lon)
CITIES: dict[str, tuple[str, float, float]] = {
    "munich":    ("munich",    48.1374, 11.5755),
    "madrid":    ("madrid",    40.4168, -3.7038),
    "paris":     ("paris",     48.8566,  2.3522),
    "london":    ("london",    51.5074, -0.1278),
    "milan":     ("milan",     45.4642,  9.1900),
    "new-york":  ("new-york",  40.7128, -74.0060),
    "berlin":    ("berlin",    52.5200, 13.4050),
    "rome":      ("rome",      41.9028, 12.4964),
    "amsterdam": ("amsterdam", 52.3676,  4.9041),
    "zurich":    ("zurich",    47.3769,  8.5417),
}

# Weather model
ENSEMBLE_MODEL = "gfs_seamless"
FORECAST_DAYS = 7          # fetch 7-day ensemble so multi-day markets work

# Cache TTLs (seconds)
WEATHER_CACHE_TTL   = 300   # 5 min – ensemble changes slowly
MARKET_CACHE_TTL    = 30    # 30 s  – market prices can move fast
WALLET_CACHE_TTL    = 60    # 1 min

# Copy-trading
COPYTRADE_MIN_SCORE       = 55     # wallet score threshold to auto-copy
COPYTRADE_BASE_SIZE_USDC  = 10.0   # default position size in USDC
COPYTRADE_MAX_SIZE_USDC   = 100.0
COPYTRADE_MIN_EDGE        = 0.04   # minimum model edge to allow a copy trade
COPYTRADE_COOLDOWN_S      = 120    # seconds between copy trades per wallet

# Algorithm
ALGO_LOOKBACK_DAYS        = 30     # historical signal window for back-analysis
ALGO_MIN_SAMPLE           = 5      # min signals needed before rating reliability
ALGO_CALIBRATION_ALPHA    = 0.25   # EMA weight for running calibration

DEFAULT_DASHBOARD_REFRESH  = 10
