from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from dateutil import tz

from core import settings, state
from core.notifications import NotificationManager, set_global_notification_manager
from core.processor import CommandProcessor
from core.reminders import ReminderManager
from core.scheduler import ReminderScheduler
from core.time_parser import ReminderTimeParser
from skills.reminders import ReminderSkill


LOCAL_TZ = tz.tzlocal()


class NotificationStub:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> bool:
        self.calls.append((title, message))
        return True


class TTSStub:
    def __init__(self):
        self.spoken: list[str] = []

    def speak(self, text: str):
        self.spoken.append(text)


class DummyLauncher:
    def launch_by_name(self, name: str):
        raise AssertionError("Launcher should not be used in reminder tests")


class DummyLLM:
    def is_available(self, force_refresh: bool = False) -> bool:
        return False

    def extract_intent(self, _text: str):
        return None


class ReminderOnlySkillsManager:
    def __init__(self, reminder_skill: ReminderSkill):
        self.browser_controller = object()
        self._reminder_skill = reminder_skill

    def load_builtin_skills(self):
        return None

    def execute_with_skill(self, context, intent: str, command: str):
        if not self._reminder_skill.can_handle(context, intent, command):
            return None
        state.active_skill = self._reminder_skill.name()
        state.last_skill_used = self._reminder_skill.name()
        return self._reminder_skill.execute(command, context)

    def set_confirm_callback(self, callback):
        return None

    @property
    def file_skill(self):
        return None

    @property
    def clipboard_skill(self):
        return None

    @property
    def reminder_skill(self):
        return self._reminder_skill


@pytest.fixture(autouse=True)
def reset_reminder_state():
    settings.reset_defaults()
    settings.set("context_detection_enabled", False)
    settings.set("context_engine_enabled", False)
    settings.set("reminders_enabled", True)
    settings.set("reminder_check_interval_seconds", 1)
    settings.set("speak_reminders", True)
    settings.set("desktop_notifications", True)
    settings.set("default_timezone", "local")
    state.active_skill = ""
    state.last_skill_used = ""
    state.last_successful_action = ""
    state.last_response = ""
    state.last_intent = ""
    state.last_entities = {}
    state.pending_confirmation = {}
    state.scheduler_running = False
    state.next_reminder_time = ""
    state.last_triggered_reminder = {}
    state.reminder_count = 0
    state.notifications_ready = False
    state.notification_queue_size = 0
    state.last_notification = {}
    state.recent_notifications = []
    set_global_notification_manager(None)
    yield
    set_global_notification_manager(None)


def _fixed_now(hour: int = 10, minute: int = 0) -> datetime:
    return datetime(2026, 4, 23, hour, minute, tzinfo=LOCAL_TZ)


def _parser(now_value: datetime) -> ReminderTimeParser:
    return ReminderTimeParser(now_provider=lambda: now_value)


def test_time_parser_parses_relative_and_tomorrow_phrases():
    now_value = _fixed_now()
    parser = _parser(now_value)

    relative = parser.parse_time("in 20 minutes remind me to drink water")
    tomorrow = parser.parse_time("remind me to call mom tomorrow 6 am")

    assert relative is not None
    assert relative.trigger_time == now_value + timedelta(minutes=20)
    assert tomorrow is not None
    assert tomorrow.trigger_time.date() == (now_value + timedelta(days=1)).date()
    assert tomorrow.trigger_time.hour == 6
    assert tomorrow.trigger_time.minute == 0


def test_reminder_skill_rewrites_timer_commands(tmp_path):
    now_value = _fixed_now()
    parser = _parser(now_value)
    manager = ReminderManager(db_path=tmp_path / "reminders.db", time_parser=parser)
    skill = ReminderSkill(manager=manager, scheduler=ReminderScheduler(manager=manager), time_parser=parser)

    result = skill.execute("set timer for 5 min", {})

    reminders = manager.list_reminders()
    assert result.success is True
    assert result.intent == "reminder_create"
    assert reminders[0].message == "timer"
    assert reminders[0].trigger_time == now_value + timedelta(minutes=5)


