import pytest

from core import settings, state
from core.browser import BrowserOperationResult
from core.errors import DeviceUnavailableError
from core.launcher import LaunchResult
from core.permissions import permission_manager as default_permission_manager
from core.processor import CommandProcessor
from core.recovery import RecoveryManager
import core.recovery as recovery_module


@pytest.fixture(autouse=True)
def reset_recovery_state():
    settings.reset_defaults()
    settings.set("context_detection_enabled", False)
    settings.set("context_engine_enabled", False)
    state.last_error = ""
    state.last_recovery_plan = {}
    state.pending_recovery_choices = []
    state.recovery_stats = {}
    state.last_entities = {}
    state.last_message_target = ""
    state.current_context = "unknown"
    state.current_app = "unknown"
    state.pending_confirmation = {}
    state.pending_confirmations = {}
    with default_permission_manager._lock:
        default_permission_manager._pending.clear()
        default_permission_manager._grants.clear()
        default_permission_manager._sync_state_locked()
    yield
    settings.reset_defaults()
    state.last_recovery_plan = {}
    state.pending_recovery_choices = []
    state.recovery_stats = {}
    state.pending_confirmation = {}
    state.pending_confirmations = {}
    with default_permission_manager._lock:
        default_permission_manager._pending.clear()
        default_permission_manager._grants.clear()
        default_permission_manager._sync_state_locked()


class DummyLauncher:
    def __init__(self, results: dict[str, LaunchResult]) -> None:
        self.results = results
        self.launched: list[str] = []

    def launch_by_name(self, name: str) -> LaunchResult:
        self.launched.append(name)
        return self.results.get(
            name,
            LaunchResult(success=True, app_name=name, matched_name=name, message=f"Opening {name.title()}"),
        )


class DummyLLM:
    def is_available(self, force_refresh: bool = False) -> bool:
        return True

    def correct_stt(self, _text: str):
        return None

    def extract_intent(self, _text: str):
        return None

    def plan_actions(self, _text: str, context=None):
        return None


class DummyBrowserController:
    def search(self, query: str, browser_name: str | None = None, engine: str | None = None):
        return BrowserOperationResult(
            success=True,
            action="search",
            message=f"Searching {query} in {(browser_name or 'browser').title()}.",
            browser_id=browser_name or "",
            query=query,
            verified=True,
            data={"engine": engine or "browser"},
        )


class DummySkillsManager:
    def __init__(self) -> None:
        self.browser_controller = DummyBrowserController()
        self.file_skill = None
        self.clipboard_skill = None
        self.reminder_skill = None

    def load_builtin_skills(self):
        return None

    def execute_with_skill(self, context, intent: str, command: str):
        return None


def _build_processor(tmp_path, launcher: DummyLauncher) -> CommandProcessor:
    processor = CommandProcessor(
        launcher=launcher,
        llm_client=DummyLLM(),
        skills_manager=DummySkillsManager(),
    )
    processor.recovery._memory_path = tmp_path / "recovery_memory.json"
    processor.recovery._memory = {"preferences": {}}
    processor.recovery.clear_pending()
    return processor


def test_missing_browser_offers_edge_recovery(monkeypatch, tmp_path):
    monkeypatch.setattr(
        recovery_module,
        "find_alternative_browser",
        lambda *args, **kwargs: [{"app_name": "edge", "display_name": "Microsoft Edge", "category": "browser"}],
    )
    launcher = DummyLauncher(
        {
            "chrome": LaunchResult(
                success=False,
                app_name="chrome",
                message="Chrome is not installed.",
                error="app_not_found",
                data={"requested_app": "chrome"},
            )
        }
    )
    processor = _build_processor(tmp_path, launcher)

    result = processor.process("open chrome")

    assert result["success"] is False
    assert result["intent"] == "recovery_required"
    assert "Chrome is not installed." in result["response"]
    assert "Open Microsoft Edge" in result["response"]
    assert state.pending_recovery_choices[0]["option_id"] == "open_edge"


