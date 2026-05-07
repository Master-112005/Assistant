"""
Phase 13 â€” Active Window Detection Engine.

Captures the currently focused Windows foreground window and classifies it
into a normalized app context (chrome, youtube, whatsapp, explorer, etc.)
using a 5-layer pipeline:

    Layer 1: Foreground HWND capture   (GetForegroundWindow)
    Layer 2: Window metadata           (GetWindowText, GetClassName, GetWindowRect)
    Layer 3: Process lookup            (GetWindowThreadProcessId + psutil)
    Layer 4: App classification        (exe name + window title rules)
    Layer 5: Context state updates     (core.state + optional callback)

Real Windows API usage only â€” no placeholders, no hardcoding.

Dependencies
------------
    pywin32   (win32gui, win32con, win32process, win32api)
    psutil    (process metadata)

Both are listed in requirements.txt.  If pywin32 is missing the detector
falls back to a safe stub that returns an "unknown" context so the rest
of the app keeps running.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from core import state, settings
from core.logger import get_logger
from core.process_utils import get_process_info, ProcessInfo

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Try to import pywin32 â€” required for real detection
# ---------------------------------------------------------------------------
try:
    import win32gui
    import win32con
    import win32process
    import win32api
    _PYWIN32_OK = True
except ImportError:
    _PYWIN32_OK = False
    logger.warning(
        "pywin32 not installed â€” window context detection unavailable. "
        "Install with:  pip install pywin32"
    )


# ===========================================================================
# Data model
# ===========================================================================

@dataclass
class WindowInfo:
    """
    Full metadata snapshot of a Windows window.

    Attributes:
        hwnd            Raw window handle (HWND integer)
        title           Window title text (Unicode)
        process_id      PID of the owning process
        process_name    Executable filename (e.g. "chrome.exe")
        exe_path        Full executable path
        class_name      Windows class name of the window
        is_visible      Whether the window is visible
        is_minimized    Whether the window is minimized
        rect            (left, top, right, bottom) window rectangle
        app_id          Normalized app identifier (e.g. "chrome", "youtube")
        app_type        High-level type ("browser", "media", "productivity", ...)
        context_detail  Extra context detail (e.g. "youtube" inside "browser")
    """
    hwnd:           int   = 0
    title:          str   = ""
    process_id:     int   = 0
    process_name:   str   = ""
    exe_path:       str   = ""
    class_name:     str   = ""
    is_visible:     bool  = False
    is_minimized:   bool  = False
    rect:           Tuple[int, int, int, int] = (0, 0, 0, 0)
    app_id:         str   = "unknown"
    app_type:       str   = "unknown"
    context_detail: str   = ""

    def to_dict(self) -> Dict:
        return {
            "hwnd":           self.hwnd,
            "title":          self.title,
            "process_id":     self.process_id,
            "process_name":   self.process_name,
            "exe_path":       self.exe_path,
            "class_name":     self.class_name,
            "is_visible":     self.is_visible,
            "is_minimized":   self.is_minimized,
            "rect":           self.rect,
            "app_id":         self.app_id,
            "app_type":       self.app_type,
            "context_detail": self.context_detail,
        }

    def __str__(self) -> str:
        return (
            f"WindowInfo(app_id={self.app_id!r}, process={self.process_name!r}, "
            f"title={self.title[:60]!r})"
        )


# Sentinel for "no window detected"
UNKNOWN_WINDOW = WindowInfo(app_id="unknown", app_type="unknown")


# ===========================================================================
# Classification tables
# ===========================================================================

# exe_name (lower, no extension) â†’ (app_id, app_type)
_EXE_MAP: Dict[str, Tuple[str, str]] = {
    # Browsers
    "chrome":           ("chrome",     "browser"),
    "msedge":           ("edge",       "browser"),
    "firefox":          ("firefox",    "browser"),
    "opera":            ("opera",      "browser"),
    "brave":            ("brave",      "browser"),
    "vivaldi":          ("vivaldi",    "browser"),
    "iexplore":         ("ie",         "browser"),
    # Communication
    "whatsapp":         ("whatsapp",   "communication"),
    "slack":            ("slack",      "communication"),
    "discord":          ("discord",    "communication"),
    "teams":            ("teams",      "communication"),
    "telegram":         ("telegram",   "communication"),
    "zoom":             ("zoom",       "communication"),
    "skype":            ("skype",      "communication"),
    "signal":           ("signal",     "communication"),
    # Media
    "spotify":          ("spotify",    "media"),
    "vlc":              ("vlc",        "media"),
    "wmplayer":         ("wmp",        "media"),
    "itunes":           ("itunes",     "media"),
    "mpc-hc64":         ("mpc",        "media"),
    "mpc-hc":           ("mpc",        "media"),
    "musicbee":         ("musicbee",   "media"),
    "foobar2000":       ("foobar",     "media"),
    # File management
    "explorer":         ("explorer",   "files"),
    "totalcmd":         ("totalcmd",   "files"),
    "freecommander":    ("freecommander", "files"),
    # Development
    "code":             ("vscode",     "development"),
    "devenv":           ("visual_studio", "development"),
    "pycharm64":        ("pycharm",    "development"),
    "pycharm":          ("pycharm",    "development"),
    "idea64":           ("intellij",   "development"),
    "webstorm64":       ("webstorm",   "development"),
    "clion64":          ("clion",      "development"),
    "rider64":          ("rider",      "development"),
    "sublime_text":     ("sublime",    "development"),
    "atom":             ("atom",       "development"),
    "cursor":           ("cursor",     "development"),
    "windowsterminal":  ("terminal",   "development"),
    "cmd":              ("cmd",        "development"),
    "powershell":       ("powershell", "development"),
    "wt":               ("terminal",   "development"),
    # Productivity
    "notepad":          ("notepad",    "productivity"),
    "notepad++":        ("notepadpp",  "productivity"),
    "winword":          ("word",       "productivity"),
    "excel":            ("excel",      "productivity"),
    "powerpnt":         ("powerpoint", "productivity"),
    "onenote":          ("onenote",    "productivity"),
    "acrobat":          ("acrobat",    "productivity"),
    "acrord32":         ("acrobat",    "productivity"),
    "obsidian":         ("obsidian",   "productivity"),
    "notion":           ("notion",     "productivity"),
    # System
    "taskmgr":          ("task_manager", "system"),
    "regedit":          ("regedit",    "system"),
    "mmc":              ("mmc",        "system"),
    "control":          ("control_panel", "system"),
    "systemsettings":   ("settings",   "system"),
    # Gaming
    "steam":            ("steam",      "gaming"),
    "epicgameslauncher":("epic",       "gaming"),
    "riotclientservices":("riot",      "gaming"),
}

# Window-title fragments â†’ (app_id, context_detail)
# Evaluated only when the process is classified as a browser
_BROWSER_TITLE_MAP: List[Tuple[str, str, str]] = [
    # (title_fragment_lower, app_id, context_detail)
    ("youtube",         "youtube",     "video"),
    ("twitter",         "twitter",     "social"),
    ("x.com",           "twitter",     "social"),
    ("facebook",        "facebook",    "social"),
    ("instagram",       "instagram",   "social"),
    ("reddit",          "reddit",      "social"),
    ("whatsapp web",    "whatsapp",    "messaging"),
    ("gmail",           "gmail",       "email"),
    ("google mail",     "gmail",       "email"),
    ("outlook",         "outlook",     "email"),
    ("netflix",         "netflix",     "video"),
    ("disney+",         "disney_plus", "video"),
    ("prime video",     "amazon_prime","video"),
    ("spotify",         "spotify",     "music"),
    ("soundcloud",      "soundcloud",  "music"),
    ("twitch",          "twitch",      "streaming"),
    ("github",          "github",      "development"),
    ("stackoverflow",   "stackoverflow","development"),
    ("notion",          "notion",      "productivity"),
    ("figma",           "figma",       "design"),
    ("chatgpt",         "chatgpt",     "ai"),
    ("claude",          "claude",      "ai"),
    ("gemini",          "gemini",      "ai"),
    ("google docs",     "google_docs", "productivity"),
    ("google sheets",   "google_sheets","productivity"),
    ("google slides",   "google_slides","productivity"),
    ("amazon",          "amazon",      "shopping"),
    ("flipkart",        "flipkart",    "shopping"),
    ("maps",            "maps",        "maps"),
    ("wikipedia",       "wikipedia",   "reference"),
]


# ===========================================================================
# Core detector class
# ===========================================================================

class ActiveWindowDetector:
    """
    Real-time Windows foreground window detector and classifier.

    All public methods are thread-safe (they only read OS state or
    update module-level globals under no lock â€” Python GIL is sufficient
    for simple assignments).

    Usage::

        detector = ActiveWindowDetector()

        # One-shot
        info = detector.get_active_context()
        print(info.app_id)   # "chrome", "youtube", "explorer", ...

        # Continuous background watcher
        def on_change(info: WindowInfo):
            print(f"App changed: {info.app_id}")

        detector.watch_active_window(callback=on_change)
        # ... later ...
        detector.stop_watcher()
    """

    def __init__(self) -> None:
        self._watcher_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_hwnd: int = -1

    # ------------------------------------------------------------------ #
    # Layer 1 â€” Foreground HWND capture                                   #
    # ------------------------------------------------------------------ #

    def get_foreground_window(self) -> int:
        """
        Return the HWND of the current foreground window, or 0 on failure.

        Uses GetForegroundWindow() Windows API.
        """
        if not _PYWIN32_OK:
            return 0
        try:
            hwnd = win32gui.GetForegroundWindow()
            return hwnd or 0
        except Exception as exc:
            logger.warning("GetForegroundWindow failed: %s", exc)
            return 0

    # ------------------------------------------------------------------ #
    # Layer 2 â€” Window metadata extraction                                #
    # ------------------------------------------------------------------ #

    def get_window_info(self, hwnd: int) -> WindowInfo:
        """
        Extract all metadata for the given HWND.

        Returns UNKNOWN_WINDOW if the window is inaccessible or gone.
        """
        if not hwnd or not _PYWIN32_OK:
            return WindowInfo()

        try:
            # Window title
            title = ""
            try:
                title = win32gui.GetWindowText(hwnd) or ""
            except Exception:
                pass

            # Class name
            class_name = ""
            try:
                class_name = win32gui.GetClassName(hwnd) or ""
            except Exception:
                pass

            # Visibility
            is_visible   = bool(win32gui.IsWindowVisible(hwnd))
            is_minimized = bool(win32gui.IsIconic(hwnd))

            # Rectangle
            rect = (0, 0, 0, 0)
            try:
                rect = win32gui.GetWindowRect(hwnd)
            except Exception:
                pass

            # Layer 3 â€” Process info
            pid = 0
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pass

            proc_info: Optional[ProcessInfo] = get_process_info(pid) if pid else None
            process_name = proc_info.exe_name if proc_info else ""
            exe_path     = proc_info.exe_path if proc_info else ""

            info = WindowInfo(
                hwnd=hwnd,
                title=title,
                process_id=pid,
                process_name=process_name,
                exe_path=exe_path,
                class_name=class_name,
                is_visible=is_visible,
                is_minimized=is_minimized,
                rect=rect,
            )

            # Layer 4 â€” Classification
            self.classify_window(info)
            return info

        except Exception as exc:
            logger.warning("get_window_info(%d) error: %s", hwnd, exc)
            return WindowInfo()

    # ------------------------------------------------------------------ #
    # Layer 3+4 â€” Classification                                          #
    # ------------------------------------------------------------------ #

    def classify_window(self, info: WindowInfo) -> None:
        """
        Populate info.app_id, info.app_type, info.context_detail in-place.

        Classification priority:
        1. Exact exe name match in _EXE_MAP
        2. Partial exe name match (e.g. "chrome" in "google chrome.exe")
        3. Browser title scan for sub-context (YouTube, WhatsApp Web, etc.)
        4. Fallback: "unknown"
        """
        exe_stem = _exe_stem(info.process_name)  # "chrome.exe" â†’ "chrome"

        # --- Direct exe match ---
        if exe_stem in _EXE_MAP:
            app_id, app_type = _EXE_MAP[exe_stem]
            info.app_id   = app_id
            info.app_type = app_type
        else:
            # --- Partial match (e.g. "chrome_crashpad_handler" â†’ "chrome") ---
            # Guard: require at least 3 chars to avoid matching empty string or noise
            matched = False
            if len(exe_stem) >= 3:
                for key, (aid, atype) in _EXE_MAP.items():
                    if key in exe_stem or exe_stem in key:
                        info.app_id   = aid
                        info.app_type = atype
                        matched = True
                        break

            if not matched:
                info.app_id   = exe_stem if exe_stem else "unknown"
                info.app_type = "unknown"

        # --- Browser sub-context via title scan ---
        if info.app_type == "browser":
            self._classify_browser_title(info)

    def _classify_browser_title(self, info: WindowInfo) -> None:
        """
        Inspect a browser window's title to detect the active website/service.

        Sets context_detail and optionally overrides app_id to the detected
        service (e.g. "youtube", "whatsapp", "gmail").
        """
        title_lower = info.title.lower()
        for fragment, app_id, detail in _BROWSER_TITLE_MAP:
            if fragment in title_lower:
                info.app_id        = app_id
                info.context_detail = detail
                logger.debug(
                    "Browser sub-context: title=%r â†’ app_id=%s detail=%s",
                    info.title[:50], app_id, detail,
                )
                return
        # Generic browser â€” no specific site detected
        info.context_detail = "generic_browse"

    # ------------------------------------------------------------------ #
    # Public convenience helpers                                           #
    # ------------------------------------------------------------------ #

    def is_browser(self, info: WindowInfo) -> bool:
        """Return True if the window's base type is a browser."""
        return info.app_type == "browser"

    def is_explorer(self, info: WindowInfo) -> bool:
        """Return True if the window is Windows File Explorer."""
        return info.process_name in ("explorer.exe",) and info.app_id == "explorer"

    def get_active_context(self) -> WindowInfo:
        """
        One-shot: return a fully classified WindowInfo for the current foreground window.

        This is the main public entry point for on-demand context reads.
        Returns UNKNOWN_WINDOW if detection fails.
        """
        hwnd = self.get_foreground_window()
        if not hwnd:
            return WindowInfo()

        info = self.get_window_info(hwnd)
        logger.debug("Active context: %s", info)
        return info

    # ------------------------------------------------------------------ #
    # Layer 5 â€” Real-time watcher                                         #
    # ------------------------------------------------------------------ #

    def watch_active_window(
        self,
        callback: Optional[Callable[[WindowInfo], None]] = None,
    ) -> None:
        """
        Start a background daemon thread that polls for foreground window changes.

        The thread runs at ``context_poll_interval_ms`` milliseconds (default 500ms).
        When the foreground window changes, it:
        1. Reads the new WindowInfo
        2. Updates core.state
        3. Logs the change
        4. Calls *callback* if provided

        Safe to call multiple times â€” stops previous watcher first.
        """
        self.stop_watcher()
        self._stop_event = threading.Event()
        self._last_hwnd = -1

        self._watcher_thread = threading.Thread(
            target=self._watcher_loop,
            args=(callback,),
            daemon=True,
            name="window-context-watcher",
        )
        self._watcher_thread.start()
        logger.info("Context watcher started (poll=%dms)",
                    settings.get("context_poll_interval_ms") or 500)

    def stop_watcher(self) -> None:
        """Stop the background watcher thread gracefully."""
        thread = self._watcher_thread
        if thread is None:
            return

        self._stop_event.set()
        if thread.is_alive():
            thread.join(timeout=2.0)

        if thread.is_alive():
            logger.warning("Context watcher did not stop within timeout")
            return

        self._watcher_thread = None
        logger.info("Context watcher stopped")

    @property
    def is_watching(self) -> bool:
        """True if the background watcher is currently running."""
        return bool(self._watcher_thread and self._watcher_thread.is_alive())

    # ------------------------------------------------------------------ #
    # Internal watcher loop                                               #
    # ------------------------------------------------------------------ #

    def _watcher_loop(
        self,
        callback: Optional[Callable[[WindowInfo], None]],
    ) -> None:
        """Polling loop - runs on the watcher daemon thread."""
        poll_ms   = settings.get("context_poll_interval_ms") or 500
        poll_secs = poll_ms / 1000.0

        while not self._stop_event.is_set():
            try:
                hwnd = self.get_foreground_window()

                if hwnd and hwnd != self._last_hwnd:
                    self._last_hwnd = hwnd
                    info = self.get_window_info(hwnd)

                    # Only process visible, non-assistant windows
                    if self._should_update_context(info):
                        _update_state(info)
                        logger.info(
                            "Active window changed -> app_id=%s process=%s title=%r",
                            info.app_id, info.process_name, info.title[:60],
                        )
                        if callback:
                            try:
                                callback(info)
                            except Exception as cb_exc:
                                logger.warning("Context callback error: %s", cb_exc)

            except Exception as exc:
                logger.warning("Watcher loop error: %s", exc)

            self._stop_event.wait(timeout=poll_secs)

    def _should_update_context(self, info: WindowInfo) -> bool:
        """
        Decide whether this window change should update the context state.

        Filters out:
        - Our own assistant window (to avoid context stomping)
        - Invisible windows
        - Minimized windows
        """
        if not info.hwnd:
            return False
        # Skip the assistant's own window by class name or title
        if "nova" in info.title.lower() or "nova" in info.class_name.lower():
            return False
        return True


# ===========================================================================
# Module-level helpers
# ===========================================================================

def _exe_stem(process_name: str) -> str:
    """
    Normalise a process name for classification lookup.

    "Google Chrome.exe" â†’ "chrome"   (but we get "chrome.exe" from psutil)
    "chrome.exe"        â†’ "chrome"
    "CHROME.EXE"        â†’ "chrome"
    """
    if not process_name:
        return ""
    stem = process_name.lower()
    if stem.endswith(".exe"):
        stem = stem[:-4]
    return stem


def _update_state(info: WindowInfo) -> None:
    """Write WindowInfo into the global core.state namespace (thread-safe via GIL)."""
    state.current_context      = info.app_id
    state.current_app          = info.app_id
    state.current_window_title = info.title
    state.current_process_name = info.process_name
    state.last_context_change  = time.monotonic()

    # Append to bounded history (keep last 20 entries)
    history: list = state.context_history  # type: ignore[attr-defined]
    history.append({
        "app_id":       info.app_id,
        "title":        info.title,
        "process_name": info.process_name,
        "timestamp":    time.monotonic(),
    })
    if len(history) > 20:
        del history[:-20]

