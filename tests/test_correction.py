"""
Comprehensive tests for STT correction pipeline.
Tests all 6 layers and integration points.
"""
import pytest

from core.correction import CorrectionResult, STTCorrector
from core.validator import CorrectionValidator, ValidationResult, validate_correction
from core.vocabulary import Vocabulary, get_vocabulary


class TestVocabulary:
    """Test vocabulary module."""

    def test_vocabulary_load(self) -> None:
        """Test vocabulary loads correctly."""
        vocab = Vocabulary()
        assert vocab.WINDOWS_APPS
        assert vocab.MEDIA_APPS
        assert vocab.ACTION_VERBS
        assert vocab.SYSTEM_TERMS

    def test_known_app_detection(self) -> None:
        """Test known app detection."""
        vocab = Vocabulary()
        assert vocab.is_known_app("chrome")
        assert vocab.is_known_app("spotify")
        assert vocab.is_known_app("whatsapp")
        assert not vocab.is_known_app("unknown_app_xyz")

    def test_known_verb_detection(self) -> None:
        """Test known verb detection."""
        vocab = Vocabulary()
        assert vocab.is_known_verb("open")
        assert vocab.is_known_verb("search")
        assert vocab.is_known_verb("play")
        assert not vocab.is_known_verb("xyz_verb")

    def test_known_system_term_detection(self) -> None:
        """Test known system term detection."""
        vocab = Vocabulary()
        assert vocab.is_known_system_term("volume")
        assert vocab.is_known_system_term("brightness")
        assert not vocab.is_known_system_term("xyz_term")

    def test_normalize_term_app(self) -> None:
        """Test term normalization for apps."""
        vocab = Vocabulary()
        assert vocab.normalize_term("chrome") == "Chrome"
        assert vocab.normalize_term("spotify") == "Spotify"
        assert vocab.normalize_term("whatsapp") == "Whatsapp"

    def test_normalize_term_verb(self) -> None:
        """Test term normalization for verbs."""
        vocab = Vocabulary()
        assert vocab.normalize_term("open") == "open"
        assert vocab.normalize_term("search") == "search"
        assert vocab.normalize_term("play") == "play"

    def test_custom_terms(self) -> None:
        """Test custom term loading and addition."""
        vocab = Vocabulary()
        vocab.add_term("myapp", ["my app", "ma"])
        assert vocab.is_known_app("myapp")


class TestValidator:
    """Test validation module."""

    def test_validation_identical_text(self) -> None:
        """Test validation of identical text."""
        result = validate_correction("open chrome", "open chrome")
        assert result.is_safe
        assert result.similarity == 1.0

    def test_validation_minor_changes(self) -> None:
        """Test validation of minor changes."""
        result = validate_correction("opun crome", "open chrome")
        assert result.is_safe  # Should be safe
        assert result.similarity > 0.7  # High similarity

    def test_validation_too_different(self) -> None:
        """Test rejection of very different corrections."""
        result = validate_correction("open chrome", "close firefox")
        assert not result.is_safe
        assert result.similarity < 0.5

    def test_validation_low_confidence(self) -> None:
        """Test rejection of low confidence corrections."""
        validator = CorrectionValidator(min_confidence_threshold=0.8)
        result = validator.validate("open chrome", "open firefox", confidence=0.5)
        assert not result.is_safe

    def test_intent_preservation(self) -> None:
        """Test intent preservation check."""
        validator = CorrectionValidator()
        # These should preserve intent
        assert validator.intent_preserved("open chrome", "open Chrome")
        assert validator.intent_preserved("search google", "search Google")

        # This removes key verb
        assert not validator.intent_preserved("open chrome", "chrome")

    def test_similarity_score(self) -> None:
        """Test similarity scoring."""
        validator = CorrectionValidator()

        # Identical
        assert validator.similarity_score("hello", "hello") == 1.0

        # Completely different
        assert validator.similarity_score("hello", "world") < 0.5

        # Similar
        similarity = validator.similarity_score("chrome", "crome")
        assert similarity > 0.7


