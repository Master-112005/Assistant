from __future__ import annotations

import pytest

from core import settings, state
from core.browser import BrowserOperationResult, BrowserState

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

from skills.chrome import ChromeSkill


class FakeChromeController:
    def __init__(self):
        self.calls: list[tuple] = []
        self.focus_success = True
        self.page_title = "IPL live scores - Google Chrome"

    def _state(self) -> BrowserState:
        return BrowserState(
            browser_id="chrome",
            hwnd=101,
            process_name="chrome.exe",
            title=self.page_title,
            rect=(0, 0, 1440, 900),
            is_foreground=True,
            is_minimized=False,
            is_ready=True,
        )

    def focus_browser(self, browser_name: str | None = None, *, launch_if_missing: bool = False):
        self.calls.append(("focus_browser", browser_name, launch_if_missing))
        if not self.focus_success:
            return BrowserOperationResult(
                success=False,
                action="focus_browser",
                message="Chrome is not open.",
                error="browser_not_found",
            )
        state.current_context = "chrome"
        state.current_app = "chrome"
        state.current_process_name = "chrome.exe"
        return BrowserOperationResult(
            success=True,
            action="focus_browser",
            message="Chrome focused.",
            browser_id="chrome",
            state=self._state(),
            verified=True,
        )

    def search(self, query: str, *, browser_name: str | None = None, engine: str | None = None):
        self.calls.append(("search", query, browser_name, engine))
        return BrowserOperationResult(
            success=True,
            action="search",
            message="Chrome search complete.",
            browser_id="chrome",
            query=query,
            state=self._state(),
            verified=True,
        )

    def new_tab(self, *, browser_name: str | None = None):
        self.calls.append(("new_tab", browser_name))
        return BrowserOperationResult(success=True, action="new_tab", message="Opened", browser_id="chrome", state=self._state(), verified=True)

    def close_tab(self, *, browser_name: str | None = None):
        self.calls.append(("close_tab", browser_name))
        return BrowserOperationResult(success=True, action="close_tab", message="Closed", browser_id="chrome", state=self._state(), verified=True)

    def go_back(self, *, browser_name: str | None = None):
        self.calls.append(("go_back", browser_name))
        return BrowserOperationResult(success=True, action="go_back", message="Back", browser_id="chrome", state=self._state(), verified=True)

    def go_forward(self, *, browser_name: str | None = None):
        self.calls.append(("go_forward", browser_name))
        return BrowserOperationResult(success=True, action="go_forward", message="Forward", browser_id="chrome", state=self._state(), verified=True)

    def refresh(self, *, browser_name: str | None = None):
        self.calls.append(("refresh", browser_name))
        return BrowserOperationResult(success=True, action="refresh", message="Refreshed", browser_id="chrome", state=self._state(), verified=True)

    def go_home(self, *, browser_name: str | None = None):
        self.calls.append(("home", browser_name))
        return BrowserOperationResult(success=True, action="home", message="Home", browser_id="chrome", state=self._state(), verified=True)

    def scroll_down(self, amount: int = 500, *, browser_name: str | None = None):
        self.calls.append(("scroll_down", amount, browser_name))
        return BrowserOperationResult(success=True, action="scroll", message="Scrolled down", browser_id="chrome", state=self._state(), verified=True)

    def scroll_up(self, amount: int = 500, *, browser_name: str | None = None):
        self.calls.append(("scroll_up", amount, browser_name))
        return BrowserOperationResult(success=True, action="scroll", message="Scrolled up", browser_id="chrome", state=self._state(), verified=True)

    def next_tab(self, *, browser_name: str | None = None):
        self.calls.append(("next_tab", browser_name))
        return BrowserOperationResult(success=True, action="next_tab", message="Next", browser_id="chrome", state=self._state(), verified=True)

    def previous_tab(self, *, browser_name: str | None = None):
        self.calls.append(("previous_tab", browser_name))
        return BrowserOperationResult(success=True, action="previous_tab", message="Previous", browser_id="chrome", state=self._state(), verified=True)

    def switch_to_tab(self, tab_index: int, *, browser_name: str | None = None):
        self.calls.append(("switch_to_tab", tab_index, browser_name))
        return BrowserOperationResult(success=True, action="switch_tab", message="Switched", browser_id="chrome", state=self._state(), verified=True)

    def copy_page_url(self, *, browser_name: str | None = None):
        self.calls.append(("copy_page_url", browser_name))
        return BrowserOperationResult(
            success=True,
            action="copy_page_url",
            message="Copied URL",
            browser_id="chrome",
            state=self._state(),
            verified=True,
            data={"url": "https://www.youtube.com/watch?v=test"},
        )

    def is_browser_installed(self, browser_name: str | None = None):
        return True

    def is_browser_ready(self, browser_name: str | None = None):
        return self.focus_success


