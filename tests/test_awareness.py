from __future__ import annotations

from PIL import Image
import pytest

from core import settings, state
from core.awareness import ScreenAwarenessEngine
from core.desktop_state import DesktopSnapshot, DesktopStateCollector
from core.automation import WindowTarget
from core.window_context import WindowInfo

# OCR removed - use mock classes
class OCRLine:
    def __init__(self, text="", confidence=0.9, bbox=(0,0,0,0), words=None):
        self.text = text
        self.confidence = confidence
        self.bbox = bbox
        self.words = words or []

class OCRResult:
    def __init__(self, lines=None, engine="mock"):
        self.lines = lines or []
        self.engine = engine


class FakeAutomation:
    def __init__(self) -> None:
        self.foreground_hwnd = 101
        self.windows = [
            WindowTarget(
                hwnd=101,
                title="IPL Live Score - Google Chrome",
                process_id=1,
                process_name="chrome.exe",
                rect=(0, 0, 1400, 900),
                is_visible=True,
                is_minimized=False,
            ),
            WindowTarget(
                hwnd=202,
                title="WhatsApp",
                process_id=2,
                process_name="whatsapp.exe",
                rect=(100, 100, 900, 800),
                is_visible=True,
                is_minimized=False,
            ),
        ]

    def list_windows(self, *, process_names=None, title_substrings=None):
        _ = process_names
        _ = title_substrings
        return list(self.windows)

    def get_foreground_window(self):
        return self.foreground_hwnd

    def get_window(self, hwnd: int):
        for window in self.windows:
            if window.hwnd == hwnd:
                return window
        return None


class FakeDetector:
    def get_window_info(self, hwnd: int):
        if hwnd == 101:
            return WindowInfo(
                hwnd=101,
                title="IPL Live Score - Google Chrome",
                process_id=1,
                process_name="chrome.exe",
                rect=(0, 0, 1400, 900),
                is_visible=True,
                is_minimized=False,
                app_id="chrome",
                app_type="browser",
            )
        if hwnd == 202:
            return WindowInfo(
                hwnd=202,
                title="WhatsApp",
                process_id=2,
                process_name="whatsapp.exe",
                rect=(100, 100, 900, 800),
                is_visible=True,
                is_minimized=False,
                app_id="whatsapp",
                app_type="communication",
            )
        return WindowInfo()

    def get_active_context(self):
        return self.get_window_info(101)


class FakeOCR:
    def read_active_window(self):
        return OCRResult(
            full_text="IPL Live Score CSK won by 5 wickets",
            words=[],
            lines=[OCRLine(text="IPL Live Score", confidence=0.97, bbox=(10, 10, 200, 40), words=[])],
            engine="fakeocr",
            processing_time=0.12,
            capture_mode="active_window",
        )


class FakeCollector:
    def __init__(self, snapshot: DesktopSnapshot):
        self.snapshot = snapshot
        self.calls = 0

    def capture_desktop_snapshot(self, *, include_ocr: bool | None = None):
        _ = include_ocr
        self.calls += 1
        return self.snapshot


class FakeChromeSkill:
    def describe_window_title(self, title: str, *, ocr_text: str = ""):
        _ = title
        _ = ocr_text
        return {"summary": "Chrome has IPL results open.", "confidence": 0.90, "details": {"source": "window_title"}}


class FakeYouTubeSkill:
    def is_playing(self):
        return None

    def describe_window_title(self, title: str, *, is_playing: bool | None = None):
        _ = title
        _ = is_playing
        return {"summary": "YouTube is showing music search results.", "confidence": 0.86, "details": {"source": "window_title"}}


class FakeWhatsAppSkill:
    def __init__(self, unread_count: int = 3):
        self.unread_count = unread_count

    def describe_visible_state(self):
        if self.unread_count <= 0:
            return {"summary": "WhatsApp is open.", "confidence": 0.62, "details": {"unread_chats": []}}
        return {
            "summary": f"WhatsApp has {self.unread_count} unread chats.",
            "confidence": 0.93,
            "details": {"unread_chats": [{"display_name": "A"}, {"display_name": "B"}]},
        }


class FakeMusicSkill:
    def __init__(self, summary: str = "Spotify is playing Believer."):
        self.summary = summary

    def describe_current_playback(self):
        if not self.summary:
            return {}
        return {"summary": self.summary, "confidence": 0.96, "details": {"provider": "spotify"}}


class FakeExplorerSkill:
    def __init__(self, summary: str = "File Explorer is open in Downloads."):
        self.summary = summary

    def describe_window(self, *, hwnd: int | None = None, title: str = ""):
        _ = hwnd
        _ = title
        return {"summary": self.summary, "confidence": 0.90, "details": {"folder_name": "Downloads"}}


@pytest.fixture(autouse=True)
def reset_awareness_settings():
    settings.reset_defaults()
    settings.set("ignore_background_windows", True)
    state.last_awareness_report = {}
    state.last_desktop_snapshot = {}
    state.awareness_ready = False
    state.last_visible_apps = []
    yield
    settings.reset_defaults()


