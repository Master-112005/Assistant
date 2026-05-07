"""
Thread-safe bounded caching primitives for latency-sensitive assistant paths.

The cache is intentionally small and explicit: entries have optional TTLs,
least-recently-used eviction, and observable hit/miss counters.  It is used for
lookups that are safe to reuse, such as application resolution and repeated
intent-pattern decisions, not for replaying side-effecting command results.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import threading
import time
from typing import Any

from core import settings, state
from core.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class _CacheEntry:
    value: Any
    expires_at: float | None
    created_at: float
    last_accessed_at: float
    hits: int = 0

    def expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at


class CacheManager:
    """Bounded TTL/LRU cache with thread-safe metrics."""

    def __init__(
        self,
        *,
        max_entries: int = 1024,
        default_ttl: float | None = None,
        enabled: bool | None = None,
        namespace: str = "default",
    ) -> None:
        self.max_entries = max(1, int(max_entries))
        self.default_ttl = default_ttl
        self.namespace = namespace
        self._enabled_override = enabled
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._sets = 0
        self._evictions = 0

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return bool(self._enabled_override)
        try:
            return bool(settings.get("cache_enabled", True))
        except Exception:
            return True

    def get(self, key: str, default: Any = None) -> Any:
        """Return a cached value or *default* when disabled, missing, or expired."""
        if not self.enabled:
            return default

        normalized_key = self._normalize_key(key)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(normalized_key)
            if entry is None:
                self._record_miss(normalized_key)
                return default
            if entry.expired(now):
                self._entries.pop(normalized_key, None)
                self._record_miss(normalized_key, expired=True)
                return default
            entry.hits += 1
            entry.last_accessed_at = now
            self._entries.move_to_end(normalized_key)
            self._hits += 1
            state.cache_hits = int(getattr(state, "cache_hits", 0) or 0) + 1
            logger.info("Cache hit: %s", normalized_key)
            return entry.value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """Store *value* under *key* with optional TTL seconds."""
        if not self.enabled:
            return

        normalized_key = self._normalize_key(key)
        now = time.monotonic()
        resolved_ttl = self.default_ttl if ttl is None else ttl
        expires_at = None if resolved_ttl is None or resolved_ttl <= 0 else now + float(resolved_ttl)
        with self._lock:
            self._entries[normalized_key] = _CacheEntry(
                value=value,
                expires_at=expires_at,
                created_at=now,
                last_accessed_at=now,
            )
            self._entries.move_to_end(normalized_key)
            self._sets += 1
            self._evict_if_needed()

    def invalidate(self, key: str) -> bool:
        """Remove one key and return True when an entry existed."""
        normalized_key = self._normalize_key(key)
        with self._lock:
            removed = self._entries.pop(normalized_key, None) is not None
        if removed:
            logger.info("Cache invalidated: %s", normalized_key)
        return removed

    def invalidate_prefix(self, prefix: str) -> int:
        """Remove all keys under a prefix and return the removed count."""
        normalized_prefix = self._normalize_key(prefix)
        with self._lock:
            keys = [key for key in self._entries if key.startswith(normalized_prefix)]
            for key in keys:
                self._entries.pop(key, None)
        if keys:
            logger.info("Cache invalidated prefix: %s (%d entries)", normalized_prefix, len(keys))
        return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
        logger.info("Cache cleared: %s", self.namespace)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "namespace": self.namespace,
                "enabled": self.enabled,
                "entries": len(self._entries),
                "max_entries": self.max_entries,
                "hits": self._hits,
                "misses": self._misses,
                "sets": self._sets,
                "evictions": self._evictions,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
            }

    def cleanup_expired(self) -> int:
        now = time.monotonic()
        with self._lock:
            expired = [key for key, entry in self._entries.items() if entry.expired(now)]
            for key in expired:
                self._entries.pop(key, None)
        return len(expired)

    def _evict_if_needed(self) -> None:
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self._evictions += 1

    def _record_miss(self, key: str, *, expired: bool = False) -> None:
        self._misses += 1
        state.cache_misses = int(getattr(state, "cache_misses", 0) or 0) + 1
        logger.info("Cache miss: %s%s", key, " (expired)" if expired else "")

    def _normalize_key(self, key: str) -> str:
        text = str(key or "").strip()
        return f"{self.namespace}.{text}" if self.namespace and not text.startswith(f"{self.namespace}.") else text


def _default_ttl() -> float:
    try:
        return float(settings.get("cache_ttl_seconds", 300) or 300)
    except Exception:
        return 300.0


cache_manager = CacheManager(max_entries=2048, default_ttl=_default_ttl(), namespace="nova")

