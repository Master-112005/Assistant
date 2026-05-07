from __future__ import annotations

import pytest

from core import settings, state
from core.notifications import set_global_notification_manager
from core.skills_manager import SkillsManager
from skills.base import SkillExecutionResult, SkillBase


class DummySkill(SkillBase):
    def __init__(self, *, should_handle: bool = True):
        self.should_handle = should_handle
        self.executed = []

    def can_handle(self, context, intent, command):
        return self.should_handle

    def execute(self, command, context):
        self.executed.append((command, context))
        return SkillExecutionResult(
            success=True,
            intent="dummy_skill",
            response=f"Handled {command}",
            skill_name=self.name(),
        )

    def get_capabilities(self):
        return {"supports": ["test"]}

    def health_check(self):
        return {"ok": True}


class FakeBrowserController:
    def is_browser_installed(self, browser_name=None):
        return True

    def is_browser_ready(self, browser_name=None):
        return True

    def detect_active_browser(self):
        return None

    def focus_browser(self, browser_name=None, *, launch_if_missing=False):
        raise AssertionError("focus_browser should not be called in this test")


@pytest.fixture(autouse=True)
def reset_skill_manager_state():
    settings.reset_defaults()
    state.active_skill = ""
    state.last_skill_used = ""
    state.last_system_action = {}
    state.last_volume = -1
    state.last_brightness = -1
    state.wifi_state = "unknown"
    state.bluetooth_state = "unknown"
    state.scheduler_running = False
    state.next_reminder_time = ""
    state.last_triggered_reminder = {}
    state.reminder_count = 0
    state.notifications_ready = False
    state.notification_queue_size = 0
    state.last_notification = {}
    state.recent_notifications = []
    set_global_notification_manager(None)
    yield
    set_global_notification_manager(None)
    settings.reset_defaults()


def test_load_builtin_skills_registers_chrome_skill():
    manager = SkillsManager(browser_controller=FakeBrowserController())
    manager.load_builtin_skills()

    names = [item["name"] for item in manager.list_skills()]
    assert "AwarenessSkill" in names
    assert "ChromeSkill" in names
    assert "ClickTextSkill" in names
    assert "FileSkill" in names
    assert "MusicSkill" in names
    assert "OCRSkill" in names
    assert "ReminderSkill" in names
    assert "SystemSkill" in names
    assert "YouTubeSkill" in names
    assert "WhatsAppSkill" in names


def test_find_skill_routes_reminder_creation_to_reminder_skill():
    manager = SkillsManager(browser_controller=FakeBrowserController())
    manager.load_builtin_skills()

    skill = manager.find_skill(
        context={
            "current_app": "unknown",
            "current_process_name": "",
            "current_window_title": "",
            "context_target_app": "",
        },
        intent="unknown",
        command="remind me to call mom tomorrow at 6",
    )

    assert skill is not None
    assert skill.name() == "ReminderSkill"


def test_find_skill_routes_reminder_route_to_reminder_skill():
    manager = SkillsManager(browser_controller=FakeBrowserController())
    manager.load_builtin_skills()

    skill = manager.find_skill(
        context={
            "current_app": "unknown",
            "current_process_name": "",
            "current_window_title": "",
            "context_target_app": "",
            "target_route": "reminders",
        },
        intent="reminder_create",
        command="set timer for 5 min",
    )

    assert skill is not None
    assert skill.name() == "ReminderSkill"


def test_find_skill_routes_search_to_chrome_skill_when_preferred_browser_is_chrome():
    manager = SkillsManager(browser_controller=FakeBrowserController())
    manager.load_builtin_skills()

    skill = manager.find_skill(
        context={
            "current_app": "unknown",
            "current_process_name": "",
            "context_target_app": "",
        },
        intent="search",
        command="search IPL score",
    )

    assert skill is not None
    assert skill.name() == "ChromeSkill"


def test_find_skill_routes_explicit_youtube_command_to_youtube_skill():
    manager = SkillsManager(browser_controller=FakeBrowserController())
    manager.load_builtin_skills()

    skill = manager.find_skill(
        context={
            "current_app": "unknown",
            "current_process_name": "",
            "current_window_title": "",
            "context_target_app": "",
        },
        intent="search",
        command="search lofi mix on youtube",
    )

    assert skill is not None
    assert skill.name() == "YouTubeSkill"