def test_recovery_reply_executes_fallback_and_remembers_choice(monkeypatch, tmp_path):
    monkeypatch.setattr(
        recovery_module,
        "find_alternative_browser",
        lambda *args, **kwargs: [{"app_name": "edge", "display_name": "Microsoft Edge", "category": "browser"}],
    )
    launcher = DummyLauncher(
        {
            "chrome": LaunchResult(
                success=False,
                app_name="chrome",
                message="Chrome is not installed.",
                error="app_not_found",
                data={"requested_app": "chrome"},
            ),
            "edge": LaunchResult(success=True, app_name="edge", matched_name="edge", message="Opening Edge"),
        }
    )
    processor = _build_processor(tmp_path, launcher)

    first = processor.process("open chrome")
    second = processor.process("1")

    assert first["intent"] == "recovery_required"
    assert second["success"] is True
    assert "Opening Edge" in second["response"]
    assert launcher.launched == ["chrome", "edge"]
    assert state.pending_recovery_choices == []
    memory = processor.recovery._memory["preferences"]
    assert memory["app_not_found:chrome"]["preferred_option_id"] == "open_edge"


def test_microphone_failure_can_switch_to_text_mode(tmp_path):
    processor = _build_processor(tmp_path, DummyLauncher({}))

    result = processor.handle_external_error(
        DeviceUnavailableError(
            "Microphone is unavailable.",
            code="microphone_unavailable",
            context={"device": "microphone"},
        ),
        command_context={"command": "voice input", "device": "microphone", "intent": "voice_input"},
        source="voice",
    )
    follow_up = processor.process("1")

    assert result["intent"] == "recovery_required"
    assert "Switch to text input" in result["response"]
    assert follow_up["success"] is True
    assert follow_up["data"]["input_mode"] == "text"
    assert follow_up["data"]["focus_text_input"] is True


def test_contact_not_found_suggests_similar_contacts(monkeypatch, tmp_path):
    monkeypatch.setattr(
        recovery_module,
        "find_similar_contact",
        lambda *args, **kwargs: [
            {"name": "Hemanth Rao", "platform": "whatsapp", "aliases": [], "score": 0.93},
            {"name": "Hemant", "platform": "whatsapp", "aliases": [], "score": 0.88},
        ],
    )
    manager = RecoveryManager(memory_path=tmp_path / "recovery_memory.json")

    outcome = manager.handle(
        {
            "success": False,
            "intent": "whatsapp_message",
            "response": "I couldn't find a WhatsApp contact named Hemanth.",
            "error": "contact_not_found",
            "data": {"contact": "Hemanth", "platform": "whatsapp"},
        },
        {"command": "call Hemanth", "contact": "Hemanth", "platform": "whatsapp"},
    )

    assert outcome.result["intent"] == "recovery_required"
    assert "Hemanth Rao" in outcome.result["response"]
    assert "Hemant" in outcome.result["response"]


def test_timeout_retries_only_once_per_recovery_attempt(tmp_path):
    calls: list[str] = []

    def executor(action: str, params_json: dict, context: dict) -> dict:
        calls.append(action)
        return {
            "success": False,
            "intent": "open_app",
            "response": "Chrome launch timed out.",
            "error": "timeout",
            "data": {"requested_app": "chrome"},
        }

    manager = RecoveryManager(action_executor=executor, memory_path=tmp_path / "recovery_memory.json")

    outcome = manager.handle(
        {
            "success": False,
            "intent": "open_app",
            "response": "Chrome launch timed out.",
            "error": "timeout",
            "data": {"requested_app": "chrome"},
        },
        {"command": "open chrome", "intent": "open_app", "requested_app": "chrome"},
    )

    assert calls == ["retry_original"]
    assert outcome.result["intent"] == "recovery_required"
    assert "It opened already" in outcome.result["response"]


def test_handle_external_error_reinfers_timed_out_open_app_intent(monkeypatch, tmp_path):
    processor = _build_processor(tmp_path, DummyLauncher({}))
    calls: list[str] = []

    def _fake_open_app(app_name: str) -> dict[str, object]:
        calls.append(app_name)
        return processor._build_result(
            True,
            "open_app",
            f"Opening {app_name}.",
            data={"target_app": app_name, "verified": True},
        )

    monkeypatch.setattr(processor, "handle_open_app", _fake_open_app)

    result = processor.handle_external_error(
        {
            "message": "The command 'open chrome' timed out after 20 seconds.",
            "error": "action_timeout",
            "intent": "action_timeout",
            "recoverable": True,
            "timeout_seconds": 20,
            "command": "open chrome",
        },
        command_context={
            "command": "open chrome",
            "raw_input": "open chrome",
            "normalized_input": "open chrome",
            "detected_intent": "action_timeout",
            "intent": "action_timeout",
        },
        source="text",
    )

    assert result["success"] is True
    assert calls == ["chrome"]
