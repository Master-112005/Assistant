"""
Safety confirmation layer for the execution engine and file backend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from core import settings
from core.logger import get_logger
from core.permissions import Decision, RiskLevel, permission_manager as default_permission_manager
from core.path_resolver import PathResolver

if TYPE_CHECKING:
    from core.plan_models import PlanStep

logger = get_logger(__name__)


_DANGEROUS_SYSTEM_CONTROLS = frozenset(
    {
        "shutdown",
        "restart",
        "reboot",
        "hibernate",
        "sleep",
        "logoff",
        "sign out",
        "format",
    }
)

_DANGEROUS_FILE_VERBS = frozenset(
    {
        "delete",
        "remove",
        "erase",
        "wipe",
        "format",
        "overwrite",
    }
)


@dataclass(slots=True)
class FileSafetyDecision:
    requires_confirmation: bool
    prompt: str = ""
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SystemSafetyDecision:
    requires_confirmation: bool
    prompt: str = ""
    reasons: list[str] = field(default_factory=list)


class SystemSafetyPolicy:
    """Assess confirmation requirements for system-control actions."""

    def evaluate(self, action: str, *, delay_seconds: int = 0) -> SystemSafetyDecision:
        normalized_action = str(action or "").strip().lower()
        reasons: list[str] = []

        if normalized_action == "shutdown" and settings.get("confirm_shutdown"):
            reasons.append("shutdown confirmation is enabled")
        elif normalized_action in {"restart", "reboot"} and settings.get("confirm_restart"):
            reasons.append("restart confirmation is enabled")
        elif normalized_action in {"lock", "lock_pc"} and settings.get("confirm_lock"):
            reasons.append("lock confirmation is enabled")
        elif normalized_action in {"hibernate", "sleep", "logoff", "sign out"}:
            reasons.append("this action immediately disrupts the active session")

        if not reasons:
            return SystemSafetyDecision(requires_confirmation=False)

        return SystemSafetyDecision(
            requires_confirmation=True,
            prompt=self._build_prompt(normalized_action, delay_seconds=delay_seconds, reasons=reasons),
            reasons=reasons,
        )

    def evaluate_step(self, step: "PlanStep") -> SystemSafetyDecision:
        action = str(step.params.get("action") or step.params.get("control") or step.target or "").strip().lower()
        delay_seconds = int(step.params.get("delay_seconds") or 0)
        return self.evaluate(action, delay_seconds=delay_seconds)

    @staticmethod
    def _build_prompt(action: str, *, delay_seconds: int, reasons: list[str]) -> str:
        label = {
            "shutdown": "shut down the PC",
            "restart": "restart the PC",
            "reboot": "restart the PC",
            "lock": "lock the PC",
            "lock_pc": "lock the PC",
            "hibernate": "hibernate the PC",
            "sleep": "put the PC to sleep",
            "logoff": "sign out of Windows",
            "sign out": "sign out of Windows",
        }.get(action, f"run '{action or 'system action'}'")

        delay_fragment = ""
        if delay_seconds > 0:
            delay_fragment = f" in {delay_seconds} second{'s' if delay_seconds != 1 else ''}"
        return f"Are you sure you want to {label}{delay_fragment}? Reason: {'; '.join(reasons)}."


class FileSafetyPolicy:
    """Assess whether a file operation should require explicit confirmation."""

    def __init__(self, *, path_resolver: PathResolver | None = None) -> None:
        self._resolver = path_resolver or PathResolver()

    def evaluate(
        self,
        action: str,
        *,
        source_path: str | Path | None = None,
        target_path: str | Path | None = None,
        permanent: bool = False,
        overwrite: bool = False,
        item_count: int | None = None,
    ) -> FileSafetyDecision:
        normalized_action = str(action or "").strip().lower()
        reasons: list[str] = []

        source = self._normalize_path(source_path)
        target = self._normalize_path(target_path)

        if normalized_action == "delete" and settings.get("confirm_delete"):
            reasons.append("delete confirmation is enabled")

        if permanent:
            reasons.append("this permanently deletes data")

        bulk_threshold = int(settings.get("file_bulk_delete_confirmation_threshold") or 25)
        if normalized_action == "delete" and item_count and item_count > 1:
            reasons.append(f"this affects {item_count} items")
            if item_count >= bulk_threshold:
                reasons.append("this is a bulk delete")

        if overwrite and target is not None and settings.get("confirm_overwrite"):
            reasons.append("this overwrites an existing item")

        for candidate in [source, target]:
            if candidate is None:
                continue
            if self._resolver.is_system_directory(candidate):
                reasons.append(f"{candidate} is inside a protected system directory")
                break

        if settings.get("confirm_outside_user_scope"):
            for candidate in [source, target]:
                if candidate is None:
                    continue
                if not self._resolver.is_in_user_scope(candidate):
                    reasons.append(f"{candidate} is outside your normal user scope")
                    break

        if not reasons:
            return FileSafetyDecision(requires_confirmation=False)

        return FileSafetyDecision(
            requires_confirmation=True,
            prompt=self._build_prompt(
                normalized_action,
                source_path=source,
                target_path=target,
                permanent=permanent,
                reasons=reasons,
            ),
            reasons=reasons,
        )

    def evaluate_step(self, step: "PlanStep") -> FileSafetyDecision:
        params = dict(step.params)
        action = str(params.get("action") or step.target or "").strip().lower()
        source_ref = params.get("source_path") or params.get("path") or params.get("filename") or step.target
        target_ref = params.get("target_path") or params.get("destination") or params.get("new_name")
        permanent = bool(params.get("permanent"))
        overwrite = bool(params.get("overwrite"))
        item_count = params.get("item_count")

        if item_count is None and action == "delete" and source_ref:
            try:
                source_path = self._resolver.resolve(source_ref)
            except Exception:
                source_path = None
            if source_path is not None and source_path.exists() and source_path.is_dir():
                item_count = self._count_items(source_path)

        return self.evaluate(
            action,
            source_path=source_ref,
            target_path=target_ref,
            permanent=permanent,
            overwrite=overwrite,
            item_count=int(item_count) if item_count is not None else None,
        )

    def _build_prompt(
        self,
        action: str,
        *,
        source_path: Path | None,
        target_path: Path | None,
        permanent: bool,
        reasons: list[str],
    ) -> str:
        if action == "delete":
            subject = source_path.name if source_path else "this item"
            mode = "permanently delete" if permanent else "move to the Recycle Bin"
            return f"Confirm: {mode} '{subject}'? Reason: {'; '.join(reasons)}."
        if action == "move":
            subject = source_path.name if source_path else "the item"
            destination = str(target_path or "the destination")
            return f"Confirm: move '{subject}' to '{destination}'? Reason: {'; '.join(reasons)}."
        if action == "rename":
            subject = source_path.name if source_path else "the item"
            destination = target_path.name if target_path else "the new name"
            return f"Confirm: rename '{subject}' to '{destination}'? Reason: {'; '.join(reasons)}."
        if action == "create":
            destination = str(target_path or "the target path")
            return f"Confirm: create or overwrite '{destination}'? Reason: {'; '.join(reasons)}."
        return f"Confirm file action '{action or 'unknown'}'? Reason: {'; '.join(reasons)}."

    def _normalize_path(self, value: str | Path | None) -> Path | None:
        if value in (None, ""):
            return None
        try:
            return self._resolver.resolve(value)
        except Exception:
            try:
                return self._resolver.normalize(str(value))
            except Exception:
                return None

    @staticmethod
    def _count_items(path: Path, *, limit: int = 500) -> int:
        count = 0
        stack = [path]
        while stack and count < limit:
            current = stack.pop()
            try:
                for child in current.iterdir():
                    count += 1
                    if count >= limit:
                        return count
                    if child.is_dir():
                        stack.append(child)
            except OSError:
                break
        return count


def is_dangerous(step: "PlanStep") -> bool:
    """
    Return True if the step requires a safety confirmation.

    Checks:
    1. step.estimated_risk == "high"
    2. ActionType.SYSTEM_CONTROL with shutdown/restart target
    3. ActionType.FILE_ACTION with file safety confirmation requirements
    """
    if step.estimated_risk == "high":
        return True

    action_name = str(step.params.get("action") or step.params.get("control") or step.target or "").strip().lower()
    if step.action.value == "system_control" and action_name in _DANGEROUS_SYSTEM_CONTROLS:
        return True
    if step.action.value == "file_action" and action_name in _DANGEROUS_FILE_VERBS:
        return True

    risk = default_permission_manager.classify_action(step.action.value, _step_permission_params(step))
    return risk == RiskLevel.DANGEROUS


class SafetyGate:
    """
    Synchronous safety gate used by ExecutionEngine.

    The gate can optionally route confirmation requests to a UI callback.
    If no callback is provided and ``confirm_dangerous_actions`` is True,
    the step is blocked by default.
    """

    def __init__(
        self,
        confirm_callback: Optional[Callable[[str], bool]] = None,
        *,
        permission_manager=None,
    ) -> None:
        self._confirm_cb = confirm_callback
        self._permission_manager = permission_manager or default_permission_manager

    def check(self, step: "PlanStep") -> tuple[bool, str]:
        permission = self._permission_manager.evaluate(step.action.value, _step_permission_params(step))
        if permission.decision == Decision.ALLOW:
            return True, ""

        description = self._describe_for_confirmation(step)
        if permission.decision == Decision.DENY:
            logger.warning("Permission denied for step %s: %s", step.id, permission.reason)
            return False, permission.reason

        token = self._permission_manager.request_confirmation(
            step.action.value,
            {
                "prompt": description,
                "reason": permission.reason,
                "risk_level": permission.risk_level,
                "params": _step_permission_params(step),
            },
        )
        logger.warning("Confirmation required for step %s (%s): %s", step.id, token, description)

        if self._confirm_cb is not None:
            allowed = self._confirm_cb(description)
            if allowed:
                self._permission_manager.approve(token)
                return True, ""
            self._permission_manager.deny(token)
            reason = f"User denied action: {description}"
            logger.info("Step %s denied by user confirmation", step.id)
            return False, reason

        reason = f"{description} Confirmation is required before execution."
        logger.warning("Step %s blocked pending confirmation token %s", step.id, token)
        return False, reason

    def set_confirm_callback(self, callback: Callable[[str], bool]) -> None:
        self._confirm_cb = callback

    def _describe_for_confirmation(self, step: "PlanStep") -> str:
        from core.plan_models import ActionType

        if step.action == ActionType.FILE_ACTION:
            try:
                decision = FileSafetyPolicy().evaluate_step(step)
                if decision.prompt:
                    return decision.prompt
            except Exception as exc:
                logger.debug("Failed to build file confirmation prompt for %s: %s", step.id, exc)
        return _describe_step(step)


def _describe_step(step: "PlanStep") -> str:
    """Build a short human-readable description for the confirmation dialog."""
    return f"Action '{step.action.value}' targeting '{step.target or '(none)'}' [risk={step.estimated_risk}]"


def _step_permission_params(step: "PlanStep") -> dict[str, Any]:
    params = dict(step.params)
    params.setdefault("target", step.target)
    if step.action.value == "file_action":
        params.setdefault("reference", step.target)
    if step.action.value == "system_control":
        params.setdefault("control", step.target or params.get("control"))
    return params