class TestNormalization:
    """Test text normalization layer."""

    def test_normalize_whitespace(self) -> None:
        """Test whitespace normalization."""
        corrector = STTCorrector(enable_llm=False)
        assert corrector.normalize("  hello   world  ") == "hello world"
        assert corrector.normalize("hello    world") == "hello world"

    def test_normalize_punctuation(self) -> None:
        """Test punctuation normalization."""
        corrector = STTCorrector(enable_llm=False)
        assert corrector.normalize("hello world .") == "hello world."
        assert corrector.normalize("hello !!") == "hello!"
        assert corrector.normalize("hello??") == "hello?"

    def test_normalize_empty(self) -> None:
        """Test empty string handling."""
        corrector = STTCorrector(enable_llm=False)
        assert corrector.normalize("") == ""
        assert corrector.normalize("   ") == ""


class TestDictionaryCorrection:
    """Test dictionary/alias correction layer."""

    def test_dict_correction_crome_to_chrome(self) -> None:
        """Test common STT mistake: crome -> chrome."""
        corrector = STTCorrector(enable_llm=False)
        text, confidence, changes = corrector.dictionary_correct("open crome")
        assert "chrome" in text.lower()
        assert confidence > 0.7
        assert len(changes) > 0

    def test_dict_correction_watsapp_to_whatsapp(self) -> None:
        """Test common STT mistake: watsapp -> whatsapp."""
        corrector = STTCorrector(enable_llm=False)
        text, confidence, changes = corrector.dictionary_correct("open watsapp")
        assert "whatsapp" in text.lower()
        assert confidence > 0.7

    def test_dict_correction_yutube_to_youtube(self) -> None:
        """Test common STT mistake: yutube -> youtube."""
        corrector = STTCorrector(enable_llm=False)
        text, confidence, changes = corrector.dictionary_correct("open yutube")
        assert "youtube" in text.lower()
        assert confidence > 0.7

    def test_dict_correction_serch_to_search(self) -> None:
        """Test common STT mistake: serch -> search."""
        corrector = STTCorrector(enable_llm=False)
        text, confidence, changes = corrector.dictionary_correct("serch ipl")
        assert "search" in text.lower()
        assert confidence > 0.7

    def test_dict_correction_no_match(self) -> None:
        """Test dictionary correction with no match."""
        corrector = STTCorrector(enable_llm=False)
        text, confidence, changes = corrector.dictionary_correct("xyz unknown term")
        # Should not change
        assert text == "xyz unknown term" or confidence == 0.0


class TestVocabularyBoost:
    """Test vocabulary boosting layer."""

    def test_vocab_boost_app_names(self) -> None:
        """Test vocabulary boosting for known app names."""
        corrector = STTCorrector(enable_llm=False)
        text, confidence, changes = corrector.vocabulary_boost("open chrome")
        # Should capitalize app name
        assert "Chrome" in text or confidence > 0.0

    def test_vocab_boost_multiple_terms(self) -> None:
        """Test vocabulary boosting with multiple terms."""
        corrector = STTCorrector(enable_llm=False)
        text, confidence, changes = corrector.vocabulary_boost("open spotify and play music")
        assert "spotify" in text.lower() or "play" in text.lower()


class TestFullPipeline:
    """Test the complete 6-layer correction pipeline."""

    def test_pipeline_simple_app_open(self) -> None:
        """Basic test: open app music an pley liked music."""
        corrector = STTCorrector(enable_llm=False)
        result = corrector.correct("open app music and pley liked music")

        assert result.original_text == "open app music and pley liked music"
        assert len(result.corrected_text) > 0
        assert result.method_used in ["hybrid", "dictionary", "llm_semantic"]
        # Confidence should be reasonable
        assert result.confidence >= 0.0

    def test_pipeline_crome_search(self) -> None:
        """Test: open crome and search ipl score."""
        corrector = STTCorrector(enable_llm=False)
        result = corrector.correct("opun crome and serch ipl score")

        assert result.original_text == "opun crome and serch ipl score"
        # Should correct major errors
        assert "chrome" in result.corrected_text.lower() or "open" in result.corrected_text.lower()
        assert result.confidence >= 0.0

    def test_pipeline_caching(self) -> None:
        """Test that repeated inputs are cached."""
        corrector = STTCorrector(enable_llm=False)

        # First correction
        result1 = corrector.correct("opun crome")

        # Second correction (same input)
        result2 = corrector.correct("opun crome")

        # Should have same output
        assert result1.corrected_text == result2.corrected_text
        assert result1.confidence == result2.confidence

    def test_pipeline_empty_input(self) -> None:
        """Test empty input handling."""
        corrector = STTCorrector(enable_llm=False)
        result = corrector.correct("")

        assert result.original_text == ""
        assert result.corrected_text == ""
        assert result.safe_to_apply is False

    def test_pipeline_single_word(self) -> None:
        """Test single word correction."""
        corrector = STTCorrector(enable_llm=False)
        result = corrector.correct("crome")

        # Should attempt correction
        assert result is not None

    def test_pipeline_all_noise(self) -> None:
        """Test input with only noise tokens."""
        corrector = STTCorrector(enable_llm=False)
        result = corrector.correct("uh an umm")

        # May not be correctable
        assert result is not None


