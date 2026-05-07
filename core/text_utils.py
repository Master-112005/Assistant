"""
Shared text normalization utilities for all skills.
Eliminates duplicate _normalize implementations across skills.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Callable

import unicodedata

_RE_LEADING_PUNCT = re.compile(r"^[\s\u200b\u3000]+")
_RE_TRAILING_PUNCT = re.compile(r"[\s\u200b\u3000]+$")
_RE_MULTI_SPACE = re.compile(r"\s{2,}")
_RE_QUOTES = re.compile(r'^["\']+|["\']+$')

_COMMON_NOISE = {
    "please", "can", "could", "would", "will", "you", "the", "a", "an",
    "to", "for", "me", "my", "i", "want", "need", "help", "assistant",
}

_ORDINAL_MAP = {
    "first": 1, "1": 1, "1st": 1, "one": 1,
    "second": 2, "2": 2, "2nd": 2, "two": 2,
    "third": 3, "3": 3, "3rd": 3, "three": 3,
    "fourth": 4, "4": 4, "4th": 4, "four": 4,
    "fifth": 5, "5": 5, "5th": 5, "five": 5,
    "sixth": 6, "6": 6, "6th": 6, "six": 6,
    "seventh": 7, "7": 7, "7th": 7, "seven": 7,
    "eighth": 8, "8": 8, "8th": 8, "eight": 8,
    "ninth": 9, "9": 9, "9th": 9, "nine": 9,
    "tenth": 10, "10": 10, "10th": 10, "ten": 10,
}


@lru_cache(maxsize=512)
def normalize_command(text: str, preserve_numbers: bool = True) -> str:
    """
    Normalize command text for skill processing.
    Uses caching for repeated commands.
    """
    if not text:
        return ""

    normalized = text.strip()
    normalized = _RE_LEADING_PUNCT.sub("", normalized)
    normalized = _RE_TRAILING_PUNCT.sub("", normalized)
    normalized = unicodedata.normalize("NFKC", normalized)
    normalized = normalized.lower()
    normalized = _RE_MULTI_SPACE.sub(" ", normalized)

    if not preserve_numbers:
        normalized = re.sub(r"\d+", "#", normalized)

    return normalized.strip()


@lru_cache(maxsize=256)
def normalize_label(value: str) -> str:
    """Normalize UI element labels for matching."""
    if not value:
        return ""
    cleaned = str(value).strip().lower()
    cleaned = _RE_QUOTES.sub("", cleaned)
    cleaned = _RE_MULTI_SPACE.sub(" ", cleaned)
    return cleaned


def normalize_for_matching(text: str) -> str:
    """Fast normalization for text matching (no caching - per-call)."""
    if not text:
        return ""
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


@lru_cache(maxsize=128)
def extract_ordinal(text: str) -> int | None:
    """Extract ordinal number from text."""
    lowered = text.lower().strip()
    for word, num in _ORDINAL_MAP.items():
        if word in lowered:
            return num
    match = re.search(r"\d+", lowered)
    return int(match.group()) if match else None


def remove_noise_words(text: str) -> str:
    """Remove common filler words from command text."""
    words = text.lower().split()
    filtered = [w for w in words if w not in _COMMON_NOISE]
    return " ".join(filtered)


def normalize_path(text: str) -> str:
    """Normalize file/folder path for matching."""
    if not text:
        return ""
    normalized = text.strip().lower()
    normalized = normalized.replace("/", "\\")
    normalized = re.sub(r"\\+", r"\\", normalized)
    return normalized.strip()


def normalize_app_name(text: str) -> str:
    """Normalize application name for matching."""
    if not text:
        return ""
    cleaned = text.strip().lower()
    cleaned = re.sub(r"[^\w\s.-]", "", cleaned)
    cleaned = _RE_MULTI_SPACE.sub(" ", cleaned)
    return cleaned.strip()


def fuzzy_match_threshold() -> int:
    """Return configurable fuzzy match threshold."""
    return 75


def create_normalizer(preserve_numbers: bool = True) -> Callable[[str], str]:
    """Create a cached normalizer for specific use case."""
    return lambda t: normalize_command(t, preserve_numbers)


def sanitize_search_query(query: str) -> str:
    """Sanitize user search query."""
    if not query:
        return ""
    cleaned = query.strip()
    cleaned = re.sub(r"[^\w\s\-.,!?]", "", cleaned)
    cleaned = _RE_MULTI_SPACE.sub(" ", cleaned)
    return cleaned[:500]


def highlight_keywords(text: str, keywords: list[str]) -> str:
    """Highlight keywords in text for display."""
    if not keywords or not text:
        return text
    pattern = re.compile("|".join(re.escape(kw.lower()) for kw in keywords), re.IGNORECASE)
    return pattern.sub(lambda m: f"**{m.group()}**", text)


def extract_numbers(text: str) -> list[int]:
    """Extract all numbers from text."""
    return [int(m.group()) for m in re.finditer(r"\d+", text)]


def is_question(text: str) -> bool:
    """Detect if text is a question."""
    return text.strip().endswith("?") or any(
        text.lower().startswith(q) for q in ("what", "how", "why", "when", "where", "who", "which")
    )