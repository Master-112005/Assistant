import pytest
import time

from core.browser import BrowserOperationResult
from core import correction, settings, state
from core.context_engine import context_engine
from core.file_index import FileIndex
from core.file_search import SmartFileSearch
from core.files import FileManager
from core.launcher import LaunchResult
from core.notifications import set_global_notification_manager
from core.path_resolver import PathResolver
from core.permissions import permission_manager as default_permission_manager
from core.processor import CommandProcessor
from core.recovery import RecoveryOutcome
from core.safety import FileSafetyPolicy
from core.schemas import CorrectedCommand, IntentSchema, PlanSchema, PlanStep
from skills.files import FileSkill
from skills.base import SkillExecutionResult


@pytest.fixture(autouse=True)
def reset_processor_state():
    settings.reset_defaults()
    settings.set("context_detection_enabled", False)
    settings.set("context_engine_enabled", False)
    settings.set("smart_file_search_enabled", False)
    settings.set("use_file_index", False)
    settings.set("index_update_on_startup", False)
    context_engine._history.clear()
    correction._corrector_instance = None
    correction.CORRECTIONS_CACHE_PATH.write_text("{}", encoding="utf-8")
    state.current_context = "unknown"
    state.current_app = "unknown"
    state.current_window_title = ""
    state.current_process_name = ""
    state.recent_commands = []
    state.last_successful_action = ""
    state.last_target_app = ""
    state.last_replayable_command = ""
    state.last_window_action = {}
    state.last_entities = {}
    state.last_browser = ""
    state.last_search_query = ""
    state.last_browser_action = ""
    state.browser_ready = False
    state.last_navigation_time = 0.0
    state.active_skill = ""
    state.last_skill_used = ""
    state.last_chrome_action = ""
    state.chrome_tabs_opened_count = 0
    state.last_page_title = ""
    state.last_recovery_plan = {}
    state.pending_recovery_choices = []
    state.recovery_stats = {}
    state.last_youtube_query = ""
    state.last_video_title = ""
    state.youtube_active = False
    state.last_media_action = ""
    state.whatsapp_active = False
    state.last_contact_search = {}
    state.pending_contact_choices = []
    state.last_message_target = ""
    state.last_chat_name = ""
    state.music_active = False
    state.active_music_provider = ""
    state.last_track_name = ""
    state.last_artist_name = ""
    state.ocr_ready = False
    state.last_ocr_text = ""
    state.last_ocr_engine = ""
    state.last_screenshot_path = ""
    state.last_text_matches = []
    state.last_awareness_report = {}
    state.last_desktop_snapshot = {}
    state.awareness_ready = False
    state.last_visible_apps = []
    state.last_text_click_target = {}
    state.last_text_click_result = {}
    state.last_clicked_position = {}
    state.text_click_count = 0
    state.last_file_action = ""
    state.last_file_path = ""
    state.last_destination_path = ""
    state.last_system_action = {}
    state.last_volume = -1
    state.last_brightness = -1
    state.wifi_state = "unknown"
    state.bluetooth_state = "unknown"
    state.pending_confirmation = {}
    state.recent_files_touched = []
    state.last_file_search_query = {}
    state.last_file_search_results = []
    state.pending_file_choices = []
    state.file_index_ready = False
    state.clipboard_ready = False
    state.last_clipboard_item = {}
    state.clipboard_count = 0
    state.pending_clipboard_choices = []
    state.scheduler_running = False
    state.next_reminder_time = ""
    state.last_triggered_reminder = {}
    state.reminder_count = 0
    state.notifications_ready = False
    state.notification_queue_size = 0
    state.last_notification = {}
    state.recent_notifications = []
    state.pending_confirmation = {}
    state.pending_confirmations = {}
    with default_permission_manager._lock:
        default_permission_manager._pending.clear()
        default_permission_manager._grants.clear()
        default_permission_manager._sync_state_locked()
    set_global_notification_manager(None)
    yield


class DummyLauncher:
    def __init__(self):
        self.launched = []

    def launch_by_name(self, name: str) -> LaunchResult:
        self.launched.append(name)
        return LaunchResult(
            success=True,
            app_name=name,
            matched_name=name,
            message=f"{name.title()} opened successfully.",
            verified=True,
        )


class DummyLLM:
    def __init__(self):
        self.available = True
        self.corrected = None
        self.intent = None
        self.plan = None

    def is_available(self, force_refresh: bool = False) -> bool:
        return self.available

    def correct_stt(self, _text: str):
        return self.corrected

    def extract_intent(self, _text: str):
        return self.intent

    def plan_actions(self, _text: str, context=None):
        return self.plan


