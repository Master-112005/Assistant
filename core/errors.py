"""
Typed assistant error taxonomy with recovery metadata.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _default_code_for(cls_name: str) -> str:
    parts: list[str] = []
    token = ""
    for char in cls_name.replace("Error", ""):
        if char.isupper() and token:
            parts.append(token.lower())
            token = char
        else:
            token += char
    if token:
        parts.append(token.lower())
    return "_".join(parts) or "assistant_error"


@dataclass(eq=False)
class AssistantError(Exception):
    """Base class for recoverable assistant failures."""

    message: str
    code: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    recoverable: bool = True

    def __post_init__(self) -> None:
        if not self.code:
            self.code = _default_code_for(type(self).__name__)
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "code": self.code,
            "message": self.message,
            "context": dict(self.context or {}),
            "recoverable": bool(self.recoverable),
        }


class AppNotFoundError(AssistantError):
    pass


class PermissionDeniedError(AssistantError):
    pass


class DeviceUnavailableError(AssistantError):
    pass


class NetworkError(AssistantError):
    pass


class ActionTimeoutError(AssistantError):
    pass


class NotSupportedError(AssistantError):
    pass


class AmbiguousMatchError(AssistantError):
    pass


class ValidationError(AssistantError):
    pass


class ExecutionError(AssistantError):
    pass


class ConfigError(AssistantError):
    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message=message, code="config_error", context=context or {}, recoverable=False)


class SettingsError(AssistantError):
    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message=message, code="settings_error", context=context or {}, recoverable=False)


class SkillError(AssistantError):
    def __init__(self, message: str, *, context: dict[str, Any] | None = None, recoverable: bool = True) -> None:
        super().__init__(message=message, code="skill_error", context=context or {}, recoverable=recoverable)


class PermissionErrorCustom(PermissionDeniedError):
    def __init__(self, message: str, *, context: dict[str, Any] | None = None, recoverable: bool = True) -> None:
        super().__init__(message=message, code="permission_denied", context=context or {}, recoverable=recoverable)


class LLMError(AssistantError):
    def __init__(self, message: str, *, context: dict[str, Any] | None = None, recoverable: bool = True) -> None:
        super().__init__(message=message, code="llm_error", context=context or {}, recoverable=recoverable)


class LLMUnavailableError(LLMError):
    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message=message, context=context or {}, recoverable=True)
        self.code = "llm_unavailable"


class LLMResponseError(LLMError):
    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message=message, context=context or {}, recoverable=True)
        self.code = "llm_response_error"


def ensure_assistant_error(error: Exception | dict[str, Any] | str, *, context: dict[str, Any] | None = None) -> AssistantError:
    """Normalize arbitrary failure payloads into an AssistantError."""
    if isinstance(error, AssistantError):
        if context:
            merged = dict(error.context)
            merged.update(context)
            error.context = merged
        return error

    merged_context = dict(context or {})
    if isinstance(error, dict):
        merged_context.update(dict(error))
        return ExecutionError(
            message=str(error.get("message") or error.get("response") or error.get("error") or "Execution failed."),
            code=str(error.get("error") or error.get("code") or "execution_error"),
            context=merged_context,
            recoverable=bool(error.get("recoverable", True)),
        )

    if isinstance(error, str):
        return ExecutionError(message=error, context=merged_context)

    return ExecutionError(
        message=str(error) or "Execution failed.",
        context=merged_context,
        recoverable=True,
    )

