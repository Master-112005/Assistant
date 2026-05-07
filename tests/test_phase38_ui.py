import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core import settings, state
from core.theme_manager import ThemeManager
from ui.floating_orb import FloatingOrb
from ui.sidebar import Sidebar
from ui.styles import build_main_stylesheet, theme_tokens
from ui.window import MainWindow


class IdentityStub:
    def format_title(self) -> str:
        return "Nova Assistant"


class ProcessorStub:
    def __init__(self):
        self.identity_mgr = IdentityStub()

    def process(self, text: str, source: str = "text"):
        return {"success": True, "intent": "test", "response": f"{source}: {text}"}


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
    def register_push_to_talk(self, callback_start, callback_stop):
        self.callback_start = callback_start
        self.callback_stop = callback_stop

    def unregister_all(self):
        return None


class STTStub:
    def transcribe(self, audio_data=None):
        return {"text": ""}


class TTSStub:
    def __init__(self):
        self.muted = False
        self.spoken = []
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


def _get_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def reset_phase38_settings():
    settings.reset_defaults()
    settings.set("context_detection_enabled", False)
    yield
    settings.reset_defaults()


def test_phase38_settings_defaults_exist():
    data = settings.load_settings()

    assert data["theme"] == "dark"
    assert data["use_system_theme"] is False
    assert data["show_floating_orb"] is True
    assert data["orb_always_on_top"] is True
    assert data["animations_enabled"] is True
    assert data["reduced_motion"] is False
    assert data["sidebar_collapsed"] is False


def test_theme_tokens_and_invalid_theme_fallback():
    tokens = theme_tokens("light", "#0f766e")
    assert tokens["theme"] == "light"
    assert tokens["accent"] == "#0f766e"
    assert "QPushButton#SidebarNavButton" in build_main_stylesheet("dark", "#0078d4")

    manager = ThemeManager()
    assert manager.resolve_theme("missing") == "dark"
    settings.set("theme", "light")
    assert manager.load_theme() == "light"
    assert state.theme_loaded == "light"


def test_sidebar_active_and_collapse_state():
    _get_app()
    settings.set("reduced_motion", True)
    sidebar = Sidebar(collapsed=False)

    sidebar.set_active("plugins")
    assert state.sidebar_page == "plugins"
    assert sidebar._buttons["plugins"].property("active") == "true"

    sidebar.toggle_collapsed()
    assert sidebar.is_collapsed() is True
    assert settings.get("sidebar_collapsed") is True

    sidebar.close()


def test_floating_orb_visibility_and_listening_state():
    app = _get_app()
    opened = []
    orb = FloatingOrb(open_callback=lambda: opened.append(True))

    orb.show_orb()
    app.processEvents()
    assert state.orb_visible is True

    orb.set_listening(True)
    assert orb._listening is True

    orb.hide_orb()
    assert state.orb_visible is False
    orb.close()


def test_main_window_sidebar_summary_and_orb_integration():
    app = _get_app()
    window = MainWindow(
        processor=ProcessorStub(),
        listener=ListenerStub(),
        hotkey_mgr=HotkeyStub(),
        stt_engine=STTStub(),
        tts_engine=TTSStub(),
    )
    window.show()
    app.processEvents()

    assert window.sidebar is not None
    assert window.floating_orb is not None

    window.add_message("User", "open chrome")
    window._on_sidebar_page_requested("history")
    app.processEvents()

    assert state.sidebar_page == "history"
    assert window.page_stack.currentWidget() is window.summary_page
    assert window.summary_layout.count() > 1

    window.start_listening()
    assert window.floating_orb._listening is True
    window.stop_listening()
    assert window.floating_orb._listening is False

    window.close()