class DummyBrowserController:
    def __init__(self):
        self.search_calls = []
        self.scroll_down_calls = 0
        self.go_back_calls = 0
        self.focus_calls = 0

    def focus_browser(self, browser_name: str | None = None, *, launch_if_missing: bool = False):
        self.focus_calls += 1
        state.current_context = browser_name or "chrome"
        state.current_app = browser_name or "chrome"
        state.current_process_name = f"{(browser_name or 'chrome')}.exe"
        return BrowserOperationResult(
            success=True,
            action="focus_browser",
            message="Chrome focused.",
            browser_id=browser_name or "chrome",
            verified=True,
            state=type("BrowserState", (), {"title": "Google - Chrome", "browser_id": browser_name or "chrome"})(),
        )

    def search(self, query: str, browser_name: str | None = None, engine: str | None = None):
        self.search_calls.append((query, browser_name, engine))
        state.current_context = browser_name or "chrome"
        state.current_app = browser_name or "chrome"
        state.current_process_name = f"{(browser_name or 'chrome')}.exe"
        state.browser_ready = True
        state.last_browser = browser_name or "chrome"
        state.last_search_query = query
        return BrowserOperationResult(
            success=True,
            action="search",
            message=f"Searching {query} in Chrome.",
            browser_id=browser_name or "chrome",
            query=query,
            verified=True,
        )

    def scroll_down(self, amount: int = 500, *, browser_name: str | None = None):
        self.scroll_down_calls += 1
        return BrowserOperationResult(
            success=True,
            action="scroll",
            message="Scrolling page down.",
            browser_id=browser_name or "chrome",
            verified=True,
        )

    def is_browser_installed(self, browser_name: str | None = None):
        return True

    def is_browser_ready(self, browser_name: str | None = None):
        return True


class DummySkillsManager:
    def __init__(
        self,
        result_by_command: dict[str, SkillExecutionResult] | None = None,
        file_skill: FileSkill | None = None,
        skills_by_name: dict[str, object] | None = None,
    ):
        self.browser_controller = DummyBrowserController()
        self.result_by_command = result_by_command or {}
        self._file_skill = file_skill
        self._skills_by_name = skills_by_name or {}
        self.calls = []

    def load_builtin_skills(self):
        return None

    def execute_with_skill(self, context, intent: str, command: str):
        self.calls.append((context, intent, command))
        result = self.result_by_command.get(command)
        if result is not None:
            state.active_skill = result.skill_name
            state.last_skill_used = result.skill_name
            return result
        if (
            self._file_skill is not None
            and (context.get("target_route") in {"file", None, ""})
        ):
            result = self._file_skill.execute(command, context)
            state.active_skill = result.skill_name
            state.last_skill_used = result.skill_name
        return result

    def go_back(self, *, browser_name: str | None = None):
        self.go_back_calls += 1
        return BrowserOperationResult(
            success=True,
            action="go_back",
            message="Going back in Chrome.",
            browser_id=browser_name or "chrome",
            verified=True,
        )

    @property
    def file_skill(self):
        return self._file_skill

    def get_skill(self, name: str):
        return self._skills_by_name.get(name)


class FailingFileSkill:
    def can_handle(self, context, intent, command):
        return True

    def execute(self, command, context):
        raise AssertionError(f"FileSkill must not handle playback command: {command}")


def test_processor_uses_llm_stt_correction_for_speech():
    llm = DummyLLM()
    llm.corrected = CorrectedCommand(
        original_text="opan chrome",
        corrected_text="open chrome",
        confidence=0.94,
    )
    launcher = DummyLauncher()
    processor = CommandProcessor(launcher=launcher, llm_client=llm, skills_manager=DummySkillsManager())
    captured = {}

    def fake_execute(intent: str, entities: dict, text: str):
        captured["intent"] = intent
        captured["entities"] = dict(entities)
        captured["text"] = text
        return {
            "success": True,
            "intent": "open_app",
            "response": "Opening Chrome. Chrome opened successfully.",
            "error": "",
            "data": {"target_app": "chrome", "backend": "app_launcher", "route": "launcher"},
            "action_result": {
                "success": True,
                "action": "open_app",
                "target": "chrome",
                "message": "Chrome opened successfully.",
                "data": {"target_app": "chrome", "backend": "app_launcher"},
                "error_code": None,
                "verified": True,
                "duration_ms": 1,
            },
        }

    processor.command_executor.execute = fake_execute

    result = processor.process("opan chrome", source="speech")

    assert result["success"] is True
    assert captured["intent"] == "open_app"
    assert captured["entities"]["app"] == "chrome"
    assert captured["text"] == "open chrome"


def test_processor_returns_honest_fallback_when_llm_is_unavailable():
    llm = DummyLLM()
    llm.available = False
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=llm, skills_manager=DummySkillsManager())

    result = processor.process("open chrome and search IPL score")

    assert result["success"] is False
    assert result["intent"] == "llm_unavailable"
    assert result["response"] == "Local AI model unavailable. Using standard command mode."


