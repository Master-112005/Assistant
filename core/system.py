"""
Unified desktop system-control adapter for explicit command intents.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any

from core.logger import get_logger
from core.media import MediaController
from core.system_controls import ParsedSystemCommand, SystemController

logger = get_logger(__name__)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(slots=True)
class BackendActionResult:
    success: bool
    action: str
    message: str
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class DesktopSystemController:
    """Maps explicit desktop intents onto the existing system/media backends."""

    def __init__(
        self,
        *,
        system_controller: SystemController | None = None,
        media_controller: MediaController | None = None,
    ) -> None:
        self._system = system_controller or SystemController()
        self._media = media_controller or MediaController()

    def execute_intent(self, intent: str, entities: dict[str, Any] | None = None) -> BackendActionResult:
        normalized_intent = str(intent or "").strip().lower()
        payload = dict(entities or {})

        if normalized_intent in {"play_media", "pause_media", "next_track", "previous_track"}:
            return self._execute_media_action(normalized_intent)

        if normalized_intent == "sleep_pc":
            return self._sleep_pc()

        command = self._command_for_intent(normalized_intent, payload)
        if command is None:
            return BackendActionResult(
                success=False,
                action=normalized_intent or "system",
                message=f"Unsupported system action: {normalized_intent or 'unknown'}.",
                error="unsupported_system_action",
            )

        result = self._system.execute(command)
        return BackendActionResult(
            success=result.success,
            action=result.action,
            message=result.message,
            error=result.error,
            data={
                "action": result.action,
                "previous_state": dict(result.previous_state),
                "current_state": dict(result.current_state),
                "timestamp": result.timestamp,
            },
        )

    def _execute_media_action(self, intent: str) -> BackendActionResult:
        action_map = {
            "play_media": "play_pause",
            "pause_media": "play_pause",
            "next_track": "next_track",
            "previous_track": "previous_track",
        }
        key_action = action_map[intent]
        sent = self._media.send_media_action(key_action)
        messages = {
            "play_media": "Sent the play command.",
            "pause_media": "Sent the pause command.",
            "next_track": "Skipping to the next track.",
            "previous_track": "Going to the previous track.",
        }
        return BackendActionResult(
            success=sent,
            action=intent,
            message=messages[intent] if sent else "I couldn't send the media key command.",
            error="" if sent else "media_key_failed",
        )

    def _sleep_pc(self) -> BackendActionResult:
        try:
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=CREATE_NO_WINDOW if CREATE_NO_WINDOW else 0,
                check=False,
            )
        except Exception as exc:
            logger.warning("Sleep command failed: %s", exc)
            return BackendActionResult(
                success=False,
                action="sleep_pc",
                message="I couldn't put the PC to sleep.",
                error="sleep_failed",
            )

        success = completed.returncode == 0
        return BackendActionResult(
            success=success,
            action="sleep_pc",
            message="PC entering sleep mode." if success else "I couldn't put the PC to sleep.",
            error="" if success else "sleep_failed",
            data={"stdout": (completed.stdout or "").strip(), "stderr": (completed.stderr or "").strip()},
        )

    def _command_for_intent(self, intent: str, entities: dict[str, Any]) -> ParsedSystemCommand | None:
        if intent == "volume_up":
            return ParsedSystemCommand("volume_up", "volume", "up", value=_coerce_int(entities.get("value")))
        if intent == "volume_down":
            return ParsedSystemCommand("volume_down", "volume", "down", value=_coerce_int(entities.get("value")))
        if intent == "mute":
            return ParsedSystemCommand("mute", "volume", "mute")
        if intent == "unmute":
            return ParsedSystemCommand("unmute", "volume", "unmute")
        if intent == "set_volume":
            return ParsedSystemCommand("set_volume", "volume", "set", value=_coerce_int(entities.get("value")))
        if intent == "brightness_up":
            return ParsedSystemCommand("brightness_up", "brightness", "up", value=_coerce_int(entities.get("value")))
        if intent == "brightness_down":
            return ParsedSystemCommand("brightness_down", "brightness", "down", value=_coerce_int(entities.get("value")))
        if intent == "set_brightness":
            return ParsedSystemCommand("set_brightness", "brightness", "set", value=_coerce_int(entities.get("value")))
        if intent == "lock_pc":
            return ParsedSystemCommand("lock_pc", "lock", "lock")
        if intent == "shutdown_pc":
            return ParsedSystemCommand(
                "shutdown",
                "shutdown",
                "shutdown",
                delay_seconds=max(0, _coerce_int(entities.get("delay_seconds")) or 0),
            )
        if intent == "restart_pc":
            return ParsedSystemCommand(
                "restart",
                "restart",
                "restart",
                delay_seconds=max(0, _coerce_int(entities.get("delay_seconds")) or 0),
            )
        return None


def _coerce_int(value: Any) -> int | None:
    try:
        if value in ("", None):
            return None
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
