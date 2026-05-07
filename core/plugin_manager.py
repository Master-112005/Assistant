"""
Manifest-driven plugin platform for Nova Assistant.

Plugins are discovered from folders containing a validated manifest.json and are
loaded dynamically with importlib. Runtime failures are isolated, persisted to a
local SQLite registry, and surfaced through state/logging without crashing core.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import ast
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import re
import sqlite3
import sys
import traceback
from types import ModuleType
from typing import Any, Mapping

from core import settings, state
from core.config import config
from core.logger import get_logger, sanitize_text
from core.paths import DATA_DIR, PLUGINS_DIR, ROOT_DIR
from core.permissions import Decision, permission_manager as default_permission_manager
from skills.base import PluginBase, SkillExecutionResult

logger = get_logger(__name__)

PLUGIN_DB_PATH = DATA_DIR / "plugins.db"
MANIFEST_NAME = "manifest.json"
PLUGIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
REQUIRED_MANIFEST_FIELDS = {
    "id",
    "name",
    "version",
    "author",
    "description",
    "entry_point",
    "min_app_version",
    "permissions_requested",
    "enabled_by_default",
    "category",
}
REQUIRED_PLUGIN_METHODS = {
    "plugin_id",
    "name",
    "version",
    "description",
    "initialize",
    "shutdown",
    "can_handle",
    "execute",
    "health_check",
    "capabilities",
}
SUPPORTED_PERMISSIONS = {
    "filesystem",
    "network",
    "automation",
    "notifications",
    "clipboard",
    "screen",
    "audio",
    "microphone",
    "camera",
    "browser",
    "memory",
    "system",
    "shell",
}
STATIC_PERMISSION_IMPORTS = {
    "network": {
        "http",
        "httpx",
        "requests",
        "socket",
        "urllib",
        "websocket",
    },
    "filesystem": {
        "glob",
        "os",
        "pathlib",
        "shutil",
        "tempfile",
    },
    "automation": {
        "keyboard",
        "mouse",
        "pyautogui",
        "pynput",
        "subprocess",
        "win32api",
        "win32com",
        "win32gui",
    },
    "clipboard": {
        "clipboard",
        "pyperclip",
    },
    "screen": {
        "mss",
        "PIL.ImageGrab",
        "pygetwindow",
    },
    "audio": {
        "pyaudio",
        "sounddevice",
        "wave",
    },
    "camera": {
        "cv2",
    },
}


class PluginValidationError(ValueError):
    """Raised when a plugin manifest or implementation violates the contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _setting(key: str, default: Any) -> Any:
    try:
        value = settings.get(key)
    except Exception:
        return default
    return default if value is None else value


def _normalize_lookup(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _parse_version(value: Any) -> tuple[int, int, int]:
    text = str(value or "").strip()
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+][A-Za-z0-9_.-]+)?$", text)
    if not match:
        raise PluginValidationError(f"Invalid semantic version: {value!r}")
    parts = [int(part) if part is not None else 0 for part in match.groups()]
    return parts[0], parts[1], parts[2]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class PluginManifest:
    id: str
    name: str
    version: str
    author: str
    description: str
    entry_point: str
    min_app_version: str
    permissions_requested: tuple[str, ...]
    enabled_by_default: bool
    category: str
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "entry_point": self.entry_point,
            "min_app_version": self.min_app_version,
            "permissions_requested": list(self.permissions_requested),
            "enabled_by_default": self.enabled_by_default,
            "category": self.category,
        }


@dataclass(slots=True)
class PluginInfo:
    id: str
    name: str
    version: str
    path: Path
    enabled: bool
    loaded: bool
    healthy: bool
    error: str = ""
    description: str = ""
    category: str = ""
    entry_point: str = ""
    min_app_version: str = ""
    permissions_requested: tuple[str, ...] = field(default_factory=tuple)
    permission_approved: bool = False
    manifest_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "path": str(self.path),
            "enabled": self.enabled,
            "loaded": self.loaded,
            "healthy": self.healthy,
            "error": self.error,
            "description": self.description,
            "category": self.category,
            "entry_point": self.entry_point,
            "min_app_version": self.min_app_version,
            "permissions_requested": list(self.permissions_requested),
            "permission_approved": self.permission_approved,
            "manifest_hash": self.manifest_hash,
        }


