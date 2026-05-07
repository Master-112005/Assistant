from __future__ import annotations

import pytest
from types import SimpleNamespace

from core import settings, state
from core.automation import WindowTarget
from core.browser import BrowserController, BrowserState
from core.window_context import WindowInfo


class FakeAutomation:
    def __init__(self, windows: list[WindowTarget]):
        self.windows = {window.hwnd: window for window in windows}
        self.foreground_hwnd = windows[0].hwnd if windows else 0
        self.hotkeys: list[tuple[str, ...]] = []
        self.typed: list[str] = []
        self.keys: list[str] = []
        self.scrolls: list[int] = []
        self.click_rects: list[tuple[int, int, int, int]] = []
        self.clipboard_text = ""

    def list_windows(self, *, process_names=None, title_substrings=None):
        if not process_names:
            return list(self.windows.values())
        normalized = {str(name).lower() for name in process_names}
        return [window for window in self.windows.values() if window.process_name.lower() in normalized]

    def get_foreground_window(self):
        return self.foreground_hwnd

    def get_window(self, hwnd: int):
        return self.windows.get(hwnd)

    def focus_window(self, *, title=None, process=None, hwnd=None, timeout=None):
        target = self.windows.get(hwnd) if hwnd else None
        if target is None and process:
            matches = self.list_windows(process_names=[process])
            target = matches[0] if matches else None
        if target:
            self.foreground_hwnd = target.hwnd
        return target

    def hotkey(self, keys):
        self.hotkeys.append(tuple(keys))
        return True

    def type_text(self, text, *, clear=False, delay_ms=None):
        self.typed.append(text)
        return True

    def press_key(self, key):
        self.keys.append(key)
        return True

    def scroll(self, amount: int):
        self.scrolls.append(amount)
        return True

    def click_center(self, rect):
        self.click_rects.append(rect)
        return True

    def safe_sleep(self, duration_ms=None):
        return None

    def wait_for(self, condition, timeout, *, interval=0.05):
        return bool(condition())

    def get_clipboard_text(self):
        return self.clipboard_text


class FakeDetector:
    def __init__(self, info: WindowInfo):
        self.info = info

    def get_active_context(self):
        return self.info

    def get_window_info(self, hwnd: int):
        if hwnd == self.info.hwnd:
            return self.info
        return WindowInfo()


@pytest.fixture(autouse=True)
def reset_browser_state():
    settings.reset_defaults()
    state.last_browser = ""
    state.last_search_query = ""
    state.last_browser_action = ""
    state.browser_ready = False
    state.last_navigation_time = 0.0
    state.current_app = "unknown"
    state.current_context = "unknown"
    state.current_process_name = ""
    state.current_window_title = ""
    yield
    settings.reset_defaults()


def _browser_window(hwnd: int = 101, title: str = "Google", process_name: str = "chrome.exe") -> WindowTarget:
    return WindowTarget(
        hwnd=hwnd,
        title=title,
        process_id=4242,
        process_name=process_name,
        rect=(0, 0, 1440, 900),
        is_visible=True,
        is_minimized=False,
    )


def _browser_info(hwnd: int = 101, title: str = "Google", process_name: str = "chrome.exe", app_id: str = "gmail") -> WindowInfo:
    return WindowInfo(
        hwnd=hwnd,
        title=title,
        process_id=4242,
        process_name=process_name,
        exe_path=rf"C:\Program Files\Browser\{process_name}",
        class_name="Chrome_WidgetWin_1",
        is_visible=True,
        is_minimized=False,
        rect=(0, 0, 1440, 900),
        app_id=app_id,
        app_type="browser",
        context_detail="web",
    )


def test_detect_active_browser_uses_supported_process_name():
    automation = FakeAutomation([_browser_window()])
    detector = FakeDetector(_browser_info(app_id="gmail"))
    controller = BrowserController(automation=automation, detector=detector, launcher=None)

    browser_state = controller.detect_active_browser()

    assert browser_state is not None
    assert browser_state.browser_id == "chrome"
    assert browser_state.site_context == "gmail"


def test_search_focuses_address_bar_types_and_submits_query():
    automation = FakeAutomation([_browser_window(title="New Tab")])
    detector = FakeDetector(_browser_info(title="New Tab", app_id="chrome"))
    controller = BrowserController(automation=automation, detector=detector, launcher=None)

    result = controller.search("IPL score", browser_name="chrome")

    assert result.success is True
    assert result.action == "search"
    assert result.query == "IPL score"
    assert ("ctrl", "l") in automation.hotkeys
    assert automation.typed == ["IPL score"]
    assert "enter" in automation.keys
    assert state.last_search_query == "IPL score"
    assert state.last_browser_action == "search"


