"""
Live Qt theme application for the assistant UI.
"""
from __future__ import annotations

import logging
from typing import Any

from core import settings, state
from ui.styles import build_main_stylesheet, theme_tokens as build_theme_tokens

logger = logging.getLogger(__name__)


class ThemeManager:
    """Resolve, persist, and apply Qt themes across the app."""

    supported_themes = {"dark", "light", "system"}

    def load_theme(self) -> str:
        """Load the persisted theme preference and return the concrete theme."""
        resolved = self.resolve_theme()
        state.theme_loaded = resolved
        state.ui_mode = resolved
        logger.info("Theme loaded %s", resolved)
        return resolved

    def apply_theme(
        self,
        name: str | None = None,
        target: Any | None = None,
        *,
        persist: bool = True,
    ) -> str:
        """
        Apply a named theme and optionally persist the user preference.

        Invalid theme names are logged and safely fall back to dark mode.
        """
        requested = self._normalize_requested_theme(name or settings.get("theme") or "dark")
        if persist:
            self._persist_theme_choice(requested)
        return self.apply(target, theme=requested)

    def apply(
        self,
        target: Any | None = None,
        *,
        theme: str | None = None,
        accent_color: str | None = None,
        window_transparency: float | None = None,
    ) -> str:
        """Apply the resolved stylesheet to QApplication and an optional target."""
        resolved = self.resolve_theme(theme)
        accent = accent_color or settings.get("accent_color") or "#0078d4"
        stylesheet = build_main_stylesheet(resolved, str(accent))

        app = self._qt_application()
        if app is not None:
            app.setStyleSheet(stylesheet)
            try:
                app.setProperty("nova_theme", resolved)
            except Exception:
                logger.debug("Failed to store QApplication theme property", exc_info=True)

        if target is not None and hasattr(target, "setStyleSheet"):
            target.setStyleSheet(stylesheet)
        if target is not None and hasattr(target, "setWindowOpacity"):
            opacity = window_transparency
            if opacity is None:
                opacity = settings.get("window_transparency") or 1.0
            try:
                target.setWindowOpacity(float(opacity))
            except Exception:
                logger.debug("Failed to apply window transparency", exc_info=True)

        state.theme_loaded = resolved
        state.ui_mode = resolved
        logger.info("Theme applied %s", resolved)
        return resolved

    def toggle_theme(self, target: Any | None = None) -> str:
        """Toggle between concrete dark and light themes."""
        current = self.resolve_theme()
        next_theme = "light" if current == "dark" else "dark"
        self._persist_theme_choice(next_theme)
        return self.apply(target, theme=next_theme)

    def resolve_theme(self, requested: str | None = None) -> str:
        theme = self._normalize_requested_theme(requested or settings.get("theme") or "dark")
        try:
            use_system = bool(settings.get("use_system_theme"))
        except Exception:
            use_system = False
        if theme == "system" or use_system:
            return self.system_theme()
        return theme

    def system_theme(self) -> str:
        """Return the current Windows app theme, falling back to dark."""
        return self._system_theme()

    def theme_tokens(self, name: str | None = None) -> dict[str, Any]:
        """Return the active token set for custom components."""
        resolved = self.resolve_theme(name)
        return build_theme_tokens(resolved, str(settings.get("accent_color") or "#0078d4"))

    def _persist_theme_choice(self, requested: str) -> None:
        try:
            if settings.get("theme") != requested:
                settings.set("theme", requested)
            use_system = requested == "system"
            if bool(settings.get("use_system_theme")) != use_system:
                settings.set("use_system_theme", use_system)
        except Exception:
            logger.debug("Failed to persist theme choice", exc_info=True)

    def _normalize_requested_theme(self, requested: str | None) -> str:
        theme = str(requested or "dark").strip().lower()
        if theme in self.supported_themes:
            return theme
        logger.warning("Invalid theme requested %r; falling back to dark", requested)
        return "dark"

    @staticmethod
    def _qt_application() -> Any | None:
        try:
            from PySide6.QtWidgets import QApplication

            return QApplication.instance()
        except Exception:
            return None

    @staticmethod
    def _system_theme() -> str:
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if int(value) else "dark"
        except Exception:
            return "dark"


theme_manager = ThemeManager()
