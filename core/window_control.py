"""
Real Windows app and window control backends.
"""
from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass, field
from typing import Any

import psutil

from core import state
from core.app_launcher import APP_PROFILES, DesktopAppLauncher, app_display_name, app_process_names, canonicalize_app_name
from core.automation import DesktopAutomation, WindowTarget
from core.logger import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - Windows-only API access
    import win32con
    import win32gui

    _WIN32_OK = True
except Exception:  # pragma: no cover
    win32con = None
    win32gui = None
    _WIN32_OK = False

_USER32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


@dataclass(slots=True)
class WindowActionResult:
    success: bool
    action: str
    app_id: str
    message: str
    error: str = ""
    window_count: int = 0
    process_count: int = 0
    forced: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "action": self.action,
            "app_id": self.app_id,
            "message": self.message,
            "error": self.error,
            "window_count": self.window_count,
            "process_count": self.process_count,
            "forced": self.forced,
            "data": dict(self.data),
        }


class WindowController:
    """Production backend for app-window control on Windows."""

    def __init__(
        self,
        *,
        automation: DesktopAutomation | None = None,
        launcher: DesktopAppLauncher | None = None,
    ) -> None:
        self._automation = automation or DesktopAutomation()
        self._launcher = launcher or DesktopAppLauncher()

    def close_app(
        self,
        app_name: str,
        *,
        allow_force: bool = True,
        timeout: float = 2.5,
    ) -> WindowActionResult:
        action_started = time.perf_counter()
        app_id = canonicalize_app_name(app_name)
        label = app_display_name(app_id or app_name)
        initial_windows = self.find_windows(app_name)
        initial_processes = self.find_processes(app_name)
        logger.info(
            "Window close requested",
            app=app_id or app_name,
            found_windows=len(initial_windows),
            found_processes=len(initial_processes),
        )

        if not initial_windows and not initial_processes:
            already_closed_message = f"{label} is already closed." if (app_id or app_name) in {"chrome", "edge", "firefox", "brave"} else f"{label} is not running."
            result = WindowActionResult(
                success=True,
                action="close_app",
                app_id=app_id or app_name,
                message=already_closed_message,
                window_count=0,
                process_count=0,
                data={"target_app": app_id or app_name, "already_closed": True},
            )
            logger.info(
                "Window action completed",
                action=result.action,
                target=result.app_id,
                success=result.success,
                verified=result.success,
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
                error_code=result.error,
            )
            return result

        for window in initial_windows:
            self._post_close(window.hwnd)

        # Fast path: wait for windows to close (non-blocking check)
        if initial_windows:
            deadline = time.monotonic() + timeout
            remaining_windows = initial_windows
            while time.monotonic() <= deadline and remaining_windows:
                time.sleep(0.05)  # Reduced from 0.1s
                remaining_windows = self._fast_find_windows(app_name)
        else:
            remaining_windows = []

        # Only check processes if windows still remain
        remaining_processes = self.find_processes(app_name) if remaining_windows else []
        forced = False

        if remaining_processes and allow_force:
            forced = self._terminate_processes(remaining_processes)
            self._automation.wait_for(
                lambda: not self.find_processes(app_name),
                timeout=2.0,
                interval=0.1,
            )

        final_windows = self.find_windows(app_name)
        final_processes = self.find_processes(app_name)
        success = not final_windows and not final_processes

        if success:
            if forced:
                message = f"{label} did not respond. Forced close complete."
            elif len(initial_windows) > 1:
                message = f"Closed {len(initial_windows)} {label} windows."
            else:
                message = f"{label} closed successfully."
        else:
            message = f"I couldn't close {label}."

        self._remember_window_action(
            action="close_app",
            app_id=app_id or app_name,
            window_count=len(initial_windows),
            process_count=len(initial_processes),
        )
        result = WindowActionResult(
            success=success,
            action="close_app",
            app_id=app_id or app_name,
            message=message,
            error="" if success else "close_failed",
            window_count=len(initial_windows),
            process_count=len(initial_processes),
            forced=forced,
            data={
                "target_app": app_id or app_name,
                "initial_window_count": len(initial_windows),
                "initial_process_count": len(initial_processes),
                "final_window_count": len(final_windows),
                "final_process_count": len(final_processes),
            },
        )
        logger.info(
            "Window action completed",
            action=result.action,
            target=result.app_id,
            success=result.success,
            verified=result.success,
            duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            error_code=result.error,
            forced=result.forced,
            initial_window_count=len(initial_windows),
            final_window_count=len(final_windows),
            final_process_count=len(final_processes),
        )
        return result

    def minimize_app(self, app_name: str) -> WindowActionResult:
        return self._show_window_action(app_name, "minimize_app", win32con.SW_MINIMIZE if _WIN32_OK else 6)

    def maximize_app(self, app_name: str) -> WindowActionResult:
        return self._show_window_action(app_name, "maximize_app", win32con.SW_MAXIMIZE if _WIN32_OK else 3)

    def restore_app(self, app_name: str) -> WindowActionResult:
        return self._show_window_action(app_name, "restore_app", win32con.SW_RESTORE if _WIN32_OK else 9)

    def focus_app(self, app_name: str) -> WindowActionResult:
        action_started = time.perf_counter()
        app_id = canonicalize_app_name(app_name)
        label = app_display_name(app_id or app_name)
        target = self._select_target_window(app_name)
        if target is None:
            result = WindowActionResult(
                success=False,
                action="focus_app",
                app_id=app_id or app_name,
                message=f"{label} is not open.",
                error="app_not_running",
                data={"target_app": app_id or app_name},
            )
            logger.info(
                "Window action completed",
                action=result.action,
                target=result.app_id,
                success=result.success,
                verified=False,
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
                error_code=result.error,
            )
            return result

        focused = self._automation.focus_window(hwnd=target.hwnd, timeout=3.0)
        success = focused is not None and focused.hwnd == target.hwnd
        if success:
            self._remember_window_action("focus_app", app_id or app_name, 1, 1)
        result = WindowActionResult(
            success=success,
            action="focus_app",
            app_id=app_id or app_name,
            message=f"{label} focused." if success else f"I couldn't focus {label}.",
            error="" if success else "focus_failed",
            window_count=1,
            process_count=len(self.find_processes(app_name)),
            data={"target_app": app_id or app_name, "hwnd": int(target.hwnd)},
        )
        logger.info(
            "Window action completed",
            action=result.action,
            target=result.app_id,
            success=result.success,
            verified=result.success,
            duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            error_code=result.error,
        )
        return result

    def toggle_app(self, app_name: str) -> WindowActionResult:
        app_id = canonicalize_app_name(app_name)
        label = app_display_name(app_id or app_name)
        foreground = self._automation.get_window(self._automation.get_foreground_window())
        if foreground and self._matches_app(foreground.process_name, app_name):
            return self.minimize_app(app_name)

        windows = self.find_windows(app_name)
        if windows:
            return self.focus_app(app_name)

        launch_result = self._launcher.launch_app(app_name)
        if launch_result.success:
            verified = bool(
                self._automation.wait_for(
                    lambda: bool(self.find_windows(app_name) or self.find_processes(app_name)),
                    timeout=2.5,
                    interval=0.1,
                )
            )
            return WindowActionResult(
                success=verified,
                action="toggle_app",
                app_id=app_id or app_name,
                message=(
                    launch_result.message or f"Opening {label}."
                    if verified
                    else f"I couldn't verify that {label} opened."
                ),
                error="" if verified else "launch_not_verified",
                process_count=len(self.find_processes(app_name)),
                data={
                    "target_app": app_id or app_name,
                    "pid": launch_result.pid,
                    "path": launch_result.path,
                    "verified": verified,
                },
            )

        return WindowActionResult(
            success=False,
            action="toggle_app",
            app_id=app_id or app_name,
            message=launch_result.message,
            error=launch_result.error,
            data=dict(launch_result.data or {}),
        )

    def find_windows(self, app_name: str) -> list[WindowTarget]:
        process_names = app_process_names(app_name)
        windows = self._automation.list_windows(process_names=process_names)
        if windows:
            return windows

        profile = APP_PROFILES.get(canonicalize_app_name(app_name))
        title_fragments = []
        if profile is not None:
            title_fragments.append(profile.display_name.lower())
        else:
            cleaned = str(app_name or "").strip().lower()
            if cleaned:
                title_fragments.append(cleaned)
        if title_fragments:
            title_matches = self._automation.list_windows(title_substrings=title_fragments)
            if process_names:
                return [
                    window
                    for window in title_matches
                    if not str(window.process_name or "").strip()
                    or self._matches_app(window.process_name, app_name)
                ]
            return title_matches
        return []

    def _fast_find_windows(self, app_name: str) -> list[WindowTarget]:
        """Fast path for close verification - only check existing process names."""
        process_names = app_process_names(app_name)
        return self._automation.list_windows(process_names=process_names)

    def find_processes(self, app_name: str) -> list[psutil.Process]:
        process_names = {name.lower() for name in app_process_names(app_name)}
        matches: list[psutil.Process] = []
        for process in psutil.process_iter(["pid", "name", "exe"]):
            try:
                name = str(process.info.get("name") or "").strip().lower()
                exe_name = os.path.basename(str(process.info.get("exe") or "")).strip().lower()
                if name in process_names or exe_name in process_names:
                    matches.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return matches

    def _show_window_action(self, app_name: str, action: str, show_flag: int) -> WindowActionResult:
        action_started = time.perf_counter()
        app_id = canonicalize_app_name(app_name)
        label = app_display_name(app_id or app_name)
        target = self._select_target_window(app_name)
        if target is None or not _WIN32_OK:
            result = WindowActionResult(
                success=False,
                action=action,
                app_id=app_id or app_name,
                message=f"{label} is not open.",
                error="app_not_running",
                data={"target_app": app_id or app_name},
            )
            logger.info(
                "Window action completed",
                action=result.action,
                target=result.app_id,
                success=result.success,
                verified=False,
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
                error_code=result.error,
            )
            return result

        try:
            if action == "minimize_app" and win32gui.IsIconic(target.hwnd):
                result = WindowActionResult(
                    success=True,
                    action=action,
                    app_id=app_id or app_name,
                    message=f"{label} is already minimized.",
                    window_count=1,
                    process_count=len(self.find_processes(app_name)),
                    data={"target_app": app_id or app_name, "hwnd": int(target.hwnd)},
                )
                logger.info(
                    "Window action completed",
                    action=result.action,
                    target=result.app_id,
                    success=result.success,
                    verified=True,
                    duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
                    error_code=result.error,
                )
                return result
            win32gui.ShowWindow(target.hwnd, show_flag)
            if action in {"maximize_app", "restore_app"}:
                self._automation.focus_window(hwnd=target.hwnd, timeout=2.0)
            time.sleep(0.1)
        except Exception as exc:
            logger.warning("ShowWindow failed for %s: %s", target.hwnd, exc)
            result = WindowActionResult(
                success=False,
                action=action,
                app_id=app_id or app_name,
                message=f"I couldn't {action.split('_')[0]} {label}.",
                error=f"{action}_failed",
                data={"target_app": app_id or app_name, "hwnd": int(target.hwnd)},
            )
            logger.info(
                "Window action completed",
                action=result.action,
                target=result.app_id,
                success=result.success,
                verified=False,
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
                error_code=result.error,
            )
            return result

        verification = self._verify_window_state(target.hwnd, action)
        if verification:
            self._remember_window_action(action, app_id or app_name, 1, len(self.find_processes(app_name)))
        readable_action = action.replace("_app", "").replace("_", " ")
        result = WindowActionResult(
            success=verification,
            action=action,
            app_id=app_id or app_name,
            message=(
                f"{label} {readable_action.split()[0]}d."
                if verification and action != "restore_app"
                else f"{label} restored."
                if verification
                else f"I couldn't {readable_action} {label}."
            ),
            error="" if verification else f"{action}_failed",
            window_count=1,
            process_count=len(self.find_processes(app_name)),
            data={"target_app": app_id or app_name, "hwnd": int(target.hwnd)},
        )
        logger.info(
            "Window action completed",
            action=result.action,
            target=result.app_id,
            success=result.success,
            verified=verification,
            duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            error_code=result.error,
        )
        return result

    def _verify_window_state(self, hwnd: int, action: str) -> bool:
        if not _WIN32_OK:
            return False
        show_cmd = self._window_show_cmd(hwnd)
        try:
            if action == "minimize_app":
                return bool(win32gui.IsIconic(hwnd) or show_cmd in {2, 6, 7})
            if action == "maximize_app":
                if hasattr(win32gui, "IsZoomed"):
                    return bool(win32gui.IsZoomed(hwnd))
                if _USER32 is not None and hasattr(_USER32, "IsZoomed"):
                    return bool(_USER32.IsZoomed(int(hwnd)))
                return show_cmd == 3
            if action == "restore_app":
                return not bool(win32gui.IsIconic(hwnd)) and show_cmd not in {2, 6, 7}
        except Exception:
            return False
        return False

    def _window_show_cmd(self, hwnd: int) -> int | None:
        if not _WIN32_OK:
            return None
        try:
            placement = win32gui.GetWindowPlacement(hwnd)
        except Exception:
            return None
        if not isinstance(placement, tuple) or len(placement) < 2:
            return None
        try:
            return int(placement[1])
        except (TypeError, ValueError):
            return None

    def _select_target_window(self, app_name: str) -> WindowTarget | None:
        windows = self.find_windows(app_name)
        return windows[0] if windows else None

    def _terminate_processes(self, processes: list[psutil.Process]) -> bool:
        terminated = False
        for process in processes:
            try:
                process.terminate()
                terminated = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        _, alive = psutil.wait_procs(processes, timeout=2.0)
        for process in alive:
            try:
                process.kill()
                terminated = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return terminated

    def _post_close(self, hwnd: int) -> None:
        if not _WIN32_OK:
            return
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception as exc:
            logger.debug("WM_CLOSE failed for %s: %s", hwnd, exc)

    def _matches_app(self, process_name: str, app_name: str) -> bool:
        return str(process_name or "").strip().lower() in {name.lower() for name in app_process_names(app_name)}

    def _remember_window_action(self, action: str, app_id: str, window_count: int, process_count: int) -> None:
        state.last_window_action = {
            "action": action,
            "target_app": app_id,
            "window_count": int(window_count),
            "process_count": int(process_count),
            "timestamp": time.time(),
        }
        state.last_target_app = app_id
