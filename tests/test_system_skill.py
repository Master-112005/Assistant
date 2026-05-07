from __future__ import annotations

import pytest

from core import settings, state
from core.launcher import LaunchResult
from core.permissions import permission_manager as default_permission_manager
from core.processor import CommandProcessor
from core.system_controls import ParsedSystemCommand, SystemActionResult
from skills.system import SystemSkill


class FakeSystemController:
    def __init__(self):
        self.volume = 50
        self.brightness = 40
        self.executed: list[ParsedSystemCommand] = []

    def capabilities(self):
        return {"volume": {"supported": True}}

    def execute(self, command: ParsedSystemCommand) -> SystemActionResult:
        self.executed.append(command)
        if command.action == "volume_up":
            self.volume += command.value or 10
            return SystemActionResult(
                success=True,
                action="volume_up",
                previous_state={"percent": self.volume - (command.value or 10)},
                current_state={"percent": self.volume, "muted": False},
                message=f"Volume increased to {self.volume}%.",
            )
        if command.action == "shutdown":
            return SystemActionResult(
                success=True,
                action="shutdown",
                previous_state={"scheduled": False},
                current_state={"scheduled": True, "delay_seconds": command.delay_seconds, "type": "shutdown"},
                message="Shutdown initiated." if not command.delay_seconds else "Shutdown scheduled in 60 seconds. Use 'cancel shutdown' to abort.",
            )
        if command.action == "restart":
            return SystemActionResult(
                success=True,
                action="restart",
                previous_state={"scheduled": False},
                current_state={"scheduled": True, "delay_seconds": command.delay_seconds, "type": "restart"},
                message="Restart initiated.",
            )
        if command.action == "set_brightness":
            self.brightness = int(command.value or self.brightness)
            return SystemActionResult(
                success=True,
                action="set_brightness",
                previous_state={"percent": 40},
                current_state={"percent": self.brightness},
                message=f"Brightness set to {self.brightness}%.",
            )
        return SystemActionResult(
            success=False,
            action=command.action,
            previous_state={},
            current_state={},
            message=f"Unsupported fake command: {command.action}",
            error="unsupported",
        )


class DummyLauncher:
    def launch_by_name(self, name: str) -> LaunchResult:
        return LaunchResult(success=True, app_name=name, matched_name=name, message=f"Opening {name.title()}")


class DummyBrowserController:
    def search(self, query: str, browser_name: str | None = None, engine: str | None = None):
        raise AssertionError("Browser search should not run in system skill tests.")


class DummyLLM:
    def is_available(self, force_refresh: bool = False) -> bool:
        return False


class SystemSkillsManager:
    def __init__(self, skill: SystemSkill):
        self.skill = skill
        self.browser_controller = DummyBrowserController()
        self.calls = []

    def load_builtin_skills(self):
        return None

    def execute_with_skill(self, context, intent: str, command: str):
        self.calls.append((context, intent, command))
        if self.skill.can_handle(context, intent, command):
            state.active_skill = self.skill.name()
            state.last_skill_used = self.skill.name()
            return self.skill.execute(command, context)
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
def reset_system_skill_state():
    settings.reset_defaults()
    state.pending_confirmation = {}
    state.pending_confirmations = {}
    state.last_system_action = {}
    state.last_volume = -1
    state.last_brightness = -1
    state.wifi_state = "unknown"
    state.bluetooth_state = "unknown"
    state.last_response = ""
    state.last_intent = ""
    state.last_entities = {}
    state.last_successful_action = ""
    state.active_skill = ""
    state.last_skill_used = ""
    with default_permission_manager._lock:
        default_permission_manager._pending.clear()
        default_permission_manager._grants.clear()
        default_permission_manager._sync_state_locked()
    yield
    settings.reset_defaults()


def test_shutdown_requires_confirmation_by_default():
    skill = SystemSkill(controller=FakeSystemController())

    result = skill.execute("shutdown pc", {"intent": "system_control", "entities": {"action": "shutdown"}})

    assert result.success is False
    assert result.error == "confirmation_required"
    assert "are you sure" in result.response.lower()
    assert state.pending_confirmation["skill"] == "system"


def test_confirmation_reply_executes_pending_shutdown():
    controller = FakeSystemController()
    skill = SystemSkill(controller=controller)

    first = skill.execute("shutdown pc", {"intent": "system_control", "entities": {"action": "shutdown"}})
    second = skill.execute("yes", {"intent": "unknown", "entities": {}})

    assert first.error == "confirmation_required"
    assert second.success is True
    assert controller.executed[-1].action == "shutdown"
    assert state.pending_confirmation == {}


def test_restart_can_execute_immediately_with_confirm_callback():
    controller = FakeSystemController()
    skill = SystemSkill(controller=controller)
    skill.set_confirm_callback(lambda prompt: "restart" in prompt.lower())

    result = skill.execute("restart pc", {"intent": "system_control", "entities": {"action": "restart"}})

    assert result.success is True
    assert controller.executed[-1].action == "restart"


def test_processor_routes_system_command_through_system_skill():
    skill = SystemSkill(controller=FakeSystemController())
    processor = CommandProcessor(
        launcher=DummyLauncher(),
        llm_client=DummyLLM(),
        skills_manager=SystemSkillsManager(skill),
    )

    result = processor.process("increase volume")

    assert result["success"] is True
    assert result["response"] == "Volume increased to 60%."
    assert state.last_skill_used == "SystemSkill"


def test_processor_returns_confirmation_prompt_for_shutdown():
    skill = SystemSkill(controller=FakeSystemController())
    processor = CommandProcessor(
        launcher=DummyLauncher(),
        llm_client=DummyLLM(),
        skills_manager=SystemSkillsManager(skill),
    )

    result = processor.process("shutdown pc")

    assert result["success"] is False
    assert result["error"] == "confirmation_required"
    assert "yes to continue" in result["response"].lower()
