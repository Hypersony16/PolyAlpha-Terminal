import time
from typing import Any


class TTLCache:
    def __init__(self):
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str):
        item = self._store.get(key)
        if not item:
            return None
        expires_at, value = item
        if time.time() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: int | None = None, ttl: int | None = None):
        seconds = ttl_seconds if ttl_seconds is not None else ttl if ttl is not None else 60
        self._store[key] = (time.time() + int(seconds), value)

    def delete(self, key: str):
        self._store.pop(key, None)

    def clear(self):
        self._store.clear()

    def size(self) -> int:
        return len(self._store)


cache = TTLCache()
