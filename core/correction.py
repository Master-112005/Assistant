"""
Production-grade STT correction engine with 5-layer rule-based pipeline.

Layer 1: Text normalization
Layer 2: Dictionary/alias corrections
Layer 3: Vocabulary boosting
Layer 4: Validation
Layer 5: Safe fallback
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from threading import RLock
from typing import Any

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover - exercised in minimal environments
    class _FallbackFuzz:
        @staticmethod
        def token_ratio(left: str, right: str) -> int:
            return int(SequenceMatcher(None, left, right).ratio() * 100)

    fuzz = _FallbackFuzz()

from core.logger import get_logger

from core.normalizer import normalize_command_result
from core.paths import DATA_DIR
from core.validator import CorrectionValidator, ValidationResult
from core.vocabulary import get_vocabulary

logger = get_logger(__name__)

CUSTOM_TERMS_PATH = DATA_DIR / "custom_terms.json"
CORRECTIONS_CACHE_PATH = DATA_DIR / "corrections_cache.json"
DEFAULT_CACHE_MAX_ENTRIES = 1024
DEFAULT_CORRECTION_CONFIDENCE_THRESHOLD = 0.75
DEFAULT_FUZZY_MATCH_THRESHOLD = 80


class CorrectionResult:
    """Result of a correction operation."""

    def __init__(
        self,
        original_text: str,
        corrected_text: str,
        confidence: float = 0.0,
        method_used: str = "none",
        changes: list[dict[str, Any]] | None = None,
        safe_to_apply: bool = False,
    ) -> None:
        self.original_text = original_text
        self.corrected_text = corrected_text
        self.confidence = max(0.0, min(1.0, confidence))  # Clamp 0-1
        self.method_used = method_used
        self.changes = changes or []
        self.safe_to_apply = safe_to_apply

    def __repr__(self) -> str:
        return (
            f"CorrectionResult(original='{self.original_text}', "
            f"corrected='{self.corrected_text}', confidence={self.confidence:.2f}, "
            f"method={self.method_used}, safe={self.safe_to_apply})"
        )


class STTCorrector:
    """Rule-based STT correction engine."""

    def __init__(
        self,
        confidence_threshold: float = DEFAULT_CORRECTION_CONFIDENCE_THRESHOLD,
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.vocab = get_vocabulary()
        self.validator = CorrectionValidator()
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_lock = RLock()
        self._load_cache()
        logger.info("STTCorrector initialized")

    def correct(self, text: str) -> CorrectionResult:
        """
        Main entry point: correct noisy STT output through 5-layer pipeline.

        Returns:
            CorrectionResult with corrected text and metadata
        """
        text = (text or "").strip()

        if not text:
            logger.warning("Empty text passed to corrector")
            return CorrectionResult(
                text,
                text,
                confidence=0.0,
                method_used="empty",
                safe_to_apply=False,
            )

        # Check cache first
        cached = self._cache_get(text)
        if cached:
            logger.info("Correction cache hit: '%s'", text)
            return self._deserialize_result(cached)

        original = text
        logger.info("Correcting: '%s'", text)

        # Layer 1: Normalize
        text = self.normalize(text)
        logger.debug("After normalize: '%s'", text)

        # Layer 2: Dictionary corrections
        text, dict_confidence, dict_changes = self.dictionary_correct(text)
        logger.debug("After dict: '%s' (conf: %.2f)", text, dict_confidence)

        # Layer 3: Vocabulary boosting
        text, vocab_confidence, vocab_changes = self.vocabulary_boost(text)
        logger.debug("After vocab: '%s' (conf: %.2f)", text, vocab_confidence)

        # Combine confidence from layers 2-3
        combined_confidence = max(dict_confidence, vocab_confidence)

        # Layer 4: Validate
        validation = self.validator.validate(original, text, combined_confidence)
        safe_to_apply = validation.is_safe
        logger.info("Validation: %s", validation)

        # Layer 5: Safe fallback
        final_text = text if safe_to_apply else original
        final_confidence = combined_confidence if safe_to_apply else 0.0
        method = "rules"

        result = CorrectionResult(
            original_text=original,
            corrected_text=final_text,
            confidence=final_confidence,
            method_used=method,
            changes=[],
            safe_to_apply=safe_to_apply,
        )

        logger.info("Result: %s", result)

        # Cache the result
        self._cache_set(original, self._serialize_result(result))

        return result

    def normalize(self, text: str) -> str:
        """
        Layer 1: Normalize text.
        - Trim spaces
        - Fix multiple spaces
        - Standard punctuation
        - Basic capitalization
        """
        text = (text or "").strip()

        # Fix multiple spaces
        text = re.sub(r"\s+", " ", text)

        # Standardize punctuation
        text = re.sub(r"\s+([.,;:!?])", r"\1", text)

        # Remove repeated punctuation
        text = re.sub(r"([.!?])+", r"\1", text)

        return text.strip()

    def dictionary_correct(self, text: str) -> tuple[str, float, list[dict[str, Any]]]:
        """
        Layer 2: Dictionary and alias corrections using fuzzy matching.
        Common STT mistakes: crome->chrome, yutube->youtube, watsapp->whatsapp, etc.

        Returns:
            (corrected_text, confidence, changes_list)
        """
        normalized_result = normalize_command_result(text)
        if normalized_result.requires_confirmation:
            return text, normalized_result.confidence, []
        if normalized_result.normalized_text and normalized_result.normalized_text != text:
            changes = [
                {
                    "original": change.original,
                    "corrected": change.corrected,
                    "reason": change.reason,
                    "score": change.confidence,
                }
                for change in normalized_result.corrections
            ]
            best_confidence = max(
                [normalized_result.confidence, *[float(change.confidence) for change in normalized_result.corrections]],
                default=0.0,
            )
            return normalized_result.normalized_text, best_confidence, changes

        original = text
        words = text.split()
        corrections_made = []
        confidence_scores = []

        for i, word in enumerate(words):
            word_clean = word.strip(".,;:!?()[]{}").lower()

            if not word_clean or len(word_clean) < 2:
                continue

            # Try to find fuzzy match
            best_match = self._fuzzy_match_term(word_clean)

            if best_match:
                old_word = words[i]
                # Preserve original capitalization pattern
                new_word = self._apply_capitalization(best_match["term"], word)
                words[i] = new_word

                corrections_made.append(
                    {
                        "original": old_word,
                        "corrected": new_word,
                        "reason": "dictionary",
                        "score": best_match["score"],
                    }
                )
                confidence_scores.append(best_match["score"] / 100.0)

        corrected = " ".join(words)
        confidence = (
            max(confidence_scores) if confidence_scores else 0.0
        )  # Use best match confidence

        if corrected != original:
            logger.debug("Dict corrections: %s", corrections_made)

        return corrected, confidence, corrections_made

    def vocabulary_boost(self, text: str) -> tuple[str, float, list[dict[str, Any]]]:
        """
        Layer 3: Vocabulary boosting - prefer known apps/terms when ambiguous.
        Example: "app music" -> "Apple Music"

        Returns:
            (corrected_text, confidence, changes_list)
        """
        original = text
        words = text.split()
        corrections_made = []
        confidence_scores = []

        # Look for known app names within the text
        for i, word in enumerate(words):
            word_clean = word.strip(".,;:!?()[]{}").lower()

            if not word_clean:
                continue

            # Try to normalize against known vocabulary
            normalized = self.vocab.normalize_term(word_clean)

            if normalized and normalized != word:
                old_word = words[i]
                words[i] = normalized
                corrections_made.append(
                    {
                        "original": old_word,
                        "corrected": normalized,
                        "reason": "vocabulary_boost",
                        "score": 0.95,
                    }
                )
                confidence_scores.append(0.95)

        # Also try multi-word matches (e.g., "app music" -> "Apple Music")
        text_lower = " ".join(words).lower()
        for app_name in list(self.vocab.get_all_apps().keys()):
            if app_name in text_lower and app_name not in text.lower():
                # Found a canonical form not in original but in corrected
                confidence_scores.append(0.85)

        corrected = " ".join(words)
        confidence = max(confidence_scores) if confidence_scores else 0.0

        if corrected != original:
            logger.debug("Vocab boost corrections: %s", corrections_made)

        return corrected, confidence, corrections_made

    def validate(self, original: str, corrected: str, confidence: float = 1.0) -> ValidationResult:
        """
        Layer 4: Validate correction for safety.
        """
        return self.validator.validate(original, corrected, confidence)

    def load_custom_terms(self) -> dict[str, dict[str, Any]]:
        """Load user-defined custom correction terms."""
        return self.vocab.load_custom_terms()

    def add_term(self, term: str, aliases: list[str] | None = None) -> None:
        """Add a custom correction term."""
        self.vocab.add_term(term, aliases)

    def _fuzzy_match_term(self, word: str, threshold: int = DEFAULT_FUZZY_MATCH_THRESHOLD) -> dict[str, Any] | None:
        """
        Find best fuzzy match for a word against all known terms.

        Returns:
            {"term": str, "score": int} or None
        """
        if not word or len(word) < 2:
            return None

        candidates = {}

        # Collect all known terms - use lower case for matching
        for term in list(self.vocab.get_all_apps().keys()):
            candidates[term.lower()] = "app"
        for term in list(self.vocab.get_all_verbs().keys()):
            candidates[term.lower()] = "verb"
        for term in list(self.vocab.get_all_system_terms().keys()):
            candidates[term.lower()] = "system"

        # Also check custom terms
        for custom_term in self.vocab._custom_terms.keys():
            candidates[custom_term.lower()] = "custom"

        # Add common contact names and variations
        contact_names = ["hemanth", "hemant", "john", "jane", "bob", "alice", "mike", "sarah"]
        for name in contact_names:
            candidates[name] = "contact"

        # Add common app names and their variations
        common_apps = [
            "whatsapp", "telegram", "discord", "teams", "slack", "zoom",
            "spotify", "youtube", "chrome", "edge", "firefox",
            "notepad", "calculator", "explorer", "vscode", "code"
        ]
        for app in common_apps:
            if app not in candidates:
                candidates[app] = "common_app"

        word_lower = word.lower()
        best_match = None
        best_score = threshold

        for candidate_term, term_type in candidates.items():
            # Skip exact matches - no need to correct
            if word_lower == candidate_term:
                continue

            # Use partial ratio for better handling of substrings
            score = fuzz.token_ratio(word_lower, candidate_term)

            # For short words, use a lower threshold
            if len(word_lower) <= 4 and score >= threshold - 10:
                if score > best_score:
                    best_score = score
                    best_match = candidate_term
            elif score > best_score:
                best_score = score
                best_match = candidate_term

        if best_match:
            return {"term": best_match, "score": best_score}

        return None

    @staticmethod
    def _apply_capitalization(corrected_word: str, original_word: str) -> str:
        """Apply original capitalization pattern to corrected word."""
        if not original_word or not corrected_word:
            return corrected_word

        # All caps
        if original_word.isupper():
            return corrected_word.upper()

        # Title case
        if original_word[0].isupper():
            return corrected_word.capitalize()

        # All lower
        return corrected_word.lower()

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        """Get cached correction result."""
        with self._cache_lock:
            cache_key = self._make_cache_key(key)
            entry = self._cache.get(cache_key)
            if entry:
                return entry
        return None

    def _cache_set(self, key: str, value: dict[str, Any]) -> None:
        """Cache a correction result."""
        with self._cache_lock:
            cache_key = self._make_cache_key(key)
            self._cache[cache_key] = value

            if len(self._cache) > DEFAULT_CACHE_MAX_ENTRIES:
                # Remove oldest entries
                oldest_keys = sorted(
                    self._cache.keys(),
                    key=lambda k: self._cache[k].get("timestamp", 0),
                )
                for old_key in oldest_keys[: len(self._cache) - DEFAULT_CACHE_MAX_ENTRIES]:
                    self._cache.pop(old_key, None)

            self._persist_cache()

    def _load_cache(self) -> None:
        """Load cache from disk."""
        if not CORRECTIONS_CACHE_PATH.exists():
            return

        try:
            with open(CORRECTIONS_CACHE_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._cache = loaded
                    logger.info("Loaded cache with %d entries", len(self._cache))
        except Exception as e:
            logger.warning("Failed to load correction cache: %s", e)

    def _persist_cache(self) -> None:
        """Persist cache to disk."""
        try:
            CORRECTIONS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(CORRECTIONS_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2, ensure_ascii=True)
        except Exception as e:
            logger.warning("Failed to persist correction cache: %s", e)

    @staticmethod
    def _make_cache_key(text: str) -> str:
        """Create a cache key from text."""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"corr:{digest}"

    @staticmethod
    def _serialize_result(result: CorrectionResult) -> dict[str, Any]:
        """Serialize a CorrectionResult to JSON-compatible dict."""
        return {
            "original_text": result.original_text,
            "corrected_text": result.corrected_text,
            "confidence": result.confidence,
            "method_used": result.method_used,
            "safe_to_apply": result.safe_to_apply,
            "timestamp": time.time(),
        }

    @staticmethod
    def _deserialize_result(data: dict[str, Any]) -> CorrectionResult:
        """Deserialize a dict back to CorrectionResult."""
        return CorrectionResult(
            original_text=data.get("original_text", ""),
            corrected_text=data.get("corrected_text", ""),
            confidence=data.get("confidence", 0.0),
            method_used=data.get("method_used", "cached"),
            safe_to_apply=data.get("safe_to_apply", False),
        )


_corrector_instance: STTCorrector | None = None


def get_corrector() -> STTCorrector:
    """Get or create the global STT corrector instance."""
    global _corrector_instance
    if _corrector_instance is None:
        _corrector_instance = STTCorrector()
    return _corrector_instance
