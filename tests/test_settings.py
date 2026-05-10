"""
Tests for settings management.
"""
import pytest
from core.paths import DATA_DIR
from core import settings
from core.errors import SettingsError

@pytest.fixture(autouse=True)
def setup_settings():
    """Fixture to reset settings before and after each test."""
    settings.reset_defaults()
    yield
    settings.reset_defaults()

def test_settings_auto_created():
    """Test that settings.json is created."""
    assert settings.SETTINGS_FILE.exists()

def test_load_works():
    """Test that loading settings returns a dictionary."""
    data = settings.load_settings()
    assert isinstance(data, dict)
    assert data["assistant_name"] == "Nova"
    assert data["planner_mode"] == "rules"

def test_set_get_works():
    """Test getting and setting values."""
    settings.set("theme", "light")
    assert settings.get("theme") == "light"
    
    settings.set("new_key", "new_val")
    assert settings.get("new_key") == "new_val"

def test_save_persists():
    """Test that saving settings persists to disk."""
    settings.set("assistant_name", "TestAssistant")
    
    # Reload from disk
    settings.load_settings()
    assert settings.get("assistant_name") == "TestAssistant"


def test_missing_llm_keys_are_merged():
    """Older settings files should be upgraded with new defaults."""
    settings.SETTINGS_FILE.write_text('{"assistant_name": "Nova"}', encoding="utf-8")
    data = settings.load_settings()
    assert data["assistant_name"] == "Nova"
    assert data["planner_mode"] == "rules"
    assert data["context_engine_enabled"] is True
    assert data["use_recent_history_for_context"] is True
    assert data["context_confidence_threshold"] == 0.70
    assert data["preferred_browser"] == "chrome"
    assert data["typing_delay_ms"] == 20
    assert data["browser_focus_timeout"] == 5
    assert data["safe_mode_clicks"] is True
    assert data["auto_launch_chrome_if_needed"] is True
    assert data["system_controls_enabled"] is True
    assert data["confirm_shutdown"] is True
    assert data["confirm_restart"] is True
    assert data["confirm_lock"] is False
    assert data["default_volume_step"] == 10
    assert data["default_brightness_step"] == 10
    assert data["chrome_skill_enabled"] is True
    assert data["read_results_mode"] == "best_available"
    assert data["youtube_skill_enabled"] is True
    assert data["auto_open_youtube_if_needed"] is True
    assert data["youtube_search_wait_ms"] == 1500
    assert data["youtube_prefer_keyboard_shortcuts"] is True
    assert data["whatsapp_skill_enabled"] is True
    assert data["auto_open_whatsapp_if_needed"] is True
    assert data["confirm_before_sending_message"] is False
    assert data["read_private_content_mode"] == "names_only"
    assert data["music_skill_enabled"] is True
assert data["preferred_music_app"] == "spotify"
    assert data["auto_open_music_app_if_needed"] is True
    assert data["media_key_control_enabled"] is True
    assert data["save_debug_screenshots"] is False
    assert data["screen_awareness_enabled"] is True
    assert data["awareness_max_items"] == 5
    assert data["speak_awareness_summary"] is False
    assert data["ignore_background_windows"] is True
    assert data["click_text_enabled"] is True
    assert data["click_text_min_confidence"] == 0.55
    assert data["click_text_verify"] is True
    assert data["click_text_fuzzy_match"] is True
    assert data["highlight_target_before_click"] is False
    assert data["file_operations_enabled"] is True
    assert data["safe_delete_default"] is True
    assert data["confirm_delete"] is True
    assert data["confirm_overwrite"] is True
    assert data["search_common_folders"] is True
    assert data["file_create_missing_parents"] is False
    assert data["file_search_max_depth"] == 4
    assert data["file_search_max_results"] == 10
    assert data["file_bulk_delete_confirmation_threshold"] == 25
    assert data["confirm_outside_user_scope"] is True
    assert data["file_recent_history_limit"] == 10
    assert data["smart_file_search_enabled"] is True
    assert data["use_file_index"] is True
    assert data["search_default_limit"] == 5
    assert data["search_default_scope"] == "user_folders"
    assert data["index_update_on_startup"] is True
    assert data["notifications_enabled"] is True
    assert data["desktop_notifications"] is True
    assert data["in_app_notifications"] is True
    assert data["voice_notifications"] is True
    assert data["notification_rate_limit_per_min"] == 10
    assert data["notification_duration_seconds"] == 5
    assert data["error_recovery_enabled"] is True
    assert data["auto_retry_transient_errors"] is True
    assert data["max_auto_retries"] == 1
    assert data["remember_successful_fallbacks"] is True
    assert data["show_detailed_errors_in_debug_mode"] is False


def test_phase37_settings_are_persisted_and_aliased():
    settings.set("assistant_name", "Orion")
    settings.set("voice_speed", 1.25)
    settings.set("push_to_talk_hotkey", "ctrl+space")

    reloaded = settings.load_settings()

    assert reloaded["assistant_name"] == "Orion"
    assert reloaded["voice_speed"] == 1.25
    assert reloaded["voice_rate"] == 225
    assert settings.get("hotkey") == "Ctrl+Space"


def test_invalid_phase37_values_are_rejected():
    with pytest.raises(SettingsError):
        settings.set("assistant_name", "")
    with pytest.raises(SettingsError):
        settings.set("permission_level", "root")
    with pytest.raises(SettingsError):
        settings.set("speech_language", "fr-FR")
    with pytest.raises(SettingsError):
        settings.set("push_to_talk_mode", "voice_activated")
    with pytest.raises(SettingsError):
        settings.set("sample_rate", 4000)
    with pytest.raises(SettingsError):
        settings.set("silence_threshold", 0.5)


def test_hotkey_conflict_is_rejected():
    current_push_to_talk = settings.get("push_to_talk_hotkey")
    with pytest.raises(SettingsError):
        settings.set("open_assistant_hotkey", current_push_to_talk)


def test_import_export_roundtrip(tmp_path):
    export_path = tmp_path / "settings-export.json"
    settings.set("assistant_name", "ExportedNova")
    settings.set("theme", "light")
    settings.export(export_path)

    settings.set("assistant_name", "Changed")
    settings.set("theme", "dark")
    imported = settings.import_file(export_path)

    assert imported["assistant_name"] == "ExportedNova"
    assert imported["theme"] == "light"
    assert settings.get("assistant_name") == "ExportedNova"


def test_invalid_import_is_rejected(tmp_path):
    path = tmp_path / "bad-settings.json"
    path.write_text('{"permission_level": "god_mode"}', encoding="utf-8")

    with pytest.raises(SettingsError):
        settings.import_file(path)


def test_corrupt_config_falls_back_to_defaults():
    settings.SETTINGS_FILE.write_text("{this is not json", encoding="utf-8")

    data = settings.load_settings()

    assert data["assistant_name"] == "Nova"
    assert data["push_to_talk_hotkey"] == "Ctrl+Space"


def test_invalid_loaded_setting_repairs_key_without_losing_valid_values():
    settings.SETTINGS_FILE.write_text(
        '{"assistant_name": "KeptName", "permission_level": "god_mode"}',
        encoding="utf-8",
    )

    data = settings.load_settings()

    assert data["assistant_name"] == "KeptName"
    assert data["permission_level"] == "BASIC"
