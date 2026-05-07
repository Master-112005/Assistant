"""
Background reminder scheduler and trigger delivery.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable

from core import settings, state
from core.logger import get_logger
from core.notifications import Notification, NotificationChannel, NotificationLevel, NotificationManager, build_default_desktop_backends
from core.reminders import Reminder, ReminderManager, ReminderStoreError

logger = get_logger(__name__)


class DesktopNotificationBackend:
    """Compatibility adapter for reminder-only desktop popup delivery."""

    def __init__(self) -> None:
        self._backends = build_default_desktop_backends()
        self._warned_missing_backend = False

    def notify(self, title: str, message: str) -> bool:
        if not settings.get("desktop_notifications"):
            return False

        notification = Notification(
            title=title,
            message=message,
            level=NotificationLevel.REMINDER,
            channel=NotificationChannel.DESKTOP,
            source="reminder_scheduler",
        )
        duration_seconds = max(1, int(settings.get("notification_duration_seconds") or 5))
        for backend in self._backends:
            try:
                if backend.notify(notification, duration_seconds=duration_seconds):
                    return True
            except Exception as exc:
                logger.warning("Reminder popup failed via %s: %s", getattr(backend, "name", "backend"), exc)

        if not self._warned_missing_backend:
            self._warned_missing_backend = True
            logger.warning("Desktop notifications are enabled but no notification backend is available.")
        return False


class ReminderScheduler:
    """Low-overhead scheduler loop that delivers due reminders."""

    def __init__(
        self,
        *,
        manager: ReminderManager,
        notification_backend: DesktopNotificationBackend | None = None,
        notification_manager: NotificationManager | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.manager = manager
        self.notification_backend = notification_backend or DesktopNotificationBackend()
        self.notification_manager = notification_manager
        self._now_provider = now_provider or self.manager.time_parser._now_provider
        self._tts_engine = None
        self._trigger_callback: Callable[[dict], None] | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def set_tts_engine(self, tts_engine) -> None:
        self._tts_engine = tts_engine
        if self.notification_manager is not None:
            self.notification_manager.set_tts_engine(tts_engine)

    def set_notification_manager(self, notification_manager: NotificationManager | None) -> None:
        self.notification_manager = notification_manager
        if self.notification_manager is not None and self._tts_engine is not None:
            self.notification_manager.set_tts_engine(self._tts_engine)

    def set_trigger_callback(self, callback: Callable[[dict], None] | None) -> None:
        self._trigger_callback = callback

    def start(self) -> None:
        if not settings.get("reminders_enabled"):
            state.scheduler_running = False
            logger.info("Reminder scheduler not started because reminders are disabled in settings.")
            return

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
        self.recover_pending()
        with self._lock:
            self._thread = threading.Thread(target=self._run_loop, name="ReminderScheduler", daemon=True)
            self._thread.start()
            state.scheduler_running = True
            logger.info("Scheduler started")

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        state.scheduler_running = False

    def tick(self) -> None:
        self.run_due_reminders()

    def run_due_reminders(self, now: datetime | None = None) -> list[Reminder]:
        current = self.manager.time_parser._normalize_dt(now or self._now_provider())
        try:
            due = self.manager.get_due_reminders(current)
        except ReminderStoreError as exc:
            logger.warning("Reminder scheduler could not query due reminders: %s", exc)
            return []

        for reminder in due:
            self.trigger(reminder, triggered_at=current)
        return due

    def trigger(self, reminder: Reminder, *, triggered_at: datetime | None = None) -> None:
        fired_at = self.manager.time_parser._normalize_dt(triggered_at or self._now_provider())
        title = "Nova Reminder"
        body = self._build_trigger_body(reminder)
        payload = {
            "type": "reminder",
            "reminder_id": reminder.id,
            "title": reminder.title,
            "message": reminder.message,
        }

        if self.notification_manager is not None:
            try:
                self.notification_manager.notify(
                    title,
                    body,
                    level=NotificationLevel.REMINDER,
                    channel=NotificationChannel.ALL,
                    speak=bool(settings.get("speak_reminders")),
                    source="reminder_scheduler",
                    payload=payload,
                )
            except Exception as exc:
                logger.warning("Reminder notification failed: %s", exc)
        else:
            try:
                self.notification_backend.notify(title, body)
            except Exception as exc:
                logger.warning("Reminder notification failed: %s", exc)

            if settings.get("speak_reminders"):
                if self._tts_engine is not None:
                    try:
                        self._tts_engine.speak(body)
                    except Exception as exc:
                        logger.warning("Reminder speech failed: %s", exc)
                else:
                    logger.warning("Reminder speech is enabled but no TTS engine is attached.")

        updated = None
        try:
            updated = self.manager.mark_triggered(int(reminder.id or 0), triggered_at=fired_at)
        except Exception as exc:
            logger.error("Failed to mark reminder id=%s as triggered: %s", reminder.id, exc)

        state.last_triggered_reminder = {
            "id": reminder.id,
            "title": reminder.title,
            "message": reminder.message,
            "triggered_at": fired_at.isoformat(),
            "repeat_rule": reminder.repeat_rule,
        }
        logger.info("Triggered reminder id=%s", reminder.id)

        if updated is not None and updated.repeat_rule and updated.enabled:
            logger.info("Rescheduled recurring reminder id=%s", updated.id)

        if self._trigger_callback is not None:
            try:
                self._trigger_callback(
                    {
                        "type": "reminder",
                        "reminder_id": reminder.id,
                        "title": reminder.title,
                        "message": body,
                        "triggered_at": fired_at.isoformat(),
                        "next_trigger_time": updated.trigger_time.isoformat() if updated and updated.enabled else None,
                    }
                )
            except Exception as exc:
                logger.warning("Reminder UI callback failed: %s", exc)

    def recover_pending(self) -> None:
        logger.info("Recovering pending reminders")
        self.run_due_reminders()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                logger.warning("Reminder scheduler tick failed: %s", exc)
            interval = max(1, int(settings.get("reminder_check_interval_seconds") or 15))
            self._stop_event.wait(interval)

    @staticmethod
    def _build_trigger_body(reminder: Reminder) -> str:
        message = str(reminder.message or "").strip()
        if not message or message.lower() == "reminder":
            return "Reminder due now."
        return f"Reminder: {message}"
