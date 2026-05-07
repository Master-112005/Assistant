"""
Convenience helpers for common notification levels.
"""
from __future__ import annotations

from typing import Any

from core import settings
from core.notifications import (
    Notification,
    NotificationChannel,
    NotificationLevel,
    get_global_notification_manager,
)


def _send(
    title: str,
    message: str,
    *,
    level: NotificationLevel | str,
    channel: NotificationChannel | str,
    speak: bool = False,
    source: str = "system",
    payload: dict[str, Any] | None = None,
) -> Notification | None:
    manager = get_global_notification_manager()
    if manager is None:
        return None
    return manager.notify(
        title,
        message,
        level=level,
        channel=channel,
        speak=speak,
        source=source,
        payload=payload,
    )


def notify_info(
    title: str,
    message: str,
    *,
    channel: NotificationChannel | str = NotificationChannel.IN_APP,
    speak: bool = False,
    source: str = "system",
    payload: dict[str, Any] | None = None,
) -> Notification | None:
    return _send(
        title,
        message,
        level=NotificationLevel.INFO,
        channel=channel,
        speak=speak,
        source=source,
        payload=payload,
    )


def notify_success(
    title: str,
    message: str,
    *,
    channel: NotificationChannel | str = NotificationChannel.IN_APP,
    speak: bool = False,
    source: str = "system",
    payload: dict[str, Any] | None = None,
) -> Notification | None:
    return _send(
        title,
        message,
        level=NotificationLevel.SUCCESS,
        channel=channel,
        speak=speak,
        source=source,
        payload=payload,
    )


def notify_error(
    title: str,
    message: str,
    *,
    channel: NotificationChannel | str = NotificationChannel.IN_APP,
    speak: bool = False,
    source: str = "system",
    payload: dict[str, Any] | None = None,
) -> Notification | None:
    return _send(
        title,
        message,
        level=NotificationLevel.ERROR,
        channel=channel,
        speak=speak,
        source=source,
        payload=payload,
    )


def notify_warning(
    title: str,
    message: str,
    *,
    channel: NotificationChannel | str = NotificationChannel.IN_APP,
    speak: bool = False,
    source: str = "system",
    payload: dict[str, Any] | None = None,
) -> Notification | None:
    return _send(
        title,
        message,
        level=NotificationLevel.WARNING,
        channel=channel,
        speak=speak,
        source=source,
        payload=payload,
    )


def notify_reminder(
    title: str,
    message: str,
    *,
    channel: NotificationChannel | str = NotificationChannel.ALL,
    speak: bool | None = None,
    source: str = "system",
    payload: dict[str, Any] | None = None,
) -> Notification | None:
    should_speak = bool(settings.get("speak_reminders")) if speak is None else speak
    return _send(
        title,
        message,
        level=NotificationLevel.REMINDER,
        channel=channel,
        speak=should_speak,
        source=source,
        payload=payload,
    )
