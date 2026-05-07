"""
Reliable queued Text-to-Speech service.

This module provides the production TTS subsystem used by the assistant.
The public surface is intentionally compatible with the legacy `core.tts`
wrapper and the existing tests.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum
from typing import Any, Callable, Optional

from core import settings, state
from core.logger import get_logger
from core.response_models import TTSSpeechEvent

logger = get_logger(__name__)


class TTSState(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    SPEAKING = "speaking"
    CANCELLED = "cancelled"
    FAILED = "failed"
    RECOVERING = "recovering"


class TTSPriority(IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


@dataclass(order=True)
class TTSSpeechItem:
    sort_index: tuple[int, int] = field(init=False, repr=False)
    priority: TTSPriority
    sequence: int
    text: str = field(compare=False)
    response_id: str | None = field(default=None, compare=False)
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)
    created_at: datetime = field(default_factory=datetime.utcnow, compare=False)
    item_id: str = field(default_factory=lambda: uuid.uuid4().hex, compare=False)

    def __post_init__(self) -> None:
        self.sort_index = (int(self.priority), int(self.sequence))


def _coinit_sta() -> None:
    """Initialize COM STA on the current thread when available."""
    try:
        import pythoncom

        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    except Exception:
        pass


class BaseTTSEngine:
    """Blocking engine adapter used only from the TTS worker thread."""

    def initialize(self) -> None:
        raise NotImplementedError

    def speak(self, text: str) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def shutdown(self) -> None:
        raise NotImplementedError

    def set_voice(self, voice_id: str) -> None:
        raise NotImplementedError

    def set_rate(self, rate: int) -> None:
        raise NotImplementedError

    def set_volume(self, volume: float) -> None:
        raise NotImplementedError

    def get_voices(self) -> list[Any]:
        raise NotImplementedError


class Pyttsx3Engine(BaseTTSEngine):
    """pyttsx3 adapter owned by a single worker thread."""

    def __init__(self) -> None:
        self._engine = None

    def initialize(self) -> None:
        if self._engine is not None:
            return
        import pyttsx3

        self._engine = pyttsx3.init()

    def speak(self, text: str) -> None:
        if self._engine is None:
            self.initialize()
        if self._engine is None:
            raise RuntimeError("TTS engine is unavailable.")
        self._engine.say(text)
        self._engine.runAndWait()

    def stop(self) -> None:
        if self._engine is None:
            return
        try:
            self._engine.stop()
        except Exception:
            logger.debug("pyttsx3 stop failed", exc_info=True)

    def shutdown(self) -> None:
        if self._engine is None:
            return
        try:
            self._engine.stop()
        except Exception:
            logger.debug("pyttsx3 shutdown stop failed", exc_info=True)
        finally:
            self._engine = None

    def set_voice(self, voice_id: str) -> None:
        if self._engine is None:
            self.initialize()
        if self._engine is not None:
            self._engine.setProperty("voice", voice_id)

    def set_rate(self, rate: int) -> None:
        if self._engine is None:
            self.initialize()
        if self._engine is not None:
            self._engine.setProperty("rate", rate)

    def set_volume(self, volume: float) -> None:
        if self._engine is None:
            self.initialize()
        if self._engine is not None:
            self._engine.setProperty("volume", volume)

    def get_voices(self) -> list[Any]:
        if self._engine is None:
            self.initialize()
        if self._engine is None:
            return []
        return list(self._engine.getProperty("voices") or [])


class TTSService:
    """
    Persistent queued speech service.

    The UI thread never blocks on speech. All heavy work is isolated to a
    single worker thread that owns the engine lifecycle and telemetry.
    """

    def __init__(
        self,
        *,
        engine_factory: Callable[[], BaseTTSEngine] | None = None,
        deduplicate_window_seconds: float = 1.0,
        watchdog_seconds: float = 30.0,
    ) -> None:
        self._engine_factory = engine_factory or Pyttsx3Engine
        self._deduplicate_window_seconds = max(0.0, float(deduplicate_window_seconds))
        self._watchdog_seconds = max(5.0, float(watchdog_seconds))
        self._queue: queue.PriorityQueue[TTSSpeechItem] = queue.PriorityQueue()
        self._history_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._listeners: list[Callable[[str, str], None]] = []
        self._telemetry: list[TTSSpeechEvent] = []
        self._max_telemetry = 500
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._worker_ready = threading.Event()
        self._state = TTSState.IDLE
        self._current_item: TTSSpeechItem | None = None
        self._current_event: TTSSpeechEvent | None = None
        self._engine_restart_count = 0
        self._sequence = 0
        self._last_text = ""
        self._last_text_at = 0.0
        self._muted = bool(settings.get("mute"))
        self._rate = self._clamp_rate(int(settings.get("voice_rate", 180) or 180))
        self._volume = self._clamp_volume(float(settings.get("voice_volume", 1.0) or 1.0))
        self._voice_id = str(settings.get("voice_id") or "").strip()
        self._engine: BaseTTSEngine | None = None
        self._ensure_worker()

    def initialize(self) -> None:
        self._ensure_worker()
        self._worker_ready.wait(timeout=5.0)

    def speak(
        self,
        text: str | None,
        *,
        priority: TTSPriority = TTSPriority.NORMAL,
        response_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        cleaned = " ".join(str(text or "").split()).strip()
        if not cleaned:
            return False
        if not bool(settings.get("voice_enabled", True)):
            return False
        if self.is_muted():
            return False
        if bool(settings.get("do_not_disturb", False)):
            return False

        now = time.monotonic()
        if (
            self._deduplicate_window_seconds > 0
            and cleaned.casefold() == self._last_text.casefold()
            and (now - self._last_text_at) < self._deduplicate_window_seconds
        ):
            logger.info("TTS duplicate suppressed", text=cleaned)
            return False

        with self._state_lock:
            self._sequence += 1
            item = TTSSpeechItem(
                priority=priority,
                sequence=self._sequence,
                text=cleaned,
                response_id=response_id,
                metadata=dict(metadata or {}),
            )
            self._queue.put(item)
            self._set_state(TTSState.QUEUED)
        self._last_text = cleaned
        self._last_text_at = now
        self._emit_state("queued", cleaned)
        return True

    def cancel_current(self) -> None:
        self._stop_current_speech(cancelled=True)

    def flush_queue(self) -> int:
        removed = 0
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            else:
                removed += 1
                self._queue.task_done()
        if removed:
            logger.info("TTS queue flushed", removed=removed)
        return removed

    def shutdown(self, timeout_seconds: float = 5.0) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        self.flush_queue()
        self._stop_current_speech(cancelled=True)
        self._worker_ready.set()
        thread = self._worker_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.5, float(timeout_seconds)))
        self._worker_thread = None
        state.tts_ready = False
        state.is_speaking = False

    def stop(self) -> None:
        self.cancel_current()

    def is_speaking(self) -> bool:
        with self._state_lock:
            return self._state == TTSState.SPEAKING

    def get_state(self) -> TTSState:
        with self._state_lock:
            return self._state

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        settings.set("mute", self._muted)
        if self._muted:
            self.cancel_current()

    def is_muted(self) -> bool:
        return bool(self._muted or settings.get("mute", False))

    def set_rate(self, rate: int) -> None:
        self._rate = self._clamp_rate(rate)
        try:
            settings.set("voice_rate", max(80, self._rate))
        except Exception:
            logger.debug("Persisting voice_rate failed", exc_info=True)
        if self._engine is not None:
            try:
                self._engine.set_rate(self._rate)
            except Exception:
                logger.debug("TTS rate update failed", exc_info=True)

    def get_rate(self) -> int:
        return self._rate

    def set_volume(self, volume: float) -> None:
        self._volume = self._clamp_volume(volume)
        settings.set("voice_volume", self._volume)
        if self._engine is not None:
            try:
                self._engine.set_volume(self._volume)
            except Exception:
                logger.debug("TTS volume update failed", exc_info=True)

    def get_volume(self) -> float:
        return self._volume

    def set_voice(self, voice_id: str) -> None:
        self._voice_id = str(voice_id or "").strip()
        settings.set("voice_id", self._voice_id)
        if self._engine is not None and self._voice_id:
            try:
                self._engine.set_voice(self._voice_id)
            except Exception:
                logger.debug("TTS voice update failed", exc_info=True)

    def list_voices(self) -> list[Any]:
        self.initialize()
        engine = self._engine
        if engine is None:
            return []
        try:
            return engine.get_voices()
        except Exception:
            logger.debug("TTS list_voices failed", exc_info=True)
            return []

    def apply_settings(self) -> None:
        self.set_muted(bool(settings.get("mute", False)))
        self.set_rate(int(settings.get("voice_rate", 180) or 180))
        self.set_volume(float(settings.get("voice_volume", 1.0) or 1.0))
        voice_id = str(settings.get("voice_id") or "").strip()
        if voice_id:
            self.set_voice(voice_id)

    def subscribe_state_changes(self, callback: Callable[[str, str], None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def unsubscribe_state_changes(self, callback: Callable[[str, str], None]) -> None:
        self._listeners = [item for item in self._listeners if item is not callback]

    def get_telemetry(self, limit: int = 100) -> list[TTSSpeechEvent]:
        with self._history_lock:
            return list(self._telemetry[-max(0, int(limit)) :])

    def _ensure_worker(self) -> None:
        thread = self._worker_thread
        if thread is not None and thread.is_alive():
            return
        with self._state_lock:
            thread = self._worker_thread
            if thread is not None and thread.is_alive():
                return
            self._stop_event.clear()
            self._worker_ready.clear()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name="TTS-Service-Worker",
            )
            self._worker_thread.start()

    def _worker_loop(self) -> None:
        _coinit_sta()
        try:
            self._initialize_engine()
        finally:
            self._worker_ready.set()

        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                with self._state_lock:
                    if self._current_item is None:
                        self._set_state(TTSState.IDLE)
                continue

            try:
                self._speak_item(item)
            finally:
                self._queue.task_done()

        engine = self._engine
        self._engine = None
        if engine is not None:
            try:
                engine.shutdown()
            except Exception:
                logger.debug("TTS engine shutdown failed", exc_info=True)

    def _initialize_engine(self) -> None:
        if self._engine is None:
            self._engine = self._engine_factory()
        self._engine.initialize()
        self._engine.set_rate(self._rate)
        self._engine.set_volume(self._volume)
        if self._voice_id:
            self._engine.set_voice(self._voice_id)
        state.tts_ready = True

    def _speak_item(self, item: TTSSpeechItem) -> None:
        engine = self._engine
        if engine is None:
            self._initialize_engine()
            engine = self._engine
        if engine is None:
            self._record_failure(item, "tts_engine_unavailable")
            return

        event = TTSSpeechEvent(
            response_id=item.response_id,
            text=item.text,
            text_length=len(item.text),
            queued_at=item.created_at,
            engine_restart_count=self._engine_restart_count,
            status="queued",
        )
        with self._state_lock:
            self._current_item = item
            self._current_event = event
            self._set_state(TTSState.SPEAKING)
        event.started_at = datetime.utcnow()
        event.status = "speaking"
        state.is_speaking = True
        state.last_spoken_text = item.text
        self._emit_state("started", item.text)

        started = time.monotonic()
        try:
            engine.speak(item.text)
            event.finished_at = datetime.utcnow()
            event.duration_ms = max(0, int(round((time.monotonic() - started) * 1000.0)))
            event.status = "finished"
            self._emit_state("finished", item.text)
        except Exception as exc:
            logger.warning("TTS speak failed", exc=exc)
            event.finished_at = datetime.utcnow()
            event.duration_ms = max(0, int(round((time.monotonic() - started) * 1000.0)))
            event.status = "failed"
            event.error = str(exc)
            self._emit_state("error", str(exc))
            self._recover_engine()
        finally:
            self._append_telemetry(event)
            with self._state_lock:
                self._current_item = None
                self._current_event = None
                self._set_state(TTSState.IDLE if self._queue.empty() else TTSState.QUEUED)
            state.is_speaking = False

    def _recover_engine(self) -> None:
        with self._state_lock:
            self._set_state(TTSState.RECOVERING)
        self._engine_restart_count += 1
        state.tts_ready = False
        engine = self._engine
        self._engine = None
        if engine is not None:
            try:
                engine.shutdown()
            except Exception:
                logger.debug("TTS engine recovery shutdown failed", exc_info=True)
        try:
            self._initialize_engine()
        except Exception as exc:
            logger.error("TTS engine recovery failed", exc=exc)
            with self._state_lock:
                self._set_state(TTSState.FAILED)
            return
        self._emit_state("recovering", "")

    def _record_failure(self, item: TTSSpeechItem, error: str) -> None:
        event = TTSSpeechEvent(
            response_id=item.response_id,
            text=item.text,
            text_length=len(item.text),
            queued_at=item.created_at,
            finished_at=datetime.utcnow(),
            duration_ms=0,
            engine_restart_count=self._engine_restart_count,
            status="failed",
            error=error,
        )
        self._append_telemetry(event)
        with self._state_lock:
            self._set_state(TTSState.FAILED)
        self._emit_state("error", error)

    def _stop_current_speech(self, *, cancelled: bool) -> None:
        engine = self._engine
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                logger.debug("TTS stop failed", exc_info=True)
        if cancelled:
            with self._state_lock:
                self._set_state(TTSState.CANCELLED)
            if self._current_event is not None:
                self._current_event.finished_at = datetime.utcnow()
                self._current_event.duration_ms = self._current_event.duration_ms or 0
                self._current_event.status = "cancelled"
                self._append_telemetry(self._current_event)
                self._current_event = None
            self._current_item = None
            state.is_speaking = False
            self._emit_state("cancelled", "")

    def _append_telemetry(self, event: TTSSpeechEvent) -> None:
        with self._history_lock:
            self._telemetry.append(event)
            if len(self._telemetry) > self._max_telemetry:
                self._telemetry = self._telemetry[-self._max_telemetry :]

    def _emit_state(self, event_name: str, payload: str) -> None:
        for callback in list(self._listeners):
            try:
                callback(str(event_name), str(payload))
            except Exception:
                logger.debug("TTS state listener failed", exc_info=True)

    def _set_state(self, state_value: TTSState) -> None:
        self._state = state_value

    @staticmethod
    def _clamp_rate(rate: int) -> int:
        return max(50, min(300, int(rate)))

    @staticmethod
    def _clamp_volume(volume: float) -> float:
        return max(0.0, min(1.0, float(volume)))


TextToSpeechService = TTSService


_tts_service: TTSService | None = None


def get_tts_service() -> TTSService:
    global _tts_service
    if _tts_service is None:
        _tts_service = TTSService()
    return _tts_service


def set_tts_service(service: TTSService | None) -> None:
    global _tts_service
    _tts_service = service
