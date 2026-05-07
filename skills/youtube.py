"""
Dedicated YouTube skill/plugin.

This module serves two roles:

1. ``SkillBase`` plugin used by the processor/skills manager for direct routing.
2. Planner/executor helper used by the context engine and execution engine.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.action_results import ActionResult
from core import settings, state
from core.browser import BrowserController, BrowserState
from core.logger import get_logger
from skills.base import SkillBase, SkillExecutionResult
from skills.browser import BrowserSkill

logger = get_logger(__name__)

try:  # pragma: no cover - optional dependency
    from pywinauto import Desktop

    _PYWINAUTO_OK = True
except Exception:  # pragma: no cover - optional dependency
    Desktop = None
    _PYWINAUTO_OK = False


_YOUTUBE_HOME_URL = "https://www.youtube.com/"
_YOUTUBE_BROWSER_IDS = {"chrome", "edge", "firefox", "brave"}
_YOUTUBE_PROCESS_NAMES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"}
_SEARCH_PREFIXES = ("search for ", "search ", "find ")
_AUTOPLAY_PREFIXES = ("play ", "watch ", "listen to ", "listen ")
_PAUSE_COMMANDS = {"pause", "pause video", "pause youtube", "pause playback"}
_RESUME_COMMANDS = {"resume", "resume video", "resume youtube", "resume playback"}
_TOGGLE_PLAY_COMMANDS = {"toggle play", "toggle playback", "play", "play video"}
_NEXT_COMMANDS = {"next", "next video", "skip"}
_PREVIOUS_COMMANDS = {"previous", "previous video", "go previous"}
_VOLUME_UP_COMMANDS = {"volume up", "increase volume", "raise volume", "youtube volume up"}
_VOLUME_DOWN_COMMANDS = {"volume down", "decrease volume", "lower volume", "youtube volume down"}
_MUTE_COMMANDS = {"mute", "mute youtube", "mute video"}
_UNMUTE_COMMANDS = {"unmute", "unmute youtube", "unmute video"}
_FULLSCREEN_ON_COMMANDS = {"fullscreen", "full screen", "enter fullscreen", "enter full screen", "go fullscreen"}
_FULLSCREEN_OFF_COMMANDS = {"exit fullscreen", "exit full screen", "leave fullscreen", "leave full screen"}
_THEATER_MODE_COMMANDS = {"theater mode", "theatre mode", "toggle theater mode", "toggle theatre mode"}
_CAPTIONS_ON_COMMANDS = {"captions on", "turn on captions", "subtitles on", "turn on subtitles"}
_CAPTIONS_OFF_COMMANDS = {"captions off", "turn off captions", "subtitles off", "turn off subtitles"}
_MINI_PLAYER_COMMANDS = {"mini player", "open mini player", "start mini player"}
_REPLAY_COMMANDS = {"replay", "restart video", "restart youtube video", "play from beginning"}
_SEEK_FORWARD_COMMANDS = {"seek forward", "forward", "skip forward", "fast forward"}
_SEEK_BACKWARD_COMMANDS = {"seek backward", "rewind", "skip backward", "backward"}
_READ_TITLE_COMMANDS = {
    "read current title",
    "read title",
    "read current video title",
    "what is playing",
    "what's playing",
    "current title",
    "current video title",
}
_READ_RESULTS_PREFIXES = (
    "read results",
    "read page",
    "summarize page",
    "summarize this page",
    "what is on screen",
    "what's on screen",
    "what is on page",
    "what's on page",
    "read first result",
    "read second result",
    "read third result",
    "read fourth result",
    "read fifth result",
)
_OPEN_COMMANDS = {
    "open youtube",
    "start youtube",
    "launch youtube",
    "youtube",
    "open you tube",
    "start you tube",
    "open youtube website",
    "launch youtube website",
    "open youtube in chrome",
    "launch youtube in chrome",
}
_OPEN_MUSIC_COMMANDS = {
    "open youtube music",
    "launch youtube music",
    "start youtube music",
}
_RESULT_WORDS = {"result", "video", "song", "track"}
_ORDINALS = {
    "first": 1,
    "1": 1,
    "1st": 1,
    "one": 1,
    "second": 2,
    "2": 2,
    "2nd": 2,
    "two": 2,
    "third": 3,
    "3": 3,
    "3rd": 3,
    "three": 3,
    "fourth": 4,
    "4": 4,
    "4th": 4,
    "four": 4,
    "fifth": 5,
    "5": 5,
    "5th": 5,
    "five": 5,
}
_YOUTUBE_NOISE_PHRASES = {
    "youtube",
    "search",
    "search with your voice",
    "home",
    "shorts",
    "subscriptions",
    "you",
    "history",
    "playlists",
    "your videos",
    "watch later",
    "liked videos",
    "downloads",
    "trending",
    "shopping",
    "music",
    "movies",
    "live",
    "gaming",
    "news",
    "sports",
    "courses",
    "fashion and beauty",
    "podcasts",
    "filters",
    "all",
    "upload date",
    "type",
    "duration",
    "features",
    "sort by",
    "search with your voice",
}
_YOUTUBE_MUSIC_URL = "https://music.youtube.com/"


@dataclass
class YouTubeActionResult:
    success: bool
    operation: str
    message: str
    error: str = ""
    target: str | None = "youtube"
    verified: bool = False
    duration_ms: int = 0
    data: dict[str, Any] = field(default_factory=dict)


class YouTubeSkill(SkillBase):
    """YouTube-specific helpers and direct command skill."""

    def __init__(
        self,
        *,
        browser: BrowserSkill | None = None,
        controller: BrowserController | None = None,
    ) -> None:
        resolved_controller = controller or getattr(browser, "_controller", None) or BrowserController()
        self._controller = resolved_controller
        self._browser = browser or BrowserSkill(controller=resolved_controller)

    def can_handle(self, context: Mapping[str, Any], intent: str, command: str) -> bool:
        if not settings.get("youtube_skill_enabled"):
            return False

        normalized = self._normalize_command(command)
        if not normalized:
            return False

        explicit_youtube = self._mentions_youtube(command)
        youtube_context = self._is_youtube_context(context)
        recent_youtube = self._has_recent_youtube_context(context)

        if (
            normalized in _PAUSE_COMMANDS
            or normalized in _RESUME_COMMANDS
            or normalized in _TOGGLE_PLAY_COMMANDS
            or normalized in _NEXT_COMMANDS
            or normalized in _PREVIOUS_COMMANDS
            or normalized in _VOLUME_UP_COMMANDS
            or normalized in _VOLUME_DOWN_COMMANDS
            or normalized in _MUTE_COMMANDS
            or normalized in _UNMUTE_COMMANDS
            or normalized in _FULLSCREEN_ON_COMMANDS
            or normalized in _FULLSCREEN_OFF_COMMANDS
            or normalized in _THEATER_MODE_COMMANDS
            or normalized in _CAPTIONS_ON_COMMANDS
            or normalized in _CAPTIONS_OFF_COMMANDS
            or normalized in _MINI_PLAYER_COMMANDS
            or normalized in _REPLAY_COMMANDS
            or normalized in _SEEK_FORWARD_COMMANDS
            or normalized in _SEEK_BACKWARD_COMMANDS
            or normalized in _READ_TITLE_COMMANDS
            or any(normalized.startswith(prefix) for prefix in _READ_RESULTS_PREFIXES)
            or self._is_result_selection_command(normalized)
        ):
            return explicit_youtube or youtube_context or recent_youtube

        if normalized in _OPEN_COMMANDS or normalized in _OPEN_MUSIC_COMMANDS:
            return True

        if self._is_query_command(normalized, intent):
            return explicit_youtube or youtube_context or self._context_targets_youtube(context)

        return False

    def execute(
        self,
        command: str,
        context: Mapping[str, Any] | None = None,
        **params: Any,
    ) -> SkillExecutionResult | YouTubeActionResult:
        if context is not None:
            return self._execute_skill_command(command, context)
        return self.execute_operation(command, **params)

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "target": "youtube",
            "supports": [
                "search_video",
                "play_first_result",
                "play_result",
                "pause",
                "resume",
                "toggle_play",
                "next_video",
                "previous_video",
                "volume_up",
                "volume_down",
                "mute",
                "unmute",
                "fullscreen_on",
                "fullscreen_off",
                "theater_mode",
                "captions_on",
                "captions_off",
                "mini_player",
                "replay",
                "seek_forward",
                "seek_backward",
                "read_current_title",
                "read_results",
            ],
            "preferred_browser": self._preferred_browser_name(),
            "prefer_keyboard_shortcuts": bool(settings.get("youtube_prefer_keyboard_shortcuts")),
        }

    def health_check(self) -> dict[str, Any]:
        preferred_browser = self._preferred_browser_name()
        active = self.detect_youtube_context()
        return {
            "enabled": bool(settings.get("youtube_skill_enabled")),
            "preferred_browser": preferred_browser,
            "browser_installed": self._controller.is_browser_installed(preferred_browser),
            "browser_ready": self._controller.is_browser_ready(preferred_browser),
            "youtube_active": active is not None,
            "auto_open": bool(settings.get("auto_open_youtube_if_needed")),
        }

    def describe_window_title(self, title: str, *, is_playing: bool | None = None) -> dict[str, Any]:
        cleaned = self._extract_youtube_title(title)
        lowered = cleaned.lower()
        if cleaned and cleaned.lower() not in {"youtube", "home"}:
            if is_playing is True:
                return {
                    "summary": f"YouTube is playing {cleaned}.",
                    "confidence": 0.93,
                    "details": {"page_title": cleaned, "playing": True, "source": "window_title"},
                }
            if "result" in lowered or "search" in lowered:
                return {
                    "summary": f"YouTube is showing {cleaned}.",
                    "confidence": 0.84,
                    "details": {"page_title": cleaned, "playing": is_playing, "source": "window_title"},
                }
            return {
                "summary": f"YouTube is open with {cleaned}.",
                "confidence": 0.79 if is_playing is None else 0.86,
                "details": {"page_title": cleaned, "playing": is_playing, "source": "window_title"},
            }

        return {
            "summary": "YouTube is active.",
            "confidence": 0.60,
            "details": {"page_title": cleaned, "playing": is_playing, "source": "window_title"},
        }

    def build_search_steps(
        self,
        query: str,
        *,
        autoplay: bool = False,
        result_index: int = 1,
    ) -> list[dict[str, Any]]:
        steps = self._browser.build_search_steps(query, target_app="youtube", engine="youtube")
        if autoplay:
            steps.append(self.build_select_step(result_index, autoplay=True))
        return steps

    def build_control_step(self, control: str) -> dict[str, Any]:
        return {
            "action": "app_action",
            "target": "youtube",
            "params": {"operation": "control", "control": control},
            "estimated_risk": "low",
        }

    def build_select_step(self, result_index: int, *, autoplay: bool = False) -> dict[str, Any]:
        return {
            "action": "app_action",
            "target": "youtube",
            "params": {
                "operation": "open_result",
                "result_index": result_index,
                "autoplay": autoplay,
            },
            "estimated_risk": "low",
        }

    def execute_operation(self, operation: str, **params: Any) -> YouTubeActionResult:
        op = self._normalize_command(operation)

        if op == "search":
            query = str(params.get("query", "")).strip()
            autoplay = bool(params.get("autoplay"))
            result_index = max(1, int(params.get("result_index", 1) or 1))
            return self._to_action_result(self.search_video(query, autoplay=autoplay, result_index=result_index), op)

        if op == "open_result":
            result_index = max(1, int(params.get("result_index", 1) or 1))
            return self._to_action_result(self.play_result(result_index), op)

        if op == "control":
            control = self._normalize_command(str(params.get("control", "")).replace("_", " "))
            return self._to_action_result(self._execute_control(control), op or "control")

        if op == "read_title":
            return self._to_action_result(self.read_current_title(), op)
        if op == "read_results":
            result_index = int(params.get("result_index", 0) or 0) or None
            return self._to_action_result(self.read_results(result_index=result_index), op)
        if op in ("open", "open_youtube") or op in _OPEN_COMMANDS:
            return self._to_action_result(self.open_youtube(), op)
        if op in ("open_music", "open_youtube_music") or op in _OPEN_MUSIC_COMMANDS:
            return self._to_action_result(self.open_youtube_music(), op)

        return YouTubeActionResult(
            success=False,
            operation=op or "unknown",
            error="unsupported_youtube_operation",
            message=f"Unsupported YouTube operation: {operation or 'unknown'}.",
        )

    def detect_youtube_context(self) -> BrowserState | None:
        active = self._controller.detect_active_browser()
        if active and self._is_youtube_browser_state(active):
            state.youtube_active = True
            return active
        state.youtube_active = False
        return None

    def focus_youtube(self) -> BrowserState | SkillExecutionResult:
        detected = self.detect_youtube_context()
        if detected is not None:
            self._update_runtime_state(detected, action="focus_youtube")
            return detected

        browser_name = self._preferred_browser_name()
        auto_open = bool(settings.get("auto_open_youtube_if_needed"))
        browser_ready_before = self._controller.is_browser_ready(browser_name)
        focus_result = self._controller.focus_browser(browser_name, launch_if_missing=auto_open)
        if not focus_result.success or not focus_result.state:
            state.youtube_active = False
            return self._failure(focus_result.message, focus_result.error or "browser_not_ready")

        if self._is_youtube_browser_state(focus_result.state):
            self._update_runtime_state(focus_result.state, action="focus_youtube")
            return focus_result.state

        if not auto_open:
            state.youtube_active = False
            return self._failure("YouTube is not open.", "youtube_not_open")

        if browser_ready_before:
            self._controller.new_tab(browser_name=browser_name)

        open_result = self._controller.open_url(_YOUTUBE_HOME_URL, browser_name=browser_name)
        if not open_result.success or not open_result.state:
            state.youtube_active = False
            return self._failure(open_result.message, open_result.error or "youtube_open_failed")

        current_state, verified = self._wait_for_title_change(
            focus_result.state.title,
            browser_name=browser_name,
            timeout_ms=800,
        )
        final_state = current_state or open_result.state
        verified = bool(verified or open_result.verified or self._is_youtube_browser_state(final_state))
        if not verified or not self._is_youtube_browser_state(final_state):
            state.youtube_active = False
            return self._failure("I couldn't verify that YouTube opened.", "youtube_open_not_verified")
        self._update_runtime_state(final_state, action="focus_youtube")
        return final_state

    def ensure_youtube_open(self) -> SkillExecutionResult | None:
        focused = self.focus_youtube()
        if isinstance(focused, SkillExecutionResult):
            return focused
        return None

    def search_video(
        self,
        query: str,
        *,
        autoplay: bool = False,
        result_index: int = 1,
    ) -> SkillExecutionResult:
        cleaned_query = str(query or "").strip()
        if not cleaned_query:
            return self._failure("Search query is empty.", "empty_query")

        logger.info("Search: %s", cleaned_query)
        focused = self.focus_youtube()
        if isinstance(focused, SkillExecutionResult):
            return focused

        browser_state = focused
        current_state, source, verified = self._perform_search(browser_state, cleaned_query)
        if current_state is None:
            return self._failure("YouTube search could not be completed.", "youtube_search_failed")

        self._update_runtime_state(current_state, action="search")
        state.last_youtube_query = cleaned_query
        state.last_media_action = "search"

        if autoplay:
            play_result = self.play_result(result_index)
            if not play_result.success:
                return SkillExecutionResult(
                    success=False,
                    intent="youtube_action",
                    response=f"Searching YouTube for {cleaned_query}. {play_result.response}",
                    skill_name=self.name(),
                    error=play_result.error,
                    data={
                        "target_app": "youtube",
                        "search_source": source,
                        "search_verified": verified,
                        "result_index": result_index,
                    },
                )
            combined = f"Searching YouTube for {cleaned_query}. {play_result.response}"
            combined_data = dict(play_result.data)
            combined_data.update(
                {
                    "target_app": "youtube",
                    "search_source": source,
                    "search_verified": verified,
                    "query": cleaned_query,
                }
            )
            return SkillExecutionResult(
                success=True,
                intent="youtube_action",
                response=combined,
                skill_name=self.name(),
                data=combined_data,
            )

        return SkillExecutionResult(
            success=True,
            intent="youtube_action",
            response=f"Searching YouTube for {cleaned_query}",
            skill_name=self.name(),
            data={
                "target_app": "youtube",
                "query": cleaned_query,
                "search_source": source,
                "verified": verified,
                "page_title": self._extract_youtube_title(current_state.title),
            },
        )

    def play_first_result(self) -> SkillExecutionResult:
        return self.play_result(1)

    def play_result(self, index: int) -> SkillExecutionResult:
        result_index = max(1, int(index or 1))
        focused = self.focus_youtube()
        if isinstance(focused, SkillExecutionResult):
            return focused

        browser_state = focused
        visible_items, _source = self._read_visible_results(browser_state)
        if visible_items and result_index > len(visible_items):
            return self._failure(
                f"I could only detect {len(visible_items)} visible YouTube result(s).",
                "youtube_result_index_out_of_range",
            )
        before_title = browser_state.title
        method = ""
        current_state: BrowserState | None = None
        verified = False

        if self._click_video_result_uia(browser_state, result_index):
            method = "youtube_uia"
            current_state, verified = self._wait_for_title_change(before_title, browser_name=browser_state.browser_id)
        else:
            click_result = self._controller.click_link(result_index, browser_name=browser_state.browser_id)
            if click_result.success:
                method = str(click_result.data.get("method") or "browser_click")
                current_state = click_result.state
                verified = bool(click_result.verified)
            elif not settings.get("safe_mode_clicks") and self._click_result_heuristic(browser_state, result_index):
                method = "heuristic"
                current_state, verified = self._wait_for_title_change(before_title, browser_name=browser_state.browser_id)

        if current_state is None:
            current_state = self.detect_youtube_context()

        if current_state is None:
            return self._failure("No YouTube result could be activated.", "youtube_result_not_opened")

        if not verified and current_state.title == before_title:
            return self._failure(
                "A YouTube result click was attempted, but playback could not be verified.",
                "youtube_result_not_verified",
            )

        self._update_runtime_state(current_state, action="play_result")
        state.last_media_action = "play_result"
        title = self._extract_youtube_title(current_state.title)
        response = "Playing first result" if result_index == 1 else f"Playing result {result_index}"
        if title:
            state.last_video_title = title

        logger.info("Result played: %s (%s)", result_index, method or "unknown")
        return SkillExecutionResult(
            success=True,
            intent="youtube_action",
            response=response,
            skill_name=self.name(),
            data={
                "target_app": "youtube",
                "result_index": result_index,
                "verified": verified,
                "method": method or "unknown",
                "page_title": title,
            },
        )

    def pause(self) -> SkillExecutionResult:
        return self._toggle_playback("pause", response="Paused video", desired_playing=False)

    def resume(self) -> SkillExecutionResult:
        return self._toggle_playback("resume", response="Resumed video", desired_playing=True)

    def toggle_play(self) -> SkillExecutionResult:
        return self._toggle_playback("toggle_play", response="Toggled YouTube playback")

    def next_video(self) -> SkillExecutionResult:
        return self._navigate_video("next_video", ["shift", "n"], "Playing next video")

    def previous_video(self) -> SkillExecutionResult:
        return self._navigate_video("previous_video", ["shift", "p"], "Playing previous video")

    def volume_up(self) -> SkillExecutionResult:
        return self._player_key_action("volume_up", "up", "Increasing YouTube volume.")

    def volume_down(self) -> SkillExecutionResult:
        return self._player_key_action("volume_down", "down", "Lowering YouTube volume.")

    def mute(self) -> SkillExecutionResult:
        return self._player_toggle_action(
            "mute",
            "m",
            "Muting YouTube.",
            state_reader=self._read_muted_state_uia,
            desired_state=True,
            already_response="YouTube is already muted.",
        )

    def unmute(self) -> SkillExecutionResult:
        return self._player_toggle_action(
            "unmute",
            "m",
            "Unmuting YouTube.",
            state_reader=self._read_muted_state_uia,
            desired_state=False,
            already_response="YouTube is already unmuted.",
        )

    def enter_fullscreen(self) -> SkillExecutionResult:
        return self._player_toggle_action(
            "fullscreen_on",
            "f",
            "Entering fullscreen mode.",
            state_reader=self._read_fullscreen_state_uia,
            desired_state=True,
            already_response="YouTube is already in fullscreen mode.",
        )

    def exit_fullscreen(self) -> SkillExecutionResult:
        return self._player_toggle_action(
            "fullscreen_off",
            "esc",
            "Exiting fullscreen mode.",
            state_reader=self._read_fullscreen_state_uia,
            desired_state=False,
            already_response="YouTube is already out of fullscreen mode.",
            prepare_shortcuts=False,
        )

    def toggle_theater_mode(self) -> SkillExecutionResult:
        return self._player_key_action("theater_mode", "t", "Switching YouTube theater mode.")

    def captions_on(self) -> SkillExecutionResult:
        return self._player_toggle_action(
            "captions_on",
            "c",
            "Turning captions on.",
            state_reader=self._read_captions_state_uia,
            desired_state=True,
            already_response="Captions are already on.",
        )

    def captions_off(self) -> SkillExecutionResult:
        return self._player_toggle_action(
            "captions_off",
            "c",
            "Turning captions off.",
            state_reader=self._read_captions_state_uia,
            desired_state=False,
            already_response="Captions are already off.",
        )

    def open_mini_player(self) -> SkillExecutionResult:
        return self._player_key_action("mini_player", "i", "Opening mini player.")

    def replay(self) -> SkillExecutionResult:
        return self._player_key_action("replay", "0", "Replaying the current video.")

    def seek_forward(self) -> SkillExecutionResult:
        return self._player_key_action("seek_forward", "l", "Seeking forward.")

    def seek_backward(self) -> SkillExecutionResult:
        return self._player_key_action("seek_backward", "j", "Seeking backward.")

    def read_current_title(self) -> SkillExecutionResult:
        focused = self.focus_youtube()
        if isinstance(focused, SkillExecutionResult):
            return focused

        browser_state = focused
        title = self._extract_youtube_title(browser_state.title)
        source = "window_title"

        if not title:
            title = self._read_title_uia(browser_state)
            source = "uia" if title else "none"

        if not title:
            return self._failure("YouTube is open, but the current title could not be detected.", "title_unavailable")

        state.last_video_title = title
        state.last_media_action = "read_title"
        state.youtube_active = True
        logger.info("Read results source used: %s", source)
        return SkillExecutionResult(
            success=True,
            intent="youtube_read",
            response=f"Current YouTube title: {title}",
            skill_name=self.name(),
            data={"target_app": "youtube", "source": source, "page_title": title},
        )

    def read_results(self, *, result_index: int | None = None) -> SkillExecutionResult:
        focused = self.focus_youtube()
        if isinstance(focused, SkillExecutionResult):
            return focused

        browser_state = focused
        title = self._extract_youtube_title(browser_state.title)
        items, source = self._read_visible_results(browser_state)
        state.last_media_action = "read_results"
        if title:
            state.last_video_title = title

        if items:
            if result_index:
                if result_index > len(items):
                    return self._failure(
                        f"I could only detect {len(items)} visible YouTube result(s).",
                        "youtube_result_index_out_of_range",
                    )
                item = items[result_index - 1]
                return SkillExecutionResult(
                    success=True,
                    intent="youtube_read",
                    response=f"Visible YouTube result {result_index}: {item}",
                    skill_name=self.name(),
                    data={
                        "target_app": "youtube",
                        "source": source,
                        "result_index": result_index,
                        "page_title": title,
                        "items": items,
                    },
                )

            joined = " | ".join(items[:3])
            return SkillExecutionResult(
                success=True,
                intent="youtube_read",
                response=f"Visible YouTube results: {joined}",
                skill_name=self.name(),
                data={"target_app": "youtube", "source": source, "page_title": title, "items": items},
            )

        if title:
            return SkillExecutionResult(
                success=True,
                intent="youtube_read",
                response=f"Current YouTube page title: {title}. No readable visible results were detected.",
                skill_name=self.name(),
                data={"target_app": "youtube", "source": "window_title", "page_title": title, "items": []},
            )

        return self._failure("YouTube is open, but no readable page content was detected.", "youtube_read_results_unavailable")

    def open_youtube(self) -> SkillExecutionResult:
        action_started = time.perf_counter()
        focused = self.detect_youtube_context()
        already_active = focused is not None
        if already_active:
            focused_result = self.focus_youtube()
            if isinstance(focused_result, SkillExecutionResult):
                return focused_result
            focused = focused_result
        else:
            browser_name = self._preferred_browser_name()
            open_result = self._controller.open_url(_YOUTUBE_HOME_URL, browser_name=browser_name)
            if not open_result.success:
                return self._failure(open_result.message, open_result.error or "youtube_open_failed")
            focused = open_result.state or self.detect_youtube_context()
            if focused is not None:
                self._update_runtime_state(focused, action="open_youtube")
            else:
                state.youtube_active = True

        state.last_media_action = "open_youtube"
        return SkillExecutionResult.from_action_result(
            intent="youtube_action",
            response="YouTube is already open. Bringing it to front." if already_active else "Opening YouTube.",
            skill_name=self.name(),
            action_result=ActionResult(
                success=True,
                action="open_youtube",
                target="youtube",
                message="YouTube opened successfully.",
                data={"target_app": "youtube", "page_title": self._extract_youtube_title(getattr(focused, "title", ""))},
                verified=focused is not None,
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            ),
        )

    def open_youtube_music(self) -> SkillExecutionResult:
        action_started = time.perf_counter()
        browser_name = self._preferred_browser_name()
        result = self._controller.open_url(_YOUTUBE_MUSIC_URL, browser_name=browser_name)
        if not result.success:
            return self._failure(result.message, result.error or "youtube_music_open_failed")

        final_state = result.state or self.detect_youtube_context()
        if final_state is not None:
            self._update_runtime_state(final_state, action="open_youtube_music")
        else:
            state.youtube_active = True
        state.last_media_action = "open_youtube_music"
        return SkillExecutionResult.from_action_result(
            intent="youtube_action",
            response="Opening YouTube Music.",
            skill_name=self.name(),
            action_result=ActionResult(
                success=True,
                action="open_youtube_music",
                target="youtube",
                message="YouTube Music opened successfully.",
                data={"target_app": "youtube", "page_title": self._extract_youtube_title(getattr(final_state, "title", ""))},
                verified=bool(result.verified or final_state is not None),
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            ),
        )

    def is_playing(self) -> bool | None:
        active = self.detect_youtube_context()
        if active is None:
            return None
        return self._read_player_state_uia(active)

    def _execute_skill_command(self, command: str, context: Mapping[str, Any]) -> SkillExecutionResult:
        normalized = self._normalize_command(command)
        cleaned_command = self._strip_explicit_youtube_tokens(command)
        action, payload = self._classify_command(cleaned_command, normalized, context, raw_command=command)

        logger.info("Action: %s", action)
        if payload:
            logger.info("Command payload: %s", payload)

        if action == "search_video":
            return self.search_video(
                str(payload.get("query") or "").strip(),
                autoplay=bool(payload.get("autoplay")),
                result_index=max(1, int(payload.get("result_index", 1) or 1)),
            )
        if action == "play_result":
            return self.play_result(int(payload.get("result_index", 1) or 1))
        if action == "pause":
            return self.pause()
        if action == "resume":
            return self.resume()
        if action == "toggle_play":
            return self.toggle_play()
        if action == "next_video":
            return self.next_video()
        if action == "previous_video":
            return self.previous_video()
        if action == "volume_up":
            return self.volume_up()
        if action == "volume_down":
            return self.volume_down()
        if action == "mute":
            return self.mute()
        if action == "unmute":
            return self.unmute()
        if action == "fullscreen_on":
            return self.enter_fullscreen()
        if action == "fullscreen_off":
            return self.exit_fullscreen()
        if action == "theater_mode":
            return self.toggle_theater_mode()
        if action == "captions_on":
            return self.captions_on()
        if action == "captions_off":
            return self.captions_off()
        if action == "mini_player":
            return self.open_mini_player()
        if action == "replay":
            return self.replay()
        if action == "seek_forward":
            return self.seek_forward()
        if action == "seek_backward":
            return self.seek_backward()
        if action == "read_title":
            return self.read_current_title()
        if action == "read_results":
            result_index = int(payload.get("result_index", 0) or 0) or None
            return self.read_results(result_index=result_index)
        if action == "open_youtube":
            return self.open_youtube()
        if action == "open_youtube_music":
            return self.open_youtube_music()

        return self._failure(f"YouTubeSkill cannot handle: {command}", "unsupported_youtube_command")

    def _execute_control(self, control: str) -> SkillExecutionResult:
        normalized = self._normalize_command(control)
        if normalized in {"pause"}:
            return self.pause()
        if normalized in {"resume"}:
            return self.resume()
        if normalized in {"play", "toggle play", "toggle playback"}:
            return self.toggle_play()
        if normalized in {"next", "next video"}:
            return self.next_video()
        if normalized in {"previous", "previous video"}:
            return self.previous_video()
        if normalized in {"volume up", "increase volume"}:
            return self.volume_up()
        if normalized in {"volume down", "decrease volume"}:
            return self.volume_down()
        if normalized == "mute":
            return self.mute()
        if normalized == "unmute":
            return self.unmute()
        if normalized in {"fullscreen", "full screen", "enter fullscreen"}:
            return self.enter_fullscreen()
        if normalized in {"exit fullscreen", "exit full screen", "leave fullscreen"}:
            return self.exit_fullscreen()
        if normalized in {"theater mode", "theatre mode", "toggle theater mode", "toggle theatre mode"}:
            return self.toggle_theater_mode()
        if normalized in {"captions on", "turn on captions", "subtitles on", "turn on subtitles"}:
            return self.captions_on()
        if normalized in {"captions off", "turn off captions", "subtitles off", "turn off subtitles"}:
            return self.captions_off()
        if normalized in {"mini player", "open mini player"}:
            return self.open_mini_player()
        if normalized in {"replay", "restart video", "play from beginning"}:
            return self.replay()
        if normalized in {"seek forward", "forward", "skip forward", "fast forward"}:
            return self.seek_forward()
        if normalized in {"seek backward", "rewind", "skip backward", "backward"}:
            return self.seek_backward()
        if normalized in {"read title", "current title"}:
            return self.read_current_title()
        if any(normalized.startswith(prefix) for prefix in _READ_RESULTS_PREFIXES):
            return self.read_results(result_index=self._extract_ordinal(normalized))
        if normalized in _OPEN_COMMANDS:
            return self.open_youtube()
        if normalized in _OPEN_MUSIC_COMMANDS:
            return self.open_youtube_music()
        return self._failure(f"Unsupported YouTube control: {control or 'unknown'}.", "unsupported_control")

    def _toggle_playback(
        self,
        action: str,
        *,
        response: str,
        desired_playing: bool | None = None,
    ) -> SkillExecutionResult:
        focused = self.focus_youtube()
        if isinstance(focused, SkillExecutionResult):
            return focused

        browser_state = focused
        before_state = self._read_player_state_uia(browser_state)
        self._prepare_player_shortcuts(browser_state)

        key_result = self._controller.press_key("k", browser_name=browser_state.browser_id, action=f"youtube_{action}")
        if not key_result.success or not key_result.state:
            return self._failure(key_result.message, key_result.error or "playback_key_failed")

        after_state = self._wait_for_play_state(before_state, browser_name=browser_state.browser_id)
        final_state = self.detect_youtube_context() or key_result.state
        self._update_runtime_state(final_state, action=action)
        state.last_media_action = action

        if desired_playing is not None and after_state is not None and after_state != desired_playing:
            return self._failure(
                f"The YouTube playback state did not switch to the requested {action} state.",
                "playback_state_not_verified",
            )

        return SkillExecutionResult(
            success=True,
            intent="youtube_action",
            response=response,
            skill_name=self.name(),
            data={
                "target_app": "youtube",
                "verified": after_state is not None,
                "playing": after_state,
                "page_title": self._extract_youtube_title(final_state.title),
            },
        )

    def _player_toggle_action(
        self,
        action: str,
        key: str,
        response: str,
        *,
        state_reader: Callable[[BrowserState], bool | None] | None = None,
        desired_state: bool | None = None,
        already_response: str | None = None,
        prepare_shortcuts: bool = True,
    ) -> SkillExecutionResult:
        focused = self.focus_youtube()
        if isinstance(focused, SkillExecutionResult):
            return focused

        browser_state = focused
        before_state = state_reader(browser_state) if state_reader is not None else None
        if desired_state is not None and before_state is desired_state:
            self._update_runtime_state(browser_state, action=action)
            state.last_media_action = action
            return SkillExecutionResult(
                success=True,
                intent="youtube_action",
                response=already_response or response,
                skill_name=self.name(),
                data={
                    "target_app": "youtube",
                    "verified": True,
                    "page_title": self._extract_youtube_title(browser_state.title),
                },
            )

        if prepare_shortcuts:
            self._prepare_player_shortcuts(browser_state)

        key_result = self._controller.press_key(key, browser_name=browser_state.browser_id, action=f"youtube_{action}")
        if not key_result.success or not key_result.state:
            return self._failure(key_result.message, key_result.error or "youtube_player_key_failed")

        after_state = self._wait_for_feature_state(state_reader, before_state, browser_name=browser_state.browser_id)
        final_state = self.detect_youtube_context() or key_result.state
        self._update_runtime_state(final_state, action=action)
        state.last_media_action = action

        if desired_state is not None and after_state is not None and after_state != desired_state:
            return self._failure(
                f"The YouTube player did not switch to the requested {action.replace('_', ' ')} state.",
                "youtube_feature_state_not_verified",
            )

        return SkillExecutionResult(
            success=True,
            intent="youtube_action",
            response=response,
            skill_name=self.name(),
            data={
                "target_app": "youtube",
                "verified": after_state is not None or bool(key_result.verified),
                "page_title": self._extract_youtube_title(final_state.title),
            },
        )

    def _navigate_video(self, action: str, keys: list[str], response: str) -> SkillExecutionResult:
        focused = self.focus_youtube()
        if isinstance(focused, SkillExecutionResult):
            return focused

        browser_state = focused
        before_title = browser_state.title
        self._prepare_player_shortcuts(browser_state)

        hotkey_result = self._controller.hotkey(keys, browser_name=browser_state.browser_id, action=f"youtube_{action}")
        if not hotkey_result.success:
            return self._failure(hotkey_result.message, hotkey_result.error or "youtube_navigation_failed")

        current_state, verified = self._wait_for_title_change(before_title, browser_name=browser_state.browser_id)
        if current_state is None:
            current_state = self.detect_youtube_context() or hotkey_result.state
        if current_state is None:
            return self._failure("The YouTube player state could not be refreshed after navigation.", "youtube_navigation_state_missing")
        if not verified and current_state.title == before_title:
            return self._failure(
                f"The {action.replace('_', ' ')} command was sent, but YouTube did not navigate to a new title.",
                "youtube_navigation_not_verified",
            )

        self._update_runtime_state(current_state, action=action)
        state.last_media_action = action
        return SkillExecutionResult(
            success=True,
            intent="youtube_action",
            response=response,
            skill_name=self.name(),
            data={
                "target_app": "youtube",
                "verified": verified,
                "page_title": self._extract_youtube_title(current_state.title),
            },
        )

    def _player_key_action(self, action: str, key: str, response: str) -> SkillExecutionResult:
        focused = self.focus_youtube()
        if isinstance(focused, SkillExecutionResult):
            return focused

        browser_state = focused
        self._prepare_player_shortcuts(browser_state)
        key_result = self._controller.press_key(key, browser_name=browser_state.browser_id, action=f"youtube_{action}")
        if not key_result.success or not key_result.state:
            return self._failure(key_result.message, key_result.error or "youtube_player_key_failed")

        final_state = self.detect_youtube_context() or key_result.state
        self._update_runtime_state(final_state, action=action)
        state.last_media_action = action
        return SkillExecutionResult(
            success=True,
            intent="youtube_action",
            response=response,
            skill_name=self.name(),
            data={
                "target_app": "youtube",
                "verified": bool(key_result.verified),
                "page_title": self._extract_youtube_title(final_state.title),
            },
        )

    def _perform_search(self, browser_state: BrowserState, query: str) -> tuple[BrowserState | None, str, bool]:
        before_title = browser_state.title
        if self._focus_youtube_search_bar(browser_state):
            type_result = self._controller.type_text(
                query,
                browser_name=browser_state.browser_id,
                clear=True,
                submit=True,
                action="youtube_search_input",
            )
            if type_result.success:
                current_state, verified = self._wait_for_title_change(
                    before_title,
                    browser_name=browser_state.browser_id,
                    timeout_ms=800,
                )
                current_state = current_state or type_result.state
                if current_state and (verified or self._title_contains_query(current_state.title, query)):
                    return current_state, "youtube_search_bar", True

        search_result = self._controller.search(query, browser_name=browser_state.browser_id, engine="youtube")
        if search_result.success:
            return search_result.state, "direct_url", bool(search_result.verified)
        return None, "failed", False

    def _focus_youtube_search_bar(self, browser_state: BrowserState) -> bool:
        if self._focus_youtube_search_uia(browser_state):
            return True

        slash_result = self._controller.press_key(
            "/",
            browser_name=browser_state.browser_id,
            action="youtube_focus_search_shortcut",
        )
        if slash_result.success:
            return True

        rect = self._youtube_search_rect(browser_state)
        click_result = self._controller.click_rect(
            rect,
            browser_name=browser_state.browser_id,
            action="youtube_focus_search_click",
        )
        return click_result.success

    def _prepare_player_shortcuts(self, browser_state: BrowserState) -> None:
        result = self._controller.press_key(
            "esc",
            browser_name=browser_state.browser_id,
            action="youtube_prepare_player",
        )
        if not result.success:
            logger.debug("Esc preparation failed before YouTube shortcut dispatch: %s", result.error)

    def _wait_for_title_change(
        self,
        before_title: str,
        *,
        browser_name: str | None = None,
        timeout_ms: int | None = None,
    ) -> tuple[BrowserState | None, bool]:
        deadline = time.monotonic() + max(1.0, float(timeout_ms or settings.get("youtube_search_wait_ms") or 1500) / 1000.0)
        last_state = self.detect_youtube_context()

        while time.monotonic() <= deadline:
            current = self.detect_youtube_context()
            if current is not None:
                last_state = current
                if current.title and current.title != before_title:
                    return current, True
            else:
                current = self._controller.detect_active_browser()
                if current and current.browser_id == browser_name:
                    last_state = current
                    if current.title and current.title != before_title:
                        return current, True
            time.sleep(0.1)

        return last_state, False

    def _wait_for_play_state(self, before_state: bool | None, *, browser_name: str) -> bool | None:
        if before_state is None:
            return None

        deadline = time.monotonic() + 2.0
        last_seen: bool | None = before_state
        while time.monotonic() <= deadline:
            current_state = self._controller.detect_active_browser()
            if current_state and current_state.browser_id == browser_name and self._is_youtube_browser_state(current_state):
                playing = self._read_player_state_uia(current_state)
                if playing is not None:
                    last_seen = playing
                    if playing != before_state:
                        return playing
            time.sleep(0.1)
        return last_seen

    def _wait_for_feature_state(
        self,
        state_reader: Callable[[BrowserState], bool | None] | None,
        before_state: bool | None,
        *,
        browser_name: str,
    ) -> bool | None:
        if state_reader is None or before_state is None:
            return None

        deadline = time.monotonic() + 1.5
        last_seen: bool | None = before_state
        while time.monotonic() <= deadline:
            current_state = self._controller.detect_active_browser()
            if current_state and current_state.browser_id == browser_name and self._is_youtube_browser_state(current_state):
                current_value = state_reader(current_state)
                if current_value is not None:
                    last_seen = current_value
                    if current_value != before_state:
                        return current_value
            time.sleep(0.1)
        return last_seen

    def _focus_youtube_search_uia(self, browser_state: BrowserState) -> bool:
        if not _PYWINAUTO_OK or Desktop is None:
            return False

        try:
            window = Desktop(backend="uia").window(handle=browser_state.hwnd)
            top_limit = browser_state.rect[1] + max(110, int(browser_state.height * 0.18))

            for control in window.descendants():
                try:
                    element_info = control.element_info
                    name = self._sanitize_visible_text(getattr(element_info, "name", "") or "")
                    control_type = str(getattr(element_info, "control_type", "") or "").lower()
                    rect = getattr(element_info, "rectangle", None)
                    if control_type not in {"edit", "combobox"} or rect is None:
                        continue
                    if rect.top > top_limit:
                        continue
                    if "search" not in name.lower():
                        continue
                    wrapper = control.wrapper_object()
                    wrapper.click_input()
                    wrapper.set_focus()
                    return True
                except Exception:
                    continue
        except Exception as exc:  # pragma: no cover - live UIA dependent
            logger.debug("YouTube search bar UIA focus failed: %s", exc)
        return False

    def _click_video_result_uia(self, browser_state: BrowserState, index: int) -> bool:
        if not _PYWINAUTO_OK or Desktop is None:
            return False

        try:
            window = Desktop(backend="uia").window(handle=browser_state.hwnd)
            content_top = browser_state.rect[1] + max(170, int(browser_state.height * 0.22))
            left_limit = browser_state.rect[0] + int(browser_state.width * 0.10)
            right_limit = browser_state.rect[0] + int(browser_state.width * 0.80)
            minimum_width = max(220, int(browser_state.width * 0.20))
            candidates: list[tuple[int, int, str, Any]] = []

            for control in window.descendants():
                try:
                    element_info = control.element_info
                    name = self._sanitize_visible_text(getattr(element_info, "name", "") or "")
                    control_type = str(getattr(element_info, "control_type", "") or "").lower()
                    rect = getattr(element_info, "rectangle", None)
                    if control_type not in {"hyperlink", "text", "button"} or rect is None or not name:
                        continue
                    if rect.top < content_top or rect.left < left_limit or rect.right > right_limit:
                        continue
                    if (rect.right - rect.left) < minimum_width:
                        continue
                    lowered = name.lower()
                    if lowered in _YOUTUBE_NOISE_PHRASES:
                        continue
                    if any(noise == lowered for noise in _YOUTUBE_NOISE_PHRASES):
                        continue
                    candidates.append((rect.top, rect.left, lowered, control))
                except Exception:
                    continue

            deduped: list[tuple[int, int, str, Any]] = []
            seen: set[str] = set()
            for item in sorted(candidates):
                if item[2] in seen:
                    continue
                seen.add(item[2])
                deduped.append(item)

            if 0 < index <= len(deduped):
                wrapper = deduped[index - 1][3].wrapper_object()
                wrapper.click_input()
                return True
        except Exception as exc:  # pragma: no cover - live UIA dependent
            logger.debug("YouTube result UIA click failed: %s", exc)
        return False

    def _click_result_heuristic(self, browser_state: BrowserState, index: int) -> bool:
        rect = self._youtube_result_rect(browser_state, index)
        click_result = self._controller.click_rect(
            rect,
            browser_name=browser_state.browser_id,
            action="youtube_result_heuristic",
        )
        return click_result.success

    def _read_player_state_uia(self, browser_state: BrowserState) -> bool | None:
        if not _PYWINAUTO_OK or Desktop is None:
            return None

        try:
            window = Desktop(backend="uia").window(handle=browser_state.hwnd)
            top_limit = browser_state.rect[1] + max(120, int(browser_state.height * 0.15))

            for control in window.descendants():
                try:
                    element_info = control.element_info
                    name = str(getattr(element_info, "name", "") or "").strip().lower()
                    control_type = str(getattr(element_info, "control_type", "") or "").lower()
                    rect = getattr(element_info, "rectangle", None)
                    if control_type != "button" or rect is None or rect.top < top_limit:
                        continue
                    if "pause" in name:
                        return True
                    if name.startswith("play") or "play (" in name or "play " in name:
                        return False
                except Exception:
                    continue
        except Exception as exc:  # pragma: no cover - live UIA dependent
            logger.debug("YouTube player-state UIA read failed: %s", exc)
        return None

    def _read_title_uia(self, browser_state: BrowserState) -> str:
        if not _PYWINAUTO_OK or Desktop is None:
            return ""

        try:
            window = Desktop(backend="uia").window(handle=browser_state.hwnd)
            top_limit = browser_state.rect[1] + max(140, int(browser_state.height * 0.18))
            bottom_limit = browser_state.rect[1] + int(browser_state.height * 0.55)
            candidates: list[tuple[int, int, str]] = []

            for control in window.descendants():
                try:
                    element_info = control.element_info
                    name = self._sanitize_visible_text(getattr(element_info, "name", "") or "")
                    control_type = str(getattr(element_info, "control_type", "") or "").lower()
                    rect = getattr(element_info, "rectangle", None)
                    if control_type not in {"heading", "text", "hyperlink"} or rect is None or not name:
                        continue
                    if rect.top < top_limit or rect.bottom > bottom_limit:
                        continue
                    if len(name) < 6 or name.lower() in _YOUTUBE_NOISE_PHRASES:
                        continue
                    candidates.append((rect.top, -(rect.right - rect.left), name))
                except Exception:
                    continue

            if candidates:
                candidates.sort()
                return candidates[0][2]
        except Exception as exc:  # pragma: no cover - live UIA dependent
            logger.debug("YouTube title UIA read failed: %s", exc)
        return ""

    def _read_visible_results(self, browser_state: BrowserState) -> tuple[list[str], str]:
        items = self._read_visible_results_uia(browser_state)
        if items:
            return items, "uia"
        return [], "none"

    def _read_visible_results_uia(self, browser_state: BrowserState) -> list[str]:
        if not _PYWINAUTO_OK or Desktop is None:
            return []

        try:
            window = Desktop(backend="uia").window(handle=browser_state.hwnd)
            content_top = browser_state.rect[1] + max(170, int(browser_state.height * 0.20))
            bottom_limit = browser_state.rect[3] - max(60, int(browser_state.height * 0.08))
            left_limit = browser_state.rect[0] + int(browser_state.width * 0.08)
            right_limit = browser_state.rect[0] + int(browser_state.width * 0.90)
            minimum_width = max(180, int(browser_state.width * 0.18))
            candidates: list[tuple[int, int, str]] = []

            for control in window.descendants():
                try:
                    element_info = control.element_info
                    name = self._sanitize_visible_text(getattr(element_info, "name", "") or "")
                    control_type = str(getattr(element_info, "control_type", "") or "").lower()
                    rect = getattr(element_info, "rectangle", None)
                    if control_type not in {"hyperlink", "text", "button", "heading"} or rect is None or not name:
                        continue
                    if rect.top < content_top or rect.bottom > bottom_limit:
                        continue
                    if rect.left < left_limit or rect.right > right_limit:
                        continue
                    if (rect.right - rect.left) < minimum_width:
                        continue
                    lowered = name.lower()
                    if lowered in _YOUTUBE_NOISE_PHRASES:
                        continue
                    candidates.append((rect.top, rect.left, name))
                except Exception:
                    continue

            visible_items: list[str] = []
            seen: set[str] = set()
            for _top, _left, label in sorted(candidates):
                normalized = label.lower()
                if normalized in seen:
                    continue
                seen.add(normalized)
                visible_items.append(label)
                if len(visible_items) >= 8:
                    break
            return visible_items
        except Exception as exc:  # pragma: no cover - live UIA dependent
            logger.debug("YouTube results UIA read failed: %s", exc)
        return []

    def _read_muted_state_uia(self, browser_state: BrowserState) -> bool | None:
        name = self._find_player_button_name(browser_state, ("mute", "unmute"))
        if not name:
            return None
        lowered = name.lower()
        if "unmute" in lowered:
            return True
        if "mute" in lowered:
            return False
        return None

    def _read_captions_state_uia(self, browser_state: BrowserState) -> bool | None:
        name = self._find_player_button_name(browser_state, ("caption", "subtitle"))
        if not name:
            return None
        lowered = name.lower()
        if "turn off captions" in lowered or "captions off" in lowered or "disable captions" in lowered:
            return True
        if "turn on captions" in lowered or "captions on" in lowered or "enable captions" in lowered:
            return False
        return None

    def _read_fullscreen_state_uia(self, browser_state: BrowserState) -> bool | None:
        name = self._find_player_button_name(browser_state, ("full screen", "fullscreen"))
        if not name:
            return None
        lowered = name.lower()
        if "exit full screen" in lowered or "exit fullscreen" in lowered:
            return True
        if "full screen" in lowered or "fullscreen" in lowered:
            return False
        return None

    def _find_player_button_name(self, browser_state: BrowserState, keywords: tuple[str, ...]) -> str:
        if not _PYWINAUTO_OK or Desktop is None:
            return ""

        try:
            window = Desktop(backend="uia").window(handle=browser_state.hwnd)
            for control in window.descendants():
                try:
                    element_info = control.element_info
                    name = str(getattr(element_info, "name", "") or "").strip()
                    control_type = str(getattr(element_info, "control_type", "") or "").lower()
                    if control_type != "button" or not name:
                        continue
                    lowered = name.lower()
                    if any(keyword in lowered for keyword in keywords):
                        return name
                except Exception:
                    continue
        except Exception as exc:  # pragma: no cover - live UIA dependent
            logger.debug("YouTube player button UIA read failed: %s", exc)
        return ""

    def _classify_command(
        self,
        cleaned_command: str,
        normalized: str,
        context: Mapping[str, Any],
        *,
        raw_command: str = "",
    ) -> tuple[str, dict[str, Any]]:
        intent = self._normalize_command(str(context.get("intent") or ""))

        if normalized in _PAUSE_COMMANDS:
            return "pause", {}
        if normalized in _RESUME_COMMANDS:
            return "resume", {}
        if normalized in _TOGGLE_PLAY_COMMANDS:
            return "toggle_play", {}
        if normalized in _NEXT_COMMANDS:
            return "next_video", {}
        if normalized in _PREVIOUS_COMMANDS:
            return "previous_video", {}
        if normalized in _VOLUME_UP_COMMANDS:
            return "volume_up", {}
        if normalized in _VOLUME_DOWN_COMMANDS:
            return "volume_down", {}
        if normalized in _MUTE_COMMANDS:
            return "mute", {}
        if normalized in _UNMUTE_COMMANDS:
            return "unmute", {}
        if normalized in _FULLSCREEN_ON_COMMANDS:
            return "fullscreen_on", {}
        if normalized in _FULLSCREEN_OFF_COMMANDS:
            return "fullscreen_off", {}
        if normalized in _THEATER_MODE_COMMANDS:
            return "theater_mode", {}
        if normalized in _CAPTIONS_ON_COMMANDS:
            return "captions_on", {}
        if normalized in _CAPTIONS_OFF_COMMANDS:
            return "captions_off", {}
        if normalized in _MINI_PLAYER_COMMANDS:
            return "mini_player", {}
        if normalized in _REPLAY_COMMANDS:
            return "replay", {}
        if normalized in _SEEK_FORWARD_COMMANDS:
            return "seek_forward", {}
        if normalized in _SEEK_BACKWARD_COMMANDS:
            return "seek_backward", {}
        if normalized in _READ_TITLE_COMMANDS:
            return "read_title", {}
        if any(normalized.startswith(prefix) for prefix in _READ_RESULTS_PREFIXES):
            return "read_results", {"result_index": self._extract_ordinal(normalized) or 0}
        if normalized in _OPEN_MUSIC_COMMANDS:
            return "open_youtube_music", {}
        if normalized in _OPEN_COMMANDS:
            return "open_youtube", {}
        if self._is_result_selection_command(normalized):
            return "play_result", {"result_index": self._extract_ordinal(normalized) or 1}
        if normalized in {"play top result", "play top video", "open top result", "open top video"}:
            return "play_result", {"result_index": 1}
        if self._is_query_command(normalized, intent):
            query, autoplay = self._extract_search_payload(
                cleaned_command, intent=intent, original_command=raw_command or cleaned_command,
            )
            return "search_video", {
                "query": query,
                "autoplay": autoplay,
                "result_index": self._extract_ordinal(normalized) or 1,
            }
        return "unsupported", {}

    def _context_targets_youtube(self, context: Mapping[str, Any]) -> bool:
        target_app = self._normalize_command(str(context.get("context_target_app") or ""))
        resolved_intent = self._normalize_command(str(context.get("context_resolved_intent") or ""))
        return target_app == "youtube" or resolved_intent.startswith("youtube")

    def _is_youtube_context(self, context: Mapping[str, Any]) -> bool:
        current_app = self._normalize_command(str(context.get("current_app") or ""))
        current_context = self._normalize_command(str(context.get("current_context") or ""))
        current_process = self._normalize_command(str(context.get("current_process_name") or ""))
        title = str(context.get("current_window_title") or "").strip().lower()
        return (
            current_app == "youtube"
            or current_context == "youtube"
            or "youtube" in title
            or current_process in _YOUTUBE_PROCESS_NAMES and "youtube" in title
            or self._context_targets_youtube(context)
        )

    def _has_recent_youtube_context(self, context: Mapping[str, Any]) -> bool:
        last_skill = self._normalize_command(str(context.get("last_skill_used") or state.last_skill_used or ""))
        last_action = str(context.get("last_successful_action") or state.last_successful_action or "").strip().lower()
        return bool(
            context.get("youtube_active")
            or state.youtube_active
            or last_skill == self.name().lower()
            or last_action.endswith(":youtube")
            or last_action.startswith("youtube_")
        )

    def _is_query_command(self, normalized: str, intent: str) -> bool:
        if self._is_result_selection_command(normalized):
            return False
        if intent == "search":
            return True
        return any(normalized.startswith(prefix) for prefix in _SEARCH_PREFIXES + _AUTOPLAY_PREFIXES)

    def _is_result_selection_command(self, normalized: str) -> bool:
        ordinal = self._extract_ordinal(normalized)
        if ordinal is None:
            return False
        tokens = set(normalized.split())
        if tokens & _RESULT_WORDS:
            return True
        return normalized in {
            "first result",
            "second result",
            "third result",
            "fourth result",
            "fifth result",
        }

    def _extract_search_payload(
        self,
        command: str,
        *,
        intent: str,
        original_command: str = "",
    ) -> tuple[str, bool]:
        cleaned = self._strip_explicit_youtube_tokens(command)
        lowered = cleaned.lower()
        # Keep the raw user command for recovering song names lost during
        # youtube-token stripping (e.g. "play X on youtube" → cleaned="play")
        raw = original_command or command

        # Check autoplay prefixes first (play, watch, listen to, listen)
        for prefix in _AUTOPLAY_PREFIXES:
            if lowered.startswith(prefix):
                query = cleaned[len(prefix):].strip()
                if query:
                    return query, True
                # Prefix matched but remaining query is empty — recover
                # the song name from the original (unstripped) command.
                query = self._recover_query_from_raw(raw, prefix)
                if query:
                    return query, True
                # User just said "play" with nothing else
                return cleaned.strip() or "music", True
            # Also match when the cleaned text IS the prefix word itself
            # (e.g. cleaned='play', prefix='play ')
            if lowered == prefix.strip():
                query = self._recover_query_from_raw(raw, prefix)
                if query:
                    return query, True
                return cleaned.strip() or "music", True

        for prefix in _SEARCH_PREFIXES:
            if lowered.startswith(prefix):
                query = cleaned[len(prefix):].strip()
                return query if query else cleaned.strip(), False

        return cleaned.strip(), intent == "play"

    def _recover_query_from_raw(self, raw_command: str, prefix: str) -> str:
        """Try to extract a search query from the original user command.

        Used when stripping youtube tokens from the command causes the query
        to be lost (e.g. 'play despacito on youtube' → cleaned='play').
        """
        raw_lower = raw_command.lower().strip()
        for pfx in _AUTOPLAY_PREFIXES:
            if raw_lower.startswith(pfx):
                remainder = raw_command.strip()[len(pfx):]
                # Strip "on/in/with youtube" phrases
                remainder = re.sub(
                    r"\s+(?:on|in|with|using)\s+youtube\b", "",
                    remainder, flags=re.IGNORECASE,
                ).strip()
                remainder = re.sub(
                    r"\byoutube\b", "", remainder, flags=re.IGNORECASE,
                ).strip()
                if remainder:
                    return remainder
        return ""

    def _update_runtime_state(self, browser_state: BrowserState, *, action: str) -> None:
        state.youtube_active = self._is_youtube_browser_state(browser_state)
        if not state.youtube_active:
            return

        state.last_media_action = action
        title = self._extract_youtube_title(browser_state.title)
        if title:
            state.last_video_title = title

    def _is_youtube_browser_state(self, browser_state: BrowserState) -> bool:
        title = str(browser_state.title or "").strip().lower()
        return (
            browser_state.site_context == "youtube"
            or "youtube" in title
            or str(state.current_app or "").strip().lower() == "youtube"
            or str(state.current_context or "").strip().lower() == "youtube"
        )

    @staticmethod
    def _preferred_browser_name() -> str:
        candidate = str(state.last_browser or settings.get("preferred_browser") or "chrome").strip().lower()
        return candidate if candidate in _YOUTUBE_BROWSER_IDS else "chrome"

    @staticmethod
    def _extract_youtube_title(title: str) -> str:
        cleaned = str(title or "").strip()
        if not cleaned:
            return ""

        suffixes = (
            " - Google Chrome",
            " - Chrome",
            " - Microsoft Edge",
            " - Mozilla Firefox",
            " - Brave",
        )
        for suffix in suffixes:
            if cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)].strip()
                break

        for suffix in (" - YouTube", " - YouTube Music"):
            if cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)].strip()
                break

        return cleaned

    @staticmethod
    def _title_contains_query(title: str, query: str) -> bool:
        title_l = str(title or "").lower()
        query_tokens = [token for token in re.split(r"\W+", str(query or "").lower()) if len(token) > 2]
        if not query_tokens:
            return False
        return any(token in title_l for token in query_tokens[:3])

    @staticmethod
    def _normalize_command(command: str) -> str:
        return " ".join(str(command or "").strip().lower().split())

    def _strip_explicit_youtube_tokens(self, command: str) -> str:
        text = str(command or "").strip()
        if not text:
            return ""

        patterns = (
            r"^\s*youtube[\s:,-]+",
            r"\s+(?:on|in|with|using)\s+youtube\b",
            r"\byoutube\b\s*$",
        )
        for pattern in patterns:
            text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
        return " ".join(text.split())

    @staticmethod
    def _mentions_youtube(command: str) -> bool:
        return bool(re.search(r"\byoutube\b", str(command or ""), flags=re.IGNORECASE))

    @staticmethod
    def _extract_ordinal(normalized: str) -> int | None:
        for token in normalized.split():
            if token in _ORDINALS:
                return _ORDINALS[token]
        return None

    @staticmethod
    def _sanitize_visible_text(value: str) -> str:
        text = " ".join(str(value or "").split())
        text = re.sub(r"https?://\S+", "", text).strip()
        if len(text) < 3:
            return ""
        if text.lower().startswith(("http://", "https://", "www.")):
            return ""
        return text[:220]

    @staticmethod
    def _youtube_search_rect(browser_state: BrowserState) -> tuple[int, int, int, int]:
        left, top, right, bottom = browser_state.rect
        width = max(0, right - left)
        height = max(0, bottom - top)
        bar_top = top + max(78, int(height * 0.085))
        bar_bottom = bar_top + max(36, int(height * 0.045))
        bar_left = left + int(width * 0.28)
        bar_right = right - int(width * 0.28)
        return (bar_left, bar_top, bar_right, bar_bottom)

    @staticmethod
    def _youtube_result_rect(browser_state: BrowserState, index: int) -> tuple[int, int, int, int]:
        left, top, right, bottom = browser_state.rect
        width = max(0, right - left)
        height = max(0, bottom - top)
        content_top = top + max(190, int(height * 0.25))
        row_gap = max(110, int(height * 0.15))
        row_top = content_top + max(0, index - 1) * row_gap
        row_bottom = row_top + max(58, int(height * 0.08))
        return (
            left + int(width * 0.10),
            row_top,
            left + int(width * 0.76),
            row_bottom,
        )

    def _failure(self, response: str, error: str) -> SkillExecutionResult:
        return SkillExecutionResult.from_action_result(
            intent="youtube_action",
            response=response,
            skill_name=self.name(),
            action_result=ActionResult(
                success=False,
                action="youtube_action",
                target="youtube",
                message=response,
                data={"target_app": "youtube"},
                error_code=error,
                verified=False,
            ),
        )

    def _to_action_result(self, result: SkillExecutionResult, operation: str) -> YouTubeActionResult:
        action_payload = dict(result.action_result or {})
        return YouTubeActionResult(
            success=result.success,
            operation=operation,
            message=result.response,
            error=result.error,
            target=str(action_payload.get("target") or "youtube"),
            verified=bool(action_payload.get("verified", False)),
            duration_ms=int(action_payload.get("duration_ms") or 0),
            data=dict(result.data),
        )
