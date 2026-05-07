"""
Real Windows screen capture helpers for OCR and desktop perception.
"""
from __future__ import annotations

import ctypes
import threading
from dataclasses import dataclass
from typing import Any, Callable

from PIL import Image, ImageGrab

from core.logger import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - optional dependency
    import mss

    _MSS_OK = True
except Exception:  # pragma: no cover - import guard
    mss = None
    _MSS_OK = False

try:  # pragma: no cover - Windows-only
    import win32gui

    _PYWIN32_OK = True
except Exception:  # pragma: no cover - import guard
    win32gui = None
    _PYWIN32_OK = False


DWMWA_EXTENDED_FRAME_BOUNDS = 9
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

_dpi_lock = threading.Lock()
_dpi_ready = False


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


@dataclass(frozen=True)
class CaptureBounds:
    left: int
    top: int
    width: int
    height: int
    hwnd: int = 0
    title: str = ""

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def as_bbox(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)

    def to_monitor(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


def _ensure_dpi_awareness() -> None:
    global _dpi_ready
    if _dpi_ready:
        return

    with _dpi_lock:
        if _dpi_ready:
            return
        try:  # pragma: no cover - Windows-only side effect
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:  # pragma: no cover - Windows-only side effect
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        _dpi_ready = True


class ScreenCapture:
    """High-level screen and active-window capture for Windows."""

    def __init__(
        self,
        *,
        mss_factory: Callable[[], Any] | None = None,
        image_grab: Callable[..., Image.Image] | None = None,
        active_window_getter: Callable[[bool], CaptureBounds | None] | None = None,
    ) -> None:
        _ensure_dpi_awareness()
        self._mss_factory = mss_factory or self._default_mss_factory
        self._mss_available = mss_factory is not None or _MSS_OK
        self._image_grab = image_grab or ImageGrab.grab
        self._active_window_getter = active_window_getter or self._get_active_window_bounds_impl
        self._lock = threading.RLock()

    def capture_fullscreen(self, monitor_index: int = 0) -> Image.Image:
        """Capture the virtual desktop or a specific monitor."""
        with self._lock:
            try:
                if self._mss_available and self._mss_factory is not None:
                    with self._mss_factory() as sct:
                        monitors = getattr(sct, "monitors", [])
                        if monitors:
                            if monitor_index <= 0 or monitor_index >= len(monitors):
                                monitor = monitors[0]
                            else:
                                monitor = monitors[monitor_index]
                            image = self._grab_with_mss(sct, monitor)
                            logger.info("Captured fullscreen")
                            return image
                image = self._image_grab(all_screens=True)
                logger.info("Captured fullscreen")
                return image.convert("RGB")
            except Exception as exc:
                logger.error("Fullscreen capture failed: %s", exc)
                raise RuntimeError(f"Fullscreen capture failed: {exc}") from exc

    def capture_active_window(self, *, client_area: bool = False) -> Image.Image:
        """Capture the active foreground window."""
        bounds = self.get_active_window_bounds(client_area=client_area)
        if bounds is None:
            raise RuntimeError("No active foreground window is available for capture.")

        image = self.capture_region(bounds.left, bounds.top, bounds.width, bounds.height)
        if self._looks_black(image):
            logger.warning("Active window capture appears black; retrying with Pillow fallback.")
            try:
                image = self._capture_region_with_image_grab(bounds.left, bounds.top, bounds.width, bounds.height)
            except Exception as exc:
                logger.warning("Pillow fallback capture failed: %s", exc)
        logger.info("Captured active window")
        return image

    def capture_region(self, x: int, y: int, w: int, h: int) -> Image.Image:
        """Capture an explicit screen region in global screen coordinates."""
        left = int(x)
        top = int(y)
        width = max(1, int(w))
        height = max(1, int(h))
        monitor = {"left": left, "top": top, "width": width, "height": height}

        with self._lock:
            try:
                if self._mss_available and self._mss_factory is not None:
                    with self._mss_factory() as sct:
                        image = self._grab_with_mss(sct, monitor)
                        return image
                return self._capture_region_with_image_grab(left, top, width, height)
            except Exception as exc:
                logger.error("Region capture failed: %s", exc)
                raise RuntimeError(f"Region capture failed: {exc}") from exc

    def save_image(self, image: Image.Image, path: str | Path) -> str:
        """Persist an image to disk and return the resolved path."""
        destination = Path(path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination)
        logger.info("Saved screenshot: %s", destination)
        return str(destination)

    def get_active_window_bounds(self, *, client_area: bool = False) -> CaptureBounds | None:
        return self._active_window_getter(client_area)

    def get_virtual_screen_bounds(self) -> CaptureBounds:
        win_dll = getattr(ctypes, "windll", None)
        if win_dll is not None:
            try:  # pragma: no cover - Windows-only branch
                metrics = win_dll.user32
                left = int(metrics.GetSystemMetrics(SM_XVIRTUALSCREEN))
                top = int(metrics.GetSystemMetrics(SM_YVIRTUALSCREEN))
                width = int(metrics.GetSystemMetrics(SM_CXVIRTUALSCREEN))
                height = int(metrics.GetSystemMetrics(SM_CYVIRTUALSCREEN))
                if width > 0 and height > 0:
                    return CaptureBounds(left=left, top=top, width=width, height=height)
            except Exception:
                pass

        image = self.capture_fullscreen()
        return CaptureBounds(left=0, top=0, width=image.width, height=image.height)

    def _grab_with_mss(self, sct: Any, monitor: dict[str, int]) -> Image.Image:
        shot = sct.grab(monitor)
        return Image.frombytes("RGB", shot.size, shot.rgb)

    def _capture_region_with_image_grab(self, left: int, top: int, width: int, height: int) -> Image.Image:
        bbox = (left, top, left + width, top + height)
        image = self._image_grab(bbox=bbox, all_screens=True)
        return image.convert("RGB")

    def _get_active_window_bounds_impl(self, client_area: bool = False) -> CaptureBounds | None:
        if not _PYWIN32_OK or win32gui is None:
            logger.warning("pywin32 is not available; active-window bounds cannot be resolved.")
            return None

        try:
            hwnd = int(win32gui.GetForegroundWindow() or 0)
        except Exception as exc:
            logger.warning("GetForegroundWindow failed: %s", exc)
            return None

        if not hwnd:
            return None

        title = ""
        try:
            title = str(win32gui.GetWindowText(hwnd) or "")
        except Exception:
            pass

        try:
            rect = self._get_window_rect(hwnd, client_area=client_area)
        except Exception as exc:
            logger.warning("Active-window bounds lookup failed: %s", exc)
            return None

        left, top, right, bottom = rect
        width = max(0, int(right - left))
        height = max(0, int(bottom - top))
        if width <= 0 or height <= 0:
            return None

        return CaptureBounds(
            left=int(left),
            top=int(top),
            width=width,
            height=height,
            hwnd=hwnd,
            title=title,
        )

    def _get_window_rect(self, hwnd: int, *, client_area: bool) -> tuple[int, int, int, int]:
        if client_area:
            client_rect = win32gui.GetClientRect(hwnd)
            origin = win32gui.ClientToScreen(hwnd, (0, 0))
            return (
                int(origin[0]),
                int(origin[1]),
                int(origin[0] + (client_rect[2] - client_rect[0])),
                int(origin[1] + (client_rect[3] - client_rect[1])),
            )

        rect = _RECT()
        try:  # pragma: no cover - Windows-only branch
            dwmapi = ctypes.windll.dwmapi
            result = dwmapi.DwmGetWindowAttribute(
                ctypes.c_void_p(hwnd),
                ctypes.c_uint(DWMWA_EXTENDED_FRAME_BOUNDS),
                ctypes.byref(rect),
                ctypes.sizeof(rect),
            )
            if result == 0:
                return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        except Exception:
            pass

        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return (int(left), int(top), int(right), int(bottom))

    @staticmethod
    def _default_mss_factory() -> Any:
        if not _MSS_OK or mss is None:  # pragma: no cover - import guard
            raise RuntimeError("mss is not installed")
        return mss.mss()

    @staticmethod
    def _looks_black(image: Image.Image) -> bool:
        try:
            extrema = image.convert("L").getextrema()
            if extrema is None:
                return False
            return int(extrema[1]) <= 4
        except Exception:
            return False
