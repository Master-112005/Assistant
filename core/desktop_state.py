"""
Desktop snapshot capture for awareness summaries.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from core import settings
from core.automation import DesktopAutomation, WindowTarget
from core.logger import get_logger
from core.window_context import ActiveWindowDetector, WindowInfo

logger = get_logger(__name__)

_PROCESS_STEM_RE = re.compile(r"\.exe$", flags=re.IGNORECASE)


@dataclass
class WindowSnapshot:
    title: str
    process_name: str
    app_type: str
    is_foreground: bool
    is_visible: bool
    bounds: tuple[int, int, int, int]
    hwnd: int = 0
    app_id: str = "unknown"
    is_minimized: bool = False

    @property
    def width(self) -> int:
        return max(0, self.bounds[2] - self.bounds[0])

    @property
    def height(self) -> int:
        return max(0, self.bounds[3] - self.bounds[1])

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "process_name": self.process_name,
            "app_type": self.app_type,
            "is_foreground": self.is_foreground,
            "is_visible": self.is_visible,
            "bounds": list(self.bounds),
            "hwnd": self.hwnd,
            "app_id": self.app_id,
            "is_minimized": self.is_minimized,
        }


@dataclass
class DesktopSnapshot:
    timestamp: float
    active_window: WindowSnapshot | None
    visible_windows: list[WindowSnapshot]
    contexts: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "active_window": _window_snapshot_to_dict(self.active_window) if self.active_window else None,
            "visible_windows": [_window_snapshot_to_dict(window) for window in self.visible_windows],
            "contexts": list(self.contexts),
        }


class DesktopStateCollector:
    """Capture active-window and visible-window state from real desktop signals."""

    def __init__(
        self,
        *,
        automation: DesktopAutomation | None = None,
        detector: ActiveWindowDetector | None = None,
    ) -> None:
        self._automation = automation or DesktopAutomation()
        self._detector = detector or ActiveWindowDetector()

    def capture_desktop_snapshot(self) -> DesktopSnapshot:
        """Capture desktop state using UIA only - no OCR."""
        visible_windows = self.list_visible_windows()
        active_window = next((window for window in visible_windows if window.is_foreground), None)
        if active_window is None:
            active_window = self.get_foreground_snapshot()
            if active_window is not None and all(window.hwnd != active_window.hwnd for window in visible_windows):
                visible_windows.insert(0, active_window)

        return DesktopSnapshot(
            timestamp=time.time(),
            active_window=active_window,
            visible_windows=visible_windows,
            contexts=self._collect_contexts(visible_windows),
        )

    def list_visible_windows(self) -> list[WindowSnapshot]:
        windows = self._automation.list_windows()
        foreground = self._automation.get_foreground_window()
        snapshots: list[WindowSnapshot] = []
        seen: set[int] = set()

        for target in windows:
            if not target.hwnd or target.hwnd in seen:
                continue
            snapshot = self._snapshot_from_target(target, foreground_hwnd=foreground)
            if not self._should_keep(snapshot):
                continue
            snapshots.append(snapshot)
            seen.add(snapshot.hwnd)

        snapshots.sort(key=lambda item: (not item.is_foreground, item.title.lower()))
        return snapshots

    def get_foreground_snapshot(self) -> WindowSnapshot | None:
        foreground = self._automation.get_foreground_window()
        if foreground:
            target = self._automation.get_window(foreground)
            if target is not None:
                snapshot = self._snapshot_from_target(target, foreground_hwnd=foreground)
                if self._should_keep(snapshot):
                    return snapshot

        try:
            info = self._detector.get_active_context()
        except Exception as exc:
            logger.warning("Foreground snapshot failed: %s", exc)
            return None

        snapshot = self._snapshot_from_info(info, foreground_hwnd=info.hwnd)
        return snapshot if self._should_keep(snapshot) else None

    def _snapshot_from_target(self, target: WindowTarget, *, foreground_hwnd: int) -> WindowSnapshot:
        try:
            info = self._detector.get_window_info(target.hwnd)
        except Exception:
            info = None
        if info is not None and info.hwnd:
            return self._snapshot_from_info(info, foreground_hwnd=foreground_hwnd, fallback_target=target)

        return WindowSnapshot(
            title=str(target.title or "").strip(),
            process_name=str(target.process_name or "").strip(),
            app_type="unknown",
            is_foreground=target.hwnd == foreground_hwnd,
            is_visible=bool(target.is_visible),
            bounds=tuple(int(value) for value in target.rect),
            hwnd=int(target.hwnd or 0),
            app_id=self._fallback_app_id(target.process_name),
            is_minimized=bool(target.is_minimized),
        )

    def _snapshot_from_info(
        self,
        info: WindowInfo,
        *,
        foreground_hwnd: int,
        fallback_target: WindowTarget | None = None,
    ) -> WindowSnapshot:
        process_name = str(info.process_name or (fallback_target.process_name if fallback_target else "") or "").strip()
        bounds = info.rect if info.rect != (0, 0, 0, 0) else (fallback_target.rect if fallback_target else (0, 0, 0, 0))
        return WindowSnapshot(
            title=str(info.title or (fallback_target.title if fallback_target else "") or "").strip(),
            process_name=process_name,
            app_type=str(info.app_type or "unknown"),
            is_foreground=bool((info.hwnd or (fallback_target.hwnd if fallback_target else 0)) == foreground_hwnd),
            is_visible=bool(info.is_visible if info.hwnd else (fallback_target.is_visible if fallback_target else False)),
            bounds=tuple(int(value) for value in bounds),
            hwnd=int(info.hwnd or (fallback_target.hwnd if fallback_target else 0) or 0),
            app_id=str(info.app_id or self._fallback_app_id(process_name)),
            is_minimized=bool(info.is_minimized if info.hwnd else (fallback_target.is_minimized if fallback_target else False)),
        )

    def _collect_contexts(self, visible_windows: list[WindowSnapshot]) -> list[str]:
        contexts: list[str] = []
        seen: set[str] = set()
        for window in visible_windows:
            candidate = str(window.app_id or window.app_type or self._fallback_app_id(window.process_name)).strip().lower()
            if not candidate or candidate in seen:
                continue
            contexts.append(candidate)
            seen.add(candidate)
        return contexts

    @staticmethod
    def _fallback_app_id(process_name: str | None) -> str:
        cleaned = _PROCESS_STEM_RE.sub("", str(process_name or "").strip().lower())
        return cleaned or "unknown"

    @staticmethod
    def _should_keep(snapshot: WindowSnapshot | None) -> bool:
        if snapshot is None:
            return False
        if not snapshot.hwnd or not snapshot.is_visible or snapshot.is_minimized:
            return False
        if snapshot.width <= 48 or snapshot.height <= 48:
            return False
        title_l = snapshot.title.strip().lower()
        if title_l.startswith(("default ime", "msctfime ui")):
            return False
        if "nova assistant" in title_l:
            return False
        return True


desktop_state_collector = DesktopStateCollector()


def capture_desktop_snapshot() -> DesktopSnapshot:
    return desktop_state_collector.capture_desktop_snapshot()


def list_visible_windows() -> list[WindowSnapshot]:
    return desktop_state_collector.list_visible_windows()


def get_foreground_snapshot() -> WindowSnapshot | None:
    return desktop_state_collector.get_foreground_snapshot()


def _window_snapshot_to_dict(window: Any) -> dict[str, Any]:
    if hasattr(window, "to_dict") and callable(window.to_dict):
        return window.to_dict()
    bounds = tuple(getattr(window, "bounds", (0, 0, 0, 0)) or (0, 0, 0, 0))
    return {
        "title": str(getattr(window, "title", "") or ""),
        "process_name": str(getattr(window, "process_name", "") or ""),
        "app_type": str(getattr(window, "app_type", "unknown") or "unknown"),
        "is_foreground": bool(getattr(window, "is_foreground", False)),
        "is_visible": bool(getattr(window, "is_visible", True)),
        "bounds": list(bounds),
        "hwnd": int(getattr(window, "hwnd", 0) or 0),
        "app_id": str(getattr(window, "app_id", "unknown") or "unknown"),
        "is_minimized": bool(getattr(window, "is_minimized", False)),
    }
