"""
Qt widgets for in-app notification toasts and a lightweight alert center.
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QPoint, QTimer, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from core.notifications import Notification, NotificationLevel
from core.theme_manager import theme_manager
from ui.animations import fade_out, slide_in


def _theme_for(level: NotificationLevel) -> dict[str, str]:
    tokens = theme_manager.theme_tokens()
    key = str(level.value if isinstance(level, NotificationLevel) else "info")
    accent = str(tokens.get(f"notification_{key}", tokens.get("accent", "#0078d4")))
    return {"accent": accent, "badge": accent, "text": str(tokens.get("accent_text", "#ffffff"))}


def _format_timestamp(created_at: datetime) -> str:
    return created_at.astimezone().strftime("%H:%M:%S")


class NotificationCard(QFrame):
    """Shared visual card for notification center rows and transient toasts."""

    def __init__(self, notification: Notification, *, compact: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.notification = notification
        theme = _theme_for(notification.level)
        self.setObjectName("NotificationCard")
        self.setProperty("level", notification.level.value)
        self.setFrameShape(QFrame.StyledPanel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6 if compact else 8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        level_badge = QLabel(notification.level.value.upper())
        level_badge.setAlignment(Qt.AlignCenter)
        level_badge.setStyleSheet(
            f"""
            background-color: {theme['badge']};
            color: {theme['text']};
            border-radius: 6px;
            padding: 2px 8px;
            font-size: 10px;
            font-weight: bold;
            """
        )
        header.addWidget(level_badge, 0, Qt.AlignLeft)

        title_label = QLabel(notification.title)
        title_label.setWordWrap(True)
        title_label.setObjectName("SectionTitle")
        header.addWidget(title_label, 1)

        timestamp_label = QLabel(_format_timestamp(notification.created_at))
        timestamp_label.setObjectName("MetaLabel")
        header.addWidget(timestamp_label, 0, Qt.AlignRight)
        layout.addLayout(header)

        message_label = QLabel(notification.rendered_message())
        message_label.setWordWrap(True)
        message_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        message_label.setObjectName("MetaLabel")
        layout.addWidget(message_label)


class NotificationToast(NotificationCard):
    """Transient toast banner that auto-dismisses after the configured duration."""

    dismissed = Signal(object)

    def __init__(self, notification: Notification, *, duration_ms: int, parent=None) -> None:
        super().__init__(notification, compact=True, parent=parent)
        self.setObjectName("NotificationToast")
        self.setMaximumWidth(340)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._dismiss)
        self._timer.start(max(1000, int(duration_ms)))

    def _dismiss(self) -> None:
        animation = fade_out(self, duration_ms=140, hide=False)
        if animation is None:
            self.dismissed.emit(self)
        else:
            animation.finished.connect(lambda: self.dismissed.emit(self))


class ToastOverlay(QWidget):
    """Top-right stack of transient in-app toast notifications."""

    def __init__(self, parent=None, *, max_visible: int = 3) -> None:
        super().__init__(parent)
        self._toasts: list[NotificationToast] = []
        self._max_visible = max(1, int(max_visible))
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.hide()

    def show_notification(self, notification: Notification, *, duration_seconds: int) -> None:
        toast = NotificationToast(notification, duration_ms=max(1000, int(duration_seconds * 1000)), parent=self)
        toast.dismissed.connect(self._remove_toast)
        toast.show()
        toast.raise_()
        self._toasts.insert(0, toast)

        while len(self._toasts) > self._max_visible:
            stale = self._toasts.pop()
            stale.deleteLater()

        self._reflow()
        slide_in(toast, start_offset=QPoint(24, 0), duration_ms=160)
        self.show()
        self.raise_()

    def sync_to_parent(self) -> None:
        if self.parentWidget() is None:
            return
        self.setGeometry(self.parentWidget().rect())

    def visible_count(self) -> int:
        return len(self._toasts)

    def _remove_toast(self, toast: NotificationToast) -> None:
        self._toasts = [item for item in self._toasts if item is not toast]
        self._reflow()
        if not self._toasts:
            self.hide()

    def _reflow(self) -> None:
        self.sync_to_parent()
        if not self._toasts:
            return

        margin = 12
        spacing = 10
        y = margin
        width = self.width()
        for toast in self._toasts:
            toast.adjustSize()
            toast_width = min(toast.sizeHint().width(), 340)
            toast_height = toast.sizeHint().height()
            toast.setGeometry(width - toast_width - margin, y, toast_width, toast_height)
            y += toast_height + spacing


class NotificationCenterWidget(QFrame):
    """Compact in-app panel showing recent notifications."""

    def __init__(self, parent=None, *, max_items: int = 6) -> None:
        super().__init__(parent)
        self._max_items = max(1, int(max_items))
        self._items: list[NotificationCard] = []
        self.setObjectName("NotificationCenter")
        self.setFrameShape(QFrame.StyledPanel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)

        title = QLabel("Notifications")
        title.setObjectName("SectionTitle")
        header.addWidget(title)
        header.addStretch()

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setObjectName("SecondaryButton")
        self.clear_btn.setFixedHeight(26)
        self.clear_btn.clicked.connect(self.clear_notifications)
        header.addWidget(self.clear_btn)
        layout.addLayout(header)

        self._content = QWidget(self)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(8)
        self._content_layout.addStretch()
        layout.addWidget(self._content)
        self.hide()

    def add_notification(self, notification: Notification) -> None:
        card = NotificationCard(notification, compact=True, parent=self._content)
        self._content_layout.insertWidget(0, card)
        self._items.insert(0, card)

        while len(self._items) > self._max_items:
            stale = self._items.pop()
            stale.deleteLater()

        self.show()

    def clear_notifications(self) -> None:
        for item in self._items:
            item.deleteLater()
        self._items.clear()
        self.hide()

    def item_count(self) -> int:
        return len(self._items)