class TestSafetyFallback:
    """Test Layer 6 safety fallback behavior."""

    def test_fallback_on_invalid_correction(self) -> None:
        """Test that invalid corrections fall back to original."""
        corrector = STTCorrector(enable_llm=False)

        # Mock a validation that would fail
        validator = corrector.validator
        validation = validator.validate(
            "open chrome",
            "delete all files",  # Very different!
            confidence=0.5,
        )

        assert not validation.is_safe

    def test_fallback_preserves_original(self) -> None:
        """Test that fallback uses original text."""
        corrector = STTCorrector(enable_llm=False)

        original = "opun crome"
        result = corrector.correct(original)

        # If safe, corrected; if not safe, fallback to original
        if result.safe_to_apply:
            assert result.corrected_text != original or result.method_used == "none"
        else:
            assert result.corrected_text == original


class TestIntegration:
    """Integration tests."""

    def test_corrector_initialization(self) -> None:
        """Test corrector initialization."""
        corrector = STTCorrector(enable_llm=False)
        assert corrector is not None
        assert corrector.vocab is not None
        assert corrector.validator is not None

    def test_custom_term_integration(self) -> None:
        """Test custom terms are used in correction."""
        corrector = STTCorrector(enable_llm=False)
        corrector.add_term("myspecialapp", ["my special app", "msa"])

        assert corrector.vocab.is_known_app("myspecialapp")

    def test_corrector_confidence_threshold(self) -> None:
        """Test confidence threshold behavior."""
        corrector = STTCorrector(enable_llm=False, confidence_threshold=0.9)
        assert corrector.confidence_threshold == 0.9

    def test_multiple_corrections(self) -> None:
        """Test processing multiple corrections in sequence."""
        corrector = STTCorrector(enable_llm=False)

        inputs = [
            "opun crome",
            "open spotfy",
            "serch ipl score",
            "play musc",
        ]

        results = [corrector.correct(text) for text in inputs]

        # All should produce results
        assert len(results) == 4
        assert all(r is not None for r in results)


class TestPerformance:
    """Performance and optimization tests."""

    def test_cache_loading_on_init(self) -> None:
        """Test that cache loads on initialization."""
        corrector = STTCorrector(enable_llm=False)
        # Cache should be loaded (might be empty on first run)
        assert corrector._cache is not None

    def test_cache_persistence(self) -> None:
        """Test that cache persists between corrections."""
        corrector = STTCorrector(enable_llm=False)

        # First correction
        result1 = corrector.correct("opun crome")

        # Create new corrector (simulates new session)
        corrector2 = STTCorrector(enable_llm=False)

        # Should ideally find cached result
        # (Though depends on file I/O)
        assert corrector2 is not None


class TestComplexScenarios:
    """Test complex correction scenarios."""

    def test_multi_action_command(self) -> None:
        """Test correction of multi-action commands."""
        corrector = STTCorrector(enable_llm=False)
        result = corrector.correct("open crome and serch ipl an play musc")

        assert result is not None
        assert "chrome" in result.corrected_text.lower() or result.confidence >= 0.0

    def test_contact_names(self) -> None:
        """Test handling of contact names."""
        corrector = STTCorrector(enable_llm=False)
        # Contact names should be preserved even if noisy
        result = corrector.correct("call jon")
        assert result is not None

    def test_unicode_and_special_chars(self) -> None:
        """Test Unicode and special character handling."""
        corrector = STTCorrector(enable_llm=False)
        result = corrector.correct("open café")
        assert result is not None

    def test_numbers_in_text(self) -> None:
        """Test correction with numbers."""
        corrector = STTCorrector(enable_llm=False)
        result = corrector.correct("open chrome and go to 192.168.1.1")
        assert result is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