def test_processor_routes_how_are_you_to_chat_success():
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())

    result = processor.process("how are you")

    assert result["success"] is True
    assert result["category"] == "chat"
    assert result["intent"] == "question"
    assert result["response"] == "I'm doing well and ready to help."
    assert result["error"] == ""


def test_processor_routes_hi_how_are_you_to_chat_success():
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())

    result = processor.process("hi how are you")

    assert result["success"] is True
    assert result["category"] == "chat"
    assert result["intent"] == "question"
    assert result["response"] == "I'm doing well and ready to help."


def test_processor_routes_timer_command_to_reminder_skill():
    class ReminderProbeSkillsManager(DummySkillsManager):
        @property
        def reminder_skill(self):
            return object()

        def execute_with_skill(self, context, intent: str, command: str):
            self.calls.append((context, intent, command))
            assert context.get("target_route") == "reminders"
            assert intent == "reminder_create"
            return SkillExecutionResult(
                success=True,
                intent="reminder_create",
                response="Reminder set for in 5 minutes: timer",
                skill_name="ReminderSkill",
                data={"target_app": "reminders"},
            )

    processor = CommandProcessor(
        launcher=DummyLauncher(),
        llm_client=DummyLLM(),
        skills_manager=ReminderProbeSkillsManager(),
    )

    result = processor.process("set timer for 5 min")

    assert result["success"] is True
    assert result["intent"] == "reminder_create"
    assert result["data"]["route"] == "reminders"


def test_processor_routes_identity_and_thanks_to_chat_success():
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())

    identity = processor.process("who are you")
    thanks = processor.process("thanks")

    assert identity["success"] is True
    assert identity["category"] == "chat"
    assert identity["intent"] == "identity"
    assert "My name is" in identity["response"]

    assert thanks["success"] is True
    assert thanks["category"] == "chat"
    assert thanks["intent"] == "gratitude"
    assert thanks["response"] == "You're welcome."


def test_processor_executes_llm_plan_for_multi_action():
    llm = DummyLLM()
    skills_manager = DummySkillsManager()
    llm.intent = IntentSchema(
        intent="multi_action",
        confidence=0.96,
        entities={"apps": ["chrome"], "query": "IPL score"},
        reason="Contains two actions",
    )
    llm.plan = PlanSchema(
        steps=[
            PlanStep(order=1, action="open_app", target="chrome", params={}),
            PlanStep(order=2, action="search", target="chrome", params={"query": "IPL score"}),
        ]
    )
    launcher = DummyLauncher()
    processor = CommandProcessor(launcher=launcher, llm_client=llm, skills_manager=skills_manager)

    result = processor.process("open chrome and search IPL score")

    assert result["success"] is True
    assert result["intent"] == "multi_action"
    assert "Chrome" in result["response"]
    assert "Searching IPL score in Chrome." in result["response"]


def test_processor_uses_skill_manager_for_chrome_search():
    skills_manager = DummySkillsManager(
        result_by_command={
            "search ipl score": SkillExecutionResult(
                success=True,
                intent="chrome_action",
                response="Searching IPL score in Chrome",
                skill_name="ChromeSkill",
            )
        }
    )
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=skills_manager)

    result = processor.process("search IPL score")

    assert result["success"] is True
    assert result["intent"] == "chrome_action"
    assert result["response"] == "Searching IPL score in Chrome"
    assert skills_manager.calls[0][1] == "search_web"
    assert state.last_skill_used == "ChromeSkill"


def test_processor_routes_close_chrome_through_direct_executor():
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())
    captured = {}

    def fake_execute(intent: str, entities: dict, text: str):
        captured["intent"] = intent
        captured["entities"] = dict(entities)
        captured["text"] = text
        return {
            "success": True,
            "intent": "close_app",
            "response": "Closing Chrome. Chrome closed successfully.",
            "error": "",
            "data": {"target_app": "chrome", "backend": "window_control", "speak_response": True},
            "action_result": {
                "success": True,
                "action": "close_app",
                "target": "chrome",
                "message": "Chrome closed successfully.",
                "data": {"target_app": "chrome", "backend": "window_control"},
                "error_code": None,
                "verified": True,
                "duration_ms": 1,
            },
        }

    processor.command_executor.execute = fake_execute

    result = processor.process("close chrome")

    assert result["success"] is True
    assert result["intent"] == "close_app"
    assert result["response"] == "Closing Chrome. Chrome closed successfully."
    assert captured["intent"] == "close_app"
    assert captured["entities"]["app"] == "chrome"
    assert result["data"]["backend"] == "window_control"
    assert result["data"]["route"] == "window_control"
    assert result["data"]["trace"]["detected_intent"] == "close_app"
    assert result["data"]["trace"]["route"] == "window_control"
    assert result["data"]["trace"]["verified"] is True
    assert state.last_successful_action == "close_app:chrome"
    assert state.last_replayable_command == "close chrome"


