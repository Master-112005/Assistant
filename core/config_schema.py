"""
Schema and validation rules for persistent assistant settings.

The project has accumulated settings over many phases.  This module keeps the
Phase 37 user-facing configuration explicit while preserving older keys that
other modules still read.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import re
from typing import Any

from core.constants import DEFAULT_THEME, SUPPORTED_LANGUAGES


PERMISSION_LEVELS = ("BASIC", "TRUSTED", "ADMIN_CONFIRM")
THEMES = ("dark", "light", "system")
VOICE_ENGINES = ("pyttsx3",)
UI_LANGUAGES = ("en",)
SPEECH_LANGUAGES = ("en", "en-US", "en-IN")
TTS_LANGUAGES = ("en", "en-US", "en-IN")
PUSH_TO_TALK_MODES = ("hold", "toggle")

HOTKEY_KEYS = (
    "push_to_talk_hotkey",
    "start_stop_listening_hotkey",
    "open_assistant_hotkey",
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "assistant_name": "Nova",
    "user_name": "User",
    "theme": DEFAULT_THEME,
    "use_system_theme": False,
    "accent_color": "#0078d4",
    "window_transparency": 1.0,
    "show_floating_orb": True,
    "orb_always_on_top": True,
    "animations_enabled": True,
    "reduced_motion": False,
    "sidebar_collapsed": False,
    "language": SUPPORTED_LANGUAGES[0],
    "ui_language": "en",
    "speech_language": "en-IN",
    "tts_language": "en",
    "voice_enabled": True,
    "voice_engine": "pyttsx3",
    "tts_engine": "pyttsx3",
    "voice_id": "",
    "voice_speed": 1.0,
    "voice_rate": 180,
    "voice_volume": 1.0,
    "mute": False,
    "hotkey": "Ctrl+Space",
    "push_to_talk_hotkey": "Ctrl+Space",
    "start_stop_listening_hotkey": "Ctrl+Alt+Shift+Space",
    "open_assistant_hotkey": "Ctrl+Alt+Space",
    "permission_level": "BASIC",
    "allow_temporary_approvals": True,
    "confirmation_timeout_seconds": 60,
    "audit_log_enabled": True,
    "sample_rate": 16000,
    "push_to_talk_mode": "hold",
    "microphone_device": "default",
    "stt_model": "base.en",
    "stt_timeout_seconds": 10,
    "auto_transcribe_after_recording": True,
    "silence_threshold": 0.01,
    "llm_enabled": True,
    "llm_provider": "ollama",
    "llm_model": "llama3",
    "llm_host": "http://localhost:11434",
    "llm_timeout": 30,
    "llm_temperature": 0.0,
    "stt_correction_enabled": True,
    "correction_confidence_threshold": 0.75,
    "show_original_transcript": False,
    "use_llm_correction": True,
    "planner_enabled": True,
    "planner_mode": "hybrid",
    "planner_use_llm": True,
    "planner_show_debug": False,
    "planner_confidence_threshold": 0.3,
    "stop_on_error": True,
    "execution_timeout": 45,
    "command_timeout_seconds": 35,
    "app_launch_timeout_seconds": 30,
    "close_app_timeout_seconds": 25,
    "window_action_timeout_seconds": 15,
    "browser_command_timeout_seconds": 25,
    "ocr_timeout_seconds": 15,
    "show_step_progress": True,
    "confirm_dangerous_actions": True,
    "preferred_browser": "chrome",
    "typing_delay_ms": 20,
    "automation_action_delay_ms": 25,  # Reduced from 150ms for better performance
    "browser_focus_timeout": 5,
    "safe_mode_clicks": True,
    "auto_launch_chrome_if_needed": True,
    "system_controls_enabled": True,
    "confirm_shutdown": True,
    "confirm_restart": True,
    "confirm_lock": False,
    "default_volume_step": 10,
    "default_brightness_step": 10,
    "chrome_skill_enabled": True,
    "read_results_mode": "best_available",
    "youtube_skill_enabled": True,
    "auto_open_youtube_if_needed": True,
    "youtube_search_wait_ms": 1500,
    "youtube_prefer_keyboard_shortcuts": True,
    "whatsapp_skill_enabled": True,
    "auto_open_whatsapp_if_needed": True,
    "whatsapp_skill_timeout_seconds": 120,
    "whatsapp_no_confirmation": True,
    "confirm_before_sending_message": False,
    "read_private_content_mode": "names_only",
    "music_skill_enabled": True,
    "preferred_music_app": "spotify",
    "auto_open_music_app_if_needed": True,
    "media_key_control_enabled": True,
    "ocr_enabled": True,
    "ocr_engine": "easyocr",
    "ocr_capture_mode": "active_window",
    "ocr_preprocess": True,
    "ocr_min_confidence": 0.40,
    "save_debug_screenshots": False,
    "screen_awareness_enabled": True,
    "awareness_use_ocr": True,
    "awareness_max_items": 5,
    "speak_awareness_summary": False,
    "ignore_background_windows": True,
    "click_text_enabled": True,
    "click_text_min_confidence": 0.55,
    "click_text_verify": True,
    "click_text_fuzzy_match": True,
    "highlight_target_before_click": False,
    "context_detection_enabled": True,
    "context_poll_interval_ms": 500,
    "show_current_app": True,
    "context_engine_enabled": True,
    "use_recent_history_for_context": True,
    "context_confidence_threshold": 0.70,
    "show_context_debug": False,
    "clipboard_enabled": True,
    "clipboard_watch_interval_ms": 500,
    "clipboard_history_limit": 100,
    "clipboard_store_sensitive": False,
    "clipboard_mask_sensitive_preview": True,
    "reminders_enabled": True,
    "reminder_check_interval_seconds": 15,
    "speak_reminders": True,
    "notifications_enabled": True,
    "desktop_notifications": True,
    "in_app_notifications": True,
    "voice_notifications": True,
    "notification_rate_limit_per_min": 10,
    "notification_duration_seconds": 5,
    "notification_queue_max_size": 128,
    "notification_history_limit": 50,
    "notification_dedupe_window_seconds": 10,
    "default_timezone": "local",
    "file_operations_enabled": True,
    "safe_delete_default": True,
    "confirm_delete": True,
    "confirm_overwrite": True,
    "search_common_folders": True,
    "smart_file_search_enabled": True,
    "use_file_index": True,
    "search_default_limit": 5,
    "search_default_scope": "user_folders",
    "index_update_on_startup": True,
    "file_create_missing_parents": False,
    "file_search_max_depth": 4,
    "file_search_max_results": 10,
    "file_bulk_delete_confirmation_threshold": 25,
    "confirm_outside_user_scope": True,
    "file_recent_history_limit": 10,
    "safety_guard_enabled": True,
    "large_delete_threshold_files": 20,
    "large_delete_threshold_mb": 100,
    "warn_on_common_folders": True,
    "prefer_recycle_bin": True,
    "memory_enabled": True,
    "history_retention_days": 90,
    "auto_learn_preferences": True,
    "store_interaction_history": True,
    "memory_backup_on_exit": True,
    "workflow_memory_enabled": True,
    "auto_capture_successful_workflows": True,
    "max_saved_workflows": 500,
    "allow_safe_auto_replay": True,
    "require_confirmation_for_risky_replay": True,
    "personalization_enabled": True,
    "preference_decay_enabled": True,
    "explicit_preferences_override_inferred": True,
    "allow_personalized_defaults": True,
    "conversation_memory_enabled": True,
    "conversation_max_turns": 10,
    "conversation_expiry_minutes": 30,
    "clarify_low_confidence_references": True,
    "logging_enabled": True,
    "log_level": "INFO",
    "log_rotation_mb": 10,
    "log_retention_days": 14,
    "analytics_enabled": True,
    "performance_metrics_enabled": True,
    "log_redact_message_content": False,
    "analytics_record_raw_input": True,
    "analytics_redact_command_text": False,
    "error_recovery_enabled": True,
    "auto_retry_transient_errors": True,
    "max_auto_retries": 1,
    "remember_successful_fallbacks": True,
    "show_detailed_errors_in_debug_mode": False,
    "plugins_enabled": True,
    "plugins_auto_load": True,
    "plugins_directory": "plugins",
    "disable_unhealthy_plugins": True,
    "require_plugin_permission_approval": True,
}


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    default: Any
    section: str
    label: str
    description: str = ""
    choices: tuple[Any, ...] | None = None
    min_value: float | None = None
    max_value: float | None = None
    restart_required: bool = False


SETTING_SPECS: dict[str, SettingSpec] = {
    "assistant_name": SettingSpec("assistant_name", "Nova", "General", "Assistant Name"),
    "user_name": SettingSpec("user_name", "User", "General", "User Name"),
    "voice_engine": SettingSpec(
        "voice_engine",
        "pyttsx3",
        "Voice",
        "Voice Engine",
        "Installed offline speech engine.",
        choices=VOICE_ENGINES,
    ),
    "voice_id": SettingSpec("voice_id", "", "Voice", "Voice Selection"),
    "voice_speed": SettingSpec("voice_speed", 1.0, "Voice", "Speed", min_value=0.5, max_value=2.0),
    "voice_rate": SettingSpec("voice_rate", 180, "Voice", "Speech Rate", min_value=80, max_value=320),
    "voice_volume": SettingSpec("voice_volume", 1.0, "Voice", "Volume", min_value=0.0, max_value=1.0),
    "mute": SettingSpec("mute", False, "Voice", "Mute"),
    "voice_enabled": SettingSpec("voice_enabled", True, "Voice", "Enable Voice Output"),
    "push_to_talk_hotkey": SettingSpec("push_to_talk_hotkey", "Ctrl+Space", "Shortcuts", "Push-to-talk"),
    "push_to_talk_mode": SettingSpec(
        "push_to_talk_mode",
        "hold",
        "Shortcuts",
        "Push-to-talk Mode",
        choices=PUSH_TO_TALK_MODES,
    ),
    "start_stop_listening_hotkey": SettingSpec(
        "start_stop_listening_hotkey",
        "Ctrl+Alt+Shift+Space",
        "Shortcuts",
        "Start/Stop Listening",
    ),
    "open_assistant_hotkey": SettingSpec(
        "open_assistant_hotkey",
        "Ctrl+Alt+Space",
        "Shortcuts",
        "Open Assistant",
    ),
    "permission_level": SettingSpec(
        "permission_level",
        "BASIC",
        "Permissions",
        "Permission Level",
        choices=PERMISSION_LEVELS,
    ),
    "theme": SettingSpec("theme", "dark", "Appearance", "Theme", choices=THEMES),
    "use_system_theme": SettingSpec("use_system_theme", False, "Appearance", "Use System Theme"),
    "accent_color": SettingSpec("accent_color", "#0078d4", "Appearance", "Accent Color"),
    "window_transparency": SettingSpec(
        "window_transparency",
        1.0,
        "Appearance",
        "Window Transparency",
        min_value=0.75,
        max_value=1.0,
    ),
    "show_floating_orb": SettingSpec("show_floating_orb", True, "Appearance", "Show Floating Orb"),
    "orb_always_on_top": SettingSpec("orb_always_on_top", True, "Appearance", "Orb Always On Top"),
    "animations_enabled": SettingSpec("animations_enabled", True, "Appearance", "Animations Enabled"),
    "reduced_motion": SettingSpec("reduced_motion", False, "Appearance", "Reduced Motion"),
    "sidebar_collapsed": SettingSpec("sidebar_collapsed", False, "Appearance", "Sidebar Collapsed"),
    "ui_language": SettingSpec(
        "ui_language",
        "en",
        "Language",
        "UI Language",
        choices=UI_LANGUAGES,
        restart_required=True,
    ),
    "speech_language": SettingSpec(
        "speech_language",
        "en-IN",
        "Language",
        "Speech Recognition Language",
        choices=SPEECH_LANGUAGES,
    ),
    "microphone_device": SettingSpec("microphone_device", "default", "Voice", "Microphone Device"),
    "sample_rate": SettingSpec("sample_rate", 16000, "Voice", "Sample Rate", min_value=8000, max_value=48000),
    "silence_threshold": SettingSpec(
        "silence_threshold",
        0.01,
        "Voice",
        "Silence Threshold",
        min_value=0.001,
        max_value=0.1,
    ),
    "tts_language": SettingSpec(
        "tts_language",
        "en",
        "Language",
        "TTS Language",
        choices=TTS_LANGUAGES,
    ),
    "error_recovery_enabled": SettingSpec(
        "error_recovery_enabled",
        True,
        "Advanced",
        "Enable Error Recovery",
        "Guided fallback options when commands fail.",
    ),
    "auto_retry_transient_errors": SettingSpec(
        "auto_retry_transient_errors",
        True,
        "Advanced",
        "Auto Retry Transient Errors",
    ),
    "max_auto_retries": SettingSpec(
        "max_auto_retries",
        1,
        "Advanced",
        "Maximum Auto Retries",
        min_value=0,
        max_value=3,
    ),
    "remember_successful_fallbacks": SettingSpec(
        "remember_successful_fallbacks",
        True,
        "Advanced",
        "Remember Successful Fallbacks",
    ),
    "show_detailed_errors_in_debug_mode": SettingSpec(
        "show_detailed_errors_in_debug_mode",
        False,
        "Advanced",
        "Show Detailed Errors In Debug Mode",
    ),
}


_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}$")
_MODIFIER_ALIASES = {
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "win": "Win",
    "windows": "Win",
    "meta": "Win",
    "cmd": "Win",
    "command": "Win",
}
_MODIFIER_ORDER = ("Ctrl", "Alt", "Shift", "Win")
_KEY_ALIASES = {
    "esc": "Escape",
    "escape": "Escape",
    "space": "Space",
    "spacebar": "Space",
    "enter": "Enter",
    "return": "Enter",
    "tab": "Tab",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "ins": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "pgup": "PageUp",
    "pagedown": "PageDown",
    "pgdn": "PageDown",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
}
_NAMED_KEYS = {
    "Space",
    "Enter",
    "Escape",
    "Tab",
    "Backspace",
    "Delete",
    "Insert",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "Up",
    "Down",
    "Left",
    "Right",
}


class SettingValidationError(ValueError):
    """Raised when a setting value cannot be accepted."""


def defaults_copy() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_SETTINGS)


def normalize_permission_level(value: Any) -> str:
    normalized = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "BASIC": "BASIC",
        "TRUSTED": "TRUSTED",
        "ADMIN": "ADMIN_CONFIRM",
        "ADMIN_CONFIRM": "ADMIN_CONFIRM",
    }
    if normalized not in aliases:
        raise SettingValidationError(f"Unknown permission level: {value!r}")
    return aliases[normalized]


def normalize_language_code(value: Any) -> str:
    text = str(value or "").strip()
    if not text or not _LANGUAGE_RE.match(text):
        raise SettingValidationError(f"Invalid language code: {value!r}")
    parts = text.replace("_", "-").split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        normalized.append(part.upper() if len(part) == 2 else part.title())
    return "-".join(normalized)


def normalize_hotkey(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise SettingValidationError("Hotkey cannot be empty.")

    raw_parts = [part.strip() for part in re.split(r"\s*\+\s*", text) if part.strip()]
    if len(raw_parts) < 2:
        raise SettingValidationError("Hotkey must include at least one modifier and one key.")

    modifiers: list[str] = []
    main_key = ""
    for raw_part in raw_parts:
        lowered = raw_part.lower()
        modifier = _MODIFIER_ALIASES.get(lowered)
        if modifier:
            if modifier in modifiers:
                raise SettingValidationError(f"Duplicate hotkey modifier: {modifier}")
            modifiers.append(modifier)
            continue
        if main_key:
            raise SettingValidationError("Hotkey can only have one non-modifier key.")
        main_key = _normalize_hotkey_key(raw_part)

    if not modifiers or not main_key:
        raise SettingValidationError("Hotkey must include at least one modifier and one key.")

    ordered = [modifier for modifier in _MODIFIER_ORDER if modifier in modifiers]
    return "+".join([*ordered, main_key])


def _normalize_hotkey_key(value: str) -> str:
    compact = value.strip()
    lowered = compact.lower()
    alias = _KEY_ALIASES.get(lowered)
    if alias:
        return alias
    if re.fullmatch(r"f(?:[1-9]|1[0-9]|2[0-4])", lowered):
        return lowered.upper()
    if re.fullmatch(r"[a-z]", lowered):
        return lowered.upper()
    if re.fullmatch(r"[0-9]", lowered):
        return lowered
    if compact in {"`", "-", "=", "[", "]", "\\", ";", "'", ",", ".", "/"}:
        return compact
    raise SettingValidationError(f"Unsupported hotkey key: {value!r}")


def validate_setting(key: str, value: Any, current: dict[str, Any] | None = None) -> Any:
    if key == "hotkey":
        key = "push_to_talk_hotkey"
    if key == "tts_engine":
        key = "voice_engine"
    if key == "language":
        key = "ui_language"

    if key in {"assistant_name", "user_name"}:
        return _validate_name(key, value)
    if key in HOTKEY_KEYS:
        normalized = normalize_hotkey(value)
        if current is not None:
            _validate_hotkey_conflict(key, normalized, current)
        return normalized
    if key == "push_to_talk_mode":
        return _validate_choice(key, str(value or "").strip().lower(), PUSH_TO_TALK_MODES)
    if key == "permission_level":
        return normalize_permission_level(value)
    if key == "theme":
        return _validate_choice(key, value, THEMES)
    if key == "voice_engine":
        return _validate_choice(key, value, VOICE_ENGINES)
    if key == "voice_id":
        text = str(value or "").strip()
        if len(text) > 260:
            raise SettingValidationError("Voice identifier is too long.")
        return text
    if key == "voice_speed":
        return _validate_float(key, value, 0.5, 2.0)
    if key == "voice_rate":
        return _validate_int(key, value, 80, 320)
    if key == "voice_volume":
        return _validate_float(key, value, 0.0, 1.0)
    if key == "window_transparency":
        return _validate_float(key, value, 0.75, 1.0)
    if key == "accent_color":
        text = str(value or "").strip()
        if not _HEX_COLOR_RE.match(text):
            raise SettingValidationError("Accent color must be a #RRGGBB hex value.")
        return text.lower()
    if key == "ui_language":
        normalized = normalize_language_code(value)
        return _validate_choice(key, normalized, UI_LANGUAGES)
    if key == "speech_language":
        normalized = normalize_language_code(value)
        return _validate_choice(key, normalized, SPEECH_LANGUAGES)
    if key == "microphone_device":
        return _validate_microphone_device(value)
    if key == "sample_rate":
        return _validate_int(key, value, 8000, 48000)
    if key == "silence_threshold":
        return _validate_float(key, value, 0.001, 0.1)
    if key == "tts_language":
        normalized = normalize_language_code(value)
        return _validate_choice(key, normalized, TTS_LANGUAGES)
    if key in {
        "voice_enabled",
        "mute",
        "allow_temporary_approvals",
        "audit_log_enabled",
        "use_system_theme",
        "show_floating_orb",
        "orb_always_on_top",
        "animations_enabled",
        "reduced_motion",
        "sidebar_collapsed",
        "error_recovery_enabled",
        "auto_retry_transient_errors",
        "remember_successful_fallbacks",
        "show_detailed_errors_in_debug_mode",
    }:
        return bool(value)
    if key == "confirmation_timeout_seconds":
        return _validate_int(key, value, 10, 600)
    if key in {
        "stt_timeout_seconds",
        "command_timeout_seconds",
        "app_launch_timeout_seconds",
        "close_app_timeout_seconds",
        "window_action_timeout_seconds",
        "browser_command_timeout_seconds",
        "ocr_timeout_seconds",
    }:
        return _validate_int(key, value, 1, 600)
    if key == "max_auto_retries":
        return _validate_int(key, value, 0, 3)

    _ensure_json_compatible(key, value)
    return value


def normalize_settings(loaded: dict[str, Any], *, strict: bool = True) -> dict[str, Any]:
    if not isinstance(loaded, dict):
        raise SettingValidationError("settings.json must contain a JSON object.")

    migrated = dict(loaded)
    if "push_to_talk_hotkey" not in migrated and "hotkey" in migrated:
        migrated["push_to_talk_hotkey"] = migrated["hotkey"]
    if "voice_engine" not in migrated and "tts_engine" in migrated:
        migrated["voice_engine"] = migrated["tts_engine"]
    if "tts_engine" not in migrated and "voice_engine" in migrated:
        migrated["tts_engine"] = migrated["voice_engine"]
    if "ui_language" not in migrated and "language" in migrated:
        migrated["ui_language"] = migrated["language"]
    if "language" not in migrated and "ui_language" in migrated:
        migrated["language"] = migrated["ui_language"]
    if "voice_speed" not in migrated and "voice_rate" in migrated:
        try:
            migrated["voice_speed"] = round(float(migrated["voice_rate"]) / 180.0, 2)
        except (TypeError, ValueError):
            migrated["voice_speed"] = 1.0

    merged = defaults_copy()
    merged.update(migrated)
    normalized = defaults_copy()
    normalized.update(merged)

    defaults = defaults_copy()
    for key in SETTING_SPECS:
        try:
            normalized[key] = validate_setting(key, normalized.get(key), normalized)
        except SettingValidationError:
            if strict:
                raise
            normalized[key] = defaults[key]

    try:
        _validate_hotkey_set(normalized)
    except SettingValidationError:
        if strict:
            raise
        for key in HOTKEY_KEYS:
            normalized[key] = defaults[key]
    _sync_aliases(normalized)
    return normalized


def validate_settings_payload(payload: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    base = defaults_copy()
    if current:
        base.update(current)
    base.update(payload)
    return normalize_settings(base, strict=True)


def setting_requires_restart(key: str) -> bool:
    if key in {"language", "tts_engine", "hotkey"}:
        key = {"language": "ui_language", "tts_engine": "voice_engine", "hotkey": "push_to_talk_hotkey"}[key]
    spec = SETTING_SPECS.get(key)
    return bool(spec and spec.restart_required)


def _sync_aliases(values: dict[str, Any]) -> None:
    values["hotkey"] = values["push_to_talk_hotkey"]
    values["tts_engine"] = values["voice_engine"]
    values["language"] = values["ui_language"]
    values["voice_rate"] = int(round(float(values["voice_speed"]) * 180))
    values["voice_rate"] = max(80, min(320, values["voice_rate"]))


def _validate_name(key: str, value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        label = "Assistant name" if key == "assistant_name" else "User name"
        raise SettingValidationError(f"{label} cannot be empty.")
    max_length = 30 if key == "assistant_name" else 60
    if len(text) > max_length:
        raise SettingValidationError(f"{key} is too long (max {max_length} characters).")
    if any(ord(char) < 32 for char in text):
        raise SettingValidationError(f"{key} contains unsupported control characters.")
    return text


def _validate_choice(key: str, value: Any, choices: tuple[Any, ...]) -> Any:
    if value not in choices:
        allowed = ", ".join(str(choice) for choice in choices)
        raise SettingValidationError(f"Unsupported {key}: {value!r}. Allowed values: {allowed}.")
    return value


def _validate_microphone_device(value: Any) -> str:
    text = str(value or "default").strip()
    if not text:
        return "default"
    if len(text) > 260:
        raise SettingValidationError("Microphone device selection is too long.")
    return text


def _validate_int(key: str, value: Any, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SettingValidationError(f"{key} must be an integer.") from exc
    if number < minimum or number > maximum:
        raise SettingValidationError(f"{key} must be between {minimum} and {maximum}.")
    return number


def _validate_float(key: str, value: Any, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SettingValidationError(f"{key} must be a number.") from exc
    if number < minimum or number > maximum:
        raise SettingValidationError(f"{key} must be between {minimum} and {maximum}.")
    return round(number, 3)


def _validate_hotkey_conflict(key: str, normalized: str, current: dict[str, Any]) -> None:
    for other_key in HOTKEY_KEYS:
        if other_key == key:
            continue
        try:
            other_value = normalize_hotkey(current.get(other_key))
        except SettingValidationError:
            continue
        if other_value.lower() == normalized.lower():
            raise SettingValidationError(f"Hotkey conflicts with {other_key}: {normalized}")


def _validate_hotkey_set(values: dict[str, Any]) -> None:
    seen: dict[str, str] = {}
    for key in HOTKEY_KEYS:
        hotkey = normalize_hotkey(values.get(key))
        lowered = hotkey.lower()
        if lowered in seen:
            raise SettingValidationError(f"Hotkey conflict: {key} and {seen[lowered]} both use {hotkey}.")
        seen[lowered] = key
        values[key] = hotkey


def _ensure_json_compatible(key: str, value: Any) -> None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for item in value:
            _ensure_json_compatible(key, item)
        return
    if isinstance(value, dict):
        for item_key, item_value in value.items():
            if not isinstance(item_key, str):
                raise SettingValidationError(f"{key} contains a non-string JSON object key.")
            _ensure_json_compatible(key, item_value)
        return
    raise SettingValidationError(f"{key} must be JSON-serializable.")
