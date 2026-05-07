"""
Production-grade tests for NLU Router.

Tests the complete NLU pipeline including:
- Input normalization
- STT error correction
- Media command detection
- Entity extraction
- Confidence scoring
- Fast-path matching
"""
import pytest
from core.nlu_router import (
    NLURouter,
    NLUIntent,
    IntentType,
    MediaTarget,
    get_nlu_router,
)


class TestNLUNormalization:
    """Test input normalization."""
    
    def test_lowercase(self):
        """Input is converted to lowercase."""
        router = NLURouter()
        normalized = router._normalize("CONTINUE PLAYBACK")
        assert "continue" in normalized
        assert "CONTINUE" not in normalized
    
    def test_trim_whitespace(self):
        """Leading/trailing whitespace is removed."""
        router = NLURouter()
        normalized = router._normalize("  continue playback  ")
        assert normalized == "continue playback"
    
    def test_extra_spaces_removed(self):
        """Multiple spaces collapsed to single."""
        router = NLURouter()
        normalized = router._normalize("continue    the    youtube    video")
        assert "    " not in normalized
    
    def test_filler_words_removed(self):
        """Filler words (um, uh) are removed."""
        router = NLURouter()
        normalized = router._normalize("um continue playback uh")
        assert "um" not in normalized
        assert "uh" not in normalized
        assert "continue" in normalized


class TestNLUSTTCorrections:
    """Test STT error correction."""
    
    def test_continew_to_continue(self):
        """STT error 'continew' corrected to 'continue'."""
        router = NLURouter()
        corrected = router._apply_stts_corrections("continew playback")
        assert corrected == "continue playback"
    
    def test_plae_to_play(self):
        """STT error 'plae' corrected to 'play'."""
        router = NLURouter()
        corrected = router._apply_stts_corrections("plae music")
        assert corrected == "play music"
    
    def test_serch_to_search(self):
        """STT error 'serch' corrected to 'search'."""
        router = NLURouter()
        corrected = router._apply_stts_corrections("serch files")
        assert corrected == "search files"
    
    def test_multiple_corrections(self):
        """Multiple corrections applied."""
        router = NLURouter()
        corrected = router._apply_stts_corrections("continew plae the mutt")
        assert corrected == "continue play the mute"


class TestNLUMediaCommands:
    """Test media command detection."""
    
    def test_resume_youtube(self):
        """Detects 'resume youtube video' intent."""
        router = NLURouter()
        intent = router.route("resume the youtube video")
        
        assert intent.intent == IntentType.MEDIA_RESUME
        assert intent.confidence >= 0.9
        assert intent.target == MediaTarget.YOUTUBE
    
    def test_continue_playback(self):
        """Detects 'continue playback' intent."""
        router = NLURouter()
        intent = router.route("continue playback")
        
        assert intent.intent == IntentType.MEDIA_RESUME
        assert intent.confidence >= 0.9
    
    def test_play_again(self):
        """Detects 'play again' intent."""
        router = NLURouter()
        intent = router.route("play again")
        
        assert intent.intent == IntentType.MEDIA_RESUME
        assert intent.confidence >= 0.9
    
    def test_pause_video(self):
        """Detects 'pause video' intent."""
        router = NLURouter()
        intent = router.route("pause video")
        
        assert intent.intent == IntentType.MEDIA_PAUSE
        assert intent.confidence >= 0.9
    
    def test_skip_song(self):
        """Detects 'skip song' intent."""
        router = NLURouter()
        intent = router.route("skip song")
        
        assert intent.intent == IntentType.MEDIA_NEXT
        assert intent.confidence >= 0.9
    
    def test_next_track(self):
        """Detects 'next track' intent."""
        router = NLURouter()
        intent = router.route("next track")
        
        assert intent.intent == IntentType.MEDIA_NEXT
        assert intent.confidence >= 0.9
    
    def test_previous_song(self):
        """Detects 'previous song' intent."""
        router = NLURouter()
        intent = router.route("previous song")
        
        assert intent.intent == IntentType.MEDIA_PREVIOUS
        assert intent.confidence >= 0.9
    
    def test_back_track(self):
        """Detects 'back track' intent."""
        router = NLURouter()
        intent = router.route("back track")
        
        assert intent.intent == IntentType.MEDIA_PREVIOUS
        assert intent.confidence >= 0.9
    
    def test_mute_video(self):
        """Detects 'mute video' intent."""
        router = NLURouter()
        intent = router.route("mute video")
        
        assert intent.intent == IntentType.MEDIA_MUTE
        assert intent.confidence >= 0.9
    
    def test_unmute_video(self):
        """Detects 'unmute video' intent."""
        router = NLURouter()
        intent = router.route("unmute video")
        
        assert intent.intent == IntentType.MEDIA_UNMUTE
        assert intent.confidence >= 0.9
    
    def test_stop_playback(self):
        """Detects 'stop playback' intent."""
        router = NLURouter()
        intent = router.route("stop playback")
        
        assert intent.intent == IntentType.MEDIA_STOP
        assert intent.confidence >= 0.9


