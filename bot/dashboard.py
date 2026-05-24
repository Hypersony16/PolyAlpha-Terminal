import json
import time

from bot.db import get_user_setting, set_user_setting


def dashboard_key(kind: str) -> str:
    return f"dashboard:{kind}"


def save_dashboard_ref(user_id: int, kind: str, chat_id: int, message_id: int):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "last_refresh": 0,
    }
    set_user_setting(user_id, dashboard_key(kind), json.dumps(payload))


def get_dashboard_ref(kind: str, user_id: int):
    raw = get_user_setting(user_id, dashboard_key(kind), "")
    if not raw:
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def set_dashboard_last_refresh(kind: str, user_id: int, ts: float | None = None):
    payload = get_dashboard_ref(kind, user_id)
    if not payload:
        return

    payload["last_refresh"] = ts if ts is not None else time.time()
    set_user_setting(user_id, dashboard_key(kind), json.dumps(payload))


def get_dashboard_last_refresh(kind: str, user_id: int) -> float:
    payload = get_dashboard_ref(kind, user_id)
    if not payload:
        return 0.0
    return float(payload.get("last_refresh", 0) or 0)


def clear_dashboard_ref(kind: str, user_id: int):
    set_user_setting(user_id, dashboard_key(kind), "")


def live_dashboards_enabled() -> bool:
    raw = get_user_setting(0, "live_dashboards_enabled", "1")
    return raw == "1"


def set_live_dashboards_enabled(value: bool):
    set_user_setting(0, "live_dashboards_enabled", "1" if value else "0")


def dashboard_refresh_seconds() -> int:
    raw = get_user_setting(0, "dashboard_refresh_seconds", "10")
    try:
        return max(5, int(raw))
    except Exception:
        return 10
