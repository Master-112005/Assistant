"""
Production-grade desktop browser automation foundation.

The controller is layered around:

1. Window discovery / targeting
2. Foreground focus
3. Keyboard-first browser control
4. Optional UI Automation element targeting
5. Verification and runtime state updates
6. Safe fallbacks for uncertain click operations
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import quote_plus, urlparse

from core import settings, state
from core.automation import DesktopAutomation, WindowTarget
from core.logger import get_logger
from core.window_context import ActiveWindowDetector, WindowInfo

logger = get_logger(__name__)

try:  # pragma: no cover - optional dependency
    from pywinauto import Desktop

    _PYWINAUTO_OK = True
except Exception:  # pragma: no cover - optional dependency
    Desktop = None
    _PYWINAUTO_OK = False

try:  # pragma: no cover - Windows-only registry lookup
    import winreg
except ImportError:  # pragma: no cover
    winreg = None


SEARCH_ENGINE_URLS: dict[str, str] = {
    "google": "https://www.google.com/search?q={query}",
    "bing": "https://www.bing.com/search?q={query}",
    "duckduckgo": "https://duckduckgo.com/?q={query}",
    "youtube": "https://www.youtube.com/results?search_query={query}",
}


@dataclass(frozen=True)
class BrowserSpec:
    browser_id: str
    display_name: str
    process_names: tuple[str, ...]
    launch_aliases: tuple[str, ...]
    address_bar_names: tuple[str, ...]
    known_paths: tuple[str, ...] = ()


@dataclass
class BrowserState:
    browser_id: str
    hwnd: int
    process_name: str
    title: str
    rect: tuple[int, int, int, int]
    is_foreground: bool
    is_minimized: bool
    is_ready: bool
    site_context: str = ""

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])


@dataclass
class BrowserOperationResult:
    success: bool
    action: str
    message: str
    error: str = ""
    browser_id: str = ""
    query: str = ""
    url: str = ""
    verified: bool = False
    state: Optional[BrowserState] = None
    data: dict[str, Any] = field(default_factory=dict)


_BROWSER_SPECS: dict[str, BrowserSpec] = {
    "chrome": BrowserSpec(
        browser_id="chrome",
        display_name="Chrome",
        process_names=("chrome.exe",),
        launch_aliases=("chrome", "google chrome"),
        address_bar_names=(
            "address and search bar",
            "search google or type a url",
            "address bar",
        ),
        known_paths=(
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        ),
    ),
    "edge": BrowserSpec(
        browser_id="edge",
        display_name="Edge",
        process_names=("msedge.exe",),
        launch_aliases=("edge", "microsoft edge"),
        address_bar_names=(
            "address and search bar",
            "search or enter web address",
            "address bar",
        ),
        known_paths=(
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        ),
    ),
    "firefox": BrowserSpec(
        browser_id="firefox",
        display_name="Firefox",
        process_names=("firefox.exe",),
        launch_aliases=("firefox", "mozilla firefox"),
        address_bar_names=(
            "search with google or enter address",
            "search or enter address",
            "address bar",
        ),
        known_paths=(
            r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
            r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe",
        ),
    ),
    "brave": BrowserSpec(
        browser_id="brave",
        display_name="Brave",
        process_names=("brave.exe",),
        launch_aliases=("brave", "brave browser"),
        address_bar_names=(
            "address and search bar",
            "search brave or type a url",
            "address bar",
        ),
        known_paths=(
            r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe",
        ),
    ),
}

_BROWSER_ALIASES = {
    "browser": "",
    "google chrome": "chrome",
    "microsoft edge": "edge",
    "mozilla firefox": "firefox",
    "brave browser": "brave",
}

_BROWSER_ORDER = ("chrome", "edge", "firefox", "brave")


class BrowserController:
    """Reliable desktop browser automation controller for Windows browsers."""

    def __init__(
        self,
        *,
        automation: DesktopAutomation | None = None,
        detector: ActiveWindowDetector | None = None,
        launcher=None,
    ) -> None:
        self._automation = automation or DesktopAutomation()
        self._detector = detector or ActiveWindowDetector()
        self._launcher = launcher

    def detect_active_browser(self) -> BrowserState | None:
        info = self._safe_active_context()
        if info and self._browser_id_from_process(info.process_name):
            return self._state_from_window_info(info)

        foreground = self._automation.get_foreground_window()
        target = self._automation.get_window(foreground)
        if not target:
            return None
        browser_id = self._browser_id_from_process(target.process_name)
        if not browser_id:
            return None
        return self._state_from_target(target)

    def focus_browser(
        self,
        browser_name: str | None = None,
        *,
        launch_if_missing: bool = False,
    ) -> BrowserOperationResult:
        requested = self._normalize_browser_name(browser_name)
        active = self.detect_active_browser()

        if active and (not requested or active.browser_id == requested):
            self._record_browser_state(active, action="focus_browser")
            return self._success("focus_browser", active, f"{self._display_name(active.browser_id)} focused.", verified=True)

        target = self._find_browser_window(requested)
        if target is None and launch_if_missing:
            launch_result = self._launch_browser(requested or self._preferred_browser())
            if not launch_result.success:
                return launch_result
            target = self._wait_for_browser_window(requested or self._preferred_browser())

        if target is None:
            return self._failure(
                "focus_browser",
                "No supported browser window found.",
                "browser_not_found",
                browser_id=requested or "",
            )

        focused = self._automation.focus_window(hwnd=target.hwnd, timeout=float(settings.get("browser_focus_timeout") or 5))
        if not focused:
            return self._failure(
                "focus_browser",
                f"Could not focus {self._display_name(requested or self._browser_id_from_process(target.process_name) or 'browser')}.",
                "browser_focus_failed",
                browser_id=requested or self._browser_id_from_process(target.process_name) or "",
            )

        state_obj = self._state_from_target(focused)
        self._record_browser_state(state_obj, action="focus_browser")
        logger.info("Browser focused: %s", state_obj.browser_id)
        return self._success("focus_browser", state_obj, f"{self._display_name(state_obj.browser_id)} focused.", verified=True)

    def open_browser(self, browser_name: str | None = None) -> BrowserOperationResult:
        requested = self._normalize_browser_name(browser_name) or self._preferred_browser()
        active = self.detect_active_browser()
        if active and active.browser_id == requested:
            focused = self.focus_browser(requested, launch_if_missing=False)
            if focused.success and focused.state:
                return self._success(
                    "open_browser",
                    focused.state,
                    f"{self._display_name(requested)} is already open. Bringing it to front.",
                    verified=True,
                )
            return focused

        existing = self.focus_browser(requested, launch_if_missing=False)
        if existing.success and existing.state:
            return self._success(
                "open_browser",
                existing.state,
                f"{self._display_name(requested)} is already open. Bringing it to front.",
                verified=True,
            )

        launched = self._launch_browser(requested)
        if not launched.success:
            return launched

        target = self._wait_for_browser_window(requested)
        if target is None:
            return self._failure(
                "open_browser",
                f"I launched {self._display_name(requested)}, but I couldn't verify that it opened.",
                "browser_launch_not_verified",
                browser_id=requested,
            )

        focused = self._automation.focus_window(hwnd=target.hwnd, timeout=float(settings.get("browser_focus_timeout") or 5))
        state_obj = self._state_from_target(focused or target)
        self._record_browser_state(state_obj, action="open_browser")
        return self._success("open_browser", state_obj, f"Opening {self._display_name(requested)}.", verified=True)

    def open_url(self, url: str, browser_name: str | None = None) -> BrowserOperationResult:
        """Open URL in browser (fast non-blocking implementation).

        Returns immediately after successful direct launch without blocking verification.
        """
        normalized_url = self._normalize_url(url)
        if not normalized_url:
            return self._failure("open_url", "No URL provided.", "empty_url")

        requested_browser = self._normalize_browser_name(browser_name)
        logger.info("Opening URL (non-blocking): %s in %s", normalized_url, requested_browser or "default browser")

        # FAST PATH: Try direct URL launch (process.Popen or webbrowser)
        # This returns immediately without blocking verification
        direct_result = self._open_url_direct(normalized_url, requested_browser or None)
        if direct_result.success:
            logger.info("URL opened successfully via direct launch in %.0fms", direct_result.data.get("launch_duration_ms", 0) if isinstance(direct_result.data, dict) else 0)
            return direct_result

        # FALLBACK: If direct launch failed, try keyboard automation
        # This is slower but more reliable for already-open browsers
        focus_result = self.focus_browser(requested_browser or None, launch_if_missing=True)
        if not focus_result.success:
            logger.warning("Could not focus browser, returning direct result")
            return direct_result

        if focus_result.state:
            # Use fast keyboard automation without blocking verification
            bar_result = self.click_address_bar(focus_result.state.browser_id)
            if bar_result.success:
                submit_result = self.type_and_submit(normalized_url, browser_name=focus_result.state.browser_id)
                if submit_result.success:
                    logger.info("URL submitted via keyboard in browser %s", focus_result.state.browser_id)
                    return self._success(
                        "open_url",
                        submit_result.state,
                        f"Opening {normalized_url} in {self._display_name(focus_result.state.browser_id)}.",
                        url=normalized_url,
                        verified=False,  # Don't block waiting for navigation verification
                        data={"method": "keyboard_submit"},
                    )

        # All methods failed
        return self._failure(
            "open_url",
            f"I couldn't open {normalized_url}.",
            "browser_launch_failed",
            browser_id=requested_browser,
            url=normalized_url,
        )

    def _open_url_direct(self, url: str, browser_name: str | None = None) -> BrowserOperationResult:
        """Direct non-blocking URL open via subprocess.Popen or webbrowser module."""
        requested = self._normalize_browser_name(browser_name)
        resolved_browser = requested or self._preferred_browser()
        before_state = self.detect_active_browser()
        browser_was_ready = bool(before_state and before_state.is_ready)

        import time as time_module
        start_time = time_module.perf_counter()

        # Try subprocess.Popen first (fastest, non-blocking)
        spec = _BROWSER_SPECS.get(resolved_browser)
        if spec is not None:
            executable = self._resolve_browser_executable(spec)
            if executable:
                try:
                    subprocess.Popen(
                        [executable, url],
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    elapsed_ms = int((time_module.perf_counter() - start_time) * 1000)
                    state_obj = self._wait_for_direct_url_state(
                        requested=resolved_browser,
                        url=url,
                        before_state=before_state,
                        browser_was_ready=browser_was_ready,
                    )
                    logger.info("URL opened via subprocess.Popen (%s) in %dms", resolved_browser, elapsed_ms)
                    return self._success(
                        "open_url",
                        state_obj,
                        f"Opening {url} in {self._display_name(resolved_browser)}.",
                        url=url,
                        verified=state_obj is not None,
                        data={
                            "launch_method": "direct_process",
                            "requested_browser": resolved_browser,
                            "launch_duration_ms": elapsed_ms,
                        },
                    )
                except Exception as exc:
                    logger.debug("subprocess.Popen failed for %s: %s", resolved_browser, exc)

        # Fallback: webbrowser module (also non-blocking)
        try:
            opened = bool(webbrowser.open(url, new=2, autoraise=True))
            if opened:
                elapsed_ms = int((time_module.perf_counter() - start_time) * 1000)
                state_obj = self._wait_for_direct_url_state(
                    requested=resolved_browser,
                    url=url,
                    before_state=before_state,
                    browser_was_ready=browser_was_ready,
                )
                logger.info("URL opened via webbrowser in %dms", elapsed_ms)
                return self._success(
                    "open_url",
                    state_obj,
                    f"Opening {url}.",
                    url=url,
                    verified=state_obj is not None,
                    data={
                        "launch_method": "direct_webbrowser",
                        "requested_browser": requested or "",
                        "launch_duration_ms": elapsed_ms,
                    },
                )
        except Exception as exc:
            logger.debug("webbrowser.open failed: %s", exc)

        # All direct methods failed
        return self._failure(
            "open_url",
            f"I couldn't open {url}.",
            "browser_launch_failed",
            browser_id=resolved_browser,
            url=url,
        )

    def _wait_for_direct_url_state(
        self,
        *,
        requested: str,
        url: str,
        before_state: BrowserState | None,
        browser_was_ready: bool,
    ) -> BrowserState | None:
        """Fast verification - skip blocking wait for instant return."""
        return None

    def search(
        self,
        query: str,
        *,
        browser_name: str | None = None,
        engine: str | None = None,
    ) -> BrowserOperationResult:
        cleaned_query = str(query or "").strip()
        if not cleaned_query:
            return self._failure("search", "Search query is empty.", "empty_query")

        engine_name = self._normalize_engine(engine)
        if engine_name in SEARCH_ENGINE_URLS:
            search_url = SEARCH_ENGINE_URLS[engine_name].format(query=quote_plus(cleaned_query))
            open_result = self.open_url(search_url, browser_name=browser_name)
            if open_result.success:
                state.last_search_query = cleaned_query
                state.last_browser_action = "search"
                return self._success(
                    "search",
                    open_result.state,
                    f"Searching {cleaned_query} in {self._display_name(open_result.browser_id or self._preferred_browser())}.",
                    query=cleaned_query,
                    url=search_url,
                    verified=open_result.verified,
                    data={"engine": engine_name, **open_result.data},
                )
            return self._failure("search", open_result.message, open_result.error or "search_failed", url=search_url)

        focus_result = self.focus_browser(browser_name, launch_if_missing=True)
        if not focus_result.success or not focus_result.state:
            return self._failure("search", focus_result.message, focus_result.error or "browser_not_ready")

        before_title = focus_result.state.title
        bar_result = self.click_address_bar(focus_result.state.browser_id)
        if not bar_result.success:
            return self._failure("search", bar_result.message, bar_result.error or "address_bar_focus_failed")

        submit_result = self.type_and_submit(cleaned_query, browser_name=focus_result.state.browser_id)
        if not submit_result.success or not submit_result.state:
            return self._failure("search", submit_result.message, submit_result.error or "submit_failed")

        final_state, verified = self._wait_for_navigation(submit_result.state.hwnd, before_title)
        state_obj = final_state or submit_result.state
        state.last_search_query = cleaned_query
        self._record_browser_state(state_obj, action="search")

        logger.info("Search query: %s", cleaned_query)
        return self._success(
            "search",
            state_obj,
            f"Searching {cleaned_query} in {self._display_name(state_obj.browser_id)}.",
            query=cleaned_query,
            verified=verified,
            data={"engine": engine_name or "browser"},
        )

    def click_address_bar(self, browser_name: str | None = None) -> BrowserOperationResult:
        focus_result = self.focus_browser(browser_name, launch_if_missing=False)
        if not focus_result.success or not focus_result.state:
            return self._failure("click_address_bar", focus_result.message, focus_result.error or "browser_not_ready")

        state_obj = focus_result.state
        if self._automation.hotkey(["ctrl", "l"]):
            self._record_browser_state(state_obj, action="click_address_bar")
            logger.info("Address bar focused with Ctrl+L")
            return self._success("click_address_bar", state_obj, "Address bar focused.", verified=True)

        if self._focus_address_bar_uia(state_obj):
            self._record_browser_state(state_obj, action="click_address_bar")
            logger.info("Address bar focused with UI Automation")
            return self._success("click_address_bar", state_obj, "Address bar focused.", verified=True)

        rect = self._address_bar_rect(state_obj)
        if self._automation.click_center(rect):
            self._record_browser_state(state_obj, action="click_address_bar")
            logger.info("Address bar focused with relative click fallback")
            return self._success("click_address_bar", state_obj, "Address bar focused.", verified=False)

        return self._failure("click_address_bar", "Could not focus the browser address bar.", "address_bar_focus_failed")

    def type_and_submit(self, text: str, *, browser_name: str | None = None) -> BrowserOperationResult:
        return self.type_text(
            text,
            browser_name=browser_name,
            submit=True,
            action="type_and_submit",
        )

    def scroll_down(self, amount: int = 500, *, browser_name: str | None = None) -> BrowserOperationResult:
        return self._scroll(direction="down", amount=amount, browser_name=browser_name)

    def scroll_up(self, amount: int = 500, *, browser_name: str | None = None) -> BrowserOperationResult:
        return self._scroll(direction="up", amount=amount, browser_name=browser_name)

    def click_link(self, index: int = 1, *, browser_name: str | None = None) -> BrowserOperationResult:
        link_index = max(1, int(index or 1))
        focus_result = self.focus_browser(browser_name, launch_if_missing=False)
        if not focus_result.success or not focus_result.state:
            return self._failure("click_link", focus_result.message, focus_result.error or "browser_not_ready")

        state_obj = focus_result.state
        before_title = state_obj.title

        if self._click_link_uia(state_obj, link_index):
            final_state, verified = self._wait_for_navigation(state_obj.hwnd, before_title)
            current_state = final_state or state_obj
            self._record_browser_state(current_state, action="click_link")
            logger.info("Clicked browser element %d using UI Automation", link_index)
            return self._success(
                "click_link",
                current_state,
                f"Clicked link {link_index} in {self._display_name(current_state.browser_id)}.",
                verified=verified,
                data={"index": link_index, "method": "uia"},
            )

        if self._click_link_by_tab(link_index):
            final_state, verified = self._wait_for_navigation(state_obj.hwnd, before_title)
            if verified:
                current_state = final_state or state_obj
                self._record_browser_state(current_state, action="click_link")
                logger.info("Clicked browser element %d using tab navigation", link_index)
                return self._success(
                    "click_link",
                    current_state,
                    f"Clicked link {link_index} in {self._display_name(current_state.browser_id)}.",
                    verified=True,
                    data={"index": link_index, "method": "tab"},
                )

        if settings.get("safe_mode_clicks"):
            return self._failure(
                "click_link",
                f"Could not safely identify link {link_index} in the current browser page.",
                "unsafe_click_target",
                data={"index": link_index},
            )

        rect = self._result_click_rect(state_obj, link_index)
        if rect and self._automation.click_center(rect):
            final_state, verified = self._wait_for_navigation(state_obj.hwnd, before_title)
            if verified:
                current_state = final_state or state_obj
                self._record_browser_state(current_state, action="click_link")
                logger.info("Clicked browser element %d using heuristic click fallback", link_index)
                return self._success(
                    "click_link",
                    current_state,
                    f"Clicked link {link_index} in {self._display_name(current_state.browser_id)}.",
                    verified=True,
                    data={"index": link_index, "method": "heuristic"},
                )

        return self._failure(
            "click_link",
            f"Link click {link_index} could not be verified in the active browser.",
            "click_not_verified",
            data={"index": link_index},
        )

    def go_back(self, *, browser_name: str | None = None) -> BrowserOperationResult:
        return self._navigation_action("go_back", ["alt", "left"], browser_name=browser_name)

    def go_forward(self, *, browser_name: str | None = None) -> BrowserOperationResult:
        return self._navigation_action("go_forward", ["alt", "right"], browser_name=browser_name)

    def refresh(self, *, browser_name: str | None = None) -> BrowserOperationResult:
        return self._navigation_action("refresh", ["f5"], browser_name=browser_name)

    def go_home(self, *, browser_name: str | None = None) -> BrowserOperationResult:
        return self._navigation_action("home", ["alt", "home"], browser_name=browser_name)

    def new_tab(self, *, browser_name: str | None = None) -> BrowserOperationResult:
        return self._navigation_action("new_tab", ["ctrl", "t"], browser_name=browser_name)

    def close_tab(self, *, browser_name: str | None = None) -> BrowserOperationResult:
        return self._navigation_action("close_tab", ["ctrl", "w"], browser_name=browser_name)

    def next_tab(self, *, browser_name: str | None = None) -> BrowserOperationResult:
        return self._navigation_action("next_tab", ["ctrl", "tab"], browser_name=browser_name)

    def previous_tab(self, *, browser_name: str | None = None) -> BrowserOperationResult:
        return self._navigation_action("previous_tab", ["ctrl", "shift", "tab"], browser_name=browser_name)

    def switch_to_tab(self, tab_index: int, *, browser_name: str | None = None) -> BrowserOperationResult:
        index = max(1, min(int(tab_index or 1), 9))
        return self._navigation_action("switch_tab", ["ctrl", str(9 if index >= 9 else index)], browser_name=browser_name)

    def copy_page_url(self, *, browser_name: str | None = None) -> BrowserOperationResult:
        focus_result = self.focus_browser(browser_name, launch_if_missing=False)
        if not focus_result.success or not focus_result.state:
            return self._failure("copy_page_url", focus_result.message, focus_result.error or "browser_not_ready")

        if not self._automation.hotkey(["ctrl", "l"]):
            return self._failure("copy_page_url", "Could not focus the page URL.", "address_bar_focus_failed")
        self._automation.safe_sleep()
        if not self._automation.hotkey(["ctrl", "c"]):
            return self._failure("copy_page_url", "Could not copy the page URL.", "clipboard_copy_failed")

        clipboard_value = ""
        def _clipboard_ready() -> bool:
            nonlocal clipboard_value
            clipboard_value = str(self._automation.get_clipboard_text() or "").strip()
            return bool(clipboard_value)

        if not self._automation.wait_for(_clipboard_ready, timeout=1.0, interval=0.05):
            return self._failure("copy_page_url", "The page URL was not available to copy.", "clipboard_copy_failed")

        state_obj = self._refresh_state(focus_result.state.hwnd) or focus_result.state
        self._record_browser_state(state_obj, action="copy_page_url")
        return self._success(
            "copy_page_url",
            state_obj,
            "Copied the current page URL.",
            verified=True,
            data={"url": clipboard_value},
        )

    def press_key(
        self,
        key: str,
        *,
        browser_name: str | None = None,
        action: str = "press_key",
    ) -> BrowserOperationResult:
        focus_result = self.focus_browser(browser_name, launch_if_missing=False)
        if not focus_result.success or not focus_result.state:
            return self._failure(action, focus_result.message, focus_result.error or "browser_not_ready")

        if not self._automation.press_key(key):
            return self._failure(action, f"Could not send key '{key}' to the active browser.", "key_send_failed")

        self._automation.safe_sleep()
        state_obj = self._refresh_state(focus_result.state.hwnd) or focus_result.state
        self._record_browser_state(state_obj, action=action)
        logger.info("Browser key executed: %s", key)
        return self._success(
            action,
            state_obj,
            f"Sent key '{key}' to {self._display_name(state_obj.browser_id)}.",
            verified=True,
            data={"key": key},
        )

    def hotkey(
        self,
        keys: Iterable[str],
        *,
        browser_name: str | None = None,
        action: str = "hotkey",
    ) -> BrowserOperationResult:
        key_list = [str(key or "").strip() for key in keys if str(key or "").strip()]
        if not key_list:
            return self._failure(action, "No hotkey keys were provided.", "empty_hotkey")

        focus_result = self.focus_browser(browser_name, launch_if_missing=False)
        if not focus_result.success or not focus_result.state:
            return self._failure(action, focus_result.message, focus_result.error or "browser_not_ready")

        if not self._automation.hotkey(key_list):
            return self._failure(action, f"Could not send hotkey '{'+'.join(key_list)}' to the active browser.", "key_send_failed")

        self._automation.safe_sleep()
        state_obj = self._refresh_state(focus_result.state.hwnd) or focus_result.state
        self._record_browser_state(state_obj, action=action)
        logger.info("Browser hotkey executed: %s", "+".join(key_list))
        return self._success(
            action,
            state_obj,
            f"Sent hotkey '{'+'.join(key_list)}' to {self._display_name(state_obj.browser_id)}.",
            verified=True,
            data={"keys": key_list},
        )

    def type_text(
        self,
        text: str,
        *,
        browser_name: str | None = None,
        clear: bool = False,
        submit: bool = False,
        action: str = "type_text",
    ) -> BrowserOperationResult:
        cleaned = str(text or "")
        if not cleaned:
            return self._failure(action, "No text provided for browser input.", "empty_input")

        focus_result = self.focus_browser(browser_name, launch_if_missing=False)
        if not focus_result.success or not focus_result.state:
            return self._failure(action, focus_result.message, focus_result.error or "browser_not_ready")

        state_obj = focus_result.state
        if not self._automation.type_text(cleaned, clear=clear):
            return self._failure(action, "Typing into the browser was interrupted.", "typing_failed")

        if submit:
            self._automation.safe_sleep()
            if not self._automation.press_key("enter"):
                return self._failure(action, "Enter key submission failed.", "submit_failed")

        refreshed = self._refresh_state(state_obj.hwnd) or state_obj
        self._record_browser_state(refreshed, action=action)
        logger.info("Browser text input executed: %s", action)
        return self._success(
            action,
            refreshed,
            "Submitted browser input." if submit else "Typed text in browser.",
            verified=True,
            data={"text": cleaned, "submit": submit, "cleared": clear},
        )

    def click_rect(
        self,
        rect: tuple[int, int, int, int],
        *,
        browser_name: str | None = None,
        action: str = "click_rect",
    ) -> BrowserOperationResult:
        focus_result = self.focus_browser(browser_name, launch_if_missing=False)
        if not focus_result.success or not focus_result.state:
            return self._failure(action, focus_result.message, focus_result.error or "browser_not_ready")

        if not self._automation.click_center(rect):
            return self._failure(action, "Could not click the requested browser region.", "click_failed")

        self._automation.safe_sleep()
        state_obj = self._refresh_state(focus_result.state.hwnd) or focus_result.state
        self._record_browser_state(state_obj, action=action)
        logger.info("Browser rect click executed: %s", rect)
        return self._success(
            action,
            state_obj,
            f"Clicked a browser region in {self._display_name(state_obj.browser_id)}.",
            verified=False,
            data={"rect": rect},
        )

    def is_browser_ready(self, browser_name: str | None = None) -> bool:
        requested = self._normalize_browser_name(browser_name)
        active = self.detect_active_browser()
        if active and (not requested or active.browser_id == requested):
            return active.is_ready
        target = self._find_browser_window(requested)
        return bool(target)

    def is_browser_installed(self, browser_name: str | None = None) -> bool:
        requested = self._normalize_browser_name(browser_name) or self._preferred_browser()
        spec = _BROWSER_SPECS.get(requested)
        if spec is None:
            return False
        return bool(self._resolve_browser_executable(spec))

    def _scroll(self, *, direction: str, amount: int, browser_name: str | None = None) -> BrowserOperationResult:
        focus_result = self.focus_browser(browser_name, launch_if_missing=False)
        if not focus_result.success or not focus_result.state:
            return self._failure("scroll", focus_result.message, focus_result.error or "browser_not_ready")

        repeats = max(1, int(round(abs(int(amount or 500)) / 500.0)))
        key_name = "page down" if direction == "down" else "page up"
        success = True
        for _ in range(repeats):
            if not self._automation.press_key(key_name):
                wheel_amount = -120 if direction == "down" else 120
                success = self._automation.scroll(wheel_amount)
            if not success:
                break
            self._automation.safe_sleep(50)

        if not success:
            return self._failure("scroll", f"Could not scroll {direction} in the active browser.", "scroll_failed")

        state_obj = self._refresh_state(focus_result.state.hwnd) or focus_result.state
        self._record_browser_state(state_obj, action=f"scroll_{direction}")
        logger.info("Scroll action: %s", direction)
        return self._success(
            "scroll",
            state_obj,
            f"Scrolling page {direction}.",
            verified=True,
            data={"direction": direction, "amount": amount},
        )

    def _navigation_action(
        self,
        action: str,
        keys: list[str],
        *,
        browser_name: str | None = None,
    ) -> BrowserOperationResult:
        focus_result = self.focus_browser(browser_name, launch_if_missing=False)
        if not focus_result.success or not focus_result.state:
            return self._failure(action, focus_result.message, focus_result.error or "browser_not_ready")

        if len(keys) == 1:
            sent = self._automation.press_key(keys[0])
        else:
            sent = self._automation.hotkey(keys)
        if not sent:
            return self._failure(action, f"Could not execute browser action: {action}.", "key_send_failed")

        self._automation.safe_sleep()
        state_obj = self._refresh_state(focus_result.state.hwnd) or focus_result.state
        self._record_browser_state(state_obj, action=action)
        logger.info("Browser action executed: %s", action)
        return self._success(action, state_obj, self._action_message(action, state_obj.browser_id), verified=True)

    def _launch_browser(self, browser_name: str) -> BrowserOperationResult:
        requested = self._normalize_browser_name(browser_name) or self._preferred_browser()
        spec = _BROWSER_SPECS.get(requested)
        if spec is None:
            return self._failure("launch_browser", f"Unsupported browser: {browser_name}.", "unsupported_browser")

        logger.info("Launching browser: %s", requested)
        errors: list[str] = []

        launcher = self._get_launcher()
        if launcher is not None:
            for alias in spec.launch_aliases:
                try:
                    launch_result = launcher.launch_by_name(alias)
                except Exception as exc:  # pragma: no cover - defensive integration
                    errors.append(str(exc))
                    continue

                if launch_result.success:
                    self._automation.safe_sleep(700)
                    return self._success("launch_browser", browser_id=requested, message=f"Launching {spec.display_name}.", verified=True)
                if launch_result.message:
                    errors.append(launch_result.message)

        executable = self._resolve_browser_executable(spec)
        if executable:
            try:
                subprocess.Popen([executable])
                self._automation.safe_sleep(700)
                return self._success("launch_browser", browser_id=requested, message=f"Launching {spec.display_name}.", verified=True)
            except Exception as exc:
                errors.append(str(exc))

        message = f"Could not launch {spec.display_name}."
        error_code = "browser_launch_failed"
        if not executable:
            message = f"I couldn't find {spec.display_name} installed."
            error_code = "browser_not_installed"
        if errors:
            message = f"{message} {' | '.join(errors[:2])}"
        logger.error("Browser launch failed: %s", message)
        return self._failure("launch_browser", message, error_code, browser_id=requested, data={"attempts": errors})

    def _find_browser_window(self, browser_name: str | None = None) -> WindowTarget | None:
        requested = self._normalize_browser_name(browser_name)
        browser_ids = [requested] if requested else self._browser_search_order()

        for browser_id in browser_ids:
            spec = _BROWSER_SPECS.get(browser_id)
            if spec is None:
                continue
            windows = self._automation.list_windows(process_names=spec.process_names)
            if windows:
                return windows[0]
        return None

    def _wait_for_browser_window(self, browser_name: str, timeout: float | None = None) -> WindowTarget | None:
        deadline = time.monotonic() + float(timeout or settings.get("browser_focus_timeout") or 5.0)
        while time.monotonic() <= deadline:
            target = self._find_browser_window(browser_name)
            if target is not None:
                return target
            time.sleep(0.1)
        return None

    def _refresh_state(self, hwnd: int) -> BrowserState | None:
        window = self._automation.get_window(hwnd)
        if window:
            return self._state_from_target(window)

        try:
            info = self._detector.get_window_info(hwnd)
        except Exception:
            return None
        if not info or not self._browser_id_from_process(info.process_name):
            return None
        return self._state_from_window_info(info)

    def _wait_for_navigation(
        self,
        hwnd: int,
        before_title: str,
        timeout: float | None = None,
    ) -> tuple[BrowserState | None, bool]:
        deadline = time.monotonic() + float(timeout or 5.0)
        last_state = self._refresh_state(hwnd)
        while time.monotonic() <= deadline:
            current_state = self._refresh_state(hwnd) or last_state
            if current_state and current_state.title and current_state.title != before_title:
                return current_state, True
            time.sleep(0.1)
        return self._refresh_state(hwnd) or last_state, False

    def _click_link_uia(self, browser_state: BrowserState, index: int) -> bool:
        if not _PYWINAUTO_OK or Desktop is None:
            return False

        try:
            window = Desktop(backend="uia").window(handle=browser_state.hwnd)
            candidates: list[tuple[int, int, str, Any]] = []
            top_limit = browser_state.rect[1] + max(90, int(browser_state.height * 0.15))

            for control in window.descendants():
                try:
                    element_info = control.element_info
                    name = str(getattr(element_info, "name", "") or "").strip()
                    control_type = str(getattr(element_info, "control_type", "") or "").lower()
                    rect = getattr(element_info, "rectangle", None)
                    if control_type not in {"hyperlink", "button"} or not name or rect is None:
                        continue
                    if rect.top < top_limit:
                        continue
                    if rect.right <= rect.left or rect.bottom <= rect.top:
                        continue
                    candidates.append((rect.top, rect.left, name.lower(), control))
                except Exception:
                    continue

            deduped: list[tuple[int, int, str, Any]] = []
            seen: set[tuple[int, int, str]] = set()
            for item in sorted(candidates):
                key = (item[0], item[1], item[2])
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(item)

            if 0 < index <= len(deduped):
                wrapper = deduped[index - 1][3].wrapper_object()
                wrapper.click_input()
                return True
        except Exception as exc:  # pragma: no cover - depends on live UIA tree
            logger.debug("UIA click fallback failed: %s", exc)
        return False

    def _click_link_by_tab(self, index: int) -> bool:
        tab_count = 6 + max(0, index - 1) * 2
        try:
            self._automation.press_key("esc")
            self._automation.safe_sleep(40)
            self._automation.press_key("home")
            self._automation.safe_sleep(60)
            for _ in range(tab_count):
                if not self._automation.press_key("tab"):
                    return False
                self._automation.safe_sleep(30)
            return self._automation.press_key("enter")
        except Exception:
            return False

    def _focus_address_bar_uia(self, browser_state: BrowserState) -> bool:
        if not _PYWINAUTO_OK or Desktop is None:
            return False

        try:
            window = Desktop(backend="uia").window(handle=browser_state.hwnd)
            top_limit = browser_state.rect[1] + max(90, int(browser_state.height * 0.16))
            title_hints = set(_BROWSER_SPECS[browser_state.browser_id].address_bar_names)

            for control in window.descendants():
                try:
                    element_info = control.element_info
                    name = str(getattr(element_info, "name", "") or "").strip().lower()
                    control_type = str(getattr(element_info, "control_type", "") or "").lower()
                    rect = getattr(element_info, "rectangle", None)
                    if control_type not in {"edit", "combobox"} or rect is None:
                        continue
                    if rect.top > top_limit:
                        continue
                    if name and any(hint in name for hint in title_hints):
                        wrapper = control.wrapper_object()
                        wrapper.click_input()
                        wrapper.set_focus()
                        return True
                except Exception:
                    continue
        except Exception as exc:  # pragma: no cover - depends on live UIA tree
            logger.debug("UIA address bar fallback failed: %s", exc)
        return False

    def _address_bar_rect(self, browser_state: BrowserState) -> tuple[int, int, int, int]:
        left, top, right, bottom = browser_state.rect
        width = max(0, right - left)
        height = max(0, bottom - top)
        toolbar_height = max(60, int(height * 0.12))
        bar_left = left + int(width * 0.18)
        bar_right = right - int(width * 0.12)
        bar_top = top + int(toolbar_height * 0.22)
        bar_bottom = top + int(toolbar_height * 0.78)
        return (bar_left, bar_top, bar_right, bar_bottom)

    def _result_click_rect(self, browser_state: BrowserState, index: int) -> tuple[int, int, int, int]:
        left, top, right, bottom = browser_state.rect
        width = max(0, right - left)
        height = max(0, bottom - top)
        content_top = top + max(130, int(height * 0.22))
        row_gap = max(58, int(height * 0.085))
        y_top = content_top + max(0, index - 1) * row_gap
        y_bottom = y_top + max(36, int(height * 0.05))
        return (
            left + int(width * 0.18),
            y_top,
            left + int(width * 0.78),
            y_bottom,
        )

    def _safe_active_context(self) -> WindowInfo | None:
        try:
            return self._detector.get_active_context()
        except Exception as exc:  # pragma: no cover - defensive OS access
            logger.debug("Active browser detection failed: %s", exc)
            return None

    def _state_from_window_info(self, info: WindowInfo) -> BrowserState:
        browser_id = self._browser_id_from_process(info.process_name) or "browser"
        site_context = info.app_id if info.app_id not in {"", "unknown", browser_id} else ""
        return BrowserState(
            browser_id=browser_id,
            hwnd=info.hwnd,
            process_name=info.process_name,
            title=info.title,
            rect=info.rect,
            is_foreground=self._automation.get_foreground_window() == info.hwnd,
            is_minimized=info.is_minimized,
            is_ready=bool(info.hwnd and info.process_name),
            site_context=site_context,
        )

    def _state_from_target(self, target: WindowTarget) -> BrowserState:
        browser_id = self._browser_id_from_process(target.process_name) or "browser"
        return BrowserState(
            browser_id=browser_id,
            hwnd=target.hwnd,
            process_name=target.process_name,
            title=target.title,
            rect=target.rect,
            is_foreground=self._automation.get_foreground_window() == target.hwnd,
            is_minimized=target.is_minimized,
            is_ready=bool(target.hwnd and target.process_name),
        )

    def _record_browser_state(self, browser_state: BrowserState | None, *, action: str) -> None:
        if browser_state is None:
            return
        resolved_context = str(browser_state.site_context or browser_state.browser_id).strip() or browser_state.browser_id
        state.last_browser = browser_state.browser_id
        state.last_browser_action = action
        state.browser_ready = browser_state.is_ready
        state.current_app = resolved_context
        state.current_context = resolved_context
        state.current_process_name = browser_state.process_name
        state.current_window_title = browser_state.title
        if action in {"search", "open_url", "open_browser", "go_back", "go_forward", "refresh", "home", "new_tab", "close_tab", "switch_tab", "click_link", "copy_page_url"}:
            state.last_navigation_time = time.time()

    def _get_launcher(self):
        if self._launcher is not None:
            return self._launcher

        try:
            from core.launcher import AppLauncher

            self._launcher = AppLauncher()
        except Exception as exc:  # pragma: no cover - depends on launcher index state
            logger.warning("AppLauncher unavailable for browser launch: %s", exc)
            self._launcher = False
        return self._launcher if self._launcher is not False else None

    def _resolve_browser_executable(self, spec: BrowserSpec) -> str:
        for process_name in spec.process_names:
            stem = process_name.removesuffix(".exe")
            which_match = shutil.which(stem)
            if which_match:
                return which_match

            registry_path = self._read_app_path_from_registry(process_name)
            if registry_path:
                return registry_path

        for candidate in spec.known_paths:
            expanded = os.path.expandvars(candidate)
            if os.path.exists(expanded):
                return expanded
        return ""

    def _read_app_path_from_registry(self, process_name: str) -> str:
        if winreg is None:
            return ""

        subkey = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{process_name}"
        hives = [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]
        for hive in hives:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    value, _ = winreg.QueryValueEx(key, "")
                    if value and os.path.exists(value):
                        return value
            except OSError:
                continue
        return ""

    def _browser_search_order(self) -> list[str]:
        preferred = self._preferred_browser()
        ordered = [preferred] if preferred else []
        last_browser = self._normalize_browser_name(getattr(state, "last_browser", ""))
        if last_browser and last_browser not in ordered:
            ordered.append(last_browser)
        for browser_id in _BROWSER_ORDER:
            if browser_id not in ordered:
                ordered.append(browser_id)
        return ordered

    def _preferred_browser(self) -> str:
        return self._normalize_browser_name(settings.get("preferred_browser")) or "chrome"

    def _normalize_browser_name(self, browser_name: Any) -> str:
        value = str(browser_name or "").strip().lower()
        if value in _BROWSER_SPECS:
            return value
        return _BROWSER_ALIASES.get(value, value if value in _BROWSER_SPECS else "")

    @staticmethod
    def _normalize_engine(engine: str | None) -> str:
        return str(engine or "").strip().lower()

    def _browser_id_from_process(self, process_name: str | None) -> str:
        normalized = str(process_name or "").strip().lower()
        for browser_id, spec in _BROWSER_SPECS.items():
            if normalized in spec.process_names:
                return browser_id
        return ""

    def _normalize_url(self, url: str) -> str:
        cleaned = str(url or "").strip()
        if not cleaned:
            return ""
        if "://" in cleaned or cleaned.startswith(("about:", "chrome:", "edge:")):
            return cleaned
        if " " in cleaned:
            return ""
        return f"https://{cleaned}"

    def _state_matches_url(self, browser_state: BrowserState | None, url: str) -> bool:
        if browser_state is None:
            return False

        parsed = urlparse(str(url or ""))
        host = str(parsed.netloc or "").lower()
        title = str(browser_state.title or "").strip().lower()
        site_context = str(browser_state.site_context or "").strip().lower()
        if not host:
            return False

        keywords = {
            token
            for token in re.split(r"[^a-z0-9]+", host)
            if token and token not in {"www", "com", "org", "net", "co", "in"}
        }
        if site_context and site_context in keywords:
            return True
        return any(keyword in title for keyword in keywords)

    def _site_context_from_url(self, url: str) -> str:
        parsed = urlparse(str(url or ""))
        host = str(parsed.netloc or "").lower()
        if not host:
            return ""
        keywords = [
            token
            for token in re.split(r"[^a-z0-9]+", host)
            if token and token not in {"www", "com", "org", "net", "co", "in"}
        ]
        if keywords[:2] == ["mail", "google"]:
            return "gmail"
        return keywords[0] if keywords else ""

    def _display_name(self, browser_id: str) -> str:
        spec = _BROWSER_SPECS.get(self._normalize_browser_name(browser_id))
        return spec.display_name if spec else "browser"

    def _action_message(self, action: str, browser_id: str) -> str:
        readable = {
            "go_back": "Going back",
            "go_forward": "Going forward",
            "refresh": "Refreshing the page",
            "home": "Opening the home page",
            "new_tab": "Opening a new tab",
            "close_tab": "Closing the current tab",
            "next_tab": "Switching to the next tab",
            "previous_tab": "Switching to the previous tab",
            "switch_tab": "Switching tabs",
        }.get(action, action.replace("_", " ").title())
        return f"{readable} in {self._display_name(browser_id)}."

    def _success(
        self,
        action: str,
        state_obj: BrowserState | None = None,
        message: str = "",
        *,
        browser_id: str = "",
        query: str = "",
        url: str = "",
        verified: bool = False,
        data: Optional[dict[str, Any]] = None,
    ) -> BrowserOperationResult:
        resolved_browser = browser_id or (state_obj.browser_id if state_obj else "")
        logger.info("Browser detected: %s", resolved_browser or "unknown")
        return BrowserOperationResult(
            success=True,
            action=action,
            message=message,
            browser_id=resolved_browser,
            query=query,
            url=url,
            verified=verified,
            state=state_obj,
            data=data or {},
        )

    def _failure(
        self,
        action: str,
        message: str,
        error: str,
        *,
        browser_id: str = "",
        query: str = "",
        url: str = "",
        data: Optional[dict[str, Any]] = None,
    ) -> BrowserOperationResult:
        logger.error("Browser action failed: %s | %s", action, error)
        state.browser_ready = False
        return BrowserOperationResult(
            success=False,
            action=action,
            message=message,
            error=error,
            browser_id=browser_id,
            query=query,
            url=url,
            verified=False,
            state=None,
            data=data or {},
        )
