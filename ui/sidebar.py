"""
Collapsible sidebar navigation for the desktop assistant shell.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from core import settings, state
from core.logger import get_logger
from core.theme_manager import theme_manager
from ui.animations import animations_enabled

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SidebarItem:
    page: str
    label: str
    short_label: str


SIDEBAR_ITEMS: tuple[SidebarItem, ...] = (
    SidebarItem("chat", "Chat", "C"),
    SidebarItem("skills", "Skills", "S"),
    SidebarItem("reminders", "Reminders", "R"),
    SidebarItem("history", "History", "H"),
    SidebarItem("plugins", "Plugins", "P"),
    SidebarItem("settings", "Settings", "G"),
)


class Sidebar(QFrame):
    """Structured, keyboard-accessible sidebar with animated collapse."""

    page_requested = Signal(str)
    collapsed_changed = Signal(bool)

    def __init__(self, parent=None, *, collapsed: bool | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setAccessibleName("Assistant navigation")
        self._tokens = theme_manager.theme_tokens()
        self._expanded_width = int(self._tokens.get("sidebar_width", 184))
        self._collapsed_width = int(self._tokens.get("sidebar_collapsed_width", 68))
        self._collapsed = bool(settings.get("sidebar_collapsed")) if collapsed is None else bool(collapsed)
        self._active_page = "chat"
        self._buttons: dict[str, QPushButton] = {}
        self._width_animation: QPropertyAnimation | None = None

        self._build_ui()
        self._apply_width(animated=False)
        self.set_active("chat")

    def set_active(self, page: str) -> None:
        """Mark the active page and emit no navigation signal."""
        normalized = self._normalize_page(page)
        self._active_page = normalized
        state.sidebar_page = normalized
        for item_page, button in self._buttons.items():
            button.setProperty("active", "true" if item_page == normalized else "false")
            self._repolish(button)
        logger.info("Sidebar page=%s", normalized)

    def toggle_collapsed(self) -> None:
        if self._collapsed:
            self.animate_open()
        else:
            self.animate_close()

    def animate_open(self) -> None:
        self._set_collapsed(False, animated=True)

    def animate_close(self) -> None:
        self._set_collapsed(True, animated=True)

    def is_collapsed(self) -> bool:
        return self._collapsed

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 14, 10, 14)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 4)
        self.title_label = QLabel("Nova")
        self.title_label.setObjectName("TitleLabel")
        self.title_label.setAccessibleName("Nova Assistant")
        header.addWidget(self.title_label)
        header.addStretch()

        self.toggle_button = QPushButton("<<")
        self.toggle_button.setObjectName("SidebarToggle")
        self.toggle_button.setFixedSize(34, 30)
        self.toggle_button.setToolTip("Collapse sidebar")
        self.toggle_button.clicked.connect(self.toggle_collapsed)
        header.addWidget(self.toggle_button)
        layout.addLayout(header)

        for item in SIDEBAR_ITEMS:
            button = QPushButton(item.label)
            button.setObjectName("SidebarNavButton")
            button.setAccessibleName(item.label)
            button.setToolTip(item.label)
            button.setProperty("active", "false")
            button.setProperty("collapsed", "false")
            button.clicked.connect(lambda _checked=False, page=item.page: self._request_page(page))
            self._buttons[item.page] = button
            layout.addWidget(button)

        layout.addStretch()
        self._sync_labels()

    def _request_page(self, page: str) -> None:
        normalized = self._normalize_page(page)
        self.set_active(normalized)
        self.page_requested.emit(normalized)

    def _set_collapsed(self, collapsed: bool, *, animated: bool) -> None:
        if self._collapsed == collapsed:
            return
        self._collapsed = collapsed
        self._sync_labels()
        self._apply_width(animated=animated)
        try:
            settings.set("sidebar_collapsed", collapsed)
        except Exception:
            logger.debug("Failed to persist sidebar collapsed setting", exc_info=True)
        self.collapsed_changed.emit(collapsed)
        logger.info("Sidebar collapsed=%s", collapsed)

    def _apply_width(self, *, animated: bool) -> None:
        target_width = self._collapsed_width if self._collapsed else self._expanded_width
        if self._width_animation is not None:
            self._width_animation.stop()
        if animated and animations_enabled():
            self._width_animation = QPropertyAnimation(self, b"minimumWidth", self)
            self._width_animation.setDuration(int(self._tokens.get("duration_normal", 180)))
            self._width_animation.setStartValue(self.width() or self.minimumWidth())
            self._width_animation.setEndValue(target_width)
            self._width_animation.setEasingCurve(QEasingCurve.OutCubic)
            self._width_animation.valueChanged.connect(lambda value: self.setMaximumWidth(int(value)))
            self._width_animation.finished.connect(lambda: self.setMaximumWidth(target_width))
            self._width_animation.start()
            logger.debug("Animation triggered sidebar_width target=%s", target_width)
            return
        self.setMinimumWidth(target_width)
        self.setMaximumWidth(target_width)

    def _sync_labels(self) -> None:
        self.title_label.setVisible(not self._collapsed)
        self.toggle_button.setText(">>" if self._collapsed else "<<")
        self.toggle_button.setToolTip("Expand sidebar" if self._collapsed else "Collapse sidebar")
        for item in SIDEBAR_ITEMS:
            button = self._buttons[item.page]
            button.setText(item.short_label if self._collapsed else item.label)
            button.setProperty("collapsed", "true" if self._collapsed else "false")
            self._repolish(button)

    @staticmethod
    def _normalize_page(page: str) -> str:
        valid_pages = {item.page for item in SIDEBAR_ITEMS}
        normalized = str(page or "chat").strip().lower()
        return normalized if normalized in valid_pages else "chat"

    @staticmethod
    def _repolish(widget) -> None:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()
