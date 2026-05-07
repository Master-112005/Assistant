"""
Always-available floating launcher orb.
"""
from __future__ import annotations

import math
from typing import Callable

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPen, QRadialGradient, QRegion
from PySide6.QtWidgets import QApplication, QWidget

from core import settings, state
from core.logger import get_logger
from core.theme_manager import theme_manager
from ui.animations import animations_enabled

logger = get_logger(__name__)


class FloatingOrb(QWidget):
    """Small draggable circular launcher that opens the assistant."""

    def __init__(
        self,
        parent=None,
        *,
        open_callback: Callable[[], None] | None = None,
        size: int | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("FloatingOrb")
        self.setAccessibleName("Open Nova Assistant")
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._open_callback = open_callback
        self._tokens = theme_manager.theme_tokens()
        self._size = int(size or self._tokens.get("orb_size", 62))
        self._hovered = False
        self._listening = False
        self._pulse_phase = 0.0
        self._drag_start_global: QPoint | None = None
        self._drag_start_pos: QPoint | None = None
        self._drag_moved = False

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(50)
        self._pulse_timer.timeout.connect(self._advance_pulse)

        self.setFixedSize(QSize(self._size, self._size))
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("Nova Launcher")
        self._configure_window_flags()
        self.setMask(QRegion(QRect(0, 0, self._size, self._size), QRegion.Ellipse))

    def show_orb(self) -> None:
        """Show the orb and place it on a usable screen edge if needed."""
        self._configure_window_flags()
        if self.pos().isNull():
            self._place_default()
        self.show()
        self.raise_()
        state.orb_visible = True
        logger.info("Orb shown")

    def hide_orb(self) -> None:
        self.hide()
        state.orb_visible = False
        logger.info("Orb hidden")

    def set_listening(self, state_value: bool) -> None:
        """Update listening indicator and optional pulse."""
        self._listening = bool(state_value)
        if self._listening and animations_enabled():
            self._pulse_timer.start()
        else:
            self._pulse_timer.stop()
            self._pulse_phase = 0.0
        self.update()

    def snap_to_edge(self) -> None:
        """Snap the orb to the nearest horizontal screen edge."""
        screen = self._current_screen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        current = self.frameGeometry()
        left_x = geometry.left() + 10
        right_x = geometry.right() - current.width() - 10
        target_x = left_x if abs(current.center().x() - geometry.left()) < abs(geometry.right() - current.center().x()) else right_x
        target_y = max(geometry.top() + 10, min(self.y(), geometry.bottom() - current.height() - 10))
        self.move(target_x, target_y)
        logger.info("Floating orb moved x=%s y=%s", target_x, target_y)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_start_global = event.globalPosition().toPoint()
            self._drag_start_pos = self.pos()
            self._drag_moved = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_start_global is None or self._drag_start_pos is None:
            super().mouseMoveEvent(event)
            return
        delta = event.globalPosition().toPoint() - self._drag_start_global
        if delta.manhattanLength() > 4:
            self._drag_moved = True
        self.move(self._drag_start_pos + delta)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._drag_start_global is not None:
            moved = self._drag_moved
            self._drag_start_global = None
            self._drag_start_pos = None
            self._drag_moved = False
            if moved:
                self.snap_to_edge()
            else:
                self._open_assistant()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        del event
        tokens = theme_manager.theme_tokens()
        accent = QColor(str(tokens.get("accent", "#0078d4")))
        surface = QColor(str(tokens.get("surface", "#20242b")))
        border = QColor(str(tokens.get("border_strong", "#4a5566")))
        text = QColor(str(tokens.get("accent_text", "#ffffff")))

        pulse = 0.0
        if self._listening and animations_enabled():
            pulse = (math.sin(self._pulse_phase) + 1.0) / 2.0

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        radius = min(self.width(), self.height()) / 2.0 - 4
        center = self.rect().center()

        glow_alpha = 70 if self._hovered else 34
        if self._listening:
            glow_alpha = 70 + int(55 * pulse)
        glow = QRadialGradient(center, radius + 12)
        glow_color = QColor(accent)
        glow_color.setAlpha(glow_alpha)
        glow.setColorAt(0.0, glow_color)
        transparent = QColor(accent)
        transparent.setAlpha(0)
        glow.setColorAt(1.0, transparent)
        painter.setBrush(glow)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(self.rect().adjusted(0, 0, -1, -1))

        body_rect = self.rect().adjusted(7, 7, -7, -7)
        body_gradient = QRadialGradient(body_rect.center(), body_rect.width() / 2)
        body_gradient.setColorAt(0.0, accent.lighter(126))
        body_gradient.setColorAt(0.72, accent)
        body_gradient.setColorAt(1.0, surface.darker(112))
        painter.setBrush(body_gradient)
        painter.setPen(QPen(border, 1.2))
        painter.drawEllipse(body_rect)

        painter.setPen(QPen(text, 2.0))
        mark = QPainterPath()
        mark.moveTo(body_rect.center().x() - 9, body_rect.center().y() + 6)
        mark.cubicTo(
            body_rect.center().x() - 3,
            body_rect.center().y() - 12,
            body_rect.center().x() + 12,
            body_rect.center().y() - 6,
            body_rect.center().x() + 4,
            body_rect.center().y() + 9,
        )
        painter.drawPath(mark)

        if self._listening:
            indicator = QRect(body_rect.right() - 12, body_rect.bottom() - 12, 10, 10)
            painter.setBrush(QColor(str(tokens.get("success", "#81c995"))))
            painter.setPen(QPen(surface, 2))
            painter.drawEllipse(indicator)

        painter.end()

    def _advance_pulse(self) -> None:
        self._pulse_phase = (self._pulse_phase + 0.22) % (math.pi * 2)
        self.update()

    def _open_assistant(self) -> None:
        if self._open_callback is None:
            return
        try:
            self._open_callback()
        except Exception as exc:
            logger.exception("Floating orb open callback failed", exc=exc)

    def _configure_window_flags(self) -> None:
        flags = Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus
        try:
            if bool(settings.get("orb_always_on_top")):
                flags |= Qt.WindowStaysOnTopHint
        except Exception:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)

    def _place_default(self) -> None:
        screen = self._current_screen() or QApplication.primaryScreen()
        if screen is None:
            self.move(40, 240)
            return
        geometry = screen.availableGeometry()
        self.move(geometry.right() - self.width() - 18, geometry.bottom() - self.height() - 92)

    def _current_screen(self):
        app = QApplication.instance()
        if app is None:
            return None
        try:
            return QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        except Exception:
            return QApplication.primaryScreen()
