"""
Persistent user settings manager.

Settings are stored as JSON, validated through ``core.config_schema``, written
atomically, and exposed through module-level helpers for older phases.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import copy
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any

from core.config_schema import (
    DEFAULT_SETTINGS,
    SettingValidationError,
    defaults_copy,
    normalize_settings,
    setting_requires_restart,
    validate_setting,
    validate_settings_payload,
)
from core.errors import SettingsError
from core.paths import DATA_DIR


SETTINGS_FILE = DATA_DIR / "settings.json"
logger = logging.getLogger(__name__)

SettingsCallback = Callable[[str, Any], None]


class SettingsManager:
    """Load, validate, persist, import, and export assistant settings."""

    def __init__(self, settings_file: Path | str | None = None) -> None:
        self.settings_file = Path(settings_file) if settings_file else SETTINGS_FILE
        self._settings: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._subscribers: list[SettingsCallback] = []

    @property
    def values(self) -> dict[str, Any]:
        if not self._settings:
            self.load()
        return copy.deepcopy(self._settings)

    def load(self) -> dict[str, Any]:
        """Load settings from disk and auto-upgrade older files with defaults."""
        with self._lock:
            if not self.settings_file.exists():
                self._settings = defaults_copy()
                self.save()
                logger.info("Settings loaded from defaults: %s", self.settings_file)
                return copy.deepcopy(self._settings)

            try:
                loaded = self._read_json(self.settings_file)
                normalized = normalize_settings(loaded, strict=False)
            except Exception as exc:
                logger.warning("Failed to load settings.json, resetting to defaults: %s", exc)
                self._backup_corrupt_file()
                self._settings = defaults_copy()
                self.save()
                return copy.deepcopy(self._settings)

            changed = normalized != loaded
            self._settings = normalized
            if changed:
                self.save()
            logger.info("Settings loaded: %s", self.settings_file)
            return copy.deepcopy(self._settings)

    def save(self) -> None:
        """Persist the current settings to disk using an atomic replace."""
        with self._lock:
            if not self._settings:
                self._settings = defaults_copy()
            last_error: Exception | None = None
            for attempt in range(5):
                try:
                    self.settings_file.parent.mkdir(parents=True, exist_ok=True)
                    temp_path = self.settings_file.with_suffix(self.settings_file.suffix + ".tmp")
                    with open(temp_path, "w", encoding="utf-8") as handle:
                        json.dump(self._settings, handle, indent=4, sort_keys=True)
                        handle.write("\n")
                    temp_path.replace(self.settings_file)
                    return
                except Exception as exc:
                    last_error = exc
                    if attempt >= 4:
                        break
                    time.sleep(0.05 * (attempt + 1))
            logger.error("Failed to save settings: %s", last_error)
            raise SettingsError(f"Failed to save settings: {last_error}") from last_error

    def get(self, key: str, default: Any = None) -> Any:
        """Return a setting value by key."""
        with self._lock:
            if not self._settings:
                self.load()
            canonical_key = self._canonical_key(key)
            if canonical_key in self._settings:
                return self._settings.get(canonical_key)
            return DEFAULT_SETTINGS.get(canonical_key, default)

    def set(self, key: str, value: Any) -> None:
        """Set a setting value, validate it, save immediately, and notify listeners."""
        with self._lock:
            if not self._settings:
                self.load()
            canonical_key = self._canonical_key(key)
            proposed = copy.deepcopy(self._settings)
            try:
                normalized_value = validate_setting(canonical_key, value, proposed)
                proposed[canonical_key] = normalized_value
                if canonical_key == "voice_rate":
                    proposed["voice_speed"] = round(float(normalized_value) / 180.0, 3)
                if canonical_key == "voice_speed":
                    proposed["voice_rate"] = int(round(float(normalized_value) * 180))
                proposed = validate_settings_payload(proposed)
            except SettingValidationError as exc:
                logger.warning("Validation failed for %s=%r: %s", key, value, exc)
                raise SettingsError(str(exc)) from exc

            old_value = self._settings.get(canonical_key)
            self._settings = proposed
            self.save()

        if old_value != self._settings.get(canonical_key):
            logger.info("Setting changed %s=%r", canonical_key, self._settings.get(canonical_key))
            self._notify(canonical_key, self._settings.get(canonical_key))

    def reset_defaults(self) -> None:
        """Reset settings to default values and save them."""
        with self._lock:
            self._settings = defaults_copy()
            self.save()
        logger.info("Settings reset to defaults")
        self._notify("*", self.values)

    def export(self, path: Path | str) -> Path:
        """Export the active settings to a JSON file."""
        target = Path(path)
        with self._lock:
            if not self._settings:
                self.load()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "w", encoding="utf-8") as handle:
                    json.dump(self._settings, handle, indent=4, sort_keys=True)
                    handle.write("\n")
            except Exception as exc:
                logger.error("Failed to export settings to %s: %s", target, exc)
                raise SettingsError(f"Failed to export settings: {exc}") from exc
        logger.info("Settings exported to %s", target)
        return target

    def import_file(self, path: Path | str) -> dict[str, Any]:
        """Import, validate, and persist settings from a JSON file."""
        source = Path(path)
        try:
            loaded = self._read_json(source)
            if not isinstance(loaded, dict):
                raise SettingValidationError("Imported settings must be a JSON object.")
            imported = validate_settings_payload(loaded, current=self._settings or defaults_copy())
        except Exception as exc:
            logger.warning("Invalid imported settings file rejected: %s", exc)
            raise SettingsError(f"Invalid settings import: {exc}") from exc

        with self._lock:
            self._settings = imported
            self.save()
        logger.info("Settings imported from %s", source)
        self._notify("*", self.values)
        return self.values

    def validate(self, key: str, value: Any) -> Any:
        """Validate a single setting and return its normalized value."""
        with self._lock:
            if not self._settings:
                self.load()
            canonical_key = self._canonical_key(key)
            try:
                return validate_setting(canonical_key, value, self._settings)
            except SettingValidationError as exc:
                logger.warning("Validation failed for %s=%r: %s", key, value, exc)
                raise SettingsError(str(exc)) from exc

    def requires_restart(self, key: str) -> bool:
        return setting_requires_restart(key)

    def subscribe(self, callback: SettingsCallback) -> None:
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: SettingsCallback) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    @staticmethod
    def _canonical_key(key: str) -> str:
        aliases = {
            "hotkey": "push_to_talk_hotkey",
            "tts_engine": "voice_engine",
            "language": "ui_language",
        }
        return aliases.get(str(key), str(key))

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise SettingValidationError("settings.json must contain a JSON object.")
        return payload

    def _backup_corrupt_file(self) -> None:
        if not self.settings_file.exists():
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup = self.settings_file.with_name(f"{self.settings_file.name}.corrupt.{timestamp}")
        try:
            self.settings_file.replace(backup)
            logger.warning("Corrupt settings backed up to %s", backup)
        except Exception:
            logger.debug("Failed to back up corrupt settings file", exc_info=True)

    def _notify(self, key: str, value: Any) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(key, value)
            except Exception:
                logger.debug("Settings subscriber failed", exc_info=True)


settings_manager = SettingsManager()
_current_settings: dict[str, Any] = {}


def _sync_manager_file() -> None:
    if settings_manager.settings_file != SETTINGS_FILE:
        settings_manager.settings_file = SETTINGS_FILE
        settings_manager._settings = {}


def _sync_current(values: dict[str, Any] | None = None) -> dict[str, Any]:
    global _current_settings
    _current_settings = copy.deepcopy(values if values is not None else settings_manager.values)
    return _current_settings


def load_settings() -> dict[str, Any]:
    _sync_manager_file()
    return _sync_current(settings_manager.load())


def save_settings() -> None:
    _sync_manager_file()
    settings_manager.save()
    _sync_current()


def get(key: str, default: Any = None) -> Any:
    _sync_manager_file()
    return settings_manager.get(key, default)


def set(key: str, value: Any) -> None:
    _sync_manager_file()
    settings_manager.set(key, value)
    _sync_current()


def reset_defaults() -> None:
    _sync_manager_file()
    settings_manager.reset_defaults()
    _sync_current()


def export(path: Path | str) -> Path:
    _sync_manager_file()
    return settings_manager.export(path)


def import_file(path: Path | str) -> dict[str, Any]:
    _sync_manager_file()
    return _sync_current(settings_manager.import_file(path))


def validate(key: str, value: Any) -> Any:
    _sync_manager_file()
    return settings_manager.validate(key, value)


load_settings()
