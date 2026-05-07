"""
Natural-language reminder skill.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from core import settings, state
from core.logger import get_logger
from core.notifications import NotificationChannel
from core.reminders import DuplicateReminderError, ReminderManager, ReminderStoreError
from core.scheduler import ReminderScheduler
from core.time_parser import ReminderTimeParser, TimeParseError
from core.text_utils import normalize_command
from skills.base import SkillBase, SkillExecutionResult

logger = get_logger(__name__)

_TIMER_PATTERN = re.compile(
    r"^(?:(?:set|start)(?:\s+a)?\s+)?timer(?:\s+for)?\s+(?P<value>\d+)\s*(?P<unit>seconds?|secs?|sec|minutes?|mins?|min|hours?|hrs?|hr)\b",
    flags=re.IGNORECASE,
)
_TIMER_UNITS = {
    "sec": "seconds",
    "secs": "seconds",
    "second": "seconds",
    "seconds": "seconds",
    "min": "minutes",
    "mins": "minutes",
    "minute": "minutes",
    "minutes": "minutes",
    "hr": "hours",
    "hrs": "hours",
    "hour": "hours",
    "hours": "hours",
}

_ORDINALS = {
    "first": 1,
    "one": 1,
    "1": 1,
    "1st": 1,
    "second": 2,
    "two": 2,
    "2": 2,
    "2nd": 2,
    "third": 3,
    "three": 3,
    "3": 3,
    "3rd": 3,
    "fourth": 4,
    "four": 4,
    "4": 4,
    "4th": 4,
    "fifth": 5,
    "five": 5,
    "5": 5,
    "5th": 5,
}


class ReminderSkill(SkillBase):
    """Create, manage, and schedule local reminders."""

    def __init__(
        self,
        *,
        manager: ReminderManager | None = None,
        scheduler: ReminderScheduler | None = None,
        time_parser: ReminderTimeParser | None = None,
    ) -> None:
        self.time_parser = time_parser or ReminderTimeParser()
        self.manager = manager or ReminderManager(time_parser=self.time_parser)
        self.scheduler = scheduler or ReminderScheduler(manager=self.manager)

    def set_tts_engine(self, tts_engine) -> None:
        self.scheduler.set_tts_engine(tts_engine)

    def set_notification_manager(self, notification_manager) -> None:
        self.scheduler.set_notification_manager(notification_manager)

    def set_event_callback(self, callback) -> None:
        self.scheduler.set_trigger_callback(callback)

    def start(self) -> None:
        self.scheduler.start()

    def shutdown(self) -> None:
        self.scheduler.stop()
        self.manager.close()

    def can_handle(self, context: Mapping[str, Any], intent: str, command: str) -> bool:
        normalized = self._normalize(command)
        if not normalized:
            return False
        if self._rewrite_timer_command(normalized) is not None:
            return True
        if normalized.startswith(
            ("remind me", "set reminder", "set a reminder", "create reminder", "add reminder")
        ):
            return True
        if normalized.startswith(
            (
                "in ",
                "tomorrow ",
                "next monday ",
                "next tuesday ",
                "next wednesday ",
                "next thursday ",
                "next friday ",
                "next saturday ",
                "next sunday ",
                "every ",
            )
        ):
            return "remind me" in normalized
        return bool(
            re.fullmatch(r"(?:list|show)(?: my)? reminders", normalized)
            or re.fullmatch(r"(?:delete|remove) reminder (?:\d+|first|second|third|fourth|fifth)", normalized)
            or re.fullmatch(r"disable reminder (?:\d+|first|second|third|fourth|fifth)", normalized)
            or re.fullmatch(r"enable reminder (?:\d+|first|second|third|fourth|fifth)", normalized)
        )

    def execute(self, command: str, context: Mapping[str, Any]) -> SkillExecutionResult:
        normalized = self._normalize(command)
        reminder_command = self._rewrite_timer_command(normalized) or command

        try:
            if self._is_list_command(normalized):
                return self._list_reminders()

            if self._is_delete_command(normalized):
                reminder_id = self._extract_reminder_id(normalized)
                if reminder_id is None:
                    return self._failure("I couldn't tell which reminder to delete.", "missing_reminder_id")
                deleted = self.manager.delete_reminder(reminder_id)
                if not deleted:
                    return self._failure(f"I couldn't find reminder {reminder_id}.", "reminder_not_found")
                return SkillExecutionResult(
                    success=True,
                    intent="reminder_delete",
                    response=f"Deleted reminder {reminder_id}.",
                    skill_name=self.name(),
                    data={
                        "target_app": "reminders",
                        "notification_title": "Reminder deleted",
                        "notification_channel": NotificationChannel.IN_APP.value,
                    },
                )

            if self._is_disable_command(normalized):
                reminder_id = self._extract_reminder_id(normalized)
                if reminder_id is None:
                    return self._failure("I couldn't tell which reminder to disable.", "missing_reminder_id")
                reminder = self.manager.disable(reminder_id)
                if reminder is None:
                    return self._failure(f"I couldn't find reminder {reminder_id}.", "reminder_not_found")
                return SkillExecutionResult(
                    success=True,
                    intent="reminder_disable",
                    response=f"Disabled reminder {reminder_id}.",
                    skill_name=self.name(),
                    data={
                        "target_app": "reminders",
                        "notification_title": "Reminder disabled",
                        "notification_channel": NotificationChannel.IN_APP.value,
                    },
                )

            if self._is_enable_command(normalized):
                reminder_id = self._extract_reminder_id(normalized)
                if reminder_id is None:
                    return self._failure("I couldn't tell which reminder to enable.", "missing_reminder_id")
                reminder = self.manager.enable(reminder_id)
                if reminder is None:
                    return self._failure(f"I couldn't find reminder {reminder_id}.", "reminder_not_found")
                return SkillExecutionResult(
                    success=True,
                    intent="reminder_enable",
                    response=f"Enabled reminder {reminder_id}.",
                    skill_name=self.name(),
                    data={
                        "target_app": "reminders",
                        "notification_title": "Reminder enabled",
                        "notification_channel": NotificationChannel.IN_APP.value,
                    },
                )

            reminder = self.manager.create_from_natural(reminder_command)
            schedule_text = self.time_parser.describe_repeat(reminder.repeat_rule, reminder.trigger_time)
            base_response = (
                f"Recurring reminder set for {schedule_text}: {reminder.message}"
                if reminder.repeat_rule
                else f"Reminder set for {schedule_text}: {reminder.message}"
            )
            if not settings.get("reminders_enabled"):
                base_response += " Reminder scheduling is disabled in settings, so it will stay pending until re-enabled."
            return SkillExecutionResult(
                success=True,
                intent="reminder_create",
                response=base_response,
                skill_name=self.name(),
                data={
                    "target_app": "reminders",
                    "reminder": reminder.to_dict(),
                    "notification_title": "Reminder scheduled",
                    "notification_channel": NotificationChannel.IN_APP.value,
                },
            )

        except DuplicateReminderError as exc:
            return self._failure(str(exc), "duplicate_reminder")
        except TimeParseError as exc:
            return self._failure(str(exc), "invalid_time")
        except ReminderStoreError as exc:
            return self._failure(str(exc), "reminder_store_unavailable")
        except Exception as exc:
            logger.error("Reminder command failed: %s", exc)
            return self._failure("I couldn't complete the reminder request.", "reminder_error")

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "target": "reminders",
            "supports": [
                "create_one_time",
                "create_recurring",
                "list",
                "delete",
                "enable",
                "disable",
                "notifications",
                "tts_alerts",
            ],
        }

    def health_check(self) -> dict[str, Any]:
        return {
            "enabled": bool(settings.get("reminders_enabled")),
            "scheduler_running": bool(getattr(state, "scheduler_running", False)),
            "reminder_count": int(getattr(state, "reminder_count", 0) or 0),
            "next_reminder_time": getattr(state, "next_reminder_time", ""),
        }

    @staticmethod
    def _normalize(text: str) -> str:
        return normalize_command(text)

    @staticmethod
    def _is_list_command(normalized: str) -> bool:
        return normalized in {"list reminders", "show reminders", "show my reminders"}

    @staticmethod
    def _is_delete_command(normalized: str) -> bool:
        return normalized.startswith(("delete reminder ", "remove reminder "))

    @staticmethod
    def _is_disable_command(normalized: str) -> bool:
        return normalized.startswith("disable reminder ")

    @staticmethod
    def _is_enable_command(normalized: str) -> bool:
        return normalized.startswith("enable reminder ")

    @staticmethod
    def _extract_reminder_id(normalized: str) -> int | None:
        match = re.search(r"\b(\d+)\b", normalized)
        if match:
            return int(match.group(1))
        for token in normalized.split():
            if token in _ORDINALS:
                return _ORDINALS[token]
        return None

    @staticmethod
    def _rewrite_timer_command(normalized: str) -> str | None:
        match = _TIMER_PATTERN.fullmatch(str(normalized or "").strip())
        if match is None:
            return None

        value = match.group("value")
        unit = _TIMER_UNITS.get(match.group("unit").lower(), match.group("unit").lower())
        return f"in {value} {unit} remind me to timer"

    def _list_reminders(self) -> SkillExecutionResult:
        reminders = self.manager.list_reminders()
        if not reminders:
            return SkillExecutionResult(
                success=True,
                intent="reminder_list",
                response="No reminders scheduled.",
                skill_name=self.name(),
                data={
                    "target_app": "reminders",
                    "speak_response": False,
                    "suppress_notification": True,
                },
            )

        lines: list[str] = []
        for reminder in reminders:
            schedule = self.time_parser.describe_repeat(reminder.repeat_rule, reminder.trigger_time)
            suffix = "" if reminder.enabled else " (disabled)"
            lines.append(f"{reminder.id}. {reminder.title} - {schedule}{suffix}")

        return SkillExecutionResult(
            success=True,
            intent="reminder_list",
            response="\n".join(lines),
            skill_name=self.name(),
            data={
                "target_app": "reminders",
                "speak_response": False,
                "suppress_notification": True,
                "reminders": [reminder.to_dict() for reminder in reminders],
            },
        )

    def _failure(self, response: str, error: str) -> SkillExecutionResult:
        return SkillExecutionResult(
            success=False,
            intent="reminder",
            response=response,
            skill_name=self.name(),
            error=error,
            data={"target_app": "reminders"},
        )