class FakeOCREngine:
    def __init__(self, result: OCRResult | None = None):
        self.result = result or OCRResult(
            full_text="IPL live score Mumbai Indians",
            words=[],
            lines=[
                OCRLine(text="IPL live score", confidence=0.97, bbox=(20, 120, 240, 150), words=[]),
                OCRLine(text="Mumbai Indians", confidence=0.95, bbox=(20, 170, 260, 200), words=[]),
            ],
            engine="fakeocr",
            processing_time=0.15,
            capture_mode="region",
        )
        self.calls: list[tuple[int, int, int, int]] = []

    def read_region(self, x: int, y: int, w: int, h: int):
        self.calls.append((x, y, w, h))
        return self.result


@pytest.fixture(autouse=True)
def reset_chrome_skill_state():
    settings.reset_defaults()
    state.current_context = "unknown"
    state.current_app = "unknown"
    state.current_process_name = ""
    state.current_window_title = ""
    state.last_browser = ""
    state.last_search_query = ""
    state.last_browser_action = ""
    state.last_chrome_action = ""
    state.chrome_tabs_opened_count = 0
    state.last_page_title = ""
    yield
    settings.reset_defaults()


def test_can_handle_routes_search_when_preferred_browser_is_chrome():
    skill = ChromeSkill(controller=FakeChromeController())
    context = {"current_app": "unknown", "current_process_name": "", "context_target_app": ""}

    assert skill.can_handle(context, "search", "search IPL score") is True


def test_can_handle_routes_navigation_when_chrome_is_active():
    skill = ChromeSkill(controller=FakeChromeController())
    context = {"current_app": "chrome", "current_process_name": "chrome.exe", "context_target_app": "chrome"}

    assert skill.can_handle(context, "unknown", "go back") is True


def test_can_handle_new_tab_when_chrome_is_preferred_browser():
    skill = ChromeSkill(controller=FakeChromeController())
    context = {"current_app": "unknown", "current_process_name": "", "context_target_app": ""}

    assert skill.can_handle(context, "browser_tab_new", "open new tab") is True


def test_handle_search_executes_real_controller_search():
    controller = FakeChromeController()
    skill = ChromeSkill(controller=controller)

    result = skill.execute("search IPL score", {"intent": "search"})

    assert result.success is True
    assert result.response == "Searching IPL score in Chrome"
    assert ("search", "IPL score", "chrome", None) in controller.calls
    assert state.last_chrome_action == "search"


def test_open_new_tab_increments_runtime_counter():
    controller = FakeChromeController()
    skill = ChromeSkill(controller=controller)

    result = skill.execute("new tab", {"intent": "unknown", "current_process_name": "chrome.exe"})

    assert result.success is True
    assert result.response == "Opened a new tab"
    assert state.chrome_tabs_opened_count == 1