def test_processor_clamps_close_app_timeout_below_command_timeout():
    settings.set("command_timeout_seconds", 20)
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())

    timeout_seconds = processor._resolve_timeout_seconds("close_app")

    assert timeout_seconds == 19.0


def test_processor_fast_path_routes_explicit_system_control_to_direct_executor():
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())
    captured = {}

    def fake_execute(intent: str, entities: dict, text: str):
        captured["intent"] = intent
        captured["entities"] = dict(entities)
        captured["text"] = text
        return {
            "success": True,
            "intent": "set_brightness",
            "response": "Brightness set to 50%.",
            "error": "",
            "data": {"target_app": "system", "backend": "system", "route": "system"},
            "action_result": {
                "success": True,
                "action": "set_brightness",
                "target": "system",
                "message": "Brightness set to 50%.",
                "data": {"target_app": "system"},
                "error_code": None,
                "verified": True,
                "duration_ms": 1,
            },
        }

    processor.command_executor.execute = fake_execute

    result = processor.process("increase the brightness to 50")

    assert result["success"] is True
    assert result["intent"] == "set_brightness"
    assert result["response"] == "Brightness set to 50%."
    assert captured["intent"] == "set_brightness"
    assert captured["text"] == "increase the brightness to 50"


def test_processor_skips_llm_health_check_for_simple_direct_command():
    llm = DummyLLM()
    availability_checks = {"count": 0}

    def _is_available(force_refresh: bool = False) -> bool:
        availability_checks["count"] += 1
        return True

    llm.is_available = _is_available
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=llm, skills_manager=DummySkillsManager())
    processor.command_executor.execute = lambda intent, entities, text: {
        "success": True,
        "intent": "open_app",
        "response": "Opening Chrome. Chrome opened successfully.",
        "error": "",
        "data": {"target_app": "chrome", "backend": "app_launcher", "route": "launcher"},
        "action_result": {
            "success": True,
            "action": "open_app",
            "target": "chrome",
            "message": "Chrome opened successfully.",
            "data": {"target_app": "chrome", "backend": "app_launcher"},
            "error_code": None,
            "verified": True,
            "duration_ms": 1,
        },
    }

    result = processor.process("open chrome")

    assert result["success"] is True
    assert availability_checks["count"] == 0


def test_processor_skips_llm_for_low_information_unknown_text():
    llm = DummyLLM()
    intent_calls = {"count": 0}

    def _extract_intent(_text: str):
        intent_calls["count"] += 1
        return None

    llm.extract_intent = _extract_intent
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=llm, skills_manager=DummySkillsManager())

    result = processor.process("asdfgh")

    assert result["success"] is False
    assert result["intent"] == "unknown"
    assert intent_calls["count"] == 0


@pytest.mark.parametrize(
    ("command", "expected_app"),
    [
        ("open whatsapp", "whatsapp"),
        ("open wahtsapp", "whatsapp"),
        ("open chrome", "chrome"),
    ],
)
def test_processor_routes_explicit_open_commands_to_launcher(command, expected_app):
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())
    captured = {}

    def fake_execute(intent: str, entities: dict, text: str):
        captured["intent"] = intent
        captured["entities"] = dict(entities)
        captured["text"] = text
        return {
            "success": True,
            "intent": "open_app",
            "response": f"Opening {expected_app.title()}. {expected_app.title()} opened successfully.",
            "error": "",
            "data": {"target_app": expected_app, "backend": "app_launcher", "route": "launcher"},
            "action_result": {
                "success": True,
                "action": "open_app",
                "target": expected_app,
                "message": f"{expected_app.title()} opened successfully.",
                "data": {"target_app": expected_app, "backend": "app_launcher"},
                "error_code": None,
                "verified": True,
                "duration_ms": 1,
            },
        }

    processor.command_executor.execute = fake_execute

    result = processor.process(command)

    assert result["success"] is True
    assert result["intent"] == "open_app"
    assert captured["intent"] == "open_app"
    assert captured["entities"]["app"] == expected_app
    assert captured["text"] == f"open {expected_app}"
    assert result["data"]["route"] == "launcher"
    assert result["data"]["trace"]["detected_intent"] == "open_app"
    assert result["data"]["trace"]["route"] == "launcher"