def test_time_parser_parses_daily_and_weekly_repeat_rules():
    now_value = _fixed_now()
    parser = _parser(now_value)

    daily = parser.parse_repeat("every day at 7 am remind me to exercise")
    friday_parser = _parser(datetime(2026, 4, 24, 10, 0, tzinfo=LOCAL_TZ))
    weekly = friday_parser.parse_repeat("every friday 5 pm remind me to submit report")

    assert daily is not None
    assert daily.repeat_rule == "FREQ=DAILY;INTERVAL=1"
    assert daily.trigger_time.date() == (now_value + timedelta(days=1)).date()
    assert daily.trigger_time.hour == 7

    assert weekly is not None
    assert weekly.repeat_rule == "FREQ=WEEKLY;BYDAY=FR;INTERVAL=1"
    assert weekly.trigger_time.hour == 17
    assert weekly.trigger_time.date() == datetime(2026, 4, 24, 10, 0, tzinfo=LOCAL_TZ).date()


def test_reminder_manager_creates_one_time_and_relative_reminders(tmp_path):
    now_value = _fixed_now()
    parser = _parser(now_value)
    manager = ReminderManager(db_path=tmp_path / "reminders.db", time_parser=parser)

    one_time = manager.create_from_natural("Remind me to call mom tomorrow 6 am")
    relative = manager.create_from_natural("In 1 minute remind me to drink water")

    reminders = manager.list_reminders()

    assert one_time.message == "call mom"
    assert relative.trigger_time == now_value + timedelta(minutes=1)
    assert len(reminders) == 2
    manager.close()


def test_reminder_manager_creates_daily_recurring_reminder(tmp_path):
    now_value = _fixed_now()
    parser = _parser(now_value)
    manager = ReminderManager(db_path=tmp_path / "reminders.db", time_parser=parser)

    reminder = manager.create_from_natural("Every day at 7 AM remind me to exercise")

    assert reminder.repeat_rule == "FREQ=DAILY;INTERVAL=1"
    assert reminder.enabled is True
    assert "Daily" in parser.describe_repeat(reminder.repeat_rule, reminder.trigger_time)
    manager.close()


def test_scheduler_triggers_due_reminder_with_notification_tts_and_callback(tmp_path):
    now_value = _fixed_now()
    parser = _parser(now_value)
    manager = ReminderManager(db_path=tmp_path / "reminders.db", time_parser=parser)
    reminder = manager.create_reminder("drink water", now_value + timedelta(minutes=1))

    notifier = NotificationStub()
    tts = TTSStub()
    events: list[dict] = []
    scheduler = ReminderScheduler(manager=manager, notification_backend=notifier, now_provider=lambda: now_value)
    scheduler.set_tts_engine(tts)
    scheduler.set_trigger_callback(events.append)

    due = scheduler.run_due_reminders(now=now_value + timedelta(minutes=2))
    updated = manager.get_by_id(int(reminder.id))

    assert len(due) == 1
    assert notifier.calls == [("Nova Reminder", "Reminder: drink water")]
    assert tts.spoken == ["Reminder: drink water"]
    assert events and events[0]["message"] == "Reminder: drink water"
    assert updated is not None
    assert updated.enabled is False
    assert state.last_triggered_reminder["id"] == reminder.id
    manager.close()


