from __future__ import annotations

import pytest

from core import settings, state
from core.browser import BrowserOperationResult, BrowserState
from skills.youtube import YouTubeSkill


class FakeYouTubeController:
    def __init__(self):
        self.calls: list[tuple] = []
        self.browser_id = "chrome"
        self.browser_ready = True
        self.youtube_active = True
        self.page_title = "YouTube - Google Chrome"
        self.click_link_success = True

    def _state(self) -> BrowserState:
        return BrowserState(
            browser_id=self.browser_id,
            hwnd=201,
            process_name=f"{self.browser_id}.exe" if self.browser_id != "edge" else "msedge.exe",
            title=self.page_title,
            rect=(0, 0, 1600, 900),
            is_foreground=True,
            is_minimized=False,
            is_ready=True,
            site_context="youtube" if self.youtube_active or "youtube" in self.page_title.lower() else "",
        )

    def detect_active_browser(self):
        if not self.browser_ready:
            return None
        return self._state()

    def focus_browser(self, browser_name: str | None = None, *, launch_if_missing: bool = False):
        self.calls.append(("focus_browser", browser_name, launch_if_missing))
        if not self.browser_ready and not launch_if_missing:
            return BrowserOperationResult(
                success=False,
                action="focus_browser",
                message="No supported browser window found.",
                error="browser_not_found",
            )
        self.browser_ready = True
        return BrowserOperationResult(
            success=True,
            action="focus_browser",
            message="Browser focused.",
            browser_id=browser_name or self.browser_id,
            state=self._state(),
            verified=True,
        )

    def open_url(self, url: str, browser_name: str | None = None):
        self.calls.append(("open_url", url, browser_name))
        self.browser_ready = True
        self.youtube_active = True
        self.page_title = "YouTube - Google Chrome"
        return BrowserOperationResult(
            success=True,
            action="open_url",
            message="Opened YouTube.",
            browser_id=browser_name or self.browser_id,
            url=url,
            state=self._state(),
            verified=True,
        )

    def new_tab(self, *, browser_name: str | None = None):
        self.calls.append(("new_tab", browser_name))
        return BrowserOperationResult(
            success=True,
            action="new_tab",
            message="Opened new tab.",
            browser_id=browser_name or self.browser_id,
            state=self._state(),
            verified=True,
        )

    def search(self, query: str, *, browser_name: str | None = None, engine: str | None = None):
        self.calls.append(("search", query, browser_name, engine))
        self.youtube_active = True
        self.page_title = f"{query} - YouTube - Google Chrome"
        return BrowserOperationResult(
            success=True,
            action="search",
            message="YouTube search complete.",
            browser_id=browser_name or self.browser_id,
            query=query,
            state=self._state(),
            verified=True,
        )

    def click_link(self, index: int = 1, *, browser_name: str | None = None):
        self.calls.append(("click_link", index, browser_name))
        if not self.click_link_success:
            return BrowserOperationResult(
                success=False,
                action="click_link",
                message="Could not safely identify a result.",
                browser_id=browser_name or self.browser_id,
                error="unsafe_click_target",
                state=self._state(),
            )
        self.youtube_active = True
        self.page_title = f"Video {index} - YouTube - Google Chrome"
        return BrowserOperationResult(
            success=True,
            action="click_link",
            message="Clicked result.",
            browser_id=browser_name or self.browser_id,
            state=self._state(),
            verified=True,
            data={"method": "browser_click"},
        )

    def press_key(self, key: str, *, browser_name: str | None = None, action: str = "press_key"):
        self.calls.append(("press_key", key, browser_name, action))
        return BrowserOperationResult(
            success=True,
            action=action,
            message=f"Sent key {key}.",
            browser_id=browser_name or self.browser_id,
            state=self._state(),
            verified=True,
        )

    def hotkey(self, keys, *, browser_name: str | None = None, action: str = "hotkey"):
        key_list = tuple(keys)
        self.calls.append(("hotkey", key_list, browser_name, action))
        if key_list == ("shift", "n"):
            self.page_title = "Next video - YouTube - Google Chrome"
        elif key_list == ("shift", "p"):
            self.page_title = "Previous video - YouTube - Google Chrome"
        return BrowserOperationResult(
            success=True,
            action=action,
            message=f"Sent hotkey {'+'.join(key_list)}.",
            browser_id=browser_name or self.browser_id,
            state=self._state(),
            verified=True,
        )

    def type_text(
        self,
        text: str,
        *,
        browser_name: str | None = None,
        clear: bool = False,
        submit: bool = False,
        action: str = "type_text",
    ):
        self.calls.append(("type_text", text, browser_name, clear, submit, action))
        if submit:
            self.youtube_active = True
            self.page_title = f"{text} - YouTube - Google Chrome"
        return BrowserOperationResult(
            success=True,
            action=action,
            message="Typed text.",
            browser_id=browser_name or self.browser_id,
            state=self._state(),
            verified=True,
        )

    def click_rect(self, rect, *, browser_name: str | None = None, action: str = "click_rect"):
        self.calls.append(("click_rect", rect, browser_name, action))
        return BrowserOperationResult(
            success=True,
            action=action,
            message="Clicked rect.",
            browser_id=browser_name or self.browser_id,
            state=self._state(),
            verified=False,
        )

    def is_browser_installed(self, browser_name: str | None = None):
        return True

    def is_browser_ready(self, browser_name: str | None = None):
        return self.browser_ready


