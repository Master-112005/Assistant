"""
Design tokens and Qt stylesheet generation for Nova Assistant.
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


PRIMARY_COLOR = "#0078d4"
STATUS_READY = "#188038"
STATUS_LISTENING = "#0b57d0"
STATUS_HEARING = "#1a73e8"
STATUS_PROCESSING = "#b06000"
STATUS_EXECUTING = "#c26401"
STATUS_SPEAKING = "#0f9d58"
STATUS_ERROR = "#b3261e"

SUPPORTED_THEMES = ("dark", "light")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

_BASE_TOKENS: dict[str, Any] = {
    "font_family": "'Segoe UI', Arial, sans-serif",
    "font_size": 14,
    "font_size_small": 12,
    "font_size_title": 18,
    "radius_xs": 4,
    "radius_sm": 6,
    "radius_md": 8,
    "radius_lg": 10,
    "spacing_xs": 4,
    "spacing_sm": 8,
    "spacing_md": 12,
    "spacing_lg": 16,
    "spacing_xl": 24,
    "shadow_soft": "rgba(0, 0, 0, 0.18)",
    "duration_fast": 120,
    "duration_normal": 180,
    "duration_slow": 260,
    "sidebar_width": 184,
    "sidebar_collapsed_width": 68,
    "orb_size": 62,
}

_PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "background": "#171a1f",
        "surface": "#20242b",
        "surface_alt": "#262b33",
        "surface_hover": "#303742",
        "field": "#1c2027",
        "overlay": "#11141a",
        "sidebar": "#151920",
        "text": "#f4f7fb",
        "text_muted": "#aab4c2",
        "text_subtle": "#7d8796",
        "border": "#343c49",
        "border_strong": "#4a5566",
        "accent": PRIMARY_COLOR,
        "accent_hover": "#2b8bd6",
        "accent_soft": "#12324b",
        "accent_text": "#ffffff",
        "focus": "#73b7ff",
        "success": "#81c995",
        "warning": "#f4bf75",
        "error": "#f28b82",
        "disabled_bg": "#3b4350",
        "disabled_text": "#8d98a8",
        "scroll": "#596272",
        "scroll_hover": "#737f91",
        "chat_user": "#24384d",
        "chat_assistant": "#1d332e",
        "chat_system": "transparent",
        "notification_info": "#3a86ff",
        "notification_success": "#2a9d8f",
        "notification_warning": "#f4a261",
        "notification_error": "#e63946",
        "notification_reminder": "#ffd166",
    },
    "light": {
        "background": "#f4f7fb",
        "surface": "#ffffff",
        "surface_alt": "#eef3f8",
        "surface_hover": "#e5edf6",
        "field": "#ffffff",
        "overlay": "#ffffff",
        "sidebar": "#edf2f8",
        "text": "#1f2937",
        "text_muted": "#526173",
        "text_subtle": "#6b7788",
        "border": "#c9d3df",
        "border_strong": "#aeb9c7",
        "accent": PRIMARY_COLOR,
        "accent_hover": "#005ea8",
        "accent_soft": "#dceeff",
        "accent_text": "#ffffff",
        "focus": "#005ea8",
        "success": STATUS_READY,
        "warning": "#9a5b00",
        "error": STATUS_ERROR,
        "disabled_bg": "#dce2ea",
        "disabled_text": "#737f8f",
        "scroll": "#aeb8c4",
        "scroll_hover": "#8d98a5",
        "chat_user": "#dceeff",
        "chat_assistant": "#dcf4ee",
        "chat_system": "transparent",
        "notification_info": "#0b57d0",
        "notification_success": "#00796b",
        "notification_warning": "#b06000",
        "notification_error": "#b3261e",
        "notification_reminder": "#8a6500",
    },
}


def normalize_theme(theme: str | None) -> str:
    """Return a concrete theme name supported by the stylesheet builder."""
    name = str(theme or "dark").strip().lower()
    return name if name in SUPPORTED_THEMES else "dark"


def normalize_accent_color(accent_color: str | None) -> str:
    """Return a valid #RRGGBB accent color."""
    text = str(accent_color or PRIMARY_COLOR).strip()
    return text.lower() if _HEX_RE.match(text) else PRIMARY_COLOR


