from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from core import correction, settings, state
from core.browser import BrowserOperationResult
from core.config_schema import normalize_hotkey
from core.context_engine import context_engine
from core.file_index import FileIndex
from core.file_search import SmartFileSearch
from core.files import FileManager
from core.launcher import LaunchResult
from core.path_resolver import PathResolver
from core.permissions import permission_manager as default_permission_manager
from core.processor import CommandProcessor
from core.system_controls import ParsedSystemCommand, SystemActionResult
from skills.base import SkillExecutionResult
from skills.files import FileSkill
from skills.system import SystemSkill


class MatrixNLP:
    def is_available(self, force_refresh: bool = False) -> bool:
        return True


class MatrixLauncher:
    def __init__(self) -> None:
        self.launched: list[str] = []

    def launch_by_name(self, name: str) -> LaunchResult:
        self.launched.append(name)
        return LaunchResult(
            success=True,
            app_name=name,
            matched_name=name,
            message=f"Opening {name.title()}",
            pid=1000 + len(self.launched),
            verified=True,
            data={"requested_app": name, "matched_name": name},
        )


class MatrixBrowserController:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, str | None, str | None]] = []
        self.opened_urls: list[str] = []

    def search(self, query: str, browser_name: str | None = None, engine: str | None = None):
        self.search_calls.append((query, browser_name, engine))
        return BrowserOperationResult(
            success=True,
            action="search",
            message=f"Searching {query}.",
            browser_id=browser_name or "chrome",
            query=query,
            url=f"https://www.google.com/search?q={query}",
            verified=True,
        )

    def open_url(self, url: str, browser_name: str | None = None):
        self.opened_urls.append(url)
        return BrowserOperationResult(
            success=True,
            action="open_url",
            message=f"Opened {url}.",
            browser_id=browser_name or "chrome",
            url=url,
            verified=True,
        )

    def is_browser_installed(self, browser_name: str | None = None):
        return True

    def is_browser_ready(self, browser_name: str | None = None):
        return True


class MatrixSystemController:
    def __init__(self) -> None:
        self.volume = 50
        self.muted = False
        self.executed: list[ParsedSystemCommand] = []

    def execute(self, command: ParsedSystemCommand) -> SystemActionResult:
        self.executed.append(command)
        if command.action == "volume_up":
            previous = self.volume
            self.volume = min(100, self.volume + (command.value or 10))
            return SystemActionResult(True, "volume_up", {"percent": previous}, {"percent": self.volume}, f"Volume increased to {self.volume}%.")
        if command.action == "mute":
            previous = self.muted
            self.muted = True
            return SystemActionResult(True, "mute", {"muted": previous}, {"muted": True}, "Muted.")
        return SystemActionResult(False, command.action, {}, {}, f"Unsupported action: {command.action}", error="unsupported")


class MatrixSkillsManager:
    def __init__(self, *, system_skill: SystemSkill | None = None) -> None:
        self.browser_controller = MatrixBrowserController()
        self.system_skill = system_skill
        self.calls: list[tuple[str, str]] = []

    def load_builtin_skills(self):
        return None

    def execute_with_skill(self, context, intent: str, command: str):
        self.calls.append((intent, command))
        if self.system_skill is not None and self.system_skill.can_handle(context, intent, command):
            state.active_skill = self.system_skill.name()
            state.last_skill_used = self.system_skill.name()
            return self.system_skill.execute(command, context)

        if intent in {"send_message", "call_contact"}:
            entities = dict(context.get("entities") or {})
            contact = str(entities.get("contact") or "").strip()
            message = str(entities.get("message") or "").strip()
            state.active_skill = "WhatsAppSkill"
            state.last_skill_used = "WhatsAppSkill"
            return SkillExecutionResult(
                success=True,
                intent="whatsapp_message" if intent == "send_message" else "whatsapp_call",
                response=f"Messaging {contact}: {message}" if message else f"Ready for {contact}.",
                skill_name="WhatsAppSkill",
                data={"target_app": "whatsapp", "contact": contact, "message": message},
            )
        return None

    @property
    def file_skill(self):
        return None

    @property
    def clipboard_skill(self):
        return None

    @property
    def reminder_skill(self):
        return None


