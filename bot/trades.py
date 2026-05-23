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
    if total_size >= 500:
        score += 35
    elif total_size >= 200:
        score += 25
    elif total_size >= 75:
        score += 15

    if avg_size >= 100:
        score += 25
    elif avg_size >= 40:
        score += 15
    elif avg_size >= 15:
        score += 8

    if trade_count >= 20:
        score += 20
    elif trade_count >= 10:
        score += 12
    elif trade_count >= 5:
        score += 6

    if same_market_pairs >= 3:
        score += 15
    elif same_market_pairs >= 1:
        score += 8

    if buy_count > 0 and sell_count > 0:
        score += 5

    score = min(score, 100)

    if score >= 75:
        label = "Whale"
        reason = "Large size + high activity"
    elif score >= 55:
        label = "Sharp / Active"
        reason = "Strong activity and decent size"
    elif score >= 35:
        label = "Scaling"
        reason = "Repeated adds/reductions on similar markets"
    elif score >= 18:
        label = "Mixed"
        reason = "Some activity but weaker conviction"
    else:
        label = "Light"
        reason = "Small or infrequent activity"

    return {
        "score": score,
        "label": label,
        "reason": reason,
        "trade_count": trade_count,
        "total_size": round(total_size, 2),
        "avg_size": round(avg_size, 2),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "same_market_pairs": same_market_pairs,
    }


def parse_trade_notification(trade: dict, wallet_label: str, wallet_profile: dict | None = None) -> str:
    side = str(trade.get("side", "UNKNOWN")).upper()
    outcome = str(trade.get("outcome", "")).strip().upper()
    title = str(trade.get("title", "")).strip()
    size = trade.get("size", 0)
    price = trade.get("price", 0)
    ts = trade.get("timestamp", 0)

    side_emoji = "🟢" if side == "BUY" else "🔴" if side == "SELL" else "⚪"
    outcome_badge = "🟢 YES" if outcome == "YES" else "🔴 NO" if outcome == "NO" else outcome
    temp_label = extract_temp_from_title(title)

    profile_text = ""
    if wallet_profile:
        profile_text = (
            f"Smart profile: {wallet_profile['label']} "
            f"({wallet_profile['score']}/100)\n"
        )

    return (
        f"👛 <b>Trade detected</b>\n"
        f"🕒 {trade_timestamp_to_berlin(ts)}\n\n"
        f"Wallet: {wallet_label}\n"
        f"{profile_text}"
        f"Action: {side_emoji} {side}\n"
        f"Side: {outcome_badge}\n"
        f"Market: {temp_label}\n"
        f"Full market: {title}\n"
        f"Size: ${float(size):.2f}\n"
        f"Price: {float(price):.4f}"
    )


def detect_wallet_intelligence_message(recent_rows, wallet_label: str, wallet_profile: dict | None = None):
    if len(recent_rows) < 2:
        return None

    first = recent_rows[0]
    second = recent_rows[1]

    _, _, side1, outcome1, title1, size1, _, _ = first
    _, _, side2, outcome2, title2, size2, _, _ = second

    same_market = str(title1) == str(title2)
    same_side = str(side1) == str(side2) and str(outcome1) == str(outcome2)

    if same_market and same_side:
        total_size = float(size1 or 0) + float(size2 or 0)
        temp_label = extract_temp_from_title(str(title1))
        action = "increasing position" if str(side1).upper() == "BUY" else "reducing position"

        profile_line = ""
        if wallet_profile:
            profile_line = (
                f"Profile: {wallet_profile['label']} "
                f"({wallet_profile['score']}/100)\n"
            )

        return (
            f"🧠 <b>Wallet intelligence</b>\n"
            f"Wallet: {wallet_label}\n"
            f"{profile_line}"
            f"Pattern: {action}\n"
            f"Market: {temp_label}\n"
            f"Combined size: ${total_size:.2f}"
        )

    return None
