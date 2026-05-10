"""
Skill wrapper for click-by-text desktop automation.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from core import settings
from core.click_text import TextClickEngine, get_text_click_engine
from core.text_utils import normalize_command
from skills.base import SkillBase, SkillExecutionResult

_CLICK_PREFIXES = ("click ", "tap ", "press ")
_OPEN_PREFIXES = ("open ",)
_UI_OPEN_ALLOWLIST = {
    "settings",
    "login",
    "log in",
    "search",
    "next",
    "continue",
    "submit",
    "ok",
    "cancel",
    "sign in",
}
_APP_OPEN_BLOCKLIST = {
    "chrome",
    "google chrome",
    "youtube",
    "whatsapp",
    "spotify",
    "explorer",
    "file explorer",
    "notepad",
    "calculator",
    "paint",
    "edge",
    "windows settings",
    "settings app",
    "terminal",
    "powershell",
    "cmd",
}


class ClickTextSkill(SkillBase):
    def __init__(self, *, engine: TextClickEngine | None = None) -> None:
        self._engine = engine or get_text_click_engine()

    def can_handle(self, context: Mapping[str, Any], intent: str, command: str) -> bool:
        if not settings.get("click_text_enabled"):
            return False

        normalized = self._normalize(command)
        if any(normalized.startswith(prefix) for prefix in _CLICK_PREFIXES):
            return True
        if not any(normalized.startswith(prefix) for prefix in _OPEN_PREFIXES):
            return False

        target = self._extract_target(command, _OPEN_PREFIXES)
        if not target:
            return False

        target_normalized = self._normalize(target)
        if target_normalized in _APP_OPEN_BLOCKLIST:
            return False
        if target_normalized in _UI_OPEN_ALLOWLIST:
            return True

        current_app = self._normalize(str(context.get("current_app") or ""))
        current_title = str(context.get("current_window_title") or "").strip()
        return bool(current_title and current_app not in {"", "unknown"} and intent != "open_app")

    def execute(self, command: str, context: Mapping[str, Any]) -> SkillExecutionResult:
        _ = context
        normalized = self._normalize(command)
        prefixes = _OPEN_PREFIXES if any(normalized.startswith(prefix) for prefix in _OPEN_PREFIXES) else _CLICK_PREFIXES
        target = self._extract_target(command, prefixes)
        if not target:
            return SkillExecutionResult(
                success=False,
                intent="click_by_text",
                response="I need visible text to click.",
                skill_name=self.name(),
                error="empty_text_target",
            )

        result = self._engine.click_text(target)
        return SkillExecutionResult(
            success=result.success,
            intent="click_by_text",
            response=result.message,
            skill_name=self.name(),
            error="" if result.success else "text_click_failed",
            data={
                "target_text": target,
                "click_result": result.to_dict(),
                "target_app": "screen",
            },
        )

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "target": "screen",
            "supports": [
                "click_visible_text",
                "tap_visible_text",
                "press_visible_text",
                "safe_ranked_text_click",
            ],
            "verify_clicks": bool(settings.get("click_text_verify")),
            "fuzzy_match": bool(settings.get("click_text_fuzzy_match")),
        }

    def health_check(self) -> dict[str, Any]:
        return {
            "enabled": bool(settings.get("click_text_enabled")),
            "min_confidence": float(settings.get("click_text_min_confidence") or 0.55),
            "verify_clicks": bool(settings.get("click_text_verify")),
            "fuzzy_match": bool(settings.get("click_text_fuzzy_match")),
            "uia_available": True,
        }

    @staticmethod
    def _normalize(value: str) -> str:
        return normalize_command(value)

    @staticmethod
    def _extract_target(command: str, prefixes: tuple[str, ...]) -> str:
        source = str(command or "").strip()
        for prefix in prefixes:
            pattern = re.compile(rf"^\s*{re.escape(prefix)}", flags=re.IGNORECASE)
            if pattern.match(source):
                return pattern.sub("", source, count=1).strip().strip("\"'")
        return ""
