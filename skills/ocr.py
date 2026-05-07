"""
Generic OCR skill for reading visible on-screen text and locating text targets.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from core import settings
from core.ocr import OCRMatch, OCRResult, OCREngine, get_ocr_engine
from skills.base import SkillBase, SkillExecutionResult


_READ_COMMANDS = {
    "read screen",
    "read the screen",
    "read visible text",
    "read current window",
    "read active window",
    "read fullscreen",
    "read full screen",
    "read results",
}
_FIND_PREFIXES = ("find text ", "locate text ", "find label ", "find word ")


class OCRSkill(SkillBase):
    """Skill wrapper around the shared OCR engine."""

    def __init__(self, *, ocr_engine: OCREngine | None = None) -> None:
        self._ocr = ocr_engine or get_ocr_engine()

    def can_handle(self, context: Mapping[str, Any], intent: str, command: str) -> bool:
        _ = context
        _ = intent
        if not settings.get("ocr_enabled"):
            return False
        normalized = self._normalize_command(command)
        return normalized in _READ_COMMANDS or any(normalized.startswith(prefix) for prefix in _FIND_PREFIXES)

    def execute(self, command: str, context: Mapping[str, Any]) -> SkillExecutionResult:
        _ = context
        normalized = self._normalize_command(command)
        action, payload = self._classify_command(command, normalized)

        if action == "find_text":
            target = str(payload.get("target") or "").strip()
            matches = self._ocr.find_text(target, capture_mode=str(payload.get("capture_mode") or ""))
            return self._format_find_response(target, matches)

        capture_mode = str(payload.get("capture_mode") or "")
        result = self._read_capture(capture_mode)
        return self._format_read_response(result)

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "target": "screen",
            "supports": [
                "read_screen_text",
                "read_active_window_text",
                "read_fullscreen_text",
                "find_text_on_screen",
                "structured_word_boxes",
            ],
            "capture_mode": settings.get("ocr_capture_mode"),
            "engine": settings.get("ocr_engine"),
        }

    def health_check(self) -> dict[str, Any]:
        return self._ocr.get_status()

    def _classify_command(self, original: str, normalized: str) -> tuple[str, dict[str, Any]]:
        if any(normalized.startswith(prefix) for prefix in _FIND_PREFIXES):
            target = re.sub(r"^(find text|locate text|find label|find word)\s+", "", original.strip(), flags=re.IGNORECASE)
            target = target.strip().strip("\"'")
            return "find_text", {"target": target, "capture_mode": self._default_capture_mode()}

        if normalized in {"read fullscreen", "read full screen"}:
            return "read_text", {"capture_mode": "fullscreen"}
        if normalized in {"read active window", "read current window"}:
            return "read_text", {"capture_mode": "active_window"}
        return "read_text", {"capture_mode": self._default_capture_mode()}

    def _read_capture(self, capture_mode: str) -> OCRResult:
        normalized = self._normalize_command(capture_mode)
        if normalized == "fullscreen":
            return self._ocr.read_fullscreen()
        return self._ocr.read_active_window()

    def _format_read_response(self, result: OCRResult) -> SkillExecutionResult:
        lines = [line.text for line in result.lines if line.text]
        capture_label = result.capture_mode.replace("_", " ") if result.capture_mode else self._default_capture_mode().replace("_", " ")
        if not lines:
            if self._ocr.last_error:
                response = f"I couldn't read the {capture_label} because {self._ocr.last_error}"
            else:
                response = f"I captured the {capture_label}, but no readable text was detected."
            return SkillExecutionResult(
                success=False,
                intent="ocr_read",
                response=response,
                skill_name=self.name(),
                error="ocr_no_text" if not self._ocr.last_error else "ocr_unavailable",
                data=result.to_dict(),
            )

        preview = lines[:12]
        suffix = ""
        if len(lines) > len(preview):
            suffix = f"\n... ({len(lines) - len(preview)} more line(s))"
        response = "Visible text:\n" + "\n".join(preview) + suffix
        return SkillExecutionResult(
            success=True,
            intent="ocr_read",
            response=response,
            skill_name=self.name(),
            data=result.to_dict(),
        )

    def _format_find_response(self, target: str, matches: list[OCRMatch]) -> SkillExecutionResult:
        if matches:
            count = len(matches)
            label = "location" if count == 1 else "locations"
            response = f'Found "{target}" on screen at {count} {label}.'
            return SkillExecutionResult(
                success=True,
                intent="ocr_find_text",
                response=response,
                skill_name=self.name(),
                data={
                    "target": target,
                    "matches": [match.to_dict() for match in matches],
                },
            )

        if self._ocr.last_error:
            response = f'I could not search the screen for "{target}" because {self._ocr.last_error}'
            error = "ocr_unavailable"
        else:
            response = f'I could not find "{target}" on screen.'
            error = "text_not_found"
        return SkillExecutionResult(
            success=False,
            intent="ocr_find_text",
            response=response,
            skill_name=self.name(),
            error=error,
            data={"target": target, "matches": []},
        )

    @staticmethod
    def _normalize_command(command: str) -> str:
        return " ".join(str(command or "").strip().lower().split())

    @staticmethod
    def _default_capture_mode() -> str:
        return str(settings.get("ocr_capture_mode") or "active_window").strip().lower()
