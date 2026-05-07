"""
Shared notification pipeline with queueing, real desktop delivery, in-app sinks,
voice alerts, and flood protection.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from queue import Empty, Full, Queue
from typing import Any, Callable, Protocol

from core import settings, state
from core.config import config
from core.logger import get_logger

logger = get_logger(__name__)

try:
    from plyer import notification as plyer_notification
except ImportError:  # pragma: no cover - optional dependency
    plyer_notification = None

try:
    from win10toast import ToastNotifier
except ImportError:  # pragma: no cover - optional dependency
    ToastNotifier = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class NotificationLevel(StrEnum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    REMINDER = "reminder"


class NotificationChannel(StrEnum):
    DESKTOP = "desktop"
    IN_APP = "in_app"
    VOICE = "voice"
    ALL = "all"


@dataclass(slots=True)
class Notification:
    title: str
    message: str
    level: NotificationLevel | str = NotificationLevel.INFO
    channel: NotificationChannel | str = NotificationChannel.ALL
    speak: bool = False
    source: str = "system"
    payload: dict[str, Any] | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=_utc_now)
    expires_at: datetime | None = None
    duplicate_count: int = 1

    def __post_init__(self) -> None:
        self.title = str(self.title or "").strip() or "Notification"
        self.message = str(self.message or "").strip()
        self.level = NotificationLevel(str(self.level or NotificationLevel.INFO).lower())
        self.channel = NotificationChannel(str(self.channel or NotificationChannel.ALL).lower())
        self.source = str(self.source or "system").strip() or "system"
        self.payload = dict(self.payload or {})
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            self.expires_at = self.expires_at.replace(tzinfo=timezone.utc)

    def dedupe_key(self) -> tuple[str, str, str, str, str]:
        normalized_message = " ".join(self.message.casefold().split())
        return (
            self.level.value,
            self.channel.value,
            self.source.casefold(),
            self.title.casefold(),
            normalized_message,
        )

    def rendered_message(self) -> str:
        if self.duplicate_count > 1:
            return f"{self.message} (x{self.duplicate_count})"
        return self.message

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "message": self.message,
            "rendered_message": self.rendered_message(),
            "level": self.level.value,
            "channel": self.channel.value,
            "speak": self.speak,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "source": self.source,
            "payload": dict(self.payload or {}),
            "duplicate_count": self.duplicate_count,
        }


class DesktopNotificationBackend(Protocol):
    name: str

    def notify(self, notification: Notification, *, duration_seconds: int) -> bool:
        ...


class PlyerDesktopNotificationBackend:
    name = "plyer"

    def notify(self, notification: Notification, *, duration_seconds: int) -> bool:
        if plyer_notification is None:
            return False
        plyer_notification.notify(
            title=notification.title,
            message=notification.rendered_message(),
            app_name=config.APP_NAME,
            timeout=duration_seconds,
        )
        return True


class Win10ToastDesktopNotificationBackend:
    name = "win10toast"

    def __init__(self) -> None:
        self._toast = ToastNotifier() if ToastNotifier is not None else None

    def notify(self, notification: Notification, *, duration_seconds: int) -> bool:
        if self._toast is None:
            return False
        self._toast.show_toast(
            notification.title,
            notification.rendered_message(),
            duration=duration_seconds,
            threaded=True,
        )
        return True


def build_default_desktop_backends() -> list[DesktopNotificationBackend]:
    backends: list[DesktopNotificationBackend] = []
    if plyer_notification is not None:
        backends.append(PlyerDesktopNotificationBackend())
    if ToastNotifier is not None:
        backends.append(Win10ToastDesktopNotificationBackend())
    return backends


NotificationHandler = Callable[[Notification], None]

_global_notification_manager: NotificationManager | None = None


class NotificationManager:
    """Threaded notification dispatcher with rate limiting and duplicate suppression."""

    def __init__(
        self,
        *,
        tts_engine=None,
        desktop_backends: list[DesktopNotificationBackend] | None = None,
        max_queue_size: int | None = None,
        history_limit: int | None = None,
    ) -> None:
        queue_size = max(8, int(max_queue_size or settings.get("notification_queue_max_size") or 128))
        recent_limit = max(10, int(history_limit or settings.get("notification_history_limit") or 50))
        self._queue: Queue[Notification | None] = Queue(maxsize=queue_size)
        self._recent: deque[Notification] = deque(maxlen=recent_limit)
        self._rate_window: deque[float] = deque()
        self._dedupe_index: dict[tuple[str, str, str, str, str], tuple[float, Notification]] = {}
        self._in_app_handlers: list[NotificationHandler] = []
        self._desktop_backends = list(desktop_backends) if desktop_backends is not None else build_default_desktop_backends()
        self._tts_engine = tts_engine
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._warned_no_desktop_backend = False

    def set_tts_engine(self, tts_engine) -> None:
        self._tts_engine = tts_engine

    def register_in_app_handler(self, handler: NotificationHandler) -> None:
        with self._lock:
            if handler not in self._in_app_handlers:
                self._in_app_handlers.append(handler)

    def unregister_in_app_handler(self, handler: NotificationHandler) -> None:
        with self._lock:
            self._in_app_handlers = [item for item in self._in_app_handlers if item is not handler]

    def start(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                state.notifications_ready = not self._stop_event.is_set()
                return
            self._stop_event = threading.Event()
            self._thread = threading.Thread(target=self._run, name="NotificationManager", daemon=True)
            self._thread.start()
            state.notifications_ready = True
            self._sync_state_locked()
        logger.info("Notification manager started")

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            state.notifications_ready = False
            if thread is None:
                self._sync_state_locked()
                return
            self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except Full:
            pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        stopped = thread is None or not thread.is_alive()
        with self._lock:
            if stopped and self._thread is thread:
                self._thread = None
            self._sync_state_locked()
        if stopped:
            logger.info("Notification manager stopped")
            return
        logger.warning("Notification manager did not stop within timeout")

    def notify(
        self,
        title: str,
        message: str,
        *,
        level: NotificationLevel | str = NotificationLevel.INFO,
        channel: NotificationChannel | str = NotificationChannel.ALL,
        speak: bool = False,
        expires_at: datetime | None = None,
        source: str = "system",
        payload: dict[str, Any] | None = None,
    ) -> Notification | None:
        created_at = _utc_now()
        if expires_at is None:
            duration_seconds = self._duration_seconds()
            expires_at = created_at + timedelta(seconds=duration_seconds)
        notification = Notification(
            title=title,
            message=message,
            level=level,
            channel=channel,
            speak=speak,
            created_at=created_at,
            expires_at=expires_at,
            source=source,
            payload=payload,
        )
        if not self.enqueue(notification):
            return None
        return notification

    def enqueue(self, notification: Notification) -> bool:
        if not settings.get("notifications_enabled"):
            logger.info("Notification skipped because notifications are disabled: %s", notification.title)
            return False

        self.start()

        with self._lock:
            if self._is_duplicate_locked(notification):
                self._sync_state_locked()
                logger.info("Duplicate notification suppressed: %s", notification.title)
                return False

            if self._is_rate_limited_locked():
                self._sync_state_locked()
                logger.warning("Notification rate limited: %s", notification.title)
                return False

            try:
                self._queue.put_nowait(notification)
            except Full:
                self._sync_state_locked()
                logger.warning("Queue overflow: %s", notification.title)
                return False

            self._rate_window.append(time.monotonic())
            self._remember_notification_locked(notification)
            self._sync_state_locked()

        logger.info("Notification queued: %s", notification.title)
        return True

    def dispatch(self, notification: Notification) -> None:
        delivered_channels: list[str] = []

        if notification.channel in {NotificationChannel.DESKTOP, NotificationChannel.ALL}:
            if self.show_desktop(notification):
                delivered_channels.append(NotificationChannel.DESKTOP.value)

        if notification.channel in {NotificationChannel.IN_APP, NotificationChannel.ALL}:
            if self.show_in_app(notification):
                delivered_channels.append(NotificationChannel.IN_APP.value)

        should_speak = notification.channel == NotificationChannel.VOICE or bool(notification.speak)
        if should_speak and self.speak(notification):
            delivered_channels.append(NotificationChannel.VOICE.value)

        if not delivered_channels:
            logger.warning("Notification had no available delivery channels: %s", notification.title)

        notification.payload["delivered_channels"] = delivered_channels
        with self._lock:
            self._push_recent_locked(notification)
            self._sync_state_locked(last_notification=notification)
        logger.info(
            "Notification delivered: %s via %s",
            notification.title,
            ", ".join(delivered_channels) if delivered_channels else "none",
        )

    def show_desktop(self, notification: Notification) -> bool:
        if not settings.get("desktop_notifications"):
            return False

        duration_seconds = self._duration_seconds()
        for backend in self._desktop_backends:
            try:
                if backend.notify(notification, duration_seconds=duration_seconds):
                    logger.info("Desktop notification shown via %s", backend.name)
                    return True
            except Exception as exc:
                logger.warning("Popup failed via %s: %s", backend.name, exc)

        if not self._warned_no_desktop_backend:
            self._warned_no_desktop_backend = True
            logger.warning("Popup failed: no Windows desktop notification backend is available.")
        return False

    def show_in_app(self, notification: Notification) -> bool:
        if not settings.get("in_app_notifications"):
            return False

        with self._lock:
            handlers = list(self._in_app_handlers)

        delivered = False
        for handler in handlers:
            try:
                handler(notification)
                delivered = True
            except Exception as exc:
                logger.warning("In-app notification handler failed: %s", exc)

        if delivered:
            logger.info("In-app notification shown: %s", notification.title)
        return delivered

    def speak(self, notification: Notification) -> bool:
        if not settings.get("voice_notifications"):
            return False
        if self._tts_engine is None:
            logger.warning("Voice alert unavailable: no TTS engine attached.")
            return False

        text = self._voice_text(notification)
        if not text:
            return False

        try:
            self._tts_engine.speak(text)
            logger.info("Voice alert spoken: %s", notification.title)
            return True
        except Exception as exc:
            logger.warning("Voice alert failed: %s", exc)
            return False

    def clear_queue(self) -> None:
        cleared = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                break
            else:
                if item is not None:
                    cleared += 1
                self._queue.task_done()
        with self._lock:
            self._sync_state_locked()
        logger.info("Notification queue cleared: %d item(s)", cleared)

    def recent(self, limit: int = 20) -> list[Notification]:
        with self._lock:
            return list(self._recent)[: max(0, int(limit))]

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                notification = self._queue.get(timeout=0.25)
            except Empty:
                continue

            if notification is None:
                self._queue.task_done()
                continue

            try:
                self.dispatch(notification)
            except Exception as exc:
                logger.error("Notification dispatch failed: %s", exc)
            finally:
                self._queue.task_done()
                with self._lock:
                    self._sync_state_locked()

    def _duration_seconds(self) -> int:
        return max(1, int(settings.get("notification_duration_seconds") or 5))

    def _voice_text(self, notification: Notification) -> str:
        message = notification.message or notification.title
        if notification.level == NotificationLevel.REMINDER and not message.lower().startswith("reminder"):
            return f"Reminder: {message}"
        if notification.level == NotificationLevel.ERROR and not message.lower().startswith("error"):
            return f"Error: {message}"
        return message

    def _is_rate_limited_locked(self) -> bool:
        limit = int(settings.get("notification_rate_limit_per_min") or 0)
        if limit <= 0:
            return False
        now = time.monotonic()
        while self._rate_window and (now - self._rate_window[0]) > 60.0:
            self._rate_window.popleft()
        return len(self._rate_window) >= limit

    def _remember_notification_locked(self, notification: Notification) -> None:
        key = notification.dedupe_key()
        self._dedupe_index[key] = (time.monotonic(), notification)

    def _is_duplicate_locked(self, notification: Notification) -> bool:
        window_seconds = max(1, int(settings.get("notification_dedupe_window_seconds") or 10))
        now = time.monotonic()
        stale_keys = [key for key, (ts, _item) in self._dedupe_index.items() if (now - ts) > window_seconds]
        for stale_key in stale_keys:
            self._dedupe_index.pop(stale_key, None)

        key = notification.dedupe_key()
        existing = self._dedupe_index.get(key)
        if existing is None:
            return False

        _ts, original = existing
        original.duplicate_count += 1
        original.payload["duplicate_count"] = original.duplicate_count
        self._dedupe_index[key] = (now, original)
        return True

    def _push_recent_locked(self, notification: Notification) -> None:
        try:
            self._recent.remove(notification)
        except ValueError:
            pass
        self._recent.appendleft(notification)

    def _sync_state_locked(self, last_notification: Notification | None = None) -> None:
        state.notification_queue_size = self._queue.qsize()
        latest = last_notification or (self._recent[0] if self._recent else None)
        state.last_notification = latest.to_dict() if latest is not None else {}
        state.recent_notifications = [item.to_dict() for item in self._recent]


def set_global_notification_manager(manager: NotificationManager | None) -> None:
    global _global_notification_manager
    _global_notification_manager = manager


def get_global_notification_manager() -> NotificationManager | None:
    return _global_notification_manager
