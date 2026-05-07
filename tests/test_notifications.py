from __future__ import annotations

import time

import pytest

from core import settings, state
from core.alerts import notify_success
from core.notifications import Notification, NotificationManager, set_global_notification_manager


class DesktopBackendStub:
    name = "stub"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, notification: Notification, *, duration_seconds: int) -> bool:
        self.calls.append((notification.title, notification.rendered_message()))
        return True


class TTSStub:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    start = time.perf_counter()
    while time.perf_counter() - start < timeout:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(autouse=True)
def reset_notification_state():
    settings.reset_defaults()
    settings.set("notification_rate_limit_per_min", 10)
    settings.set("notification_duration_seconds", 1)
    settings.set("notification_dedupe_window_seconds", 10)
    settings.set("desktop_notifications", True)
    settings.set("in_app_notifications", True)
    settings.set("voice_notifications", True)
    state.notifications_ready = False
    state.notification_queue_size = 0
    state.last_notification = {}
    state.recent_notifications = []
    set_global_notification_manager(None)
    yield
    set_global_notification_manager(None)


def test_notification_manager_dispatches_all_channels():
    desktop = DesktopBackendStub()
    tts = TTSStub()
    in_app: list[Notification] = []
    manager = NotificationManager(tts_engine=tts, desktop_backends=[desktop])
    manager.register_in_app_handler(in_app.append)

    manager.notify("Reminder", "Call mom now", level="reminder", channel="all", speak=True, source="test")

    assert _wait_for(lambda: len(desktop.calls) == 1 and len(in_app) == 1 and len(tts.spoken) == 1)
    assert desktop.calls == [("Reminder", "Call mom now")]
    assert in_app[0].title == "Reminder"
    assert tts.spoken == ["Reminder: Call mom now"]
    assert state.notifications_ready is True
    assert state.last_notification["title"] == "Reminder"
    manager.stop()


def test_notification_manager_suppresses_duplicates():
    desktop = DesktopBackendStub()
    manager = NotificationManager(desktop_backends=[desktop])

    first = manager.notify("Warning", "Network lost", level="warning", channel="desktop", source="network")
    second = manager.notify("Warning", "Network lost", level="warning", channel="desktop", source="network")

    assert first is not None
    assert second is None
    assert _wait_for(lambda: len(desktop.calls) == 1 and len(manager.recent(1)) == 1)
    recent = manager.recent(1)
    assert recent[0].duplicate_count == 2
    assert desktop.calls[0][0] == "Warning"
    assert desktop.calls[0][1] in {"Network lost", "Network lost (x2)"}
    manager.stop()


def test_notification_manager_rate_limits_unique_alerts():
    settings.set("notification_rate_limit_per_min", 2)
    desktop = DesktopBackendStub()
    manager = NotificationManager(desktop_backends=[desktop])

    manager.notify("One", "First", channel="desktop", source="test")
    manager.notify("Two", "Second", channel="desktop", source="test")
    dropped = manager.notify("Three", "Third", channel="desktop", source="test")

    assert dropped is None
    assert _wait_for(lambda: len(desktop.calls) == 2)
    assert [title for title, _message in desktop.calls] == ["One", "Two"]
    manager.stop()


def test_notification_manager_falls_back_to_in_app_when_desktop_disabled():
    settings.set("desktop_notifications", False)
    desktop = DesktopBackendStub()
    in_app: list[Notification] = []
    manager = NotificationManager(desktop_backends=[desktop])
    manager.register_in_app_handler(in_app.append)

    manager.notify("Task complete", "report.pdf saved", level="success", channel="all", source="files")

    assert _wait_for(lambda: len(in_app) == 1)
    assert desktop.calls == []
    assert in_app[0].title == "Task complete"
    manager.stop()


def test_alert_helpers_use_global_notification_manager():
    desktop = DesktopBackendStub()
    manager = NotificationManager(desktop_backends=[desktop])
    set_global_notification_manager(manager)

    notification = notify_success("Complete", "Backup finished", channel="desktop", source="test")

    assert notification is not None
    assert _wait_for(lambda: len(desktop.calls) == 1)
    assert desktop.calls == [("Complete", "Backup finished")]
    manager.stop()