def test_processor_bounds_open_app_execution_with_internal_timeout():
    settings.set("command_timeout_seconds", 5)
    settings.set("error_recovery_enabled", False)
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())
    processor._resolve_timeout_seconds = lambda *args, **kwargs: 0.5

    def fake_execute(intent: str, entities: dict, text: str):
        time.sleep(0.8)
        return {
            "success": True,
            "intent": "open_app",
            "response": "Opening WhatsApp.",
            "error": "",
            "data": {"target_app": "whatsapp", "backend": "app_launcher", "route": "launcher"},
        }

    processor.command_executor.execute = fake_execute

    started_at = time.perf_counter()
    result = processor.process("open whatsapp")
    elapsed = time.perf_counter() - started_at

    assert result["success"] is False
    assert result["intent"] == "open_app"
    assert result["error"] == "action_timeout"
    assert elapsed < 0.75


def test_processor_routes_restore_chrome_through_direct_executor():
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())
    captured = {}

    def fake_execute(intent: str, entities: dict, text: str):
        captured["intent"] = intent
        captured["entities"] = dict(entities)
        captured["text"] = text
        return {
            "success": True,
            "intent": "restore_app",
            "response": "Restoring Chrome. Chrome restored.",
            "error": "",
            "data": {"target_app": "chrome", "backend": "window_control", "speak_response": True},
            "action_result": {
                "success": True,
                "action": "restore_app",
                "target": "chrome",
                "message": "Chrome restored.",
                "data": {"target_app": "chrome", "backend": "window_control"},
                "error_code": None,
                "verified": True,
                "duration_ms": 1,
            },
        }

    processor.command_executor.execute = fake_execute

    result = processor.process("restore chrome")

    assert result["success"] is True
    assert result["intent"] == "restore_app"
    assert captured["intent"] == "restore_app"
    assert captured["entities"]["app"] == "chrome"


def test_processor_resolves_close_it_from_active_context():
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())
    state.current_context = "chrome"
    state.current_app = "chrome"
    captured = {}

    def fake_execute(intent: str, entities: dict, text: str):
        captured["intent"] = intent
        captured["entities"] = dict(entities)
        return {
            "success": True,
            "intent": "close_app",
            "response": "Closing Chrome. Chrome closed successfully.",
            "error": "",
            "data": {"target_app": "chrome", "backend": "window_control", "speak_response": True},
            "action_result": {
                "success": True,
                "action": "close_app",
                "target": "chrome",
                "message": "Chrome closed successfully.",
                "data": {"target_app": "chrome", "backend": "window_control"},
                "error_code": None,
                "verified": True,
                "duration_ms": 1,
            },
        }

    processor.command_executor.execute = fake_execute

    result = processor.process("close it")

    assert result["success"] is True
    assert captured["intent"] == "close_app"
    assert captured["entities"]["app"] == "chrome"
    assert state.last_replayable_command == "close chrome"


def test_processor_repeats_last_replayable_command():
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())
    state.last_replayable_command = "close chrome"
    calls = []

    def fake_execute(intent: str, entities: dict, text: str):
        calls.append((intent, dict(entities), text))
        return {
            "success": True,
            "intent": "close_app",
            "response": "Closing Chrome. Chrome closed successfully.",
            "error": "",
            "data": {"target_app": "chrome", "backend": "window_control", "speak_response": True},
            "action_result": {
                "success": True,
                "action": "close_app",
                "target": "chrome",
                "message": "Chrome closed successfully.",
                "data": {"target_app": "chrome", "backend": "window_control"},
                "error_code": None,
                "verified": True,
                "duration_ms": 1,
            },
        }

    processor.command_executor.execute = fake_execute

    result = processor.process("do the same thing")

    assert result["success"] is True
    assert result["intent"] == "close_app"
    assert calls[0][0] == "close_app"
    assert calls[0][1]["app"] == "chrome"


def test_processor_uses_skill_manager_for_chrome_navigation():
    skills_manager = DummySkillsManager(
        result_by_command={
            "scroll down": SkillExecutionResult(
                success=True,
                intent="chrome_action",
                response="Scrolling page down",
                skill_name="ChromeSkill",
            )
        }
    )
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=skills_manager)

    result = processor.process("scroll down")

    assert result["success"] is True
    assert result["intent"] == "chrome_action"
    assert result["response"] == "Scrolling page down"


def test_processor_routes_open_new_tab_through_chrome_skill():
    skills_manager = DummySkillsManager(
        result_by_command={
            "open new tab": SkillExecutionResult(
                success=True,
                intent="chrome_action",
                response="Opened a new tab",
                skill_name="ChromeSkill",
            )
        }
    )
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=skills_manager)

    result = processor.process("open new tab")

    assert result["success"] is True
    assert result["intent"] == "chrome_action"
    assert skills_manager.calls[0][1] == "browser_tab_new"