def test_desktop_state_collector_detects_active_chrome_and_visible_windows():
    collector = DesktopStateCollector(automation=FakeAutomation(), detector=FakeDetector(), ocr_engine=FakeOCR())

    snapshot = collector.capture_desktop_snapshot(include_ocr=True)

    assert snapshot.active_window is not None
    assert snapshot.active_window.app_id == "chrome"
    assert len(snapshot.visible_windows) == 2
    assert snapshot.contexts == ["chrome", "whatsapp"]
    assert snapshot.ocr_text == "IPL Live Score CSK won by 5 wickets"


def test_awareness_engine_summarizes_multiple_real_signal_items():
    snapshot = DesktopSnapshot(
        timestamp=1.0,
        active_window=type(
            "Snap",
            (),
            {
                "app_id": "chrome",
                "process_name": "chrome.exe",
                "title": "IPL Live Score - Google Chrome",
                "bounds": (0, 0, 100, 100),
                "hwnd": 1,
                "is_foreground": True,
            },
        )(),
        visible_windows=[
            type(
                "Snap",
                (),
                {
                    "app_id": "chrome",
                    "process_name": "chrome.exe",
                    "title": "IPL Live Score - Google Chrome",
                    "bounds": (0, 0, 100, 100),
                    "hwnd": 1,
                    "is_foreground": True,
                },
            )(),
            type(
                "Snap",
                (),
                {
                    "app_id": "whatsapp",
                    "process_name": "whatsapp.exe",
                    "title": "WhatsApp",
                    "bounds": (0, 0, 100, 100),
                    "hwnd": 2,
                    "is_foreground": False,
                },
            )(),
            type(
                "Snap",
                (),
                {
                    "app_id": "spotify",
                    "process_name": "spotify.exe",
                    "title": "Spotify Premium",
                    "bounds": (0, 0, 100, 100),
                    "hwnd": 3,
                    "is_foreground": False,
                },
            )(),
        ],
        contexts=["chrome", "whatsapp", "spotify"],
        ocr_text="IPL Live Score",
        ocr_result=None,
    )
    engine = ScreenAwarenessEngine(
        collector=FakeCollector(snapshot),
        chrome_skill=FakeChromeSkill(),
        youtube_skill=FakeYouTubeSkill(),
        whatsapp_skill=FakeWhatsAppSkill(unread_count=3),
        music_skill=FakeMusicSkill(),
        explorer_skill=FakeExplorerSkill(),
    )

    report = engine.analyze()

    assert "Chrome has IPL results open." in report.final_summary
    assert "WhatsApp has 3 unread chats." in report.final_summary
    assert "Spotify is playing Believer." in report.final_summary
    assert state.awareness_ready is True
    assert state.last_visible_apps == ["chrome", "whatsapp", "spotify"]


def test_awareness_engine_summarizes_explorer_folder():
    snapshot = DesktopSnapshot(
        timestamp=1.0,
        active_window=type(
            "Snap",
            (),
            {
                "app_id": "explorer",
                "process_name": "explorer.exe",
                "title": "Downloads",
                "bounds": (0, 0, 100, 100),
                "hwnd": 9,
                "is_foreground": True,
            },
        )(),
        visible_windows=[],
        contexts=["explorer"],
        ocr_text="",
        ocr_result=None,
    )
    engine = ScreenAwarenessEngine(
        collector=FakeCollector(snapshot),
        chrome_skill=FakeChromeSkill(),
        youtube_skill=FakeYouTubeSkill(),
        whatsapp_skill=FakeWhatsAppSkill(unread_count=0),
        music_skill=FakeMusicSkill(summary=""),
        explorer_skill=FakeExplorerSkill(),
    )

    report = engine.analyze()

    assert report.final_summary == "File Explorer is open in Downloads."


def test_awareness_engine_handles_empty_desktop_honestly():
    snapshot = DesktopSnapshot(timestamp=1.0, active_window=None, visible_windows=[], contexts=[], ocr_text="", ocr_result=None)
    engine = ScreenAwarenessEngine(
        collector=FakeCollector(snapshot),
        chrome_skill=FakeChromeSkill(),
        youtube_skill=FakeYouTubeSkill(),
        whatsapp_skill=FakeWhatsAppSkill(unread_count=0),
        music_skill=FakeMusicSkill(summary=""),
        explorer_skill=FakeExplorerSkill(summary=""),
    )

    report = engine.analyze()

    assert report.final_summary == "I couldn't detect any visible windows on the desktop."


def test_awareness_engine_repeated_calls_are_stable():
    snapshot = DesktopSnapshot(
        timestamp=1.0,
        active_window=type(
            "Snap",
            (),
            {
                "app_id": "chrome",
                "process_name": "chrome.exe",
                "title": "IPL Live Score - Google Chrome",
                "bounds": (0, 0, 100, 100),
                "hwnd": 1,
                "is_foreground": True,
            },
        )(),
        visible_windows=[],
        contexts=["chrome"],
        ocr_text="IPL Live Score",
        ocr_result=None,
    )
    collector = FakeCollector(snapshot)
    engine = ScreenAwarenessEngine(
        collector=collector,
        chrome_skill=FakeChromeSkill(),
        youtube_skill=FakeYouTubeSkill(),
        whatsapp_skill=FakeWhatsAppSkill(unread_count=0),
        music_skill=FakeMusicSkill(summary=""),
        explorer_skill=FakeExplorerSkill(summary=""),
    )

    first = engine.analyze()
    second = engine.analyze()

    assert first.final_summary == second.final_summary
    assert collector.calls == 2
