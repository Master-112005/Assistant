from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from core import settings, state
from core.plugin_manager import PluginManager
from core.processor import CommandProcessor


def _write_plugin(
    plugins_dir: Path,
    folder_name: str,
    *,
    plugin_id: str,
    version: str = "1.0.0",
    min_app_version: str = "0.1.0",
    permissions: list[str] | None = None,
    enabled_by_default: bool = True,
    body: str | None = None,
) -> Path:
    folder = plugins_dir / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": plugin_id,
        "name": f"{plugin_id.title()} Plugin",
        "version": version,
        "author": "Test",
        "description": f"Test plugin {plugin_id}",
        "entry_point": "plugin.py",
        "min_app_version": min_app_version,
        "permissions_requested": permissions or [],
        "enabled_by_default": enabled_by_default,
        "category": "tests",
    }
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    plugin_body = body or f'''
from skills.base import PluginBase, SkillExecutionResult

class TestPlugin(PluginBase):
    def plugin_id(self):
        return "{plugin_id}"
    def name(self):
        return "{plugin_id.title()} Plugin"
    def version(self):
        return "{version}"
    def description(self):
        return "Test plugin {plugin_id}"
    def initialize(self, context):
        self.initialized = True
    def shutdown(self):
        self.shutdown_called = True
    def can_handle(self, command, context):
        return "{plugin_id}" in command.lower()
    def execute(self, command, context):
        return SkillExecutionResult(True, "{plugin_id}_intent", "handled by {plugin_id}", "")
    def health_check(self):
        return {{"ok": True}}
    def capabilities(self):
        return {{"commands": ["{plugin_id}"]}}
'''
    (folder / "plugin.py").write_text(plugin_body, encoding="utf-8")
    return folder


@pytest.fixture(autouse=True)
def reset_plugin_state():
    settings.reset_defaults()
    state.plugins_ready = False
    state.loaded_plugins = []
    state.plugin_errors = {}
    state.last_plugin_used = ""
    yield
    state.plugins_ready = False
    state.loaded_plugins = []
    state.plugin_errors = {}
    state.last_plugin_used = ""
    settings.reset_defaults()


def _manager(tmp_path: Path, app_version: str = "1.0.0") -> PluginManager:
    return PluginManager(
        plugins_dir=tmp_path / "plugins",
        db_path=tmp_path / "plugins.db",
        app_version=app_version,
    )


