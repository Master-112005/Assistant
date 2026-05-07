"""
NLP caching layer for frequently computed operations.
Caches intent detection, command parsing, and entity extraction.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    value: T
    timestamp: float
    hits: int = 1


class NLPCache:
    """Time-bounded cache for NLP operations."""

    def __init__(self, max_size: int = 512, ttl_seconds: float = 300.0):
        self._cache: dict[str, CacheEntry] = {}
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry is None:
            self._misses += 1
            return None
        if time.time() - entry.timestamp > self._ttl:
            del self._cache[key]
            self._misses += 1
            return None
        entry.hits += 1
        self._hits += 1
        return entry.value

    def set(self, key: str, value: Any) -> None:
        if len(self._cache) >= self._max_size:
            self._evict_oldest()
        self._cache[key] = CacheEntry(value=value, timestamp=time.time())

    def _evict_oldest(self) -> None:
        if not self._cache:
            return
        oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k].timestamp)
        del self._cache[oldest_key]

    def clear(self) -> None:
        self._cache.clear()
        self._hits = 0
        self._misses = 0

    @property
    def stats(self) -> dict[str, Any]:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total > 0 else 0,
            "size": len(self._cache),
        }


_global_intent_cache = NLPCache(max_size=256, ttl_seconds=120.0)
_global_parse_cache = NLPCache(max_size=512, ttl_seconds=300.0)


def _make_key(text: str, prefix: str = "") -> str:
    """Create a cache key from text."""
    return f"{prefix}:{hashlib.md5(text.encode()).hexdigest()[:16]}"


def cached_intent(text: str) -> Any | None:
    """Get cached intent detection result."""
    return _global_intent_cache.get(_make_key(text, "intent"))


def cache_intent(text: str, result: Any) -> None:
    """Cache intent detection result."""
    _global_intent_cache.set(_make_key(text, "intent"), result)


def cached_parse(text: str) -> Any | None:
    """Get cached parsing result."""
    return _global_parse_cache.get(_make_key(text, "parse"))


def cache_parse(text: str, result: Any) -> None:
    """Cache parsing result."""
    _global_parse_cache.set(_make_key(text, "parse"), result)


def get_cache_stats() -> dict[str, dict[str, Any]]:
    """Get cache statistics."""
    return {
        "intent_cache": _global_intent_cache.stats,
        "parse_cache": _global_parse_cache.stats,
    }


def clear_nlp_caches() -> None:
    """Clear all NLP caches."""
    _global_intent_cache.clear()
    _global_parse_cache.clear()


def cached_call(func: Callable[[], T], key: str, cache: NLPCache | None = None) -> T:
    """Decorator-style cache lookup."""
    if cache is None:
        cache = _global_parse_cache
    result = cache.get(key)
    if result is not None:
        return result
    result = func()
    cache.set(key, result)
    return result