"""
Dedicated Google Chrome skill plugin.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from core import settings, state
from core.browser import BrowserController, BrowserOperationResult, BrowserState
from core.browser_commands import ParsedBrowserCommand, parse_browser_command
from core.logger import get_logger
from core.ocr import OCREngine, get_ocr_engine
from skills.base import SkillBase, SkillExecutionResult

logger = get_logger(__name__)

try:  # pragma: no cover - optional dependency
    from pywinauto import Desktop

    _PYWINAUTO_OK = True
except Exception:  # pragma: no cover - optional dependency
    Desktop = None
    _PYWINAUTO_OK = False


_SEARCH_PREFIXES = ("search for ", "search ", "google ", "find ")
_NEW_TAB_COMMANDS = {"new tab", "open tab", "open new tab"}
_CLOSE_TAB_COMMANDS = {"close tab", "close this tab", "close current tab"}
_BACK_COMMANDS = {"back", "go back"}
_FORWARD_COMMANDS = {"forward", "go forward"}
_REFRESH_COMMANDS = {"refresh", "refresh page", "reload", "reload page"}
_SCROLL_DOWN_COMMANDS = {"scroll down", "page down"}
_SCROLL_UP_COMMANDS = {"scroll up", "page up"}
_READ_TITLE_COMMANDS = {"read page title", "read title", "what is the page title", "page title"}
_READ_RESULTS_PREFIXES = ("read results", "what is on page", "what's on page", "read first result", "read second result", "read third result")
_CHROME_NOISE_PHRASES = {
    "address and search bar",
    "search google or type a url",
    "minimize",
    "maximize",
    "close",
    "new tab",
    "tab search",
    "bookmark this tab",
    "profile",
    "customize and control google chrome",
}
_ORDINALS = {"first": 1, "1": 1, "1st": 1, "second": 2, "2": 2, "2nd": 2, "third": 3, "3": 3, "3rd": 3}
_GENERIC_PAGE_TITLES = {"", "new tab", "google", "chrome", "google chrome", "start page", "home"}


class ChromeSkill(SkillBase):
    """Google Chrome-specific skill built on top of BrowserController."""

    def __init__(
        self,
        *,
        controller: BrowserController | None = None,
        ocr_engine: OCREngine | None = None,
    ) -> None:
        self._controller = controller or BrowserController()
        self._ocr = ocr_engine or get_ocr_engine()

    def can_handle(self, context: Mapping[str, Any], intent: str, command: str) -> bool:
        if not settings.get("chrome_skill_enabled"):
            return False

        normalized = self._normalize_command(command)
        if not normalized:
            return False
        parsed = parse_browser_command(command)

        current_app = str(context.get("current_app") or "").strip().lower()
        current_process = str(context.get("current_process_name") or "").strip().lower()
        decision_target = str(context.get("context_target_app") or "").strip().lower()
        explicit_chrome = self._mentions_chrome(command)
        chrome_context = (
            current_app == "chrome"
            or current_process == "chrome.exe"
            or decision_target == "chrome"
            or str(state.last_browser or "").strip().lower() == "chrome"
        )

        if parsed is not None:
            if parsed.browser not in {"", "browser", "chrome"}:
                return False
            if parsed.action in {"search", "new_tab", "close_tab", "next_tab", "previous_tab", "switch_tab", "go_back", "go_forward", "refresh", "home", "scroll_down", "scroll_up", "read_page", "read_page_title", "copy_page_url"}:
                return explicit_chrome or chrome_context or settings.get("preferred_browser") == "chrome"

        if self._is_tab_command(normalized) or self._is_navigation_command(normalized) or self._is_read_command(normalized):
            return explicit_chrome or chrome_context or settings.get("preferred_browser") == "chrome"

        if self._is_search_command(normalized, intent):
            return explicit_chrome or chrome_context or settings.get("preferred_browser") == "chrome"

        return False

    def execute(self, command: str, context: Mapping[str, Any]) -> SkillExecutionResult:
        normalized = self._normalize_command(command)
        cleaned_command = self._strip_explicit_chrome_tokens(command)
        action, payload = self._classify_command(command, cleaned_command, normalized, context)

        logger.info("Action: %s", action)
        if payload:
            logger.info("Command payload: %s", payload)

        if action == "search":
            query = str(payload.get("query") or "").strip()
            engine = str(payload.get("engine") or "").strip() or None
            return self.handle_search(query, engine=engine)
        if action == "new_tab":
            return self.open_new_tab()
        if action == "close_tab":
            return self.close_tab()
        if action == "next_tab":
            return self.next_tab()
        if action == "previous_tab":
            return self.previous_tab()
        if action == "go_back":
            return self.navigate_back()
        if action == "go_forward":
            return self.navigate_forward()
        if action == "refresh":
            return self.refresh_page()
        if action == "home":
            return self.go_home()
        if action == "scroll_down":
            return self.scroll_page("down")
        if action == "scroll_up":
            return self.scroll_page("up")
        if action == "switch_tab":
            return self.switch_tab(int(payload.get("tab_index") or 0))
        if action == "read_page_title":
            return self.read_page_title()
        if action == "read_page":
            return self.read_page()
        if action == "copy_page_url":
            return self.copy_page_url()
        if action == "read_results":
            return self.read_results(result_index=payload.get("result_index"))

        return SkillExecutionResult(
            success=False,
            intent="chrome_action",
            response=f"ChromeSkill cannot handle: {command}",
            skill_name=self.name(),
            error="unsupported_chrome_command",
        )

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "browser": "chrome",
            "supports": [
                "search_web",
                "open_new_tab",
                "close_tab",
                "navigate_back",
                "navigate_forward",
                "refresh_page",
                "scroll_up",
                "scroll_down",
                "read_page_title",
                "read_visible_results",
            ],
            "read_results_mode": settings.get("read_results_mode"),
        }

    def health_check(self) -> dict[str, Any]:
        installed = self._controller.is_browser_installed("chrome")
        running = self._controller.is_browser_ready("chrome")
        return {
            "enabled": bool(settings.get("chrome_skill_enabled")),
            "chrome_installed": installed,
            "chrome_running": running,
            "auto_launch": bool(settings.get("auto_launch_chrome_if_needed")),
            "read_results_mode": settings.get("read_results_mode"),
            "ocr_enabled": bool(settings.get("ocr_enabled")),
        }

    def describe_window_title(self, title: str, *, ocr_text: str = "") -> dict[str, Any]:
        cleaned = self._extract_page_title(title)
        cleaned_lower = cleaned.lower()
        if cleaned and cleaned_lower not in _GENERIC_PAGE_TITLES:
            return {
                "summary": f"Chrome has {cleaned} open.",
                "confidence": 0.88,
                "details": {"page_title": cleaned, "source": "window_title"},
            }

        ocr_line = self._first_meaningful_ocr_line(ocr_text)
        if ocr_line:
            return {
                "summary": f'Chrome is open and shows "{ocr_line}".',
                "confidence": 0.66,
                "details": {"page_title": cleaned, "source": "ocr", "ocr_snippet": ocr_line},
            }

        return {
            "summary": "Chrome is active.",
            "confidence": 0.60,
            "details": {"page_title": cleaned, "source": "window_title"},
        }

    def handle_search(self, query: str, *, engine: str | None = None) -> SkillExecutionResult:
        if not query:
            return self._failure("Search query is empty.", "empty_query")

        logger.info("Query: %s", query)
        ensure = self.ensure_chrome_open()
        if ensure is not None:
            return ensure

        result = self._controller.search(query, browser_name="chrome", engine=engine)
        return self._from_browser_result(result, action="search", response=f"Searching {query} in Chrome")

    def open_new_tab(self) -> SkillExecutionResult:
        ensure = self.ensure_chrome_open()
        if ensure is not None:
            return ensure

        result = self._controller.new_tab(browser_name="chrome")
        if result.success:
            state.chrome_tabs_opened_count = int(getattr(state, "chrome_tabs_opened_count", 0) or 0) + 1
        return self._from_browser_result(result, action="new_tab", response="Opened a new tab")

    def close_tab(self) -> SkillExecutionResult:
        ensure = self.ensure_chrome_open()
        if ensure is not None:
            return ensure

        result = self._controller.close_tab(browser_name="chrome")
        return self._from_browser_result(result, action="close_tab", response="Closed the current tab")

    def next_tab(self) -> SkillExecutionResult:
        ensure = self.ensure_chrome_open()
        if ensure is not None:
            return ensure

        result = self._controller.next_tab(browser_name="chrome")
        return self._from_browser_result(result, action="next_tab", response="Switching to the next tab in Chrome")

    def previous_tab(self) -> SkillExecutionResult:
        ensure = self.ensure_chrome_open()
        if ensure is not None:
            return ensure

        result = self._controller.previous_tab(browser_name="chrome")
        return self._from_browser_result(result, action="previous_tab", response="Switching to the previous tab in Chrome")

    def navigate_back(self) -> SkillExecutionResult:
        ensure = self.ensure_chrome_open()
        if ensure is not None:
            return ensure

        result = self._controller.go_back(browser_name="chrome")
        return self._from_browser_result(result, action="go_back", response="Going back in Chrome")

    def navigate_forward(self) -> SkillExecutionResult:
        ensure = self.ensure_chrome_open()
        if ensure is not None:
            return ensure

        result = self._controller.go_forward(browser_name="chrome")
        return self._from_browser_result(result, action="go_forward", response="Going forward in Chrome")

    def go_home(self) -> SkillExecutionResult:
        ensure = self.ensure_chrome_open()
        if ensure is not None:
            return ensure

        result = self._controller.go_home(browser_name="chrome")
        return self._from_browser_result(result, action="home", response="Opening the home page in Chrome")

    def refresh_page(self) -> SkillExecutionResult:
        ensure = self.ensure_chrome_open()
        if ensure is not None:
            return ensure

        result = self._controller.refresh(browser_name="chrome")
        return self._from_browser_result(result, action="refresh", response="Refreshing the page")

    def scroll_page(self, direction: str) -> SkillExecutionResult:
        ensure = self.ensure_chrome_open()
        if ensure is not None:
            return ensure

        if direction == "down":
            result = self._controller.scroll_down(browser_name="chrome")
            response = "Scrolling page down"
        else:
            result = self._controller.scroll_up(browser_name="chrome")
            response = "Scrolling page up"
        return self._from_browser_result(result, action=f"scroll_{direction}", response=response)

    def switch_tab(self, tab_index: int) -> SkillExecutionResult:
        index = int(tab_index or 0)
        if index <= 0:
            return self._failure("Tab number is missing.", "tab_index_missing")

        ensure = self.ensure_chrome_open()
        if ensure is not None:
            return ensure

        result = self._controller.switch_to_tab(index, browser_name="chrome")
        return self._from_browser_result(result, action="switch_tab", response=f"Switching to tab {index} in Chrome")

    def copy_page_url(self) -> SkillExecutionResult:
        ensure = self.ensure_chrome_open()
        if ensure is not None:
            return ensure

        result = self._controller.copy_page_url(browser_name="chrome")
        response = "Copied the current page URL."
        if result.success and result.data.get("url"):
            state.last_page_title = state.last_page_title or self._extract_page_title(result.state.title if result.state else "")
        return self._from_browser_result(result, action="copy_page_url", response=response)

    def read_page_title(self) -> SkillExecutionResult:
        browser_state = self.focus_chrome()
        if isinstance(browser_state, SkillExecutionResult):
            return browser_state

        title = self._extract_page_title(browser_state.title)
        if not title:
            return self._failure("Chrome is focused, but the page title is unavailable.", "page_title_unavailable")

        state.last_page_title = title
        state.last_chrome_action = "read_page_title"
        logger.info("Read results source used: window_title")
        return SkillExecutionResult(
            success=True,
            intent="chrome_read",
            response=f"Current Chrome page title: {title}",
            skill_name=self.name(),
            data={"source": "window_title", "page_title": title},
        )

    def read_page(self) -> SkillExecutionResult:
        return self.read_results()

    def read_results(self, *, result_index: int | None = None) -> SkillExecutionResult:
        browser_state = self.focus_chrome()
        if isinstance(browser_state, SkillExecutionResult):
            return browser_state

        title = self._extract_page_title(browser_state.title)
        state.last_page_title = title or state.last_page_title

        text_items, source = self._read_visible_content(browser_state)
        logger.info("Read results source used: %s", source)
        state.last_chrome_action = "read_results"

        if text_items:
            if result_index:
                if result_index > len(text_items):
                    return self._failure(
                        f"Only {len(text_items)} visible content item(s) were detected in Chrome.",
                        "result_index_out_of_range",
                    )
                item = text_items[result_index - 1]
                return SkillExecutionResult(
                    success=True,
                    intent="chrome_read",
                    response=f"Visible result {result_index}: {item}",
                    skill_name=self.name(),
                    data={"source": source, "result_index": result_index, "page_title": title, "items": text_items},
                )

            joined = " | ".join(text_items[:3])
            return SkillExecutionResult(
                success=True,
                intent="chrome_read",
                response=f"Visible Chrome content: {joined}",
                skill_name=self.name(),
                data={"source": source, "page_title": title, "items": text_items},
            )

        if title:
            return SkillExecutionResult(
                success=True,
                intent="chrome_read",
                response=f"Current page title: {title}. No accessible visible result text was detected.",
                skill_name=self.name(),
                data={"source": "window_title", "page_title": title, "items": []},
            )

        return self._failure("Chrome is open, but no readable page content was detected.", "read_results_unavailable")

    def focus_chrome(self) -> BrowserState | SkillExecutionResult:
        launch_if_missing = bool(settings.get("auto_launch_chrome_if_needed"))
        result = self._controller.focus_browser("chrome", launch_if_missing=launch_if_missing)
        if result.success and result.state:
            state.last_page_title = self._extract_page_title(result.state.title)
            state.last_chrome_action = "focus_chrome"
            return result.state

        message = result.message
        if not launch_if_missing and result.error == "browser_not_found":
            message = "Chrome is not open."
        return self._failure(message, result.error or "chrome_focus_failed")

    def ensure_chrome_open(self) -> SkillExecutionResult | None:
        focused = self.focus_chrome()
        if isinstance(focused, SkillExecutionResult):
            return focused
        return None

    def _classify_command(
        self,
        original_command: str,
        cleaned_command: str,
        normalized: str,
        context: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        intent = str(context.get("intent") or "").strip().lower()
        parsed = parse_browser_command(original_command)
        if parsed is not None:
            if parsed.action == "search":
                query, engine = self._extract_search_payload(cleaned_command)
                return "search", {"query": query, "engine": engine}
            mapped = self._map_parsed_action(parsed)
            if mapped is not None:
                return mapped

        if self._is_search_command(normalized, intent):
            query, engine = self._extract_search_payload(cleaned_command)
            return "search", {"query": query, "engine": engine}

        if normalized in _NEW_TAB_COMMANDS:
            return "new_tab", {}
        if normalized in _CLOSE_TAB_COMMANDS:
            return "close_tab", {}
        if normalized in _BACK_COMMANDS:
            return "go_back", {}
        if normalized in _FORWARD_COMMANDS:
            return "go_forward", {}
        if normalized in _REFRESH_COMMANDS:
            return "refresh", {}
        if normalized in _SCROLL_DOWN_COMMANDS or normalized.startswith("scroll down"):
            return "scroll_down", {}
        if normalized in _SCROLL_UP_COMMANDS or normalized.startswith("scroll up"):
            return "scroll_up", {}
        if normalized in _READ_TITLE_COMMANDS:
            return "read_page_title", {}
        if any(normalized.startswith(prefix) for prefix in _READ_RESULTS_PREFIXES):
            return "read_results", {"result_index": self._extract_ordinal(normalized)}

        return "unsupported", {}

    @staticmethod
    def _map_parsed_action(parsed: ParsedBrowserCommand) -> tuple[str, dict[str, Any]] | None:
        action_map = {
            "search": ("search", {"query": parsed.query, "engine": None}),
            "new_tab": ("new_tab", {}),
            "close_tab": ("close_tab", {}),
            "next_tab": ("next_tab", {}),
            "previous_tab": ("previous_tab", {}),
            "go_back": ("go_back", {}),
            "go_forward": ("go_forward", {}),
            "refresh": ("refresh", {}),
            "home": ("home", {}),
            "scroll_down": ("scroll_down", {}),
            "scroll_up": ("scroll_up", {}),
            "switch_tab": ("switch_tab", {"tab_index": parsed.tab_index}),
            "read_page_title": ("read_page_title", {}),
            "read_page": ("read_page", {}),
            "copy_page_url": ("copy_page_url", {}),
        }
        return action_map.get(parsed.action)

    def _read_visible_content(self, browser_state: BrowserState) -> tuple[list[str], str]:
        mode = str(settings.get("read_results_mode") or "best_available").strip().lower()

        if mode in {"best_available", "uia"}:
            items = self._read_visible_text_uia(browser_state)
            if items:
                return items, "uia"

        if mode in {"best_available", "ocr"}:
            ocr_items = self._read_visible_text_ocr(browser_state)
            if ocr_items:
                return ocr_items, "ocr"

        title = self._extract_page_title(browser_state.title)
        if title:
            return [title], "window_title"

        return [], "none"

    def _read_visible_text_uia(self, browser_state: BrowserState) -> list[str]:
        if not _PYWINAUTO_OK or Desktop is None:
            return []

        try:
            window = Desktop(backend="uia").window(handle=browser_state.hwnd)
            top_limit = browser_state.rect[1] + max(110, int(browser_state.height * 0.18))
            bottom_limit = browser_state.rect[3] - 30
            collected: list[tuple[int, int, str]] = []

            for control in window.descendants():
                try:
                    wrapper = control.wrapper_object()
                    if hasattr(wrapper, "is_visible") and not wrapper.is_visible():
                        continue
                    element = control.element_info
                    name = self._sanitize_visible_text(getattr(element, "name", "") or "")
                    control_type = str(getattr(element, "control_type", "") or "").lower()
                    rect = getattr(element, "rectangle", None)
                    if rect is None or not name:
                        continue
                    if rect.top < top_limit or rect.bottom > bottom_limit:
                        continue
                    if control_type not in {"text", "hyperlink", "document", "group", "heading", "listitem", "pane", "button"}:
                        continue
                    if name.lower() in _CHROME_NOISE_PHRASES:
                        continue
                    collected.append((rect.top, rect.left, name))
                except Exception:
                    continue

            deduped: list[str] = []
            seen: set[str] = set()
            for _, _, item in sorted(collected):
                lowered = item.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                deduped.append(item)
            return deduped[:10]
        except Exception as exc:  # pragma: no cover - depends on live UIA tree
            logger.debug("Chrome UIA read failed: %s", exc)
            return []

    def _read_visible_text_ocr(self, browser_state: BrowserState) -> list[str]:
        if not settings.get("ocr_enabled"):
            return []

        left, top, width, height = self._ocr_content_region(browser_state)
        if width <= 0 or height <= 0:
            return []

        result = self._ocr.read_region(left, top, width, height)
        if not result.lines:
            return []

        collected: list[str] = []
        seen: set[str] = set()
        for line in result.lines:
            text = self._sanitize_visible_text(line.text)
            lowered = text.lower()
            if not text or lowered in seen or lowered in _CHROME_NOISE_PHRASES:
                continue
            seen.add(lowered)
            collected.append(text)
        return collected[:10]

    @staticmethod
    def _first_meaningful_ocr_line(text: str) -> str:
        for raw_line in str(text or "").splitlines():
            line = " ".join(raw_line.split()).strip()
            if len(line) < 4:
                continue
            if line.lower() in _CHROME_NOISE_PHRASES:
                continue
            return line[:120]
        return ""

    @staticmethod
    def _ocr_content_region(browser_state: BrowserState) -> tuple[int, int, int, int]:
        left, top, right, bottom = browser_state.rect
        width = max(0, right - left)
        height = max(0, bottom - top)
        content_top = top + max(110, int(height * 0.16))
        content_bottom = bottom - max(30, int(height * 0.05))
        content_left = left + int(width * 0.08)
        content_right = right - int(width * 0.06)
        return (
            content_left,
            content_top,
            max(1, content_right - content_left),
            max(1, content_bottom - content_top),
        )

    def _extract_search_payload(self, command: str) -> tuple[str, str | None]:
        cleaned = self._strip_explicit_chrome_tokens(command)
        for prefix in ("search for ", "search ", "find "):
            if cleaned.lower().startswith(prefix):
                return cleaned[len(prefix) :].strip(), None
        if cleaned.lower().startswith("google "):
            return cleaned[len("google ") :].strip(), "google"
        return cleaned.strip(), None

    def _from_browser_result(
        self,
        result: BrowserOperationResult,
        *,
        action: str,
        response: str,
    ) -> SkillExecutionResult:
        if result.state:
            state.last_page_title = self._extract_page_title(result.state.title)
        state.last_chrome_action = action
        payload = {
            "action": action,
            "target_app": "chrome",
            "browser_result": result.data,
            "verified": result.verified,
            "page_title": state.last_page_title,
        }
        if result.url:
            payload["url"] = result.url
        if result.data:
            payload.update(dict(result.data))
        return SkillExecutionResult(
            success=result.success,
            intent="chrome_action",
            response=response if result.success else result.message,
            skill_name=self.name(),
            error=result.error,
            data=payload,
        )

    def _failure(self, response: str, error: str) -> SkillExecutionResult:
        return SkillExecutionResult(
            success=False,
            intent="chrome_action",
            response=response,
            skill_name=self.name(),
            error=error,
        )

    @staticmethod
    def _normalize_command(command: str) -> str:
        return " ".join(str(command or "").strip().lower().split())

    def _strip_explicit_chrome_tokens(self, command: str) -> str:
        text = str(command or "").strip()
        if not text:
            return ""

        patterns = (
            r"^\s*chrome[\s:,-]+",
            r"\s+(?:in|on|with|using)\s+chrome\b",
            r"\bchrome\b\s*$",
        )
        for pattern in patterns:
            text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
        return " ".join(text.split())

    @staticmethod
    def _mentions_chrome(command: str) -> bool:
        return bool(re.search(r"\bchrome\b", str(command or ""), flags=re.IGNORECASE))

    def _is_search_command(self, normalized: str, intent: str) -> bool:
        if intent == "search":
            return True
        return any(normalized.startswith(prefix) for prefix in _SEARCH_PREFIXES)

    @staticmethod
    def _is_tab_command(normalized: str) -> bool:
        return normalized in _NEW_TAB_COMMANDS or normalized in _CLOSE_TAB_COMMANDS

    @staticmethod
    def _is_navigation_command(normalized: str) -> bool:
        return (
            normalized in _BACK_COMMANDS
            or normalized in _FORWARD_COMMANDS
            or normalized in _REFRESH_COMMANDS
            or normalized.startswith("scroll down")
            or normalized.startswith("scroll up")
        )

    @staticmethod
    def _is_read_command(normalized: str) -> bool:
        return normalized in _READ_TITLE_COMMANDS or any(normalized.startswith(prefix) for prefix in _READ_RESULTS_PREFIXES)

    @staticmethod
    def _extract_ordinal(normalized: str) -> int | None:
        for token in normalized.split():
            if token in _ORDINALS:
                return _ORDINALS[token]
        return None

    @staticmethod
    def _extract_page_title(title: str) -> str:
        cleaned = str(title or "").strip()
        if not cleaned:
            return ""
        suffixes = (" - Google Chrome", " - Chrome", " - Guest mode - Google Chrome")
        for suffix in suffixes:
            if cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)].strip()
                break
        return cleaned

    @staticmethod
    def _sanitize_visible_text(value: str) -> str:
        text = " ".join(str(value or "").split())
        text = re.sub(r"https?://\S+", "", text).strip()
        if len(text) < 3:
            return ""
        if text.lower().startswith(("http://", "https://", "www.")):
            return ""
        return text[:220]