@pytest.fixture(autouse=True)
def reset_youtube_skill_state():
    settings.reset_defaults()
    state.current_context = "unknown"
    state.current_app = "unknown"
    state.current_process_name = ""
    state.current_window_title = ""
    state.last_browser = ""
    state.last_skill_used = ""
    state.last_successful_action = ""
    state.last_youtube_query = ""
    state.last_video_title = ""
    state.youtube_active = False
    state.last_media_action = ""
    yield
    settings.reset_defaults()


def test_can_handle_explicit_youtube_search_command():
    skill = YouTubeSkill(controller=FakeYouTubeController())

    assert skill.can_handle({"current_app": "unknown", "current_window_title": ""}, "search", "search lofi mix on youtube") is True


def test_can_handle_pause_when_youtube_context_is_active():
    skill = YouTubeSkill(controller=FakeYouTubeController())
    context = {
        "current_app": "youtube",
        "current_context": "youtube",
        "current_process_name": "chrome.exe",
        "current_window_title": "Lofi mix - YouTube - Google Chrome",
    }

    assert skill.can_handle(context, "unknown", "pause") is True


def test_can_handle_fullscreen_when_youtube_context_is_active():
    skill = YouTubeSkill(controller=FakeYouTubeController())
    context = {
        "current_app": "youtube",
        "current_context": "youtube",
        "current_process_name": "chrome.exe",
        "current_window_title": "Lofi mix - YouTube - Google Chrome",
    }

    assert skill.can_handle(context, "unknown", "fullscreen") is True


def test_search_video_uses_youtube_input_and_updates_runtime_state():
    controller = FakeYouTubeController()
    skill = YouTubeSkill(controller=controller)

    result = skill.execute("search lofi mix on youtube", {"intent": "search", "current_app": "unknown"})

    assert result.success is True
    assert result.response == "Searching YouTube for lofi mix"
    assert ("type_text", "lofi mix", "chrome", True, True, "youtube_search_input") in controller.calls
    assert state.last_youtube_query == "lofi mix"
    assert state.last_media_action == "search"


def test_play_query_searches_and_plays_first_result():
    controller = FakeYouTubeController()
    skill = YouTubeSkill(controller=controller)

    result = skill.execute(
        "play dulaander song",
        {"intent": "unknown", "current_app": "youtube", "current_window_title": "YouTube - Google Chrome"},
    )

    assert result.success is True
    assert result.response == "Searching YouTube for dulaander song. Playing first result"
    assert ("type_text", "dulaander song", "chrome", True, True, "youtube_search_input") in controller.calls
    assert ("click_link", 1, "chrome") in controller.calls


def test_pause_and_resume_send_k_shortcut(monkeypatch):
    controller = FakeYouTubeController()
    controller.page_title = "Dulaander - YouTube - Google Chrome"
    skill = YouTubeSkill(controller=controller)

    pause_states = iter([True, False])
    monkeypatch.setattr(skill, "_read_player_state_uia", lambda _state: next(pause_states, False))
    pause_result = skill.execute("pause", {"intent": "unknown", "current_app": "youtube"})

    resume_states = iter([False, True])
    monkeypatch.setattr(skill, "_read_player_state_uia", lambda _state: next(resume_states, True))
    resume_result = skill.execute("resume", {"intent": "unknown", "current_app": "youtube"})

    assert pause_result.success is True
    assert pause_result.response == "Paused video"
    assert resume_result.success is True
    assert resume_result.response == "Resumed video"
    assert ("press_key", "k", "chrome", "youtube_pause") in controller.calls
    assert ("press_key", "k", "chrome", "youtube_resume") in controller.calls


