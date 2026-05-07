"""
Normalized command-result contract shared across processor, workers, and UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from core.action_results import ensure_action_result


class CommandCategory(StrEnum):
    ACTION = "action"
    CHAT = "chat"
    QUERY = "query"
    SYSTEM = "system"


_CHAT_INTENTS = {
    "greeting",
    "question",
    "help",
    "gratitude",
    "identity",
}

_QUERY_INTENTS = {
    "time_query",
    "analytics_logs",
    "analytics_errors",
    "analytics_stats",
    "clipboard_query",
    "search_file",
}

_SYSTEM_INTENTS = {
    "system_control",
    "volume_up",
    "volume_down",
    "mute",
    "unmute",
    "set_volume",
    "brightness_up",
    "brightness_down",
    "set_brightness",
    "lock_pc",
    "shutdown_pc",
    "restart_pc",
    "sleep_pc",
}


def infer_command_category(intent: str, *, explicit: str = "") -> str:
    normalized_explicit = str(explicit or "").strip().lower()
    if normalized_explicit in {category.value for category in CommandCategory}:
        return normalized_explicit

    normalized_intent = str(intent or "").strip().lower()
    if normalized_intent in _CHAT_INTENTS:
        return CommandCategory.CHAT.value
    if normalized_intent in _QUERY_INTENTS:
        return CommandCategory.QUERY.value
    if normalized_intent in _SYSTEM_INTENTS:
        return CommandCategory.SYSTEM.value
    return CommandCategory.ACTION.value


@dataclass(slots=True)
class CommandResult:
    success: bool
    intent: str
    category: str
    message: str
    error_code: str | None = None
    verified: bool = False
    data: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    action_result: dict[str, Any] = field(default_factory=dict)
    skill_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.data)
        payload.setdefault("category", self.category)
        payload.setdefault("verified", bool(self.verified))
        payload.setdefault("duration_ms", int(max(0, int(self.duration_ms or 0))))
        action_result = ensure_action_result(
            self.action_result
            if self.action_result
            else {
                "success": self.success,
                "action": self.intent,
                "target": payload.get("target_app"),
                "message": self.message,
                "error_code": self.error_code,
                "verified": self.verified,
                "duration_ms": self.duration_ms,
                "data": payload,
            },
            default_action=self.intent,
            default_target=str(payload.get("target_app") or "").strip() or None,
        )
        return {
            "success": bool(self.success),
            "intent": str(self.intent or ""),
            "category": self.category,
            "message": str(self.message or ""),
            "response": str(self.message or ""),
            "error_code": self.error_code or None,
            "error": str(self.error_code or ""),
            "verified": bool(self.verified),
            "duration_ms": int(max(0, int(self.duration_ms or 0))),
            "data": payload,
            "action_result": action_result,
            "skill_name": str(self.skill_name or ""),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "CommandResult":
        mapping = dict(payload or {})
        data = dict(mapping.get("data") or {}) if isinstance(mapping.get("data"), Mapping) else {}
        intent = str(mapping.get("intent") or data.get("intent") or "").strip()
        action_result = ensure_action_result(
            mapping,
            default_action=intent or "unknown",
            default_target=str(data.get("target_app") or "").strip() or None,
        )
        message = str(mapping.get("message") or mapping.get("response") or action_result.get("message") or "").strip()
        error_code = str(
            mapping.get("error_code")
            or mapping.get("error")
            or action_result.get("error_code")
            or ""
        ).strip() or None
        raw_duration = mapping.get("duration_ms", action_result.get("duration_ms", data.get("duration_ms", 0)))
        try:
            duration_ms = int(round(float(raw_duration or 0)))
        except (TypeError, ValueError):
            duration_ms = 0
        verified = bool(mapping.get("verified", action_result.get("verified", data.get("verified", False))))
        category = infer_command_category(
            intent,
            explicit=str(mapping.get("category") or data.get("category") or ""),
        )
        skill_name = str(mapping.get("skill_name") or mapping.get("skill") or "").strip()
        success = bool(mapping.get("success", False))
        return cls(
            success=success,
            intent=intent,
            category=category,
            message=message,
            error_code=error_code,
            verified=verified,
            data=data,
            duration_ms=max(0, duration_ms),
            action_result=action_result,
            skill_name=skill_name,
        )


def ensure_command_result(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    return CommandResult.from_mapping(payload).to_dict()


__all__ = [
    "CommandCategory",
    "CommandResult",
    "ensure_command_result",
    "infer_command_category",
]