def test_discovery_finds_valid_plugin_and_ignores_invalid_folder(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    _write_plugin(plugins_dir, "demo", plugin_id="demo")
    (plugins_dir / "invalid").mkdir(parents=True)

    manager = _manager(tmp_path)
    plugins = manager.discover_plugins()

    assert [plugin.id for plugin in plugins] == ["demo"]
    assert any("Missing manifest" in error["error"] for error in manager.discovery_errors)


def test_load_plugin_imports_dynamically_and_updates_state(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    _write_plugin(plugins_dir, "demo", plugin_id="demo")
    manager = _manager(tmp_path)
    manager.discover_plugins()

    info = manager.load_plugin("demo")

    assert info is not None
    assert info.loaded is True
    assert info.healthy is True
    assert state.loaded_plugins == ["demo"]


def test_broken_plugin_is_isolated_and_disabled(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    body = '''
from skills.base import PluginBase, SkillExecutionResult

class BrokenPlugin(PluginBase):
    def plugin_id(self): return "broken"
    def name(self): return "Broken Plugin"
    def version(self): return "1.0.0"
    def description(self): return "Broken"
    def initialize(self, context): raise RuntimeError("init boom")
    def shutdown(self): pass
    def can_handle(self, command, context): return True
    def execute(self, command, context): return SkillExecutionResult(True, "broken", "no", "")
    def health_check(self): return {"ok": True}
    def capabilities(self): return {}
'''
    _write_plugin(plugins_dir, "broken", plugin_id="broken", body=body)
    manager = _manager(tmp_path)
    manager.discover_plugins()

    info = manager.load_plugin("broken")

    assert info is not None
    assert info.loaded is False
    assert info.healthy is False
    assert info.enabled is False
    assert "RuntimeError" in info.error
    assert "broken" in state.plugin_errors


def test_enable_disable_and_reload_plugin(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    _write_plugin(plugins_dir, "demo", plugin_id="demo", enabled_by_default=False)
    manager = _manager(tmp_path)
    manager.discover_plugins()

    assert manager.get_plugin("demo").enabled is False

    enabled = manager.enable("demo")
    assert enabled is not None
    assert enabled.enabled is True
    assert enabled.loaded is True

    reloaded = manager.reload("demo")
    assert reloaded is not None
    assert reloaded.loaded is True

    disabled = manager.disable("demo")
    assert disabled is not None
    assert disabled.enabled is False
    assert disabled.loaded is False


def test_route_command_to_plugin(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    _write_plugin(plugins_dir, "demo", plugin_id="demo")
    manager = _manager(tmp_path)
    manager.discover_plugins()
    manager.load_all_enabled()

    result = manager.route("demo command", {"intent": "unknown"})

    assert result is not None
    assert result.success is True
    assert result.intent == "demo_intent"
    assert result.skill_name == "Plugin:Demo Plugin"
    assert result.data["plugin_id"] == "demo"
    assert state.last_plugin_used == "demo"


def test_old_app_version_is_rejected_safely(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    _write_plugin(plugins_dir, "old", plugin_id="old", min_app_version="99.0.0")
    manager = _manager(tmp_path, app_version="1.0.0")

    plugins = manager.discover_plugins()

    assert plugins == []
    assert any("requires app version" in error["error"] for error in manager.discovery_errors)


def test_static_permission_scan_rejects_undeclared_network_import(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    body = '''
import socket
from skills.base import PluginBase, SkillExecutionResult

class UnsafePlugin(PluginBase):
    def plugin_id(self): return "unsafe"
    def name(self): return "Unsafe Plugin"
    def version(self): return "1.0.0"
    def description(self): return "Uses network without declaring it"
    def initialize(self, context): pass
    def shutdown(self): pass
    def can_handle(self, command, context): return True
    def execute(self, command, context): return SkillExecutionResult(True, "unsafe", "unsafe", "")
    def health_check(self): return {"ok": True}
    def capabilities(self): return {}
'''
    _write_plugin(plugins_dir, "unsafe", plugin_id="unsafe", body=body)
    manager = _manager(tmp_path)

    plugins = manager.discover_plugins()

    assert plugins == []
    assert any("undeclared permissions" in error["error"] for error in manager.discovery_errors)


def test_duplicate_plugin_id_is_reported_and_first_plugin_survives(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    _write_plugin(plugins_dir, "demo_a", plugin_id="demo")
    _write_plugin(plugins_dir, "demo_b", plugin_id="demo")
    manager = _manager(tmp_path)

    plugins = manager.discover_plugins()

    assert [plugin.id for plugin in plugins] == ["demo"]
    assert any("Duplicate plugin id" in error["error"] for error in manager.discovery_errors)


def test_runtime_crash_returns_failure_result_and_marks_unhealthy(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    body = '''
from skills.base import PluginBase

class CrashPlugin(PluginBase):
    def plugin_id(self): return "crash"
    def name(self): return "Crash Plugin"
    def version(self): return "1.0.0"
    def description(self): return "Crashes during execute"
    def initialize(self, context): pass
    def shutdown(self): pass
    def can_handle(self, command, context): return "crash" in command
    def execute(self, command, context): raise RuntimeError("execute boom")
    def health_check(self): return {"ok": True}
    def capabilities(self): return {}
'''
    _write_plugin(plugins_dir, "crash", plugin_id="crash", body=body)
    manager = _manager(tmp_path)
    manager.discover_plugins()
    manager.load_all_enabled()

    result = manager.route("crash now", {"intent": "unknown"})
    info = manager.get_plugin("crash")

    assert result is not None
    assert result.success is False
    assert result.error == "plugin_runtime_error"
    assert info is not None
    assert info.healthy is False
    assert info.enabled is False


def test_permission_approval_blocks_default_autoload_and_enable_approves(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    _write_plugin(plugins_dir, "secure", plugin_id="secure", permissions=["network"], enabled_by_default=True)
    manager = _manager(tmp_path)

    [info] = manager.discover_plugins()
    assert info.enabled is False
    assert info.permission_approved is False

    manager.load_all_enabled()
    assert manager.get_plugin("secure").loaded is False

    enabled = manager.enable("secure")
    assert enabled is not None
    assert enabled.permission_approved is True
    assert enabled.enabled is True
    assert enabled.loaded is True


def test_restart_preserves_enablement_in_plugins_db(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    _write_plugin(plugins_dir, "demo", plugin_id="demo", enabled_by_default=False)
    first = _manager(tmp_path)
    first.discover_plugins()
    first.enable("demo")
    first.unload("demo")

    second = _manager(tmp_path)
    [info] = second.discover_plugins()

    assert info.enabled is True
    assert info.permission_approved is True
    assert info.loaded is False
    with sqlite3.connect(tmp_path / "plugins.db") as conn:
        row = conn.execute("SELECT enabled FROM plugin_registry WHERE id = 'demo'").fetchone()
    assert row == (1,)


def test_processor_parses_plugin_management_commands():
    assert CommandProcessor._parse_plugin_management_command("list plugins") == ("list", "")
    assert CommandProcessor._parse_plugin_management_command("enable telegram plugin") == ("enable", "telegram")
    assert CommandProcessor._parse_plugin_management_command("plugin reload vs code") == ("reload", "vs code")
