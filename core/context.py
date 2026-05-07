"""
Phase 13+ runtime context manager.

ContextManager is the public interface for querying active window context.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from core import settings, state
from core.logger import get_logger
from core.window_context import ActiveWindowDetector, UNKNOWN_WINDOW, WindowInfo

logger = get_logger(__name__)


class ContextManager:
    """
    High-level runtime context manager for the active Windows window.

    Wraps ActiveWindowDetector and exposes a stable API. Methods are safe to
    call even when pywin32 is unavailable.
    """

    def __init__(self) -> None:
        self._detector = ActiveWindowDetector()
        self._last_info: WindowInfo = UNKNOWN_WINDOW

    def refresh(self) -> WindowInfo:
        """Perform an immediate foreground window detection and update state."""
        if not settings.get("context_detection_enabled"):
            return UNKNOWN_WINDOW

        try:
            info = self._detector.get_active_context()
            self._last_info = info
            _write_state(info)
            return info
        except Exception as exc:
            logger.warning("ContextManager.refresh() error: %s", exc)
            return UNKNOWN_WINDOW

    def get_current_app(self) -> str:
        """Return the normalized app id of the current context."""
        return state.current_app or state.current_context or "unknown"

    def get_current_context(self) -> str:
        """Alias for get_current_app() used by the context engine."""
        return self.get_current_app()

    def get_current_title(self) -> str:
        """Return the current window title."""
        return state.current_window_title or ""

    def get_current_process(self) -> str:
        """Return the current process executable name."""
        return state.current_process_name or ""

    def get_context_snapshot(self) -> Dict[str, Any]:
        """Return the current runtime context as a dictionary."""
        return {
            "app_id": state.current_app,
            "current_context": state.current_context,
            "window_title": state.current_window_title,
            "process_name": state.current_process_name,
            "last_changed": state.last_context_change,
            "history_depth": len(state.context_history),
            "selected_item": getattr(state, "selected_item_context", {}),
        }

    def is_browser_active(self) -> bool:
        """True if the active window is a browser."""
        return self._last_info.app_type == "browser"

    def is_youtube_active(self) -> bool:
        """True if the active browser tab appears to be YouTube."""
        return (state.current_app or state.current_context) == "youtube"

    def start_watcher(
        self,
        callback: Optional[Callable[[WindowInfo], None]] = None,
    ) -> None:
        """Start the background watcher."""
        if not settings.get("context_detection_enabled"):
            logger.info("Context detection disabled - watcher not started")
            return

        self._detector.watch_active_window(callback=callback)

    def stop_watcher(self) -> None:
        """Stop the background watcher."""
        self._detector.stop_watcher()

    @property
    def is_watching(self) -> bool:
        """True if the watcher thread is active."""
        return self._detector.is_watching


context_manager = ContextManager()


def _write_state(info: WindowInfo) -> None:
    """Write a WindowInfo into core.state."""
    state.current_context = info.app_id
    state.current_app = info.app_id
    state.current_window_title = info.title
    state.current_process_name = info.process_name
    state.last_context_change = time.monotonic()
