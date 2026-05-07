import time

import pytest

from core import settings, state
from core.clipboard import ClipboardManager
from core.clipboard_store import ClipboardStore
from core.launcher import LaunchResult
from core.processor import CommandProcessor
from skills.clipboard import ClipboardSkill


class FakeClipboardBackend:
    def __init__(self):
        self.content_type = "empty"
        self.payload = None
        self.source_app = "notepad"
        self.write_calls: list[str] = []
        self.clear_calls = 0

    def read(self):
        return self.content_type, self.payload

    def write_text(self, text: str):
        self.content_type = "text"
        self.payload = text
        self.write_calls.append(text)

    def clear(self):
        self.content_type = "empty"
        self.payload = None
        self.clear_calls += 1

    def get_source_app(self):
        return self.source_app

    def set_text(self, text: str):
        self.content_type = "text"
        self.payload = text

    def set_empty(self):
        self.content_type = "empty"
        self.payload = None


class DummyLauncher:
    def launch_by_name(self, name: str) -> LaunchResult:
        return LaunchResult(success=True, app_name=name, matched_name=name, message=f"Opening {name}")


class DummyLLM:
    def is_available(self, force_refresh: bool = False) -> bool:
        return False

    def extract_intent(self, _text: str):
        return None


class ClipboardOnlySkillsManager:
    def __init__(self, clipboard_skill: ClipboardSkill):
        self.browser_controller = object()
        self._clipboard_skill = clipboard_skill

    def load_builtin_skills(self):
        return None

    def execute_with_skill(self, context, intent: str, command: str):
        if not self._clipboard_skill.can_handle(context, intent, command):
            return None
        state.active_skill = self._clipboard_skill.name()
        state.last_skill_used = self._clipboard_skill.name()
        return self._clipboard_skill.execute(command, context)

    def set_confirm_callback(self, callback):
        self._clipboard_skill.set_confirm_callback(callback)

    @property
    def file_skill(self):
        return None

    @property
    def clipboard_skill(self):
        return self._clipboard_skill


@pytest.fixture(autouse=True)
def reset_clipboard_state():
    settings.reset_defaults()
    settings.set("context_detection_enabled", False)
    settings.set("context_engine_enabled", False)
    settings.set("clipboard_enabled", True)
    settings.set("clipboard_watch_interval_ms", 50)
    settings.set("clipboard_history_limit", 100)
    settings.set("clipboard_store_sensitive", False)
    settings.set("clipboard_mask_sensitive_preview", True)
    state.pending_confirmation = {}
    state.active_skill = ""
    state.last_skill_used = ""
    state.last_successful_action = ""
    state.last_response = ""
    state.last_intent = ""
    state.last_entities = {}
    state.clipboard_ready = False
    state.last_clipboard_item = {}
    state.clipboard_count = 0
    state.pending_clipboard_choices = []
    yield


def _manager(tmp_path, backend: FakeClipboardBackend) -> ClipboardManager:
    store = ClipboardStore(db_path=tmp_path / "clipboard.db", history_limit=100)
    return ClipboardManager(store=store, backend=backend)


def _wait_for(predicate, timeout: float = 1.5) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_clipboard_watcher_detects_text_changes(tmp_path):
    backend = FakeClipboardBackend()
    manager = _manager(tmp_path, backend)

    manager.start_watcher()
    backend.set_text("Meeting notes for tomorrow")

    assert _wait_for(lambda: manager.get_last() is not None)
    assert manager.get_last().text_preview == "Meeting notes for tomorrow"
    manager.shutdown()


def test_clipboard_watcher_ignores_duplicate_text(tmp_path):
    backend = FakeClipboardBackend()
    manager = _manager(tmp_path, backend)

    backend.set_text("Project ideas")
    assert manager.capture_change() is not None
    assert manager.capture_change() is None
    assert manager.store.stats()["count"] == 1
    manager.shutdown()


