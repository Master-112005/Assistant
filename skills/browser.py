"""
Browser skill.

Wraps the production browser controller with planner-friendly helpers and
execution-friendly result objects used by the context engine and executor.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from core.browser import BrowserController, BrowserOperationResult, SEARCH_ENGINE_URLS
from core.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_SEARCH_ENGINE = "google"


@dataclass
class SearchResult:
    """Result from a browser search attempt."""

    success: bool
    query: str
    engine: str
    browser: str = ""
    url: str = ""
    message: str = ""
    error: str = ""
    launched_at: float = field(default_factory=time.monotonic)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class BrowserActionResult:
    """Result from a browser navigation or active-window action."""

    success: bool
    operation: str
    message: str
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class BrowserSkill:
    """Skill facade for browser automation."""

    def __init__(self, controller: BrowserController | None = None) -> None:
        self._controller = controller or BrowserController()

    def search(
        self,
        query: str,
        engine: Optional[str] = None,
        target_app: Optional[str] = None,
    ) -> SearchResult:
        if not query or not query.strip():
            return SearchResult(
                success=False,
                query=query or "",
                engine="browser",
                error="empty_query",
                message="Cannot search: query is empty.",
            )

        browser_name, engine_name = self._resolve_search_target(engine, target_app)
        controller_result = self._controller.search(
            query.strip(),
            browser_name=browser_name,
            engine=engine_name or None,
        )
        resolved_engine = engine_name or "browser"
        logger.info(
            "BrowserSkill.search: query=%r browser=%s engine=%s",
            query,
            controller_result.browser_id or browser_name or "browser",
            resolved_engine,
        )
        return SearchResult(
            success=controller_result.success,
            query=query.strip(),
            engine=resolved_engine,
            browser=controller_result.browser_id or browser_name or "",
            url=controller_result.url,
            message=controller_result.message,
            error=controller_result.error,
            data=dict(controller_result.data),
        )

    def build_search_steps(
        self,
        query: str,
        *,
        target_app: str = "browser",
        engine: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        browser_name, engine_name = self._resolve_search_target(engine, target_app)
        params: dict[str, Any] = {"query": query, "target_app": target_app}
        if engine_name:
            params["engine"] = engine_name
        return [
            {
                "action": "search",
                "target": browser_name or target_app,
                "params": params,
                "estimated_risk": "low",
            }
        ]

    def build_navigation_step(self, action: str, *, target_app: str = "browser") -> dict[str, Any]:
        return {
            "action": "app_action",
            "target": target_app,
            "params": {"operation": "navigate", "action": action},
            "estimated_risk": "low",
        }

    def build_open_result_step(self, index: int, *, target_app: str = "browser") -> dict[str, Any]:
        return {
            "action": "app_action",
            "target": target_app,
            "params": {"operation": "open_result", "result_index": index},
            "estimated_risk": "low",
        }

    def execute_action(
        self,
        operation: str,
        *,
        action: str = "",
        result_index: int = 0,
        direction: str = "",
        amount: int = 1,
    ) -> BrowserActionResult:
        op = (operation or "").strip().lower()
        nav_action = (action or "").strip().lower().replace(" ", "_")

        if op == "navigate":
            controller_result = self._execute_navigation(nav_action)
            return self._wrap_result(nav_action or op, controller_result)

        if op == "scroll":
            scroll_direction = (direction or action or "down").strip().lower()
            pixels = max(1, int(amount or 1)) * 500
            controller_result = (
                self._controller.scroll_down(pixels)
                if scroll_direction == "down"
                else self._controller.scroll_up(pixels)
            )
            return self._wrap_result(f"scroll_{scroll_direction}", controller_result)

        if op == "open_result":
            controller_result = self._controller.click_link(max(1, int(result_index or 1)))
            return self._wrap_result(op, controller_result)

        return BrowserActionResult(
            success=False,
            operation=op or "unknown",
            error="unsupported_browser_operation",
            message=f"Unsupported browser operation: {operation or 'unknown'}.",
        )

    def _execute_navigation(self, action: str) -> BrowserOperationResult:
        handlers = {
            "back": self._controller.go_back,
            "go_back": self._controller.go_back,
            "forward": self._controller.go_forward,
            "go_forward": self._controller.go_forward,
            "refresh": self._controller.refresh,
            "reload": self._controller.refresh,
            "new_tab": self._controller.new_tab,
            "close_tab": self._controller.close_tab,
            "next_tab": self._controller.next_tab,
            "previous_tab": self._controller.previous_tab,
        }
        handler = handlers.get(action)
        if handler is None:
            return BrowserOperationResult(
                success=False,
                action=action or "unknown",
                message=f"Unsupported browser navigation action: {action or 'unknown'}.",
                error="unsupported_navigation_action",
            )
        return handler()

    def _resolve_search_target(
        self,
        engine: Optional[str],
        target_app: Optional[str],
    ) -> tuple[str, str]:
        normalized_engine = str(engine or "").strip().lower()
        normalized_target = str(target_app or "").strip().lower()

        if normalized_engine in SEARCH_ENGINE_URLS:
            return (normalized_target if normalized_target in {"chrome", "edge", "firefox", "brave"} else "", normalized_engine)

        if normalized_target in {"", "browser", "default browser", "web browser"}:
            return "", _DEFAULT_SEARCH_ENGINE
        if normalized_target in {"chrome", "edge", "firefox", "brave"}:
            return normalized_target, ""
        if normalized_target in {"google", "youtube", "bing", "duckduckgo"}:
            return "", normalized_target
        return "", ""

    @staticmethod
    def _wrap_result(operation: str, result: BrowserOperationResult) -> BrowserActionResult:
        return BrowserActionResult(
            success=result.success,
            operation=operation,
            message=result.message,
            error=result.error,
            data=dict(result.data),
        )
