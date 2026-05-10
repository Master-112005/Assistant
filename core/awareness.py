"""
Screen awareness engine that summarizes what is visible on screen.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from core import settings, state
from core.desktop_state import DesktopSnapshot, DesktopStateCollector, WindowSnapshot
from core.logger import get_logger
from skills.chrome import ChromeSkill
from skills.explorer import ExplorerSkill
from skills.music import MusicSkill
from skills.whatsapp import WhatsAppSkill
from skills.youtube import YouTubeSkill

logger = get_logger(__name__)

_APP_LABELS = {
    "chrome": "Chrome",
    "edge": "Edge",
    "firefox": "Firefox",
    "brave": "Brave",
    "opera": "Opera",
    "vivaldi": "Vivaldi",
    "youtube": "YouTube",
    "whatsapp": "WhatsApp",
    "spotify": "Spotify",
    "explorer": "File Explorer",
}


@dataclass
class AwarenessItem:
    source: str
    category: str
    summary: str
    confidence: float
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "category": self.category,
            "summary": self.summary,
            "confidence": self.confidence,
            "details": dict(self.details),
        }


@dataclass
class AwarenessReport:
    items: list[AwarenessItem]
    final_summary: str
    generated_at: float
    voice_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "final_summary": self.final_summary,
            "generated_at": self.generated_at,
            "voice_summary": self.voice_summary,
        }


class ScreenAwarenessEngine:
    """Compose a concise, truthful summary from current desktop signals."""

    def __init__(
        self,
        *,
        collector: DesktopStateCollector | None = None,
        chrome_skill: ChromeSkill | None = None,
        youtube_skill: YouTubeSkill | None = None,
        whatsapp_skill: WhatsAppSkill | None = None,
        music_skill: MusicSkill | None = None,
        explorer_skill: ExplorerSkill | None = None,
    ) -> None:
        self._collector = collector or DesktopStateCollector()
        self._chrome = chrome_skill or ChromeSkill()
        self._youtube = youtube_skill or YouTubeSkill()
        self._whatsapp = whatsapp_skill or WhatsAppSkill()
        self._music = music_skill or MusicSkill()
        self._explorer = explorer_skill or ExplorerSkill()

    def analyze(self) -> AwarenessReport:
        logger.info("Awareness started")
        if not settings.get("screen_awareness_enabled"):
            report = AwarenessReport(
                items=[],
                final_summary="Screen awareness is disabled in settings.",
                generated_at=time.time(),
                voice_summary="Screen awareness is disabled in settings.",
            )
            state.awareness_ready = False
            state.last_awareness_report = report.to_dict()
            state.last_desktop_snapshot = {}
            state.last_visible_apps = []
            return report

        snapshot = self._collector.capture_desktop_snapshot()
        logger.info("Visible windows: %d", len(snapshot.visible_windows))

        items: list[AwarenessItem] = []
        items.extend(self.analyze_active_window(snapshot))
        items.extend(self.analyze_visible_windows(snapshot))
        items.extend(self.collect_skill_insights(snapshot))
        filtered = self.filter_noise(items)

        report = AwarenessReport(
            items=filtered,
            final_summary=self.summarize(filtered, snapshot),
            generated_at=time.time(),
            voice_summary=self.generate_voice_summary(filtered),
        )

        state.awareness_ready = True
        state.last_awareness_report = report.to_dict()
        state.last_desktop_snapshot = snapshot.to_dict()
        state.last_visible_apps = list(snapshot.contexts)

        logger.info("Awareness items: %d", len(filtered))
        logger.info("Summary generated successfully")
        return report

    def analyze_active_window(self, snapshot: DesktopSnapshot) -> list[AwarenessItem]:
        active = snapshot.active_window
        if active is None:
            return []
        item = self._describe_window(active, snapshot=snapshot, allow_fallback=True)
        return [item] if item is not None else []

    def analyze_visible_windows(self, snapshot: DesktopSnapshot) -> list[AwarenessItem]:
        if settings.get("ignore_background_windows"):
            return []

        items: list[AwarenessItem] = []
        for window in snapshot.visible_windows:
            if window.is_foreground or window.app_id in {"spotify", "whatsapp"}:
                continue
            item = self._describe_window(window, snapshot=snapshot, allow_fallback=False)
            if item is not None:
                items.append(item)
        return items

    def collect_skill_insights(self, snapshot: DesktopSnapshot) -> list[AwarenessItem]:
        items: list[AwarenessItem] = []
        visible_apps = set(snapshot.contexts)

        if "whatsapp" in visible_apps:
            item = self._item_from_insight("whatsapp", "messaging", self._whatsapp.describe_visible_state())
            if item is not None:
                items.append(item)

        if "spotify" in visible_apps or getattr(state, "last_track_name", ""):
            item = self._item_from_insight("spotify", "music", self._music.describe_current_playback())
            if item is not None:
                items.append(item)

        explorer_window = self._select_window(snapshot.visible_windows, "explorer")
        if explorer_window is not None:
            item = self._item_from_insight(
                "explorer",
                "files",
                self._explorer.describe_window(hwnd=explorer_window.hwnd, title=explorer_window.title),
            )
            if item is not None:
                items.append(item)

        return items

    def summarize(self, items: list[AwarenessItem], snapshot: DesktopSnapshot) -> str:
        if items:
            return " ".join(self._ensure_period(item.summary) for item in items).strip()

        active = snapshot.active_window
        if active is not None:
            return f"I can see {self._app_label(active.app_id, active.process_name)} is active, but no additional details were detected."
        return "I couldn't detect any visible windows on the desktop."

    def generate_voice_summary(self, items: list[AwarenessItem]) -> str:
        if not items:
            return ""
        return " ".join(self._ensure_period(item.summary) for item in items[:2]).strip()

    def filter_noise(self, items: Iterable[AwarenessItem]) -> list[AwarenessItem]:
        best_by_source: dict[str, AwarenessItem] = {}
        for item in items:
            if not item.summary.strip():
                continue
            existing = best_by_source.get(item.source)
            if existing is None or self._sort_key(item) < self._sort_key(existing):
                best_by_source[item.source] = item

        filtered = [
            item
            for item in best_by_source.values()
            if item.confidence >= 0.58 or bool(item.details.get("is_foreground"))
        ]
        filtered.sort(key=self._sort_key)
        return filtered[: max(1, int(settings.get("awareness_max_items") or 5))]

    def _describe_window(
        self,
        window: WindowSnapshot,
        *,
        snapshot: DesktopSnapshot,
        allow_fallback: bool,
    ) -> AwarenessItem | None:
        if window.app_id == "youtube":
            playing = self._youtube.is_playing() if window.is_foreground else None
            return self._item_from_insight(
                "youtube",
                "media",
                self._youtube.describe_window_title(window.title, is_playing=playing),
                window=window,
            )

        if window.app_id == "chrome":
            return self._item_from_insight(
                "chrome",
                "browser",
                self._chrome.describe_window_title(window.title),
                window=window,
            )

        if window.app_id in {"edge", "firefox", "brave", "opera", "vivaldi"}:
            cleaned = self._clean_browser_title(window.title)
            if cleaned:
                return AwarenessItem(
                    source=window.app_id,
                    category="browser",
                    summary=f"{self._app_label(window.app_id, window.process_name)} has {cleaned} open.",
                    confidence=0.80 if window.is_foreground else 0.72,
                    details=self._window_details(window),
                )

        if window.app_id == "explorer" and window.is_foreground:
            return self._item_from_insight(
                "explorer",
                "files",
                self._explorer.describe_window(hwnd=window.hwnd, title=window.title),
                window=window,
            )

        if allow_fallback:
            return AwarenessItem(
                source=window.app_id or "active_window",
                category="window",
                summary=f"{self._app_label(window.app_id, window.process_name)} is active.",
                confidence=0.60,
                details=self._window_details(window),
            )
        return None

    def _item_from_insight(
        self,
        source: str,
        category: str,
        insight: dict[str, Any] | None,
        *,
        window: WindowSnapshot | None = None,
    ) -> AwarenessItem | None:
        if not insight:
            return None
        summary = str(insight.get("summary") or "").strip()
        if not summary:
            return None
        details = dict(insight.get("details") or {})
        if window is not None:
            details = {**self._window_details(window), **details}
        return AwarenessItem(
            source=source,
            category=category,
            summary=summary,
            confidence=float(insight.get("confidence") or 0.0),
            details=details,
        )

    @staticmethod
    def _sort_key(item: AwarenessItem) -> tuple[int, float, str]:
        foreground_penalty = 0 if item.details.get("is_foreground") else 1
        category_priority = {
            "music": 0,
            "messaging": 1,
            "browser": 2,
            "media": 3,
            "files": 4,
            "window": 5,
        }.get(item.category, 6)
        return (foreground_penalty, category_priority, -item.confidence, item.summary.lower())

    @staticmethod
    def _select_window(windows: list[WindowSnapshot], app_id: str) -> WindowSnapshot | None:
        matches = [window for window in windows if window.app_id == app_id]
        if not matches:
            return None
        matches.sort(key=lambda item: (not item.is_foreground, item.title.lower()))
        return matches[0]

    @staticmethod
    def _clean_browser_title(title: str) -> str:
        cleaned = str(title or "").strip()
        suffixes = (
            " - Google Chrome",
            " - Chrome",
            " - Microsoft Edge",
            " - Mozilla Firefox",
            " - Brave",
            " - Opera",
            " - Vivaldi",
        )
        for suffix in suffixes:
            if cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)].strip()
                break
        return cleaned

    @staticmethod
    def _window_details(window: WindowSnapshot) -> dict[str, Any]:
        return {
            "app_id": window.app_id,
            "process_name": window.process_name,
            "title": window.title,
            "bounds": list(window.bounds),
            "hwnd": window.hwnd,
            "is_foreground": window.is_foreground,
        }

    @staticmethod
    def _app_label(app_id: str, process_name: str) -> str:
        normalized = str(app_id or "").strip().lower()
        if normalized in _APP_LABELS:
            return _APP_LABELS[normalized]
        fallback = str(process_name or "").strip()
        if fallback.lower().endswith(".exe"):
            fallback = fallback[:-4]
        return fallback.replace("_", " ").title() or "The current app"

    @staticmethod
    def _ensure_period(text: str) -> str:
        cleaned = str(text or "").strip()
        if not cleaned:
            return ""
        return cleaned if cleaned.endswith((".", "!", "?")) else f"{cleaned}."


_awareness_engine_instance: ScreenAwarenessEngine | None = None


def get_awareness_engine() -> ScreenAwarenessEngine:
    global _awareness_engine_instance
    if _awareness_engine_instance is None:
        _awareness_engine_instance = ScreenAwarenessEngine()
    return _awareness_engine_instance
