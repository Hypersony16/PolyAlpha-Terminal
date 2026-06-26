"""Market quality and freshness filters for PolyAlpha Terminal.
Keeps stale/settled/long-shot noise out of actionable smart-money views.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta, date
from typing import Optional

_DATE_PATTERNS = [
    re.compile(r"(20\d{2})[-_](\d{2})[-_](\d{2})"),
    re.compile(r"on\s+(20\d{2})-(\d{2})-(\d{2})", re.I),
]

BAD_OUTRIGHT_PATTERNS = [
    "win the 2026 fifa world cup",
    "world cup winner",
    "to win the world cup",
    "win fifa world cup",
    "champion of the 2026 fifa world cup",
]

LOW_QUALITY_WORDS = [
    "will brazil win the 2026 fifa world cup",
    "will senegal win the 2026 fifa world cup",
]


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def extract_market_date(title: str = "", market: str = "") -> Optional[date]:
    s = f"{title or ''} {market or ''}"
    for pat in _DATE_PATTERNS:
        m = pat.search(s)
        if not m:
            continue
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            return None
    return None


def is_outright_noise(title: str = "", market: str = "") -> bool:
    s = f"{title or ''} {market or ''}".lower()
    if any(x in s for x in BAD_OUTRIGHT_PATTERNS):
        return True
    if any(x in s for x in LOW_QUALITY_WORDS):
        return True
    # very broad futures with huge implied fantasy edge from thin consensus
    if "win the 2026" in s and "on 2026-" not in s:
        return True
    return False


def is_stale_market(title: str = "", market: str = "", *, max_days_ahead: int = 21) -> bool:
    d = extract_market_date(title, market)
    if d is None:
        return False
    today = utc_today()
    if d < today:
        return True
    if d > today + timedelta(days=max_days_ahead):
        return True
    return False


def is_quality_market(title: str = "", market: str = "") -> bool:
    # hide already-played markets and long-term outright noise
    if is_stale_market(title, market):
        return False
    if is_outright_noise(title, market):
        return False
    return True


def market_filter_reason(title: str = "", market: str = "") -> str:
    if is_stale_market(title, market):
        d = extract_market_date(title, market)
        return f"stale/old event date {d}" if d else "stale market"
    if is_outright_noise(title, market):
        return "low-quality long-term outright market"
    return "ok"