def test_processor_routes_open_website_in_chrome_with_browser_entity():
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())
    captured = {}

    def fake_execute(intent: str, entities: dict, text: str):
        captured["intent"] = intent
        captured["entities"] = dict(entities)
        return {
            "success": True,
            "intent": "open_website",
            "response": "Opening YouTube.",
            "error": "",
            "data": {"target_app": "chrome", "backend": "browser", "route": "browser"},
            "action_result": {
                "success": True,
                "action": "open_website",
                "target": "youtube",
                "message": "YouTube opened successfully.",
                "data": {"target_app": "chrome"},
                "error_code": None,
                "verified": True,
                "duration_ms": 1,
            },
        }

    processor.command_executor.execute = fake_execute

    result = processor.process("open youtube in chrome")

    assert result["success"] is True
    assert captured["intent"] == "open_website"
    assert captured["entities"]["browser"] == "chrome"
    assert captured["entities"]["url"] == "https://www.youtube.com"


def test_processor_routes_open_youtube_to_youtube_skill_before_generic_browser():
    skills_manager = DummySkillsManager(
        result_by_command={
            "open youtube": SkillExecutionResult(
                success=True,
                intent="youtube_action",
                response="Opening YouTube.",
                skill_name="YouTubeSkill",
                data={"target_app": "youtube"},
            )
        }
    )
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=skills_manager)

    result = processor.process("open youtube")

    assert result["success"] is True
    assert result["intent"] == "youtube_action"
    assert result["response"] == "Opening YouTube."
    assert skills_manager.calls[0][1] == "open_website"
    assert result["data"]["route"] == "youtube"
    assert result["data"]["backend"] == "YouTubeSkill"
    assert state.last_skill_used == "YouTubeSkill"


def test_processor_routes_search_whatsapp_features_to_search():
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())

    result = processor.process("search whatsapp features")

    assert result["success"] is True
    assert result["intent"] == "search_web"
    assert result["data"]["route"] == "browser"
    assert result["data"]["target_app"] == "chrome"
    assert state.last_search_query == "whatsapp features"


def test_processor_falls_back_to_search_for_unknown_app():
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())

    def fake_execute(intent: str, entities: dict, text: str):
        return {
            "success": False,
            "intent": "open_app",
            "response": "I couldn't find an installed app named fluxcap.",
            "error": "app_not_found",
            "data": {"target_app": "fluxcap", "requested_app": "fluxcap", "backend": "app_launcher", "route": "launcher"},
            "action_result": {
                "success": False,
                "action": "open_app",
                "target": "fluxcap",
                "message": "I couldn't find an installed app named fluxcap.",
                "data": {"target_app": "fluxcap"},
                "error_code": "app_not_found",
                "verified": False,
                "duration_ms": 1,
            },
        }

    processor.command_executor.execute = fake_execute

    result = processor.process("open fluxcap")

    assert result["success"] is True
    assert result["intent"] in {"search", "search_web"}
    assert result["data"]["route"] == "browser"
    assert result["data"]["fallback_from"] == "open_app"
    assert state.last_search_query == "fluxcap"
    assert processor.skills_manager.browser_controller.search_calls[-1] == ("fluxcap", "", "google")


def test_processor_preserves_attempted_route_in_recovery_trace():
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())

    def fake_execute(intent: str, entities: dict, text: str):
        return {
            "success": False,
            "intent": "open_app",
            "response": "I couldn't verify that WhatsApp opened.",
            "error": "launch_not_verified",
            "data": {"target_app": "whatsapp", "requested_app": "whatsapp", "backend": "app_launcher", "route": "launcher"},
            "action_result": {
                "success": False,
                "action": "open_app",
                "target": "whatsapp",
                "message": "I couldn't verify that WhatsApp opened.",
                "data": {"target_app": "whatsapp"},
                "error_code": "launch_not_verified",
                "verified": False,
                "duration_ms": 1,
            },
            "skill_name": "CommandExecutor",
        }

    processor.command_executor.execute = fake_execute
    processor.recovery.handle = lambda error, context: RecoveryOutcome(
        resolved=False,
        result={
            "success": False,
            "intent": "recovery_required",
            "response": "Retry or cancel.",
            "error": "launch_not_verified",
            "data": {"target_app": "recovery"},
        },
        plan=None,
    )

    result = processor.process("open whatsapp")
    trace = result["data"]["trace"]

    assert trace["route"] == "launcher"
    assert trace["backend"] == "app_launcher"
    assert trace["selected_skill"] == "CommandExecutor"
    assert trace["fallback_triggered"] is True


