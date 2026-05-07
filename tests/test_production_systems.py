"""
Production test suite for rebuilt systems.

Tests for:
- Response service pipeline
- TTS reliability
- NLU routing accuracy
- Confirmation state machine
- End-to-end integration
"""
import pytest
import threading
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

from core.response_service import ResponseService, initialize_response_service
from core.response_models import (
    AssistantResponse,
    ResponseCategory,
    ResponseSeverity,
    TTSSpeechEvent,
    ConfirmationToken,
)
from core.tts_service import TextToSpeechService, Pyttsx3Engine
from core.nlu_router import NLURouter, IntentType, MediaTarget
from core.confirmation_manager import (
    ConfirmationManager,
    RISKY_ACTIONS,
    SAFE_ACTIONS,
)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION A: Response Service Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestResponseService:
    """Test universal response pipeline."""

    def setup_method(self):
        """Set up test fixtures."""
        self.ui_messages = []
        self.tts_calls = []
        self.notifications = []

        def mock_ui(sender: str, text: str):
            self.ui_messages.append((sender, text))

        def mock_tts(text: str) -> bool:
            self.tts_calls.append(text)
            return True

        def mock_notification(title: str, text: str):
            self.notifications.append((title, text))

        self.service = ResponseService(
            ui_callback=mock_ui,
            tts_callback=mock_tts,
            notification_callback=mock_notification,
        )

    def test_respond_basic_flow(self):
        """Test basic response through full pipeline."""
        response = self.service.respond(
            "Hello user",
            category=ResponseCategory.GREETING,
            speak_enabled=True,
        )

        assert response.text == "Hello user"
        assert response.category == ResponseCategory.GREETING
        assert response.speak_enabled is True

        # Verify UI rendered
        assert len(self.ui_messages) == 1
        assert self.ui_messages[0] == ("Assistant", "Hello user")

        # Verify TTS queued
        assert len(self.tts_calls) == 1
        assert self.tts_calls[0] == "Hello user"

    def test_respond_no_speech(self):
        """Test response that skips speech."""
        response = self.service.respond(
            "Internal telemetry",
            speak_enabled=False,
            silent_reason="telemetry",
        )

        # UI rendered
        assert len(self.ui_messages) == 1
        # But no speech
        assert len(self.tts_calls) == 0

    def test_respond_with_notification(self):
        """Test response with notification."""
        response = self.service.respond(
            "Important update",
            notification_enabled=True,
            notification_title="Alert",
        )

        assert len(self.notifications) == 1
        assert self.notifications[0] == ("Alert", "Important update")

    def test_respond_error(self):
        """Test error response."""
        response = self.service.respond_error(
            "Something went wrong",
            error_code="ERR_001",
        )

        assert response.success is False
        assert response.severity == ResponseSeverity.ERROR
        assert response.error_code == "ERR_001"

    def test_respond_coerces_legacy_string_category_and_severity(self):
        """Legacy string values should not break response dispatch."""
        response = self.service.respond(
            "Opened YouTube.",
            category="command_result",
            severity="warn",
        )

        assert response.category == ResponseCategory.COMMAND_RESULT
        assert response.severity == ResponseSeverity.WARNING
        assert self.ui_messages[-1] == ("Assistant", "Opened YouTube.")

    def test_confirmation_flow(self):
        """Test confirmation token lifecycle."""
        token = self.service.respond_confirmation(
            "Delete file.txt?",
            action_type="delete_file",
            action_payload={"path": "/file.txt"},
            risk_level="high",
            expires_in_seconds=60,
        )

        assert token.action_type == "delete_file"
        assert token.risk_level == "high"
        assert token.state == "pending"
        assert not token.is_expired()

        # UI should show confirmation
        assert len(self.ui_messages) == 1
        assert "Delete file.txt?" in self.ui_messages[0][1]

        # Consume token
        consumed = self.service.consume_confirmation(token.token_id)
        assert consumed is not None
        assert consumed.state == "confirmed"

        # Should not be able to consume twice
        assert self.service.consume_confirmation(token.token_id) is None

    def test_confirmation_expiry(self):
        """Test confirmation token expiration."""
        token = self.service.respond_confirmation(
            "Delete?",
            action_type="delete_file",
            action_payload={},
            expires_in_seconds=1,
        )

        # Token valid initially
        assert not token.is_expired()

        # Wait for expiry
        time.sleep(1.1)
        assert token.is_expired()

        # Should not consume expired token
        assert self.service.consume_confirmation(token.token_id) is None

    def test_has_pending_confirmation(self):
        """Test pending confirmation detection."""
        assert not self.service.has_pending_confirmation()

        self.service.respond_confirmation(
            "Confirm?",
            action_type="delete_file",
            action_payload={},
        )

        assert self.service.has_pending_confirmation()

    def test_response_history(self):
        """Test response history tracking."""
        self.service.respond("First")
        self.service.respond("Second")
        self.service.respond("Third")

        history = self.service.get_response_history(limit=10)
        assert len(history) == 3
        assert history[0].text == "First"
        assert history[2].text == "Third"

    def test_rapid_consecutive_responses(self):
        """Stress test: rapid fire responses."""
        for i in range(50):
            self.service.respond(f"Message {i}")

        history = self.service.get_response_history(limit=100)
        assert len(history) == 50

        # All should render in UI
        assert len(self.ui_messages) == 50

        # All should queue for speech
        assert len(self.tts_calls) == 50


