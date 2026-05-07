"""
Low-level Windows desktop automation primitives.

This module provides a small, reusable automation layer for:

1. Window discovery and focus
2. Mouse clicks
3. Keyboard input
4. Scroll input
5. Timing / wait helpers

The implementation is Win32-first and uses optional third-party libraries
when they are available, but never depends on placeholder coordinates alone.
"""
from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from core import settings
from core.logger import get_logger
from core.process_utils import get_process_info

logger = get_logger(__name__)

try:
    import keyboard as keyboard_lib
except ImportError:  # pragma: no cover - optional dependency
    keyboard_lib = None

try:
    import pyautogui as pyautogui_lib

    pyautogui_lib.FAILSAFE = False
except ImportError:  # pragma: no cover - optional dependency
    pyautogui_lib = None

try:
    import win32api
    import win32clipboard
    import win32con
    import win32gui
    import win32process

    _WIN32_OK = True
except ImportError:  # pragma: no cover - exercised via fakes in tests
    win32api = None
    win32clipboard = None
    win32con = None
    win32gui = None
    win32process = None
    _WIN32_OK = False

_USER32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
_KERNEL32 = ctypes.windll.kernel32 if hasattr(ctypes, "windll") else None


@dataclass
class WindowTarget:
    """Basic metadata for a top-level desktop window."""

    hwnd: int
    title: str
    process_id: int
    process_name: str
    rect: tuple[int, int, int, int]
    is_visible: bool
    is_minimized: bool

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])


