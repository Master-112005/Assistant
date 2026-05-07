"""
Wake word detection and stripping.
"""
from __future__ import annotations

import re


# Conservative wake phrase variants with strict confidence thresholds
# IMPORTANT: Only include exact variants we actually want to trigger.
# Removed "noah" (too similar to "nova") and "nava" (loose interpretation)
_WAKE_VARIANTS: tuple[tuple[str, float], ...] = (
    ("nova assistant", 1.0),
    ("hey nova", 0.99),
    ("hi nova", 0.98),
    ("hello nova", 0.98),
    ("nova", 0.97),
)

_WAKE_PATTERN = re.compile(
    r"^\s*(nova assistant|hey nova|hi nova|hello nova|nova)\b[\s,!.?;:-]*",
    flags=re.IGNORECASE,
)


def _normalize(text: str) -> str:
    cleaned = str(text or "").strip().lower()
    cleaned = cleaned.replace("\u2019", "'").replace("\u2018", "'")
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def detect_wake_word(text: str) -> bool:
    """Return ``True`` when the input begins with a supported wake phrase."""
    normalized = _normalize(text)
    return any(normalized == variant or normalized.startswith(f"{variant} ") for variant, _ in _WAKE_VARIANTS)


def strip_wake_word(text: str) -> str:
    """Remove a leading wake phrase while keeping the remaining command intact."""
    source = str(text or "").strip()
    if not source:
        return ""
    match = _WAKE_PATTERN.match(source)
    if not match:
        return source
    remainder = source[match.end() :].strip()
    return remainder if re.search(r"[A-Za-z0-9]", remainder) else ""


def wake_confidence(text: str) -> float:
    normalized = _normalize(text)
    for variant, score in _WAKE_VARIANTS:
        if normalized == variant or normalized.startswith(f"{variant} "):
            return score
    return 0.0