def test_processor_routing_latency_under_50ms_for_explicit_app_command():
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=DummySkillsManager())

    def fake_execute(intent: str, entities: dict, text: str):
        app_name = str(entities.get("app") or "whatsapp")
        return {
            "success": True,
            "intent": "open_app",
            "response": f"Opening {app_name.title()}. {app_name.title()} opened successfully.",
            "error": "",
            "data": {"target_app": app_name, "backend": "app_launcher", "route": "launcher"},
            "action_result": {
                "success": True,
                "action": "open_app",
                "target": app_name,
                "message": f"{app_name.title()} opened successfully.",
                "data": {"target_app": app_name},
                "error_code": None,
                "verified": True,
                "duration_ms": 1,
            },
            "skill_name": "CommandExecutor",
        }

    processor.command_executor.execute = fake_execute

    started = time.perf_counter()
    for _ in range(25):
        result = processor.process("open wahtsapp")
        assert result["success"] is True
    average_latency = (time.perf_counter() - started) / 25

    assert average_latency < 0.05


def test_processor_repeated_routing_is_stable():
    llm = DummyLLM()
    llm.available = False
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=llm, skills_manager=DummySkillsManager())

    def fake_execute(intent: str, entities: dict, text: str):
        app_name = str(entities.get("app") or "chrome")
        return {
            "success": True,
            "intent": intent,
            "response": f"Handled {text}",
            "error": "",
            "data": {"target_app": app_name, "backend": "app_launcher", "route": "launcher"},
            "action_result": {
                "success": True,
                "action": intent,
                "target": app_name,
                "message": f"Handled {text}",
                "data": {"target_app": app_name},
                "error_code": None,
                "verified": True,
                "duration_ms": 1,
            },
            "skill_name": "CommandExecutor",
        }

    processor.command_executor.execute = fake_execute
    latencies = []
    commands = ["open whatsapp", "open wahtsapp", "open chrome", "search whatsapp features"]

    for index in range(50):
        command = commands[index % len(commands)]
        started = time.perf_counter()
        result = processor.process(command)
        latencies.append(time.perf_counter() - started)
        assert result["success"] is True

    first_window = sum(latencies[:10]) / 10
    last_window = sum(latencies[-10:]) / 10
    assert last_window <= first_window * 2.5


def test_processor_uses_skill_manager_for_youtube_control():
    skills_manager = DummySkillsManager(
        result_by_command={
            "pause": SkillExecutionResult(
                success=True,
                intent="youtube_action",
                response="Paused video",
                skill_name="YouTubeSkill",
                data={"target_app": "youtube"},
            )
        }
    )
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=skills_manager)

    state.current_context = "youtube"
    state.current_app = "youtube"
    state.current_window_title = "Lofi mix - YouTube - Google Chrome"
    state.current_process_name = "chrome.exe"
    result = processor.process("pause")

    assert result["success"] is True
    assert result["intent"] == "youtube_action"
    assert result["response"] == "Paused video"
    assert state.last_skill_used == "YouTubeSkill"
    assert state.last_successful_action == "youtube_action:youtube"


def test_processor_routes_continue_playback_through_nlu_media_fast_path():
    class FakeYouTubeSkill:
        def resume(self):
            return SkillExecutionResult(
                success=True,
                intent="youtube_action",
                response="Resumed video",
                skill_name="YouTubeSkill",
                data={"target_app": "youtube"},
            )

    processor = CommandProcessor(
        launcher=DummyLauncher(),
        llm_client=DummyLLM(),
        skills_manager=DummySkillsManager(skills_by_name={"YouTubeSkill": FakeYouTubeSkill()}),
    )

    state.current_context = "youtube"
    state.current_app = "youtube"
    state.current_window_title = "Lofi mix - YouTube - Google Chrome"
    state.current_process_name = "chrome.exe"

    result = processor.process("continue playback")

    assert result["success"] is True
    assert result["response"] == "Resuming YouTube playback."
    assert result["data"]["target_app"] == "youtube"
    assert result["data"]["trace"]["detected_intent"] == "media_resume"


def test_processor_uses_skill_manager_for_whatsapp_message():
    skills_manager = DummySkillsManager(
        result_by_command={
            "message rakesh hello": SkillExecutionResult(
                success=True,
                intent="whatsapp_message",
                response="Message sent to Rakesh",
                skill_name="WhatsAppSkill",
                data={"target_app": "whatsapp", "contact": "Rakesh"},
            )
        }
    )
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=skills_manager)

    result = processor.process("message rakesh hello")

    assert result["success"] is True
    assert result["intent"] == "whatsapp_message"
    assert result["response"] == "Message sent to Rakesh"
    assert state.last_skill_used == "WhatsAppSkill"