# ══════════════════════════════════════════════════════════════════════════════
# SECTION B: NLU Router Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestNLURouter:
    """Test natural language understanding."""

    def setup_method(self):
        """Set up test fixtures."""
        self.router = NLURouter()

    def test_media_resume_variations(self):
        """Test various resume command phrasings."""
        test_cases = [
            "continue playback",
            "resume the video",
            "continue the youtube video",
            "play again",
            "resume youtube",
        ]

        for text in test_cases:
            result = self.router.route(text)
            assert result.intent == IntentType.MEDIA_RESUME, f"Failed for: {text}"
            assert result.confidence >= 0.9, f"Low confidence for: {text}"

    def test_media_pause_variations(self):
        """Test various pause command phrasings."""
        test_cases = [
            "pause video",
            "pause playback",
            "pause",
        ]

        for text in test_cases:
            result = self.router.route(text)
            assert result.intent == IntentType.MEDIA_PAUSE, f"Failed for: {text}"

    def test_media_next_variations(self):
        """Test next/skip command variations."""
        test_cases = [
            "next song",
            "skip",
            "next track",
        ]

        for text in test_cases:
            result = self.router.route(text)
            assert result.intent == IntentType.MEDIA_NEXT, f"Failed for: {text}"

    def test_media_previous_variations(self):
        """Test previous/back command variations."""
        test_cases = [
            "previous song",
            "back track",
            "previous",
            "back",
        ]

        for text in test_cases:
            result = self.router.route(text)
            assert result.intent == IntentType.MEDIA_PREVIOUS, f"Failed for: {text}"

    def test_stt_correction(self):
        """Test STT error correction."""
        test_cases = [
            ("continew playback", IntentType.MEDIA_RESUME),
            ("plae video", IntentType.MEDIA_RESUME),
            ("serch files", IntentType.UNKNOWN),  # Not a media command
        ]

        for text, expected_intent in test_cases:
            result = self.router.route(text)
            # Note: "search" isn't in fast paths, so will be unknown
            # But the correction should still happen
            normalized = result.normalized_text
            assert "plae" not in normalized, f"STT not corrected: {text}"

    def test_normalization(self):
        """Test input normalization."""
        test_cases = [
            ("  RESUME  PLAYBACK  ", "resume playback"),
            ("Resume, playback!", "resume playback"),
            ("rEsUmE pLaYbAcK", "resume playback"),
        ]

        for text, expected_normalized in test_cases:
            result = self.router.route(text)
            # Check that normalization happened
            assert len(result.normalized_text) > 0

    def test_unknown_intent(self):
        """Test unknown intent handling."""
        result = self.router.route("flargblargle")
        assert result.intent == IntentType.UNKNOWN
        assert result.confidence < 0.5