def test_go_back_sends_navigation_hotkey():
    automation = FakeAutomation([_browser_window()])
    detector = FakeDetector(_browser_info(app_id="chrome"))
    controller = BrowserController(automation=automation, detector=detector, launcher=None)

    result = controller.go_back(browser_name="chrome")

    assert result.success is True
    assert ("alt", "left") in automation.hotkeys
    assert result.message == "Going back in Chrome."


def test_open_browser_focuses_existing_window_without_relaunch():
    automation = FakeAutomation([_browser_window(title="Google Chrome")])
    detector = FakeDetector(_browser_info(title="Google Chrome", app_id="chrome"))
    controller = BrowserController(automation=automation, detector=detector, launcher=None)

    result = controller.open_browser("chrome")

    assert result.success is True
    assert "already open" in result.message.lower()
    assert result.browser_id == "chrome"


def test_switch_to_tab_sends_ctrl_number_hotkey():
    automation = FakeAutomation([_browser_window()])
    detector = FakeDetector(_browser_info(app_id="chrome"))
    controller = BrowserController(automation=automation, detector=detector, launcher=None)

    result = controller.switch_to_tab(2, browser_name="chrome")

    assert result.success is True
    assert ("ctrl", "2") in automation.hotkeys


def test_copy_page_url_reads_clipboard():
    automation = FakeAutomation([_browser_window()])
    automation.clipboard_text = "https://www.youtube.com/watch?v=test"
    detector = FakeDetector(_browser_info(app_id="chrome"))
    controller = BrowserController(automation=automation, detector=detector, launcher=None)

    result = controller.copy_page_url(browser_name="chrome")

    assert result.success is True
    assert ("ctrl", "l") in automation.hotkeys
    assert ("ctrl", "c") in automation.hotkeys
    assert result.data["url"] == "https://www.youtube.com/watch?v=test"


def test_click_link_blocks_uncertain_fallback_in_safe_mode(monkeypatch):
    automation = FakeAutomation([_browser_window()])
    detector = FakeDetector(_browser_info(app_id="chrome"))
    controller = BrowserController(automation=automation, detector=detector, launcher=None)

    monkeypatch.setattr(controller, "_click_link_uia", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(controller, "_click_link_by_tab", lambda *_args, **_kwargs: False)

    result = controller.click_link(2, browser_name="chrome")

    assert result.success is False
    assert result.error == "unsafe_click_target"
    assert automation.click_rects == []


def test_click_link_uses_heuristic_fallback_when_safe_mode_disabled(monkeypatch):
    settings.set("safe_mode_clicks", False)
    automation = FakeAutomation([_browser_window()])
    detector = FakeDetector(_browser_info(app_id="chrome"))
    controller = BrowserController(automation=automation, detector=detector, launcher=None)

    browser_state = controller.detect_active_browser()
    assert browser_state is not None

    monkeypatch.setattr(controller, "_click_link_uia", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(controller, "_click_link_by_tab", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(controller, "_wait_for_navigation", lambda *_args, **_kwargs: (browser_state, True))

    result = controller.click_link(1, browser_name="chrome")

    assert result.success is True
    assert result.data["method"] == "heuristic"
    assert len(automation.click_rects) == 1


def test_open_url_direct_launch_sets_site_context(monkeypatch):
    automation = FakeAutomation([])
    detector = FakeDetector(WindowInfo())
    controller = BrowserController(automation=automation, detector=detector, launcher=None)
    state_obj = BrowserState(
        browser_id="chrome",
        hwnd=303,
        process_name="chrome.exe",
        title="YouTube - Google Chrome",
        rect=(0, 0, 1440, 900),
        is_foreground=True,
        is_minimized=False,
        is_ready=True,
        site_context="youtube",
    )

    monkeypatch.setattr(controller, "_resolve_browser_executable", lambda _spec: r"C:\fake\chrome.exe")
    monkeypatch.setattr(controller, "_wait_for_direct_url_state", lambda **_kwargs: state_obj)
    monkeypatch.setattr("core.browser.subprocess.Popen", lambda *_args, **_kwargs: SimpleNamespace(pid=1234))

    result = controller._open_url_direct("https://www.youtube.com", "chrome")

    assert result.success is True
    assert result.verified is True
    assert result.data["launch_method"] == "direct_process"
    assert result.state is not None
    assert result.state.site_context == "youtube"