def theme_tokens(theme: str = "dark", accent_color: str = PRIMARY_COLOR) -> dict[str, Any]:
    """Return the merged token set for a concrete theme."""
    concrete_theme = normalize_theme(theme)
    tokens = deepcopy(_BASE_TOKENS)
    tokens.update(deepcopy(_PALETTES[concrete_theme]))
    accent = normalize_accent_color(accent_color)
    tokens["theme"] = concrete_theme
    tokens["accent"] = accent
    tokens["accent_hover"] = _accent_hover(accent, concrete_theme)
    tokens["accent_soft"] = _accent_soft(accent, concrete_theme)
    return tokens


def build_main_stylesheet(theme: str = "dark", accent_color: str = PRIMARY_COLOR) -> str:
    """Return the application Qt stylesheet for the requested theme."""
    t = theme_tokens(theme, accent_color)
    return f"""
QMainWindow, QDialog {{
    background-color: {t['background']};
    color: {t['text']};
}}
QWidget {{
    background-color: {t['background']};
    color: {t['text']};
    font-family: {t['font_family']};
    font-size: {t['font_size']}px;
}}
QWidget#AppRoot {{
    background-color: {t['background']};
}}
QFrame#ContentPanel, QFrame#TopBar, QFrame#PagePanel {{
    background-color: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: {t['radius_md']}px;
}}
QFrame#TopBar {{
    border-radius: {t['radius_sm']}px;
}}
QFrame#Divider {{
    background-color: {t['border']};
    border: none;
    min-height: 1px;
    max-height: 1px;
}}
QFrame#Sidebar {{
    background-color: {t['sidebar']};
    border-right: 1px solid {t['border']};
}}
QFrame#Sidebar QLabel {{
    background: transparent;
}}
QGroupBox {{
    background-color: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: {t['radius_sm']}px;
    margin-top: 10px;
    padding: 12px 8px 8px 8px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
}}
QPushButton {{
    background-color: {t['accent']};
    color: {t['accent_text']};
    border: 1px solid {t['accent']};
    border-radius: {t['radius_sm']}px;
    padding: 8px 14px;
    font-weight: 600;
    min-height: 18px;
}}
QPushButton:hover {{
    background-color: {t['accent_hover']};
    border-color: {t['accent_hover']};
}}
QPushButton:focus {{
    border: 1px solid {t['focus']};
}}
QPushButton:disabled {{
    background-color: {t['disabled_bg']};
    border-color: {t['disabled_bg']};
    color: {t['disabled_text']};
}}
QPushButton#SecondaryButton, QPushButton#SidebarToggle {{
    background-color: {t['surface_alt']};
    color: {t['text']};
    border: 1px solid {t['border']};
}}
QPushButton#SecondaryButton:hover, QPushButton#SidebarToggle:hover {{
    background-color: {t['surface_hover']};
}}
QPushButton#SidebarNavButton {{
    background-color: transparent;
    color: {t['text_muted']};
    border: 1px solid transparent;
    border-radius: {t['radius_sm']}px;
    padding: 9px 10px;
    text-align: left;
    font-weight: 600;
}}
QPushButton#SidebarNavButton:hover {{
    background-color: {t['surface_hover']};
    color: {t['text']};
}}
QPushButton#SidebarNavButton[active="true"] {{
    background-color: {t['accent_soft']};
    color: {t['text']};
    border-left: 3px solid {t['accent']};
}}
QPushButton#SidebarNavButton[collapsed="true"] {{
    text-align: center;
    padding-left: 4px;
    padding-right: 4px;
}}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QKeySequenceEdit {{
    background-color: {t['field']};
    color: {t['text']};
    border: 1px solid {t['border']};
    border-radius: {t['radius_sm']}px;
    padding: 7px;
    selection-background-color: {t['accent']};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QKeySequenceEdit:focus {{
    border: 1px solid {t['focus']};
}}
QTabWidget::pane {{
    background-color: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: {t['radius_sm']}px;
    top: -1px;
}}
QTabBar::tab {{
    background: {t['surface_alt']};
    border: 1px solid {t['border']};
    padding: 8px 12px;
    margin-right: 2px;
    border-top-left-radius: {t['radius_sm']}px;
    border-top-right-radius: {t['radius_sm']}px;
}}
QTabBar::tab:selected {{
    background: {t['field']};
    border-bottom-color: {t['field']};
}}
QCheckBox, QRadioButton {{
    spacing: 8px;
}}
QCheckBox:focus, QRadioButton:focus {{
    color: {t['focus']};
}}
QSlider::groove:horizontal {{
    height: 6px;
    background: {t['border']};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: {t['accent']};
    border: 1px solid {t['accent_hover']};
    width: 16px;
    margin: -5px 0;
    border-radius: 8px;
}}
QScrollArea {{
    border: none;
    background-color: transparent;
}}
QScrollArea > QWidget > QWidget {{
    background-color: transparent;
}}
QScrollBar:vertical {{
    border: none;
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {t['scroll']};
    min-height: 20px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{
    background: {t['scroll_hover']};
}}
QLabel#TitleLabel {{
    font-size: {t['font_size_title']}px;
    font-weight: 700;
}}
QLabel#PageTitle {{
    font-size: 16px;
    font-weight: 700;
}}
QLabel#SectionTitle {{
    color: {t['text']};
    font-weight: 700;
}}
QLabel#MetaLabel, QLabel#ContextAppLabel, QLabel#ExecProgressLabel {{
    color: {t['text_muted']};
    font-size: {t['font_size_small']}px;
}}
QLabel#MetricValue {{
    color: {t['text']};
    font-size: 20px;
    font-weight: 700;
}}
QLabel#MetricLabel {{
    color: {t['text_muted']};
    font-size: {t['font_size_small']}px;
}}
QFrame#ChatBubble {{
    border-radius: {t['radius_md']}px;
    border: 1px solid {t['border']};
}}
QFrame#ChatBubble[role="user"] {{
    background-color: {t['chat_user']};
}}
QFrame#ChatBubble[role="assistant"] {{
    background-color: {t['chat_assistant']};
}}
QFrame#ChatBubble[role="system"] {{
    background-color: {t['chat_system']};
    border-color: transparent;
}}
QLabel#ChatUser {{
    color: {t['accent']};
    font-weight: 700;
}}
QLabel#ChatAssistant {{
    color: {t['success']};
    font-weight: 700;
}}
QLabel#ChatSystem {{
    color: {t['text_muted']};
    font-style: italic;
}}
QFrame#NotificationCenter, QFrame#NotificationCard {{
    background-color: {t['overlay']};
    border: 1px solid {t['border']};
    border-radius: {t['radius_lg']}px;
}}
QFrame#NotificationCard[level="info"] {{
    border-color: {t['notification_info']};
}}
QFrame#NotificationCard[level="success"] {{
    border-color: {t['notification_success']};
}}
QFrame#NotificationCard[level="warning"] {{
    border-color: {t['notification_warning']};
}}
QFrame#NotificationCard[level="error"] {{
    border-color: {t['notification_error']};
}}
QFrame#NotificationCard[level="reminder"] {{
    border-color: {t['notification_reminder']};
}}
QLabel#SettingsStatus[error="true"] {{
    color: {t['error']};
}}
QLabel#SettingsStatus[error="false"] {{
    color: {t['success']};
}}
"""


def _accent_hover(accent: str, theme: str) -> str:
    return _shift_hex(accent, 0.16 if theme == "dark" else -0.15)


def _accent_soft(accent: str, theme: str) -> str:
    return _blend(accent, "#171a1f" if theme == "dark" else "#ffffff", 0.72 if theme == "dark" else 0.82)


def _shift_hex(hex_color: str, amount: float) -> str:
    channels = _hex_to_rgb(hex_color)
    shifted = []
    for value in channels:
        target = 255 if amount >= 0 else 0
        shifted.append(round(value + (target - value) * abs(amount)))
    return _rgb_to_hex(tuple(shifted))


def _blend(foreground: str, background: str, background_weight: float) -> str:
    fg = _hex_to_rgb(foreground)
    bg = _hex_to_rgb(background)
    weight = max(0.0, min(1.0, background_weight))
    blended = tuple(round(fg[i] * (1.0 - weight) + bg[i] * weight) for i in range(3))
    return _rgb_to_hex(blended)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = normalize_accent_color(value).lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{max(0, min(255, channel)):02x}" for channel in rgb)


MAIN_STYLESHEET = build_main_stylesheet("dark", PRIMARY_COLOR)
