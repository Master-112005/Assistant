"""
Phase 13 — Active Window Detection Test Suite

Tests all 10 scenarios from the spec plus classification, watcher, and state.

1.  Detect Chrome active
2.  Detect WhatsApp active
3.  Detect Explorer active
4.  Detect Spotify active
5.  Detect YouTube tab in browser (title-based)
6.  Context changes when switching windows
7.  State variables updated correctly
8.  Unknown app returns generic context (not crash)
9.  Minimized windows still classified (visibility doesn't block classification)
10. No crash on rapid switching / bad data

+ Classification table completeness
+ Browser sub-context detection
+ Process utils safety (no exceptions)
+ ContextManager API
+ Watcher threading
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch, PropertyMock
import pytest

from core import state
from core.window_context import (
    ActiveWindowDetector,
    WindowInfo,
    UNKNOWN_WINDOW,
    _exe_stem,
    _update_state,
    _EXE_MAP,
    _BROWSER_TITLE_MAP,
)
from core.context import ContextManager
from core.process_utils import ProcessInfo, get_process_info


# ===========================================================================
# Fixtures and helpers
# ===========================================================================

def _detector() -> ActiveWindowDetector:
    """Fresh detector for each test."""
    return ActiveWindowDetector()


def _mock_proc_info(name: str = "chrome.exe", pid: int = 1234) -> ProcessInfo:
    return ProcessInfo(
        pid=pid,
        name=name,
        exe_path=f"C:\\Program Files\\{name}",
        cmdline="",
        status="running",
    )


def _window(
    hwnd: int = 1001,
    title: str = "Test Window",
    process_name: str = "chrome.exe",
    pid: int = 1234,
    visible: bool = True,
    minimized: bool = False,
) -> WindowInfo:
    """Create a pre-built WindowInfo for classification tests."""
    info = WindowInfo(
        hwnd=hwnd,
        title=title,
        process_id=pid,
        process_name=process_name,
        is_visible=visible,
        is_minimized=minimized,
    )
    det = _detector()
    det.classify_window(info)
    return info


# ===========================================================================
# 1 — Detect Chrome active
# ===========================================================================

class TestChromeDetection:
    def test_chrome_exe_classifies_as_chrome(self):
        info = _window(process_name="chrome.exe", title="New Tab - Google Chrome")
        assert info.app_id == "chrome"
        assert info.app_type == "browser"

    def test_chrome_is_browser(self):
        det  = _detector()
        info = _window(process_name="chrome.exe")
        assert det.is_browser(info)

    def test_chrome_with_google_title(self):
        info = _window(process_name="chrome.exe", title="Google - Google Chrome")
        assert info.app_id == "chrome"
        assert info.app_type == "browser"

    def test_edge_classifies_as_browser(self):
        info = _window(process_name="msedge.exe", title="New Tab - Microsoft Edge")
        assert info.app_id == "edge"
        assert info.app_type == "browser"

    def test_firefox_classifies_as_browser(self):
        info = _window(process_name="firefox.exe", title="Mozilla Firefox")
        assert info.app_id == "firefox"
        assert info.app_type == "browser"


# ===========================================================================
# 2 — Detect WhatsApp active
# ===========================================================================

class TestWhatsAppDetection:
    def test_whatsapp_exe(self):
        info = _window(process_name="whatsapp.exe", title="WhatsApp")
        assert info.app_id == "whatsapp"
        assert info.app_type == "communication"

    def test_whatsapp_web_in_browser(self):
        """WhatsApp Web open in Chrome → classified as whatsapp."""
        info = _window(
            process_name="chrome.exe",
            title="WhatsApp Web - Google Chrome",
        )
        assert info.app_id == "whatsapp"
        assert info.context_detail == "messaging"

    def test_discord_classifies_correctly(self):
        info = _window(process_name="discord.exe", title="Discord")
        assert info.app_id == "discord"
        assert info.app_type == "communication"

    def test_telegram_classifies_correctly(self):
        info = _window(process_name="telegram.exe", title="Telegram")
        assert info.app_id == "telegram"
        assert info.app_type == "communication"


# ===========================================================================
# 3 — Detect Explorer active
# ===========================================================================

class TestExplorerDetection:
    def test_explorer_exe(self):
        info = _window(process_name="explorer.exe", title="Downloads")
        assert info.app_id == "explorer"
        assert info.app_type == "files"

    def test_is_explorer_helper(self):
        det  = _detector()
        info = _window(process_name="explorer.exe", title="Documents")
        assert det.is_explorer(info)

    def test_non_explorer_returns_false(self):
        det  = _detector()
        info = _window(process_name="chrome.exe", title="Google")
        assert not det.is_explorer(info)


# ===========================================================================
# 4 — Detect Spotify active
# ===========================================================================

class TestSpotifyDetection:
    def test_spotify_exe(self):
        info = _window(process_name="spotify.exe", title="Spotify")
        assert info.app_id == "spotify"
        assert info.app_type == "media"

    def test_spotify_in_browser(self):
        """open.spotify.com in a browser tab → classified as spotify."""
        info = _window(
            process_name="chrome.exe",
            title="Spotify - Google Chrome",
        )
        assert info.app_id == "spotify"


# ===========================================================================
# 5 — Detect YouTube tab inside browser
# ===========================================================================

class TestYouTubeDetection:
    def test_youtube_in_chrome_title(self):
        info = _window(
            process_name="chrome.exe",
            title="YouTube - Google Chrome",
        )
        assert info.app_id == "youtube"
        assert info.context_detail == "video"

    def test_youtube_video_title(self):
        info = _window(
            process_name="chrome.exe",
            title="Never Gonna Give You Up - YouTube - Google Chrome",
        )
        assert info.app_id == "youtube"

    def test_youtube_in_edge(self):
        info = _window(
            process_name="msedge.exe",
            title="YouTube - Microsoft Edge",
        )
        assert info.app_id == "youtube"

    def test_youtube_in_firefox(self):
        info = _window(
            process_name="firefox.exe",
            title="YouTube — Mozilla Firefox",
        )
        assert info.app_id == "youtube"

    def test_non_youtube_browser_not_classified_as_youtube(self):
        info = _window(
            process_name="chrome.exe",
            title="Google Search - Google Chrome",
        )
        assert info.app_id == "chrome"   # browser, not youtube

    def test_context_manager_youtube_detection(self):
        """ContextManager.is_youtube_active() reads from state."""
        state.current_app = "youtube"
        cm = ContextManager()
        assert cm.is_youtube_active()
        state.current_app = "unknown"  # reset


# ===========================================================================
# 6 — Context changes when switching windows
# ===========================================================================

class TestContextChangeOnSwitch:
    def test_state_updated_after_classify(self):
        """_update_state() writes correct fields to core.state."""
        info = WindowInfo(
            hwnd=9999,
            title="YouTube - Google Chrome",
            process_name="chrome.exe",
            process_id=2222,
            app_id="youtube",
            app_type="browser",
        )
        _update_state(info)

        assert state.current_context      == "youtube"
        assert state.current_app          == "youtube"
        assert state.current_window_title == "YouTube - Google Chrome"
        assert state.current_process_name == "chrome.exe"
        assert state.last_context_change  >  0

    def test_context_history_appended(self):
        """Every _update_state call appends to context_history."""
        state.context_history = []  # reset
        for app in ("chrome", "youtube", "whatsapp"):
            info = WindowInfo(hwnd=1, title=app, process_name=f"{app}.exe", app_id=app)
            _update_state(info)

        assert len(state.context_history) == 3
        assert state.context_history[-1]["app_id"] == "whatsapp"

    def test_context_history_bounded_to_20(self):
        """History is trimmed to 20 entries."""
        state.context_history = []
        for i in range(30):
            info = WindowInfo(hwnd=i, title=str(i), process_name="app.exe", app_id=str(i))
            _update_state(info)

        assert len(state.context_history) <= 20


# ===========================================================================
# 7 — State variables updated correctly
# ===========================================================================

class TestStateVariables:
    def test_initial_state_defaults(self):
        """Phase 13 state vars must have sane defaults."""
        # These are set at module level in core/state.py
        from core import state as st
        assert hasattr(st, "current_context")
        assert hasattr(st, "current_app")
        assert hasattr(st, "current_window_title")
        assert hasattr(st, "current_process_name")
        assert hasattr(st, "last_context_change")
        assert hasattr(st, "context_history")
        assert isinstance(st.context_history, list)

    def test_context_manager_get_current_app(self):
        state.current_app = "vscode"
        cm = ContextManager()
        assert cm.get_current_app() == "vscode"
        state.current_app = "unknown"

    def test_context_manager_get_current_title(self):
        state.current_window_title = "Visual Studio Code"
        cm = ContextManager()
        assert cm.get_current_title() == "Visual Studio Code"
        state.current_window_title = ""

    def test_context_manager_get_snapshot_keys(self):
        cm   = ContextManager()
        snap = cm.get_context_snapshot()
        required = {"app_id", "window_title", "process_name", "last_changed", "history_depth"}
        assert required.issubset(snap.keys())

    def test_context_manager_is_browser_active(self):
        cm = ContextManager()
        # Force last_info to a browser window
        info = WindowInfo(app_type="browser", app_id="chrome")
        cm._last_info = info
        assert cm.is_browser_active()


# ===========================================================================
# 8 — Unknown app returns generic context (no crash)
# ===========================================================================

class TestUnknownAppHandling:
    def test_unknown_exe_gives_exe_stem_as_app_id(self):
        """An unrecognised exe should use its stem as app_id, not crash."""
        info = _window(process_name="mynewapp.exe", title="My New App")
        # Should NOT raise; should return some non-empty app_id
        assert info.app_id != ""
        # And app_type should be 'unknown' since we don't know it
        assert info.app_type == "unknown"

    def test_empty_process_name_gives_unknown(self):
        det  = _detector()
        info = WindowInfo(hwnd=1, title="Some Window", process_name="")
        det.classify_window(info)
        # Empty process name → exe_stem is "" → app_id should be "unknown" or ""
        assert info.app_id == "unknown" or info.app_id == ""

    def test_get_active_context_returns_windowinfo_when_pywin32_missing(self):
        """If pywin32 is not installed, detector returns empty WindowInfo (no crash)."""
        det = ActiveWindowDetector()
        with patch("core.window_context._PYWIN32_OK", False):
            result = det.get_active_context()
        # Must return a WindowInfo, not raise
        assert isinstance(result, WindowInfo)

    def test_context_manager_refresh_returns_unknown_when_disabled(self):
        cm = ContextManager()
        with patch("core.settings.get", return_value=False):
            result = cm.refresh()
        assert result == UNKNOWN_WINDOW

    def test_window_info_to_dict(self):
        info = WindowInfo(
            hwnd=1, title="Test", process_name="test.exe",
            app_id="test", app_type="unknown",
        )
        d = info.to_dict()
        assert d["app_id"] == "test"
        assert d["title"]  == "Test"
        assert "hwnd"      in d

    def test_window_info_str(self):
        info = WindowInfo(app_id="chrome", process_name="chrome.exe", title="Google Chrome")
        s = str(info)
        assert "chrome" in s


# ===========================================================================
# 9 — Minimized windows still classified
# ===========================================================================

class TestMinimizedWindows:
    def test_minimized_window_still_gets_app_id(self):
        """Classification works regardless of minimized state."""
        info = _window(
            process_name="spotify.exe",
            title="Spotify",
            minimized=True,
        )
        assert info.app_id == "spotify"
        assert info.is_minimized is True

    def test_visible_false_still_classified(self):
        info = _window(
            process_name="discord.exe",
            title="Discord",
            visible=False,
        )
        assert info.app_id == "discord"
        assert info.is_visible is False


# ===========================================================================
# 10 — No crash on rapid switching / bad data
# ===========================================================================

class TestRobustness:
    def test_get_window_info_with_zero_hwnd(self):
        det    = _detector()
        result = det.get_window_info(0)
        assert isinstance(result, WindowInfo)

    def test_get_window_info_with_negative_hwnd(self):
        det    = _detector()
        result = det.get_window_info(-1)
        assert isinstance(result, WindowInfo)

    def test_classify_window_with_empty_info(self):
        det  = _detector()
        info = WindowInfo()  # all defaults
        det.classify_window(info)   # must not raise
        # app_id may be "unknown" or "" — just not an exception
        assert isinstance(info.app_id, str)

    def test_update_state_does_not_crash_with_empty_info(self):
        info = WindowInfo()  # all defaults
        _update_state(info)  # must not raise

    def test_rapid_classification_no_exception(self):
        """Classify 100 windows rapidly — no exceptions."""
        det      = _detector()
        exes     = [
            "chrome.exe", "discord.exe", "explorer.exe", "notepad.exe",
            "unknownapp.exe", "", "spotify.exe", "whatsapp.exe",
        ]
        titles = [
            "YouTube - Google Chrome", "Discord", "Documents",
            "Untitled", "Random", "", "Spotify", "WhatsApp",
        ]
        for i in range(100):
            exe   = exes[i % len(exes)]
            title = titles[i % len(titles)]
            info  = WindowInfo(hwnd=i, process_name=exe, title=title)
            det.classify_window(info)
        # If we got here — no crash


# ===========================================================================
# Classification table completeness
# ===========================================================================

class TestClassificationTable:
    def test_exe_map_has_all_required_entries(self):
        """Spot-check that key entries are in the classification table."""
        required_exes = [
            "chrome", "msedge", "firefox", "whatsapp", "discord",
            "spotify", "explorer", "code", "notepad",
        ]
        for exe in required_exes:
            assert exe in _EXE_MAP, f"{exe!r} missing from _EXE_MAP"

    def test_all_exe_map_values_are_tuples(self):
        for exe, val in _EXE_MAP.items():
            assert isinstance(val, tuple), f"{exe} → {val} is not a tuple"
            assert len(val) == 2, f"{exe} → {val} has wrong length"

    def test_browser_title_map_is_list_of_tuples(self):
        for entry in _BROWSER_TITLE_MAP:
            assert len(entry) == 3, f"Bad entry: {entry}"

    def test_youtube_in_browser_title_map(self):
        fragments = [e[0] for e in _BROWSER_TITLE_MAP]
        assert "youtube" in fragments

    def test_whatsapp_web_in_browser_title_map(self):
        fragments = [e[0] for e in _BROWSER_TITLE_MAP]
        assert "whatsapp web" in fragments


# ===========================================================================
# _exe_stem helper
# ===========================================================================

class TestExeStem:
    def test_chrome_exe(self):
        assert _exe_stem("chrome.exe") == "chrome"

    def test_uppercase_exe(self):
        assert _exe_stem("CHROME.EXE") == "chrome"

    def test_no_extension(self):
        assert _exe_stem("spotify") == "spotify"

    def test_empty_string(self):
        assert _exe_stem("") == ""

    def test_none_safe(self):
        assert _exe_stem(None) == ""  # type: ignore[arg-type]


# ===========================================================================
# ProcessInfo dataclass
# ===========================================================================

class TestProcessInfo:
    def test_exe_name_from_exe_path(self):
        pi = ProcessInfo(pid=1, exe_path="C:\\Program Files\\Google\\chrome.exe")
        assert pi.exe_name == "chrome.exe"

    def test_exe_name_fallback_to_name(self):
        pi = ProcessInfo(pid=1, name="chrome.exe", exe_path="")
        assert pi.exe_name == "chrome.exe"

    def test_exe_name_empty(self):
        pi = ProcessInfo(pid=1)
        assert pi.exe_name == ""


# ===========================================================================
# get_process_info safety
# ===========================================================================

class TestGetProcessInfoSafety:
    def test_zero_pid_returns_none(self):
        assert get_process_info(0) is None

    def test_negative_pid_returns_none(self):
        assert get_process_info(-1) is None

    def test_nonexistent_pid_returns_none(self):
        """A very high PID that almost certainly doesn't exist."""
        result = get_process_info(9999999)
        assert result is None or isinstance(result, ProcessInfo)

    def test_valid_pid_returns_processinfo_or_none(self):
        """We can't guarantee any specific PID exists, but it must not raise."""
        import os
        result = get_process_info(os.getpid())
        # Our own process should work
        if result is not None:
            assert isinstance(result, ProcessInfo)
            assert result.pid == os.getpid()


