import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core import settings
from core.context import context_manager as _ctx_mgr
from core.notifications import NotificationManager, set_global_notification_manager
from core.runtime_tasks import drain_runtime_threads
from ui.window import MainWindow


class IdentityStub:
    def format_title(self) -> str:
        return "Nova Assistant"


class SlowProcessor:
    def __init__(self):
        self.identity_mgr = IdentityStub()

    def process(self, text: str, source: str = "text"):
        time.sleep(0.25)
        return {"success": True, "intent": "search", "response": f"Processed {source}: {text}"}

    def handle_external_error(self, error, *, command_context=None, source: str = "ui"):
        message = ""
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("error") or "Command failed.")
        else:
            message = str(error or "Command failed.")
        return {"success": False, "intent": "action_timeout", "response": message, "error": "action_timeout"}


class HungProcessor(SlowProcessor):
    def process(self, text: str, source: str = "text"):
        time.sleep(2.0)
        return {"success": True, "intent": "search", "response": f"Processed {source}: {text}"}


class ExplodingProcessor(SlowProcessor):
    def process(self, text: str, source: str = "text"):
        raise RuntimeError("backend boom")

    def handle_external_error(self, error, *, command_context=None, source: str = "ui"):
        return {
            "success": False,
            "intent": "runtime_error",
            "response": "I couldn't complete that command because backend boom.",
            "error": "backend_error",
            "category": "action",
        }


class ListenerStub:
    def __init__(self):
        self.active = False
        self.paused = False

    def start(self):
        self.active = True

    def stop(self, finalize_utterance: bool = False):
        self.active = False
        self.paused = False

    def pause(self, *, reason: str = ""):
        self.paused = True

    def resume(self):
        if self.active:
            self.paused = False

    def cancel_current_utterance(self):
        return None

    def is_active(self):
        return self.active

    def is_paused(self):
        return self.paused

    def is_capturing_speech(self):
        return False


class HotkeyStub:
    hotkey_name = "ctrl+space"
    mode = "hold"

    def register_push_to_talk(self, callback_start, callback_stop):
        self.callback_start = callback_start
        self.callback_stop = callback_stop

    def unregister_all(self):
        return None


class STTStub:
    def transcribe(self, audio_data=None):
        return {"text": ""}


class RuntimeSettingsSTTStub(STTStub):
    def __init__(self):
        self.runtime_updates = []

    def apply_runtime_settings(self, **kwargs):
        self.runtime_updates.append(dict(kwargs))


class SpeechSTTStub:
    def transcribe(self, audio_data=None):
        return {"text": "open chrome"}


class TTSStub:
    def __init__(self):
        self.spoken = []
        self.muted = False
        self.speaking = False

    def speak(self, text: str):
        self.spoken.append(text)
        return False

    def stop(self):
        self.speaking = False
        return None

    def set_muted(self, value: bool):
        self.muted = value

    def is_speaking(self):
        return self.speaking


class RuntimeSettingsListenerStub(ListenerStub):
    def __init__(self):
        super().__init__()
        self.runtime_updates = []

    def apply_runtime_settings(self, **kwargs):
        self.runtime_updates.append(dict(kwargs))


def _get_app():
    return QApplication.instance() or QApplication([])


def _wait_for(predicate, timeout=2.0):
    app = _get_app()
    start = time.perf_counter()
    while time.perf_counter() - start < timeout:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


@pytest.fixture(autouse=True)
def reset_notification_globals():
    set_global_notification_manager(None)
    settings.set("smart_file_search_enabled", False)
    settings.set("use_file_index", False)
    settings.set("index_update_on_startup", False)
    yield
    app = _get_app()
    for widget in list(app.topLevelWidgets()):
        try:
            widget.close()
        except Exception:
            pass
    app.processEvents()
    drain_runtime_threads(timeout_seconds=2.0)
    set_global_notification_manager(None)


def test_llm_calls_do_not_freeze_ui():
    app = _get_app()
    tts = TTSStub()
    window = MainWindow(
        processor=SlowProcessor(),
        listener=ListenerStub(),
        hotkey_mgr=HotkeyStub(),
        stt_engine=STTStub(),
        tts_engine=tts,
    )
    window.show()
    app.processEvents()

    window.text_input.setText("search for weather")
    start = time.perf_counter()
    window.send_message()
    elapsed = time.perf_counter() - start

    assert elapsed < 0.1
    assert _wait_for(lambda: window.command_worker is not None and window.command_worker.isRunning(), timeout=0.2)
    assert _wait_for(lambda: window.command_worker is None or not window.command_worker.isRunning())
    assert _wait_for(lambda: bool(tts.spoken))

    window.close()


def test_in_app_notification_panel_and_toast_render():
    app = _get_app()
    manager = NotificationManager(tts_engine=TTSStub(), desktop_backends=[])
    window = MainWindow(
        processor=SlowProcessor(),
        listener=ListenerStub(),
        hotkey_mgr=HotkeyStub(),
        stt_engine=STTStub(),
        tts_engine=TTSStub(),
        notification_manager=manager,
    )
    window.show()
    app.processEvents()

    manager.notify("Reminder", "Call mom now", level="reminder", channel="in_app", source="test")

    assert _wait_for(lambda: window.notification_center.item_count() == 1)
    assert window.toast_overlay.visible_count() == 1

    window.close()
    manager.stop()


