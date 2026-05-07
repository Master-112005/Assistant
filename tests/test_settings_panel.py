import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QWidget

from core.settings import SettingsManager
from ui.settings_panel import SettingsPanel


class TTSStub:
    def __init__(self):
        self.voice_id = ""
        self.rate = 0
        self.volume = 0.0
        self.muted = False
        self.spoken = []

    def list_voices(self):
        return [SimpleNamespace(id="voice-1", name="Test Voice")]

    def set_voice(self, voice_id: str):
        self.voice_id = voice_id

    def set_rate(self, rate: int):
        self.rate = rate

    def set_volume(self, volume: float):
        self.volume = volume

    def set_muted(self, muted: bool):
        self.muted = muted

    def stop(self):
        return None

    def apply_settings(self):
        return None

    def speak(self, text: str):
        self.spoken.append(text)


class ParentSpy(QWidget):
    def __init__(self):
        super().__init__()
        self.hotkey_refreshes = 0
        self.speech_apply_keys = []
        self.identity_updates = 0
        self.ui_updates = []

    def refresh_hotkeys(self):
        self.hotkey_refreshes += 1

    def apply_speech_preferences(self, *, changed_key: str | None = None):
        self.speech_apply_keys.append(changed_key)

    def update_identity_title(self):
        self.identity_updates += 1

    def apply_ui_preferences(self, *, changed_key: str | None = None):
        self.ui_updates.append(changed_key)


def _get_app():
    return QApplication.instance() or QApplication([])


def test_settings_panel_opens_loads_and_saves(tmp_path):
    app = _get_app()
    manager = SettingsManager(tmp_path / "settings.json")
    manager.load()
    panel = SettingsPanel(settings_manager=manager, tts_engine=TTSStub())
    panel.show()
    app.processEvents()

    assert panel.assistant_name_input.text() == "Nova"

    panel.assistant_name_input.setText("PanelNova")
    assert panel.apply_change("assistant_name", "PanelNova")

    manager.load()
    assert manager.get("assistant_name") == "PanelNova"

    panel.close()


def test_settings_panel_rejects_conflicting_hotkey(tmp_path):
    app = _get_app()
    manager = SettingsManager(tmp_path / "settings.json")
    manager.load()
    panel = SettingsPanel(settings_manager=manager, tts_engine=TTSStub())
    app.processEvents()

    existing = manager.get("push_to_talk_hotkey")
    assert not panel.apply_change("open_assistant_hotkey", existing)
    assert manager.get("open_assistant_hotkey") != existing

    panel.close()


def test_settings_panel_apply_all_live_replays_hotkeys_and_speech(tmp_path):
    app = _get_app()
    manager = SettingsManager(tmp_path / "settings.json")
    manager.load()
    manager.set("speech_language", "en-US")
    manager.set("push_to_talk_hotkey", "Ctrl+Shift+Space")
    manager.set("push_to_talk_mode", "toggle")
    manager.set("microphone_device", "default")
    manager.set("silence_threshold", 0.015)
    manager.set("start_stop_listening_hotkey", "Ctrl+Alt+Shift+Enter")
    manager.set("open_assistant_hotkey", "Ctrl+Win+Space")

    parent = ParentSpy()
    panel = SettingsPanel(parent=parent, settings_manager=manager, tts_engine=TTSStub())
    app.processEvents()

    panel._apply_all_live()

    assert parent.hotkey_refreshes >= 1
    assert "speech_language" in parent.speech_apply_keys
    assert "microphone_device" in parent.speech_apply_keys
    assert "push_to_talk_mode" in parent.ui_updates or parent.hotkey_refreshes >= 1

    panel.close()
