"""
Speech-to-Text module using faster-whisper.
"""
from __future__ import annotations

import time
import numpy as np
from core.logger import get_logger
from core import settings
from core import state
from core.paths import MODELS_DIR
import os

logger = get_logger(__name__)

class SpeechToTextEngine:
    def __init__(self):
        self.model = None
        # Use "small.en" instead of "base.en" for better command recognition
        # "small" is optimized for short English phrases and commands
        self.model_name = settings.get("stt_model") or "small.en"
        self.language = self._normalize_language(settings.get("speech_language") or "en")
        self.silence_threshold = settings.get("silence_threshold") or 0.01
        self.sample_rate = int(settings.get("sample_rate") or 16000)

        # Whisper models download to models/whisper by default
        self.download_root = str(MODELS_DIR / "whisper")
        os.makedirs(self.download_root, exist_ok=True)

    def apply_runtime_settings(
        self,
        *,
        language: str | None = None,
        sample_rate: int | None = None,
        silence_threshold: float | None = None,
        model_name: str | None = None,
    ) -> None:
        """Apply persisted speech settings to the live STT engine."""
        if language is not None:
            self.language = self._normalize_language(language)
        if sample_rate is not None:
            self.sample_rate = max(8000, int(sample_rate))
        if silence_threshold is not None:
            self.silence_threshold = max(0.0, float(silence_threshold))
        if model_name is not None:
            requested_model = str(model_name).strip()
            if requested_model and requested_model != self.model_name:
                self.model_name = requested_model
                self.model = None
                state.stt_loaded = False
        logger.info(
            "STT runtime settings applied",
            language=self.language,
            sample_rate=self.sample_rate,
            silence_threshold=self.silence_threshold,
            model=self.model_name,
        )
        
    def load_model(self):
        """Loads the Faster-Whisper model into memory."""
        if self.is_loaded():
            return

        from faster_whisper import WhisperModel
        logger.info(f"Loading STT model: {self.model_name}...")
        
        try:
            # We use auto to fallback to CPU if CUDA is not available
            self.model = WhisperModel(
                self.model_name,
                device="auto",
                compute_type="default",
                download_root=self.download_root
            )
            state.stt_loaded = True
            logger.info("STT ready")
        except Exception as e:
            logger.error(f"Failed to load STT model: {e}")
            state.stt_loaded = False
            raise e

    def is_loaded(self) -> bool:
        """Returns True if the model is currently loaded."""
        return self.model is not None and state.stt_loaded

    def set_model(self, name: str):
        """Changes the model and reloads."""
        if self.model_name != name:
            self.model_name = name
            self.model = None
            state.stt_loaded = False
            self.load_model()

    def set_language(self, code: str):
        """Changes the expected language."""
        self.language = self._normalize_language(code)

    def transcribe(self, audio_data: np.ndarray = None, file_path: str = None) -> dict:
        """
        Transcribes audio data (numpy array) or a file.
        Returns a dict with text, language, and timings.
        """
        if not self.is_loaded():
            self.load_model()
            
        if audio_data is None and file_path is None:
            raise ValueError("Must provide either audio_data or file_path")

        # Basic silence detection for numpy data
        short_command = False
        if audio_data is not None:
            audio_data = np.asarray(audio_data, dtype=np.float32).reshape(-1)
            if audio_data.size == 0:
                logger.warning("Empty audio provided to STT.")
                return self._empty_result()

            rms = np.sqrt(np.mean(np.square(audio_data)))
            if rms < self.silence_threshold:
                logger.info(f"Audio below silence threshold (RMS: {rms:.4f})")
                return self._empty_result("No speech detected.")
            short_command = (audio_data.size / max(1, self.sample_rate)) <= 3.0

        logger.info("Transcription started")
        start_time = time.time()
        
        try:
            # target can be either file path or numpy array (faster_whisper accepts 1D float32 array)
            target = audio_data.flatten() if audio_data is not None else file_path

            segments, info = self.model.transcribe(
                target,
                language=self.language if self.language != "auto" else None,
                beam_size=1 if short_command else 5,
                best_of=1 if short_command else 5,
                temperature=0.0,
                condition_on_previous_text=False,
                vad_filter=True, # Built-in Voice Activity Detection to trim silence
                vad_parameters=dict(min_silence_duration_ms=400 if short_command else 500)
            )
            
            text = " ".join([segment.text.strip() for segment in segments])
            text = text.strip()
            
            processing_time = time.time() - start_time
            state.last_stt_time = processing_time
            state.last_transcript = text
            
            logger.info(f"Transcript: {text}")
            logger.info(f"Time: {processing_time:.2f} sec")
            
            return {
                "text": text,
                "language": info.language,
                "duration": info.duration,
                "processing_time": processing_time
            }
            
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            raise e

    def _empty_result(self, default_text="") -> dict:
        return {
            "text": default_text,
            "language": self.language,
            "duration": 0.0,
            "processing_time": 0.0
        }

    @staticmethod
    def _normalize_language(code: str) -> str:
        text = str(code or "en").strip()
        if not text:
            return "en"
        return text.split("-")[0]
