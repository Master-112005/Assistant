"""
Reusable UI widgets.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ui.styles import (
    STATUS_ERROR,
    STATUS_EXECUTING,
    STATUS_HEARING,
    STATUS_LISTENING,
    STATUS_PROCESSING,
    STATUS_READY,
    STATUS_SPEAKING,
)


class StatusIndicator(QWidget):
    """Visual status indicator badge."""

    _LABELS = {
        "ready": "Ready",
        "idle": "Ready",
        "listening": "Listening...",
        "hearing_speech": "Hearing...",
        "understanding": "Understanding...",
        "processing": "Understanding...",
        "executing": "Executing...",
        "speaking": "Speaking...",
        "completed": "Done",
        "cancelled": "Cancelled",
        "error": "Error - Ready again",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(8)

        self.dot = QFrame()
        self.dot.setAccessibleName("Status dot")
        self.dot.setFixedSize(10, 10)
        self.dot.setStyleSheet(f"background-color: {STATUS_READY}; border-radius: 5px;")

        self.label = QLabel("Ready")
        self.label.setObjectName("MetaLabel")

        self.layout.addWidget(self.dot)
        self.layout.addWidget(self.label)
        self.layout.addStretch()

    def set_status(self, status: str) -> None:
        normalized = str(status or "ready").lower()
        self.label.setText(self._LABELS.get(normalized, normalized.replace("_", " ").title()))
        color = STATUS_READY
        if normalized == "listening":
            color = STATUS_LISTENING
        elif normalized == "hearing_speech":
            color = STATUS_HEARING
        elif normalized in {"understanding", "processing"}:
            color = STATUS_PROCESSING
        elif normalized == "executing":
            color = STATUS_EXECUTING
        elif normalized == "speaking":
            color = STATUS_SPEAKING
        elif normalized == "completed":
            color = STATUS_READY
        elif normalized == "cancelled":
            color = STATUS_PROCESSING
        elif normalized == "error":
            color = STATUS_ERROR
        self.dot.setStyleSheet(f"background-color: {color}; border-radius: 5px;")


class ChatBubble(QWidget):
    """A chat message widget."""

    def __init__(self, sender: str, message: str, parent=None):
        super().__init__(parent)
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 4, 0, 4)

        bubble = QFrame()
        bubble.setObjectName("ChatBubble")
        bubble.setProperty("role", _sender_role(sender))
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(10, 8, 10, 8)
        bubble_layout.setSpacing(4)

        sender_label = QLabel(sender)
        msg_label = QLabel(message)
        msg_label.setWordWrap(True)
        msg_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        msg_label.setMinimumWidth(120)
        msg_label.setMaximumWidth(560)

        bubble_layout.addWidget(sender_label)
        bubble_layout.addWidget(msg_label)

        role = _sender_role(sender)
        if role == "user":
            sender_label.setObjectName("ChatUser")
            main_layout.addStretch()
            main_layout.addWidget(bubble)
            return

        if role == "system":
            sender_label.setObjectName("ChatSystem")
        else:
            sender_label.setObjectName("ChatAssistant")
        main_layout.addWidget(bubble)
        main_layout.addStretch()


def _sender_role(sender: str) -> str:
    normalized = str(sender or "").strip().lower()
    if normalized == "user":
        return "user"
    if normalized == "system":
        return "system"
    return "assistant"
