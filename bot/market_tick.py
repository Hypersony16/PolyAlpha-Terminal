
from __future__ import annotations

import time
from typing import Dict, Any

# Ultra-light in-memory tick store.
# This is not permanent and is refreshed constantly by market_tick_job.
_STATE: Dict[str, Any] = {
    "ts": 0.0,
    "market": {},
    "model": {},
}


def set_latest_tick(market: Dict[str, Any], model: Dict[str, Any]):
    _STATE["ts"] = time.time()
    _STATE["market"] = market or {}
    _STATE["model"] = model or {}


def get_latest_tick(max_age: float = 3.0) -> Dict[str, Any]:
    age = time.time() - float(_STATE.get("ts") or 0)
    if age <= max_age and _STATE.get("market") and _STATE.get("model"):
        return {
            "fresh": True,
            "age": age,
            "market": _STATE.get("market") or {},
            "model": _STATE.get("model") or {},
        }
    return {"fresh": False, "age": age, "market": {}, "model": {}}


def clear_latest_tick():
    _STATE.update({"ts": 0.0, "market": {}, "model": {}})
