import numpy as np

from core.audio import ListenerConfig, LiveAudioListener


def _frame(level: float, samples: int) -> np.ndarray:
    return np.full(samples, level, dtype=np.float32)


def test_live_audio_listener_finalizes_on_silence():
    utterances = []
    listener = LiveAudioListener(
        config=ListenerConfig(
            sample_rate=16000,
            frame_duration_ms=20,
            silence_duration_ms=120,
            min_utterance_ms=60,
            speech_start_frames=1,
            rms_gate=0.01,
        ),
        on_utterance_ready=lambda audio, meta: utterances.append((audio, meta)),
    )

    for _ in range(5):
        listener.feed_audio_frame(_frame(0.001, listener.config.frame_samples))
    for _ in range(6):
        listener.feed_audio_frame(_frame(0.08, listener.config.frame_samples))
    for _ in range(7):
        listener.feed_audio_frame(_frame(0.001, listener.config.frame_samples))

    assert len(utterances) == 1
    audio, meta = utterances[0]
    assert audio.size > 0
    assert meta["duration"] > 0.0


def test_live_audio_listener_ignores_silence_only():
    utterances = []
    listener = LiveAudioListener(
        config=ListenerConfig(
            sample_rate=16000,
            frame_duration_ms=20,
            silence_duration_ms=120,
            min_utterance_ms=60,
            speech_start_frames=1,
            rms_gate=0.01,
        ),
        on_utterance_ready=lambda audio, meta: utterances.append((audio, meta)),
    )

    for _ in range(20):
        listener.feed_audio_frame(_frame(0.001, listener.config.frame_samples))

    assert utterances == []


def test_stop_with_finalize_emits_last_utterance():
    utterances = []
    listener = LiveAudioListener(
        config=ListenerConfig(
            sample_rate=16000,
            frame_duration_ms=20,
            silence_duration_ms=500,
            min_utterance_ms=60,
            speech_start_frames=1,
            rms_gate=0.01,
        ),
        on_utterance_ready=lambda audio, meta: utterances.append((audio, meta)),
    )

    listener._active = True
    listener._set_state_locked("hearing_speech")
    listener._current_frames = [_frame(0.08, listener.config.frame_samples) for _ in range(5)]
    listener.stop(finalize_utterance=True)

    assert len(utterances) == 1
