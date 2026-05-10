from __future__ import annotations

import pytest

from core import settings, state
from core.click_text import TextClickEngine
from core.screen import CaptureBounds

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

class OCRWord:
    def __init__(self, text="", confidence=0.9, bbox=(0,0,0,0)):
        self.text = text
        self.confidence = confidence
        self.bbox = bbox


class FakeWindow:
    def __init__(self, title: str):
        self.title = title


class FakeAutomation:
    def __init__(self, *, titles: list[str] | None = None, hwnds: list[int] | None = None, click_success: bool = True):
        self.titles = titles or ["Login - Chrome"]
        self.hwnds = hwnds or [101]
        self.click_success = click_success
        self.clicks: list[tuple[int, int]] = []
        self.moves: list[tuple[int, int]] = []
        self._title_index = 0
        self._hwnd_index = 0

    def get_foreground_window(self) -> int:
        return self.hwnds[min(self._hwnd_index, len(self.hwnds) - 1)]

    def get_window(self, hwnd: int):
        _ = hwnd
        return FakeWindow(self.titles[min(self._title_index, len(self.titles) - 1)])

    def click_point(self, x: int, y: int, *, button: str = "left", double: bool = False) -> bool:
        _ = button
        _ = double
        self.clicks.append((x, y))
        self._title_index = min(self._title_index + 1, len(self.titles) - 1)
        self._hwnd_index = min(self._hwnd_index + 1, len(self.hwnds) - 1)
        return self.click_success

    def move_point(self, x: int, y: int) -> bool:
        self.moves.append((x, y))
        return True

    def safe_sleep(self, duration_ms: int | None = None) -> None:
        _ = duration_ms


class FakeCapture:
    def __init__(self, *, active_bounds: CaptureBounds | None = None, virtual_bounds: CaptureBounds | None = None):
        self.active_bounds = active_bounds or CaptureBounds(left=0, top=0, width=1280, height=720, hwnd=101, title="Login - Chrome")
        self.virtual_bounds = virtual_bounds or CaptureBounds(left=0, top=0, width=1280, height=720)

    def get_active_window_bounds(self, *, client_area: bool = False):
        _ = client_area
        return self.active_bounds

    def get_virtual_screen_bounds(self):
        return self.virtual_bounds


class FakeOCREngine:
    def __init__(self, results: list[OCRResult]):
        self.results = list(results)
        self.read_calls = 0

    def read_capture_mode(self, capture_mode: str | None = None):
        _ = capture_mode
        index = min(self.read_calls, len(self.results) - 1)
        self.read_calls += 1
        return self.results[index]

    def read_image(self, image, *, capture_mode: str = "image", capture_bounds=None):
        _ = image
        _ = capture_mode
        _ = capture_bounds
        return self.read_capture_mode("image")


def _make_result(
    *,
    words: list[tuple[str, float, tuple[int, int, int, int]]] | None = None,
    lines: list[tuple[str, float, tuple[int, int, int, int]]] | None = None,
    full_text: str = "",
    capture_mode: str = "active_window",
) -> OCRResult:
    word_items = [OCRWord(text=text, confidence=confidence, bbox=bbox) for text, confidence, bbox in (words or [])]
    line_items = [OCRLine(text=text, confidence=confidence, bbox=bbox, words=[]) for text, confidence, bbox in (lines or [])]
    if not full_text:
        full_text = " ".join(item.text for item in line_items or word_items)
    return OCRResult(
        full_text=full_text,
        words=word_items,
        lines=line_items,
        engine="fakeocr",
        processing_time=0.1,
        capture_mode=capture_mode,
        capture_bounds=(0, 0, 1280, 720),
    )


@pytest.fixture(autouse=True)
def reset_click_text_state():
    settings.reset_defaults()
    state.last_text_click_target = {}
    state.last_text_click_result = {}
    state.last_clicked_position = {}
    state.text_click_count = 0
    yield
    settings.reset_defaults()


