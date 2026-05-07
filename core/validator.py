"""
Validation and safety checks for STT correction.
Prevents harmful over-corrections and ensures semantic preservation.
"""
from __future__ import annotations

from difflib import SequenceMatcher

from core.logger import get_logger
from core.vocabulary import get_vocabulary

logger = get_logger(__name__)

DEFAULT_MAX_SIMILARITY_THRESHOLD = 0.3  # Allow up to 30% difference
DEFAULT_MIN_CONFIDENCE_THRESHOLD = 0.6
DEFAULT_LEVENSHTEIN_MAX_DISTANCE = 5


class ValidationResult:
    """Result of validation check."""

    def __init__(
        self,
        is_safe: bool,
        reason: str = "",
        similarity: float = 1.0,
        confidence: float = 1.0,
    ) -> None:
        self.is_safe = is_safe
        self.reason = reason
        self.similarity = similarity
        self.confidence = confidence

    def __repr__(self) -> str:
        return f"ValidationResult(is_safe={self.is_safe}, similarity={self.similarity:.2f}, confidence={self.confidence:.2f})"


class CorrectionValidator:
    """Validate corrections for safety and quality."""

    def __init__(
        self,
        max_similarity_threshold: float = DEFAULT_MAX_SIMILARITY_THRESHOLD,
        min_confidence_threshold: float = DEFAULT_MIN_CONFIDENCE_THRESHOLD,
    ) -> None:
        self.max_similarity_threshold = max_similarity_threshold
        self.min_confidence_threshold = min_confidence_threshold
        self.vocab = get_vocabulary()

    def validate(
        self,
        original: str,
        corrected: str,
        confidence: float = 1.0,
    ) -> ValidationResult:
        """
        Validate that a correction is safe to apply.

        Args:
            original: Original (noisy) text
            corrected: Corrected text
            confidence: Confidence score of correction (0-1)

        Returns:
            ValidationResult with safety determination and reason
        """
        original = (original or "").strip()
        corrected = (corrected or "").strip()

        if not original or not corrected:
            return ValidationResult(False, reason="Empty text", similarity=0.0, confidence=confidence)

        if original.lower() == corrected.lower():
            return ValidationResult(
                True,
                reason="Text unchanged (already correct)",
                similarity=1.0,
                confidence=confidence,
            )

        # Check confidence threshold
        if confidence < self.min_confidence_threshold:
            return ValidationResult(
                False,
                reason=f"Confidence {confidence:.2f} below threshold {self.min_confidence_threshold}",
                similarity=self.similarity_score(original, corrected),
                confidence=confidence,
            )

        # Check if too different
        similarity = self.similarity_score(original, corrected)
        if similarity < (1.0 - self.max_similarity_threshold):
            return ValidationResult(
                False,
                reason=f"Correction too different (similarity {similarity:.2f})",
                similarity=similarity,
                confidence=confidence,
            )

        # Check if meaning preserved
        if not self.intent_preserved(original, corrected):
            return ValidationResult(
                False,
                reason="Original intent not preserved",
                similarity=similarity,
                confidence=confidence,
            )

        # Check for recognized entities
        if not self.contains_known_entities(corrected) and not self._looks_valid_command(corrected):
            return ValidationResult(
                False,
                reason="Corrected text contains no recognized entities",
                similarity=similarity,
                confidence=confidence,
            )

        return ValidationResult(
            True,
            reason="Correction passed all validation checks",
            similarity=similarity,
            confidence=confidence,
        )

    @staticmethod
    def similarity_score(text_a: str, text_b: str) -> float:
        """
        Calculate similarity between two texts (0-1).
        1.0 = identical, 0.0 = completely different.
        """
        if not text_a or not text_b:
            return 0.0

        text_a_lower = text_a.lower()
        text_b_lower = text_b.lower()

        if text_a_lower == text_b_lower:
            return 1.0

        matcher = SequenceMatcher(None, text_a_lower, text_b_lower)
        return matcher.ratio()

    def intent_preserved(self, original: str, corrected: str) -> bool:
        """
        Check if the original intent is preserved in the correction.
        Checks:
        - Key verbs present
        - Major nouns present
        - Command structure similar
        """
        original_lower = original.lower()
        corrected_lower = corrected.lower()

        # Tokenize
        original_tokens = set(original_lower.split())
        corrected_tokens = set(corrected_lower.split())

        # Check if any major action verbs were removed
        for verb in ["open", "search", "play", "delete", "create", "move", "copy", "save"]:
            if verb in original_lower and verb not in corrected_lower:
                # Allow removal if similar verb is present
                similar_verbs = ["launch", "start", "run", "find", "look", "remove", "erase"]
                if not any(sv in corrected_lower for sv in similar_verbs if sv != verb):
                    logger.warning(
                        "Major action verb '%s' removed in correction: '%s' -> '%s'",
                        verb,
                        original,
                        corrected,
                    )
                    return False

        # Check token overlap (should have significant overlap)
        # But be lenient if the texts are already similar at character level
        if original_tokens and corrected_tokens:
            overlap = len(original_tokens & corrected_tokens)
            total_unique = len(original_tokens | corrected_tokens)

            # If we have some direct token overlap, that's good
            if overlap == 0:
                # No direct overlap - check if this might be due to spelling corrections
                # (e.g., "opun" -> "open", "crome" -> "chrome")
                # In this case, high character similarity should be sufficient
                char_similarity = self.similarity_score(original, corrected)
                if char_similarity < 0.6:
                    logger.warning(
                        "Low token overlap and low char similarity: '%s' -> '%s' (similarity: %.2f)",
                        original,
                        corrected,
                        char_similarity,
                    )
                    return False
            else:
                # We have some token overlap, check it's reasonable
                overlap_ratio = overlap / max(len(original_tokens), len(corrected_tokens))
                if overlap_ratio < 0.2:  # More lenient threshold
                    logger.warning(
                        "Low token overlap: '%s' -> '%s' (overlap: %.2f)",
                        original,
                        corrected,
                        overlap_ratio,
                    )
                    return False

        return True

    def contains_known_entities(self, text: str) -> bool:
        """
        Check if text contains at least one known entity
        (app name, verb, system term).
        """
        words = text.lower().split()
        for word in words:
            word_clean = word.strip(".,;:!?()[]{}").lower()
            if (
                self.vocab.is_known_app(word_clean)
                or self.vocab.is_known_verb(word_clean)
                or self.vocab.is_known_system_term(word_clean)
            ):
                return True
        return False

    @staticmethod
    def _looks_valid_command(text: str) -> bool:
        """Check if text looks like a valid command structure."""
        words = text.split()
        if not words:
            return False

        # Should start with a verb or contain common command structure
        first_word = words[0].lower()
        command_starters = ["open", "search", "play", "create", "delete", "move", "save", "copy"]

        if any(first_word.startswith(starter) for starter in command_starters):
            return True

        # Or contain recognizable sentence structure
        if len(words) >= 2:
            return True

        return False

    def too_different(self, original: str, corrected: str) -> bool:
        """Check if correction is too different from original."""
        similarity = self.similarity_score(original, corrected)
        return similarity < (1.0 - self.max_similarity_threshold)


def validate_correction(
    original: str,
    corrected: str,
    confidence: float = 1.0,
) -> ValidationResult:
    """
    Convenience function to validate a single correction.
    """
    validator = CorrectionValidator()
    return validator.validate(original, corrected, confidence)