def test_processor_routes_smart_file_search_and_follow_up_selection(tmp_path):
    settings.set("smart_file_search_enabled", True)
    settings.set("use_file_index", True)
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    documents = home / "Documents"
    downloads = home / "Downloads"
    desktop = home / "Desktop"
    pictures = home / "Pictures"
    music = home / "Music"
    videos = home / "Videos"
    for folder in (home, workspace, documents, downloads, desktop, pictures, music, videos):
        folder.mkdir(parents=True, exist_ok=True)

    (documents / "budget.xlsx").write_text("numbers", encoding="utf-8")
    (documents / "budget-2025.xlsx").write_text("numbers", encoding="utf-8")

    opened: list[str] = []
    resolver = PathResolver(
        base_dir=workspace,
        user_home=home,
        known_locations={
            "desktop": desktop,
            "documents": documents,
            "downloads": downloads,
            "pictures": pictures,
            "music": music,
            "videos": videos,
        },
    )
    manager = FileManager(path_resolver=resolver, opener=lambda path: opened.append(path), trash_func=lambda _path: None)
    search = SmartFileSearch(
        file_manager=manager,
        path_resolver=resolver,
        file_index=FileIndex(db_path=tmp_path / "file_index.db"),
        max_files_examined=100000,
    )
    search._index.build_index([documents])
    state.file_index_ready = True
    file_skill = FileSkill(
        file_manager=manager,
        safety_policy=FileSafetyPolicy(path_resolver=resolver),
        smart_search=search,
    )
    processor = CommandProcessor(
        launcher=DummyLauncher(),
        llm_client=DummyLLM(),
        skills_manager=DummySkillsManager(file_skill=file_skill),
    )

    first = processor.process("show excel files in documents")

    assert first["success"] is True
    assert first["intent"] == "file_search"
    assert "budget.xlsx" in first["response"]
    expected_second = str(state.pending_file_choices[1]["path"])

    second = processor.process("open second one")

    assert second["success"] is True
    assert second["intent"] == "file_open"
    assert opened == [expected_second]
    assert state.last_successful_action == "file_open:files"


def test_processor_uses_skill_manager_for_music_query():
    skills_manager = DummySkillsManager(
        result_by_command={
            "play believer": SkillExecutionResult(
                success=True,
                intent="music_play_track",
                response="Playing Believer - Imagine Dragons in Spotify",
                skill_name="MusicSkill",
                data={"target_app": "spotify", "track_name": "Believer", "artist_name": "Imagine Dragons"},
            )
        }
    )
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=skills_manager)

    result = processor.process("play believer")

    assert result["success"] is True
    assert result["intent"] == "music_play_track"
    assert result["response"] == "Playing Believer - Imagine Dragons in Spotify"
    assert state.last_skill_used == "MusicSkill"
    assert state.last_successful_action == "music_play_track:spotify"


def test_processor_routes_resume_the_video_to_media_and_not_file_skill():
    processor = CommandProcessor(
        launcher=DummyLauncher(),
        llm_client=DummyLLM(),
        skills_manager=DummySkillsManager(file_skill=FailingFileSkill()),
    )

    result = processor.process("resume the video")

    assert result["success"] is True
    assert result["intent"] in {"play_media", "youtube_action", "music_action"}
    assert "file" not in result["intent"]


def test_processor_uses_skill_manager_for_click_by_text():
    skills_manager = DummySkillsManager(
        result_by_command={
            "click login": SkillExecutionResult(
                success=True,
                intent="click_by_text",
                response="Found Login and clicked it.",
                skill_name="ClickTextSkill",
                data={
                    "target_app": "screen",
                    "click_result": {
                        "success": True,
                        "matched_text": "Login",
                        "clicked_x": 640,
                        "clicked_y": 360,
                        "match_score": 0.97,
                        "verification_passed": True,
                        "message": "Found Login and clicked it.",
                    },
                },
            )
        }
    )
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=skills_manager)

    result = processor.process("click login")

    assert result["success"] is True
    assert result["intent"] == "click_by_text"
    assert result["response"] == "Found Login and clicked it."
    assert state.last_skill_used == "ClickTextSkill"
    assert state.last_successful_action == "click_by_text:screen"


def test_processor_uses_skill_manager_for_file_action():
    skills_manager = DummySkillsManager(
        result_by_command={
            "delete notes.txt": SkillExecutionResult(
                success=True,
                intent="file_delete",
                response="Moved notes.txt to Recycle Bin.",
                skill_name="FileSkill",
                data={
                    "target_app": "files",
                    "action": "delete",
                    "source_path": "C:\\Users\\User\\Desktop\\notes.txt",
                },
            )
        }
    )
    processor = CommandProcessor(launcher=DummyLauncher(), llm_client=DummyLLM(), skills_manager=skills_manager)

    result = processor.process("delete notes.txt")

    assert result["success"] is True
    assert result["intent"] == "file_delete"
    assert result["response"] == "Moved notes.txt to Recycle Bin."
    assert state.last_skill_used == "FileSkill"
    assert state.last_successful_action == "file_delete:files"
