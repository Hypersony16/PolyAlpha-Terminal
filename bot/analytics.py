from collections import defaultdict
from datetime import datetime, timedelta, timezone


def _cutoff_timestamp(hours: int) -> float:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    return cutoff.timestamp()


def summarize_wallet_rows(rows):
    """
    rows format:
    (created_at, transaction_hash, side, outcome, title, size, price, trade_timestamp)
    """
    total_size = 0.0
    buy_count = 0
    sell_count = 0
    yes_count = 0
    no_count = 0
    markets = defaultdict(float)

    for row in rows:
        _, _, side, outcome, title, size, price, ts = row
        size = float(size or 0)
        total_size += size

        if str(side).upper() == "BUY":
            buy_count += 1
        elif str(side).upper() == "SELL":
            sell_count += 1

        if str(outcome).upper() == "YES":
            yes_count += 1
        elif str(outcome).upper() == "NO":
            no_count += 1

        key = f"{title} | {str(outcome).upper()}"
        markets[key] += size

    top_markets = sorted(markets.items(), key=lambda x: x[1], reverse=True)[:5]

    return {
        "trade_count": len(rows),
        "total_size": round(total_size, 2),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "yes_count": yes_count,
        "no_count": no_count,
        "top_markets": top_markets,
    }


def filter_rows_by_hours(rows, hours: int):
    cutoff = _cutoff_timestamp(hours)
    out = []
    for row in rows:
        ts = float(row[7] or 0)
        if ts >= cutoff:
            out.append(row)
    return out


def compare_wallet_positions_like(tracked_rows, own_rows):
    """
    Trade-based comparison approximation.
    Finds markets/outcomes the tracked wallet traded that own wallet did not.
    """
    tracked_keys = set()
    own_keys = set()

    for row in tracked_rows:
        _, _, side, outcome, title, size, price, ts = row
        if str(side).upper() == "BUY":
            tracked_keys.add((title, str(outcome).upper()))

    for row in own_rows:
        _, _, side, outcome, title, size, price, ts = row
        if str(side).upper() == "BUY":
            own_keys.add((title, str(outcome).upper()))

    missing = tracked_keys - own_keys
    overlap = tracked_keys & own_keys

    return {
        "missing": list(missing),
        "overlap": list(overlap),
        "tracked_count": len(tracked_keys),
        "own_count": len(own_keys),
        "missing_count": len(missing),
        "overlap_count": len(overlap),
    }
