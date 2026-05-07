"""
Windows clipboard monitoring, privacy filtering, and history management.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from core import settings, state
from core.clipboard_store import ClipboardStore, ClipboardStoreError
from core.logger import get_logger
from core.process_utils import get_process_info

logger = get_logger(__name__)

try:
    import pyperclip
except ImportError:  # pragma: no cover - optional dependency
    pyperclip = None

try:
    import win32clipboard
    import win32con
    import win32gui
    import win32process

    _WIN32_CLIPBOARD_OK = True
except ImportError:  # pragma: no cover - optional dependency
    win32clipboard = None
    win32con = None
    win32gui = None
    win32process = None
    _WIN32_CLIPBOARD_OK = False

@dataclass(slots=True)
class ClipboardItem:
    """Structured clipboard history record."""

    id: int | None
    content_type: str
    text_preview: str
    full_text: str | None
    hash: str
    created_at: float
    source_app: str | None = None
    is_sensitive: bool = False

    def to_dict(self, *, include_full_text: bool = False) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "content_type": self.content_type,
            "text_preview": self.text_preview,
            "hash": self.hash,
            "created_at": self.created_at,
            "source_app": self.source_app,
            "is_sensitive": self.is_sensitive,
        }
        if include_full_text:
            payload["full_text"] = self.full_text
        return payload


class SystemClipboardBackend:
    """Thin Windows clipboard adapter with pyperclip fallback for text access."""

    def __init__(self, retries: int = 5, retry_delay: float = 0.05) -> None:
        self.retries = max(1, int(retries))
        self.retry_delay = max(0.01, float(retry_delay))

    def read(self) -> tuple[str, Any]:
        if _WIN32_CLIPBOARD_OK:
            for _ in range(self.retries):
                try:
                    win32clipboard.OpenClipboard()
                    try:
                        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
                            return "file_paths", list(win32clipboard.GetClipboardData(win32con.CF_HDROP))
                        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                            return "text", str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT))
                        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_TEXT):
                            raw = win32clipboard.GetClipboardData(win32con.CF_TEXT)
                            if isinstance(raw, bytes):
                                return "text", raw.decode("utf-8", errors="replace")
                            return "text", str(raw)
                        return "unknown", None
                    finally:
                        win32clipboard.CloseClipboard()
                except Exception:
                    time.sleep(self.retry_delay)

        if pyperclip is not None:
            try:
                value = pyperclip.paste()
                if value is None:
                    return "empty", None
                return "text", str(value)
            except Exception:
                pass

        raise RuntimeError("No clipboard backend is available.")

    def write_text(self, text: str) -> None:
        if _WIN32_CLIPBOARD_OK:
            for _ in range(self.retries):
                try:
                    win32clipboard.OpenClipboard()
                    try:
                        win32clipboard.EmptyClipboard()
                        win32clipboard.SetClipboardText(str(text), win32con.CF_UNICODETEXT)
                        return
                    finally:
                        win32clipboard.CloseClipboard()
                except Exception:
                    time.sleep(self.retry_delay)

        if pyperclip is not None:
            pyperclip.copy(str(text))
            return

        raise RuntimeError("Clipboard write is unavailable.")

    def clear(self) -> None:
        if _WIN32_CLIPBOARD_OK:
            for _ in range(self.retries):
                try:
                    win32clipboard.OpenClipboard()
                    try:
                        win32clipboard.EmptyClipboard()
                        return
                    finally:
                        win32clipboard.CloseClipboard()
                except Exception:
                    time.sleep(self.retry_delay)

        if pyperclip is not None:
            pyperclip.copy("")
            return

        raise RuntimeError("Clipboard clear is unavailable.")

    def get_source_app(self) -> str | None:
        if not _WIN32_CLIPBOARD_OK:
            return None

        try:
            hwnd = int(win32gui.GetForegroundWindow() or 0)
            if not hwnd:
                return None
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            info = get_process_info(int(pid or 0))
            if not info:
                return None
            exe_name = info.exe_name or info.name
            if exe_name.lower().endswith(".exe"):
                return exe_name[:-4]
            return exe_name or None
        except Exception as exc:
            logger.debug("Clipboard source application lookup failed: %s", exc)
            return None


class ClipboardManager:
    """Real clipboard watcher with persistent history and privacy controls."""

    def __init__(
        self,
        *,
        store: ClipboardStore | None = None,
        backend: SystemClipboardBackend | None = None,
        time_fn=time.time,
        sleep_fn=time.sleep,
    ) -> None:
        self.store = store or ClipboardStore()
        self.backend = backend or SystemClipboardBackend()
        self._time = time_fn
        self._sleep = sleep_fn
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._watcher_thread: threading.Thread | None = None
        self._last_seen_signature: tuple[str, str] | None = None
        self._ignored_signature_once: tuple[str, str] | None = None
        self._session_salt = hashlib.sha256(os.urandom(32)).hexdigest()
        self._refresh_state()

    def start_watcher(self) -> None:
        """Start the background clipboard watcher thread."""
        if not settings.get("clipboard_enabled"):
            state.clipboard_ready = False
            logger.info("Clipboard watcher not started because clipboard support is disabled.")
            return

        with self._lock:
            if self._watcher_thread and self._watcher_thread.is_alive():
                return
            self._stop_event.clear()
            try:
                self.capture_change()
            except Exception as exc:
                logger.warning("Initial clipboard capture failed: %s", exc)
            self._watcher_thread = threading.Thread(
                target=self._watch_loop,
                name="ClipboardWatcher",
                daemon=True,
            )
            self._watcher_thread.start()
            state.clipboard_ready = True
            logger.info("Clipboard watcher started")

    def stop_watcher(self) -> None:
        """Stop the background clipboard watcher thread."""
        with self._lock:
            self._stop_event.set()
            thread = self._watcher_thread
            self._watcher_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        logger.info("Clipboard watcher stopped")

    def shutdown(self) -> None:
        """Stop background work and close persistent resources."""
        self.stop_watcher()
        self.store.close()

    def read_current(self) -> Any | None:
        """Return the current clipboard payload."""
        try:
            content_type, payload = self.backend.read()
        except Exception as exc:
            logger.warning("Clipboard read failed: %s", exc)
            return None

        detected = content_type if payload is None else self.detect_type(payload)
        if detected == "text":
            return str(payload)
        if detected == "file_paths":
            return list(payload or [])
        return None

    def write_text(self, text: str) -> bool:
        """Write text to the system clipboard."""
        normalized = str(text)
        signature = ("text", self._content_digest(normalized))
        try:
            self.backend.write_text(normalized)
            with self._lock:
                self._ignored_signature_once = signature
                self._last_seen_signature = signature
            return True
        except Exception as exc:
            logger.warning("Clipboard write failed: %s", exc)
            return False

    def clear_current(self) -> bool:
        """Clear the live system clipboard."""
        try:
            self.backend.clear()
            with self._lock:
                self._ignored_signature_once = ("empty", "")
                self._last_seen_signature = ("empty", "")
            logger.info("Clipboard cleared")
            return True
        except Exception as exc:
            logger.warning("Clipboard clear failed: %s", exc)
            return False

    def capture_change(self) -> ClipboardItem | None:
        """Read the clipboard, detect a real change, and persist it when supported."""
        try:
            content_type, payload = self.backend.read()
        except Exception as exc:
            logger.warning("Clipboard read failed: %s", exc)
            return None

        detected_type = content_type if payload is None else self.detect_type(payload)
        signature = self._build_signature(detected_type, payload)

        with self._lock:
            if self._ignored_signature_once == signature:
                self._ignored_signature_once = None
                self._last_seen_signature = signature
                return None
            if self._last_seen_signature == signature:
                return None
            self._last_seen_signature = signature

        if detected_type != "text":
            if detected_type not in {"empty", "unknown"}:
                logger.info("Clipboard changed but content type '%s' is not stored yet", detected_type)
            return None

        text = str(payload or "")
        if text == "":
            return None

        try:
            logger.info("Clipboard changed")
            item = self._build_item(text)
            stored = self.store.insert_item(item)
            self._refresh_state(stored)
            logger.info("New clipboard item stored")
            return stored
        except ClipboardStoreError:
            return None
        except Exception as exc:
            logger.error("Clipboard capture failed: %s", exc)
            return None

    def get_last(self) -> ClipboardItem | None:
        """Return the newest clipboard history item."""
        try:
            items = self.store.list_recent(1)
        except ClipboardStoreError:
            return None
        item = items[0] if items else None
        self._refresh_state(item)
        return item

    def get_recent(self, limit: int = 10) -> list[ClipboardItem]:
        """Return recent clipboard history items."""
        try:
            items = self.store.list_recent(max(1, int(limit or 10)))
        except ClipboardStoreError:
            return []
        self._refresh_state(items[0] if items else None)
        return items

    def restore(self, item_id: int) -> ClipboardItem | None:
        """Write a stored text item back to the live clipboard."""
        try:
            item = self.store.get_by_id(int(item_id))
        except ClipboardStoreError:
            return None

        if item is None or item.full_text is None:
            return None
        if not self.write_text(item.full_text):
            return None
        logger.info("Restored clipboard item id=%s", item.id)
        return item

    def clear_history(self) -> int:
        """Delete clipboard history rows and reset related runtime state."""
        try:
            removed = self.store.delete_all()
        except ClipboardStoreError:
            return 0
        state.pending_clipboard_choices = []
        state.last_clipboard_item = {}
        state.clipboard_count = 0
        logger.info("Clipboard history cleared")
        return removed

    def detect_type(self, data: Any) -> str:
        """Detect the clipboard content type in a future-ready way."""
        if data is None:
            return "empty"
        if isinstance(data, list) and all(isinstance(item, str) for item in data):
            return "file_paths"
        if isinstance(data, bytes):
            return "image"
        if isinstance(data, str):
            return "empty" if data == "" else "text"
        return "unknown"

    def _watch_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.capture_change()
            except Exception as exc:
                logger.warning("Clipboard watcher iteration failed: %s", exc)
            interval_ms = int(settings.get("clipboard_watch_interval_ms") or 500)
            self._stop_event.wait(max(0.05, interval_ms / 1000.0))

    def _build_item(self, text: str) -> ClipboardItem:
        cleaned_text = text.replace("\r\n", "\n")
        sensitive_reason = self._sensitive_reason(cleaned_text)
        is_sensitive = bool(sensitive_reason)
        store_sensitive = bool(settings.get("clipboard_store_sensitive"))
        full_text = cleaned_text if (not is_sensitive or store_sensitive) else None
        preview = self._build_preview(cleaned_text)
        if is_sensitive and settings.get("clipboard_mask_sensitive_preview"):
            preview = self._masked_preview(sensitive_reason)
        return ClipboardItem(
            id=None,
            content_type="text",
            text_preview=preview,
            full_text=full_text,
            hash=self._history_hash(cleaned_text, sensitive=is_sensitive and not store_sensitive),
            created_at=float(self._time()),
            source_app=self.backend.get_source_app(),
            is_sensitive=is_sensitive,
        )

    def _refresh_state(self, latest_item: ClipboardItem | None = None) -> None:
        try:
            stats = self.store.stats()
        except ClipboardStoreError:
            stats = {"count": 0}
        state.clipboard_ready = True
        state.clipboard_count = int(stats.get("count", 0) or 0)

        item = latest_item
        if item is None and state.clipboard_count:
            try:
                recent = self.store.list_recent(1)
                item = recent[0] if recent else None
            except ClipboardStoreError:
                item = None
        state.last_clipboard_item = item.to_dict() if item is not None else {}

    @staticmethod
    def _build_preview(text: str, max_chars: int = 80) -> str:
        compact = " ".join(str(text).split())
        if len(compact) <= max_chars:
            return compact
        return compact[: max_chars - 3].rstrip() + "..."

    @staticmethod
    def _masked_preview(reason: str) -> str:
        labels = {
            "password": "[Sensitive password masked]",
            "otp": "[Sensitive OTP code masked]",
            "card": "[Sensitive payment card masked]",
            "token": "[Sensitive token masked]",
            "secret": "[Sensitive secret masked]",
        }
        return labels.get(reason, "[Sensitive clipboard text masked]")

    def _history_hash(self, text: str, *, sensitive: bool) -> str:
        if sensitive:
            raw = f"{self._session_salt}:{text}".encode("utf-8", errors="ignore")
        else:
            raw = text.encode("utf-8", errors="ignore")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _content_digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()

    def _build_signature(self, content_type: str, payload: Any) -> tuple[str, str]:
        if content_type == "file_paths":
            joined = "\n".join(str(item) for item in payload or [])
            return content_type, self._content_digest(joined)
        if content_type == "text":
            return content_type, self._content_digest(str(payload or ""))
        return content_type, ""

    def _sensitive_reason(self, text: str) -> str | None:
        lowered = str(text or "").strip().lower()
        compact = re.sub(r"\s+", " ", lowered)

        if not compact:
            return None

        if any(token in compact for token in ("password", "passcode", "passwd", "pin:", "otp", "2fa code", "verification code")):
            return "password" if "password" in compact or "passcode" in compact or "passwd" in compact else "otp"

        if "bearer " in compact or "api key" in compact or "secret key" in compact or "private key" in compact:
            return "secret"

        if compact.startswith("sk-") or compact.startswith("ghp_") or compact.startswith("gho_") or compact.startswith("xoxb-"):
            return "token"

        if re.fullmatch(r"[A-Za-z0-9_-]{24,}", text.strip()):
            return "token"

        if re.fullmatch(r"[A-Fa-f0-9]{32,}", text.strip()):
            return "token"

        if re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", text.strip()):
            return "token"

        digits = re.sub(r"\D", "", text)
        if 13 <= len(digits) <= 19 and self._is_luhn_valid(digits):
            return "card"

        if re.fullmatch(r"\d{4,8}", text.strip()):
            return "otp"

        if re.search(r"\b(?:otp|code|passcode|verification)\b", compact) and re.search(r"\b\d{4,8}\b", compact):
            return "otp"

        return None

    @staticmethod
    def _is_luhn_valid(digits: str) -> bool:
        total = 0
        reverse_digits = digits[::-1]
        for index, char in enumerate(reverse_digits):
            value = int(char)
            if index % 2 == 1:
                value *= 2
                if value > 9:
                    value -= 9
            total += value
        return total % 10 == 0