class PluginManager:
    """Discover, validate, load, route, and manage assistant plugins."""

    def __init__(
        self,
        *,
        plugins_dir: Path | str | None = None,
        db_path: Path | str | None = None,
        app_version: str | None = None,
        permission_manager=None,
    ) -> None:
        directory_setting = _setting("plugins_directory", "plugins")
        configured_dir = Path(plugins_dir) if plugins_dir is not None else Path(str(directory_setting))
        if not configured_dir.is_absolute():
            configured_dir = ROOT_DIR / configured_dir
        self.plugins_dir = configured_dir.resolve()
        self.db_path = Path(db_path or PLUGIN_DB_PATH)
        self.app_version = app_version or str(config.VERSION)
        self.permission_manager = permission_manager or default_permission_manager
        self._registry: dict[str, PluginInfo] = {}
        self._manifests: dict[str, PluginManifest] = {}
        self._instances: dict[str, PluginBase] = {}
        self._modules: dict[str, ModuleType] = {}
        self._discovery_errors: list[dict[str, str]] = []
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._sync_state()

    @property
    def discovery_errors(self) -> list[dict[str, str]]:
        return list(self._discovery_errors)

    def discover_plugins(self) -> list[PluginInfo]:
        """Scan plugins/ for valid plugin folders and persist registry metadata."""
        self._discovery_errors = []
        discovered_ids: set[str] = set()
        if not bool(_setting("plugins_enabled", True)):
            state.plugins_ready = False
            logger.info("Plugin discovery skipped because plugins are disabled")
            return []

        try:
            candidates = sorted(path for path in self.plugins_dir.iterdir() if path.is_dir())
        except Exception as exc:
            self._record_discovery_error(self.plugins_dir, f"Plugin directory scan failed: {exc}")
            logger.exception("Plugin discovery failed", exc=exc, plugins_dir=str(self.plugins_dir))
            self._sync_state()
            return []

        valid_ids: set[str] = set()
        for folder in candidates:
            if folder.name.startswith((".", "__")):
                continue
            manifest_path = folder / MANIFEST_NAME
            if not manifest_path.exists():
                self._record_discovery_error(folder, "Missing manifest.json")
                logger.warning("Ignoring plugin folder without manifest", plugin_path=str(folder))
                continue
            try:
                manifest = self.validate_manifest(manifest_path)
                if manifest.id in discovered_ids:
                    raise PluginValidationError(f"Duplicate plugin id: {manifest.id}")
                discovered_ids.add(manifest.id)
                info = self._build_info(folder, manifest, self._hash_file(manifest_path))
                self._registry[manifest.id] = info
                self._manifests[manifest.id] = manifest
                self._persist_info(info)
                valid_ids.add(manifest.id)
                logger.info(
                    "Plugin discovered",
                    plugin_id=manifest.id,
                    version=manifest.version,
                    enabled=info.enabled,
                    permissions=list(manifest.permissions_requested),
                )
            except PluginValidationError as exc:
                self._record_discovery_error(folder, str(exc))
                logger.warning("Plugin rejected during discovery", plugin_path=str(folder), error=str(exc))
            except Exception as exc:
                self._record_discovery_error(folder, f"Unexpected discovery failure: {exc}")
                logger.exception("Unexpected plugin discovery failure", exc=exc, plugin_path=str(folder))

        for plugin_id in list(self._registry):
            if plugin_id not in valid_ids and self._registry[plugin_id].path.parent == self.plugins_dir:
                self.unload(plugin_id)
                self._registry.pop(plugin_id, None)
                self._manifests.pop(plugin_id, None)

        state.plugins_ready = True
        self._sync_state()
        return self.list_plugins(discover=False)

    def validate_manifest(self, path: Path | str) -> PluginManifest:
        """Read and validate a plugin manifest file."""
        manifest_path = Path(path)
        if manifest_path.name != MANIFEST_NAME:
            manifest_path = manifest_path / MANIFEST_NAME
        folder = manifest_path.parent.resolve()
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PluginValidationError(f"Bad manifest JSON: {exc}") from exc
        except OSError as exc:
            raise PluginValidationError(f"Cannot read manifest: {exc}") from exc
        if not isinstance(raw, dict):
            raise PluginValidationError("Manifest must be a JSON object")

        missing = sorted(REQUIRED_MANIFEST_FIELDS - set(raw))
        if missing:
            raise PluginValidationError(f"Missing manifest fields: {', '.join(missing)}")

        plugin_id = str(raw.get("id") or "").strip()
        if not PLUGIN_ID_PATTERN.match(plugin_id):
            raise PluginValidationError("Plugin id must match ^[a-z][a-z0-9_]{1,63}$")

        name = str(raw.get("name") or "").strip()
        author = str(raw.get("author") or "").strip()
        description = str(raw.get("description") or "").strip()
        category = str(raw.get("category") or "").strip()
        if not all((name, author, description, category)):
            raise PluginValidationError("Name, author, description, and category are required")

        version = str(raw.get("version") or "").strip()
        min_app_version = str(raw.get("min_app_version") or "").strip()
        _parse_version(version)
        if _parse_version(min_app_version) > _parse_version(self.app_version):
            raise PluginValidationError(
                f"Plugin requires app version {min_app_version}, current version is {self.app_version}"
            )

        entry_point = str(raw.get("entry_point") or "").strip()
        if not entry_point or Path(entry_point).is_absolute():
            raise PluginValidationError("entry_point must be a relative file path")
        entry_path = (folder / entry_point).resolve()
        if not _is_relative_to(entry_path, folder):
            raise PluginValidationError("entry_point escapes the plugin directory")
        if not entry_path.exists() or not entry_path.is_file():
            raise PluginValidationError(f"entry_point not found: {entry_point}")
        if entry_path.suffix.lower() != ".py":
            raise PluginValidationError("entry_point must be a Python file")

        requested = raw.get("permissions_requested")
        if not isinstance(requested, list) or not all(isinstance(item, str) for item in requested):
            raise PluginValidationError("permissions_requested must be a string array")
        permissions = tuple(sorted({item.strip().lower() for item in requested if item.strip()}))
        unknown_permissions = sorted(set(permissions) - SUPPORTED_PERMISSIONS)
        if unknown_permissions:
            raise PluginValidationError(f"Unsupported permissions requested: {', '.join(unknown_permissions)}")
        detected_permissions = self._detect_static_permissions(entry_path)
        undeclared_permissions = sorted(detected_permissions - set(permissions))
        if undeclared_permissions:
            raise PluginValidationError(
                f"Entry point uses undeclared permissions: {', '.join(undeclared_permissions)}"
            )

        enabled_by_default = raw.get("enabled_by_default")
        if not isinstance(enabled_by_default, bool):
            raise PluginValidationError("enabled_by_default must be a boolean")

        return PluginManifest(
            id=plugin_id,
            name=name,
            version=version,
            author=author,
            description=description,
            entry_point=entry_point,
            min_app_version=min_app_version,
            permissions_requested=permissions,
            enabled_by_default=enabled_by_default,
            category=category,
            raw=dict(raw),
        )

    def load_all_enabled(self) -> list[PluginInfo]:
        if not self._registry:
            self.discover_plugins()
        loaded: list[PluginInfo] = []
        for info in self.list_plugins(discover=False):
            if not info.enabled:
                continue
            result = self.load_plugin(info.id)
            if result and result.loaded:
                loaded.append(result)
        self._sync_state()
        return loaded

    def load_plugin(self, plugin_id: str) -> PluginInfo | None:
        plugin_id = self.resolve_plugin_id(plugin_id) or str(plugin_id or "").strip().lower()
        if not plugin_id:
            return None
        if not self._registry:
            self.discover_plugins()
        info = self._registry.get(plugin_id)
        manifest = self._manifests.get(plugin_id)
        if info is None or manifest is None:
            logger.warning("Plugin load requested for unknown plugin", plugin_id=plugin_id)
            return None
        if info.loaded and plugin_id in self._instances:
            return info
        if not info.enabled:
            logger.info("Plugin load skipped because plugin is disabled", plugin_id=plugin_id)
            return info
        if not self._permissions_approved(info):
            self._mark_failed(info, "Plugin permission approval required before loading", disable=False)
            return info

        entry_path = (info.path / info.entry_point).resolve()
        module_name = f"nova_plugins.{plugin_id}_{info.manifest_hash[:12]}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, entry_path)
            if spec is None or spec.loader is None:
                raise PluginValidationError(f"Unable to create import spec for {entry_path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            plugin = self._create_plugin_instance(module)
            self._validate_plugin_instance(plugin, manifest)
            plugin.initialize(self._build_plugin_context(info))
            info.loaded = True
            info.healthy = True
            info.error = ""
            self._instances[plugin_id] = plugin
            self._modules[plugin_id] = module
            self._persist_info(info)
            self._sync_state()
            logger.info("Plugin loaded", plugin_id=plugin_id, version=info.version, plugin_name=info.name)
            return info
        except Exception as exc:
            sys.modules.pop(module_name, None)
            self._mark_failed(info, f"{type(exc).__name__}: {exc}", exc=exc)
            self._record_plugin_error(info, exc, phase="load")
            return info

    def enable(self, plugin_id: str) -> PluginInfo | None:
        resolved = self.resolve_plugin_id(plugin_id)
        if resolved is None:
            logger.warning("Plugin enable requested for unknown plugin", plugin_id=plugin_id)
            return None
        info = self._registry[resolved]
        info.enabled = True
        info.permission_approved = True
        if not info.error or info.error == "Plugin permission approval required before loading":
            info.healthy = True
            info.error = ""
        self._persist_info(info)
        self._sync_state()
        logger.info(
            "Plugin enabled",
            plugin_id=info.id,
            permissions=list(info.permissions_requested),
            permission_approved=info.permission_approved,
        )
        if bool(_setting("plugins_auto_load", True)):
            self.load_plugin(info.id)
        return info

    def disable(self, plugin_id: str) -> PluginInfo | None:
        resolved = self.resolve_plugin_id(plugin_id)
        if resolved is None:
            logger.warning("Plugin disable requested for unknown plugin", plugin_id=plugin_id)
            return None
        info = self._registry[resolved]
        self.unload(info.id)
        info.enabled = False
        self._persist_info(info)
        self._sync_state()
        logger.info("Plugin disabled", plugin_id=info.id)
        return info

    def reload(self, plugin_id: str) -> PluginInfo | None:
        resolved = self.resolve_plugin_id(plugin_id)
        if resolved is None:
            logger.warning("Plugin reload requested for unknown plugin", plugin_id=plugin_id)
            return None
        self.unload(resolved)
        logger.info("Plugin reload requested", plugin_id=resolved)
        return self.load_plugin(resolved)

    def unload(self, plugin_id: str) -> PluginInfo | None:
        resolved = self.resolve_plugin_id(plugin_id) or str(plugin_id or "").strip().lower()
        info = self._registry.get(resolved)
        plugin = self._instances.pop(resolved, None)
        if plugin is not None:
            try:
                plugin.shutdown()
            except Exception as exc:
                logger.warning("Plugin shutdown failed", plugin_id=resolved, error=str(exc))
        module = self._modules.pop(resolved, None)
        if module is not None:
            sys.modules.pop(module.__name__, None)
        if info is not None:
            info.loaded = False
            self._persist_info(info)
        self._sync_state()
        return info

    def shutdown_all(self) -> None:
        for plugin_id in list(self._instances):
            self.unload(plugin_id)
        logger.info("All loaded plugins shut down", loaded_plugins=list(state.loaded_plugins))

    def list_plugins(self, *, discover: bool = True) -> list[PluginInfo]:
        if discover and not self._registry:
            self.discover_plugins()
        return [self._registry[key] for key in sorted(self._registry)]

    def get_plugin(self, plugin_id: str) -> PluginInfo | None:
        resolved = self.resolve_plugin_id(plugin_id)
        return self._registry.get(resolved) if resolved else None

    def resolve_plugin_id(self, query: str) -> str | None:
        if not self._registry:
            try:
                self.discover_plugins()
            except Exception:
                return None
        raw = str(query or "").strip().lower()
        if raw.endswith(" plugin"):
            raw = raw[: -len(" plugin")].strip()
        if raw in self._registry:
            return raw
        normalized = _normalize_lookup(raw)
        for plugin_id, info in self._registry.items():
            names = {
                _normalize_lookup(plugin_id),
                _normalize_lookup(info.name),
                _normalize_lookup(info.name.replace("Plugin", "")),
            }
            if normalized in names:
                return plugin_id
        return None

    def route(self, command: str, context: Mapping[str, Any] | None = None) -> SkillExecutionResult | None:
        if not bool(_setting("plugins_enabled", True)):
            return None
        if not self._registry:
            self.discover_plugins()
            if bool(_setting("plugins_auto_load", True)):
                self.load_all_enabled()

        runtime_context = dict(context or {})
        for info in self.list_plugins(discover=False):
            if not info.enabled:
                continue
            if bool(_setting("disable_unhealthy_plugins", True)) and info.error and not info.healthy:
                continue
            if not info.loaded:
                self.load_plugin(info.id)
            plugin = self._instances.get(info.id)
            if plugin is None:
                continue
            plugin_context = {**runtime_context, **self._build_plugin_context(info)}
            try:
                if not bool(plugin.can_handle(command, plugin_context)):
                    continue
            except Exception as exc:
                self._mark_failed(info, f"can_handle failed: {type(exc).__name__}: {exc}", exc=exc)
                self._record_plugin_error(info, exc, phase="can_handle", command=command)
                continue

            permission_message = self._evaluate_runtime_permission(info, command, runtime_context)
            if permission_message:
                return SkillExecutionResult(
                    success=False,
                    intent="plugin_permission",
                    response=permission_message,
                    skill_name=self._skill_name(info),
                    error="permission_denied",
                    data={"target_app": "plugins", "plugin_id": info.id},
                )

            logger.info("Plugin route matched", plugin_id=info.id, command=command)
            state.last_plugin_used = info.id
            try:
                result = plugin.execute(command, plugin_context)
                normalized = self._normalize_result(result, info)
                self.permission_manager.record_execution(
                    "plugin_execute",
                    {"plugin_id": info.id, "permissions_requested": list(info.permissions_requested)},
                    success=normalized.success,
                    error=normalized.error,
                )
                return normalized
            except Exception as exc:
                self._mark_failed(info, f"execute failed: {type(exc).__name__}: {exc}", exc=exc)
                self._record_plugin_error(info, exc, phase="execute", command=command)
                self.permission_manager.record_execution(
                    "plugin_execute",
                    {"plugin_id": info.id, "permissions_requested": list(info.permissions_requested)},
                    success=False,
                    error=str(exc),
                )
                return SkillExecutionResult(
                    success=False,
                    intent="plugin_error",
                    response=f"{info.name} failed while handling the command.",
                    skill_name=self._skill_name(info),
                    error="plugin_runtime_error",
                    data={
                        "target_app": "plugins",
                        "plugin_id": info.id,
                        "error_type": type(exc).__name__,
                    },
                )
        return None

    def health_check_all(self) -> list[dict[str, Any]]:
        if not self._registry:
            self.discover_plugins()
        results: list[dict[str, Any]] = []
        for info in self.list_plugins(discover=False):
            if not info.enabled:
                results.append({**info.to_dict(), "health": {"ok": False, "status": "disabled"}})
                continue
            if not info.loaded:
                self.load_plugin(info.id)
            plugin = self._instances.get(info.id)
            if plugin is None:
                results.append({**info.to_dict(), "health": {"ok": False, "status": "not_loaded"}})
                continue
            try:
                health = plugin.health_check()
                ok = bool(health.get("ok", health.get("healthy", True))) if isinstance(health, dict) else bool(health)
                info.healthy = ok
                info.error = "" if ok else "Health check reported unhealthy"
                self._persist_info(info)
                results.append({**info.to_dict(), "health": health if isinstance(health, dict) else {"ok": ok}})
            except Exception as exc:
                self._mark_failed(info, f"health_check failed: {type(exc).__name__}: {exc}", exc=exc)
                self._record_plugin_error(info, exc, phase="health_check")
                results.append({**info.to_dict(), "health": {"ok": False, "error": str(exc)}})
        self._sync_state()
        return results

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS plugin_registry (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    path TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    permission_approved INTEGER NOT NULL,
                    loaded INTEGER NOT NULL,
                    healthy INTEGER NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    permissions_requested TEXT NOT NULL DEFAULT '[]',
                    category TEXT NOT NULL DEFAULT '',
                    last_seen TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS plugin_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plugin_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    traceback TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _persist_info(self, info: PluginInfo) -> None:
        try:
            now = _utc_now()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO plugin_registry (
                        id, name, version, path, manifest_hash, enabled,
                        permission_approved, loaded, healthy, error,
                        permissions_requested, category, last_seen, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        version=excluded.version,
                        path=excluded.path,
                        manifest_hash=excluded.manifest_hash,
                        enabled=excluded.enabled,
                        permission_approved=excluded.permission_approved,
                        loaded=excluded.loaded,
                        healthy=excluded.healthy,
                        error=excluded.error,
                        permissions_requested=excluded.permissions_requested,
                        category=excluded.category,
                        last_seen=excluded.last_seen,
                        updated_at=excluded.updated_at
                    """,
                    (
                        info.id,
                        info.name,
                        info.version,
                        str(info.path),
                        info.manifest_hash,
                        int(info.enabled),
                        int(info.permission_approved),
                        int(info.loaded),
                        int(info.healthy),
                        info.error,
                        json.dumps(list(info.permissions_requested)),
                        info.category,
                        now,
                        now,
                    ),
                )
        except Exception as exc:
            logger.error("Plugin registry persistence failed", exc=exc, plugin_id=info.id)

    def _persist_event(self, plugin_id: str, event_type: str, message: str, stacktrace: str = "") -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO plugin_events (plugin_id, event_type, message, traceback, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (plugin_id, event_type, sanitize_text(message), stacktrace, _utc_now()),
                )
        except Exception as exc:
            logger.error("Plugin event persistence failed", exc=exc, plugin_id=plugin_id, event_type=event_type)

    def _registry_row(self, plugin_id: str) -> sqlite3.Row | None:
        try:
            with self._connect() as conn:
                return conn.execute("SELECT * FROM plugin_registry WHERE id = ?", (plugin_id,)).fetchone()
        except Exception as exc:
            logger.error("Plugin registry read failed", exc=exc, plugin_id=plugin_id)
            return None

    def _build_info(self, folder: Path, manifest: PluginManifest, manifest_hash: str) -> PluginInfo:
        row = self._registry_row(manifest.id)
        permission_approved = (
            bool(row["permission_approved"])
            if row is not None
            else not bool(manifest.permissions_requested) or not bool(_setting("require_plugin_permission_approval", True))
        )
        enabled = bool(row["enabled"]) if row is not None else bool(manifest.enabled_by_default)
        if bool(_setting("require_plugin_permission_approval", True)) and manifest.permissions_requested and not permission_approved:
            enabled = False
        return PluginInfo(
            id=manifest.id,
            name=manifest.name,
            version=manifest.version,
            path=folder.resolve(),
            enabled=enabled,
            loaded=False,
            healthy=True,
            error="",
            description=manifest.description,
            category=manifest.category,
            entry_point=manifest.entry_point,
            min_app_version=manifest.min_app_version,
            permissions_requested=manifest.permissions_requested,
            permission_approved=permission_approved,
            manifest_hash=manifest_hash,
        )

    def _create_plugin_instance(self, module: ModuleType) -> PluginBase:
        factory = getattr(module, "create_plugin", None)
        if callable(factory):
            plugin = factory()
            if not isinstance(plugin, PluginBase):
                self._validate_plugin_shape(plugin)
            return plugin

        for _name, candidate in inspect.getmembers(module, inspect.isclass):
            if candidate is PluginBase:
                continue
            if issubclass(candidate, PluginBase):
                return candidate()

        for _name, candidate in inspect.getmembers(module, inspect.isclass):
            if candidate.__module__ != module.__name__:
                continue
            try:
                instance = candidate()
            except Exception:
                continue
            self._validate_plugin_shape(instance)
            return instance
        raise PluginValidationError("No PluginBase implementation or create_plugin() factory found")

    def _validate_plugin_shape(self, plugin: Any) -> None:
        missing = sorted(method for method in REQUIRED_PLUGIN_METHODS if not callable(getattr(plugin, method, None)))
        if missing:
            raise PluginValidationError(f"Plugin implementation missing methods: {', '.join(missing)}")

    def _validate_plugin_instance(self, plugin: Any, manifest: PluginManifest) -> None:
        self._validate_plugin_shape(plugin)
        if str(plugin.plugin_id()).strip() != manifest.id:
            raise PluginValidationError("plugin_id() does not match manifest id")
        if str(plugin.version()).strip() != manifest.version:
            raise PluginValidationError("version() does not match manifest version")

    def _build_plugin_context(self, info: PluginInfo) -> dict[str, Any]:
        return {
            "app_version": self.app_version,
            "plugin_id": info.id,
            "plugin_name": info.name,
            "plugin_path": str(info.path),
            "manifest": self._manifests.get(info.id).to_dict() if info.id in self._manifests else {},
            "permissions_requested": list(info.permissions_requested),
            "permission_approved": info.permission_approved,
            "settings": settings,
        }

    def _permissions_approved(self, info: PluginInfo) -> bool:
        if not bool(_setting("require_plugin_permission_approval", True)):
            return True
        return not info.permissions_requested or info.permission_approved

    def _evaluate_runtime_permission(
        self,
        info: PluginInfo,
        command: str,
        context: Mapping[str, Any],
    ) -> str:
        try:
            result = self.permission_manager.evaluate(
                "plugin_execute",
                {
                    "plugin_id": info.id,
                    "plugin_name": info.name,
                    "command": command,
                    "intent": context.get("intent", ""),
                    "permissions_requested": list(info.permissions_requested),
                    "permission_approved": info.permission_approved,
                },
            )
        except Exception as exc:
            logger.warning("Plugin permission evaluation failed", plugin_id=info.id, error=str(exc))
            return "Plugin permission evaluation failed. The plugin was not executed."
        if result.decision == Decision.ALLOW:
            return ""
        return result.reason

    def _normalize_result(self, result: Any, info: PluginInfo) -> SkillExecutionResult:
        if isinstance(result, SkillExecutionResult):
            if not result.skill_name:
                result.skill_name = self._skill_name(info)
            result.data = {**(result.data or {}), "plugin_id": info.id, "target_app": "plugins"}
            return result
        if isinstance(result, dict):
            return SkillExecutionResult(
                success=bool(result.get("success", False)),
                intent=str(result.get("intent") or "plugin_action"),
                response=str(result.get("response") or ""),
                skill_name=str(result.get("skill_name") or result.get("skill") or self._skill_name(info)),
                handled=bool(result.get("handled", True)),
                error=str(result.get("error") or ""),
                data={**(result.get("data") if isinstance(result.get("data"), dict) else {}), "plugin_id": info.id, "target_app": "plugins"},
            )
        return SkillExecutionResult(
            success=True,
            intent="plugin_action",
            response=str(result),
            skill_name=self._skill_name(info),
            data={"plugin_id": info.id, "target_app": "plugins"},
        )

    def _mark_failed(
        self,
        info: PluginInfo,
        message: str,
        *,
        exc: BaseException | None = None,
        disable: bool | None = None,
    ) -> None:
        info.loaded = False
        info.healthy = False
        info.error = message
        self._instances.pop(info.id, None)
        module = self._modules.pop(info.id, None)
        if module is not None:
            sys.modules.pop(module.__name__, None)
        should_disable = bool(_setting("disable_unhealthy_plugins", True)) if disable is None else disable
        if should_disable:
            info.enabled = False
        self._persist_info(info)
        self._persist_event(info.id, "failure", message, traceback.format_exc() if exc else "")
        self._sync_state()
        logger.error("Plugin failed", exc=exc, plugin_id=info.id, error=message, disabled=should_disable)

    def _record_plugin_error(
        self,
        info: PluginInfo,
        exc: BaseException,
        *,
        phase: str,
        command: str = "",
    ) -> None:
        try:
            from core.analytics import analytics

            analytics.record_error(
                f"plugin_{phase}_error",
                str(exc),
                exc=exc,
                module=f"plugin:{info.id}",
                context=command,
                command_context=json.dumps({"plugin_id": info.id, "phase": phase, "command": command}),
                source="plugin_manager",
                metadata={"plugin_id": info.id, "plugin_name": info.name, "phase": phase},
            )
        except Exception:
            logger.debug("Failed to record plugin error in analytics", plugin_id=info.id, exc_info=True)

    def _record_discovery_error(self, path: Path, message: str) -> None:
        entry = {"path": str(path), "error": message}
        self._discovery_errors.append(entry)
        state.plugin_errors = {**getattr(state, "plugin_errors", {}), str(path): message}

    def _sync_state(self) -> None:
        state.loaded_plugins = sorted(plugin_id for plugin_id, info in self._registry.items() if info.loaded)
        state.plugin_errors = {plugin_id: info.error for plugin_id, info in self._registry.items() if info.error}

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _skill_name(info: PluginInfo) -> str:
        return f"Plugin:{info.name}"

    @staticmethod
    def _detect_static_permissions(entry_path: Path) -> set[str]:
        try:
            tree = ast.parse(entry_path.read_text(encoding="utf-8"), filename=str(entry_path))
        except SyntaxError as exc:
            raise PluginValidationError(f"Plugin entry point has invalid Python syntax: {exc}") from exc
        except OSError as exc:
            raise PluginValidationError(f"Cannot scan plugin entry point: {exc}") from exc

        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)

        detected: set[str] = set()
        for permission, modules in STATIC_PERMISSION_IMPORTS.items():
            for imported in imports:
                if any(imported == module or imported.startswith(f"{module}.") for module in modules):
                    detected.add(permission)
                    break
        return detected


__all__ = [
    "PluginBase",
    "PluginInfo",
    "PluginManager",
    "PluginManifest",
    "PluginValidationError",
]
