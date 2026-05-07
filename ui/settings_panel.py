"""
Production settings panel for Nova Assistant.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QKeySequenceEdit,
)

from core import settings, state
from core.audio import LiveAudioListener
from core.config_schema import (
    PERMISSION_LEVELS,
    PUSH_TO_TALK_MODES,
    SPEECH_LANGUAGES,
    THEMES,
    TTS_LANGUAGES,
    UI_LANGUAGES,
    VOICE_ENGINES,
)
from core.errors import SettingsError
from core.identity import IdentityManager
from core.logger import get_logger
from core.theme_manager import theme_manager
from core.tts import TextToSpeechEngine

logger = get_logger(__name__)


_LEVEL_DESCRIPTIONS: dict[str, str] = {
    "BASIC": (
        "Safe commands run automatically. Medium and dangerous actions "
        "require explicit confirmation before execution."
    ),
    "TRUSTED": (
        "Safe and medium-risk commands run automatically. Dangerous actions "
        "still require confirmation."
    ),
    "ADMIN_CONFIRM": (
        "Safe and medium-risk commands run automatically. Dangerous actions "
        "require explicit confirmation and are audit logged."
    ),
}

_LANGUAGE_LABELS = {
    "en": "English",
    "en-US": "English (United States)",
    "en-IN": "English (India)",
}


class PermissionSettingsPanel(QWidget):
    """Reusable settings panel for permission controls."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        level_group = QGroupBox("Permission Level")
        level_layout = QVBoxLayout(level_group)

        self.permission_level_combo = QComboBox()
        self.permission_level_combo.addItem("Basic - safe actions only", "BASIC")
        self.permission_level_combo.addItem("Trusted - safe and medium actions", "TRUSTED")
        self.permission_level_combo.addItem("Admin Confirm - confirmation plus audit", "ADMIN_CONFIRM")
        self.permission_level_combo.currentIndexChanged.connect(self._update_description)
        level_layout.addWidget(self.permission_level_combo)

        self.level_description = QLabel("")
        self.level_description.setWordWrap(True)
        self.level_description.setObjectName("MetaLabel")
        level_layout.addWidget(self.level_description)
        layout.addWidget(level_group)

        confirm_group = QGroupBox("Confirmation Behavior")
        confirm_layout = QVBoxLayout(confirm_group)
        self.allow_temporary_approvals_cb = QCheckBox("Allow temporary approvals")
        self.allow_temporary_approvals_cb.setToolTip(
            "Allows medium-risk actions to be approved for a short period. Dangerous actions still require confirmation."
        )
        confirm_layout.addWidget(self.allow_temporary_approvals_cb)

        self.confirmation_timeout_spin = QSpinBox()
        self.confirmation_timeout_spin.setRange(10, 600)
        self.confirmation_timeout_spin.setSingleStep(5)
        self.confirmation_timeout_spin.setToolTip("Seconds before a pending confirmation expires.")
        _add_labeled_widget(confirm_layout, "Confirmation timeout", self.confirmation_timeout_spin)
        layout.addWidget(confirm_group)

        audit_group = QGroupBox("Audit & Logging")
        audit_layout = QVBoxLayout(audit_group)
        self.audit_log_enabled_cb = QCheckBox("Write permission audit log")
        self.audit_log_enabled_cb.setToolTip("Writes permission decisions to logs/permission_audit.log.")
        audit_layout.addWidget(self.audit_log_enabled_cb)
        layout.addWidget(audit_group)

        layout.addStretch()
        self.load_from_settings()

    def load_from_settings(self) -> None:
        current_level = str(settings.get("permission_level") or "BASIC").upper()
        index = self.permission_level_combo.findData(current_level)
        if index >= 0:
            self.permission_level_combo.setCurrentIndex(index)
        self.allow_temporary_approvals_cb.setChecked(bool(settings.get("allow_temporary_approvals")))
        self.confirmation_timeout_spin.setValue(int(settings.get("confirmation_timeout_seconds") or 60))
        self.audit_log_enabled_cb.setChecked(bool(settings.get("audit_log_enabled")))
        self._update_description()

    def save_to_settings(self) -> None:
        settings.set("permission_level", str(self.permission_level_combo.currentData() or "BASIC"))
        settings.set("allow_temporary_approvals", self.allow_temporary_approvals_cb.isChecked())
        settings.set("confirmation_timeout_seconds", self.confirmation_timeout_spin.value())
        settings.set("audit_log_enabled", self.audit_log_enabled_cb.isChecked())

    def _update_description(self) -> None:
        level = str(self.permission_level_combo.currentData() or "BASIC")
        self.level_description.setText(_LEVEL_DESCRIPTIONS.get(level, "Select a permission level."))