def test_navigation_and_refresh_commands_route_to_controller():
    controller = FakeChromeController()
    skill = ChromeSkill(controller=controller)

    back = skill.execute("go back", {"intent": "unknown", "current_process_name": "chrome.exe"})
    refresh = skill.execute("refresh page", {"intent": "unknown", "current_process_name": "chrome.exe"})

    assert back.success is True
    assert refresh.success is True
    assert ("go_back", "chrome") in controller.calls
    assert ("refresh", "chrome") in controller.calls


def test_switch_tab_routes_to_controller():
    controller = FakeChromeController()
    skill = ChromeSkill(controller=controller)

    result = skill.execute("switch to second tab", {"intent": "browser_tab_switch", "current_process_name": "chrome.exe"})

    assert result.success is True
    assert result.response == "Switching to tab 2 in Chrome"
    assert ("switch_to_tab", 2, "chrome") in controller.calls


def test_copy_page_url_routes_to_controller():
    controller = FakeChromeController()
    skill = ChromeSkill(controller=controller)

    result = skill.execute("copy page url", {"intent": "browser_action", "current_process_name": "chrome.exe"})

    assert result.success is True
    assert result.response == "Copied the current page URL."
    assert result.data["url"] == "https://www.youtube.com/watch?v=test"
    assert ("copy_page_url", "chrome") in controller.calls


def test_scroll_command_routes_to_controller():
    controller = FakeChromeController()
    skill = ChromeSkill(controller=controller)

    result = skill.execute("scroll down", {"intent": "unknown", "current_process_name": "chrome.exe"})

    assert result.success is True
    assert result.response == "Scrolling page down"
    assert ("scroll_down", 500, "chrome") in controller.calls


def test_read_page_title_returns_real_title():
    controller = FakeChromeController()
    skill = ChromeSkill(controller=controller)

    result = skill.execute("read page title", {"intent": "unknown", "current_process_name": "chrome.exe"})

    assert result.success is True
    assert result.response == "Current Chrome page title: IPL live scores"
    assert state.last_page_title == "IPL live scores"


def test_read_results_returns_detected_visible_content(monkeypatch):
    controller = FakeChromeController()
    skill = ChromeSkill(controller=controller)
    monkeypatch.setattr(skill, "_read_visible_content", lambda _state: (["IPL live scores", "Mumbai Indians"], "uia"))

    result = skill.execute("read results", {"intent": "unknown", "current_process_name": "chrome.exe"})

    assert result.success is True
    assert result.response == "Visible Chrome content: IPL live scores | Mumbai Indians"
    assert result.data["source"] == "uia"


def test_read_first_result_returns_requested_item(monkeypatch):
    controller = FakeChromeController()
    skill = ChromeSkill(controller=controller)
    monkeypatch.setattr(skill, "_read_visible_content", lambda _state: (["IPL live scores", "Points table"], "uia"))

    result = skill.execute("read first result", {"intent": "unknown", "current_process_name": "chrome.exe"})

    assert result.success is True
    assert result.response == "Visible result 1: IPL live scores"


def test_chrome_closed_is_handled_honestly():
    controller = FakeChromeController()
    controller.focus_success = False
    skill = ChromeSkill(controller=controller)

    result = skill.execute("new tab", {"intent": "unknown", "current_process_name": ""})

    assert result.success is False
    assert "Chrome is not open" in result.response


def test_read_results_uses_ocr_fallback_when_uia_has_no_content(monkeypatch):
    controller = FakeChromeController()
    ocr = FakeOCREngine()
    skill = ChromeSkill(controller=controller, ocr_engine=ocr)
    monkeypatch.setattr(skill, "_read_visible_text_uia", lambda _state: [])
    controller.page_title = ""

    result = skill.execute("read results", {"intent": "unknown", "current_process_name": "chrome.exe"})

    assert result.success is True
    assert result.response == "Visible Chrome content: IPL live score | Mumbai Indians"
    assert result.data["source"] == "ocr"
    assert ocr.calls