def test_live_listener_processes_utterance_without_manual_stop():
    app = _get_app()
    listener = ListenerStub()
    tts = TTSStub()
    window = MainWindow(
        processor=SlowProcessor(),
        listener=listener,
        hotkey_mgr=HotkeyStub(),
        stt_engine=SpeechSTTStub(),
        tts_engine=tts,
    )
    window.show()
    app.processEvents()

    window.start_listening()
    window._on_utterance_ready(b"\x00" * 320, {"duration": 0.5})

    assert _wait_for(lambda: bool(tts.spoken))
    assert listener.is_active() is True
    assert listener.is_paused() is False
    assert "Processed speech: open chrome" in tts.spoken[-1]

    window.close()


def test_cancelled_command_resets_ui_and_allows_next_command():
    app = _get_app()
    tts = TTSStub()
    window = MainWindow(
        processor=SlowProcessor(),
        listener=ListenerStub(),
        hotkey_mgr=HotkeyStub(),
        stt_engine=STTStub(),
        tts_engine=tts,
    )
    window.show()
    app.processEvents()

    window.text_input.setText("open chrome")
    window.send_message()
    assert _wait_for(lambda: window.command_worker is not None and window.command_worker.isRunning(), timeout=0.2)

    window.cancel_execution()

    assert _wait_for(lambda: window.text_input.isEnabled())
    assert _wait_for(lambda: window.status_indicator.label.text() in {"Cancelled", "Ready"}, timeout=1.5)

    window.text_input.setText("open youtube")
    window.send_message()

    assert _wait_for(lambda: any("Processed text: open youtube" in spoken for spoken in tts.spoken), timeout=2.0)

    window.close()


def test_command_timeout_recovers_ui_state():
    settings.reset_defaults()
    settings.set("command_timeout_seconds", 1)

    app = _get_app()
    window = MainWindow(
        processor=HungProcessor(),
        listener=ListenerStub(),
        hotkey_mgr=HotkeyStub(),
        stt_engine=STTStub(),
        tts_engine=TTSStub(),
    )
    window.show()
    app.processEvents()

    window.text_input.setText("open chrome")
    window.send_message()

    assert _wait_for(lambda: window.status_indicator.label.text() in {"Error - Ready again", "Ready"}, timeout=2.5)
    assert _wait_for(lambda: window.text_input.isEnabled(), timeout=2.5)
    assert any("timed out" in text.lower() for _, text in window._message_history)

    window.close()
    settings.reset_defaults()


def test_command_worker_exception_returns_structured_result_and_recovers_ui():
    app = _get_app()
    window = MainWindow(
        processor=ExplodingProcessor(),
        listener=ListenerStub(),
        hotkey_mgr=HotkeyStub(),
        stt_engine=STTStub(),
        tts_engine=TTSStub(),
    )
    window.show()
    app.processEvents()

    window.text_input.setText("open chrome")
    window.send_message()

    assert _wait_for(
        lambda: any("backend boom" in text.lower() for _, text in window._message_history),
        timeout=2.5,
    )
    assert not any("Command processing failed:" in text for _, text in window._message_history)
    assert _wait_for(lambda: window.status_indicator.label.text() in {"Error - Ready again", "Ready"}, timeout=2.5)
    assert window.text_input.isEnabled() is True

    window.close()


def test_main_window_applies_current_speech_settings_before_listening():
    settings.reset_defaults()
    settings.set("speech_language", "en-US")
    settings.set("sample_rate", 22050)
    settings.set("silence_threshold", 0.02)
    settings.set("microphone_device", "Mic 2")

    app = _get_app()
    listener = RuntimeSettingsListenerStub()
    stt = RuntimeSettingsSTTStub()
    window = MainWindow(
        processor=SlowProcessor(),
        listener=listener,
        hotkey_mgr=HotkeyStub(),
        stt_engine=stt,
        tts_engine=TTSStub(),
    )
    window.show()
    app.processEvents()

    window.start_listening()

    assert stt.runtime_updates[-1]["language"] == "en-US"
    assert stt.runtime_updates[-1]["sample_rate"] == 22050
    assert stt.runtime_updates[-1]["silence_threshold"] == 0.02
    assert listener.runtime_updates[-1]["sample_rate"] == 22050
    assert listener.runtime_updates[-1]["rms_gate"] == 0.02
    assert listener.runtime_updates[-1]["device"] == "Mic 2"

    window.close()
    settings.reset_defaults()


def test_repeated_main_window_lifecycle_stops_context_watcher_between_runs():
    settings.reset_defaults()
    settings.set("context_detection_enabled", True)
    settings.set("show_current_app", True)

    app = _get_app()
    for _ in range(4):
        window = MainWindow(
            processor=SlowProcessor(),
            listener=ListenerStub(),
            hotkey_mgr=HotkeyStub(),
            stt_engine=STTStub(),
            tts_engine=TTSStub(),
        )
        window.show()
        app.processEvents()

        assert _wait_for(lambda: _ctx_mgr.is_watching, timeout=1.0)

        window.close()
        app.processEvents()

        assert _wait_for(lambda: not _ctx_mgr.is_watching, timeout=2.0)

    settings.reset_defaults()