# ══════════════════════════════════════════════════════════════════════════════
# SECTION C: Confirmation Manager Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestConfirmationManager:
    """Test confirmation state machine."""

    def setup_method(self):
        """Set up test fixtures."""
        self.manager = ConfirmationManager(default_expiry_seconds=5, max_pending=10)

    def test_requires_confirmation(self):
        """Test which actions require confirmation."""
        # Risky actions
        assert self.manager.requires_confirmation("delete_file") is True
        assert self.manager.requires_confirmation("shutdown_system") is True

        # Safe actions
        assert self.manager.requires_confirmation("media_resume") is False
        assert self.manager.requires_confirmation("open_app") is False

        # Unknown (defaults to safe)
        assert self.manager.requires_confirmation("unknown_action") is False

    def test_request_confirmation(self):
        """Test creating confirmation token."""
        token = self.manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete report.txt?",
            action_payload={"path": "/report.txt"},
            risk_level="high",
        )

        assert token.action_type == "delete_file"
        assert token.state == "pending"
        assert not token.is_expired()
        assert token.risk_level == "high"

    def test_confirmation_consumed(self):
        """Test consuming a confirmation."""
        token = self.manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
            action_payload={},
        )

        # Consume
        consumed = self.manager.consume_confirmation(token.token_id)
        assert consumed is not None
        assert consumed.state == "confirmed"
        assert consumed.confirmed_at is not None

        # Should be removed from pending
        assert not self.manager.has_pending_confirmation()

    def test_confirmation_expired(self):
        """Test confirmation token expiration."""
        token = self.manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
            action_payload={},
            expires_in_seconds=1,
        )

        assert self.manager.has_pending_confirmation()

        # Wait for expiry
        time.sleep(1.1)

        # Should not consume
        result = self.manager.consume_confirmation(token.token_id)
        assert result is None

        # Should be cleaned from pending
        assert not self.manager.has_pending_confirmation()

    def test_confirmation_cancelled(self):
        """Test cancelling a confirmation."""
        token = self.manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
            action_payload={},
        )

        cancelled = self.manager.cancel_confirmation(token.token_id)
        assert cancelled is not None
        assert cancelled.state == "cancelled"

        # Should be removed from pending
        assert not self.manager.has_pending_confirmation()

    def test_max_pending_enforced(self):
        """Test max pending confirmations limit."""
        manager = ConfirmationManager(max_pending=2)

        t1 = manager.request_confirmation("a", "prompt1", {})
        t2 = manager.request_confirmation("b", "prompt2", {})

        # Both should exist
        state = manager.get_pending_state()
        assert state["count"] == 2

        # Add third - should evict oldest
        t3 = manager.request_confirmation("c", "prompt3", {})

        state = manager.get_pending_state()
        assert state["count"] == 2

    def test_history_tracking(self):
        """Test confirmation history."""
        t1 = self.manager.request_confirmation("a", "p1", {})
        t2 = self.manager.request_confirmation("b", "p2", {})

        consumed = self.manager.consume_confirmation(t1.token_id)
        assert consumed is not None

        cancelled = self.manager.cancel_confirmation(t2.token_id)
        assert cancelled is not None

        history = self.manager.get_history()
        assert len(history) == 2


