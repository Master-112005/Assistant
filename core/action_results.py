"""
Standard action result contract for executable desktop actions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(slots=True)
class ActionResult:
    success: bool
    action: str
    target: str | None
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    verified: bool = False
    duration_ms: int = 0
    trace_id: str = ""
    recovered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": bool(self.success),
            "action": str(self.action or ""),
            "target": self.target if self.target not in {"", None} else None,
            "message": str(self.message or ""),
            "data": dict(self.data),
            "error_code": self.error_code or None,
            "verified": bool(self.verified),
            "duration_ms": int(max(0, int(self.duration_ms or 0))),
            "trace_id": str(self.trace_id or ""),
            "recovered": bool(self.recovered),
        }

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        default_action: str = "",
        default_target: str | None = None,
    ) -> "ActionResult":
        raw_data = payload.get("data", {})
        data = dict(raw_data) if isinstance(raw_data, Mapping) else {}
        error_code = payload.get("error_code")
        if not isinstance(error_code, str) or not error_code.strip():
            legacy_error = payload.get("error")
            error_code = str(legacy_error).strip() if legacy_error else None

        raw_duration = payload.get("duration_ms", data.get("duration_ms", 0))
        try:
            duration_ms = int(round(float(raw_duration or 0)))
        except (TypeError, ValueError):
            duration_ms = 0

        raw_target = payload.get("target", data.get("target_app", default_target))
        target = str(raw_target).strip() if raw_target not in {"", None} else default_target

        action = str(payload.get("action") or data.get("action") or default_action or payload.get("intent") or "").strip()
        message = str(payload.get("message") or payload.get("response") or "").strip()

        verified = payload.get("verified")
        if verified is None:
            verified = data.get("verified", False)
        trace_id = str(payload.get("trace_id") or data.get("trace_id") or "").strip()
        recovered = bool(payload.get("recovered", data.get("recovered", False)))

        success = bool(payload.get("success", False))
        if success and not verified and action in {
            "open_app",
            "open_website",
            "close_app",
            "minimize_app",
            "maximize_app",
            "focus_app",
            "restore_app",
            "toggle_app",
            "mute",
            "unmute",
            "volume_up",
            "volume_down",
            "set_volume",
        }:
            success = False
            if not error_code:
                error_code = "not_verified"

        return cls(
            success=success,
            action=action or default_action,
            target=target,
            message=message,
            data=data,
            error_code=error_code,
            verified=bool(verified),
            duration_ms=max(0, duration_ms),
            trace_id=trace_id,
            recovered=recovered,
        )


def ensure_action_result(
    payload: Mapping[str, Any] | None,
    *,
    default_action: str = "",
    default_target: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return ActionResult(
            success=False,
            action=default_action or "unknown",
            target=default_target,
            message=str(payload or ""),
            error_code="invalid_result",
            verified=False,
        ).to_dict()

    existing = payload.get("action_result")
    if isinstance(existing, Mapping):
        return ActionResult.from_mapping(
            existing,
            default_action=default_action or str(payload.get("intent") or ""),
            default_target=default_target,
        ).to_dict()

    return ActionResult.from_mapping(
        payload,
        default_action=default_action,
        default_target=default_target,
    ).to_dict()