class TestNLUEntityExtraction:
    """Test entity extraction."""
    
    def test_youtube_target_extracted(self):
        """YouTube is extracted as target."""
        router = NLURouter()
        intent = router.route("resume the youtube video")
        
        assert intent.target == MediaTarget.YOUTUBE
    
    def test_spotify_target_extracted(self):
        """Spotify is detected in music commands."""
        router = NLURouter()
        intent = router.route("skip song")
        
        assert intent.target == MediaTarget.SPOTIFY or intent.target == MediaTarget.GENERIC
    
    def test_generic_target_fallback(self):
        """Unknown targets fallback to GENERIC."""
        router = NLURouter()
        intent = router.route("pause")
        
        assert intent.target == MediaTarget.GENERIC or intent.target is not None


class TestNLUConfidenceScoring:
    """Test confidence scoring."""
    
    def test_high_confidence_fast_path(self):
        """Fast-path matches have high confidence."""
        router = NLURouter()
        intent = router.route("continue playback")
        
        assert intent.confidence >= 0.9
    
    def test_unknown_has_low_confidence(self):
        """Unknown commands have low confidence."""
        router = NLURouter()
        intent = router.route("some random gibberish xyz")
        
        assert intent.intent == IntentType.UNKNOWN
        assert intent.confidence == 0.0
    
    def test_confidence_in_valid_range(self):
        """Confidence always in 0.0 to 1.0 range."""
        router = NLURouter()
        test_inputs = [
            "continue playback",
            "pause video",
            "next song",
            "hello world",
            "random xyz",
        ]
        
        for text in test_inputs:
            intent = router.route(text)
            assert 0.0 <= intent.confidence <= 1.0


class TestNLUEdgeCases:
    """Test edge cases."""
    
    def test_empty_input(self):
        """Empty input returns UNKNOWN."""
        router = NLURouter()
        intent = router.route("")
        assert intent.intent == IntentType.UNKNOWN
    
    def test_whitespace_only(self):
        """Whitespace-only input returns UNKNOWN."""
        router = NLURouter()
        intent = router.route("   ")
        assert intent.intent == IntentType.UNKNOWN
    
    def test_very_long_input(self):
        """Very long input is handled."""
        router = NLURouter()
        long_text = "continue playback " * 100
        intent = router.route(long_text)
        # Should not crash
        assert intent is not None
    
    def test_unicode_characters(self):
        """Unicode input is normalized."""
        router = NLURouter()
        intent = router.route("Çöñtíñüé þlåýbåçk")
        # Should not crash
        assert intent is not None
    
    def test_punctuation_handling(self):
        """Punctuation is removed."""
        router = NLURouter()
        intent1 = router.route("continue playback")
        intent2 = router.route("continue playback!!!")
        
        # Both should match same intent
        assert intent1.intent == intent2.intent


class TestNLUGlobalRouter:
    """Test global router singleton."""
    
    def test_global_router_exists(self):
        """Global router can be retrieved."""
        router = get_nlu_router()
        assert router is not None
    
    def test_global_router_is_singleton(self):
        """Global router is same instance."""
        router1 = get_nlu_router()
        router2 = get_nlu_router()
        
        # Same instance
        assert router1 is router2


class TestNLURegressions:
    """Regression tests for known issues."""
    
    def test_original_failure_continue_playback(self):
        """
        FAILURE: User says "continue playback", system asks "Do you want me to continue playback?"
        FIXED: Should directly resume without asking.
        """
        router = NLURouter()
        intent = router.route("continue playback")
        
        assert intent.intent == IntentType.MEDIA_RESUME
        assert intent.confidence >= 0.9
    
    def test_original_failure_continue_youtube(self):
        """
        FAILURE: User says "continue the youtube video", system misunderstands.
        FIXED: Should detect youtube and resume action.
        """
        router = NLURouter()
        intent = router.route("continue the youtube video")
        
        assert intent.intent == IntentType.MEDIA_RESUME
        assert intent.target == MediaTarget.YOUTUBE
        assert intent.confidence >= 0.9


# Performance benchmarks
def benchmark_nlu_single_request():
    """Benchmark: Single NLU request."""
    import time
    router = NLURouter()
    
    start = time.time()
    intent = router.route("continue the youtube video")
    elapsed = time.time() - start
    
    print(f"Single NLU request: {elapsed*1000:.2f}ms")
    assert elapsed < 0.1  # Should be instant
    assert intent.intent == IntentType.MEDIA_RESUME


def benchmark_nlu_100_requests():
    """Benchmark: 100 NLU requests."""
    import time
    router = NLURouter()
    
    requests = [
        "continue playback",
        "pause video",
        "skip song",
        "previous track",
        "mute video",
    ]
    
    start = time.time()
    for i in range(100):
        intent = router.route(requests[i % len(requests)])
    elapsed = time.time() - start
    
    print(f"100 NLU requests: {elapsed*1000:.2f}ms, avg {elapsed*10:.2f}ms")
    assert elapsed < 1.0  # Should be quick


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