def test_next_video_uses_shift_n_and_verifies_title_change():
    controller = FakeYouTubeController()
    controller.page_title = "Current video - YouTube - Google Chrome"
    skill = YouTubeSkill(controller=controller)

    result = skill.execute("next video", {"intent": "unknown", "current_app": "youtube"})

    assert result.success is True
    assert result.response == "Playing next video"
    assert ("hotkey", ("shift", "n"), "chrome", "youtube_next_video") in controller.calls
    assert state.last_media_action == "next_video"


def test_volume_and_mute_use_youtube_player_keys():
    controller = FakeYouTubeController()
    skill = YouTubeSkill(controller=controller)

    up_result = skill.execute("volume up", {"intent": "unknown", "current_app": "youtube"})
    mute_result = skill.execute("mute", {"intent": "unknown", "current_app": "youtube"})

    assert up_result.success is True
    assert mute_result.success is True
    assert ("press_key", "up", "chrome", "youtube_volume_up") in controller.calls
    assert ("press_key", "m", "chrome", "youtube_mute") in controller.calls
    assert up_result.response == "Increasing YouTube volume."
    assert mute_result.response == "Muting YouTube."


def test_fullscreen_and_captions_commands_use_expected_shortcuts():
    controller = FakeYouTubeController()
    skill = YouTubeSkill(controller=controller)

    fullscreen_result = skill.execute("fullscreen", {"intent": "unknown", "current_app": "youtube"})
    captions_result = skill.execute("captions on", {"intent": "unknown", "current_app": "youtube"})

    assert fullscreen_result.success is True
    assert captions_result.success is True
    assert fullscreen_result.response == "Entering fullscreen mode."
    assert captions_result.response == "Turning captions on."
    assert ("press_key", "f", "chrome", "youtube_fullscreen_on") in controller.calls
    assert ("press_key", "c", "chrome", "youtube_captions_on") in controller.calls


def test_replay_and_seek_controls_use_player_shortcuts():
    controller = FakeYouTubeController()
    skill = YouTubeSkill(controller=controller)

    replay_result = skill.execute("replay", {"intent": "unknown", "current_app": "youtube"})
    seek_result = skill.execute("forward", {"intent": "unknown", "current_app": "youtube"})

    assert replay_result.success is True
    assert seek_result.success is True
    assert ("press_key", "0", "chrome", "youtube_replay") in controller.calls
    assert ("press_key", "l", "chrome", "youtube_seek_forward") in controller.calls


def test_read_results_falls_back_to_page_title_when_no_uia_results():
    controller = FakeYouTubeController()
    controller.page_title = "Python tutorial results - YouTube - Google Chrome"
    skill = YouTubeSkill(controller=controller)

    result = skill.execute("read results", {"intent": "unknown", "current_app": "youtube"})

    assert result.success is True
    assert result.response == "Current YouTube page title: Python tutorial results. No readable visible results were detected."


def test_open_youtube_in_chrome_is_supported_directly():
    controller = FakeYouTubeController()
    controller.youtube_active = False
    controller.page_title = "Google - Chrome"
    skill = YouTubeSkill(controller=controller)

    result = skill.execute("open youtube in chrome", {"intent": "open_website", "current_app": "unknown"})

    assert result.success is True
    assert result.response == "Opening YouTube."


def test_open_youtube_music_uses_music_url():
    controller = FakeYouTubeController()
    skill = YouTubeSkill(controller=controller)

    result = skill.execute("open youtube music", {"intent": "unknown", "current_app": "unknown"})

    assert result.success is True
    assert ("open_url", "https://music.youtube.com/", "chrome") in controller.calls
    assert result.response == "Opening YouTube Music."


def test_read_current_title_returns_actual_title():
    controller = FakeYouTubeController()
    controller.page_title = "Dulaander official video - YouTube - Google Chrome"
    skill = YouTubeSkill(controller=controller)

    result = skill.execute("read current title", {"intent": "unknown", "current_app": "youtube"})

    assert result.success is True
    assert result.response == "Current YouTube title: Dulaander official video"
    assert state.last_video_title == "Dulaander official video"


def test_youtube_closed_is_handled_honestly():
    settings.set("auto_open_youtube_if_needed", False)
    controller = FakeYouTubeController()
    controller.youtube_active = False
    controller.page_title = "Google - Chrome"
    skill = YouTubeSkill(controller=controller)

    result = skill.execute("pause", {"intent": "unknown", "current_app": "youtube"})

    assert result.success is False
    assert "YouTube is not open" in result.response