def test_reminder_skill_lists_deletes_and_toggles_reminders(tmp_path):
    now_value = _fixed_now()
    parser = _parser(now_value)
    manager = ReminderManager(db_path=tmp_path / "reminders.db", time_parser=parser)
    skill = ReminderSkill(manager=manager, scheduler=ReminderScheduler(manager=manager), time_parser=parser)

    first = skill.execute("remind me to call mom tomorrow 6 am", {})
    second = skill.execute("every day at 7 am remind me to exercise", {})
    listing = skill.execute("list reminders", {})
    disabled = skill.execute(f"disable reminder {manager.list_reminders()[0].id}", {})
    enabled = skill.execute(f"enable reminder {manager.list_reminders()[0].id}", {})
    deleted_id = manager.list_reminders()[-1].id
    deleted = skill.execute(f"delete reminder {deleted_id}", {})

    assert first.success is True
    assert second.success is True
    assert listing.success is True
    assert "call mom" in listing.response.lower()
    assert disabled.success is True
    assert enabled.success is True
    assert deleted.success is True
    assert manager.get_by_id(int(deleted_id)) is None
    skill.shutdown()


def test_reminder_persists_and_recovery_triggers_after_restart(tmp_path):
    baseline = _fixed_now()
    create_parser = _parser(baseline)
    db_path = tmp_path / "reminders.db"

    manager_a = ReminderManager(db_path=db_path, time_parser=create_parser)
    created = manager_a.create_reminder("submit report", baseline + timedelta(minutes=1))
    manager_a.close()

    recovery_time = baseline + timedelta(minutes=5)
    recovery_parser = _parser(recovery_time)
    manager_b = ReminderManager(db_path=db_path, time_parser=recovery_parser)
    notifier = NotificationStub()
    tts = TTSStub()
    events: list[dict] = []
    scheduler = ReminderScheduler(manager=manager_b, notification_backend=notifier, now_provider=lambda: recovery_time)
    scheduler.set_tts_engine(tts)
    scheduler.set_trigger_callback(events.append)

    persisted = manager_b.list_reminders()
    scheduler.recover_pending()
    updated = manager_b.get_by_id(int(created.id))

    assert len(persisted) == 1
    assert notifier.calls == [("Nova Reminder", "Reminder: submit report")]
    assert tts.spoken == ["Reminder: submit report"]
    assert events and events[0]["reminder_id"] == created.id
    assert updated is not None
    assert updated.enabled is False
    manager_b.close()


def test_scheduler_uses_shared_notification_manager_when_attached(tmp_path):
    now_value = _fixed_now()
    parser = _parser(now_value)
    manager = ReminderManager(db_path=tmp_path / "reminders.db", time_parser=parser)
    reminder = manager.create_reminder("stretch", now_value + timedelta(minutes=1))

    class DesktopBackendStub:
        name = "stub"

        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        def notify(self, notification, *, duration_seconds: int) -> bool:
            self.calls.append((notification.title, notification.rendered_message()))
            return True

    desktop = DesktopBackendStub()
    tts = TTSStub()
    notification_manager = NotificationManager(tts_engine=tts, desktop_backends=[desktop])
    scheduler = ReminderScheduler(
        manager=manager,
        notification_manager=notification_manager,
        now_provider=lambda: now_value,
    )

    scheduler.run_due_reminders(now=now_value + timedelta(minutes=2))

    assert desktop.calls == [("Nova Reminder", "Reminder: stretch")]
    assert tts.spoken == ["Reminder: stretch"]
    assert state.last_notification["title"] == "Nova Reminder"
    notification_manager.stop()
    manager.close()


def test_invalid_time_phrase_returns_error_through_processor(tmp_path):
    now_value = _fixed_now()
    parser = _parser(now_value)
    manager = ReminderManager(db_path=tmp_path / "reminders.db", time_parser=parser)
    skill = ReminderSkill(manager=manager, scheduler=ReminderScheduler(manager=manager), time_parser=parser)
    processor = CommandProcessor(
        launcher=DummyLauncher(),
        llm_client=DummyLLM(),
        skills_manager=ReminderOnlySkillsManager(skill),
    )

    result = processor.process("remind me to call mom sometime later")

    assert result["success"] is False
    assert result["error"] == "invalid_time"
    assert "couldn't understand" in result["response"].lower()
    processor.shutdown()