class DesktopAutomation:
    """Low-level desktop automation helpers used by higher-level controllers."""

    def list_windows(
        self,
        *,
        process_names: Optional[Iterable[str]] = None,
        title_substrings: Optional[Iterable[str]] = None,
    ) -> list[WindowTarget]:
        if not _WIN32_OK:
            return []

        normalized_processes = {_normalize_process_name(name) for name in (process_names or []) if name}
        normalized_titles = [str(part or "").strip().lower() for part in (title_substrings or []) if str(part or "").strip()]
        windows: list[WindowTarget] = []

        def _callback(hwnd: int, _extra: int) -> bool:
            try:
                if not win32gui.IsWindow(hwnd):
                    return True

                title = win32gui.GetWindowText(hwnd) or ""
                is_visible = bool(win32gui.IsWindowVisible(hwnd))
                is_minimized = bool(win32gui.IsIconic(hwnd))

                if not is_visible and not is_minimized:
                    return True

                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                process_info = get_process_info(pid) if pid else None
                process_name = process_info.exe_name if process_info else ""

                if normalized_processes and _normalize_process_name(process_name) not in normalized_processes:
                    return True

                if normalized_titles:
                    title_l = title.lower()
                    if not any(fragment in title_l for fragment in normalized_titles):
                        return True

                rect = win32gui.GetWindowRect(hwnd)
                windows.append(
                    WindowTarget(
                        hwnd=hwnd,
                        title=title,
                        process_id=pid,
                        process_name=process_name,
                        rect=rect,
                        is_visible=is_visible,
                        is_minimized=is_minimized,
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive OS access
                logger.debug("Window enumeration skipped a window: %s", exc)
            return True

        win32gui.EnumWindows(_callback, 0)
        foreground = self.get_foreground_window()
        windows.sort(key=lambda item: (item.hwnd != foreground, not item.title, item.is_minimized))
        return windows

    def get_foreground_window(self) -> int:
        if not _WIN32_OK:
            return 0
        try:
            return int(win32gui.GetForegroundWindow() or 0)
        except Exception as exc:  # pragma: no cover - defensive OS access
            logger.debug("GetForegroundWindow failed: %s", exc)
            return 0

    def get_window(self, hwnd: int) -> Optional[WindowTarget]:
        if not _WIN32_OK or not hwnd:
            return None

        try:
            title = win32gui.GetWindowText(hwnd) or ""
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process_info = get_process_info(pid) if pid else None
            process_name = process_info.exe_name if process_info else ""
            rect = win32gui.GetWindowRect(hwnd)
            return WindowTarget(
                hwnd=hwnd,
                title=title,
                process_id=pid,
                process_name=process_name,
                rect=rect,
                is_visible=bool(win32gui.IsWindowVisible(hwnd)),
                is_minimized=bool(win32gui.IsIconic(hwnd)),
            )
        except Exception as exc:  # pragma: no cover - defensive OS access
            logger.debug("get_window(%s) failed: %s", hwnd, exc)
            return None

    def focus_window(
        self,
        *,
        title: str | None = None,
        process: str | None = None,
        hwnd: int | None = None,
        timeout: float | None = None,
    ) -> Optional[WindowTarget]:
        if not _WIN32_OK:
            return None

        target: Optional[WindowTarget] = None
        if hwnd:
            target = self.get_window(hwnd)
        elif process or title:
            matches = self.list_windows(
                process_names=[process] if process else None,
                title_substrings=[title] if title else None,
            )
            target = matches[0] if matches else None

        if not target:
            return None

        try:
            if target.is_minimized:
                win32gui.ShowWindow(target.hwnd, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(target.hwnd, win32con.SW_SHOW)
        except Exception as exc:  # pragma: no cover - defensive OS access
            logger.debug("ShowWindow failed for %s: %s", target.hwnd, exc)

        self._force_foreground(target.hwnd)

        timeout = float(timeout or settings.get("browser_focus_timeout") or 5.0)
        focused = self.wait_for(lambda: self.get_foreground_window() == target.hwnd, timeout=timeout)
        if not focused:
            return None
        return self.get_window(target.hwnd)

    def click_center(self, rect: tuple[int, int, int, int]) -> bool:
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            return False
        center_x = left + (right - left) // 2
        center_y = top + (bottom - top) // 2
        return self.click_point(center_x, center_y)

    def move_point(self, x: int, y: int) -> bool:
        if pyautogui_lib is not None:  # pragma: no branch - straightforward happy path
            try:
                pyautogui_lib.moveTo(x=int(x), y=int(y))
                return True
            except Exception as exc:  # pragma: no cover - depends on desktop state
                logger.debug("pyautogui move failed: %s", exc)

        if not _WIN32_OK:
            return False

        try:
            win32api.SetCursorPos((int(x), int(y)))
            return True
        except Exception as exc:  # pragma: no cover - depends on desktop state
            logger.debug("Win32 mouse move failed: %s", exc)
            return False

    def click_point(self, x: int, y: int, *, button: str = "left", double: bool = False) -> bool:
        if pyautogui_lib is not None:  # pragma: no branch - straightforward happy path
            try:
                pyautogui_lib.click(x=x, y=y, clicks=2 if double else 1, button=button)
                return True
            except Exception as exc:  # pragma: no cover - depends on desktop state
                logger.debug("pyautogui click failed: %s", exc)

        if not _WIN32_OK:
            return False

        down_flag, up_flag = _mouse_flags(button)
        if down_flag is None or up_flag is None:
            return False

        try:
            win32api.SetCursorPos((int(x), int(y)))
            self.safe_sleep(40)
            click_count = 2 if double else 1
            for _ in range(click_count):
                win32api.mouse_event(down_flag, 0, 0, 0, 0)
                win32api.mouse_event(up_flag, 0, 0, 0, 0)
                self.safe_sleep(30)
            return True
        except Exception as exc:  # pragma: no cover - depends on desktop state
            logger.debug("Win32 click failed: %s", exc)
            return False

    def type_text(self, text: str, *, clear: bool = False, delay_ms: int | None = None) -> bool:
        if text is None:
            return False

        if clear:
            self.hotkey(["ctrl", "a"])
            self.safe_sleep(40)

        delay_ms = int(delay_ms if delay_ms is not None else settings.get("typing_delay_ms") or 20)
        text = str(text)

        if keyboard_lib is not None and _is_ascii(text):
            try:
                keyboard_lib.write(text, delay=max(0.0, delay_ms / 1000.0))
                return True
            except Exception as exc:  # pragma: no cover - depends on keyboard hook state
                logger.debug("keyboard.write failed: %s", exc)

        if pyautogui_lib is not None and _is_ascii(text):
            try:
                pyautogui_lib.write(text, interval=max(0.0, delay_ms / 1000.0))
                return True
            except Exception as exc:  # pragma: no cover - depends on desktop state
                logger.debug("pyautogui.write failed: %s", exc)

        if _WIN32_OK and win32clipboard is not None:
            previous_clipboard = self._get_clipboard_text()
            try:
                self._set_clipboard_text(text)
                if self.hotkey(["ctrl", "v"]):
                    self.safe_sleep(50)
                    return True
            finally:
                if previous_clipboard is not None:
                    self._set_clipboard_text(previous_clipboard)

        return False

    def press_key(self, key: str) -> bool:
        normalized = _normalize_key_name(key)
        if not normalized:
            return False

        if keyboard_lib is not None:
            try:
                keyboard_lib.send(normalized)
                return True
            except Exception as exc:  # pragma: no cover - depends on keyboard hook state
                logger.debug("keyboard.send failed for %s: %s", normalized, exc)

        if pyautogui_lib is not None:
            try:
                pyautogui_lib.press(normalized)
                return True
            except Exception as exc:  # pragma: no cover - depends on desktop state
                logger.debug("pyautogui.press failed for %s: %s", normalized, exc)

        return self._send_vk_sequence([normalized])

    def hotkey(self, keys: Iterable[str]) -> bool:
        normalized = [_normalize_key_name(key) for key in keys if _normalize_key_name(key)]
        if not normalized:
            return False

        if keyboard_lib is not None:
            try:
                keyboard_lib.send("+".join(normalized))
                return True
            except Exception as exc:  # pragma: no cover - depends on keyboard hook state
                logger.debug("keyboard.send hotkey failed for %s: %s", normalized, exc)

        if pyautogui_lib is not None:
            try:
                pyautogui_lib.hotkey(*normalized)
                return True
            except Exception as exc:  # pragma: no cover - depends on desktop state
                logger.debug("pyautogui.hotkey failed for %s: %s", normalized, exc)

        return self._send_vk_hotkey(normalized)

    def scroll(self, amount: int) -> bool:
        if pyautogui_lib is not None:
            try:
                pyautogui_lib.scroll(int(amount))
                return True
            except Exception as exc:  # pragma: no cover - depends on desktop state
                logger.debug("pyautogui.scroll failed: %s", exc)

        if not _WIN32_OK:
            return False

        try:
            win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, int(amount), 0)
            return True
        except Exception as exc:  # pragma: no cover - depends on desktop state
            logger.debug("Win32 scroll failed: %s", exc)
            return False

    def wait_for(
        self,
        condition: Callable[[], bool],
        timeout: float,
        *,
        interval: float = 0.05,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() <= deadline:
            try:
                if condition():
                    return True
            except Exception:
                pass
            time.sleep(max(0.01, interval))
        return False

    def safe_sleep(self, duration_ms: int | None = None) -> None:
        """Sleep for automation timing - reduced from 50ms to 25ms for better performance."""
        delay_ms = int(duration_ms if duration_ms is not None else settings.get("automation_action_delay_ms") or 25)
        time.sleep(max(0.0, delay_ms / 1000.0))

    def fast_sleep(self, duration_ms: int = 10) -> None:
        """Fast sleep for non-critical delays - use when verification is not needed."""
        time.sleep(max(0.0, duration_ms / 1000.0))

    def get_clipboard_text(self) -> str | None:
        return self._get_clipboard_text()

    def set_clipboard_text(self, text: str) -> None:
        self._set_clipboard_text(text)

    def _force_foreground(self, hwnd: int) -> None:
        if not _WIN32_OK or not hwnd:
            return

        try:
            win32gui.BringWindowToTop(hwnd)
        except Exception:
            pass

        try:
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_TOPMOST,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
            )
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_NOTOPMOST,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
            )
        except Exception:
            pass

        attached_threads: list[tuple[int, int]] = []
        try:
            foreground_hwnd = self.get_foreground_window()
            current_thread = int(_KERNEL32.GetCurrentThreadId()) if _KERNEL32 is not None else 0
            if foreground_hwnd and _USER32 is not None and win32process is not None:
                foreground_thread, _ = win32process.GetWindowThreadProcessId(foreground_hwnd)
                target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
                for other_thread in {foreground_thread, target_thread}:
                    if other_thread and current_thread and other_thread != current_thread:
                        if _USER32.AttachThreadInput(current_thread, other_thread, True):
                            attached_threads.append((current_thread, other_thread))

            try:
                win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            except Exception:
                pass

            for setter in (win32gui.SetForegroundWindow, win32gui.SetActiveWindow, win32gui.SetFocus):
                try:
                    setter(hwnd)
                except Exception:
                    continue
        finally:
            if _USER32 is not None:
                for src, dst in attached_threads:
                    try:
                        _USER32.AttachThreadInput(src, dst, False)
                    except Exception:
                        pass

    def _send_vk_sequence(self, keys: list[str]) -> bool:
        if not _WIN32_OK:
            return False

        try:
            for key in keys:
                vk = _virtual_key(key)
                if vk is None:
                    return False
                win32api.keybd_event(vk, 0, 0, 0)
                win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
            return True
        except Exception as exc:  # pragma: no cover - depends on desktop state
            logger.debug("Virtual key send failed: %s", exc)
            return False

    def _send_vk_hotkey(self, keys: list[str]) -> bool:
        if not _WIN32_OK:
            return False

        pressed: list[int] = []
        try:
            for key in keys:
                vk = _virtual_key(key)
                if vk is None:
                    return False
                win32api.keybd_event(vk, 0, 0, 0)
                pressed.append(vk)
            return True
        except Exception as exc:  # pragma: no cover - depends on desktop state
            logger.debug("Virtual hotkey send failed: %s", exc)
            return False
        finally:
            for vk in reversed(pressed):
                try:
                    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
                except Exception:
                    pass

    def _get_clipboard_text(self) -> str | None:
        if not _WIN32_OK or win32clipboard is None:
            return None

        for _ in range(3):
            try:
                win32clipboard.OpenClipboard()
                try:
                    if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                        return str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT))
                    return None
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:
                time.sleep(0.05)
        return None

    def _set_clipboard_text(self, text: str) -> None:
        if not _WIN32_OK or win32clipboard is None:
            return

        for _ in range(3):
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardText(str(text), win32con.CF_UNICODETEXT)
                    return
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:
                time.sleep(0.05)