# ===========================================================================
# ContextManager watcher thread safety
# ===========================================================================

class TestWatcherThreadSafety:
    def test_watcher_starts_and_stops_cleanly(self):
        cm = ContextManager()
        events: List[WindowInfo] = []

        with patch("core.settings.get", side_effect=lambda k: (
            True if k == "context_detection_enabled" else 500
        )):
            cm.start_watcher(callback=events.append)
            assert cm.is_watching
            time.sleep(0.1)  # Let it start
            cm.stop_watcher()
            assert not cm.is_watching

    def test_watcher_not_started_when_disabled(self):
        cm = ContextManager()
        with patch("core.settings.get", return_value=False):
            cm.start_watcher()
        assert not cm.is_watching

    def test_double_start_does_not_crash(self):
        """Calling start_watcher twice must not raise."""
        cm = ContextManager()
        with patch("core.settings.get", side_effect=lambda k: (
            True if k == "context_detection_enabled" else 500
        )):
            cm.start_watcher()
            cm.start_watcher()   # Should stop old watcher first
            cm.stop_watcher()

    def test_stop_when_not_running_does_not_crash(self):
        cm = ContextManager()
        cm.stop_watcher()  # Not running — should be a no-op


# ===========================================================================
# Detector helper methods
# ===========================================================================

class TestDetectorHelpers:
    def test_get_foreground_window_returns_int(self):
        det    = _detector()
        result = det.get_foreground_window()
        assert isinstance(result, int)

    def test_get_foreground_window_returns_zero_without_pywin32(self):
        det = _detector()
        with patch("core.window_context._PYWIN32_OK", False):
            hwnd = det.get_foreground_window()
        assert hwnd == 0

    def test_should_update_context_skips_zero_hwnd(self):
        det  = _detector()
        info = WindowInfo(hwnd=0)
        assert not det._should_update_context(info)

    def test_should_update_context_skips_nova_window(self):
        det  = _detector()
        info = WindowInfo(hwnd=1, title="Nova Assistant", class_name="MainWindow")
        assert not det._should_update_context(info)

    def test_should_update_context_passes_normal_window(self):
        det  = _detector()
        info = WindowInfo(hwnd=1234, title="YouTube - Google Chrome")
        assert det._should_update_context(info)
