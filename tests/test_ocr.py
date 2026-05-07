from __future__ import annotations

from PIL import Image
import pytest

from core import settings, state
from core.ocr import OCRLine, OCRMatch, OCREngine, OCRResult, OCRWord
from core.screen import CaptureBounds, ScreenCapture


class FakeCapture:
    def __init__(self, image: Image.Image | None = None):
        self.image = image or Image.new("RGB", (320, 180), "white")
        self.region_calls: list[tuple[int, int, int, int]] = []

    def get_active_window_bounds(self, client_area: bool = False):
        _ = client_area
        return CaptureBounds(left=100, top=200, width=self.image.width, height=self.image.height, hwnd=77, title="Test")

    def capture_active_window(self, *, client_area: bool = False):
        _ = client_area
        return self.image.copy()

    def get_virtual_screen_bounds(self):
        return CaptureBounds(left=0, top=0, width=self.image.width, height=self.image.height)

    def capture_fullscreen(self):
        return self.image.copy()

    def capture_region(self, x: int, y: int, w: int, h: int):
        self.region_calls.append((x, y, w, h))
        return self.image.copy()


class FakeBackend:
    engine_name = "fakeocr"

    def __init__(self):
        self.initialized = False

    def initialize(self) -> None:
        self.initialized = True

    def read(self, image: Image.Image) -> list[OCRWord]:
        _ = image
        return [
            OCRWord(text="Search", confidence=0.98, bbox=(10, 20, 110, 50)),
            OCRWord(text="Results", confidence=0.96, bbox=(120, 20, 250, 50)),
            OCRWord(text="CSK", confidence=0.95, bbox=(15, 90, 80, 120)),
            OCRWord(text="won", confidence=0.92, bbox=(90, 90, 150, 120)),
        ]


class FakeShot:
    def __init__(self, width: int, height: int, color: str):
        image = Image.new("RGB", (width, height), color)
        self.size = image.size
        self.rgb = image.tobytes()


class FakeMSSContext:
    def __init__(self):
        self.monitors = [{"left": 0, "top": 0, "width": 80, "height": 40}]
        self.last_monitor = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def grab(self, monitor):
        self.last_monitor = monitor
        return FakeShot(monitor["width"], monitor["height"], "white")


@pytest.fixture(autouse=True)
def reset_ocr_state():
    settings.reset_defaults()
    settings.set("ocr_preprocess", False)
    state.ocr_ready = False
    state.last_ocr_text = ""
    state.last_ocr_engine = ""
    state.last_screenshot_path = ""
    state.last_text_matches = []
    yield
    settings.reset_defaults()


def test_ocr_engine_reads_active_window_and_maps_screen_coordinates():
    capture = FakeCapture()
    backend = FakeBackend()
    engine = OCREngine(capture=capture, backends=[backend])

    result = engine.read_active_window()

    assert backend.initialized is True
    assert result.engine == "fakeocr"
    assert result.full_text == "Search Results CSK won"
    assert result.words[0].bbox == (110, 220, 210, 250)
    assert result.lines[0].text == "Search Results"
    assert state.ocr_ready is True
    assert state.last_ocr_engine == "fakeocr"
    assert state.last_ocr_text == "Search Results CSK won"


def test_ocr_engine_find_text_returns_phrase_bbox():
    capture = FakeCapture()
    engine = OCREngine(capture=capture, backends=[FakeBackend()])
    result = engine.read_active_window()

    matches = engine.find_text("Search Results", result=result)

    assert len(matches) == 1
    assert matches[0].source == "phrase"
    assert matches[0].bbox == (110, 220, 350, 250)
    assert state.last_text_matches[0]["text"] == "Search Results"


def test_screen_capture_region_uses_real_monitor_spec():
    fake_mss = FakeMSSContext()
    screen = ScreenCapture(mss_factory=lambda: fake_mss)

    image = screen.capture_region(10, 20, 80, 40)

    assert image.size == (80, 40)
    assert fake_mss.last_monitor == {"left": 10, "top": 20, "width": 80, "height": 40}


def test_screen_capture_active_window_uses_bounds_provider():
    fake_mss = FakeMSSContext()
    bounds = CaptureBounds(left=50, top=60, width=80, height=40, hwnd=5, title="Chrome")
    screen = ScreenCapture(
        mss_factory=lambda: fake_mss,
        active_window_getter=lambda client_area=False: bounds,
    )

    image = screen.capture_active_window()

    assert image.size == (80, 40)
    assert fake_mss.last_monitor == {"left": 50, "top": 60, "width": 80, "height": 40}
