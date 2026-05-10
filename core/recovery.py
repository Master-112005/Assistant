"""
Error recovery planning, fallback execution, and recovery memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable

from core import settings, state
from core.analytics import analytics
from core.errors import (
    ActionTimeoutError,
    AmbiguousMatchError,
    AppNotFoundError,
    AssistantError,
    DeviceUnavailableError,
    ExecutionError,
    NetworkError,
    NotSupportedError,
    PermissionDeniedError,
    ValidationError,
    ensure_assistant_error,
)
from core.fallback import (
    fallback_input_mode,
    find_alternative_app,
    find_alternative_browser,
    find_similar_contact,
    reduced_capability_mode,
)
from core.logger import get_logger
from core.paths import DATA_DIR

logger = get_logger(__name__)

_MEMORY_PATH = DATA_DIR / "recovery_memory.json"
_SAFE_AUTO_ACTIONS = {"open_app", "search", "switch_input_mode", "acknowledge_opened", "reduced_mode"}


@dataclass(slots=True)
class RecoveryOption:
    label: str
    action: str
    params_json: dict[str, Any] = field(default_factory=dict)
    recommended: bool = False
    option_id: str = ""

    def __post_init__(self) -> None:
        if not self.option_id:
            base = self.label.strip().lower().replace(" ", "_")
            self.option_id = base or self.action

    def to_dict(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "label": self.label,
            "action": self.action,
            "params_json": dict(self.params_json),
            "recommended": self.recommended,
        }


@dataclass(slots=True)
class RecoveryPlan:
    error_type: str
    summary: str
    options: list[RecoveryOption]
    auto_retry: bool = False
    fallback_action: dict[str, Any] | None = None
    requires_user_choice: bool = True
    confidence: float = 0.0
    memory_key: str = ""
    error_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "error_code": self.error_code,
            "summary": self.summary,
            "options": [option.to_dict() for option in self.options],
            "auto_retry": self.auto_retry,
            "fallback_action": dict(self.fallback_action or {}),
            "requires_user_choice": self.requires_user_choice,
            "confidence": round(float(self.confidence), 3),
            "memory_key": self.memory_key,
        }


@dataclass(slots=True)
class RecoveryOutcome:
    resolved: bool
    result: dict[str, Any]
    plan: RecoveryPlan | None = None


class RecoveryManager:
    """Central recovery planner with retry, fallback memory, and circuit breakers."""

    def __init__(
        self,
        *,
        action_executor: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
        memory_path: Path | None = None,
    ) -> None:
        self._action_executor = action_executor
        self._memory_path = Path(memory_path) if memory_path is not None else _MEMORY_PATH
        self._lock = threading.RLock()
        self._active_plan: RecoveryPlan | None = None
        self._active_context: dict[str, Any] = {}
        self._memory = self._load_memory()
        self._stats = {
            "plans_created": 0,
            "options_executed": 0,
            "auto_retries": 0,
            "fallback_successes": 0,
            "fallback_failures": 0,
            "remembered_choices": 0,
        }
        self._circuit_breakers: dict[str, dict[str, float | int]] = {}
        self._sync_state()

    def handle(self, error: Exception | dict[str, Any] | str, command_context: dict[str, Any]) -> RecoveryOutcome:
        try:
            normalized = self.classify(error, command_context)
            state.last_error = normalized.message
            logger.error("Error captured: %s %s", type(normalized).__name__, normalized.code)
            analytics.record_error(
                normalized.code,
                normalized.message,
                module=__name__,
                context=str(command_context.get("command") or command_context.get("raw_input") or ""),
                command_context=json.dumps(command_context, ensure_ascii=True, default=str),
                source=str(command_context.get("source") or ""),
                metadata={
                    "recoverable": normalized.recoverable,
                    "error_type": type(normalized).__name__,
                },
            )
            analytics.increment_feature(f"recovery:error:{normalized.code}", metadata={"type": type(normalized).__name__})

            plan = self.build_plan(normalized, command_context)
            self._stats["plans_created"] += 1
            state.last_recovery_plan = plan.to_dict()
            self._sync_state()
            logger.info("Recovery plan created: %s", plan.error_type)

            if plan.auto_retry and self._retry_allowed(plan.memory_key):
                retry_action = dict(plan.fallback_action or {"action": "retry_original", "params_json": {}})
                self._stats["auto_retries"] += 1
                retry_result = self.retry(retry_action, command_context)
                if retry_result.get("success"):
                    self._stats["fallback_successes"] += 1
                    self._sync_state()
                    analytics.increment_feature(f"recovery:retry_success:{normalized.code}")
                    return RecoveryOutcome(
                        resolved=True,
                        result=self._wrap_success(plan.summary, retry_result, source="retry"),
                        plan=plan,
                    )
                self._stats["fallback_failures"] += 1
                self._record_breaker_failure(plan.memory_key)
                self._sync_state()
                analytics.increment_feature(f"recovery:retry_failed:{normalized.code}")

            remembered = self._preferred_option_for_plan(plan)
            if remembered is not None and remembered.action in _SAFE_AUTO_ACTIONS and not plan.requires_user_choice:
                result = self._execute_option(remembered, command_context, remember=False)
                if result.get("success"):
                    self._stats["fallback_successes"] += 1
                    self._sync_state()
                    return RecoveryOutcome(
                        resolved=True,
                        result=self._wrap_success(plan.summary, result, source="memory"),
                        plan=plan,
                    )
                self._stats["fallback_failures"] += 1
                self._sync_state()

            self._store_plan(plan, command_context)
            return RecoveryOutcome(resolved=False, result=self._plan_result(plan), plan=plan)
        except Exception as exc:
            logger.exception("Recovery manager failed", exc=exc)
            analytics.record_error(
                "recovery_manager_error",
                str(exc),
                exc=exc,
                module=__name__,
                context=str(command_context.get("command") or command_context.get("raw_input") or ""),
                source=str(command_context.get("source") or ""),
            )
            self.clear_pending()
            state.last_error = str(error) if error else "The action failed."
            self._sync_state()
            return RecoveryOutcome(
                resolved=False,
                result=self._simple_result(
                    False,
                    "error",
                    "The action failed, and recovery could not be planned. You can try again or continue with text input.",
                    error="recovery_manager_error",
                    data={"target_app": "recovery", "focus_text_input": True, "speak_response": False},
                ),
                plan=None,
            )

    def classify(self, error: Exception | dict[str, Any] | str, command_context: dict[str, Any] | None = None) -> AssistantError:
        context = dict(command_context or {})
        normalized = ensure_assistant_error(error, context=context)
        payload = dict(normalized.context)
        code = str(payload.get("error") or payload.get("code") or normalized.code).strip().lower()
        intent = str(payload.get("intent") or context.get("intent") or context.get("detected_intent") or "").strip().lower()
        message = str(payload.get("message") or payload.get("response") or normalized.message).strip() or normalized.message

        if isinstance(normalized, (AppNotFoundError, PermissionDeniedError, DeviceUnavailableError, NetworkError, ActionTimeoutError, NotSupportedError, AmbiguousMatchError, ValidationError)):
            return normalized

        if code in {"app_not_found", "browser_not_found", "path_not_found"} or "couldn't find an installed app" in message.lower():
            return AppNotFoundError(
                message=message or "The requested app is not installed.",
                code=code or "app_not_found",
                context=payload,
                recoverable=True,
            )
        if code in {"contact_not_found", "empty_contact", "contact_missing"}:
            return ValidationError(message=message, code=code or "contact_not_found", context=payload, recoverable=True)
        if code in {"whatsapp_disambiguation", "ambiguous_match"} or payload.get("matches"):
            return AmbiguousMatchError(message=message, code=code or "ambiguous_match", context=payload, recoverable=True)
        if code in {"permission_denied"}:
            return PermissionDeniedError(message=message, code=code, context=payload, recoverable=True)
        if code in {"timeout", "action_timeout", "launch_timeout"}:
            return ActionTimeoutError(message=message, code=code, context=payload, recoverable=True)
        if code in {"backend_unavailable", "device_unavailable", "microphone_unavailable", "launcher_unavailable"}:
            return DeviceUnavailableError(message=message, code=code, context=payload, recoverable=True)
        if code in {"unsupported", "unsupported_app_type", "safe_delete_unavailable", "music_provider_unavailable", "multi_action_failed"}:
            return NotSupportedError(message=message, code=code or "not_supported", context=payload, recoverable=True)
        if code in {"network_error", "search_failed"}:
            return NetworkError(message=message, code=code, context=payload, recoverable=True)
        lowered_message = message.lower()
        if "microphone" in lowered_message or "audio device" in lowered_message:
            return DeviceUnavailableError(message=message, code=code or "microphone_unavailable", context=payload, recoverable=True)
        if "permission" in lowered_message or "access is denied" in lowered_message:
            return PermissionDeniedError(message=message, code=code or "permission_denied", context=payload, recoverable=True)
        if isinstance(error, TimeoutError):
            return ActionTimeoutError(message=str(error), context=payload, recoverable=True)
        if isinstance(error, PermissionError):
            return PermissionDeniedError(message=str(error), context=payload, recoverable=True)
        return ExecutionError(message=message or "The action failed.", code=code or normalized.code, context=payload, recoverable=True)

    def build_plan(self, error: AssistantError, context: dict[str, Any]) -> RecoveryPlan:
        error_context = dict(error.context)
        original_command = str(context.get("command") or context.get("raw_input") or "").strip()
        requested_app = str(error_context.get("requested_app") or error_context.get("app_name") or error_context.get("target_app") or "").strip()
        memory_key = self._memory_key(error, requested_app or original_command or error.code)
        options: list[RecoveryOption] = []
        fallback_action: dict[str, Any] | None = None
        auto_retry = False
        requires_user_choice = True
        confidence = 0.75

        if isinstance(error, AppNotFoundError):
            category = self._app_category_for_error(error, original_command)
            if category == "browser":
                alternatives = find_alternative_browser(requested_app, preferred_browser=str(context.get("preferred_browser") or ""))
                for alternative in alternatives:
                    options.append(
                        RecoveryOption(
                            label=f"Open {alternative['display_name']}",
                            action="open_app",
                            params_json={"app_name": alternative["app_name"]},
                            recommended=not options,
                            option_id=f"open_{alternative['app_name']}",
                        )
                    )
                if original_command:
                    query = self._query_from_command(original_command)
                    if query:
                        options.append(
                            RecoveryOption(
                                label="Search in available browser",
                                action="search",
                                params_json={"query": query},
                                recommended=not options,
                                option_id="search_in_browser",
                            )
                        )
            else:
                alternatives = find_alternative_app(category, exclude=requested_app or original_command)
                for alternative in alternatives:
                    options.append(
                        RecoveryOption(
                            label=f"Open {alternative['display_name']}",
                            action="open_app",
                            params_json={"app_name": alternative["app_name"]},
                            recommended=not options,
                            option_id=f"open_{alternative['app_name']}",
                        )
                    )
                browser_fallbacks = find_alternative_browser(limit=1)
                if browser_fallbacks:
                    query = self._query_from_command(original_command or requested_app)
                    if query:
                        options.append(
                            RecoveryOption(
                                label=f"Search for {query}",
                                action="search",
                                params_json={"query": query, "target": browser_fallbacks[0]["app_name"]},
                                recommended=not options,
                                option_id="search_web_fallback",
                            )
                        )
            if not options:
                options.append(RecoveryOption(label="Cancel", action="cancel_recovery", option_id="cancel"))
            summary = error.message or f"{requested_app or 'That app'} is not installed."
            confidence = 0.93 if options else 0.60

        elif isinstance(error, AmbiguousMatchError):
            matches = list(error_context.get("matches") or error_context.get("choices") or [])
            for index, match in enumerate(matches[:5], start=1):
                label = str(match.get("display_name") if isinstance(match, dict) else match).strip() or f"Option {index}"
                command = self._replace_contact_in_command(original_command, label, context)
                options.append(
                    RecoveryOption(
                        label=label,
                        action="retry_command",
                        params_json={"command": command},
                        recommended=index == 1,
                        option_id=f"choice_{index}",
                    )
                )
            summary = error.message or "I found multiple matches. Choose one."
            confidence = 0.90

        elif isinstance(error, ValidationError) and error.code == "contact_not_found":
            similar = find_similar_contact(str(context.get("contact") or error_context.get("contact") or requested_app), platform=str(context.get("platform") or ""))
            for index, match in enumerate(similar[:5], start=1):
                options.append(
                    RecoveryOption(
                        label=match["name"],
                        action="retry_command",
                        params_json={"command": self._replace_contact_in_command(original_command, match["name"], context)},
                        recommended=index == 1,
                        option_id=f"similar_contact_{index}",
                    )
                )
            if not options:
                options.append(RecoveryOption(label="Cancel", action="cancel_recovery", option_id="cancel"))
            summary = error.message or "I could not find that contact."
            confidence = 0.82 if similar else 0.55

        elif isinstance(error, DeviceUnavailableError):
            if self._device_name(error, context) == "microphone":
                options = [
                    RecoveryOption(label="Switch to text input", action="switch_input_mode", params_json={"mode": "text"}, recommended=True, option_id="switch_text"),
                    RecoveryOption(label="Retry microphone", action="retry_original", params_json={}, recommended=False, option_id="retry_microphone"),
                    RecoveryOption(label="Cancel", action="cancel_recovery", option_id="cancel"),
                ]
                summary = error.message or "Microphone is unavailable."
                confidence = 0.94
            else:
                options = [
                    RecoveryOption(label="Retry", action="retry_original", params_json={}, recommended=True, option_id="retry"),
                    RecoveryOption(label="Cancel", action="cancel_recovery", option_id="cancel"),
                ]
                summary = error.message
                confidence = 0.74

        elif isinstance(error, NetworkError):
            auto_retry = bool(settings.get("auto_retry_transient_errors", True))
            fallback_action = {"action": "retry_original", "params_json": {}}
            query = self._query_from_command(original_command)
            options = [
                RecoveryOption(label="Retry", action="retry_original", params_json={}, recommended=True, option_id="retry"),
            ]
            if query:
                options.append(RecoveryOption(label="Open browser search", action="search", params_json={"query": query}, option_id="browser_search"))
            cached_result = error_context.get("cached_result") or context.get("cached_result")
            if isinstance(cached_result, dict):
                options.append(RecoveryOption(label="Use cached result", action="use_cached_result", params_json={"result": cached_result}, option_id="cached"))
            options.append(RecoveryOption(label="Cancel", action="cancel_recovery", option_id="cancel"))
            summary = error.message or "The online request failed."
            requires_user_choice = False
            confidence = 0.78

        elif isinstance(error, PermissionDeniedError):
            options = [
                RecoveryOption(label="Explain limitation", action="explain_limitation", params_json={"message": error.message}, recommended=True, option_id="explain"),
                RecoveryOption(label="Cancel", action="cancel_recovery", option_id="cancel"),
            ]
            summary = error.message or "That action is blocked by the current permissions."
            confidence = 0.72

        elif isinstance(error, ActionTimeoutError):
            timed_out_intent = str(error_context.get("intent") or context.get("intent") or context.get("detected_intent") or "").strip().lower()
            auto_retry = bool(settings.get("auto_retry_transient_errors", True)) and timed_out_intent != "open_app"
            fallback_action = {"action": "retry_original", "params_json": {}}
            requires_user_choice = timed_out_intent == "open_app"
            options = [
                RecoveryOption(label="Retry", action="retry_original", params_json={}, recommended=True, option_id="retry"),
                RecoveryOption(label="It opened already", action="acknowledge_opened", option_id="opened"),
                RecoveryOption(label="Cancel", action="cancel_recovery", option_id="cancel"),
            ]
            summary = error.message or "The action timed out."
            confidence = 0.84

        elif isinstance(error, NotSupportedError):
            degraded = reduced_capability_mode(str(context.get("feature") or error.code))
            options = [
                RecoveryOption(label="Use reduced mode", action="reduced_mode", params_json=degraded, recommended=True, option_id="reduced"),
                RecoveryOption(label="Cancel", action="cancel_recovery", option_id="cancel"),
            ]
            summary = error.message or degraded["message"]
            confidence = 0.80

        else:
            options = [
                RecoveryOption(label="Retry", action="retry_original", params_json={}, recommended=True, option_id="retry"),
                RecoveryOption(label="Cancel", action="cancel_recovery", option_id="cancel"),
            ]
            summary = error.message or "The action failed."
            confidence = 0.50

        preferred = self._memory.get("preferences", {}).get(memory_key, {})
        preferred_option_id = str(preferred.get("preferred_option_id") or "")
        if preferred_option_id:
            for option in options:
                option.recommended = option.option_id == preferred_option_id
        if preferred.get("count", 0) >= 2:
            chosen = next((item for item in options if item.option_id == preferred_option_id), None)
            if chosen is not None and chosen.action in _SAFE_AUTO_ACTIONS:
                requires_user_choice = False
                fallback_action = chosen.to_dict()
                confidence = max(confidence, 0.94)

        if not any(option.action == "cancel_recovery" for option in options):
            options.append(RecoveryOption(label="Cancel", action="cancel_recovery", option_id="cancel"))

        return RecoveryPlan(
            error_type=type(error).__name__,
            error_code=error.code,
            summary=summary.rstrip(".") + ".",
            options=options,
            auto_retry=auto_retry,
            fallback_action=fallback_action,
            requires_user_choice=requires_user_choice,
            confidence=confidence,
            memory_key=memory_key,
        )

    def execute_option(self, option_id: str) -> dict[str, Any]:
        try:
            with self._lock:
                plan = self._active_plan
                context = dict(self._active_context)
            if plan is None:
                return self._simple_result(False, "recovery", "No recovery option is pending.", error="no_pending_recovery")
            option = next((item for item in plan.options if item.option_id == option_id), None)
            if option is None:
                return self._simple_result(False, "recovery", "That recovery option is not available.", error="invalid_recovery_option")
            result = self._execute_option(option, context)
            if result.get("success") and option.action in _SAFE_AUTO_ACTIONS | {"retry_original", "retry_command"}:
                self.remember_successful_recovery(plan.memory_key, option)
            self.clear_pending()
            self._sync_state()
            return self._wrap_success(plan.summary, result, source="option") if result.get("success") else result
        except Exception as exc:
            logger.exception("Recovery option execution failed", exc=exc)
            self.clear_pending()
            self._sync_state()
            return self._simple_result(
                False,
                "error",
                "The recovery action failed. You can try the command again.",
                error="recovery_option_failed",
                data={"target_app": "recovery", "focus_text_input": True, "speak_response": False},
            )

    def consume_reply(self, text: str) -> dict[str, Any] | None:
        with self._lock:
            plan = self._active_plan
        if plan is None:
            return None
        normalized = " ".join(str(text or "").strip().lower().split())
        if not normalized:
            return None
        if normalized in {"yes", "y", "ok", "okay", "sure"}:
            preferred = next((option for option in plan.options if option.recommended), plan.options[0] if plan.options else None)
            if preferred is not None:
                return self.execute_option(preferred.option_id)
        if normalized in {"cancel", "stop", "never mind", "no"}:
            self.clear_pending()
            return self._simple_result(False, "recovery_cancelled", "Cancelled the recovery.", error="cancelled")
        if normalized.isdigit():
            index = int(normalized)
            if 1 <= index <= len(plan.options):
                return self.execute_option(plan.options[index - 1].option_id)
        for option in plan.options:
            if normalized == option.option_id or normalized == option.label.strip().lower():
                return self.execute_option(option.option_id)
        return None

    def retry(self, action: dict[str, Any], command_context: dict[str, Any]) -> dict[str, Any]:
        if self._action_executor is None:
            return self._simple_result(False, "recovery_retry", "Retry is unavailable.", error="retry_unavailable")
        action_name = str(action.get("action") or "retry_original")
        params_json = dict(action.get("params_json") or {})
        logger.info("Retry attempted: %s", action_name)
        if action_name == "retry_original":
            return self._action_executor("retry_original", params_json, command_context)
        return self._action_executor(action_name, params_json, command_context)

    def remember_successful_recovery(self, memory_key: str, option: RecoveryOption) -> None:
        if not memory_key or not settings.get("remember_successful_fallbacks", True):
            return
        with self._lock:
            preferences = self._memory.setdefault("preferences", {})
            entry = dict(preferences.get(memory_key) or {})
            entry["preferred_option_id"] = option.option_id
            entry["label"] = option.label
            entry["count"] = int(entry.get("count", 0) or 0) + 1
            entry["updated_at"] = self._utc_iso()
            preferences[memory_key] = entry
            self._stats["remembered_choices"] += 1
            self._save_memory_locked()
            self._sync_state()
        logger.info("Recovery succeeded via fallback: %s", option.option_id)
        analytics.increment_feature("recovery:remembered_success", metadata={"memory_key": memory_key, "option_id": option.option_id})

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                **dict(self._stats),
                "pending_plan": self._active_plan.to_dict() if self._active_plan is not None else None,
                "circuit_breakers": {key: dict(value) for key, value in self._circuit_breakers.items()},
                "remembered_preferences": len(self._memory.get("preferences", {})),
            }

    def clear_pending(self) -> None:
        with self._lock:
            self._active_plan = None
            self._active_context = {}
            state.pending_recovery_choices = []
            self._sync_state()

    def _execute_option(self, option: RecoveryOption, command_context: dict[str, Any], *, remember: bool = True) -> dict[str, Any]:
        self._stats["options_executed"] += 1
        logger.info("Fallback chosen: %s", option.option_id)
        if option.action == "cancel_recovery":
            return self._simple_result(False, "recovery_cancelled", "Cancelled the recovery.", error="cancelled")
        if option.action == "switch_input_mode":
            payload = fallback_input_mode()
            return self._simple_result(
                True,
                "recovery_switch_input_mode",
                payload["message"],
                data={
                    "target_app": "recovery",
                    "input_mode": payload["mode"],
                    "focus_text_input": True,
                    "speak_response": False,
                },
            )
        if option.action == "use_cached_result":
            cached = dict(option.params_json.get("result") or {})
            cached.setdefault("data", {})
            cached["data"] = {**dict(cached.get("data") or {}), "target_app": "recovery", "recovered_from_cache": True}
            return cached
        if option.action == "acknowledge_opened":
            return self._simple_result(True, "recovery_acknowledged", "Continuing with the assumption that the app opened.", data={"target_app": "recovery"})
        if option.action == "explain_limitation":
            return self._simple_result(False, "recovery_explained", str(option.params_json.get("message") or "That action is blocked."), error="permission_denied")
        if option.action == "reduced_mode":
            payload = dict(option.params_json)
            return self._simple_result(True, "recovery_reduced_mode", str(payload.get("message") or "Using reduced mode."), data={"target_app": "recovery", **payload})

        if self._action_executor is None:
            return self._simple_result(False, "recovery", "That recovery action is not available.", error="executor_unavailable")
        result = self._action_executor(option.action, dict(option.params_json), command_context)
        if result.get("success"):
            logger.info("Recovery success/failure: success option=%s", option.option_id)
        else:
            logger.info("Recovery success/failure: failure option=%s", option.option_id)
        return result

    def _plan_result(self, plan: RecoveryPlan) -> dict[str, Any]:
        lines = [plan.summary]
        for index, option in enumerate(plan.options, start=1):
            suffix = " (recommended)" if option.recommended else ""
            lines.append(f"{index}. {option.label}{suffix}")
        return self._simple_result(
            False,
            "recovery_required",
            "\n".join(lines),
            error=plan.error_code or plan.error_type.lower(),
            data={
                "target_app": "recovery",
                "recovery_plan": plan.to_dict(),
                "choices": [option.label for option in plan.options],
                "recovery_prompt": True,
                "focus_text_input": True,
                "speak_response": False,
            },
        )

    def _store_plan(self, plan: RecoveryPlan, command_context: dict[str, Any]) -> None:
        with self._lock:
            self._active_plan = plan
            self._active_context = dict(command_context)
            state.pending_recovery_choices = [option.to_dict() for option in plan.options]
            self._sync_state()

    def _preferred_option_for_plan(self, plan: RecoveryPlan) -> RecoveryOption | None:
        if not plan.fallback_action:
            return None
        option_id = str(plan.fallback_action.get("option_id") or "")
        if option_id:
            return next((item for item in plan.options if item.option_id == option_id), None)
        action = str(plan.fallback_action.get("action") or "")
        return next((item for item in plan.options if item.action == action), None)

    def _retry_allowed(self, memory_key: str) -> bool:
        if not settings.get("auto_retry_transient_errors", True):
            return False
        attempts = max(0, int(settings.get("max_auto_retries", 1) or 1))
        if attempts <= 0:
            return False
        breaker = self._circuit_breakers.get(memory_key or "__default__")
        if not breaker:
            return True
        open_until = float(breaker.get("open_until", 0.0) or 0.0)
        return time.monotonic() >= open_until

    def _record_breaker_failure(self, memory_key: str) -> None:
        key = memory_key or "__default__"
        now = time.monotonic()
        with self._lock:
            entry = dict(self._circuit_breakers.get(key) or {})
            failure_count = int(entry.get("failure_count", 0) or 0) + 1
            entry["failure_count"] = failure_count
            entry["last_failure_at"] = now
            if failure_count >= 2:
                entry["open_until"] = now + 5.0
            self._circuit_breakers[key] = entry

    @staticmethod
    def _memory_key(error: AssistantError, discriminator: str) -> str:
        return f"{error.code}:{str(discriminator or '').strip().lower()}"

    @staticmethod
    def _utc_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _load_memory(self) -> dict[str, Any]:
        try:
            if not self._memory_path.exists():
                return {"preferences": {}}
            payload = json.loads(self._memory_path.read_text(encoding="utf-8") or "{}")
            if isinstance(payload, dict):
                payload.setdefault("preferences", {})
                return payload
        except Exception as exc:
            logger.warning("Recovery memory load failed: %s", exc)
        return {"preferences": {}}

    def _save_memory_locked(self) -> None:
        try:
            self._memory_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._memory_path.with_suffix(self._memory_path.suffix + ".tmp")
            temp_path.write_text(json.dumps(self._memory, indent=2, ensure_ascii=True), encoding="utf-8")
            temp_path.replace(self._memory_path)
        except Exception as exc:
            logger.warning("Recovery memory save failed: %s", exc)

    def _sync_state(self) -> None:
        with self._lock:
            state.recovery_stats = {
                **dict(self._stats),
                "pending_plan": self._active_plan.to_dict() if self._active_plan is not None else None,
                "circuit_breakers": {key: dict(value) for key, value in self._circuit_breakers.items()},
                "remembered_preferences": len(self._memory.get("preferences", {})),
            }

    @staticmethod
    def _simple_result(success: bool, intent: str, response: str, *, error: str = "", data: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "success": success,
            "intent": intent,
            "response": response,
            "error": error,
            "data": data or {},
        }

    @staticmethod
    def _wrap_success(summary: str, result: dict[str, Any], *, source: str) -> dict[str, Any]:
        response = str(result.get("response") or "").strip()
        if summary and response:
            result = dict(result)
            result["response"] = f"{summary} {response}"
            result["data"] = {**dict(result.get("data") or {}), "recovery_source": source, "recovery_handled": True}
        return result

    @staticmethod
    def _app_category_for_error(error: AssistantError, original_command: str) -> str:
        requested = str(error.context.get("requested_app") or error.context.get("app_name") or "").lower()
        command = original_command.lower()
        if requested in {"chrome", "edge", "firefox", "brave"} or "browser" in error.code:
            return "browser"
        if requested in {"spotify", "itunes"} or any(token in command for token in ("play ", "music", "song", "track")):
            return "music"
        if requested in {"whatsapp", "telegram", "slack", "discord"}:
            return "chat"
        return "browser" if "search" in command else "editor"

    @staticmethod
    def _query_from_command(command: str) -> str:
        lowered = str(command or "").strip()
        if not lowered:
            return ""
        lower = lowered.lower()
        for prefix in ("search for ", "search ", "google ", "find ", "look up ", "lookup "):
            if lower.startswith(prefix):
                return lowered[len(prefix) :].strip()
        for prefix in ("play song ", "play track ", "play ", "open "):
            if lower.startswith(prefix):
                return lowered[len(prefix) :].strip()
        return lowered

    @staticmethod
    def _replace_contact_in_command(command: str, replacement: str, context: dict[str, Any]) -> str:
        original = str(context.get("contact") or context.get("requested_contact") or "").strip()
        if original:
            index = command.lower().find(original.lower())
            if index >= 0:
                return command[:index] + replacement + command[index + len(original) :]
        return replacement

    @staticmethod
    def _device_name(error: AssistantError, context: dict[str, Any]) -> str:
        for candidate in (error.context.get("device"), context.get("device"), context.get("stage"), error.code):
            text = str(candidate or "").lower()
            if "mic" in text or "speech" in text or "voice" in text:
                return "microphone"
        return "device"


recovery_manager = RecoveryManager()