@pytest.fixture(autouse=True)
def reset_product_matrix_state():
    settings.reset_defaults()
    settings.set("context_detection_enabled", False)
    settings.set("context_engine_enabled", False)
    context_engine._history.clear()
    correction._corrector_instance = None
    correction.CORRECTIONS_CACHE_PATH.write_text("{}", encoding="utf-8")
    state.current_context = "unknown"
    state.current_app = "unknown"
    state.current_process_name = ""
    state.current_window_title = ""
    state.last_successful_action = ""
    state.last_target_app = ""
    state.last_replayable_command = ""
    state.last_message_target = ""
    state.last_entities = {}
    state.active_skill = ""
    state.last_skill_used = ""
    state.pending_confirmation = {}
    state.pending_confirmations = {}
    with default_permission_manager._lock:
        default_permission_manager._pending.clear()
        default_permission_manager._grants.clear()
        default_permission_manager._sync_state_locked()
    yield
    settings.reset_defaults()


def _processor(system_controller: MatrixSystemController | None = None):
    launcher = MatrixLauncher()
    system_skill = SystemSkill(controller=system_controller) if system_controller is not None else None
    skills = MatrixSkillsManager(system_skill=system_skill)
    processor = CommandProcessor(launcher=launcher, llm_client=MatrixLLM(), skills_manager=skills)
    return SimpleNamespace(processor=processor, launcher=launcher, skills=skills)


def test_voice_wake_greeting_is_personalized_and_fast():
    settings.set("user_name", "Rakesh")
    env = _processor()

    started = time.perf_counter()
    result = env.processor.process("Hi Nova", source="speech")

    assert result["success"] is True
    assert result["intent"] == "greeting"
    assert result["response"] == "Hi Rakesh, how are you?"
    assert time.perf_counter() - started < 2.0


def test_open_app_search_and_repeat_execute_real_routes():
    env = _processor()

    opened = env.processor.process("open chrome")
    searched = env.processor.process("search today's IPL result")
    repeated = env.processor.process("do the same thing")

    assert opened["success"] is True
    assert env.launcher.launched[0].lower() == "chrome"
    assert searched["success"] is True
    assert "ipl result" in env.skills.browser_controller.search_calls[0][0].lower()
    assert repeated["success"] is True
    assert len(env.skills.browser_controller.search_calls) == 2


def test_system_volume_and_mute_route_through_system_skill():
    controller = MatrixSystemController()
    env = _processor(system_controller=controller)

    volume = env.processor.process("increase volume")
    mute = env.processor.process("mute")

    assert volume["success"] is True
    assert volume["response"] == "Volume increased to 60%."
    assert mute["success"] is True
    assert [cmd.action for cmd in controller.executed] == ["volume_up", "mute"]
    assert state.last_skill_used == "SystemSkill"


def test_contact_follow_up_resolves_pronoun_for_tell_him():
    env = _processor()

    first = env.processor.process("message Hemanth")
    second = env.processor.process("tell him hi")

    assert first["success"] is True
    assert second["success"] is True
    assert second["data"]["contact"].lower() == "hemanth"
    assert second["data"]["message"] == "hi"


def test_delete_downloads_folder_requires_confirmation_with_impact_count(tmp_path):
    home = tmp_path / "home"
    downloads = home / "Downloads"
    workspace = tmp_path / "workspace"
    downloads.mkdir(parents=True)
    workspace.mkdir()
    for index in range(3):
        (downloads / f"item-{index}.txt").write_text("content", encoding="utf-8")

    known_locations = {
        "desktop": home / "Desktop",
        "documents": home / "Documents",
        "downloads": downloads,
        "pictures": home / "Pictures",
        "music": home / "Music",
        "videos": home / "Videos",
    }
    for folder in known_locations.values():
        folder.mkdir(parents=True, exist_ok=True)

    resolver = PathResolver(base_dir=workspace, user_home=home, known_locations=known_locations)
    manager = FileManager(path_resolver=resolver, trash_func=lambda _path: None)
    search = SmartFileSearch(
        file_manager=manager,
        path_resolver=resolver,
        file_index=FileIndex(db_path=tmp_path / "file_index.db"),
    )
    skill = FileSkill(file_manager=manager, smart_search=search)

    result = skill.execute("delete Downloads folder", {})

    assert result.success is False
    assert result.error == "confirmation_required"
    assert "3 items" in result.response


def test_theme_and_hotkey_settings_validate_and_persist():
    settings.set("theme", "light")
    settings.set("push_to_talk_hotkey", "Ctrl+Shift+Space")

    assert settings.get("theme") == "light"
    assert settings.get("push_to_talk_hotkey") == "Ctrl+Shift+Space"
    assert normalize_hotkey(settings.get("push_to_talk_hotkey")) == "Ctrl+Shift+Space"