def test_find_targets_supports_fuzzy_match_for_login():
    result = _make_result(words=[("Login", 0.98, (520, 320, 760, 380))], lines=[("Login", 0.98, (520, 320, 760, 380))])
    engine = TextClickEngine(ocr_engine=FakeOCREngine([result]), automation=FakeAutomation(), capture=FakeCapture())

    targets = engine.find_targets("log in")

    assert targets
    assert targets[0].text == "Login"
    assert targets[0].bbox == (520, 320, 760, 380)
    assert targets[0].match_score >= 0.80


def test_rank_targets_prefers_larger_centered_login_target():
    result = _make_result(
        words=[
            ("Login", 0.96, (30, 40, 110, 72)),
            ("Login", 0.92, (520, 320, 760, 392)),
        ],
        lines=[
            ("Login", 0.96, (30, 40, 110, 72)),
            ("Login", 0.92, (520, 320, 760, 392)),
        ],
    )
    engine = TextClickEngine(ocr_engine=FakeOCREngine([result]), automation=FakeAutomation(), capture=FakeCapture())

    targets = engine.find_targets("login")

    assert len(targets) >= 2
    assert targets[0].bbox == (520, 320, 760, 392)


def test_click_text_clicks_visible_target_and_verifies_on_ocr_change():
    before = _make_result(
        words=[("Login", 0.98, (520, 320, 760, 380)), ("Cancel", 0.90, (780, 320, 940, 380))],
        lines=[("Login", 0.98, (520, 320, 760, 380)), ("Cancel", 0.90, (780, 320, 940, 380))],
        full_text="Login Cancel",
    )
    after = _make_result(
        words=[("Dashboard", 0.98, (420, 80, 860, 140))],
        lines=[("Dashboard", 0.98, (420, 80, 860, 140))],
        full_text="Dashboard",
    )
    automation = FakeAutomation(titles=["Login - Chrome", "Dashboard - Chrome"])
    engine = TextClickEngine(ocr_engine=FakeOCREngine([before, after]), automation=automation, capture=FakeCapture())

    result = engine.click_text("Login")

    assert result.success is True
    assert result.verification_passed is True
    assert result.clicked_x == 640
    assert result.clicked_y == 350
    assert automation.clicks == [(640, 350)]
    assert state.last_text_click_target["text"] == "Login"
    assert state.last_clicked_position == {"x": 640, "y": 350}
    assert state.text_click_count == 1


def test_click_text_handles_unknown_target_honestly():
    result = _make_result(words=[("Search", 0.96, (100, 80, 280, 120))], lines=[("Search", 0.96, (100, 80, 280, 120))])
    automation = FakeAutomation()
    engine = TextClickEngine(ocr_engine=FakeOCREngine([result]), automation=automation, capture=FakeCapture())

    click_result = engine.click_text("Login")

    assert click_result.success is False
    assert 'visible text matching "Login"' in click_result.message
    assert automation.clicks == []


def test_click_text_avoids_ambiguous_dangerous_targets():
    result = _make_result(
        words=[
            ("Delete", 0.95, (140, 500, 280, 548)),
            ("Delete", 0.95, (900, 500, 1040, 548)),
        ],
        lines=[
            ("Delete", 0.95, (140, 500, 280, 548)),
            ("Delete", 0.95, (900, 500, 1040, 548)),
        ],
    )
    automation = FakeAutomation()
    engine = TextClickEngine(ocr_engine=FakeOCREngine([result]), automation=automation, capture=FakeCapture())

    click_result = engine.click_text("Delete")

    assert click_result.success is False
    assert click_result.ambiguous is True
    assert "dangerous" in click_result.message.lower() or "specific" in click_result.message.lower()
    assert automation.clicks == []


def test_repeated_clicks_remain_stable_when_verification_is_disabled():
    settings.set("click_text_verify", False)
    result = _make_result(words=[("Next", 0.94, (1040, 620, 1180, 684))], lines=[("Next", 0.94, (1040, 620, 1180, 684))])
    automation = FakeAutomation()
    engine = TextClickEngine(ocr_engine=FakeOCREngine([result, result]), automation=automation, capture=FakeCapture())

    first = engine.click_text("Next")
    engine._cached_at = 0.0
    second = engine.click_text("Next")

    assert first.success is True
    assert second.success is True
    assert automation.clicks == [(1110, 652), (1110, 652)]
    assert state.text_click_count == 2
