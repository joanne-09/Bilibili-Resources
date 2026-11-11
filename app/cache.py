import time
from collections.abc import Callable
from typing import Any


class TTLCache:
    """Tiny in-memory cache whose entries expire after ttl_s seconds."""

    def __init__(self, ttl_s: float, clock: Callable[[], float] = time.monotonic):
        self._ttl, self._clock = ttl_s, clock
        self._items: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if self._clock() - stored_at > self._ttl:
            del self._items[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._items[key] = (self._clock(), value)
