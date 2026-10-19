"""A small thread-safe in-memory cache with a time-to-live (TTL)."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any


class TTLCache:
    """Stores values for `ttl_seconds`. The oldest entry is dropped when full."""

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int = 512,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._time = time_func
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable, default: Any = None) -> Any:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return default
            expires_at, value = entry
            if self._time() >= expires_at:
                del self._data[key]
                return default
            return value

    def set(self, key: Hashable, value: Any) -> None:
        if self._ttl <= 0:
            return
        with self._lock:
            self._data[key] = (self._time() + self._ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max_entries:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