def test_find_skill_routes_whatsapp_call_command_to_whatsapp_skill():
    manager = SkillsManager(browser_controller=FakeBrowserController())
    manager.load_builtin_skills()

    skill = manager.find_skill(
        context={
            "current_app": "unknown",
            "current_process_name": "",
            "current_window_title": "",
            "context_target_app": "",
        },
        intent="unknown",
        command="call hemanth",
    )

    assert skill is not None
    assert skill.name() == "WhatsAppSkill"


def test_find_skill_routes_play_liked_songs_to_music_skill():
    manager = SkillsManager(browser_controller=FakeBrowserController())
    manager.load_builtin_skills()

    skill = manager.find_skill(
        context={
            "current_app": "unknown",
            "current_process_name": "",
            "current_window_title": "",
            "context_target_app": "",
        },
        intent="unknown",
        command="play liked songs",
    )

    assert skill is not None
    assert skill.name() == "MusicSkill"


def test_find_skill_routes_read_screen_to_ocr_skill():
    manager = SkillsManager(browser_controller=FakeBrowserController())
    manager.load_builtin_skills()

    skill = manager.find_skill(
        context={
            "current_app": "unknown",
            "current_process_name": "",
            "current_window_title": "",
            "context_target_app": "",
        },
        intent="unknown",
        command="read screen",
    )

    assert skill is not None
    assert skill.name() == "OCRSkill"


def test_find_skill_routes_click_login_to_click_text_skill():
    manager = SkillsManager(browser_controller=FakeBrowserController())
    manager.load_builtin_skills()

    skill = manager.find_skill(
        context={
            "current_app": "chrome",
            "current_process_name": "chrome.exe",
            "current_window_title": "Login - Google Chrome",
            "context_target_app": "",
        },
        intent="unknown",
        command="click login",
    )

    assert skill is not None
    assert skill.name() == "ClickTextSkill"


def test_find_skill_does_not_route_open_chrome_to_click_text_skill():
    manager = SkillsManager(browser_controller=FakeBrowserController())
    manager.load_builtin_skills()

    skill = manager.find_skill(
        context={
            "current_app": "unknown",
            "current_process_name": "",
            "current_window_title": "",
            "context_target_app": "",
        },
        intent="open_app",
        command="open chrome",
    )

    assert skill is None or skill.name() != "ClickTextSkill"


def test_find_skill_routes_screen_summary_to_awareness_skill():
    manager = SkillsManager(browser_controller=FakeBrowserController())
    manager.load_builtin_skills()

    skill = manager.find_skill(
        context={
            "current_app": "unknown",
            "current_process_name": "",
            "current_window_title": "",
            "context_target_app": "",
        },
        intent="unknown",
        command="what is on screen",
    )

    assert skill is not None
    assert skill.name() == "AwarenessSkill"


def test_find_skill_routes_search_song_to_music_skill():
    manager = SkillsManager(browser_controller=FakeBrowserController())
    manager.load_builtin_skills()

    skill = manager.find_skill(
        context={
            "current_app": "unknown",
            "current_process_name": "",
            "current_window_title": "",
            "context_target_app": "",
        },
        intent="search",
        command="search song shape of you",
    )

    assert skill is not None
    assert skill.name() == "MusicSkill"


def test_execute_with_skill_updates_runtime_state():
    manager = SkillsManager(browser_controller=FakeBrowserController())
    dummy = DummySkill()
    manager.register(dummy)

    result = manager.execute_with_skill(
        context={"current_app": "unknown", "current_process_name": ""},
        intent="unknown",
        command="dummy command",
    )

    assert result is not None
    assert result.success is True
    assert state.active_skill == "DummySkill"
    assert state.last_skill_used == "DummySkill"


def test_execute_with_skill_returns_none_when_no_skill_matches():
    manager = SkillsManager(browser_controller=FakeBrowserController())
    manager.register(DummySkill(should_handle=False))

    result = manager.execute_with_skill(
        context={"current_app": "unknown", "current_process_name": ""},
        intent="unknown",
        command="no match",
    )

    assert result is None
    assert state.active_skill == ""