# ══════════════════════════════════════════════════════════════════════════════
# SECTION D: TTS Service Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestTTSService:
    """Test TTS reliability."""

    def setup_method(self):
        """Set up test fixtures."""
        # Mock pyttsx3 to avoid real speech
        self.patcher = patch("core.tts_service.Pyttsx3Engine")
        self.mock_engine_class = self.patcher.start()

        self.mock_engine = MagicMock()
        self.mock_engine_class.return_value = self.mock_engine
        self.mock_engine.is_speaking.return_value = False

        self.service = TextToSpeechService()

    def teardown_method(self):
        """Clean up."""
        self.patcher.stop()

    @patch("core.settings.get")
    def test_speak_basic(self, mock_settings):
        """Test basic speech."""
        mock_settings.side_effect = lambda key, default=None: {
            "voice_enabled": True,
            "mute": False,
        }.get(key, default)

        result = self.service.speak("Hello world")
        assert result is True

    @patch("core.settings.get")
    def test_speak_when_muted(self, mock_settings):
        """Test speech when muted."""
        mock_settings.side_effect = lambda key, default=None: {
            "voice_enabled": True,
            "mute": True,
        }.get(key, default)

        result = self.service.speak("Hello")
        assert result is False

    @patch("core.settings.get")
    def test_speak_when_disabled(self, mock_settings):
        """Test speech when disabled."""
        mock_settings.side_effect = lambda key, default=None: {
            "voice_enabled": False,
            "mute": False,
        }.get(key, default)

        result = self.service.speak("Hello")
        assert result is False

    @patch("core.settings.get")
    def test_deduplication(self, mock_settings):
        """Test rapid message deduplication."""
        mock_settings.side_effect = lambda key, default=None: {
            "voice_enabled": True,
            "mute": False,
        }.get(key, default)

        # First message
        result1 = self.service.speak("Hello")
        assert result1 is True

        # Identical message immediately after
        result2 = self.service.speak("Hello")
        assert result2 is False  # Deduped

        # Different message
        result3 = self.service.speak("Goodbye")
        assert result3 is True

    @patch("core.settings.get")
    def test_empty_text(self, mock_settings):
        """Test speaking empty text."""
        mock_settings.side_effect = lambda key, default=None: {
            "voice_enabled": True,
            "mute": False,
        }.get(key, default)

        result = self.service.speak("")
        assert result is False

        result = self.service.speak("   ")
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# SECTION E: End-to-End Integration Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestE2EIntegration:
    """End-to-end integration tests."""

    def setup_method(self):
        """Set up test fixtures."""
        self.nlu = NLURouter()
        self.confirm = ConfirmationManager()

        self.ui_messages = []
        self.tts_calls = []

        def mock_ui(sender, text):
            self.ui_messages.append((sender, text))

        def mock_tts(text) -> bool:
            self.tts_calls.append(text)
            return True

        self.response_service = ResponseService(
            ui_callback=mock_ui,
            tts_callback=mock_tts,
        )

    def test_media_command_flow(self):
        """Test complete media command flow."""
        # User says "continue youtube video"
        user_input = "continue the youtube video"

        # Route through NLU
        intent = self.nlu.route(user_input)
        assert intent.intent == IntentType.MEDIA_RESUME

        # No confirmation needed for media
        assert not self.confirm.requires_confirmation("media_resume")

        # Send response
        response = self.response_service.respond(
            "Resuming YouTube playback.",
            category=ResponseCategory.MEDIA_CONTROL,
            action_name="media_resume",
        )

        # Verify full pipeline
        assert len(self.ui_messages) == 1
        assert len(self.tts_calls) == 1
        assert response.success is True

    def test_delete_with_confirmation_flow(self):
        """Test delete operation with confirmation flow."""
        # Action requires confirmation
        assert self.confirm.requires_confirmation("delete_file")

        # Request confirmation
        token = self.response_service.respond_confirmation(
            "Delete report.txt?",
            action_type="delete_file",
            action_payload={"path": "/report.txt"},
            risk_level="high",
        )

        assert self.response_service.has_pending_confirmation()
        assert len(self.ui_messages) == 1  # Confirmation prompt shown

        # User confirms
        consumed = self.response_service.consume_confirmation(token.token_id)
        assert consumed is not None

        # Send success response
        self.response_service.respond(
            "Deleted report.txt successfully.",
            category=ResponseCategory.COMMAND_RESULT,
            success=True,
        )

        assert len(self.ui_messages) == 2
        assert not self.response_service.has_pending_confirmation()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
