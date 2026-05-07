"""
Centralized permission, confirmation, and temporary-approval framework.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from core import settings, state
from core.logger import get_audit_logger, get_logger
from core.path_resolver import PathResolver

logger = get_logger(__name__)
audit_logger = get_audit_logger("audit.permissions")

_YES_WORDS = {"yes", "y", "confirm", "confirmed", "approve", "approved", "continue", "proceed", "ok", "okay"}
_NO_WORDS = {"no", "n", "cancel", "deny", "denied", "stop", "abort", "never mind", "dont", "don't"}


class PermissionLevel(str, Enum):
    BASIC = "BASIC"
    TRUSTED = "TRUSTED"
    ADMIN_CONFIRM = "ADMIN_CONFIRM"

    @classmethod
    def coerce(cls, value: Any) -> "PermissionLevel":
        normalized = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
        aliases = {
            "BASIC": cls.BASIC,
            "TRUSTED": cls.TRUSTED,
            "ADMIN_CONFIRM": cls.ADMIN_CONFIRM,
            "ADMIN": cls.ADMIN_CONFIRM,
        }
        if normalized not in aliases:
            raise ValueError(f"Invalid permission level: {value!r}")
        return aliases[normalized]


class RiskLevel(str, Enum):
    SAFE = "SAFE"
    MEDIUM = "MEDIUM"
    DANGEROUS = "DANGEROUS"


class Decision(str, Enum):
    ALLOW = "ALLOW"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"
    DENY = "DENY"


@dataclass(slots=True)
class PermissionResult:
    allowed: bool
    decision: Decision
    permission_level: PermissionLevel
    risk_level: RiskLevel
    reason: str
    requires_confirmation: bool
    expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "decision": self.decision.value,
            "permission_level": self.permission_level.value,
            "risk_level": self.risk_level.value,
            "reason": self.reason,
            "requires_confirmation": self.requires_confirmation,
            "expires_at": self.expires_at,
        }


@dataclass(slots=True)
class PendingConfirmation:
    token: str
    action: str
    normalized_action: str
    permission_level: PermissionLevel
    risk_level: RiskLevel
    reason: str
    prompt: str
    details: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    callback: Callable[[], Any] | None = None
    status: str = "pending"

    def to_public_dict(self) -> dict[str, Any]:
        expires_at = None
        if self.expires_at is not None:
            expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.expires_at))
        return {
            "token": self.token,
            "action": self.action,
            "normalized_action": self.normalized_action,
            "permission_level": self.permission_level.value,
            "risk_level": self.risk_level.value,
            "reason": self.reason,
            "prompt": self.prompt,
            "details": dict(self.details),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.created_at)),
            "expires_at": expires_at,
            "status": self.status,
            "allow_temporary_approval": self.risk_level != RiskLevel.DANGEROUS and bool(settings.get("allow_temporary_approvals")),
        }


@dataclass(slots=True)
class TemporaryGrant:
    grant_id: str
    action: str
    normalized_action: str
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None

    def active(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at > now

    def to_public_dict(self) -> dict[str, Any]:
        expires_at = None
        if self.expires_at is not None:
            expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.expires_at))
        return {
            "grant_id": self.grant_id,
            "action": self.action,
            "normalized_action": self.normalized_action,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.created_at)),
            "expires_at": expires_at,
        }


class PermissionManager:
    """Policy engine for assistant actions."""

    def __init__(
        self,
        *,
        path_resolver: PathResolver | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._resolver = path_resolver or PathResolver()
        self._time_fn = time_fn or time.time
        self._lock = threading.RLock()
        self._pending: dict[str, PendingConfirmation] = {}
        self._grants: dict[str, TemporaryGrant] = {}
        self._confirmation_callback: Callable[[str], bool] | None = None
        self._sync_state_locked()

    def set_confirmation_callback(self, callback: Callable[[str], bool] | None) -> None:
        self._confirmation_callback = callback

    def get_current_level(self) -> PermissionLevel:
        raw_value = settings.get("permission_level")
        try:
            level = PermissionLevel.coerce(raw_value)
        except ValueError:
            logger.warning("Invalid permission level in settings: %r. Falling back to BASIC.", raw_value)
            level = PermissionLevel.BASIC
            try:
                settings.set("permission_level", level.value)
            except Exception:
                logger.debug("Failed to persist normalized permission level", exc_info=True)
        state.permission_level = level.value
        return level

    def set_level(self, level: PermissionLevel | str) -> PermissionLevel:
        resolved = PermissionLevel.coerce(level)
        settings.set("permission_level", resolved.value)
        state.permission_level = resolved.value
        self._audit("level_changed", level=resolved.value)
        return resolved

    def classify_action(self, action: Any, params: dict[str, Any] | None = None) -> RiskLevel:
        _normalized_action, risk_level, _reason, _summary = self._classify_details(action, params or {})
        return risk_level

    def evaluate(self, action: Any, params: dict[str, Any] | None = None) -> PermissionResult:
        payload = dict(params or {})
        with self._lock:
            self._prune_expired_locked()
            level = self.get_current_level()
            normalized_action, risk_level, reason, _summary = self._classify_details(action, payload)

            if not normalized_action:
                result = PermissionResult(
                    allowed=False,
                    decision=Decision.DENY,
                    permission_level=level,
                    risk_level=RiskLevel.DANGEROUS,
                    reason="Unknown action type. Blocking by default.",
                    requires_confirmation=False,
                )
                self._record_decision_locked(result, action, payload, normalized_action="")
                return result

            override = self._policy_override(normalized_action, payload, risk_level)
            grant = self._match_active_grant_locked(normalized_action)

            if grant is not None and risk_level != RiskLevel.DANGEROUS:
                expires_at = None
                if grant.expires_at is not None:
                    expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(grant.expires_at))
                result = PermissionResult(
                    allowed=True,
                    decision=Decision.ALLOW,
                    permission_level=level,
                    risk_level=risk_level,
                    reason=f"Temporary approval active for {normalized_action}.",
                    requires_confirmation=False,
                    expires_at=expires_at,
                )
                self._record_decision_locked(result, action, payload, normalized_action)
                return result

            if override is not None:
                decision = override
            elif risk_level == RiskLevel.SAFE:
                decision = Decision.ALLOW
            elif risk_level == RiskLevel.MEDIUM:
                decision = Decision.ALLOW if level in {PermissionLevel.TRUSTED, PermissionLevel.ADMIN_CONFIRM} else Decision.REQUIRE_CONFIRMATION
            else:
                decision = Decision.REQUIRE_CONFIRMATION

            result = PermissionResult(
                allowed=decision == Decision.ALLOW,
                decision=decision,
                permission_level=level,
                risk_level=risk_level,
                reason=reason,
                requires_confirmation=decision == Decision.REQUIRE_CONFIRMATION,
            )
            self._record_decision_locked(result, action, payload, normalized_action)
            return result

    def request_confirmation(
        self,
        action: Any,
        details: dict[str, Any],
        *,
        callback: Callable[[], Any] | None = None,
    ) -> str:
        payload = dict(details)
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        normalized_action, risk_level, reason, summary = self._classify_details(action, params)
        permission_level = self.get_current_level()
        timeout_seconds = max(10, int(settings.get("confirmation_timeout_seconds") or 60))
        expires_at = self._time_fn() + timeout_seconds
        token = uuid.uuid4().hex[:12]
        pending = PendingConfirmation(
            token=token,
            action=str(action.value if hasattr(action, "value") else action),
            normalized_action=payload.get("normalized_action") or normalized_action,
            permission_level=permission_level,
            risk_level=payload.get("risk_level") or risk_level,
            reason=str(payload.get("reason") or reason),
            prompt=str(payload.get("prompt") or summary),
            details={k: v for k, v in payload.items() if k not in {"callback"}},
            created_at=self._time_fn(),
            expires_at=expires_at,
            callback=callback,
        )
        with self._lock:
            self._pending[token] = pending
            self._sync_state_locked()
        self._audit(
            "confirmation_requested",
            token=token,
            action=pending.normalized_action,
            risk=pending.risk_level.value,
            prompt=pending.prompt,
            expires_at=pending.to_public_dict().get("expires_at"),
        )
        return token

    def approve(self, token: str) -> PendingConfirmation | None:
        with self._lock:
            self._prune_expired_locked()
            pending = self._pending.pop(token, None)
            if pending is None:
                self._audit("confirmation_approve_missing", token=token)
                return None
            pending.status = "approved"
            self._sync_state_locked()
        self._audit("confirmation_approved", token=token, action=pending.normalized_action, risk=pending.risk_level.value)
        return pending

    def deny(self, token: str) -> PendingConfirmation | None:
        with self._lock:
            self._prune_expired_locked()
            pending = self._pending.pop(token, None)
            if pending is None:
                self._audit("confirmation_deny_missing", token=token)
                return None
            pending.status = "denied"
            self._sync_state_locked()
        self._audit("confirmation_denied", token=token, action=pending.normalized_action, risk=pending.risk_level.value)
        state.last_denied_action = {
            "token": token,
            "action": pending.normalized_action,
            "risk_level": pending.risk_level.value,
            "reason": pending.reason,
        }
        return pending

    def grant_temporary(self, action: Any, duration: int | float | None) -> TemporaryGrant | None:
        normalized_action, risk_level, _reason, _summary = self._classify_details(action, {})
        if not normalized_action:
            return None
        if risk_level == RiskLevel.DANGEROUS:
            self._audit("temporary_grant_rejected", action=normalized_action, reason="dangerous_action")
            return None
        now = self._time_fn()
        expires_at = None if duration in (None, 0) else now + max(1, int(duration))
        grant = TemporaryGrant(
            grant_id=uuid.uuid4().hex[:10],
            action=str(action.value if hasattr(action, "value") else action),
            normalized_action=normalized_action,
            created_at=now,
            expires_at=expires_at,
        )
        with self._lock:
            self._grants[grant.grant_id] = grant
            self._sync_state_locked()
        self._audit("temporary_grant_created", action=normalized_action, grant_id=grant.grant_id, expires_at=grant.to_public_dict().get("expires_at"))
        return grant

    def revoke_temporary(self, grant_id: str | None = None, action: Any | None = None) -> int:
        removed = 0
        normalized_action = None
        if action is not None:
            normalized_action, _risk, _reason, _summary = self._classify_details(action, {})
        with self._lock:
            candidates = list(self._grants.items())
            for current_id, grant in candidates:
                if grant_id and current_id != grant_id:
                    continue
                if normalized_action and grant.normalized_action != normalized_action:
                    continue
                self._grants.pop(current_id, None)
                removed += 1
            self._sync_state_locked()
        if removed:
            self._audit("temporary_grant_revoked", action=normalized_action or "", grant_id=grant_id or "", count=removed)
        return removed

    def handle_confirmation_reply(self, text: str) -> Any | None:
        normalized = self._normalize_text(text)
        if normalized not in _YES_WORDS and normalized not in _NO_WORDS:
            return None

        with self._lock:
            self._prune_expired_locked()
            pending = self._latest_pending_locked()
            if pending is None:
                return {
                    "success": True,
                    "intent": "permission_confirmation",
                    "response": "I don't have any pending action to confirm.",
                    "error": "no_pending_confirmation",
                    "data": {"target_app": "permissions"},
                }
            token = pending.token

        if normalized in _NO_WORDS:
            denied = self.deny(token)
            if denied is None:
                return {
                    "success": False,
                    "intent": "permission_confirmation",
                    "response": "That confirmation is no longer available.",
                    "error": "confirmation_expired",
                    "data": {"target_app": "permissions"},
                }
            return {
                "success": False,
                "intent": "permission_confirmation",
                "response": f"Cancelled {denied.normalized_action.replace('_', ' ')}.",
                "error": "cancelled",
                "data": {"target_app": "permissions", "action": denied.normalized_action},
            }

        approved = self.approve(token)
        if approved is None:
            return {
                "success": False,
                "intent": "permission_confirmation",
                "response": "That confirmation expired. Please request the action again.",
                "error": "confirmation_expired",
                "data": {"target_app": "permissions"},
            }

        callback = approved.callback
        if callback is None:
            return {
                "success": True,
                "intent": "permission_confirmation",
                "response": f"Approved {approved.normalized_action.replace('_', ' ')}.",
                "data": {"target_app": "permissions", "action": approved.normalized_action},
            }

        try:
            result = callback()
            success = self._result_success(result)
            error = self._result_error(result)
            self.record_execution(approved.normalized_action, approved.details.get("params", {}), success=success, error=error)
            return result
        except Exception as exc:
            logger.error("Failed to execute approved action %s: %s", approved.normalized_action, exc)
            self.record_execution(approved.normal_action if hasattr(approved, "normal_action") else approved.normalized_action, approved.details.get("params", {}), success=False, error=str(exc))
            return {
                "success": False,
                "intent": "permission_confirmation",
                "response": f"Approved action failed: {exc}",
                "error": "approved_action_failed",
                "data": {"target_app": "permissions", "action": approved.normalized_action},
            }

    def latest_pending_confirmation(self) -> dict[str, Any] | None:
        with self._lock:
            self._prune_expired_locked()
            pending = self._latest_pending_locked()
            return pending.to_public_dict() if pending is not None else None

    def has_pending_confirmation(self) -> bool:
        with self._lock:
            self._prune_expired_locked()
            return bool(self._pending)

    def record_execution(self, action: Any, params: dict[str, Any] | None = None, *, success: bool, error: str = "") -> None:
        normalized_action, _risk_level, _reason, _summary = self._classify_details(action, params or {})
        self._audit(
            "action_executed",
            action=normalized_action or str(action),
            success=success,
            error=error,
        )

    def _record_decision_locked(
        self,
        result: PermissionResult,
        action: Any,
        params: dict[str, Any],
        normalized_action: str,
    ) -> None:
        payload = {
            "action": str(action.value if hasattr(action, "value") else action),
            "normalized_action": normalized_action,
            "params": self._safe_preview(params),
            **result.to_dict(),
        }
        state.last_permission_decision = payload
        if result.decision == Decision.DENY:
            state.last_denied_action = payload
        self._audit(
            "permission_evaluated",
            action=normalized_action or payload["action"],
            risk=result.risk_level.value,
            decision=result.decision.value,
            level=result.permission_level.value,
            reason=result.reason,
        )

    def _policy_override(
        self,
        normalized_action: str,
        params: dict[str, Any],
        risk_level: RiskLevel,
    ) -> Decision | None:
        if normalized_action in {"shutdown"} and settings.get("confirm_shutdown"):
            return Decision.REQUIRE_CONFIRMATION
        if normalized_action in {"restart"} and settings.get("confirm_restart"):
            return Decision.REQUIRE_CONFIRMATION
        if normalized_action in {"lock_pc"} and settings.get("confirm_lock"):
            return Decision.REQUIRE_CONFIRMATION
        if normalized_action in {"send_message"} and settings.get("confirm_before_sending_message") and not settings.get("whatsapp_no_confirmation"):
            return Decision.REQUIRE_CONFIRMATION
        if normalized_action in {"delete_file", "permanent_delete", "delete_folder_many_files"} and settings.get("confirm_delete"):
            return Decision.REQUIRE_CONFIRMATION
        if params.get("overwrite") and settings.get("confirm_overwrite"):
            return Decision.REQUIRE_CONFIRMATION
        if risk_level == RiskLevel.DANGEROUS:
            return Decision.REQUIRE_CONFIRMATION
        return None

    def _classify_details(self, action: Any, params: dict[str, Any]) -> tuple[str, RiskLevel, str, str]:
        action_name = self._normalize_action_name(action)
        if not action_name:
            return "", RiskLevel.DANGEROUS, "Unknown action type.", "Unknown action."

        file_result = self._classify_file_action(action_name, params)
        if file_result is not None:
            return file_result

        system_result = self._classify_system_action(action_name, params)
        if system_result is not None:
            return system_result

        app_result = self._classify_app_action(action_name, params)
        if app_result is not None:
            return app_result

        plugin_result = self._classify_plugin_action(action_name, params)
        if plugin_result is not None:
            return plugin_result

        skill_result = self._classify_skill_action(action_name, params)
        if skill_result is not None:
            return skill_result

        safe_actions = {
            "open_app": "Opening an application is a safe action.",
            "search": "Web searches are safe to auto-run.",
            "read_screen": "Reading the screen is safe.",
            "new_tab": "Opening a new tab is safe.",
            "pause_music": "Media pause is safe.",
            "play_music": "Media playback is safe.",
            "clipboard_history": "Reading clipboard history is safe.",
            "wait": "Waiting does not change system state.",
            "help": "Help is informational.",
            "time_query": "Time queries are informational.",
            "ocr": "OCR only reads content.",
            "awareness": "Awareness only reads content.",
        }
        if action_name in safe_actions:
            return action_name, RiskLevel.SAFE, safe_actions[action_name], f"Run {action_name.replace('_', ' ')}?"

        medium_actions = {
            "close_app": "Closing an app can disrupt user work.",
            "click": "Click automation affects the active application.",
            "type": "Typing automation affects the active application.",
            "send_message": "Sending a message has external side effects.",
        }
        if action_name in medium_actions:
            return action_name, RiskLevel.MEDIUM, medium_actions[action_name], f"Run {action_name.replace('_', ' ')}?"

        return "", RiskLevel.DANGEROUS, "Unknown action type. Blocking by default.", "Unknown action."

    def _classify_file_action(self, action_name: str, params: dict[str, Any]) -> tuple[str, RiskLevel, str, str] | None:
        from core import entities

        if action_name not in {"file_action", "fileskill", "file"}:
            return None

        file_action = self._normalize_text(
            params.get("action")
            or params.get("operation")
            or (params.get("entities") or {}).get("action")
        )
        if not file_action and isinstance(params.get("command"), str):
            extracted = entities.extract_file_action(str(params.get("command") or ""))
            file_action = self._normalize_text(extracted.get("action"))
            params = {**params, **extracted}

        source_ref = params.get("resolved_source") or params.get("source_path") or params.get("path") or params.get("reference") or params.get("filename")
        target_ref = params.get("target_path") or params.get("destination") or params.get("new_name")
        permanent = bool(params.get("permanent"))
        overwrite = bool(params.get("overwrite"))
        item_count = self._safe_int(params.get("item_count"))
        bulk_threshold = int(settings.get("file_bulk_delete_confirmation_threshold") or 25)

        protected = any(self._is_protected_path(candidate) for candidate in (source_ref, target_ref) if candidate)
        if protected:
            return (
                "system_protected_write",
                RiskLevel.DANGEROUS,
                "The action touches a protected Windows directory.",
                "This action writes to or modifies a protected system location.",
            )

        if file_action == "open":
            return "open_file", RiskLevel.SAFE, "Opening a file is a read-like action.", "Open the selected file or folder?"
        if file_action == "create":
            if overwrite:
                return "overwrite_file", RiskLevel.MEDIUM, "Overwriting a file can destroy existing data.", "Overwrite the existing file?"
            return "create_file", RiskLevel.SAFE, "Creating a file in user space is safe.", "Create the requested file?"
        if file_action == "rename":
            return "rename_file", RiskLevel.MEDIUM, "Renaming changes filesystem state.", "Rename the selected file or folder?"
        if file_action == "move":
            return "move_file", RiskLevel.MEDIUM, "Moving files changes filesystem state.", "Move the selected file or folder?"
        if file_action == "delete":
            if permanent:
                return "permanent_delete", RiskLevel.DANGEROUS, "Permanent delete removes data without recovery.", "Permanently delete the selected item?"
            if item_count is not None and item_count >= bulk_threshold:
                return "delete_folder_many_files", RiskLevel.DANGEROUS, "Bulk delete affects many files.", "Delete a folder containing many files?"
            return "delete_file", RiskLevel.MEDIUM, "Deleting to the Recycle Bin changes filesystem state.", "Move the selected item to the Recycle Bin?"

        return None

    def _classify_system_action(self, action_name: str, params: dict[str, Any]) -> tuple[str, RiskLevel, str, str] | None:
        if action_name not in {"system_control", "systemskill", "system"}:
            return None

        control = self._normalize_text(params.get("action") or params.get("control") or "")
        if not control and isinstance(params.get("command"), str):
            from core.system_controls import parse_system_command

            parsed = parse_system_command(str(params.get("command") or ""))
            if parsed is not None:
                control = self._normalize_text(parsed.action or parsed.control)

        safe_controls = {
            "get_volume": "Reading volume is safe.",
            "set_volume": "Volume changes are reversible and safe.",
            "volume_up": "Volume changes are reversible and safe.",
            "volume_down": "Volume changes are reversible and safe.",
            "mute": "Muting is reversible and safe.",
            "unmute": "Muting is reversible and safe.",
            "toggle_mute": "Muting is reversible and safe.",
            "get_brightness": "Reading brightness is safe.",
            "set_brightness": "Brightness changes are reversible and safe.",
            "brightness_up": "Brightness changes are reversible and safe.",
            "brightness_down": "Brightness changes are reversible and safe.",
            "cancel_shutdown": "Cancelling shutdown is safe.",
            "wifi_status": "Reading Wi-Fi status is safe.",
            "bluetooth_status": "Reading Bluetooth status is safe.",
        }
        if control in safe_controls:
            return control, RiskLevel.SAFE, safe_controls[control], f"Run {control.replace('_', ' ')}?"

        medium_controls = {
            "wifi_off": "Turning off Wi-Fi interrupts connectivity.",
            "wifi_on": "Changing Wi-Fi state affects connectivity.",
            "bluetooth_off": "Changing Bluetooth state affects connectivity.",
            "bluetooth_on": "Changing Bluetooth state affects connectivity.",
            "lock_pc": "Locking the PC disrupts the active session.",
        }
        if control in medium_controls:
            return control, RiskLevel.MEDIUM, medium_controls[control], f"Run {control.replace('_', ' ')}?"

        dangerous_controls = {
            "shutdown": "Shutdown can terminate work and power off the machine.",
            "restart": "Restart can terminate work and reboot the machine.",
            "reboot": "Restart can terminate work and reboot the machine.",
            "sleep": "Sleep disrupts the active session immediately.",
            "hibernate": "Hibernate disrupts the active session immediately.",
            "logoff": "Signing out ends the active session immediately.",
            "sign_out": "Signing out ends the active session immediately.",
        }
        if control in dangerous_controls:
            return control, RiskLevel.DANGEROUS, dangerous_controls[control], f"Run {control.replace('_', ' ')}?"

        return None

    def _classify_app_action(self, action_name: str, params: dict[str, Any]) -> tuple[str, RiskLevel, str, str] | None:
        if action_name != "app_action":
            return None

        app_name = self._normalize_text(params.get("app") or params.get("target_app") or params.get("target"))
        operation = self._normalize_text(params.get("operation") or params.get("action"))

        if app_name == "whatsapp" and operation in {"send", "send_message", "message"}:
            return "send_whatsapp_message", RiskLevel.MEDIUM, "Sending a message has external side effects.", "Send the WhatsApp message?"
        if operation in {"close", "close_app", "close_tab", "close_window"}:
            return "close_app", RiskLevel.MEDIUM, "Closing an app or tab can disrupt user work.", "Close the active app or tab?"
        if operation in {"new_tab", "search", "open_result", "back", "forward", "refresh"}:
            return operation, RiskLevel.SAFE, "This browser action is safe.", f"Run {operation.replace('_', ' ')}?"
        return "app_action", RiskLevel.MEDIUM, "App actions can affect application state.", "Run the app action?"

    def _classify_plugin_action(self, action_name: str, params: dict[str, Any]) -> tuple[str, RiskLevel, str, str] | None:
        if action_name not in {"plugin_execute", "plugin_load", "plugin_enable"} and not action_name.startswith("plugin_"):
            return None

        permissions = {
            self._normalize_text(permission)
            for permission in (params.get("permissions_requested") or params.get("permissions") or [])
            if permission
        }
        approved = bool(params.get("permission_approved"))
        plugin_name = str(params.get("plugin_name") or params.get("plugin_id") or "plugin").strip()

        if approved:
            return (
                "plugin_execute",
                RiskLevel.SAFE,
                f"Permissions were approved for {plugin_name}.",
                f"Run {plugin_name}?",
            )
        if permissions & {"shell", "system", "camera", "microphone"}:
            return (
                "plugin_execute",
                RiskLevel.DANGEROUS,
                f"{plugin_name} requests high-risk plugin permissions.",
                f"Allow {plugin_name} to run with high-risk permissions?",
            )
        if permissions & {"filesystem", "network", "automation", "clipboard", "screen", "browser", "memory", "audio"}:
            return (
                "plugin_execute",
                RiskLevel.MEDIUM,
                f"{plugin_name} requests plugin permissions that can affect local resources.",
                f"Allow {plugin_name} to run?",
            )
        return "plugin_execute", RiskLevel.SAFE, f"{plugin_name} has no sensitive permissions.", f"Run {plugin_name}?"

    def _classify_skill_action(self, action_name: str, params: dict[str, Any]) -> tuple[str, RiskLevel, str, str] | None:
        skill_name = action_name
        command = self._normalize_text(params.get("command"))
        intent = self._normalize_text(params.get("intent"))

        safe_skills = {
            "browserskill": ("search", "Browser skill actions are safe by default."),
            "chromeskill": ("browser_action", "Browser skill actions are safe by default."),
            "youtubeskill": ("youtube_action", "YouTube skill actions are safe by default."),
            "musicskill": ("music_action", "Media actions are safe by default."),
            "ocrskill": ("read_screen", "OCR only reads content."),
            "awarenessskill": ("read_screen", "Awareness only reads content."),
            "clipboardskill": ("clipboard_history", "Clipboard history viewing is safe."),
            "reminderskill": ("reminder_action", "Reminder actions are safe."),
        }
        if skill_name in safe_skills:
            normalized, reason = safe_skills[skill_name]
            return normalized, RiskLevel.SAFE, reason, f"Run {normalized.replace('_', ' ')}?"

        if skill_name == "whatsappskill":
            if "send" in command or intent == "send_message":
                return "send_message", RiskLevel.MEDIUM, "Sending a message has external side effects.", "Send the message?"
            return "whatsapp_action", RiskLevel.MEDIUM, "WhatsApp actions can contact other people.", "Run the WhatsApp action?"

        if skill_name == "clicktextskill":
            return "click", RiskLevel.MEDIUM, "Click-by-text automation affects the active application.", "Click the requested target?"

        if skill_name.endswith("skill"):
            return "skill_execute", RiskLevel.SAFE, "Local in-process skill execution is allowed by default.", "Run the requested skill?"

        return None

    def _match_active_grant_locked(self, normalized_action: str) -> TemporaryGrant | None:
        now = self._time_fn()
        for grant in self._grants.values():
            if grant.normalized_action == normalized_action and grant.active(now):
                return grant
        return None

    def _latest_pending_locked(self) -> PendingConfirmation | None:
        if not self._pending:
            return None
        return max(self._pending.values(), key=lambda item: item.created_at)

    def _prune_expired_locked(self) -> None:
        now = self._time_fn()
        pending_removed = []
        for token, pending in list(self._pending.items()):
            if pending.expires_at is not None and pending.expires_at <= now:
                pending.status = "expired"
                pending_removed.append((token, pending.normalized_action))
                self._pending.pop(token, None)
        grants_removed = []
        for grant_id, grant in list(self._grants.items()):
            if grant.expires_at is not None and grant.expires_at <= now:
                grants_removed.append((grant_id, grant.normalized_action))
                self._grants.pop(grant_id, None)
        if pending_removed or grants_removed:
            self._sync_state_locked()
        for token, action in pending_removed:
            self._audit("confirmation_expired", token=token, action=action)
        for grant_id, action in grants_removed:
            self._audit("temporary_grant_expired", grant_id=grant_id, action=action)

    def _sync_state_locked(self) -> None:
        state.permission_level = self.get_current_level().value
        state.pending_confirmations = {token: pending.to_public_dict() for token, pending in self._pending.items()}
        latest = self._latest_pending_locked()
        state.pending_confirmation = latest.to_public_dict() if latest is not None else {}
        state.temporary_grants = [grant.to_public_dict() for grant in self._grants.values()]

    def _safe_preview(self, params: dict[str, Any]) -> dict[str, Any]:
        preview: dict[str, Any] = {}
        for key, value in params.items():
            if key in {"content", "message_body", "text"} and isinstance(value, str) and len(value) > 120:
                preview[key] = value[:117] + "..."
                continue
            if isinstance(value, Path):
                preview[key] = str(value)
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                preview[key] = value
                continue
            if isinstance(value, dict):
                preview[key] = self._safe_preview(value)
                continue
            preview[key] = str(value)
        return preview

    def _is_protected_path(self, candidate: Any) -> bool:
        if candidate in (None, ""):
            return False
        try:
            path = candidate if isinstance(candidate, Path) else self._resolver.resolve(str(candidate))
        except Exception:
            try:
                path = self._resolver.normalize(str(candidate))
            except Exception:
                return False
        return self._resolver.is_system_directory(path)

    @staticmethod
    def _normalize_action_name(action: Any) -> str:
        if action is None:
            return ""
        value = action.value if hasattr(action, "value") else action
        return PermissionManager._normalize_text(value)

    @staticmethod
    def _normalize_text(text: Any) -> str:
        return " ".join(str(text or "").strip().lower().replace("-", "_").replace(" ", "_").split())

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _result_success(result: Any) -> bool:
        if isinstance(result, dict):
            return bool(result.get("success"))
        return bool(getattr(result, "success", False))

    @staticmethod
    def _result_error(result: Any) -> str:
        if isinstance(result, dict):
            return str(result.get("error") or "")
        return str(getattr(result, "error", "") or "")

    def _audit(self, event: str, **payload: Any) -> None:
        if not settings.get("audit_log_enabled"):
            return
        message = {"event": event, **payload}
        try:
            audit_logger.info(json.dumps(message, sort_keys=True, default=str))
        except Exception:
            logger.debug("Failed to write permission audit log", exc_info=True)


permission_manager = PermissionManager()
