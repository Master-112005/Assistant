"""
Live microphone streaming and utterance detection for voice commands.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import queue
import threading
import time
from typing import Any, Callable

import numpy as np

from core import settings, state
from core.logger import get_logger

try:
    import sounddevice as sd
except Exception:  # pragma: no cover - optional dependency
    sd = None

try:
    import webrtcvad
except Exception:  # pragma: no cover - optional dependency
    webrtcvad = None

logger = get_logger(__name__)


StateCallback = Callable[[str, dict[str, Any]], None]
UtteranceCallback = Callable[[np.ndarray, dict[str, Any]], None]
ErrorCallback = Callable[[str, Exception | None], None]


@dataclass(slots=True)
class ListenerConfig:
    sample_rate: int = 16000
    channels: int = 1
    frame_duration_ms: int = 20
    silence_duration_ms: int = 900
    preroll_ms: int = 200
    min_utterance_ms: int = 250
    max_utterance_seconds: float = 8.0
    vad_aggressiveness: int = 2
    rms_gate: float = 0.01
    speech_start_frames: int = 2
    queue_max_frames: int = 256

    @property
    def frame_samples(self) -> int:
        return max(160, int(self.sample_rate * self.frame_duration_ms / 1000))

    @property
    def max_utterance_frames(self) -> int:
        return max(1, int(self.max_utterance_seconds * 1000 / self.frame_duration_ms))

    @property
    def silence_frames(self) -> int:
        return max(1, int(self.silence_duration_ms / self.frame_duration_ms))

    @property
    def preroll_frames(self) -> int:
        return max(1, int(self.preroll_ms / self.frame_duration_ms))

    @property
    def min_utterance_frames(self) -> int:
        return max(1, int(self.min_utterance_ms / self.frame_duration_ms))


class LiveAudioListener:
    """Continuous microphone listener with speech start/end detection."""

    def __init__(
        self,
        *,
        on_state_change: StateCallback | None = None,
        on_utterance_ready: UtteranceCallback | None = None,
        on_error: ErrorCallback | None = None,
        config: ListenerConfig | None = None,
        sounddevice_module=None,
        device: str | int | None = None,
    ) -> None:
        self.config = config or ListenerConfig(
            sample_rate=int(settings.get("sample_rate") or 16000),
            rms_gate=float(settings.get("silence_threshold") or 0.01),
        )
        self._sd = sounddevice_module if sounddevice_module is not None else sd
        self._device = device if device is not None else settings.get("microphone_device")
        self._on_state_change = on_state_change
        self._on_utterance_ready = on_utterance_ready
        self._on_error = on_error

        self._stream = None
        self._worker: threading.Thread | None = None
        self._queue: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=self.config.queue_max_frames)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()

        self._active = False
        self._paused = False
        self._state = "idle"

        self._vad = self._build_vad()
        self._noise_floor = max(0.001, self.config.rms_gate * 0.35)
        self._speech_run = 0
        self._silence_run = 0
        self._speech_padding_frames = max(1, min(6, self.config.preroll_frames // 2))
        self._preroll: deque[np.ndarray] = deque(maxlen=self.config.preroll_frames)
        self._current_frames: list[np.ndarray] = []
        self._current_start_time = 0.0
        self._last_utterance: np.ndarray = np.zeros(0, dtype=np.float32)

    def start(self) -> None:
        worker: threading.Thread | None = None
        with self._lock:
            if self._active:
                logger.debug("Live listener start ignored: already active")
                return
            if self._sd is None:
                raise RuntimeError("sounddevice is not installed.")
            self._reset_detection_locked()
            self._stop_event.clear()
            self._paused = False
            self._queue = queue.Queue(maxsize=self.config.queue_max_frames)
            self._worker = threading.Thread(target=self._worker_loop, name="LiveAudioListener", daemon=True)
            worker = self._worker
            self._worker.start()
            try:
                self._stream = self._sd.InputStream(
                    samplerate=self.config.sample_rate,
                    channels=self.config.channels,
                    dtype="float32",
                    blocksize=self.config.frame_samples,
                    device=self._resolved_device(),
                    callback=self._audio_callback,
                )
                self._stream.start()
                self._active = True
                state.is_listening = True
                self._set_state_locked("listening")
            except Exception:
                self._stop_event.set()
                self._queue_put_sentinel_locked()
                self._close_stream_locked()
                self._worker = None
                self._reset_detection_locked()
                state.is_listening = False
                raise
        if worker is not None and not self._active and worker.is_alive():
            worker.join(timeout=1.0)
        logger.info("Live listener started")

    def stop(self, *, finalize_utterance: bool = False) -> None:
        worker: threading.Thread | None = None
        pending_audio: np.ndarray | None = None
        pending_meta: dict[str, Any] | None = None
        with self._lock:
            if not self._active and self._stream is None:
                self._paused = False
                self._set_state_locked("idle")
                state.is_listening = False
                return
            if finalize_utterance:
                pending_audio, pending_meta = self._build_utterance_locked(force=True)
            self._active = False
            self._paused = False
            self._stop_event.set()
            self._close_stream_locked()
            worker = self._worker
            self._worker = None
            self._queue_put_sentinel_locked()
            self._reset_detection_locked()
            self._set_state_locked("idle")
            state.is_listening = False
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)
        if pending_audio is not None and pending_meta is not None:
            self._emit_utterance(pending_audio, pending_meta)
        logger.info("Live listener stopped")

    def pause(self, *, reason: str = "") -> None:
        with self._lock:
            if not self._active or self._paused:
                return
            self._paused = True
            self._flush_queue_locked()
            self._reset_detection_locked()
            state.is_listening = False
        logger.info("Listener paused%s", f" ({reason})" if reason else "")

    def resume(self) -> None:
        with self._lock:
            if not self._active:
                return
            self._paused = False
            self._flush_queue_locked()
            self._set_state_locked("listening")
            state.is_listening = True
        logger.info("Listener resumed")

    def cancel_current_utterance(self) -> None:
        with self._lock:
            self._reset_detection_locked()
            if self._active and not self._paused:
                self._set_state_locked("listening")
        logger.info("Current utterance cleared")

    def is_active(self) -> bool:
        with self._lock:
            return bool(self._active)

    def is_paused(self) -> bool:
        with self._lock:
            return bool(self._paused)

    def is_capturing_speech(self) -> bool:
        with self._lock:
            return self._state == "hearing_speech"

    def current_state(self) -> str:
        with self._lock:
            return self._state

    def last_utterance(self) -> np.ndarray:
        with self._lock:
            return np.array(self._last_utterance, copy=True)

    def feed_audio_frame(self, frame: np.ndarray) -> None:
        """Inject a single frame. Used by tests and alternate audio front ends."""
        normalized = np.asarray(frame, dtype=np.float32).reshape(-1)
        self._process_frame(normalized)

    @staticmethod
    def available_input_devices() -> list[dict[str, Any]]:
        if sd is None:
            return []
        devices = []
        for index, device in enumerate(sd.query_devices()):
            if int(device.get("max_input_channels", 0) or 0) <= 0:
                continue
            devices.append(
                {
                    "index": index,
                    "name": str(device.get("name") or f"Input {index}"),
                    "default_samplerate": int(device.get("default_samplerate") or 0),
                }
            )
        return devices

    def apply_runtime_settings(
        self,
        *,
        sample_rate: int | None = None,
        rms_gate: float | None = None,
        device: str | int | None = None,
        restart_if_active: bool = True,
    ) -> None:
        """Apply persisted audio settings to the live listener."""
        with self._lock:
            was_active = bool(self._active)
            was_paused = bool(self._paused)

        if was_active and restart_if_active:
            self.stop(finalize_utterance=False)

        with self._lock:
            if sample_rate is not None:
                self.config.sample_rate = max(8000, int(sample_rate))
            if rms_gate is not None:
                self.config.rms_gate = max(0.0001, float(rms_gate))
            if device is not None:
                self._device = device
            self._vad = self._build_vad()
            self._noise_floor = max(0.001, self.config.rms_gate * 0.35)
            self._queue = queue.Queue(maxsize=self.config.queue_max_frames)
            self._reset_detection_locked()
            if not self._active:
                self._set_state_locked("idle")

        if was_active and restart_if_active:
            self.start()
            if was_paused:
                self.pause(reason="restoring_listener_pause")

        logger.info(
            "Live listener settings applied",
            sample_rate=self.config.sample_rate,
            rms_gate=self.config.rms_gate,
            device=self._device,
            restarted=bool(was_active and restart_if_active),
        )

    def _audio_callback(self, indata, frames, callback_time, status) -> None:
        del frames, callback_time
        if status:
            logger.warning("Live listener stream status: %s", status)
        with self._lock:
            if not self._active or self._paused:
                return
        frame = np.asarray(indata, dtype=np.float32).reshape(-1).copy()
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            with self._lock:
                self._flush_one_locked()
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                logger.warning("Live listener queue overflow; dropping frame")

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                frame = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if frame is None:
                break
            try:
                self._process_frame(frame)
            except Exception as exc:
                logger.exception("Live listener worker failed", exc=exc)
                self._emit_error("live_listener_failed", exc)
                break

    def _process_frame(self, frame: np.ndarray) -> None:
        with self._lock:
            if self._paused:
                return
        if frame.size == 0:
            return
        rms = float(np.sqrt(np.mean(np.square(frame), dtype=np.float64)))
        speech = self._detect_speech(frame, rms)
        with self._lock:
            if self._paused:
                return
            self._preroll.append(frame.copy())
            if self._state != "hearing_speech":
                if speech:
                    self._speech_run += 1
                else:
                    self._speech_run = 0
                    self._update_noise_floor_locked(rms)
                if self._speech_run >= self.config.speech_start_frames:
                    self._current_start_time = time.monotonic()
                    self._current_frames = list(self._preroll)
                    self._silence_run = 0
                    self._speech_run = 0
                    self._set_state_locked("hearing_speech")
                    logger.info("Speech detected")
                return

            self._current_frames.append(frame.copy())
            if speech:
                self._silence_run = 0
            else:
                self._silence_run += 1
            utterance_exceeded = len(self._current_frames) >= self.config.max_utterance_frames
            if self._silence_run < self.config.silence_frames and not utterance_exceeded:
                return
            audio, meta = self._build_utterance_locked(force=utterance_exceeded)
            if audio is None or meta is None:
                self._set_state_locked("listening")
                return
            self._paused = True
            state.is_listening = False
        logger.info("Speech ended duration=%.2fs", float(meta.get("duration", 0.0)))
        self._emit_utterance(audio, meta)

    def _build_utterance_locked(self, *, force: bool) -> tuple[np.ndarray | None, dict[str, Any] | None]:
        if not self._current_frames:
            self._reset_detection_locked()
            return None, None
        frame_count = len(self._current_frames)
        if not force:
            trim_frames = max(0, self._silence_run - self._speech_padding_frames)
            if trim_frames > 0:
                frame_count = max(0, frame_count - trim_frames)
        if frame_count <= 0:
            self._reset_detection_locked()
            return None, None
        if frame_count < self.config.min_utterance_frames:
            logger.info("Ignoring short utterance (%d frames)", frame_count)
            self._reset_detection_locked()
            return None, None
        frames = self._current_frames[:frame_count]
        audio = np.concatenate(frames, axis=0).astype(np.float32, copy=False).reshape(-1)
        audio = np.clip(audio, -1.0, 1.0)
        self._last_utterance = np.array(audio, copy=True)
        duration = float(audio.size / self.config.sample_rate)
        meta = {
            "sample_rate": self.config.sample_rate,
            "duration": duration,
            "force_finalized": bool(force),
            "speech_end_time": time.time(),
        }
        state.last_audio_path = ""
        self._reset_detection_locked()
        return audio, meta

    def _detect_speech(self, frame: np.ndarray, rms: float) -> bool:
        threshold = max(self.config.rms_gate * 1.6, self._noise_floor * 2.8, 0.006)
        rms_gate = rms >= threshold
        if self._vad is None:
            return rms_gate
        pcm = self._to_pcm16_bytes(frame)
        try:
            vad_gate = self._vad.is_speech(pcm, self.config.sample_rate)
        except Exception:
            vad_gate = False
        return bool(vad_gate and rms >= max(self._noise_floor * 1.1, self.config.rms_gate * 0.65)) or rms_gate

    def _build_vad(self):
        if webrtcvad is None:
            return None
        try:
            vad = webrtcvad.Vad()
            vad.set_mode(max(0, min(3, int(self.config.vad_aggressiveness))))
            return vad
        except Exception as exc:
            logger.warning("webrtcvad unavailable; falling back to RMS VAD: %s", exc)
            return None

    @staticmethod
    def _to_pcm16_bytes(frame: np.ndarray) -> bytes:
        pcm = np.clip(frame, -1.0, 1.0)
        return (pcm * 32767.0).astype(np.int16).tobytes()

    def _resolved_device(self) -> str | int | None:
        device = self._device
        if device in {None, "", "default"}:
            return None
        text = str(device).strip()
        if text.isdigit():
            return int(text)
        return text

    def _set_state_locked(self, name: str, **payload: Any) -> None:
        if self._state == name and not payload:
            return
        self._state = name
        state.voice_state = name
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(name, dict(payload))
        except Exception as exc:
            logger.debug("Live listener state callback failed: %s", exc)

    def _emit_utterance(self, audio: np.ndarray, metadata: dict[str, Any]) -> None:
        if self._on_utterance_ready is None:
            return
        try:
            self._on_utterance_ready(audio, dict(metadata))
        except Exception as exc:
            logger.exception("Live listener utterance callback failed", exc=exc)
            self._emit_error("utterance_callback_failed", exc)

    def _emit_error(self, message: str, exc: Exception | None) -> None:
        if self._on_error is None:
            return
        try:
            self._on_error(message, exc)
        except Exception:
            logger.debug("Live listener error callback failed", exc_info=True)

    def _close_stream_locked(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            logger.debug("Live listener stream stop failed", exc_info=True)
        try:
            stream.close()
        except Exception:
            logger.debug("Live listener stream close failed", exc_info=True)

    def _queue_put_sentinel_locked(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            self._flush_queue_locked()
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass

    def _flush_queue_locked(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _flush_one_locked(self) -> None:
        try:
            self._queue.get_nowait()
        except queue.Empty:
            return

    def _reset_detection_locked(self) -> None:
        self._speech_run = 0
        self._silence_run = 0
        self._current_start_time = 0.0
        self._current_frames = []
        self._preroll.clear()

    def _update_noise_floor_locked(self, rms: float) -> None:
        bounded = max(0.0005, min(rms, 0.05))
        self._noise_floor = (self._noise_floor * 0.97) + (bounded * 0.03)


# Backwards-compatible alias for older callers still importing the old name.
AudioRecorder = LiveAudioListener