class SettingsPanel(QDialog):
    """Desktop settings window with validation, persistence, and live apply."""

    setting_changed = Signal(str, object)

    def __init__(
        self,
        parent=None,
        *,
        settings_manager: settings.SettingsManager | None = None,
        tts_engine: TextToSpeechEngine | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings_manager or settings.settings_manager
        self.identity_mgr = IdentityManager()
        self.tts = tts_engine or getattr(parent, "tts", None) or TextToSpeechEngine()
        self.voice_map: dict[str, str] = {}
        self._loading = False

        self.setWindowTitle("Settings")
        self.setMinimumSize(740, 640)
        self.resize(820, 680)
        self.setAccessibleName("Assistant settings")

        self._build_ui()
        self.load_values()
        self.bind_events()
        theme_manager.apply(self)

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Settings")
        title.setObjectName("TitleLabel")
        subtitle = QLabel("Changes are validated and saved immediately.")
        subtitle.setObjectName("MetaLabel")
        title_block = QVBoxLayout()
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        header.addLayout(title_block)
        header.addStretch()
        main_layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        main_layout.addWidget(self.tabs, stretch=1)

        self._setup_general_tab()
        self._setup_voice_tab()
        self._setup_shortcuts_tab()
        self._setup_permissions_tab()
        self._setup_appearance_tab()
        self._setup_language_tab()
        self._setup_advanced_tab()

        self.status_label = QLabel("")
        self.status_label.setObjectName("SettingsStatus")
        self.status_label.setProperty("error", "false")
        self.status_label.setWordWrap(True)
        main_layout.addWidget(self.status_label)

        button_layout = QHBoxLayout()
        self.import_btn = QPushButton("Import")
        self.export_btn = QPushButton("Export")
        self.reset_btn = QPushButton("Reset Defaults")
        self.apply_btn = QPushButton("Apply All")
        self.close_btn = QPushButton("Close")
        self.import_btn.setObjectName("SecondaryButton")
        self.export_btn.setObjectName("SecondaryButton")
        self.reset_btn.setObjectName("SecondaryButton")
        self.close_btn.setObjectName("SecondaryButton")

        button_layout.addWidget(self.import_btn)
        button_layout.addWidget(self.export_btn)
        button_layout.addWidget(self.reset_btn)
        button_layout.addStretch()
        button_layout.addWidget(self.apply_btn)
        button_layout.addWidget(self.close_btn)
        main_layout.addLayout(button_layout)

    def _setup_general_tab(self) -> None:
        tab, layout = self._new_scroll_tab("General")
        group = QGroupBox("Identity")
        group_layout = QVBoxLayout(group)

        self.assistant_name_input = QLineEdit()
        self.assistant_name_input.setMaxLength(30)
        self.assistant_name_input.setAccessibleName("Assistant name")
        _add_labeled_widget(group_layout, "Assistant Name", self.assistant_name_input)

        self.user_name_input = QLineEdit()
        self.user_name_input.setMaxLength(60)
        self.user_name_input.setAccessibleName("User name")
        _add_labeled_widget(group_layout, "User Name", self.user_name_input)

        layout.addWidget(group)
        layout.addStretch()
        self.tabs.addTab(tab, "General")

    def _setup_voice_tab(self) -> None:
        tab, layout = self._new_scroll_tab("Voice")
        engine_group = QGroupBox("Engine")
        engine_layout = QVBoxLayout(engine_group)

        self.voice_enabled_cb = QCheckBox("Enable voice output")
        self.voice_enabled_cb.setAccessibleName("Enable voice output")
        engine_layout.addWidget(self.voice_enabled_cb)

        self.voice_engine_combo = QComboBox()
        for engine in VOICE_ENGINES:
            self.voice_engine_combo.addItem("Windows SAPI (pyttsx3)" if engine == "pyttsx3" else engine, engine)
        _add_labeled_widget(engine_layout, "Voice Engine", self.voice_engine_combo)

        self.voice_combo = QComboBox()
        self.voice_combo.setAccessibleName("Voice selection")
        self._load_voice_options()
        _add_labeled_widget(engine_layout, "Voice Selection", self.voice_combo)
        layout.addWidget(engine_group)

        playback_group = QGroupBox("Playback")
        playback_layout = QVBoxLayout(playback_group)

        self.speed_value_label = QLabel("")
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(50, 200)
        self.speed_slider.setSingleStep(5)
        _add_slider_row(playback_layout, "Speed", self.speed_slider, self.speed_value_label)

        self.volume_value_label = QLabel("")
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setSingleStep(5)
        _add_slider_row(playback_layout, "Volume", self.volume_slider, self.volume_value_label)

        self.mute_cb = QCheckBox("Mute voice output")
        self.mute_cb.setAccessibleName("Mute voice output")
        playback_layout.addWidget(self.mute_cb)

        test_row = QHBoxLayout()
        self.test_voice_btn = QPushButton("Test Voice")
        self.stop_voice_btn = QPushButton("Stop")
        self.stop_voice_btn.setObjectName("SecondaryButton")
        test_row.addWidget(self.test_voice_btn)
        test_row.addWidget(self.stop_voice_btn)
        test_row.addStretch()
        playback_layout.addLayout(test_row)
        layout.addWidget(playback_group)

        input_group = QGroupBox("Voice Input")
        input_layout = QVBoxLayout(input_group)
        self.microphone_device_combo = QComboBox()
        self.microphone_device_combo.setAccessibleName("Microphone device")
        self._load_microphone_options()
        _add_labeled_widget(input_layout, "Microphone", self.microphone_device_combo)

        self.sensitivity_value_label = QLabel("")
        self.sensitivity_slider = QSlider(Qt.Horizontal)
        self.sensitivity_slider.setRange(1, 100)
        self.sensitivity_slider.setSingleStep(1)
        _add_slider_row(input_layout, "Mic Sensitivity", self.sensitivity_slider, self.sensitivity_value_label)

        sensitivity_hint = QLabel(
            "Higher sensitivity picks up quieter speech. Lower sensitivity reduces background noise."
        )
        sensitivity_hint.setWordWrap(True)
        sensitivity_hint.setObjectName("MetaLabel")
        input_layout.addWidget(sensitivity_hint)
        layout.addWidget(input_group)
        layout.addStretch()
        self.tabs.addTab(tab, "Voice")

    def _setup_shortcuts_tab(self) -> None:
        tab, layout = self._new_scroll_tab("Shortcuts")
        group = QGroupBox("Global Hotkeys")
        group_layout = QVBoxLayout(group)

        self.push_to_talk_hotkey_edit = QKeySequenceEdit()
        self.push_to_talk_hotkey_edit.setAccessibleName("Push-to-talk hotkey")
        _add_labeled_widget(group_layout, "Push-to-talk hotkey", self.push_to_talk_hotkey_edit)

        self.start_stop_hotkey_edit = QKeySequenceEdit()
        self.start_stop_hotkey_edit.setAccessibleName("Start stop listening hotkey")
        _add_labeled_widget(group_layout, "Start/Stop listening hotkey", self.start_stop_hotkey_edit)

        self.open_assistant_hotkey_edit = QKeySequenceEdit()
        self.open_assistant_hotkey_edit.setAccessibleName("Open assistant hotkey")
        _add_labeled_widget(group_layout, "Open assistant hotkey", self.open_assistant_hotkey_edit)

        self.push_to_talk_mode_combo = QComboBox()
        for mode in PUSH_TO_TALK_MODES:
            self.push_to_talk_mode_combo.addItem("Hold to talk" if mode == "hold" else "Toggle listening", mode)
        _add_labeled_widget(group_layout, "Push-to-talk mode", self.push_to_talk_mode_combo)

        hint = QLabel("Hotkeys must include a modifier and cannot conflict with each other.")
        hint.setWordWrap(True)
        hint.setObjectName("MetaLabel")
        group_layout.addWidget(hint)
        layout.addWidget(group)
        layout.addStretch()
        self.tabs.addTab(tab, "Shortcuts")

    def _setup_permissions_tab(self) -> None:
        tab, layout = self._new_scroll_tab("Permissions")
        level_group = QGroupBox("Permission Level")
        level_layout = QVBoxLayout(level_group)
        self.permission_group = QButtonGroup(self)
        self.permission_buttons: dict[str, QRadioButton] = {}
        for level in PERMISSION_LEVELS:
            radio = QRadioButton(level.replace("_", " ").title())
            radio.setAccessibleName(f"Permission level {level}")
            self.permission_group.addButton(radio)
            self.permission_buttons[level] = radio
            level_layout.addWidget(radio)
            desc = QLabel(_LEVEL_DESCRIPTIONS[level])
            desc.setWordWrap(True)
            desc.setObjectName("MetaLabel")
            level_layout.addWidget(desc)
        layout.addWidget(level_group)

        behavior_group = QGroupBox("Confirmation Behavior")
        behavior_layout = QVBoxLayout(behavior_group)
        self.allow_temporary_approvals_cb = QCheckBox("Allow temporary approvals")
        behavior_layout.addWidget(self.allow_temporary_approvals_cb)
        self.confirmation_timeout_spin = QSpinBox()
        self.confirmation_timeout_spin.setRange(10, 600)
        self.confirmation_timeout_spin.setSingleStep(5)
        _add_labeled_widget(behavior_layout, "Confirmation timeout", self.confirmation_timeout_spin)
        self.audit_log_enabled_cb = QCheckBox("Write permission audit log")
        behavior_layout.addWidget(self.audit_log_enabled_cb)
        layout.addWidget(behavior_group)
        layout.addStretch()
        self.tabs.addTab(tab, "Permissions")

    def _setup_appearance_tab(self) -> None:
        tab, layout = self._new_scroll_tab("Appearance")
        group = QGroupBox("Window")
        group_layout = QVBoxLayout(group)

        self.theme_combo = QComboBox()
        for theme in THEMES:
            self.theme_combo.addItem(theme.title(), theme)
        _add_labeled_widget(group_layout, "Theme", self.theme_combo)

        self.use_system_theme_cb = QCheckBox("Use system theme")
        self.use_system_theme_cb.setAccessibleName("Use system theme")
        group_layout.addWidget(self.use_system_theme_cb)

        accent_row = QHBoxLayout()
        self.accent_color_input = QLineEdit()
        self.accent_color_input.setMaxLength(7)
        self.accent_color_btn = QPushButton("Choose")
        self.accent_color_btn.setObjectName("SecondaryButton")
        accent_row.addWidget(self.accent_color_input)
        accent_row.addWidget(self.accent_color_btn)
        _add_labeled_layout(group_layout, "Accent color", accent_row, self.accent_color_input)

        self.transparency_value_label = QLabel("")
        self.transparency_slider = QSlider(Qt.Horizontal)
        self.transparency_slider.setRange(75, 100)
        self.transparency_slider.setSingleStep(1)
        _add_slider_row(group_layout, "Window transparency", self.transparency_slider, self.transparency_value_label)

        layout.addWidget(group)

        launcher_group = QGroupBox("Launcher")
        launcher_layout = QVBoxLayout(launcher_group)
        self.show_floating_orb_cb = QCheckBox("Show floating orb")
        self.show_floating_orb_cb.setAccessibleName("Show floating orb")
        self.orb_always_on_top_cb = QCheckBox("Keep orb on top")
        self.orb_always_on_top_cb.setAccessibleName("Keep orb on top")
        self.sidebar_collapsed_cb = QCheckBox("Start with collapsed sidebar")
        self.sidebar_collapsed_cb.setAccessibleName("Start with collapsed sidebar")
        launcher_layout.addWidget(self.show_floating_orb_cb)
        launcher_layout.addWidget(self.orb_always_on_top_cb)
        launcher_layout.addWidget(self.sidebar_collapsed_cb)
        layout.addWidget(launcher_group)

        motion_group = QGroupBox("Motion")
        motion_layout = QVBoxLayout(motion_group)
        self.animations_enabled_cb = QCheckBox("Enable animations")
        self.animations_enabled_cb.setAccessibleName("Enable animations")
        self.reduced_motion_cb = QCheckBox("Reduced motion")
        self.reduced_motion_cb.setAccessibleName("Reduced motion")
        motion_layout.addWidget(self.animations_enabled_cb)
        motion_layout.addWidget(self.reduced_motion_cb)
        layout.addWidget(motion_group)

        layout.addStretch()
        self.tabs.addTab(tab, "Appearance")

    def _setup_language_tab(self) -> None:
        tab, layout = self._new_scroll_tab("Language")
        group = QGroupBox("Language")
        group_layout = QVBoxLayout(group)

        self.ui_language_combo = QComboBox()
        _populate_language_combo(self.ui_language_combo, UI_LANGUAGES)
        _add_labeled_widget(group_layout, "UI language", self.ui_language_combo)

        self.speech_language_combo = QComboBox()
        _populate_language_combo(self.speech_language_combo, SPEECH_LANGUAGES)
        _add_labeled_widget(group_layout, "Speech recognition language", self.speech_language_combo)

        self.tts_language_combo = QComboBox()
        _populate_language_combo(self.tts_language_combo, TTS_LANGUAGES)
        _add_labeled_widget(group_layout, "TTS language", self.tts_language_combo)

        restart_note = QLabel("UI language changes are saved now and take effect after restart.")
        restart_note.setWordWrap(True)
        restart_note.setObjectName("MetaLabel")
        group_layout.addWidget(restart_note)
        layout.addWidget(group)
        layout.addStretch()
        self.tabs.addTab(tab, "Language")

    def _setup_advanced_tab(self) -> None:
        tab, layout = self._new_scroll_tab("Advanced")
        group = QGroupBox("Runtime")
        group_layout = QVBoxLayout(group)
        self.logging_enabled_cb = QCheckBox("Enable file logging")
        self.analytics_enabled_cb = QCheckBox("Enable local analytics")
        self.plugins_enabled_cb = QCheckBox("Enable plugins")
        self.plugins_auto_load_cb = QCheckBox("Auto-load plugins at startup")
        self.llm_enabled_cb = QCheckBox("Enable local LLM pipeline")
        for widget in (
            self.logging_enabled_cb,
            self.analytics_enabled_cb,
            self.plugins_enabled_cb,
            self.plugins_auto_load_cb,
            self.llm_enabled_cb,
        ):
            group_layout.addWidget(widget)
        layout.addWidget(group)

        recovery_group = QGroupBox("Error Recovery")
        recovery_layout = QVBoxLayout(recovery_group)
        self.error_recovery_enabled_cb = QCheckBox("Enable guided error recovery")
        self.auto_retry_transient_errors_cb = QCheckBox("Auto-retry transient failures")
        self.remember_successful_fallbacks_cb = QCheckBox("Remember successful fallbacks")
        self.show_detailed_errors_in_debug_mode_cb = QCheckBox("Show detailed debug errors")
        self.max_auto_retries_spin = QSpinBox()
        self.max_auto_retries_spin.setRange(0, 3)
        self.max_auto_retries_spin.setSingleStep(1)
        for widget in (
            self.error_recovery_enabled_cb,
            self.auto_retry_transient_errors_cb,
            self.remember_successful_fallbacks_cb,
            self.show_detailed_errors_in_debug_mode_cb,
        ):
            recovery_layout.addWidget(widget)
        _add_labeled_widget(recovery_layout, "Maximum auto retries", self.max_auto_retries_spin)
        layout.addWidget(recovery_group)
        layout.addStretch()
        self.tabs.addTab(tab, "Advanced")

    def load_values(self) -> None:
        """Load current settings into controls."""
        self._loading = True
        try:
            self.assistant_name_input.setText(str(self.settings.get("assistant_name") or "Nova"))
            self.user_name_input.setText(str(self.settings.get("user_name") or "User"))

            self.voice_enabled_cb.setChecked(bool(self.settings.get("voice_enabled")))
            _set_combo_data(self.voice_engine_combo, self.settings.get("voice_engine"))
            _set_combo_data(self.voice_combo, self.settings.get("voice_id") or "")
            self.speed_slider.setValue(int(round(float(self.settings.get("voice_speed") or 1.0) * 100)))
            self.volume_slider.setValue(int(round(float(self.settings.get("voice_volume") or 1.0) * 100)))
            self.mute_cb.setChecked(bool(self.settings.get("mute")))
            self._load_microphone_options(selected=self.settings.get("microphone_device"))
            self.sensitivity_slider.setValue(_threshold_to_sensitivity(self.settings.get("silence_threshold") or 0.01))
            self._update_speed_label(self.speed_slider.value())
            self._update_volume_label(self.volume_slider.value())
            self._update_sensitivity_label(self.sensitivity_slider.value())

            _set_key_sequence(self.push_to_talk_hotkey_edit, self.settings.get("push_to_talk_hotkey"))
            _set_key_sequence(self.start_stop_hotkey_edit, self.settings.get("start_stop_listening_hotkey"))
            _set_key_sequence(self.open_assistant_hotkey_edit, self.settings.get("open_assistant_hotkey"))
            _set_combo_data(self.push_to_talk_mode_combo, self.settings.get("push_to_talk_mode"))

            permission = str(self.settings.get("permission_level") or "BASIC")
            self.permission_buttons.get(permission, self.permission_buttons["BASIC"]).setChecked(True)
            self.allow_temporary_approvals_cb.setChecked(bool(self.settings.get("allow_temporary_approvals")))
            self.confirmation_timeout_spin.setValue(int(self.settings.get("confirmation_timeout_seconds") or 60))
            self.audit_log_enabled_cb.setChecked(bool(self.settings.get("audit_log_enabled")))

            _set_combo_data(self.theme_combo, self.settings.get("theme"))
            self.use_system_theme_cb.setChecked(bool(self.settings.get("use_system_theme")))
            self.accent_color_input.setText(str(self.settings.get("accent_color") or "#0078d4"))
            self.transparency_slider.setValue(int(round(float(self.settings.get("window_transparency") or 1.0) * 100)))
            self._update_transparency_label(self.transparency_slider.value())
            self.show_floating_orb_cb.setChecked(bool(self.settings.get("show_floating_orb")))
            self.orb_always_on_top_cb.setChecked(bool(self.settings.get("orb_always_on_top")))
            self.animations_enabled_cb.setChecked(bool(self.settings.get("animations_enabled")))
            self.reduced_motion_cb.setChecked(bool(self.settings.get("reduced_motion")))
            self.sidebar_collapsed_cb.setChecked(bool(self.settings.get("sidebar_collapsed")))

            _set_combo_data(self.ui_language_combo, self.settings.get("ui_language"))
            _set_combo_data(self.speech_language_combo, self.settings.get("speech_language"))
            _set_combo_data(self.tts_language_combo, self.settings.get("tts_language"))

            self.logging_enabled_cb.setChecked(bool(self.settings.get("logging_enabled")))
            self.analytics_enabled_cb.setChecked(bool(self.settings.get("analytics_enabled")))
            self.plugins_enabled_cb.setChecked(bool(self.settings.get("plugins_enabled")))
            self.plugins_auto_load_cb.setChecked(bool(self.settings.get("plugins_auto_load")))
            self.llm_enabled_cb.setChecked(bool(self.settings.get("llm_enabled")))
            self.error_recovery_enabled_cb.setChecked(bool(self.settings.get("error_recovery_enabled")))
            self.auto_retry_transient_errors_cb.setChecked(bool(self.settings.get("auto_retry_transient_errors")))
            self.remember_successful_fallbacks_cb.setChecked(bool(self.settings.get("remember_successful_fallbacks")))
            self.show_detailed_errors_in_debug_mode_cb.setChecked(bool(self.settings.get("show_detailed_errors_in_debug_mode")))
            self.max_auto_retries_spin.setValue(int(self.settings.get("max_auto_retries") or 1))
            self._set_status("Settings loaded.", error=False)
        finally:
            self._loading = False

    def bind_events(self) -> None:
        """Bind control events to validation, save, and live apply."""
        self.assistant_name_input.editingFinished.connect(
            lambda: self.apply_change("assistant_name", self.assistant_name_input.text())
        )
        self.user_name_input.editingFinished.connect(
            lambda: self.apply_change("user_name", self.user_name_input.text())
        )

        self.voice_enabled_cb.toggled.connect(lambda value: self.apply_change("voice_enabled", value))
        self.voice_engine_combo.currentIndexChanged.connect(
            lambda _idx: self.apply_change("voice_engine", self.voice_engine_combo.currentData())
        )
        self.voice_combo.currentIndexChanged.connect(
            lambda _idx: self.apply_change("voice_id", self.voice_combo.currentData() or "")
        )
        self.microphone_device_combo.currentIndexChanged.connect(
            lambda _idx: self.apply_change("microphone_device", self.microphone_device_combo.currentData() or "default")
        )
        self.speed_slider.valueChanged.connect(self._update_speed_label)
        self.speed_slider.sliderReleased.connect(
            lambda: self.apply_change("voice_speed", self.speed_slider.value() / 100.0)
        )
        self.volume_slider.valueChanged.connect(self._update_volume_label)
        self.volume_slider.sliderReleased.connect(
            lambda: self.apply_change("voice_volume", self.volume_slider.value() / 100.0)
        )
        self.sensitivity_slider.valueChanged.connect(self._update_sensitivity_label)
        self.sensitivity_slider.sliderReleased.connect(
            lambda: self.apply_change("silence_threshold", _sensitivity_to_threshold(self.sensitivity_slider.value()))
        )
        self.mute_cb.toggled.connect(lambda value: self.apply_change("mute", value))
        self.test_voice_btn.clicked.connect(self.test_voice)
        self.stop_voice_btn.clicked.connect(self.tts.stop)

        self.push_to_talk_hotkey_edit.editingFinished.connect(
            lambda: self.apply_change("push_to_talk_hotkey", _key_sequence_text(self.push_to_talk_hotkey_edit))
        )
        self.start_stop_hotkey_edit.editingFinished.connect(
            lambda: self.apply_change("start_stop_listening_hotkey", _key_sequence_text(self.start_stop_hotkey_edit))
        )
        self.open_assistant_hotkey_edit.editingFinished.connect(
            lambda: self.apply_change("open_assistant_hotkey", _key_sequence_text(self.open_assistant_hotkey_edit))
        )
        self.push_to_talk_mode_combo.currentIndexChanged.connect(
            lambda _idx: self.apply_change("push_to_talk_mode", self.push_to_talk_mode_combo.currentData())
        )

        self.permission_group.buttonClicked.connect(self._permission_clicked)
        self.allow_temporary_approvals_cb.toggled.connect(
            lambda value: self.apply_change("allow_temporary_approvals", value)
        )
        self.confirmation_timeout_spin.valueChanged.connect(
            lambda value: self.apply_change("confirmation_timeout_seconds", value)
        )
        self.audit_log_enabled_cb.toggled.connect(lambda value: self.apply_change("audit_log_enabled", value))

        self.theme_combo.currentIndexChanged.connect(
            lambda _idx: self.apply_change("theme", self.theme_combo.currentData())
        )
        self.use_system_theme_cb.toggled.connect(lambda value: self.apply_change("use_system_theme", value))
        self.accent_color_input.editingFinished.connect(
            lambda: self.apply_change("accent_color", self.accent_color_input.text())
        )
        self.accent_color_btn.clicked.connect(self._choose_accent_color)
        self.transparency_slider.valueChanged.connect(self._update_transparency_label)
        self.transparency_slider.sliderReleased.connect(
            lambda: self.apply_change("window_transparency", self.transparency_slider.value() / 100.0)
        )
        self.show_floating_orb_cb.toggled.connect(lambda value: self.apply_change("show_floating_orb", value))
        self.orb_always_on_top_cb.toggled.connect(lambda value: self.apply_change("orb_always_on_top", value))
        self.animations_enabled_cb.toggled.connect(lambda value: self.apply_change("animations_enabled", value))
        self.reduced_motion_cb.toggled.connect(lambda value: self.apply_change("reduced_motion", value))
        self.sidebar_collapsed_cb.toggled.connect(lambda value: self.apply_change("sidebar_collapsed", value))

        self.ui_language_combo.currentIndexChanged.connect(
            lambda _idx: self.apply_change("ui_language", self.ui_language_combo.currentData())
        )
        self.speech_language_combo.currentIndexChanged.connect(
            lambda _idx: self.apply_change("speech_language", self.speech_language_combo.currentData())
        )
        self.tts_language_combo.currentIndexChanged.connect(
            lambda _idx: self.apply_change("tts_language", self.tts_language_combo.currentData())
        )

        self.logging_enabled_cb.toggled.connect(lambda value: self.apply_change("logging_enabled", value))
        self.analytics_enabled_cb.toggled.connect(lambda value: self.apply_change("analytics_enabled", value))
        self.plugins_enabled_cb.toggled.connect(lambda value: self.apply_change("plugins_enabled", value))
        self.plugins_auto_load_cb.toggled.connect(lambda value: self.apply_change("plugins_auto_load", value))
        self.llm_enabled_cb.toggled.connect(lambda value: self.apply_change("llm_enabled", value))
        self.error_recovery_enabled_cb.toggled.connect(lambda value: self.apply_change("error_recovery_enabled", value))
        self.auto_retry_transient_errors_cb.toggled.connect(lambda value: self.apply_change("auto_retry_transient_errors", value))
        self.remember_successful_fallbacks_cb.toggled.connect(lambda value: self.apply_change("remember_successful_fallbacks", value))
        self.show_detailed_errors_in_debug_mode_cb.toggled.connect(
            lambda value: self.apply_change("show_detailed_errors_in_debug_mode", value)
        )
        self.max_auto_retries_spin.valueChanged.connect(lambda value: self.apply_change("max_auto_retries", value))

        self.import_btn.clicked.connect(self.import_clicked)
        self.export_btn.clicked.connect(self.export_clicked)
        self.reset_btn.clicked.connect(lambda: self.reset_clicked())
        self.apply_btn.clicked.connect(self.save_all)
        self.close_btn.clicked.connect(self.accept)

    def apply_change(self, key: str, value: Any) -> bool:
        """Validate, persist, and live-apply one setting."""
        if self._loading:
            return True
        try:
            self.settings.set(key, value)
            normalized = self.settings.get(key)
            self._apply_live(key, normalized)
            self.setting_changed.emit(key, normalized)
            suffix = " Restart required." if self.settings.requires_restart(key) else ""
            self._set_status(f"Saved {key}.{suffix}", error=False)
            logger.info("Setting changed %s=%r", key, normalized)
            return True
        except SettingsError as exc:
            logger.warning("Invalid setting rejected: %s=%r", key, value)
            self._set_status(str(exc), error=True)
            self.load_values()
            return False

    def save_all(self) -> None:
        """Apply all controls. Useful after keyboard navigation or import review."""
        changes = [
            ("assistant_name", self.assistant_name_input.text()),
            ("user_name", self.user_name_input.text()),
            ("voice_enabled", self.voice_enabled_cb.isChecked()),
            ("voice_engine", self.voice_engine_combo.currentData()),
            ("voice_id", self.voice_combo.currentData() or ""),
            ("microphone_device", self.microphone_device_combo.currentData() or "default"),
            ("voice_speed", self.speed_slider.value() / 100.0),
            ("voice_volume", self.volume_slider.value() / 100.0),
            ("silence_threshold", _sensitivity_to_threshold(self.sensitivity_slider.value())),
            ("mute", self.mute_cb.isChecked()),
            ("push_to_talk_hotkey", _key_sequence_text(self.push_to_talk_hotkey_edit)),
            ("start_stop_listening_hotkey", _key_sequence_text(self.start_stop_hotkey_edit)),
            ("open_assistant_hotkey", _key_sequence_text(self.open_assistant_hotkey_edit)),
            ("push_to_talk_mode", self.push_to_talk_mode_combo.currentData()),
            ("allow_temporary_approvals", self.allow_temporary_approvals_cb.isChecked()),
            ("confirmation_timeout_seconds", self.confirmation_timeout_spin.value()),
            ("audit_log_enabled", self.audit_log_enabled_cb.isChecked()),
            ("theme", self.theme_combo.currentData()),
            ("use_system_theme", self.use_system_theme_cb.isChecked()),
            ("accent_color", self.accent_color_input.text()),
            ("window_transparency", self.transparency_slider.value() / 100.0),
            ("show_floating_orb", self.show_floating_orb_cb.isChecked()),
            ("orb_always_on_top", self.orb_always_on_top_cb.isChecked()),
            ("animations_enabled", self.animations_enabled_cb.isChecked()),
            ("reduced_motion", self.reduced_motion_cb.isChecked()),
            ("sidebar_collapsed", self.sidebar_collapsed_cb.isChecked()),
            ("ui_language", self.ui_language_combo.currentData()),
            ("speech_language", self.speech_language_combo.currentData()),
            ("tts_language", self.tts_language_combo.currentData()),
            ("logging_enabled", self.logging_enabled_cb.isChecked()),
            ("analytics_enabled", self.analytics_enabled_cb.isChecked()),
            ("plugins_enabled", self.plugins_enabled_cb.isChecked()),
            ("plugins_auto_load", self.plugins_auto_load_cb.isChecked()),
            ("llm_enabled", self.llm_enabled_cb.isChecked()),
            ("error_recovery_enabled", self.error_recovery_enabled_cb.isChecked()),
            ("auto_retry_transient_errors", self.auto_retry_transient_errors_cb.isChecked()),
            ("remember_successful_fallbacks", self.remember_successful_fallbacks_cb.isChecked()),
            ("show_detailed_errors_in_debug_mode", self.show_detailed_errors_in_debug_mode_cb.isChecked()),
            ("max_auto_retries", self.max_auto_retries_spin.value()),
        ]
        checked = self.permission_group.checkedButton()
        if checked is not None:
            for level, button in self.permission_buttons.items():
                if button is checked:
                    changes.append(("permission_level", level))
                    break
        for key, value in changes:
            if not self.apply_change(key, value):
                return
        self._set_status("All settings applied.", error=False)

    def reset_clicked(self, *, confirm: bool = True) -> None:
        """Reset to defaults and live-apply them."""
        if confirm:
            answer = QMessageBox.question(
                self,
                "Reset Settings",
                "Reset all settings to defaults?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.settings.reset_defaults()
        self.identity_mgr.load_identity()
        self.load_values()
        self._apply_all_live()
        self.setting_changed.emit("*", self.settings.values)
        self._set_status("Settings reset to defaults.", error=False)
        logger.info("Settings reset to defaults via UI")

    def import_clicked(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Import Settings",
            str(Path.home()),
            "JSON settings (*.json)",
        )
        if not path:
            return
        try:
            self.settings.import_file(path)
            self.identity_mgr.load_identity()
            self.load_values()
            self._apply_all_live()
            self.setting_changed.emit("*", self.settings.values)
            self._set_status(f"Imported settings from {path}.", error=False)
        except SettingsError as exc:
            self._set_status(str(exc), error=True)
            QMessageBox.warning(self, "Import Failed", str(exc))

    def export_clicked(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export Settings",
            str(Path.home() / "nova-settings.json"),
            "JSON settings (*.json)",
        )
        if not path:
            return
        try:
            exported = self.settings.export(path)
            self._set_status(f"Exported settings to {exported}.", error=False)
        except SettingsError as exc:
            self._set_status(str(exc), error=True)
            QMessageBox.warning(self, "Export Failed", str(exc))

    def test_voice(self) -> None:
        try:
            self.save_all()
            if hasattr(self.tts, "apply_settings"):
                self.tts.apply_settings()
            assistant_name = self.settings.get("assistant_name") or "Nova"
            user_name = self.settings.get("user_name") or "User"
            self.tts.speak(f"Hi {user_name}, I am {assistant_name}.")
            self._set_status("Test voice sent to the speech engine.", error=False)
        except Exception as exc:
            logger.warning("Test voice failed: %s", exc)
            self._set_status(f"Test voice failed: {exc}", error=True)

    def _apply_live(self, key: str, value: Any) -> None:
        parent = self.parent()
        if key in {"assistant_name", "user_name"}:
            self.identity_mgr.load_identity()
            if parent is not None and hasattr(parent, "update_identity_title"):
                parent.update_identity_title()
            return

        if key in {
            "theme",
            "use_system_theme",
            "accent_color",
            "window_transparency",
            "show_floating_orb",
            "orb_always_on_top",
            "animations_enabled",
            "reduced_motion",
            "sidebar_collapsed",
        }:
            theme_manager.apply(parent or self)
            if parent is not None and hasattr(parent, "apply_ui_preferences"):
                parent.apply_ui_preferences(changed_key=key)
            logger.info("Theme applied %s", self.settings.get("theme"))
            return

        if key in {"voice_enabled", "voice_id", "voice_speed", "voice_rate", "voice_volume", "mute"}:
            self._apply_voice_live(key, value)
            return

        if key == "permission_level":
            state.permission_level = str(value)
            logger.info("Permission level applied %s", value)
            return

        if key in {"speech_language", "sample_rate", "silence_threshold", "microphone_device"}:
            if parent is not None and hasattr(parent, "apply_speech_preferences"):
                parent.apply_speech_preferences(changed_key=key)
            logger.info("Speech input setting applied %s=%s", key, value)
            return

        if key in {"push_to_talk_hotkey", "start_stop_listening_hotkey", "open_assistant_hotkey", "push_to_talk_mode"}:
            if parent is not None and hasattr(parent, "refresh_hotkeys"):
                parent.refresh_hotkeys()
            logger.info("Hotkey re-registered %s=%s", key, value)

    def _apply_all_live(self) -> None:
        for key in (
            "assistant_name",
            "theme",
            "use_system_theme",
            "show_floating_orb",
            "orb_always_on_top",
            "animations_enabled",
            "reduced_motion",
            "sidebar_collapsed",
            "voice_enabled",
            "voice_id",
            "voice_speed",
            "voice_volume",
            "mute",
            "permission_level",
            "speech_language",
            "microphone_device",
            "silence_threshold",
            "push_to_talk_hotkey",
            "push_to_talk_mode",
            "start_stop_listening_hotkey",
            "open_assistant_hotkey",
        ):
            self._apply_live(key, self.settings.get(key))

    def _apply_voice_live(self, key: str, value: Any) -> None:
        try:
            if key == "voice_id" and value:
                self.tts.set_voice(str(value))
            elif key == "voice_enabled" and not bool(value):
                self.tts.stop()
            elif key in {"voice_speed", "voice_rate"}:
                self.tts.set_rate(int(self.settings.get("voice_rate") or 180))
            elif key == "voice_volume":
                self.tts.set_volume(float(value))
            elif key == "mute":
                self.tts.set_muted(bool(value))
        except Exception as exc:
            logger.warning("Voice setting could not be applied live: %s", exc)

    def _permission_clicked(self, button: QRadioButton) -> None:
        for level, candidate in self.permission_buttons.items():
            if candidate is button:
                self.apply_change("permission_level", level)
                return

    def _choose_accent_color(self) -> None:
        color = QColorDialog.getColor(parent=self)
        if color.isValid():
            self.accent_color_input.setText(color.name())
            self.apply_change("accent_color", color.name())

    def _load_voice_options(self) -> None:
        self.voice_combo.clear()
        self.voice_map.clear()
        self.voice_combo.addItem("System default", "")
        try:
            voices = self.tts.list_voices()
        except Exception as exc:
            logger.warning("Unable to list voices: %s", exc)
            voices = []
        current_voice_id = self.settings.get("voice_id") or ""
        found_current = not current_voice_id
        for index, voice in enumerate(voices):
            voice_id = str(getattr(voice, "id", "") or "")
            label = str(getattr(voice, "name", "") or f"Voice {index + 1}")
            if voice_id:
                self.voice_combo.addItem(label, voice_id)
                self.voice_map[label] = voice_id
                if voice_id == current_voice_id:
                    found_current = True
        if current_voice_id and not found_current:
            self.voice_combo.addItem("Unavailable saved voice", current_voice_id)

    def _load_microphone_options(self, selected: Any = None) -> None:
        self.microphone_device_combo.clear()
        self.microphone_device_combo.addItem("System default", "default")
        found_selected = selected in {None, "", "default"}
        try:
            devices = LiveAudioListener.available_input_devices()
        except Exception as exc:
            logger.warning("Unable to list microphone devices: %s", exc)
            devices = []
        for device in devices:
            label = str(device.get("name") or f"Input {device.get('index', '?')}")
            value = str(device.get("index"))
            self.microphone_device_combo.addItem(label, value)
            if str(selected) == value or str(selected) == label:
                found_selected = True
        if selected not in {None, "", "default"} and not found_selected:
            self.microphone_device_combo.addItem("Unavailable saved microphone", str(selected))
        _set_combo_data(self.microphone_device_combo, "default" if selected in {None, ""} else str(selected))

    def _new_scroll_tab(self, _name: str) -> tuple[QWidget, QVBoxLayout]:
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        return container, layout

    def _set_status(self, message: str, *, error: bool) -> None:
        self.status_label.setText(message)
        self.status_label.setProperty("error", "true" if error else "false")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _update_speed_label(self, value: int) -> None:
        self.speed_value_label.setText(f"{value}%")

    def _update_volume_label(self, value: int) -> None:
        self.volume_value_label.setText(f"{value}%")

    def _update_transparency_label(self, value: int) -> None:
        self.transparency_value_label.setText(f"{value}%")

    def _update_sensitivity_label(self, value: int) -> None:
        self.sensitivity_value_label.setText(f"{int(value)}%")


def _add_labeled_widget(layout: QVBoxLayout, label_text: str, widget: QWidget) -> None:
    row = QVBoxLayout()
    label = QLabel(label_text)
    label.setBuddy(widget)
    label.setAccessibleName(label_text)
    row.addWidget(label)
    row.addWidget(widget)
    layout.addLayout(row)


def _add_labeled_layout(layout: QVBoxLayout, label_text: str, child_layout: QHBoxLayout, buddy: QWidget) -> None:
    label = QLabel(label_text)
    label.setBuddy(buddy)
    layout.addWidget(label)
    layout.addLayout(child_layout)


def _add_slider_row(layout: QVBoxLayout, label_text: str, slider: QSlider, value_label: QLabel) -> None:
    label_row = QHBoxLayout()
    label = QLabel(label_text)
    label.setBuddy(slider)
    label_row.addWidget(label)
    label_row.addStretch()
    label_row.addWidget(value_label)
    layout.addLayout(label_row)
    layout.addWidget(slider)


def _populate_language_combo(combo: QComboBox, codes: tuple[str, ...]) -> None:
    for code in codes:
        combo.addItem(_LANGUAGE_LABELS.get(code, code), code)


def _set_combo_data(combo: QComboBox, value: Any) -> None:
    index = combo.findData(value)
    if index >= 0:
        combo.setCurrentIndex(index)


def _set_key_sequence(edit: QKeySequenceEdit, value: Any) -> None:
    edit.setKeySequence(QKeySequence(str(value or "")))


def _key_sequence_text(edit: QKeySequenceEdit) -> str:
    return edit.keySequence().toString(QKeySequence.PortableText)


def _threshold_to_sensitivity(value: Any) -> int:
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        threshold = 0.01
    threshold = max(0.001, min(0.1, threshold))
    ratio = (0.1 - threshold) / 0.099
    return int(round(1 + (ratio * 99)))


def _sensitivity_to_threshold(value: Any) -> float:
    try:
        sensitivity = int(value)
    except (TypeError, ValueError):
        sensitivity = 50
    sensitivity = max(1, min(100, sensitivity))
    ratio = (sensitivity - 1) / 99
    threshold = 0.1 - (ratio * 0.099)
    return round(max(0.001, min(0.1, threshold)), 4)
