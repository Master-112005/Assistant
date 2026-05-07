"""
Screen awareness skill.
"""
from __future__ import annotations

from typing import Any, Mapping

from core import settings
from core.awareness import ScreenAwarenessEngine, get_awareness_engine
from core.text_utils import normalize_command
from skills.base import SkillBase, SkillExecutionResult

_COMMANDS = {
    "what is on screen",
    "what's on screen",
    "what is on the screen",
    "what's on the screen",
    "describe screen",
    "describe the screen",
    "screen status",
    "screen summary",
    "what can you see",
}


class AwarenessSkill(SkillBase):
    def __init__(self, *, engine: ScreenAwarenessEngine | None = None) -> None:
        self._engine = engine or get_awareness_engine()

    def can_handle(self, context: Mapping[str, Any], intent: str, command: str) -> bool:
        _ = context
        _ = intent
        if not settings.get("screen_awareness_enabled"):
            return False
        return self._normalize(command) in _COMMANDS

    def execute(self, command: str, context: Mapping[str, Any]) -> SkillExecutionResult:
        _ = command
        _ = context
        report = self._engine.analyze()
        return SkillExecutionResult(
            success=bool(report.final_summary),
            intent="screen_awareness",
            response=report.final_summary,
            skill_name=self.name(),
            data={
                "report": report.to_dict(),
                "voice_summary": report.voice_summary,
                "speak_response": bool(settings.get("speak_awareness_summary")),
                "target_app": "screen",
            },
        )

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "target": "screen",
            "supports": ["describe_screen", "screen_status", "desktop_summary"],
            "use_ocr": bool(settings.get("awareness_use_ocr")),
            "max_items": int(settings.get("awareness_max_items") or 5),
        }

    def health_check(self) -> dict[str, Any]:
        return {
            "enabled": bool(settings.get("screen_awareness_enabled")),
            "use_ocr": bool(settings.get("awareness_use_ocr")),
            "max_items": int(settings.get("awareness_max_items") or 5),
            "ignore_background_windows": bool(settings.get("ignore_background_windows")),
        }

    @staticmethod
    def _normalize(value: str) -> str:
        return normalize_command(value)
