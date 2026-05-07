"""
Persistent reminder management backed by SQLite.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dateutil import parser as date_parser

from core import settings, state
from core.logger import get_logger
from core.paths import DATA_DIR
from core.time_parser import ParsedRepeat, ParsedTime, ReminderTimeParser, TimeParseError

logger = get_logger(__name__)


class ReminderError(RuntimeError):
    """Base error for reminder failures."""


class ReminderStoreError(ReminderError):
    """Raised when the reminder database is unavailable."""


class DuplicateReminderError(ReminderError):
    """Raised when an identical enabled reminder already exists."""


@dataclass(slots=True)
class Reminder:
    id: int | None
    title: str
    message: str
    trigger_time: datetime
    timezone: str
    repeat_rule: str | None
    enabled: bool
    created_at: datetime
    last_triggered_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "message": self.message,
            "trigger_time": self.trigger_time.isoformat(),
            "timezone": self.timezone,
            "repeat_rule": self.repeat_rule,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(),
            "last_triggered_at": self.last_triggered_at.isoformat() if self.last_triggered_at else None,
        }


class ReminderManager:
    """Create, persist, query, and mutate reminders."""

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        time_parser: ReminderTimeParser | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else DATA_DIR / "reminders.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.time_parser = time_parser or ReminderTimeParser()
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self.last_error: str = ""
        self._connect()
        self._refresh_state()

    def create_reminder(
        self,
        text: str,
        when: datetime,
        *,
        repeat_rule: str | None = None,
        timezone_name: str | None = None,
    ) -> Reminder:
        """Create a reminder directly from a message and datetime."""
        self._ensure_available()
        message = self._clean_message(text)
        trigger_time = self.time_parser._normalize_dt(when)
        current = self.time_parser._normalize_dt(self.time_parser._now_provider())
        created_at = current
        timezone_value = str(timezone_name or settings.get("default_timezone") or "local")

        if trigger_time <= current:
            raise TimeParseError("That reminder time is already in the past.")

        self._check_duplicate(message, trigger_time, repeat_rule)

        reminder = Reminder(
            id=None,
            title=self._build_title(message),
            message=message,
            trigger_time=trigger_time,
            timezone=timezone_value,
            repeat_rule=repeat_rule,
            enabled=True,
            created_at=created_at,
            last_triggered_at=None,
        )

        sql = """
        INSERT INTO reminders (
            title,
            message,
            trigger_time,
            timezone,
            repeat_rule,
            enabled,
            created_at,
            last_triggered_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._lock:
            cursor = self._conn.execute(
                sql,
                (
                    reminder.title,
                    reminder.message,
                    self._serialize_dt(reminder.trigger_time),
                    reminder.timezone,
                    reminder.repeat_rule,
                    1 if reminder.enabled else 0,
                    self._serialize_dt(reminder.created_at),
                    None,
                ),
            )
            self._conn.commit()
            reminder.id = int(cursor.lastrowid)
        self._refresh_state(reminder=reminder)
        logger.info("Reminder created id=%s at %s", reminder.id, reminder.trigger_time.strftime("%H:%M"))
        return reminder

    def create_from_natural(self, text: str) -> Reminder:
        """Parse and persist a reminder from a natural-language command."""
        command = str(text or "").strip()
        if not command:
            raise TimeParseError("Reminder text cannot be empty.")

        parsed_repeat = self.time_parser.parse_repeat(command)
        if parsed_repeat is not None:
            message = self._extract_message(command, parsed_repeat.matched_text)
            return self.create_reminder(
                message,
                parsed_repeat.trigger_time,
                repeat_rule=parsed_repeat.repeat_rule,
            )

        parsed_time = self.time_parser.parse_time(command)
        if parsed_time is None:
            raise TimeParseError("I couldn't understand when to schedule that reminder.")

        message = self._extract_message(command, parsed_time.matched_text)
        return self.create_reminder(message, parsed_time.trigger_time)

    def list_reminders(self) -> list[Reminder]:
        """Return all reminders ordered by enabled status and next trigger time."""
        self._ensure_available()
        sql = """
        SELECT id, title, message, trigger_time, timezone, repeat_rule, enabled, created_at, last_triggered_at
        FROM reminders
        ORDER BY enabled DESC, trigger_time ASC, id ASC
        """
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        reminders = [self._row_to_reminder(row) for row in rows]
        self._refresh_state(reminders=reminders)
        return reminders

    def delete_reminder(self, reminder_id: int) -> bool:
        """Delete a reminder by id."""
        self._ensure_available()
        with self._lock:
            cursor = self._conn.execute("DELETE FROM reminders WHERE id = ?", (int(reminder_id),))
            deleted = cursor.rowcount > 0
            if deleted:
                self._conn.commit()
        if deleted:
            logger.info("Reminder deleted id=%s", reminder_id)
        self._refresh_state()
        return deleted

    def enable(self, reminder_id: int) -> Reminder | None:
        """Enable a stored reminder."""
        return self._set_enabled(reminder_id, True)

    def disable(self, reminder_id: int) -> Reminder | None:
        """Disable a stored reminder."""
        return self._set_enabled(reminder_id, False)

    def get_due_reminders(self, now: datetime | None = None) -> list[Reminder]:
        """Return enabled reminders due at or before the provided timestamp."""
        self._ensure_available()
        current = self.time_parser._normalize_dt(now or self.time_parser._now_provider())
        sql = """
        SELECT id, title, message, trigger_time, timezone, repeat_rule, enabled, created_at, last_triggered_at
        FROM reminders
        WHERE enabled = 1 AND trigger_time <= ?
        ORDER BY trigger_time ASC, id ASC
        """
        with self._lock:
            rows = self._conn.execute(sql, (self._serialize_dt(current),)).fetchall()
        reminders = [self._row_to_reminder(row) for row in rows]
        self._refresh_state()
        return reminders

    def mark_triggered(self, reminder_id: int, *, triggered_at: datetime | None = None) -> Reminder | None:
        """Update a reminder after firing, rescheduling recurring reminders as needed."""
        self._ensure_available()
        reminder = self.get_by_id(reminder_id)
        if reminder is None:
            return None

        fired_at = self.time_parser._normalize_dt(triggered_at or self.time_parser._now_provider())
        next_trigger: datetime | None = None
        enabled = False
        if reminder.repeat_rule:
            next_trigger = self.time_parser.next_occurrence(
                reminder.repeat_rule,
                dtstart=reminder.trigger_time,
                after=fired_at,
            )
            enabled = next_trigger is not None

        updated_trigger = next_trigger or reminder.trigger_time
        sql = """
        UPDATE reminders
        SET trigger_time = ?, enabled = ?, last_triggered_at = ?
        WHERE id = ?
        """
        with self._lock:
            self._conn.execute(
                sql,
                (
                    self._serialize_dt(updated_trigger),
                    1 if enabled else 0,
                    self._serialize_dt(fired_at),
                    int(reminder_id),
                ),
            )
            self._conn.commit()
        updated = Reminder(
            id=reminder.id,
            title=reminder.title,
            message=reminder.message,
            trigger_time=updated_trigger,
            timezone=reminder.timezone,
            repeat_rule=reminder.repeat_rule,
            enabled=enabled,
            created_at=reminder.created_at,
            last_triggered_at=fired_at,
        )
        self._refresh_state(reminder=updated)
        return updated

    def get_by_id(self, reminder_id: int) -> Reminder | None:
        self._ensure_available()
        sql = """
        SELECT id, title, message, trigger_time, timezone, repeat_rule, enabled, created_at, last_triggered_at
        FROM reminders
        WHERE id = ?
        """
        with self._lock:
            row = self._conn.execute(sql, (int(reminder_id),)).fetchone()
        return self._row_to_reminder(row) if row is not None else None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                    self._conn.close()
                except sqlite3.DatabaseError as exc:
                    logger.warning("Reminder database close failed: %s", exc)
                finally:
                    self._conn = None

    def _connect(self) -> None:
        try:
            self._conn = sqlite3.connect(
                self.db_path,
                timeout=5.0,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    trigger_time TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    repeat_rule TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_triggered_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_reminders_due
                    ON reminders(enabled, trigger_time);

                CREATE INDEX IF NOT EXISTS idx_reminders_repeat
                    ON reminders(repeat_rule);
                """
            )
            self._conn.commit()
        except sqlite3.DatabaseError as exc:
            self.last_error = f"Reminder database unavailable: {exc}"
            self._conn = None
            logger.error(self.last_error)

    def _ensure_available(self) -> None:
        if self._conn is None:
            raise ReminderStoreError(self.last_error or "Reminder database is unavailable.")

    def _set_enabled(self, reminder_id: int, enabled: bool) -> Reminder | None:
        self._ensure_available()
        reminder = self.get_by_id(reminder_id)
        if reminder is None:
            return None
        with self._lock:
            self._conn.execute("UPDATE reminders SET enabled = ? WHERE id = ?", (1 if enabled else 0, int(reminder_id)))
            self._conn.commit()
        updated = Reminder(
            id=reminder.id,
            title=reminder.title,
            message=reminder.message,
            trigger_time=reminder.trigger_time,
            timezone=reminder.timezone,
            repeat_rule=reminder.repeat_rule,
            enabled=enabled,
            created_at=reminder.created_at,
            last_triggered_at=reminder.last_triggered_at,
        )
        self._refresh_state(reminder=updated)
        return updated

    def _check_duplicate(self, message: str, trigger_time: datetime, repeat_rule: str | None) -> None:
        normalized = " ".join(message.lower().split())
        sql = """
        SELECT id
        FROM reminders
        WHERE lower(message) = ?
          AND trigger_time = ?
          AND COALESCE(repeat_rule, '') = ?
          AND enabled = 1
        LIMIT 1
        """
        with self._lock:
            row = self._conn.execute(
                sql,
                (
                    normalized,
                    self._serialize_dt(trigger_time),
                    str(repeat_rule or ""),
                ),
            ).fetchone()
        if row is not None:
            raise DuplicateReminderError("That reminder already exists.")

    def _refresh_state(
        self,
        *,
        reminder: Reminder | None = None,
        reminders: list[Reminder] | None = None,
    ) -> None:
        if self._conn is None:
            state.reminder_count = 0
            state.next_reminder_time = ""
            return

        with self._lock:
            count_row = self._conn.execute("SELECT COUNT(*) AS count FROM reminders").fetchone()
            next_row = self._conn.execute(
                """
                SELECT trigger_time
                FROM reminders
                WHERE enabled = 1
                ORDER BY trigger_time ASC
                LIMIT 1
                """
            ).fetchone()

        state.reminder_count = int(count_row["count"]) if count_row is not None else 0
        state.next_reminder_time = str(next_row["trigger_time"]) if next_row is not None else ""

        latest = reminder
        if latest is None and reminders:
            latest = reminders[0]
        if latest is not None and latest.last_triggered_at is not None:
            state.last_triggered_reminder = latest.to_dict()

    def _extract_message(self, command: str, matched_text: str) -> str:
        text = str(command or "").strip()
        if matched_text:
            text = re.sub(re.escape(matched_text), "", text, count=1, flags=re.IGNORECASE)

        cleanup_patterns = (
            r"\b(?:please\s+)?(?:can you\s+)?remind me(?:\s+to)?\b",
            r"\b(?:set|create|add)\s+(?:a\s+)?reminder(?:\s+to)?\b",
        )
        for pattern in cleanup_patterns:
            text = re.sub(pattern, "", text, count=1, flags=re.IGNORECASE)

        text = re.sub(r"^[\s,:-]+|[\s,:-]+$", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text.lower().startswith("to "):
            text = text[3:].strip()
        return self._clean_message(text)

    @staticmethod
    def _clean_message(message: str) -> str:
        cleaned = re.sub(r"\s+", " ", str(message or "").strip())
        return cleaned or "Reminder"

    @staticmethod
    def _build_title(message: str) -> str:
        cleaned = str(message or "Reminder").strip().rstrip(".")
        if not cleaned:
            return "Reminder"
        title = cleaned[0].upper() + cleaned[1:]
        if len(title) <= 80:
            return title
        return title[:77].rstrip() + "..."

    @staticmethod
    def _serialize_dt(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    def _row_to_reminder(self, row) -> Reminder:
        return Reminder(
            id=int(row["id"]),
            title=str(row["title"]),
            message=str(row["message"]),
            trigger_time=self.time_parser._normalize_dt(date_parser.isoparse(str(row["trigger_time"]))),
            timezone=str(row["timezone"]),
            repeat_rule=str(row["repeat_rule"]) if row["repeat_rule"] else None,
            enabled=bool(row["enabled"]),
            created_at=self.time_parser._normalize_dt(date_parser.isoparse(str(row["created_at"]))),
            last_triggered_at=self.time_parser._normalize_dt(date_parser.isoparse(str(row["last_triggered_at"])))
            if row["last_triggered_at"]
            else None,
        )
