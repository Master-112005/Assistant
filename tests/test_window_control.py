from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import core.window_control as window_control
from core.window_control import WindowController


def _controller() -> WindowController:
    return WindowController(automation=MagicMock(), launcher=MagicMock())


def test_verify_window_state_uses_show_cmd_when_iszoomed_missing(monkeypatch):
    fake_gui = SimpleNamespace(
        GetWindowPlacement=lambda hwnd: (0, 3, (0, 0), (0, 0), (0, 0, 0, 0)),
        IsIconic=lambda hwnd: False,
    )
    monkeypatch.setattr(window_control, "_WIN32_OK", True)
    monkeypatch.setattr(window_control, "win32gui", fake_gui)
    monkeypatch.setattr(window_control, "_USER32", None)

    controller = _controller()

    assert controller._verify_window_state(101, "maximize_app") is True


def test_verify_window_state_uses_user32_iszoomed_fallback(monkeypatch):
    fake_gui = SimpleNamespace(
        GetWindowPlacement=lambda hwnd: (0, 1, (0, 0), (0, 0), (0, 0, 0, 0)),
        IsIconic=lambda hwnd: False,
    )
    fake_user32 = SimpleNamespace(IsZoomed=lambda hwnd: 1)
    monkeypatch.setattr(window_control, "_WIN32_OK", True)
    monkeypatch.setattr(window_control, "win32gui", fake_gui)
    monkeypatch.setattr(window_control, "_USER32", fake_user32)

    controller = _controller()

    assert controller._verify_window_state(202, "maximize_app") is True


def test_verify_window_state_treats_showminimized_as_minimized(monkeypatch):
    fake_gui = SimpleNamespace(
        GetWindowPlacement=lambda hwnd: (0, 2, (0, 0), (0, 0), (0, 0, 0, 0)),
        IsIconic=lambda hwnd: False,
    )
    monkeypatch.setattr(window_control, "_WIN32_OK", True)
    monkeypatch.setattr(window_control, "win32gui", fake_gui)
    monkeypatch.setattr(window_control, "_USER32", None)

    controller = _controller()

    assert controller._verify_window_state(303, "minimize_app") is True


def test_toggle_app_requires_verified_launch():
    controller = _controller()
    controller._automation.get_foreground_window.return_value = 0
    controller._automation.get_window.return_value = None
    controller._automation.wait_for.return_value = False
    controller.find_windows = MagicMock(return_value=[])
    controller.find_processes = MagicMock(return_value=[])
    controller._launcher.launch_app.return_value = SimpleNamespace(
        success=True,
        message="Opening Chrome.",
        pid=4444,
        path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    )

    result = controller.toggle_app("chrome")

    assert result.success is False
    assert result.error == "launch_not_verified"


def test_close_browser_reports_already_closed_when_not_running():
    controller = _controller()
    controller.find_windows = MagicMock(return_value=[])
    controller.find_processes = MagicMock(return_value=[])

    result = controller.close_app("chrome")

    assert result.success is True
    assert result.message == "Chrome is already closed."


def test_find_windows_title_fallback_does_not_match_unrelated_process():
    controller = _controller()

    def _list_windows(*, process_names=None, title_substrings=None):
        if process_names:
            return []
        if title_substrings:
            return [
                window_control.WindowTarget(
                    hwnd=101,
                    title="Google Chrome",
                    process_id=1,
                    process_name="assistant.exe",
                    rect=(0, 0, 100, 100),
                    is_visible=True,
                    is_minimized=False,
                )
            ]
        return []

    controller._automation.list_windows.side_effect = _list_windows

    assert controller.find_windows("chrome") == []