def _normalize_process_name(name: str | None) -> str:
    cleaned = str(name or "").strip().lower()
    if cleaned and not cleaned.endswith(".exe"):
        cleaned = f"{cleaned}.exe"
    return cleaned


def _normalize_key_name(name: str | None) -> str:
    key = str(name or "").strip().lower().replace("_", " ")
    aliases = {
        "return": "enter",
        "escape": "esc",
        "pageup": "page up",
        "pgup": "page up",
        "page down": "page down",
        "pagedown": "page down",
        "pgdn": "page down",
        "control": "ctrl",
        "controlormeta": "ctrl",
        "media playpause": "media play pause",
        "play pause media": "media play pause",
        "play/pause media": "media play pause",
        "media next track": "media next",
        "next track": "media next",
        "media previous track": "media previous",
        "previous track": "media previous",
        "volume mute": "media mute",
        "mute volume": "media mute",
        "volume up": "media volume up",
        "volume down": "media volume down",
    }
    return aliases.get(key, key)


def _mouse_flags(button: str) -> tuple[int | None, int | None]:
    if not _WIN32_OK:
        return None, None

    normalized = str(button or "left").strip().lower()
    mapping = {
        "left": (win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP),
        "right": (win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP),
        "middle": (win32con.MOUSEEVENTF_MIDDLEDOWN, win32con.MOUSEEVENTF_MIDDLEUP),
    }
    return mapping.get(normalized, (None, None))


