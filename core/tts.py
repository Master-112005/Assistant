"""
Compatibility wrapper for the production TTS service.
"""
from __future__ import annotations

from core.tts_service import (
    BaseTTSEngine,
    Pyttsx3Engine,
    TTSService,
    TTSState,
    TTSPriority,
    TTSSpeechItem,
    TextToSpeechService,
    get_tts_service,
    set_tts_service,
)


class TextToSpeechEngine(TTSService):
    """Backward-compatible app-facing TTS class."""


__all__ = [
    "BaseTTSEngine",
    "Pyttsx3Engine",
    "TTSService",
    "TTSState",
    "TTSPriority",
    "TTSSpeechItem",
    "TextToSpeechEngine",
    "TextToSpeechService",
    "get_tts_service",
    "set_tts_service",
]
