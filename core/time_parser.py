"""
Natural-language time parsing helpers for reminders.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from dateutil import parser as date_parser
from dateutil import rrule, tz

from core import settings

_WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_RRULE_DAY_NAMES = {
    "MO": "Monday",
    "TU": "Tuesday",
    "WE": "Wednesday",
    "TH": "Thursday",
    "FR": "Friday",
    "SA": "Saturday",
    "SU": "Sunday",
}

_CLOCK_PREFIX = re.compile(r"^\s*(?:at\s+)?(?P<clock>\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?)", re.IGNORECASE)
_RELATIVE_PATTERN = re.compile(r"\bin\s+(?P<value>\d+)\s+(?P<unit>minutes?|hours?|days?|weeks?)\b", re.IGNORECASE)


class TimeParseError(ValueError):
    """Raised when a reminder time phrase cannot be interpreted."""


@dataclass(slots=True, frozen=True)
class ParsedTime:
    trigger_time: datetime
    matched_text: str


@dataclass(slots=True, frozen=True)
class ParsedRepeat:
    trigger_time: datetime
    repeat_rule: str
    matched_text: str
    label: str


class ReminderTimeParser:
    """Parse local reminder time phrases into timezone-aware datetimes."""

    def __init__(
        self,
        *,
        timezone_name: str | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.timezone_name = str(timezone_name or settings.get("default_timezone") or "local").strip() or "local"
        self._now_provider = now_provider or self._default_now

    def parse_time(self, text: str, *, now: datetime | None = None) -> ParsedTime | None:
        """Parse one-time reminder phrases such as 'tomorrow 6 am' or 'in 20 minutes'."""
        original = str(text or "").strip()
        if not original:
            return None

        baseline = self._normalize_dt(now or self._now_provider())
        lowered = original.lower()

        relative_match = _RELATIVE_PATTERN.search(original)
        if relative_match:
            value = int(relative_match.group("value"))
            unit = relative_match.group("unit").lower()
            delta = self._relative_delta(value, unit)
            return ParsedTime(
                trigger_time=(baseline + delta).replace(second=0, microsecond=0),
                matched_text=original[relative_match.start() : relative_match.end()],
            )

        if "tomorrow" in lowered:
            trigger_time, matched = self._parse_tomorrow(original, baseline)
            return ParsedTime(trigger_time=trigger_time, matched_text=matched)

        next_day_match = re.search(r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lowered)
        if next_day_match:
            trigger_time, matched = self._parse_next_weekday(original, baseline, next_day_match.group(1))
            return ParsedTime(trigger_time=trigger_time, matched_text=matched)

        explicit = self._parse_explicit_datetime(original, baseline)
        if explicit is not None:
            return explicit

        at_matches = list(re.finditer(r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?\b", original, re.IGNORECASE))
        if at_matches:
            match = at_matches[-1]
            clock_text = match.group(0)[2:].strip()
            trigger_time = self._combine_date_and_clock(baseline, clock_text)
            if trigger_time <= baseline:
                trigger_time += timedelta(days=1)
            return ParsedTime(trigger_time=trigger_time, matched_text=match.group(0))

        return None

    def parse_repeat(self, text: str, *, now: datetime | None = None) -> ParsedRepeat | None:
        """Parse recurring reminder phrases such as 'every day at 7 am'."""
        original = str(text or "").strip()
        if not original:
            return None

        baseline = self._normalize_dt(now or self._now_provider())
        lowered = original.lower()

        daily_match = re.search(r"\b(every day|daily)\b", lowered)
        if daily_match:
            clock_text, matched_text = self._extract_repeat_clock(original, daily_match.start(), daily_match.end())
            if clock_text is None:
                raise TimeParseError("I couldn't understand the daily reminder time.")
            trigger_time = self._combine_date_and_clock(baseline, clock_text)
            if trigger_time <= baseline:
                trigger_time += timedelta(days=1)
            return ParsedRepeat(
                trigger_time=trigger_time,
                repeat_rule="FREQ=DAILY;INTERVAL=1",
                matched_text=matched_text,
                label=f"Daily {self.format_clock(trigger_time)}",
            )

        weekday_match = re.search(
            r"\b(every weekday|every weekdays|weekdays)\b",
            lowered,
        )
        if weekday_match:
            clock_text, matched_text = self._extract_repeat_clock(original, weekday_match.start(), weekday_match.end())
            if clock_text is None:
                raise TimeParseError("I couldn't understand the weekday reminder time.")
            trigger_time = self._next_matching_day(
                baseline,
                clock_text=clock_text,
                weekday_indexes={0, 1, 2, 3, 4},
                strict_today=False,
            )
            return ParsedRepeat(
                trigger_time=trigger_time,
                repeat_rule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;INTERVAL=1",
                matched_text=matched_text,
                label=f"Weekdays {self.format_clock(trigger_time)}",
            )

        weekly_match = re.search(
            r"\bevery\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            lowered,
        )
        if weekly_match:
            day_name = weekly_match.group(1)
            clock_text, matched_text = self._extract_repeat_clock(original, weekly_match.start(), weekly_match.end())
            if clock_text is None:
                raise TimeParseError(f"I couldn't understand the weekly reminder time for {day_name}.")
            trigger_time = self._next_matching_day(
                baseline,
                clock_text=clock_text,
                weekday_indexes={_WEEKDAY_INDEX[day_name]},
                strict_today=False,
            )
            return ParsedRepeat(
                trigger_time=trigger_time,
                repeat_rule=f"FREQ=WEEKLY;BYDAY={self._weekday_rrule_name(day_name)};INTERVAL=1",
                matched_text=matched_text,
                label=f"Every {day_name.title()} {self.format_clock(trigger_time)}",
            )

        return None

    def next_occurrence(
        self,
        repeat_rule: str,
        *,
        dtstart: datetime,
        after: datetime,
    ) -> datetime | None:
        """Return the next occurrence for a stored RRULE."""
        if not repeat_rule:
            return None
        start = self._normalize_dt(dtstart)
        reference = self._normalize_dt(after)
        try:
            schedule = rrule.rrulestr(str(repeat_rule), dtstart=start)
            next_dt = schedule.after(reference, inc=False)
            return self._normalize_dt(next_dt) if next_dt is not None else None
        except Exception as exc:
            raise TimeParseError(f"Failed to compute the next recurring reminder time: {exc}") from exc

    def format_trigger_time(self, value: datetime, *, now: datetime | None = None) -> str:
        current = self._normalize_dt(now or self._now_provider())
        target = self._normalize_dt(value)
        if target.date() == current.date():
            return f"Today {self.format_clock(target)}"
        if target.date() == (current + timedelta(days=1)).date():
            return f"Tomorrow {self.format_clock(target)}"
        if target.year == current.year:
            return f"{target.strftime('%b %d')} {self.format_clock(target)}"
        return f"{target.strftime('%Y-%m-%d')} {self.format_clock(target)}"

    def describe_repeat(self, repeat_rule: str | None, trigger_time: datetime) -> str:
        if not repeat_rule:
            return self.format_trigger_time(trigger_time)

        rule = str(repeat_rule)
        if "FREQ=DAILY" in rule:
            return f"Daily {self.format_clock(trigger_time)}"
        if "BYDAY=MO,TU,WE,TH,FR" in rule:
            return f"Weekdays {self.format_clock(trigger_time)}"
        byday_match = re.search(r"BYDAY=([A-Z,]+)", rule)
        if byday_match:
            parts = [part.strip() for part in byday_match.group(1).split(",") if part.strip()]
            if len(parts) == 1:
                label = _RRULE_DAY_NAMES.get(parts[0], parts[0])
                return f"Every {label} {self.format_clock(trigger_time)}"
        return f"Recurring {self.format_clock(trigger_time)}"

    @staticmethod
    def format_clock(value: datetime) -> str:
        return value.strftime("%I:%M %p").lstrip("0")

    def _parse_tomorrow(self, original: str, baseline: datetime) -> tuple[datetime, str]:
        match = re.search(r"\btomorrow\b", original, re.IGNORECASE)
        if not match:
            raise TimeParseError("I couldn't parse 'tomorrow' in that reminder.")
        tail = original[match.end() :]
        clock_text, consumed = self._extract_clock_prefix(tail)
        if clock_text is None:
            raise TimeParseError("I couldn't understand the time after 'tomorrow'.")
        next_day = baseline + timedelta(days=1)
        trigger = self._combine_date_and_clock(next_day, clock_text)
        matched = original[match.start() : match.end() + consumed]
        return trigger, matched

    def _parse_next_weekday(self, original: str, baseline: datetime, day_name: str) -> tuple[datetime, str]:
        match = re.search(rf"\bnext\s+{re.escape(day_name)}\b", original, re.IGNORECASE)
        if not match:
            raise TimeParseError("I couldn't parse the next weekday in that reminder.")
        tail = original[match.end() :]
        clock_text, consumed = self._extract_clock_prefix(tail)
        if clock_text is None:
            raise TimeParseError(f"I couldn't understand the time after 'next {day_name}'.")
        target_weekday = _WEEKDAY_INDEX[day_name.lower()]
        trigger = self._next_matching_day(
            baseline,
            clock_text=clock_text,
            weekday_indexes={target_weekday},
            strict_today=True,
        )
        matched = original[match.start() : match.end() + consumed]
        return trigger, matched

    def _parse_explicit_datetime(self, original: str, baseline: datetime) -> ParsedTime | None:
        patterns = (
            r"\bon\s+(?P<value>\d{4}-\d{2}-\d{2}(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?)?)",
            r"\bon\s+(?P<value>\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?)?)",
            r"\bon\s+(?P<value>[A-Za-z]{3,9}\s+\d{1,2}(?:,\s*\d{4})?(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?)?)",
        )
        for pattern in patterns:
            match = re.search(pattern, original, re.IGNORECASE)
            if not match:
                continue
            value = match.group("value")
            try:
                parsed = date_parser.parse(value, default=baseline.replace(second=0, microsecond=0), fuzzy=False)
            except (ValueError, OverflowError):
                continue
            aware = self._normalize_dt(parsed)
            if aware <= baseline:
                raise TimeParseError("That reminder time is already in the past.")
            return ParsedTime(trigger_time=aware, matched_text=match.group(0))
        return None

    def _extract_repeat_clock(self, original: str, start: int, end: int) -> tuple[str | None, str]:
        tail = original[end:]
        clock_text, consumed = self._extract_clock_prefix(tail)
        if clock_text is None:
            return None, original[start:end]
        return clock_text, original[start : end + consumed]

    @staticmethod
    def _relative_delta(value: int, unit: str) -> timedelta:
        if unit.startswith("minute"):
            return timedelta(minutes=value)
        if unit.startswith("hour"):
            return timedelta(hours=value)
        if unit.startswith("day"):
            return timedelta(days=value)
        if unit.startswith("week"):
            return timedelta(weeks=value)
        raise TimeParseError(f"Unsupported reminder interval: {unit}")

    def _combine_date_and_clock(self, target_date: datetime, clock_text: str) -> datetime:
        try:
            parsed = date_parser.parse(
                clock_text,
                default=target_date.replace(second=0, microsecond=0),
                fuzzy=False,
            )
        except (ValueError, OverflowError) as exc:
            raise TimeParseError(f"I couldn't understand the time '{clock_text}'.") from exc
        aware = self._normalize_dt(parsed)
        return aware.replace(year=target_date.year, month=target_date.month, day=target_date.day, second=0, microsecond=0)

    def _next_matching_day(
        self,
        baseline: datetime,
        *,
        clock_text: str,
        weekday_indexes: set[int],
        strict_today: bool,
    ) -> datetime:
        for offset in range(0 if not strict_today else 1, 14):
            candidate_day = baseline + timedelta(days=offset)
            if candidate_day.weekday() not in weekday_indexes:
                continue
            candidate = self._combine_date_and_clock(candidate_day, clock_text)
            if candidate > baseline:
                return candidate
        raise TimeParseError("I couldn't compute the next occurrence for that recurring reminder.")

    @staticmethod
    def _extract_clock_prefix(text: str) -> tuple[str | None, int]:
        cleaned = str(text or "")
        before_remind = re.split(r"\bremind me\b", cleaned, maxsplit=1, flags=re.IGNORECASE)[0]
        match = _CLOCK_PREFIX.match(before_remind)
        if not match:
            return None, 0
        return match.group("clock"), match.end()

    @staticmethod
    def _weekday_rrule_name(day_name: str) -> str:
        return {
            "monday": "MO",
            "tuesday": "TU",
            "wednesday": "WE",
            "thursday": "TH",
            "friday": "FR",
            "saturday": "SA",
            "sunday": "SU",
        }[day_name.lower()]

    def _normalize_dt(self, value: datetime | None) -> datetime:
        if value is None:
            raise TimeParseError("Missing reminder datetime.")
        target_tz = self._target_tz()
        if value.tzinfo is None:
            return value.replace(tzinfo=target_tz)
        return value.astimezone(target_tz)

    def _target_tz(self):
        if self.timezone_name.lower() == "local":
            return tz.tzlocal()
        return tz.gettz(self.timezone_name) or tz.tzlocal()

    def _default_now(self) -> datetime:
        return datetime.now(self._target_tz())

