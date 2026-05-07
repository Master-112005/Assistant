"""
Base contract for assistant skills/plugins.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

from core.action_results import ActionResult, ensure_action_result


@dataclass
class SkillExecutionResult:
    """Standard result container returned by skill executions."""

    success: bool
    intent: str
    response: str
    skill_name: str
    handled: bool = True
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    action_result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        target = ""
        if isinstance(self.data, dict):
            target = str(self.data.get("target_app") or "").strip()
        action_result = ensure_action_result(
            self.action_result if self.action_result else {
                "success": self.success,
                "action": self.intent or self.skill_name,
                "target": target or None,
                "message": self.response,
                "error_code": self.error or None,
                "verified": bool(self.data.get("verified")) if isinstance(self.data, dict) else False,
                "duration_ms": int(self.data.get("duration_ms") or 0) if isinstance(self.data, dict) else 0,
                "data": dict(self.data),
            },
            default_action=self.intent or self.skill_name,
            default_target=target or None,
        )
        return {
            "success": self.success,
            "intent": self.intent,
            "response": self.response,
            "skill": self.skill_name,
            "skill_name": self.skill_name,
            "handled": self.handled,
            "error": self.error,
            "data": dict(self.data),
            "action_result": action_result,
        }

    @classmethod
    def from_action_result(
        cls,
        *,
        intent: str,
        response: str,
        skill_name: str,
        action_result: ActionResult,
        handled: bool = True,
        data: dict[str, Any] | None = None,
    ) -> "SkillExecutionResult":
        payload = dict(action_result.data)
        if data:
            payload.update(data)
        return cls(
            success=action_result.success,
            intent=intent,
            response=response,
            skill_name=skill_name,
            handled=handled,
            error=action_result.error_code or "",
            data=payload,
            action_result=action_result.to_dict(),
        )


class SkillBase(ABC):
    """Abstract contract implemented by app-specific skills."""

    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    def can_handle(self, context: Mapping[str, Any], intent: str, command: str) -> bool:
        """Return True when the skill should own the command."""

    @abstractmethod
    def execute(self, command: str, context: Mapping[str, Any]) -> SkillExecutionResult:
        """Execute a command that this skill has claimed."""

    @abstractmethod
    def get_capabilities(self) -> dict[str, Any]:
        """Return a structured description of what this skill supports."""

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """Return a lightweight runtime health summary for the skill."""


class PluginBase(ABC):
    """Stable SDK contract implemented by dynamically loaded plugins."""

    @abstractmethod
    def plugin_id(self) -> str:
        """Return the manifest plugin id."""

    @abstractmethod
    def name(self) -> str:
        """Return the display name."""

    @abstractmethod
    def version(self) -> str:
        """Return the semantic plugin version."""

    @abstractmethod
    def description(self) -> str:
        """Return a short human-readable description."""

    @abstractmethod
    def initialize(self, context: Mapping[str, Any]) -> None:
        """Initialize the plugin with app-provided context."""

    @abstractmethod
    def shutdown(self) -> None:
        """Release plugin resources."""

    @abstractmethod
    def can_handle(self, command: str, context: Mapping[str, Any]) -> bool:
        """Return True when this plugin should execute the command."""

    @abstractmethod
    def execute(self, command: str, context: Mapping[str, Any]) -> SkillExecutionResult:
        """Execute a command claimed by this plugin."""

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """Return plugin health details."""

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        """Return a structured capability description."""