def test_clipboard_get_last_returns_latest_item(tmp_path):
    backend = FakeClipboardBackend()
    manager = _manager(tmp_path, backend)

    backend.set_text("First item")
    manager.capture_change()
    backend.set_text("Second item")
    manager.capture_change()

    latest = manager.get_last()

    assert latest is not None
    assert latest.text_preview == "Second item"
    assert latest.source_app == "notepad"
    manager.shutdown()


def test_clipboard_recent_history_lists_last_five_items(tmp_path):
    backend = FakeClipboardBackend()
    manager = _manager(tmp_path, backend)

    for index in range(6):
        backend.set_text(f"Item {index}")
        manager.capture_change()

    recent = manager.get_recent(5)

    assert [item.text_preview for item in recent] == ["Item 5", "Item 4", "Item 3", "Item 2", "Item 1"]
    manager.shutdown()


def test_clipboard_restore_selected_item_writes_back_to_clipboard(tmp_path):
    backend = FakeClipboardBackend()
    manager = _manager(tmp_path, backend)

    backend.set_text("Alpha")
    manager.capture_change()
    backend.set_text("Beta")
    manager.capture_change()
    recent = manager.get_recent(2)

    restored = manager.restore(int(recent[1].id))

    assert restored is not None
    assert backend.payload == "Alpha"
    assert backend.write_calls[-1] == "Alpha"
    manager.shutdown()


def test_clipboard_clear_current_empties_system_clipboard(tmp_path):
    backend = FakeClipboardBackend()
    manager = _manager(tmp_path, backend)

    backend.set_text("Temporary")

    assert manager.clear_current() is True
    assert backend.payload is None
    assert backend.clear_calls == 1
    manager.shutdown()


def test_clipboard_clear_history_removes_rows(tmp_path):
    backend = FakeClipboardBackend()
    manager = _manager(tmp_path, backend)

    backend.set_text("One")
    manager.capture_change()
    backend.set_text("Two")
    manager.capture_change()

    removed = manager.clear_history()

    assert removed == 2
    assert manager.store.stats()["count"] == 0
    assert state.clipboard_count == 0
    manager.shutdown()


def test_clipboard_sensitive_text_is_masked_and_not_stored_in_full(tmp_path):
    backend = FakeClipboardBackend()
    manager = _manager(tmp_path, backend)

    backend.set_text("123456")
    manager.capture_change()
    item = manager.get_last()

    assert item is not None
    assert item.is_sensitive is True
    assert item.full_text is None
    assert "masked" in item.text_preview.lower()
    manager.shutdown()


def test_clipboard_handles_empty_clipboard_without_crashing(tmp_path):
    backend = FakeClipboardBackend()
    manager = _manager(tmp_path, backend)

    backend.set_empty()

    assert manager.capture_change() is None
    assert manager.store.stats()["count"] == 0
    manager.shutdown()


def test_processor_routes_clipboard_history_and_restore_commands(tmp_path):
    backend = FakeClipboardBackend()
    manager = _manager(tmp_path, backend)
    backend.set_text("Alpha")
    manager.capture_change()
    backend.set_text("Beta")
    manager.capture_change()

    clipboard_skill = ClipboardSkill(clipboard_manager=manager)
    processor = CommandProcessor(
        launcher=DummyLauncher(),
        llm_client=DummyLLM(),
        skills_manager=ClipboardOnlySkillsManager(clipboard_skill),
    )

    history_result = processor.process("show clipboard history")
    restore_result = processor.process("restore second clipboard item")

    assert history_result["success"] is True
    assert "1. Beta" in history_result["response"]
    assert "2. Alpha" in history_result["response"]
    assert restore_result["success"] is True
    assert restore_result["intent"] == "clipboard_restore"
    assert backend.payload == "Alpha"
    assert state.last_skill_used == "ClipboardSkill"
    assert state.last_successful_action == "clipboard_restore:clipboard"
    processor.shutdown()
