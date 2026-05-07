"""
Evidence-based personalization engine for the Nova Assistant.

Learns user preferences from *actual behaviour* (not fabrications),
tracks confidence via weighted signals with time-decay, and applies
personalised defaults at runtime.  Explicit user overrides always
take priority over inferred values.

Architecture
------------
    1. Signal collection     — raw behavioural observations
    2. Preference inference  — aggregate signals → best value
    3. Confidence scoring    — frequency + recency + consistency
    4. Profile storage       — ``user_preferences`` table
    5. Runtime application   — ``get_preference``, ``rank_options``
    6. User overrides        — ``set_explicit_preference``
    7. Drift handling        — time-decay on old signals
    8. Privacy controls      — local-only, forget, stats
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from core import settings, state
from core.logger import get_logger
from core.memory_store import MemoryStore
from core.memory import MemoryManager, memory_manager as _default_memory

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Well-known categories
# ---------------------------------------------------------------------------

class Category:
    """Well-known preference categories."""
    BROWSER = "browser"
    MUSIC_APP = "music_app"
    MUSIC_GENRE = "music_genre"
    CONTACT = "contact"
    APP = "app"
    SEARCH_ENGINE = "search_engine"
    HABIT = "habit"


# Category ↔ intent mapping used by ``learn_from_interaction``
_INTENT_CATEGORY_MAP: dict[str, str] = {
    "open_app": Category.APP,
    "search": Category.BROWSER,
    "play_music": Category.MUSIC_APP,
    "play": Category.MUSIC_APP,
    "call": Category.CONTACT,
    "message": Category.CONTACT,
}

# Apps that qualify as browsers
_BROWSER_NAMES = frozenset({
    "chrome", "firefox", "edge", "brave", "opera", "vivaldi", "safari", "arc",
})

# Apps that qualify as music players
_MUSIC_APP_NAMES = frozenset({
    "spotify", "youtube music", "apple music", "vlc", "winamp",
    "foobar2000", "musicbee", "groove", "itunes", "amazon music",
})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PreferenceSignal:
    """A single behavioural observation."""
    category: str
    key: str
    value: str
    weight: float = 1.0
    source: str = "auto"
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "key": self.key,
            "value": self.value,
            "weight": self.weight,
            "source": self.source,
            "timestamp": self.timestamp,
        }


@dataclass(slots=True)
class UserPreference:
    """An aggregated, scored preference."""
    category: str
    key: str
    value: str
    confidence: float = 0.0
    evidence_count: int = 0
    is_explicit: bool = False
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "key": self.key,
            "value": self.value,
            "confidence": round(self.confidence, 3),
            "evidence_count": self.evidence_count,
            "is_explicit": self.is_explicit,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# PersonalizationEngine
# ---------------------------------------------------------------------------

class PersonalizationEngine:
    """
    Evidence-based preference learning and application.

    Uses the Phase 31 MemoryManager's underlying store for persistence.
    Signals are stored in ``preference_signals``; aggregated results in
    ``user_preferences``.
    """

    # Decay half-life in days — signals older than this lose influence
    DECAY_HALF_LIFE_DAYS = 14.0

    # Minimum evidence required before inference is trusted
    MIN_EVIDENCE = 3

    # Maximum signals kept per (category, key) to bound storage
    MAX_SIGNALS_PER_KEY = 200

    def __init__(
        self,
        *,
        memory: MemoryManager | None = None,
    ) -> None:
        self._memory = memory or _default_memory
        self._ready = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def init(self) -> None:
        if self._ready:
            return
        if not settings.get("personalization_enabled"):
            logger.info("Personalization disabled by settings")
            return
        if not self._memory.ready:
            self._memory.init()
        self._ready = True
        state.personalization_ready = True
        logger.info("PersonalizationEngine initialised")

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def _store(self) -> MemoryStore:
        return self._memory._store

    # ================================================================== #
    # Signal recording                                                   #
    # ================================================================== #

    def record_signal(
        self,
        category: str,
        key: str,
        value: str,
        *,
        weight: float = 1.0,
        source: str = "auto",
    ) -> None:
        """
        Record a raw behavioural signal.

        Signals are observations, not decisions.  The engine aggregates
        them into preferences asynchronously or on demand.
        """
        self._ensure_ready()
        if not settings.get("auto_learn_preferences"):
            return

        category = _norm(category)
        key = _norm(key)
        value = value.strip()
        if not category or not value:
            return

        now = _utc_iso()
        self._store.execute(
            "INSERT INTO preference_signals (category, key, value, weight, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (category, key, value, weight, source, now),
        )

        state.last_signal_recorded = {"category": category, "key": key, "value": value}
        logger.debug("Signal: %s/%s = %s weight=%.1f", category, key, value, weight)

        # Prune old signals to bound storage
        self._prune_signals(category, key)

        # Incrementally update the preference for this (category, key)
        self._recompute_one(category, key)

    def learn_from_interaction(
        self,
        user_input: str,
        action: str,
        target: str,
        *,
        success: bool = True,
        params: dict[str, Any] | None = None,
    ) -> None:
        """
        Extract and record signals from a completed interaction.

        Called by the processor after every successful action.
        """
        if not self._ready or not success:
            return
        if not settings.get("auto_learn_preferences"):
            return

        action_lower = action.strip().lower()
        target_lower = target.strip().lower()
        params = params or {}

        # App usage signal
        if action_lower == "open_app" and target_lower:
            self.record_signal(Category.APP, "default", target_lower)

            if target_lower in _BROWSER_NAMES:
                self.record_signal(Category.BROWSER, "default", target_lower, weight=1.5)

            if target_lower in _MUSIC_APP_NAMES:
                self.record_signal(Category.MUSIC_APP, "default", target_lower, weight=1.5)

        # Search signal → browser preference
        if action_lower == "search":
            browser = str(params.get("browser") or params.get("target_app") or "").strip().lower()
            if browser and browser in _BROWSER_NAMES:
                self.record_signal(Category.BROWSER, "default", browser, weight=1.2)

        # Play/music signal
        if action_lower in {"play", "play_music"}:
            genre = str(params.get("genre", "")).strip().lower()
            if genre:
                self.record_signal(Category.MUSIC_GENRE, "default", genre)
            app = str(params.get("app") or target_lower or "").strip().lower()
            if app and app in _MUSIC_APP_NAMES:
                self.record_signal(Category.MUSIC_APP, "default", app, weight=1.2)

        # Contact signal
        if action_lower in {"call", "message", "whatsapp"} and target_lower:
            self.record_signal(Category.CONTACT, "frequent", target_lower, weight=1.3)

    # ================================================================== #
    # Preference retrieval                                               #
    # ================================================================== #

    def get_preference(
        self,
        category: str,
        key: str = "default",
    ) -> UserPreference | None:
        """
        Get the current best preference for a (category, key).

        Explicit preferences always beat inferred ones.
        Returns None if no preference exists.
        """
        self._ensure_ready()
        category = _norm(category)
        key = _norm(key)

        row = self._store.query_one(
            "SELECT * FROM user_preferences WHERE category = ? AND key = ?",
            (category, key),
        )
        if not row:
            return None

        pref = self._row_to_preference(row)
        state.last_preference_used = pref.to_dict()
        return pref

    def get_all_preferences(self, category: str | None = None) -> list[UserPreference]:
        """Return all stored preferences, optionally filtered by category."""
        self._ensure_ready()
        if category:
            rows = self._store.query(
                "SELECT * FROM user_preferences WHERE category = ? ORDER BY confidence DESC",
                (_norm(category),),
            )
        else:
            rows = self._store.query(
                "SELECT * FROM user_preferences ORDER BY category, confidence DESC",
            )
        return [self._row_to_preference(r) for r in rows]

    def set_explicit_preference(
        self,
        category: str,
        key: str,
        value: str,
    ) -> UserPreference:
        """
        Set an explicit user preference (overrides inferred values).

        Returns the updated preference.
        """
        self._ensure_ready()
        category = _norm(category)
        key = _norm(key)
        value = value.strip()
        now = _utc_iso()

        self._store.execute(
            """
            INSERT INTO user_preferences (category, key, value, confidence, evidence_count, is_explicit, updated_at)
            VALUES (?, ?, ?, 1.0, 1, 1, ?)
            ON CONFLICT(category, key) DO UPDATE SET
                value = excluded.value,
                confidence = 1.0,
                is_explicit = 1,
                updated_at = excluded.updated_at
            """,
            (category, key, value, now),
        )

        # Also record as a strong signal so the inference aligns
        self.record_signal(category, key, value, weight=5.0, source="explicit")

        pref = UserPreference(
            category=category, key=key, value=value,
            confidence=1.0, evidence_count=1,
            is_explicit=True, updated_at=now,
        )
        state.last_preference_used = pref.to_dict()
        logger.info("Explicit preference set: %s/%s = %s", category, key, value)
        return pref

    # ================================================================== #
    # Ranking & application                                              #
    # ================================================================== #

    def rank_options(
        self,
        category: str,
        candidates: Sequence[str],
        key: str = "default",
    ) -> list[tuple[str, float]]:
        """
        Rank a list of candidate values by preference score.

        Returns list of (value, score) sorted by score descending.
        """
        self._ensure_ready()
        if not candidates:
            return []

        category = _norm(category)
        key = _norm(key)
        signals = self._get_signals(category, key)
        now_ts = datetime.now(timezone.utc).timestamp()

        scores: dict[str, float] = {c.lower(): 0.0 for c in candidates}

        for sig in signals:
            val = sig["value"].lower()
            if val in scores:
                age_days = max(0, (now_ts - self._parse_ts(sig["created_at"])) / 86400)
                decay = 0.5 ** (age_days / self.DECAY_HALF_LIFE_DAYS)
                scores[val] += float(sig["weight"]) * decay

        # Boost if explicit preference matches
        pref = self.get_preference(category, key)
        if pref and pref.value.lower() in scores:
            scores[pref.value.lower()] += 10.0 if pref.is_explicit else 5.0

        result = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return result

    def apply_defaults(
        self,
        intent: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Enrich an options dict with personalised defaults where missing.

        For example, if ``intent='search'`` and no browser is specified,
        fill in the preferred browser.
        """
        self._ensure_ready()
        if not settings.get("allow_personalized_defaults"):
            return options

        result = dict(options)
        intent_lower = intent.strip().lower()

        if intent_lower == "search" and not result.get("browser"):
            pref = self.get_preference(Category.BROWSER)
            if pref and pref.confidence >= 0.5:
                result["browser"] = pref.value
                logger.info("Applied personalised default: browser=%s", pref.value)

        if intent_lower in {"play", "play_music"} and not result.get("app"):
            pref = self.get_preference(Category.MUSIC_APP)
            if pref and pref.confidence >= 0.5:
                result["app"] = pref.value
                logger.info("Applied personalised default: music_app=%s", pref.value)

        if intent_lower in {"play", "play_music"} and not result.get("genre"):
            pref = self.get_preference(Category.MUSIC_GENRE)
            if pref and pref.confidence >= 0.4:
                result["genre"] = pref.value

        return result

    # ================================================================== #
    # Forget / privacy                                                   #
    # ================================================================== #

    def forget_preference(
        self,
        category: str,
        key: str | None = None,
    ) -> int:
        """
        Delete preference(s) and their signals.

        If key is None, all preferences in the category are deleted.
        Returns the number of rows removed.
        """
        self._ensure_ready()
        category = _norm(category)
        total = 0

        if key:
            key = _norm(key)
            total += self._store.execute(
                "DELETE FROM user_preferences WHERE category = ? AND key = ?",
                (category, key),
            ).rowcount
            total += self._store.execute(
                "DELETE FROM preference_signals WHERE category = ? AND key = ?",
                (category, key),
            ).rowcount
        else:
            total += self._store.execute(
                "DELETE FROM user_preferences WHERE category = ?",
                (category,),
            ).rowcount
            total += self._store.execute(
                "DELETE FROM preference_signals WHERE category = ?",
                (category,),
            ).rowcount

        logger.info("Forgot preference: category=%s key=%s rows=%d", category, key or "*", total)
        return total

    def forget_all(self) -> int:
        """Delete all preferences and signals."""
        self._ensure_ready()
        a = self._store.execute("DELETE FROM user_preferences").rowcount
        b = self._store.execute("DELETE FROM preference_signals").rowcount
        logger.info("All preferences forgotten (%d rows)", a + b)
        return a + b

    # ================================================================== #
    # Recomputation                                                      #
    # ================================================================== #

    def recompute_preferences(self) -> int:
        """
        Recompute all aggregated preferences from raw signals.

        Returns the number of preferences updated.
        """
        self._ensure_ready()
        # Get distinct (category, key) pairs from signals
        pairs = self._store.query(
            "SELECT DISTINCT category, key FROM preference_signals"
        )
        count = 0
        for pair in pairs:
            self._recompute_one(pair["category"], pair["key"])
            count += 1
        logger.info("Recomputed %d preference(s)", count)
        return count

    def _recompute_one(self, category: str, key: str) -> None:
        """Recompute a single preference from its signals."""
        # Check for explicit override — don't overwrite
        existing = self._store.query_one(
            "SELECT is_explicit FROM user_preferences WHERE category = ? AND key = ?",
            (category, key),
        )
        if existing and existing.get("is_explicit"):
            return

        signals = self._get_signals(category, key)
        if not signals:
            return

        # Aggregate with time-decay
        now_ts = datetime.now(timezone.utc).timestamp()
        value_scores: dict[str, float] = {}
        value_counts: dict[str, int] = {}

        for sig in signals:
            val = sig["value"].lower()
            age_days = max(0, (now_ts - self._parse_ts(sig["created_at"])) / 86400)
            decay = 0.5 ** (age_days / self.DECAY_HALF_LIFE_DAYS)
            weighted = float(sig["weight"]) * decay
            value_scores[val] = value_scores.get(val, 0.0) + weighted
            value_counts[val] = value_counts.get(val, 0) + 1

        if not value_scores:
            return

        # Best value
        best_value = max(value_scores, key=value_scores.get)
        best_score = value_scores[best_value]
        total_score = sum(value_scores.values())
        evidence = value_counts.get(best_value, 0)

        # Confidence = dominance ratio × evidence factor
        dominance = best_score / total_score if total_score > 0 else 0.0
        evidence_factor = min(1.0, evidence / max(self.MIN_EVIDENCE, 1))
        confidence = dominance * evidence_factor

        now = _utc_iso()
        self._store.execute(
            """
            INSERT INTO user_preferences (category, key, value, confidence, evidence_count, is_explicit, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(category, key) DO UPDATE SET
                value = excluded.value,
                confidence = excluded.confidence,
                evidence_count = excluded.evidence_count,
                updated_at = excluded.updated_at
            WHERE is_explicit = 0
            """,
            (category, key, best_value, round(confidence, 4), evidence, now),
        )

    # ================================================================== #
    # Stats                                                              #
    # ================================================================== #

    def stats(self) -> dict[str, Any]:
        """Return personalization statistics."""
        self._ensure_ready()
        return {
            "total_signals": self._store.row_count("preference_signals"),
            "total_preferences": self._store.row_count("user_preferences"),
            "explicit_count": self._store.query_scalar(
                "SELECT COUNT(*) FROM user_preferences WHERE is_explicit = 1", default=0,
            ),
            "inferred_count": self._store.query_scalar(
                "SELECT COUNT(*) FROM user_preferences WHERE is_explicit = 0", default=0,
            ),
            "categories": [
                r["category"] for r in self._store.query(
                    "SELECT DISTINCT category FROM user_preferences ORDER BY category"
                )
            ],
        }

    def profile_summary(self) -> list[dict[str, str]]:
        """Return a user-facing preference summary."""
        self._ensure_ready()
        prefs = self.get_all_preferences()
        summary = []
        for p in prefs:
            if p.confidence < 0.3 and not p.is_explicit:
                continue
            label = f"{p.category}/{p.key}" if p.key != "default" else p.category
            summary.append({
                "label": label.replace("_", " ").title(),
                "value": p.value,
                "confidence": f"{p.confidence:.0%}",
                "source": "set by you" if p.is_explicit else "learned",
            })
        state.active_profile_summary = summary
        return summary

    # ================================================================== #
    # Internal helpers                                                   #
    # ================================================================== #

    def _get_signals(self, category: str, key: str) -> list[dict[str, Any]]:
        return self._store.query(
            "SELECT * FROM preference_signals WHERE category = ? AND key = ? ORDER BY created_at DESC",
            (category, key),
        )

    def _prune_signals(self, category: str, key: str) -> None:
        """Keep only the most recent MAX_SIGNALS_PER_KEY signals."""
        count = self._store.query_scalar(
            "SELECT COUNT(*) FROM preference_signals WHERE category = ? AND key = ?",
            (category, key), default=0,
        )
        if count > self.MAX_SIGNALS_PER_KEY:
            excess = count - self.MAX_SIGNALS_PER_KEY
            self._store.execute(
                "DELETE FROM preference_signals WHERE id IN ("
                "  SELECT id FROM preference_signals WHERE category = ? AND key = ?"
                "  ORDER BY created_at ASC LIMIT ?"
                ")",
                (category, key, excess),
            )

    def _row_to_preference(self, row: dict[str, Any]) -> UserPreference:
        return UserPreference(
            category=str(row.get("category", "")),
            key=str(row.get("key", "")),
            value=str(row.get("value", "")),
            confidence=float(row.get("confidence", 0)),
            evidence_count=int(row.get("evidence_count", 0)),
            is_explicit=bool(row.get("is_explicit")),
            updated_at=str(row.get("updated_at", "")),
        )

    @staticmethod
    def _parse_ts(ts_str: str) -> float:
        try:
            return datetime.fromisoformat(ts_str).timestamp()
        except (ValueError, TypeError):
            try:
                return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except (ValueError, TypeError):
                return 0.0

    def _ensure_ready(self) -> None:
        if not self._ready:
            self.init()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", "_", text.strip().lower())


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

personalization_engine = PersonalizationEngine()
