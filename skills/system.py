"""
System control skill for real Windows device actions.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from core import settings, state
from core.logger import get_logger
from core.permissions import Decision, permission_manager as default_permission_manager
from core.safety import SystemSafetyPolicy
from core.system_controls import (
    ParsedSystemCommand,
    SystemActionResult,
    SystemController,
    command_from_entities,
    parse_system_command,
)
from core.text_utils import normalize_command
from skills.base import SkillBase, SkillExecutionResult

logger = get_logger(__name__)


class SystemSkill(SkillBase):
    """Natural-language system control skill with confirmation safeguards."""

    def __init__(
        self,
        *,
        controller: SystemController | None = None,
        safety_policy: SystemSafetyPolicy | None = None,
        permission_manager=None,
    ) -> None:
        self._controller = controller or SystemController()
        self._safety = safety_policy or SystemSafetyPolicy()
        self._permission_manager = permission_manager or default_permission_manager
        self._confirm_cb: Callable[[str], bool] | None = None

    def set_confirm_callback(self, callback: Callable[[str], bool]) -> None:
        self._confirm_cb = callback

    def set_permission_manager(self, permission_manager) -> None:
        self._permission_manager = permission_manager

    def can_handle(self, context: Mapping[str, Any], intent: str, command: str) -> bool:
        if parse_system_command(command) is not None:
            return True

        return str(intent or "").strip().lower() == "system_control"

    def execute(self, command: str, context: Mapping[str, Any]) -> SkillExecutionResult:
        if not settings.get("system_controls_enabled"):
            return self._failure("System controls are disabled in settings.", "system_controls_disabled")

        confirmation_reply = self._permission_manager.handle_confirmation_reply(command)
        if confirmation_reply is not None:
            if isinstance(confirmation_reply, SkillExecutionResult):
                return confirmation_reply
            if hasattr(confirmation_reply, "to_dict"):
                confirmation_reply = confirmation_reply.to_dict()
            if isinstance(confirmation_reply, dict):
                return SkillExecutionResult(
                    success=bool(confirmation_reply.get("success")),
                    intent=str(confirmation_reply.get("intent") or "system_control"),
                    response=str(confirmation_reply.get("response") or ""),
                    skill_name=self.name(),
                    error=str(confirmation_reply.get("error") or ""),
                    data=dict(confirmation_reply.get("data") or {}),
                )

        parsed = self._resolve_command(command, context)
        if parsed is None:
            return self._failure("I couldn't understand the system control command.", "invalid_system_command")

        if not context.get("permission_prechecked"):
            gate = self._authorize_or_defer(parsed, command)
            if gate is not None:
                return gate
        return self._finalize(self._controller.execute(parsed))

    def execute_operation(
        self,
        control: str,
        *,
        confirmed: bool = False,
        require_confirmation: bool = False,
        **params: Any,
    ) -> SkillExecutionResult:
        if not settings.get("system_controls_enabled"):
            return self._failure("System controls are disabled in settings.", "system_controls_disabled")

        parsed = command_from_entities(control, params) or parse_system_command(control)
        if parsed is None:
            return self._failure(f"I couldn't resolve the system action '{control}'.", "invalid_system_command")

        if require_confirmation and not confirmed:
            gate = self._authorize_or_defer(parsed, str(control or parsed.action))
            if gate is not None:
                return gate

        return self._finalize(self._controller.execute(parsed))

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "target": "system",
            "supports": [
                "volume",
                "brightness",
                "wifi",
                "bluetooth",
                "lock",
                "shutdown",
                "restart",
                "cancel_shutdown",
                "confirmation_flow",
                "state_verification",
            ],
        }

    def health_check(self) -> dict[str, Any]:
        return {
            "target": "system",
            "enabled": bool(settings.get("system_controls_enabled")),
            "pending_confirmation": bool(self._permission_manager.has_pending_confirmation()),
            "last_volume": int(getattr(state, "last_volume", -1) or -1),
            "last_brightness": int(getattr(state, "last_brightness", -1) or -1),
            "wifi_state": getattr(state, "wifi_state", "unknown"),
            "bluetooth_state": getattr(state, "bluetooth_state", "unknown"),
            "last_action": dict(getattr(state, "last_system_action", {}) or {}),
        }

    def _resolve_command(self, command: str, context: Mapping[str, Any]) -> ParsedSystemCommand | None:
        parsed = parse_system_command(command)
        if parsed is not None:
            return parsed

        entities = context.get("entities") if isinstance(context.get("entities"), dict) else {}
        intent_name = str(context.get("intent") or "").strip().lower()
        if intent_name != "system_control" and str(context.get("context_resolved_intent") or "").strip().lower() != "system_control":
            return None

        control = str(entities.get("control") or "").strip()
        if not control:
            return None
        return command_from_entities(control, dict(entities))

    def _finalize(self, result: SystemActionResult) -> SkillExecutionResult:
        skill_result = SkillExecutionResult(
            success=result.success,
            intent=self._intent_for_result(result),
            response=result.message,
            skill_name=self.name(),
            error=result.error,
            data={
                "target_app": "system",
                "action": result.action,
                "previous_state": dict(result.previous_state),
                "current_state": dict(result.current_state),
                "timestamp": result.timestamp,
            },
        )
        self._permission_manager.record_execution(result.action, {"target_app": "system"}, success=result.success, error=result.error)
        return skill_result

    @staticmethod
    def _normalize(text: str) -> str:
        return normalize_command(text)

    @staticmethod
    def _intent_for(command: ParsedSystemCommand) -> str:
        if command.action.startswith("wifi"):
            return "wifi_control"
        if command.action.startswith("bluetooth"):
            return "bluetooth_control"
        if "brightness" in command.action:
            return "brightness_control"
        if command.action in {"lock_pc", "shutdown", "restart", "cancel_shutdown"}:
            return "power_control"
        return "volume_control"

    @classmethod
    def _intent_for_result(cls, result: SystemActionResult) -> str:
        return cls._intent_for(
            ParsedSystemCommand(
                action=result.action,
                control=result.action,
                direction=result.action,
            )
        )

    def _failure(self, response: str, error: str) -> SkillExecutionResult:
        return SkillExecutionResult(
            success=False,
            intent="system_control",
            response=response,
            skill_name=self.name(),
            error=error,
            data={"target_app": "system"},
        )

    def _cancelled(self, response: str) -> SkillExecutionResult:
        return SkillExecutionResult(
            success=False,
            intent="system_control",
            response=response,
            skill_name=self.name(),
            error="cancelled",
            data={"target_app": "system"},
        )

    def _authorize_or_defer(self, parsed: ParsedSystemCommand, source_text: str) -> SkillExecutionResult | None:
        permission = self._permission_manager.evaluate(
            "system_control",
            {
                "action": parsed.action,
                "control": parsed.control,
                "delay_seconds": parsed.delay_seconds,
                "command": source_text,
            },
        )
        if permission.decision == Decision.DENY:
            return self._failure(permission.reason, "permission_denied")
        if permission.decision != Decision.REQUIRE_CONFIRMATION:
            return None

        policy_prompt = self._safety.evaluate(
            parsed.control if parsed.control in {"shutdown", "restart"} else parsed.action,
            delay_seconds=parsed.delay_seconds,
        ).prompt or f"Confirm system action: {parsed.action}"
        token = self._permission_manager.request_confirmation(
            "system_control",
            {
                "prompt": policy_prompt,
                "reason": permission.reason,
                "risk_level": permission.risk_level,
                "params": {
                    "action": parsed.action,
                    "control": parsed.control,
                    "delay_seconds": parsed.delay_seconds,
                },
            },
            callback=lambda: self._finalize(self._controller.execute(parsed)),
        )
        pending = dict(getattr(state, "pending_confirmation", {}) or {})
        pending["skill"] = "system"
        state.pending_confirmation = pending
        if self._confirm_cb is not None and self._confirm_cb(policy_prompt):
            self._permission_manager.approve(token)
            return self._finalize(self._controller.execute(parsed))
        if self._confirm_cb is not None:
            self._permission_manager.deny(token)
            return self._cancelled("Cancelled the system action.")
        return SkillExecutionResult(
            success=False,
            intent=self._intent_for(parsed),
            response=f"{policy_prompt} Say yes to continue or cancel to stop.",
            skill_name=self.name(),
            error="confirmation_required",
            data={"target_app": "system", "action": parsed.action, "token": token},
        )
