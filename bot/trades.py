import re
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo

BERLIN_TZ = ZoneInfo("Europe/Berlin")


async def fetch_wallet_trades(wallet: str, limit: int = 100):
    url = "https://data-api.polymarket.com/trades"
    params = {
        "user": wallet,
        "limit": limit,
        "offset": 0,
        "takerOnly": "false",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

    if not isinstance(data, list):
        raise ValueError("Unexpected trades response format")

    return data


def trade_timestamp_to_berlin(ts: int | float) -> str:
    dt = datetime.fromtimestamp(float(ts), tz=BERLIN_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def extract_temp_from_title(title: str) -> str:
    match = re.search(r"(\d+)\s*°?C", title, re.IGNORECASE)
    if match:
        return f"{match.group(1)}°C"
    return title


def score_wallet_from_rows(recent_rows):
    if not recent_rows:
        return {
            "score": 0,
            "label": "No data",
            "reason": "No recent tracked trades",
            "trade_count": 0,
            "total_size": 0.0,
            "avg_size": 0.0,
        }

    total_size = 0.0
    buy_count = 0
    sell_count = 0
    same_market_pairs = 0

    prev_title = None
    prev_side = None
    prev_outcome = None

    for row in recent_rows:
        _, _, side, outcome, title, size, _, _ = row
        size = float(size or 0)
        total_size += size

        if str(side).upper() == "BUY":
            buy_count += 1
        elif str(side).upper() == "SELL":
            sell_count += 1

        if prev_title == title and prev_side == side and prev_outcome == outcome:
            same_market_pairs += 1

        prev_title = title
        prev_side = side
        prev_outcome = outcome

    trade_count = len(recent_rows)
    avg_size = total_size / trade_count if trade_count else 0.0

    score = 0
    if trade_count >= 5:
        score += 20
    if trade_count >= 20:
        score += 20
    if avg_size >= 10:
        score += 20
    if buy_count > sell_count:
        score += 10
    if same_market_pairs >= 2:
        score += 10

    if score >= 70:
        label = "Smart"
    elif score >= 40:
        label = "Active"
    else:
        label = "Low activity"

    return {
        "score": score,
        "label": label,
        "reason": f"{trade_count} trades, avg ${avg_size:.2f}",
        "trade_count": trade_count,
        "total_size": round(total_size, 2),
        "avg_size": round(avg_size, 2),
    }


def parse_trade_notification(trade: dict) -> str | None:
    try:
        side = str(trade.get("side", "")).upper()
        outcome = str(trade.get("outcome", trade.get("outcomeName", "")))
        title = str(trade.get("title", trade.get("marketTitle", trade.get("question", ""))))
        size = float(trade.get("size", trade.get("usdcSize", 0)) or 0)
        price = float(trade.get("price", 0) or 0)
        ts = float(trade.get("timestamp", trade.get("createdAt", 0)) or 0)

        if not title:
            return None

        time_str = trade_timestamp_to_berlin(ts) if ts else "unknown time"
        return (
            f"{'🟢' if side == 'BUY' else '🔴'} <b>{side}</b> {outcome}\n"
            f"<i>{title[:80]}</i>\n"
            f"Size: ${size:.2f} @ {price:.3f}\n"
            f"{time_str}"
        )
    except Exception:
        return None


def detect_wallet_intelligence_message(trades: list) -> str:
    if not trades:
        return "No recent trades found."

    buy_count = sum(1 for t in trades if str(t.get("side", "")).upper() == "BUY")
    sell_count = sum(1 for t in trades if str(t.get("side", "")).upper() == "SELL")
    total = len(trades)

    lines = [f"📊 <b>Wallet Intelligence</b>", f"Recent trades: {total}"]
    if buy_count:
        lines.append(f"Buys: {buy_count}")
    if sell_count:
        lines.append(f"Sells: {sell_count}")

    return "\n".join(lines)