def _virtual_key(name: str) -> int | None:
    key = _normalize_key_name(name)
    if not _WIN32_OK:
        return None

    mapping = {
        "ctrl": win32con.VK_CONTROL,
        "shift": win32con.VK_SHIFT,
        "alt": win32con.VK_MENU,
        "enter": win32con.VK_RETURN,
        "tab": win32con.VK_TAB,
        "esc": win32con.VK_ESCAPE,
        "left": win32con.VK_LEFT,
        "right": win32con.VK_RIGHT,
        "up": win32con.VK_UP,
        "down": win32con.VK_DOWN,
        "page up": win32con.VK_PRIOR,
        "page down": win32con.VK_NEXT,
        "home": win32con.VK_HOME,
        "end": win32con.VK_END,
        "f5": win32con.VK_F5,
        "media play pause": win32con.VK_MEDIA_PLAY_PAUSE,
        "media next": win32con.VK_MEDIA_NEXT_TRACK,
        "media previous": win32con.VK_MEDIA_PREV_TRACK,
        "media mute": win32con.VK_VOLUME_MUTE,
        "media volume up": win32con.VK_VOLUME_UP,
        "media volume down": win32con.VK_VOLUME_DOWN,
    }
    if key in mapping:
        return mapping[key]

    if len(key) == 1:
        return ord(key.upper())
    return None


def _is_ascii(text: str) -> bool:
    return all(ord(ch) < 128 for ch in text)
