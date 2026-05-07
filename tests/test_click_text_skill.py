from __future__ import annotations

import pytest

from core import settings
from core.click_text import ClickResult
from skills.click_text import ClickTextSkill


class FakeClickEngine:
    def __init__(self, result: ClickResult | None = None):
        self.result = result or ClickResult(
            success=True,
            matched_text="Login",
            clicked_x=640,
            clicked_y=360,
            match_score=0.96,
            verification_passed=True,
            message="Found Login and clicked it.",
        )
        self.calls: list[str] = []

    def click_text(self, target_text: str) -> ClickResult:
        self.calls.append(target_text)
        return self.result


@pytest.fixture(autouse=True)
def reset_click_skill_settings():
    settings.reset_defaults()
    yield
    settings.reset_defaults()


def test_click_text_skill_executes_click_command():
    engine = FakeClickEngine()
    skill = ClickTextSkill(engine=engine)

    result = skill.execute("click login", {"current_app": "chrome"})

    assert result.success is True
    assert result.intent == "click_by_text"
    assert result.response == "Found Login and clicked it."
    assert engine.calls == ["login"]
    assert result.data["click_result"]["clicked_x"] == 640


def test_click_text_skill_handles_open_settings_as_ui_action():
    skill = ClickTextSkill(engine=FakeClickEngine())

    assert skill.can_handle(
        {
            "current_app": "chrome",
            "current_window_title": "Account - Google Chrome",
            "current_process_name": "chrome.exe",
        },
        "open_app",
        "open settings",
    )


def test_click_text_skill_does_not_claim_open_chrome():
    skill = ClickTextSkill(engine=FakeClickEngine())

    assert skill.can_handle(
        {
            "current_app": "unknown",
            "current_window_title": "",
            "current_process_name": "",
        },
        "open_app",
        "open chrome",
    ) is False
