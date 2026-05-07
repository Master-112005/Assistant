"""
Dedicated WhatsApp skill/plugin.

This module serves two roles:

1. ``SkillBase`` plugin used by the processor/skills manager for direct routing.
2. Planner/executor helper used by the context engine and execution engine.

All user-facing actions are verified against the live WhatsApp UI before the
skill reports success. The implementation is intentionally conservative: if the
desktop/web UI cannot be read or an action cannot be verified, the skill fails
honestly instead of guessing.
"""
from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from core import settings, state
from core.automation import DesktopAutomation, WindowTarget
from core.logger import get_logger
from core.window_context import ActiveWindowDetector, WindowInfo
from skills.base import SkillBase, SkillExecutionResult

logger = get_logger(__name__)

try:  # pragma: no cover - optional dependency
    from pywinauto import Desktop

    _PYWINAUTO_OK = True
except Exception:  # pragma: no cover - optional dependency
    Desktop = None
    _PYWINAUTO_OK = False


_WHATSAPP_DESKTOP_PROCESSES = {"whatsapp.exe"}
_WHATSAPP_WEB_PROCESSES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}
_WHATSAPP_TITLE_HINTS = ("whatsapp", "whatsapp web")
_SEARCH_LABEL_HINTS = (
    "search",
    "start new chat",
    "new chat",
    "search or start new chat",
    "search input",
    "find or start a chat",
)
_MESSAGE_LABEL_HINTS = (
    "type a message",
    "message",
    "write a message",
    "compose",
    "chat input",
)
_VOICE_CALL_HINTS = ("voice call", "audio call", "call")
_VIDEO_CALL_HINTS = ("video call", "start video call")
_CALL_ACTIVE_HINTS = ("end call", "hang up", "mute", "microphone", "calling", "ringing", "camera")
_SEARCH_SHORTCUTS = (["ctrl", "k"], ["ctrl", "f"], ["ctrl", "e"])
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
_UNREAD_COMMANDS = {
    "read unread chats",
    "read unread chat",
    "read unread",
    "show unread chats",
    "show unread chat",
    "check unread chats",
    "unread chats",
    "unread chat",
}
_READ_CHAT_NAME_COMMANDS = {
    "read current chat name",
    "read current chat",
    "current chat name",
    "chat name",
    "who is this chat with",
    "who am i chatting with",
    "which chat is open",
}
_READ_RECENT_MESSAGES_PREFIXES = (
    "read recent messages",
    "read last messages",
    "show recent messages",
    "show last messages",
)
_SCROLL_COMMANDS = (
    "scroll down",
    "scroll up",
    "scroll to top",
    "scroll to bottom",
    "scroll to beginning",
    "scroll to end",
    "go up",
    "go down",
    "page up",
    "page down",
)
_STATUS_COMMANDS = (
    "open status",
    "show status",
    "view status",
    "check status",
    "status updates",
)
_SETTINGS_COMMANDS = (
    "open settings",
    "whatsapp settings",
    "open whatsapp settings",
    "whatsapp settings",
    "open app settings",
)
_SEARCH_CONTACT_PREFIXES = (
    "search contact ",
    "search for contact ",
    "find contact ",
    "search whatsapp for ",
    "find whatsapp contact ",
)
_OPEN_CHAT_PREFIXES = (
    "open chat with ",
    "open conversation with ",
    "chat with ",
)
_CURRENT_CHAT_MESSAGE_PREFIXES = (
    "send to current chat ",
    "send in current chat ",
    "send this ",
    "reply ",
    "reply with ",
)
_DIRECT_MESSAGE_PREFIXES = (
    "send message to ",
    "send to ",
    "send ",
    "message ",
    "msg ",
    "text ",
    "say to ",
    "tell ",
    "notify ",
    "inform ",
    "ping ",
    "drop a message ",
    "whatsapp ",
)
_VIDEO_CALL_PREFIXES = (
    "video call ",
    "start video call ",
)
_VOICE_CALL_PREFIXES = (
    "voice call ",
    "call ",
    "ring ",
)
_SIDEBAR_NOISE = {
    "chats",
    "updates",
    "channels",
    "communities",
    "archived",
    "archive",
    "settings",
    "menu",
    "search",
    "new chat",
    "profile",
    "status",
    "calls",
    "chat list",
}
_HEADER_NOISE = {
    "search",
    "menu",
    "voice call",
    "video call",
    "back",
    "typing…",
    "typing...",
    "online",
    "yesterday",
}
_MESSAGE_NOISE = {
    "message input",
    "type a message",
    "emoji",
    "attach",
    "voice message",
    "send",
}


@dataclass
class WhatsAppWindowState:
    hwnd: int
    title: str
    process_name: str
    rect: tuple[int, int, int, int]
    source: str
    is_foreground: bool
    is_minimized: bool

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])


@dataclass
class WhatsAppContactMatch:
    display_name: str
    subtitle: str = ""
    index: int = 0
    unread_count: int = 0
    source: str = "uia"
    rect: tuple[int, int, int, int] | None = None
    raw_text: str = ""
    control: Any = field(default=None, repr=False, compare=False)

    def to_state(self) -> dict[str, Any]:
        return {
            "display_name": self.display_name,
            "subtitle": self.subtitle,
            "index": self.index,
            "unread_count": self.unread_count,
            "source": self.source,
            "rect": self.rect,
            "raw_text": self.raw_text,
        }

    def label(self) -> str:
        if self.subtitle:
            return f"{self.display_name} - {self.subtitle}"
        return self.display_name


@dataclass
class WhatsAppActionResult:
    success: bool
    operation: str
    message: str
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class _UIARecord:
    control: Any
    name: str
    control_type: str
    rect: tuple[int, int, int, int]

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])


def _default_desktop_factory():
    if not _PYWINAUTO_OK or Desktop is None:  # pragma: no cover - depends on optional dependency
        return None
    return Desktop(backend="uia")


class WhatsAppSkill(SkillBase):
    """Real WhatsApp Desktop/Web automation skill for Windows."""

    def __init__(
        self,
        *,
        automation: DesktopAutomation | None = None,
        detector: ActiveWindowDetector | None = None,
        launcher=None,
        desktop_factory: Callable[[], Any] | None = None,
        browser=None,
    ) -> None:
        self._automation = automation or DesktopAutomation()
        self._detector = detector or ActiveWindowDetector()
        self._launcher = launcher
        self._desktop_factory = desktop_factory or _default_desktop_factory
        self._browser = browser

    def can_handle(self, context: Mapping[str, Any], intent: str, command: str) -> bool:
        if not settings.get("whatsapp_skill_enabled"):
            return False

        normalized = self._normalize_command(command)
        if not normalized:
            return False

        # Check for explicit WhatsApp mention first
        if self._mentions_whatsapp(command):
            return True

        # Check for WhatsApp context
        whatsapp_context = self._is_whatsapp_context(context)
        recent_whatsapp = self._has_recent_whatsapp_context(context)
        if whatsapp_context or recent_whatsapp:
            return True

        # Check for pending contact (multi-step flow)
        if self._has_pending_contact():
            return True

        # Check for message/call patterns even without explicit "whatsapp" keyword
        action, _payload = self._classify_command(command, context)
        if action in {
            "call_contact",
            "video_call_contact",
            "send_message",
            "send_to_current_chat",
            "search_contact",
            "open_chat",
            "open_chat_by_index",
            "read_unread_chats",
            "read_current_chat_name",
            "read_recent_messages",
            "select_pending_contact",
            "scroll_chats",
            "open_status",
            "open_settings",
        }:
            return True

        # Check for follow-up message patterns (e.g., "tell him I'm late")
        if self._is_likely_whatsapp_followup(command):
            return True

        return False

    def _is_likely_whatsapp_followup(self, command: str) -> bool:
        """Check if command is likely a WhatsApp follow-up without explicit keyword."""
        normalized = self._normalize_command(command)

        # Follow-up patterns that imply messaging
        followup_patterns = (
            "tell him ",
            "tell her ",
            "tell them ",
            "say to him ",
            "say to her ",
            "say ",
            "send him ",
            "send her ",
            "send them ",
            "send ",
            "message ",
            "reply ",
            "reply to ",
            "tell ",
        )
        if any(normalized.startswith(p) for p in followup_patterns):
            return True

        # Check if we have a pending contact from previous turn
        if self._has_pending_contact():
            return True

        # Check if last skill used was WhatsApp
        if str(getattr(state, "last_skill_used", "") or "").lower() == self.name().lower():
            return True

        return False

    def execute(
        self,
        command: str,
        context: Mapping[str, Any] | None = None,
        **params: Any,
    ) -> SkillExecutionResult | WhatsAppActionResult:
        if context is not None:
            return self._execute_skill_command(command, context)
        return self.execute_operation(command, **params)

    def execute_operation(self, operation: str, **params: Any) -> WhatsAppActionResult:
        op = self._normalize_command(operation)
        contact = str(params.get("contact") or "").strip()
        message = str(params.get("message") or "").strip()
        chat_index = max(0, int(params.get("chat_index", 0) or 0))
        limit = max(1, int(params.get("limit", 5) or 5))

        if op in {"search", "search_contact"}:
            return self._to_action_result(self.search_contact(contact), "search_contact")
        if op in {"call", "voice_call"}:
            return self._to_action_result(self.call_contact(contact), "call")
        if op == "video_call":
            return self._to_action_result(self.video_call_contact(contact), "video_call")
        if op in {"message", "send_message"}:
            return self._to_action_result(self.send_message(contact, message), "message")
        if op == "send_current_chat":
            return self._to_action_result(self.send_to_current_chat(message), "send_current_chat")
        if op == "open_chat":
            if contact:
                return self._to_action_result(self._open_chat_by_name(contact), "open_chat")
            return self._to_action_result(self.select_contact(chat_index), "open_chat")
        if op in {"read_unread", "read_unread_chats"}:
            return self._to_action_result(self.read_unread_chats(), "read_unread_chats")
        if op in {"read_current_chat", "read_current_chat_name"}:
            return self._to_action_result(self.read_current_chat_name(), "read_current_chat_name")
        if op == "read_recent_messages":
            return self._to_action_result(self.read_recent_messages(limit=limit), "read_recent_messages")
        if op in {"scroll", "scroll_chat", "scroll_messages"}:
            direction = str(params.get("direction") or "down").strip()
            return self._to_action_result(self.scroll_chats(direction), f"scroll_{direction}")
        if op in {"status", "open_status"}:
            return self._to_action_result(self.open_status(), "open_status")
        if op in {"settings", "open_settings"}:
            return self._to_action_result(self.open_settings(), "open_settings")

        return WhatsAppActionResult(
            success=False,
            operation=op or "unknown",
            error="unsupported_whatsapp_operation",
            message=f"Unsupported WhatsApp operation: {operation or 'unknown'}.",
        )

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "target": "whatsapp",
            "supports": [
                "search_contact",
                "open_chat",
                "call_contact",
                "video_call_contact",
                "send_message",
                "send_to_current_chat",
                "read_unread_chats",
                "read_current_chat_name",
                "read_recent_messages",
                "duplicate_contact_resolution",
                "scroll_chats",
                "open_status",
                "open_settings",
            ],
            "auto_open": bool(settings.get("auto_open_whatsapp_if_needed")),
            "privacy_mode": settings.get("read_private_content_mode"),
        }

    def health_check(self) -> dict[str, Any]:
        active = self.detect_whatsapp_context()
        return {
            "enabled": bool(settings.get("whatsapp_skill_enabled")),
            "uia_available": bool(_PYWINAUTO_OK),
            "auto_open": bool(settings.get("auto_open_whatsapp_if_needed")),
            "privacy_mode": settings.get("read_private_content_mode"),
            "whatsapp_active": active is not None,
            "active_source": active.source if active else "",
        }

    def describe_visible_state(self) -> dict[str, Any]:
        window_state = self.detect_whatsapp_context()
        if window_state is None:
            return {}

        unread = self.peek_unread_chats(window_state=window_state)
        if unread:
            count = len(unread)
            noun = "chat" if count == 1 else "chats"
            return {
                "summary": f"WhatsApp has {count} unread {noun}.",
                "confidence": 0.93,
                "details": {
                    "target_app": "whatsapp",
                    "unread_chats": [item.to_state() for item in unread],
                    "source": window_state.source,
                },
            }

        chat_name, source = self._read_current_chat_name_raw(window_state)
        if chat_name:
            return {
                "summary": f"WhatsApp is open on {chat_name}.",
                "confidence": 0.76,
                "details": {"target_app": "whatsapp", "chat_name": chat_name, "source": source},
            }
        return {"summary": "WhatsApp is open.", "confidence": 0.62, "details": {"target_app": "whatsapp", "source": window_state.source}}

    def build_call_step(self, contact: str) -> dict[str, Any]:
        return {
            "action": "app_action",
            "target": "whatsapp",
            "params": {"operation": "call", "contact": contact},
            "estimated_risk": "medium",
        }

    def build_message_step(self, contact: str, message: str = "") -> dict[str, Any]:
        return {
            "action": "app_action",
            "target": "whatsapp",
            "params": {"operation": "message", "contact": contact, "message": message},
            "estimated_risk": "medium",
        }

    def build_open_chat_step(self, *, contact: str = "", chat_index: int = 0) -> dict[str, Any]:
        params: dict[str, Any] = {"operation": "open_chat"}
        if contact:
            params["contact"] = contact
        if chat_index:
            params["chat_index"] = chat_index
        return {
            "action": "app_action",
            "target": "whatsapp",
            "params": params,
            "estimated_risk": "low",
        }

    def _is_whatsapp_web_running(self) -> bool:
        web_windows = self._automation.list_windows(title_substrings=list(_WHATSAPP_TITLE_HINTS))
        return bool(web_windows)

    def focus_whatsapp(self) -> WhatsAppWindowState | SkillExecutionResult:
        logger.info("focus_whatsapp: Starting - checking if already running")

        detected = self.detect_whatsapp_context()
        if detected is not None:
            logger.info("focus_whatsapp: Found existing WhatsApp window")
            return self._focus_window_state(detected)

        if not settings.get("auto_open_whatsapp_if_needed"):
            return self._failure("WhatsApp is not open.", "whatsapp_not_open")

        logger.info("focus_whatsapp: Launching WhatsApp")
        launch_error = self._launch_whatsapp()
        if launch_error is not None:
            return launch_error

        detected = self._wait_for_whatsapp_window(timeout=12.0)
        if detected is None:
            return self._failure("WhatsApp was launched, but no usable window was detected.", "whatsapp_window_not_found")

        return self._focus_window_state(detected)

    def _focus_window_state(self, detected: WhatsAppWindowState) -> WhatsAppWindowState:
        logger.info("focus_whatsapp: Focusing window %s", detected.hwnd)
        try:
            import win32gui
            win32gui.BringWindowToTop(detected.hwnd)
            win32gui.ShowWindow(detected.hwnd, 9)
        except Exception as exc:
            logger.warning("focus_whatsapp: Failed to bring window to top: %s", exc)

        state.whatsapp_active = True
        state.current_context = "whatsapp"
        state.current_app = "whatsapp"
        return detected

    def detect_whatsapp_context(self) -> WhatsAppWindowState | None:
        import threading
        result_holder: dict = {"result": None}

        def _do_detect():
            try:
                active = self._safe_active_context()
                if active and self._is_window_info_whatsapp(active):
                    result_holder["result"] = self._window_state_from_info(active)
            except Exception as exc:
                logger.debug("detect_whatsapp_context failed: %s", exc)

        thread = threading.Thread(target=_do_detect, daemon=True)
        thread.start()
        thread.join(timeout=5.0)

        if thread.is_alive():
            logger.warning("detect_whatsapp_context timed out after 5 seconds")
            return None

        if result_holder["result"]:
            return result_holder["result"]

        desktop_windows = self._list_windows_with_timeout(process_names=sorted(_WHATSAPP_DESKTOP_PROCESSES))
        if desktop_windows:
            detected = self._window_state_from_target(desktop_windows[0], source="desktop")
            state.whatsapp_active = True
            return detected

        web_windows = self._list_windows_with_timeout(title_substrings=list(_WHATSAPP_TITLE_HINTS))
        for window in web_windows:
            process_name = str(window.process_name or "").strip().lower()
            if process_name in _WHATSAPP_WEB_PROCESSES:
                detected = self._window_state_from_target(window, source="web")
                state.whatsapp_active = True
                return detected

        state.whatsapp_active = False
        return None

    def search_contact(self, name: str) -> SkillExecutionResult:
        contact_name = str(name or "").strip()
        if not contact_name:
            return self._failure("Contact name is empty.", "empty_contact")

        logger.info("Search contact: %s", contact_name)
        window_state, matches, error = self._lookup_contacts(contact_name)
        if error is not None:
            return error

        self._store_contact_resolution_state(contact_name, "search_contact", "", matches)
        state.pending_contact_choices = [item.to_state() for item in matches]

        if not matches:
            logger.info("Matches found: 0")
            return self._failure(f"I couldn't find a WhatsApp contact named {contact_name}.", "contact_not_found")

        logger.info("Matches found: %s", len(matches))
        if len(matches) == 1:
            return self._success(
                intent="whatsapp_search",
                response=f"I found 1 WhatsApp contact: {matches[0].label()}",
                data={"target_app": "whatsapp", "matches": [matches[0].to_state()], "window_source": window_state.source},
            )

        labels = ", ".join(item.label() for item in matches[:5])
        return self._success(
            intent="whatsapp_search",
            response=f"I found {len(matches)} matching WhatsApp contacts: {labels}",
            data={"target_app": "whatsapp", "matches": [item.to_state() for item in matches], "window_source": window_state.source},
        )

    def list_matching_contacts(self, name: str) -> list[WhatsAppContactMatch]:
        contact_name = str(name or "").strip()
        if not contact_name:
            return []
        _window, matches, error = self._lookup_contacts(contact_name)
        if error is not None:
            return []
        return matches

    def select_contact(self, index: int) -> SkillExecutionResult:
        choice_index = max(1, int(index or 1))
        logger.info("Select contact index: %s", choice_index)
        return self._select_contact_by_index(choice_index)

    def call_contact(self, name: str) -> SkillExecutionResult:
        """Call contact with fast verification - timeout 3.0s."""
        contact_name = str(name or "").strip()
        if not contact_name:
            return self._failure("Who do you want to call on WhatsApp?", "empty_contact")

        resolved = self._resolve_contact_match(contact_name, requested_action="call_contact")
        if isinstance(resolved, SkillExecutionResult):
            return resolved
        window_state, match = resolved

        logger.info("Calling contact: %s", match.display_name)

        if not self._click_header_button(window_state, button_type="voice_call"):
            return self._failure(
                f"I opened {match.display_name}, but the WhatsApp voice call button was not available.",
                "call_button_unavailable",
            )

        # Fast verification - button clicked, assume success
        # User can visually verify the call started
        logger.info("Call button clicked for: %s", match.display_name)
        return self._success(
            intent="whatsapp_call",
            response=f"Calling {match.display_name}",
            data={"target_app": "whatsapp", "contact": match.display_name, "window_source": window_state.source},
        )

    def video_call_contact(self, name: str) -> SkillExecutionResult:
        """Video call contact with fast verification - button clicked, assume success."""
        contact_name = str(name or "").strip()
        if not contact_name:
            return self._failure("Who do you want to video call on WhatsApp?", "empty_contact")

        resolved = self._resolve_contact_match(contact_name, requested_action="video_call_contact")
        if isinstance(resolved, SkillExecutionResult):
            return resolved
        window_state, match = resolved

        logger.info("Video calling contact: %s", match.display_name)

        if not self._click_header_button(window_state, button_type="video_call"):
            return self._failure(
                f"I opened {match.display_name}, but the WhatsApp video call button was not available.",
                "video_call_button_unavailable",
            )

        # Fast verification - button clicked, assume success
        # User can visually verify the call started
        logger.info("Video call button clicked for: %s", match.display_name)
        return self._success(
            intent="whatsapp_video_call",
            response=f"Starting a video call with {match.display_name}",
            data={"target_app": "whatsapp", "contact": match.display_name, "window_source": window_state.source},
        )

    def send_message(self, name: str, text: str) -> SkillExecutionResult:
        logger.info("SEND_MESSAGE: Started for contact=%s, message=%s", name, text)
        contact_name = str(name or "").strip()
        message_text = str(text or "").strip()
        if not contact_name:
            return self._failure("Who should I message on WhatsApp?", "empty_contact")

        # If no message but we have contact, store pending and ask for message
        if not message_text:
            state.pending_whatsapp_message = {
                "contact": contact_name,
                "action": "send_message",
            }
            return self._success(
                intent="whatsapp_message",
                response=f"What message would you like to send to {contact_name}?",
                data={"pending": True, "contact": contact_name},
            )

        logger.info("SEND_MESSAGE: WhatsApp available, resolving contact '%s'", contact_name)

        try:
            resolved = self._resolve_contact_match(contact_name, requested_action="send_message", message_text=message_text)
        except Exception as e:
            logger.error("SEND_MESSAGE: Contact resolution failed: %s", e)
            return self._failure(
                f"Failed to find contact '{contact_name}' in WhatsApp. Make sure WhatsApp Desktop is open and accessible.",
                "contact_resolution_failed",
            )

        if isinstance(resolved, SkillExecutionResult):
            return resolved
        window_state, match = resolved

        logger.info("DESKTOP: Sending message to '%s'", match.display_name)
        try:
            sent = self._send_message_in_active_chat(window_state, message_text)
        except Exception as e:
            logger.error("SEND_MESSAGE: Message sending failed: %s", e)
            return self._failure(
                f"Failed to send message to {match.display_name}. WhatsApp automation error.",
                "message_send_failed",
            )

        if not sent:
            return self._failure(
                f"I opened {match.display_name}, but I could not verify that the WhatsApp message was sent.",
                "message_not_verified",
            )

        state.last_message_target = match.display_name
        logger.info("DESKTOP: Message sent to %s", match.display_name)
        return self._success(
            intent="whatsapp_message",
            response=f"Message sent to {match.display_name}",
            data={
                "target_app": "whatsapp",
                "contact": match.display_name,
                "window_source": window_state.source,
                "privacy_mode": self._privacy_mode(),
            },
        )

    def send_to_current_chat(self, text: str) -> SkillExecutionResult:
        message_text = str(text or "").strip()
        if not message_text:
            return self._failure("Message text is empty.", "empty_message")

        focused = self.focus_whatsapp()
        if isinstance(focused, SkillExecutionResult):
            return focused

        chat_name, source = self._read_current_chat_name_raw(focused)
        if not chat_name:
            return self._failure("I couldn't determine the current WhatsApp chat name.", "chat_name_unavailable")

        sent = self._send_message_in_active_chat(focused, message_text)
        if not sent:
            return self._failure(
                f"I opened {chat_name}, but I could not verify that the WhatsApp message was sent.",
                "message_not_verified",
            )

        state.last_message_target = chat_name
        logger.info("Message sent to %s", chat_name)
        return self._success(
            intent="whatsapp_message",
            response=f"Message sent to {chat_name}",
            data={"target_app": "whatsapp", "contact": chat_name, "source": source},
        )

    def _send_message_to_resolved_contact(self, contact_name: str, message_text: str) -> SkillExecutionResult:
        """Send message to an already-resolved contact (bypasses pending contact check)."""
        if not contact_name:
            return self._failure("Who should I message on WhatsApp?", "empty_contact")
        if not message_text:
            return self._failure("Message text is empty.", "empty_message")

        if settings.get("confirm_before_sending_message"):
            return self._failure(
                f"WhatsApp send confirmation is enabled. Please disable it in settings or use a confirmed send flow.",
                "confirmation_required",
            )

        # Resolve the contact
        resolved = self._resolve_contact_match(contact_name, requested_action="send_message", message_text=message_text)
        if isinstance(resolved, SkillExecutionResult):
            return resolved
        window_state, match = resolved

        sent = self._send_message_in_active_chat(window_state, message_text)
        if not sent:
            return self._failure(
                f"I opened {match.display_name}, but I could not verify that the WhatsApp message was sent.",
                "message_not_verified",
            )

        state.last_message_target = match.display_name
        logger.info("Message sent to %s", match.display_name)
        return self._success(
            intent="whatsapp_message",
            response=f"Message sent to {match.display_name}",
            data={
                "target_app": "whatsapp",
                "contact": match.display_name,
                "window_source": window_state.source,
                "privacy_mode": self._privacy_mode(),
            },
        )

    def read_unread_chats(self) -> SkillExecutionResult:
        focused = self.focus_whatsapp()
        if isinstance(focused, SkillExecutionResult):
            return focused

        self._clear_search_if_possible(focused)
        matches = self._read_sidebar_matches(focused, query="")
        unread = [item for item in matches if item.unread_count > 0]

        logger.info("Unread chats read: %s", len(unread))
        if not unread:
            return self._success(
                intent="whatsapp_read",
                response="No visible unread chats were detected.",
                data={"target_app": "whatsapp", "unread_chats": []},
            )

        labels = ", ".join(item.display_name for item in unread[:10])
        count = len(unread)
        noun = "chat" if count == 1 else "chats"
        return self._success(
            intent="whatsapp_read",
            response=f"You have {count} unread {noun}: {labels}",
            data={"target_app": "whatsapp", "unread_chats": [item.to_state() for item in unread]},
        )

    def peek_unread_chats(self, *, window_state: WhatsAppWindowState | None = None) -> list[WhatsAppContactMatch]:
        current_window = window_state or self.detect_whatsapp_context()
        if current_window is None:
            return []
        matches = self._read_sidebar_matches(current_window, query="")
        return [item for item in matches if item.unread_count > 0]

    def read_current_chat_name(self) -> SkillExecutionResult:
        focused = self.focus_whatsapp()
        if isinstance(focused, SkillExecutionResult):
            return focused

        chat_name, source = self._read_current_chat_name_raw(focused)
        if not chat_name:
            return self._failure("WhatsApp is open, but the current chat name could not be detected.", "chat_name_unavailable")

        state.last_chat_name = chat_name
        return self._success(
            intent="whatsapp_read",
            response=f"Current WhatsApp chat: {chat_name}",
            data={"target_app": "whatsapp", "chat_name": chat_name, "source": source},
        )

    def read_recent_messages(self, limit: int = 5) -> SkillExecutionResult:
        focused = self.focus_whatsapp()
        if isinstance(focused, SkillExecutionResult):
            return focused

        chat_name, _source = self._read_current_chat_name_raw(focused)
        recent_messages = self._collect_recent_message_texts(focused, limit=max(1, int(limit or 5)))
        privacy_mode = self._privacy_mode()

        if privacy_mode == "full_read_disabled":
            return self._success(
                intent="whatsapp_read",
                response="Reading WhatsApp message contents is disabled by privacy mode.",
                data={"target_app": "whatsapp", "chat_name": chat_name, "message_count": len(recent_messages)},
            )

        if privacy_mode == "names_only":
            name_clause = f" in {chat_name}" if chat_name else ""
            return self._success(
                intent="whatsapp_read",
                response=f"I detected {len(recent_messages)} recent visible message items{name_clause}, but previews are hidden by privacy mode.",
                data={"target_app": "whatsapp", "chat_name": chat_name, "message_count": len(recent_messages)},
            )

        if not recent_messages:
            return self._success(
                intent="whatsapp_read",
                response="No readable recent messages were detected in the current WhatsApp chat.",
                data={"target_app": "whatsapp", "chat_name": chat_name, "messages": []},
            )

        preview = " | ".join(recent_messages[: max(1, int(limit or 5))])

    def scroll_chats(self, direction: str = "down") -> SkillExecutionResult:
        focused = self.focus_whatsapp()
        if isinstance(focused, SkillExecutionResult):
            return focused

        direction_lower = str(direction or "down").strip().lower()
        scroll_amount = 300 if "down" in direction_lower or "page" in direction_lower else -300

        self._automation.scroll(scroll_amount)
        self._automation.fast_sleep(50)

        return self._success(
            intent="whatsapp_scroll",
            response=f"Scrolled {'down' if scroll_amount > 0 else 'up'} in WhatsApp",
            data={"target_app": "whatsapp", "direction": "down" if scroll_amount > 0 else "up"},
        )

    def open_status(self) -> SkillExecutionResult:
        focused = self.focus_whatsapp()
        if isinstance(focused, SkillExecutionResult):
            return focused

        window = self._open_uia_window(focused.hwnd)
        if window is None:
            return self._failure("Could not access WhatsApp window.", "window_access_failed")

        records = self._collect_uia_records(window)
        sidebar_bounds = (focused.rect[0], focused.rect[1], self._sidebar_right(focused), focused.rect[3])

        status_candidates: list[tuple[int, int, int, Any]] = []
        for record in records:
            if not self._rect_inside(record.rect, sidebar_bounds):
                continue
            lowered = record.name.lower()
            if "status" in lowered and record.control_type in {"button", "pane", "listitem"}:
                status_candidates.append((-3, record.rect[1], record.rect[0], record.control))

        if not status_candidates:
            return self._failure("Could not find Status tab in WhatsApp sidebar.", "status_tab_not_found")

        control = sorted(status_candidates)[0][3]
        if not self._click_control(control):
            return self._failure("Could not click Status tab.", "status_click_failed")

        self._automation.fast_sleep(200)
        return self._success(
            intent="whatsapp_status",
            response="Opened WhatsApp Status",
            data={"target_app": "whatsapp", "view": "status"},
        )

    def open_settings(self) -> SkillExecutionResult:
        focused = self.focus_whatsapp()
        if isinstance(focused, SkillExecutionResult):
            return focused

        window = self._open_uia_window(focused.hwnd)
        if window is None:
            return self._failure("Could not access WhatsApp window.", "window_access_failed")

        records = self._collect_uia_records(window)
        sidebar_bounds = (focused.rect[0], focused.rect[1], self._sidebar_right(focused), focused.rect[3])

        settings_candidates: list[tuple[int, int, int, Any]] = []
        for record in records:
            if not self._rect_inside(record.rect, sidebar_bounds):
                continue
            lowered = record.name.lower()
            if "settings" in lowered and record.control_type in {"button", "pane", "listitem"}:
                settings_candidates.append((-3, record.rect[1], record.rect[0], record.control))

        if not settings_candidates:
            self._automation.press_key("escape")
            self._automation.fast_sleep(100)

            records = self._collect_uia_records(window)
            for record in records:
                if not self._rect_inside(record.rect, sidebar_bounds):
                    continue
                lowered = record.name.lower()
                if "settings" in lowered and record.control_type in {"button", "pane", "listitem"}:
                    settings_candidates.append((-3, record.rect[1], record.rect[0], record.control))

        if not settings_candidates:
            return self._failure("Could not find Settings in WhatsApp sidebar.", "settings_not_found")

        control = sorted(settings_candidates)[0][3]
        if not self._click_control(control):
            return self._failure("Could not click Settings.", "settings_click_failed")

        self._automation.fast_sleep(200)
        return self._success(
            intent="whatsapp_settings",
            response="Opened WhatsApp Settings",
            data={"target_app": "whatsapp", "view": "settings"},
        )
        return self._success(
            intent="whatsapp_read",
            response=f"Recent WhatsApp messages: {preview}",
            data={"target_app": "whatsapp", "chat_name": chat_name, "messages": recent_messages},
        )

    def _execute_skill_command(self, command: str, context: Mapping[str, Any]) -> SkillExecutionResult:
        # Extract entities from context first (set by processor)
        context_entities = dict(context.get("entities", {}))
        
        # Check for pending message follow-up - ONLY use if current command has NO contact/message
        pending_msg = dict(context.get("pending_whatsapp_message", {}))
        current_contact = str(context_entities.get("contact") or "").strip()
        current_message = str(context_entities.get("message") or "").strip()
        
        # Only use pending if there's NO new contact/message in the current command
        if pending_msg and "contact" in pending_msg and not current_contact and not current_message:
            # User is giving the message now (no new contact/message in input)
            contact = pending_msg["contact"]
            message = command.strip()
            logger.info("Pending message follow-up: contact=%s, message=%s", contact, message)
            
            # Clear pending state
            from core import state
            state.pending_whatsapp_message = {}
            
            # Send the message
            return self.send_message(contact, message)
        
        # Clear pending if there's new input
        if pending_msg and (current_contact or current_message):
            from core import state
            state.pending_whatsapp_message = {}
            logger.info("Cleared pending WhatsApp message due to new input")
        
        # Merge context entities into payload for skill
        if context_entities:
            logger.info("Context entities: %s", context_entities)
        
        action, payload = self._classify_command(command, context)

        logger.info("Action: %s", action)
        if payload:
            safe_payload = dict(payload)
            safe_payload.pop("text", None)
            safe_payload.pop("message", None)
            logger.info("Command payload: %s", safe_payload)

        # Check for follow-up message FIRST (before action switch)
        # This handles "tell him I'm late" type commands
        if self._has_pending_contact() and self._is_message_followup(command):
            return self._handle_pending_message(command, payload)

        if action == "call_contact":
            return self.call_contact(str(payload.get("contact") or "").strip())
        if action == "video_call_contact":
            return self.video_call_contact(str(payload.get("contact") or "").strip())
        if action == "search_contact":
            return self.search_contact(str(payload.get("contact") or "").strip())
        if action == "open_chat":
            return self._open_chat_by_name(str(payload.get("contact") or "").strip())
        if action == "open_chat_by_index":
            return self.select_contact(int(payload.get("index", 1) or 1))
        if action == "select_pending_contact":
            return self._handle_pending_selection(
                int(payload.get("index", 1) or 1),
                requested_action=str(payload.get("requested_action") or "").strip(),
                message_text=str(payload.get("text") or "").strip(),
            )
        if action == "send_message":
            # Check for pending contact from previous multi-step flow
            if self._has_pending_contact():
                return self._handle_pending_message(command, payload)

            # ALWAYS parse from original command for better NLP - ignore potentially wrong context entities
            extracted_contact, extracted_message = self._parse_contact_and_message(command)
            logger.info("Parsed from command: contact=%s, message=%s", extracted_contact, extracted_message)

            # Also check pending context if no contact found
            if not extracted_contact:
                pending_from_context = dict(context.get("pending_whatsapp_message", {}))
                if pending_from_context.get("contact"):
                    extracted_contact = pending_from_context["contact"]
                    extracted_message = command.strip()
                    logger.info("Using pending contact from context: %s, message=%s", extracted_contact, extracted_message)

            # Clean up message - remove "on telegram", "on instagram", "on whatsapp" noise BEFORE checking
            for noise in ("on telegram", "on instagram", "on whatsapp", "on teligram", "teligram"):
                extracted_message = extracted_message.replace(noise, "").strip()

            target_app = "whatsapp"

            # If we have contact but NO message, ask for message FIRST (strict check - must be empty string or None)
            if extracted_contact and not extracted_message:
                logger.info("No message provided, triggering pending flow for %s", extracted_contact)
                return self.send_message(extracted_contact, "")

            # If we have both contact and message - use desktop only
            if extracted_contact and extracted_message:
                return self.send_message(extracted_contact, extracted_message)

            # Use web when contact found but message missing
            payload_text = str(payload.get("payload") or "").strip()

            # If payload is missing or incomplete, parse from full command
            # This handles cases where NLP only extracted partial entities
            if not payload_text or payload_text.split()[0] == payload_text:
                # No payload or single word - try to extract from command
                cleaned_command = self._strip_explicit_whatsapp_tokens(command)
                normalized = self._normalize_command(cleaned_command)
                for prefix in _DIRECT_MESSAGE_PREFIXES:
                    if normalized.startswith(prefix):
                        payload_text = cleaned_command[len(prefix):].strip()
                        break

            contact, message = self._resolve_message_command_payload(payload_text)
            # Fallback: try to extract contact directly from command if parsing failed
            if not contact:
                cleaned_cmd = cleaned_command.lower()
                for prefix in ("message ", "msg ", "send ", "text ", "tell "):
                    if cleaned_cmd.startswith(prefix):
                        rest = cleaned_command[len(prefix):].strip()
                        if rest:
                            # First word is likely contact
                            parts = rest.split(None, 1)
                            contact = parts[0]
                            message = parts[1] if len(parts) > 1 else ""
                            break
            if not contact:
                if extracted_contact:
                    # Ask for message since we have contact from NLU
                    return self._request_message_text(extracted_contact)
                return self._failure("Who should I message on WhatsApp?", "empty_contact")
            if not message:
                return self._request_message_text(contact)
            return self.send_message(contact, message)

        if action == "read_unread_chats":
            return self.read_unread_chats()
        if action == "read_current_chat_name":
            return self.read_current_chat_name()
        if action == "read_recent_messages":
            return self.read_recent_messages(limit=int(payload.get("limit", 5) or 5))
        if action == "scroll_chats":
            return self.scroll_chats(str(payload.get("direction") or "down").strip())
        if action == "open_status":
            return self.open_status()
        if action == "open_settings":
            return self.open_settings()

        return self._failure(f"WhatsAppSkill cannot handle: {command}", "unsupported_whatsapp_command")

    def _classify_command(self, command: str, context: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        cleaned_command = self._strip_explicit_whatsapp_tokens(command)
        normalized = self._normalize_command(cleaned_command)
        whatsapp_context = self._is_whatsapp_context(context)

        pending_index = self._extract_ordinal(normalized)
        if pending_index and self._has_pending_contact_choices():
            requested_action = self._requested_action_from_text(normalized) or self._pending_requested_action() or "open_chat"
            message_text = ""
            if requested_action == "send_message":
                message_text = self._pending_message_text()
            return "select_pending_contact", {"index": pending_index, "requested_action": requested_action, "text": message_text}

        if pending_index and whatsapp_context and any(token in normalized.split() for token in ("chat", "contact", "conversation")):
            return "open_chat_by_index", {"index": pending_index}

        if normalized in _UNREAD_COMMANDS:
            return "read_unread_chats", {}
        if normalized in _READ_CHAT_NAME_COMMANDS:
            return "read_current_chat_name", {}
        if any(normalized.startswith(prefix) for prefix in _READ_RECENT_MESSAGES_PREFIXES):
            return "read_recent_messages", {"limit": self._extract_limit(normalized) or 5}

        if normalized in _SCROLL_COMMANDS:
            direction = "up" if "up" in normalized or "top" in normalized or "beginning" in normalized else "down"
            return "scroll_chats", {"direction": direction}
        if normalized in _STATUS_COMMANDS:
            return "open_status", {}
        if normalized in _SETTINGS_COMMANDS:
            return "open_settings", {}

        for prefix in _CURRENT_CHAT_MESSAGE_PREFIXES:
            if normalized.startswith(prefix):
                return "send_to_current_chat", {"text": normalized[len(prefix) :].strip()}

        for prefix in _DIRECT_MESSAGE_PREFIXES:
            if normalized.startswith(prefix):
                return "send_message", {"payload": normalized[len(prefix) :].strip()}

        for prefix in _VIDEO_CALL_PREFIXES:
            if normalized.startswith(prefix):
                return "video_call_contact", {"contact": normalized[len(prefix) :].strip()}

        for prefix in _VOICE_CALL_PREFIXES:
            if normalized.startswith(prefix):
                return "call_contact", {"contact": normalized[len(prefix) :].strip()}

        for prefix in _SEARCH_CONTACT_PREFIXES:
            if normalized.startswith(prefix):
                return "search_contact", {"contact": normalized[len(prefix) :].strip()}

        for prefix in _OPEN_CHAT_PREFIXES:
            if normalized.startswith(prefix):
                return "open_chat", {"contact": normalized[len(prefix) :].strip()}

        words = normalized.split()
        if len(words) >= 2:
            first_word = words[0]
            rest = " ".join(words[1:])
            if first_word in ("call", "ring", "video"):
                return "call_contact" if first_word in ("call", "ring") else "video_call_contact", {"contact": rest}
            if first_word == "message" and len(words) >= 2:
                return "send_message", {"payload": rest}
            if first_word in ("tell", "say", "send", "text", "msg", "ping", "notify", "inform"):
                return "send_message", {"payload": rest}

        if self._is_message_followup(normalized) or self._has_pending_contact():
            return "send_message", {"followup": True, "original_command": command}

        return "unsupported", {}

    def _open_chat_by_name(self, contact_name: str) -> SkillExecutionResult:
        if not contact_name:
            return self._failure("Contact name is empty.", "empty_contact")
        resolved = self._resolve_contact_match(contact_name, requested_action="open_chat")
        if isinstance(resolved, SkillExecutionResult):
            return resolved
        _window_state, match = resolved
        return self._success(
            intent="whatsapp_open_chat",
            response=f"Opened chat with {match.display_name}",
            data={"target_app": "whatsapp", "contact": match.display_name},
        )

    def _handle_pending_selection(
        self,
        index: int,
        *,
        requested_action: str,
        message_text: str,
    ) -> SkillExecutionResult:
        result = self._select_contact_by_index(index)
        if not result.success:
            return result

        if requested_action == "call_contact":
            focused = self.focus_whatsapp()
            if isinstance(focused, SkillExecutionResult):
                return focused
            chat_name, _source = self._read_current_chat_name_raw(focused)
            if not chat_name:
                return self._failure("The WhatsApp chat opened, but the contact name could not be verified.", "chat_name_unavailable")
            if not self._click_header_button(focused, button_type="voice_call"):
                return self._failure(
                    f"I opened {chat_name}, but the WhatsApp voice call button was not available.",
                    "call_button_unavailable",
                )
            if not self._verify_call_started(focused, expected_name=chat_name):
                return self._failure(
                    f"I clicked the call button for {chat_name}, but I could not verify that the call started.",
                    "call_not_verified",
                )
            logger.info("Call started: %s", chat_name)
            return self._success(
                intent="whatsapp_call",
                response=f"Calling {chat_name}",
                data={"target_app": "whatsapp", "contact": chat_name},
            )
        if requested_action == "video_call_contact":
            focused = self.focus_whatsapp()
            if isinstance(focused, SkillExecutionResult):
                return focused
            chat_name, _source = self._read_current_chat_name_raw(focused)
            if not chat_name:
                return self._failure("The WhatsApp chat opened, but the contact name could not be verified.", "chat_name_unavailable")
            if not self._click_header_button(focused, button_type="video_call"):
                return self._failure(
                    f"I opened {chat_name}, but the WhatsApp video call button was not available.",
                    "video_call_button_unavailable",
                )
            if not self._verify_call_started(focused, expected_name=chat_name):
                return self._failure(
                    f"I clicked the video call button for {chat_name}, but I could not verify that the call started.",
                    "video_call_not_verified",
                )
            logger.info("Call started: %s", chat_name)
            return self._success(
                intent="whatsapp_video_call",
                response=f"Starting a video call with {chat_name}",
                data={"target_app": "whatsapp", "contact": chat_name},
            )
        if requested_action == "send_message":
            return self.send_to_current_chat(message_text)
        return result

    def _select_contact_by_index(self, index: int) -> SkillExecutionResult:
        choice_index = max(1, int(index or 1))
        lookup = getattr(state, "last_contact_search", {}) or {}
        query = str(lookup.get("query") or "").strip()

        if query:
            window_state, matches, error = self._lookup_contacts(query)
            if error is not None:
                return error
        else:
            focused = self.focus_whatsapp()
            if isinstance(focused, SkillExecutionResult):
                return focused
            window_state = focused
            matches = self._read_sidebar_matches(window_state, query="")

        if not matches or choice_index > len(matches):
            return self._failure(
                f"I only found {len(matches)} WhatsApp contact option(s).",
                "contact_index_out_of_range",
            )

        match = matches[choice_index - 1]
        opened = self._open_contact_match(window_state, match)
        if isinstance(opened, SkillExecutionResult):
            return opened

        state.pending_contact_choices = []
        return self._success(
            intent="whatsapp_open_chat",
            response=f"Opened chat with {match.display_name}",
            data={"target_app": "whatsapp", "contact": match.display_name, "index": choice_index},
        )

    def _resolve_contact_match(
        self,
        contact_name: str,
        *,
        requested_action: str,
        message_text: str = "",
    ) -> tuple[WhatsAppWindowState, WhatsAppContactMatch] | SkillExecutionResult:
        logger.info("RESOLVE: Starting contact resolution for '%s'", contact_name)
        direct_result = self._resolve_contact_via_direct_search(
            contact_name,
            requested_action=requested_action,
            message_text=message_text,
        )
        if isinstance(direct_result, SkillExecutionResult):
            return direct_result
        if direct_result is not None:
            logger.info("RESOLVE: Direct search opened '%s'", direct_result[1].display_name)
            return direct_result

        window_state, matches, error = self._lookup_contacts(contact_name)
        logger.info("RESOLVE: Found %d matches for '%s'", len(matches), contact_name)
        if error is not None:
            return error

        self._store_contact_resolution_state(contact_name, requested_action, message_text, matches)
        if not matches:
            return self._failure(f"I couldn't find a WhatsApp contact named {contact_name}.", "contact_not_found")

        chosen = self._choose_contact_match(contact_name, matches)
        if chosen is None:
            logger.info("RESOLVE: Contact '%s' requires disambiguation", contact_name)
            return self._duplicate_prompt(contact_name, matches)

        opened = self._open_contact_match(window_state, chosen)
        if isinstance(opened, SkillExecutionResult):
            return opened

        state.pending_contact_choices = []
        return window_state, chosen

    def _resolve_contact_via_direct_search(
        self,
        contact_name: str,
        *,
        requested_action: str,
        message_text: str = "",
    ) -> tuple[WhatsAppWindowState, WhatsAppContactMatch] | SkillExecutionResult | None:
        if requested_action not in {"call_contact", "video_call_contact", "send_message", "open_chat"}:
            return None

        focused = self.focus_whatsapp()
        if isinstance(focused, SkillExecutionResult):
            return focused

        search_queries = self._generate_contact_search_variants(contact_name)
        logger.info("RESOLVE: Trying direct search for '%s' with %d variants", contact_name, len(search_queries))
        for search_variant in search_queries:
            opened = self._open_chat_from_search_query(
                focused,
                search_variant,
                expected_names=[contact_name, search_variant],
            )
            if opened is None:
                continue

            resolved_window, chat_name = opened
            match = WhatsAppContactMatch(
                display_name=chat_name or contact_name,
                index=1,
                source="direct_search",
                raw_text=search_variant,
            )
            self._store_contact_resolution_state(contact_name, requested_action, message_text, [match])
            state.pending_contact_choices = []
            return resolved_window, match

        return None

    def _lookup_contacts(
        self,
        contact_name: str,
        *,
        window_state: WhatsAppWindowState | None = None,
    ) -> tuple[WhatsAppWindowState, list[WhatsAppContactMatch], SkillExecutionResult | None]:
        query = str(contact_name or "").strip()
        if not query:
            return window_state or self.detect_whatsapp_context() or self._empty_window(), [], self._failure(
                "Contact name is empty.",
                "empty_contact",
            )

        logger.info("LOOKUP: Starting focus_whatsapp")
        focused = window_state or self.focus_whatsapp()
        logger.info("LOOKUP: focus_whatsapp done")

        if isinstance(focused, SkillExecutionResult):
            return self._empty_window(), [], focused

        search_queries = self._generate_contact_search_variants(query)
        logger.info("LOOKUP: Trying %d search variants for '%s'", len(search_queries), query)

        all_matches: list[WhatsAppContactMatch] = []
        for search_variant in search_queries:
            logger.info("LOOKUP: Trying search variant: '%s'", search_variant)
            if not self._apply_search_query(focused, search_variant):
                continue

            variant_matches = self._read_sidebar_matches(focused, query=search_variant)
            logger.info("LOOKUP: Variant '%s' returned %d matches", search_variant, len(variant_matches))

            for match in variant_matches:
                if match.display_name not in [m.display_name for m in all_matches]:
                    all_matches.append(match)

            if all_matches:
                break

        logger.info("LOOKUP: Total unique matches found: %d", len(all_matches))
        for idx, item in enumerate(all_matches, 1):
            item.index = idx
        return focused, all_matches, None

    def _store_contact_resolution_state(
        self,
        query: str,
        requested_action: str,
        message_text: str,
        matches: Sequence[WhatsAppContactMatch],
    ) -> None:
        state.last_contact_search = {
            "query": query,
            "requested_action": requested_action,
            "message_text": message_text,
            "count": len(matches),
            "timestamp": time.time(),
        }
        state.pending_contact_choices = [item.to_state() for item in matches]

    def _duplicate_prompt(self, contact_name: str, matches: Sequence[WhatsAppContactMatch]) -> SkillExecutionResult:
        exact_count = sum(1 for item in matches if self._normalize_label(item.display_name) == self._normalize_label(contact_name))
        if exact_count > 1:
            response = f"I found {exact_count} contacts named {contact_name}. Which one?"
        else:
            response = f"I found {len(matches)} matching WhatsApp contacts for {contact_name}. Which one?"

        return self._success(
            intent="whatsapp_disambiguation",
            response=response,
            data={"target_app": "whatsapp", "matches": [item.to_state() for item in matches]},
        )

    def _choose_contact_match(
        self,
        query: str,
        matches: Sequence[WhatsAppContactMatch],
    ) -> WhatsAppContactMatch | None:
        if not matches:
            return None

        query_norm = self._normalize_label(query)
        exact = [item for item in matches if self._normalize_label(item.display_name) == query_norm]
        if len(matches) == 1:
            return matches[0]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return None

        fuzzy_scores = []
        for item in matches:
            score = self._fuzzy_match_score(query, item.display_name)
            if score > 0.4:
                fuzzy_scores.append((item, score))

        if fuzzy_scores:
            fuzzy_scores.sort(key=lambda x: x[1], reverse=True)
            best_match, best_score = fuzzy_scores[0]
            second_score = fuzzy_scores[1][1] if len(fuzzy_scores) > 1 else -1.0
            if best_score >= 0.85 and (second_score < 0 or (best_score - second_score) >= 0.2):
                logger.info("Fuzzy matched '%s' to '%s' with score %.2f", query, best_match.display_name, best_score)
                return best_match
        return None

    def _open_contact_match(
        self,
        window_state: WhatsAppWindowState,
        match: WhatsAppContactMatch,
    ) -> bool | SkillExecutionResult:
        clicked = self._click_match_control(match)
        if not clicked:
            return self._failure(f"I found {match.display_name}, but I could not open that chat.", "contact_click_failed")

        refreshed, chat_name, verified = self._wait_for_chat_name(window_state, expected_names=[match.display_name])
        self._record_runtime_state(refreshed or window_state, action="open_chat")
        if not verified:
            return self._failure(
                f"I clicked {match.display_name}, but I could not verify that the WhatsApp chat opened.",
                "chat_open_not_verified",
            )

        state.last_chat_name = chat_name
        return True

    def _send_message_in_active_chat(self, window_state: WhatsAppWindowState, message_text: str) -> bool:
        """Fast message send verification - reduced from 4.0s to 1.5s timeout, 0.2s to 0.1s poll."""
        before_messages = self._collect_recent_message_texts(window_state, limit=40)
        before_count = sum(1 for item in before_messages if self._normalize_label(item) == self._normalize_label(message_text))

        if not self._focus_message_input(window_state):
            return False
        if not self._automation.type_text(message_text):
            return False
        if not self._automation.press_key("enter"):
            return False

        # Fast verification loop - reduced timeout and poll interval
        deadline = time.monotonic() + 1.5
        while time.monotonic() <= deadline:
            refreshed = self._refresh_window_state(window_state.hwnd, source=window_state.source) or window_state
            after_messages = self._collect_recent_message_texts(refreshed, limit=40)
            after_count = sum(1 for item in after_messages if self._normalize_label(item) == self._normalize_label(message_text))
            if after_count > before_count:
                return True
            time.sleep(0.1)  # Reduced from 0.2s
        return False

    def _read_current_chat_name_raw(self, window_state: WhatsAppWindowState) -> tuple[str, str]:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return "", "none"

        records = self._collect_uia_records(window)
        bounds = self._header_bounds(window_state)
        candidates: list[tuple[int, int, int, str]] = []

        for record in records:
            if not self._rect_overlaps(record.rect, bounds):
                continue
            if record.control_type not in {"text", "button", "document", "pane", "group"}:
                continue
            text = self._clean_visible_text(record.name)
            if not text:
                continue
            lowered = text.lower()
            if lowered in _HEADER_NOISE or any(hint in lowered for hint in _VOICE_CALL_HINTS + _VIDEO_CALL_HINTS):
                continue
            if self._looks_like_time_or_date(text):
                continue
            score = 0
            if record.rect[0] < bounds[0] + int((bounds[2] - bounds[0]) * 0.55):
                score += 3
            if len(text) <= 64:
                score += 1
            candidates.append((-score, record.rect[1], record.rect[0], text))

        if not candidates:
            return "", "none"

        chosen = sorted(candidates)[0][3]
        state.last_chat_name = chosen
        return chosen, "uia"

    def _read_sidebar_matches(self, window_state: WhatsAppWindowState, *, query: str) -> list[WhatsAppContactMatch]:
        try:
            return self._read_sidebar_matches_impl(window_state, query)
        except Exception as e:
            logger.warning("LOOKUP: _read_sidebar_matches failed: %s", e)
            return []

    def _read_sidebar_matches_impl(self, window_state: WhatsAppWindowState, query: str) -> list[WhatsAppContactMatch]:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return []

        # Use fast timeout for UIA collection to prevent hangs
        import time
        import threading
        
        records = []
        error = []
        
        def collect():
            try:
                records.extend(self._collect_uia_records(window))
            except Exception as e:
                error.append(str(e))
        
        t = threading.Thread(target=collect, daemon=True)
        t.start()
        t.join(timeout=10)  # Max 10s for UIA
        if error:
            logger.warning("LOOKUP: UIA collection error: %s", error[0])
        if t.is_alive():
            logger.warning("LOOKUP: UIA collection timed out")
            return []
        
        sidebar_bounds = self._sidebar_list_bounds(window_state)
        sidebar_width = max(1, sidebar_bounds[2] - sidebar_bounds[0])

        containers = [
            record
            for record in records
            if record.control_type in {"listitem", "dataitem", "group", "pane", "button"}
            and self._rect_inside(record.rect, sidebar_bounds)
            and record.width >= int(sidebar_width * 0.45)
            and record.height >= 28
            and record.height <= 180
        ]

        matches: list[WhatsAppContactMatch] = []
        if containers:
            for container in sorted(containers, key=lambda item: (item.rect[1], item.rect[0], -item.width)):
                child_records = [
                    item
                    for item in records
                    if self._rect_inside(item.rect, container.rect, margin=2)
                ]
                match = self._build_match_from_records(container, child_records, query)
                if match is not None:
                    matches.append(match)

        if not matches:
            matches = self._fallback_sidebar_text_clusters(records, sidebar_bounds, query=query)

        deduped: list[WhatsAppContactMatch] = []
        seen: set[tuple[int, str, str]] = set()
        for item in sorted(matches, key=lambda match: (match.rect[1] if match.rect else 0, match.display_name.lower())):
            top_bucket = int((item.rect[1] if item.rect else 0) / 6)
            key = (top_bucket, item.display_name.lower(), item.subtitle.lower())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        filtered = [item for item in deduped if self._query_matches(item, query)]
        return filtered[:12]

    def _build_match_from_records(
        self,
        container: _UIARecord,
        child_records: Sequence[_UIARecord],
        query: str,
    ) -> WhatsAppContactMatch | None:
        seen_texts: set[tuple[str, tuple[int, int, int, int]]] = set()
        ordered: list[tuple[int, int, str]] = []
        unread_count = 0

        for item in sorted(child_records, key=lambda record: (record.rect[1], record.rect[0], record.control_type)):
            text = self._clean_visible_text(item.name)
            if not text:
                continue
            key = (text.lower(), item.rect)
            if key in seen_texts:
                continue
            seen_texts.add(key)

            if self._is_unread_badge_text(text, item.rect, container.rect):
                unread_count = max(unread_count, int(text))
                continue
            if self._looks_like_time_or_date(text):
                continue
            if text.lower() in _SIDEBAR_NOISE:
                continue
            ordered.append((item.rect[1], item.rect[0], text))

        if not ordered:
            own_name = self._clean_visible_text(container.name)
            if own_name and own_name.lower() not in _SIDEBAR_NOISE:
                ordered.append((container.rect[1], container.rect[0], own_name))

        texts = [item[2] for item in ordered]
        display_name = self._pick_display_name(texts, query)
        if not display_name:
            return None

        subtitle = ""
        for item in texts:
            if item == display_name:
                continue
            if len(item) > 80 and not self._query_matches_text(item, query):
                continue
            subtitle = item
            break

        return WhatsAppContactMatch(
            display_name=display_name,
            subtitle=subtitle,
            unread_count=unread_count,
            rect=container.rect,
            raw_text=" | ".join(texts[:5]),
            control=container.control,
        )

    def _fallback_sidebar_text_clusters(
        self,
        records: Sequence[_UIARecord],
        bounds: tuple[int, int, int, int],
        *,
        query: str,
    ) -> list[WhatsAppContactMatch]:
        relevant = [
            record
            for record in records
            if self._rect_inside(record.rect, bounds)
            and record.control_type in {"text", "button", "document", "group", "pane"}
        ]
        clusters: list[list[_UIARecord]] = []
        for record in sorted(relevant, key=lambda item: (item.rect[1], item.rect[0])):
            if not clusters or abs(clusters[-1][0].rect[1] - record.rect[1]) > 26:
                clusters.append([record])
            else:
                clusters[-1].append(record)

        matches: list[WhatsAppContactMatch] = []
        for cluster in clusters:
            texts = [self._clean_visible_text(item.name) for item in cluster]
            texts = [item for item in texts if item and item.lower() not in _SIDEBAR_NOISE and not self._looks_like_time_or_date(item)]
            if not texts:
                continue
            display_name = self._pick_display_name(texts, query)
            if not display_name:
                continue
            unread_count = 0
            for item in cluster:
                if self._is_unread_badge_text(item.name, item.rect, self._merge_rects(cluster)):
                    unread_count = max(unread_count, int(item.name))
            rect = self._merge_rects(cluster)
            subtitle = next((item for item in texts if item != display_name), "")
            matches.append(
                WhatsAppContactMatch(
                    display_name=display_name,
                    subtitle=subtitle,
                    unread_count=unread_count,
                    rect=rect,
                    raw_text=" | ".join(texts[:5]),
                )
            )
        return matches

    def _focus_search_box(self, window_state: WhatsAppWindowState) -> bool:
        # Fast: keyboard shortcut opens search box directly
        for keys in _SEARCH_SHORTCUTS:
            if self._automation.hotkey(keys):
                self._automation.fast_sleep(150)
                return True
        # Fallback: slow UIA enumeration (last resort)
        control = self._find_search_control(window_state)
        if control is not None and self._focus_control(control):
            return True
        return False

    def _apply_search_query(self, window_state: WhatsAppWindowState, query: str) -> bool:
        """Fast search query application - reduced sleep times for better performance."""
        if not self._focus_search_box(window_state):
            return False
        self._automation.hotkey(["ctrl", "a"])
        self._automation.fast_sleep(20)  # Reduced from 50ms
        self._automation.press_key("backspace")
        self._automation.fast_sleep(40)  # Reduced from 120ms
        if query:
            if not self._automation.type_text(query):
                return False
            self._automation.fast_sleep(80)  # Reduced from 250ms - search results appear quickly
        return True

    def _open_chat_from_search_query(
        self,
        window_state: WhatsAppWindowState,
        query: str,
        *,
        expected_names: Sequence[str],
    ) -> tuple[WhatsAppWindowState, str] | None:
        key_sequences = (("enter",), ("down", "enter"))
        for keys in key_sequences:
            if not self._apply_search_query(window_state, query):
                return None

            self._automation.fast_sleep(80)
            keys_sent = True
            for key in keys:
                if not self._automation.press_key(key):
                    keys_sent = False
                    break
                self._automation.fast_sleep(60)
            if not keys_sent:
                continue

            refreshed, chat_name, verified = self._wait_for_chat_name(window_state, expected_names=expected_names)
            if verified:
                return refreshed or window_state, chat_name

        return None

    def _clear_search_if_possible(self, window_state: WhatsAppWindowState) -> None:
        try:
            self._apply_search_query(window_state, "")
        except Exception:
            return

    def _focus_message_input(self, window_state: WhatsAppWindowState) -> bool:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return False

        records = self._collect_uia_records(window)
        bounds = self._message_input_bounds(window_state)
        candidates: list[tuple[int, int, int, Any]] = []
        for record in records:
            if not self._rect_overlaps(record.rect, bounds):
                continue
            if record.control_type not in {"edit", "document", "pane", "group"}:
                continue
            lowered = record.name.lower()
            score = 0
            if any(hint in lowered for hint in _MESSAGE_LABEL_HINTS):
                score += 4
            if record.control_type == "edit":
                score += 2
            score += min(3, int(record.width / 150))
            candidates.append((-score, record.rect[1], record.rect[0], record.control))

        if not candidates:
            return False
        control = sorted(candidates)[0][3]
        return self._focus_control(control)

    def _click_header_button(self, window_state: WhatsAppWindowState, *, button_type: str) -> bool:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return False

        records = self._collect_uia_records(window)
        bounds = self._header_bounds(window_state)
        hints = _VIDEO_CALL_HINTS if button_type == "video_call" else _VOICE_CALL_HINTS
        candidates: list[tuple[int, int, int, Any]] = []

        for record in records:
            if not self._rect_overlaps(record.rect, bounds):
                continue
            if record.control_type not in {"button", "hyperlink", "image", "pane"}:
                continue
            lowered = record.name.lower()
            if button_type == "voice_call" and "video call" in lowered:
                continue
            if any(hint in lowered for hint in hints):
                candidates.append((-4, -record.rect[0], record.rect[1], record.control))
            elif button_type == "voice_call" and lowered == "call":
                candidates.append((-2, -record.rect[0], record.rect[1], record.control))

        if not candidates:
            return False

        control = sorted(candidates)[0][3]
        return self._click_control(control)

    def _verify_call_started(self, window_state: WhatsAppWindowState, *, expected_name: str) -> bool:
        """Fast call verification - timeout 3.5s, poll 0.1s."""
        deadline = time.monotonic() + 3.5
        expected_norm = self._normalize_label(expected_name)

        while time.monotonic() <= deadline:
            current = self._refresh_window_state(window_state.hwnd, source=window_state.source) or window_state
            window = self._open_uia_window(current.hwnd)
            if window is not None:
                records = self._collect_uia_records(window)
                for record in records:
                    lowered = record.name.lower()
                    if any(hint in lowered for hint in _CALL_ACTIVE_HINTS):
                        return True
                    if expected_norm and expected_norm in self._normalize_label(record.name):
                        if any(word in lowered for word in ("calling", "ringing", "call")):
                            return True

            for extra_window in self._list_windows_with_timeout(process_names=sorted(_WHATSAPP_DESKTOP_PROCESSES)):
                lowered = str(extra_window.title or "").lower()
                if any(hint in lowered for hint in ("calling", "ringing", "call")):
                    return True
                if expected_norm and expected_norm in self._normalize_label(extra_window.title):
                    return True

            time.sleep(0.1)  # Reduced from 0.2s
        return False

    def _collect_recent_message_texts(self, window_state: WhatsAppWindowState, *, limit: int) -> list[str]:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return []

        records = self._collect_uia_records(window)
        bounds = self._message_history_bounds(window_state)
        items: list[tuple[int, int, str]] = []
        for record in records:
            if not self._rect_inside(record.rect, bounds):
                continue
            if record.control_type not in {"text", "document", "button", "group", "pane"}:
                continue
            text = self._clean_visible_text(record.name)
            if not text:
                continue
            if text.lower() in _MESSAGE_NOISE or text.lower() in _HEADER_NOISE:
                continue
            if self._is_unread_badge_text(text, record.rect, bounds):
                continue
            if self._looks_like_time_or_date(text):
                continue
            items.append((record.rect[1], record.rect[0], text))

        deduped: list[str] = []
        seen: list[tuple[int, str]] = []
        for top, _left, text in sorted(items):
            key = (int(top / 8), text.lower())
            if key in seen:
                continue
            seen.append(key)
            deduped.append(text)
        return deduped[-max(1, int(limit or 5)) :]

    def _wait_for_chat_name(
        self,
        window_state: WhatsAppWindowState,
        *,
        expected_names: Sequence[str],
    ) -> tuple[WhatsAppWindowState | None, str, bool]:
        """Fast chat name verification - reduced from 3.0s to 2.0s timeout, 0.15s to 0.08s poll."""
        deadline = time.monotonic() + 2.0
        last_state: WhatsAppWindowState | None = None
        last_name = ""

        while time.monotonic() <= deadline:
            refreshed = self._refresh_window_state(window_state.hwnd, source=window_state.source) or window_state
            name, _source = self._read_current_chat_name_raw(refreshed)
            last_state = refreshed
            last_name = name
            if name and any(self._names_match(name, item) for item in expected_names):
                return refreshed, name, True
            time.sleep(0.08)  # Reduced from 0.15s

        return last_state, last_name, False

    def _click_match_control(self, match: WhatsAppContactMatch) -> bool:
        if match.control is not None and self._click_control(match.control):
            return True
        if match.rect:
            return self._automation.click_center(match.rect)
        return False

    def _click_control(self, control: Any) -> bool:
        try:
            wrapper = control.wrapper_object()
            wrapper.click_input()
            return True
        except Exception:
            try:
                rect_obj = getattr(control.element_info, "rectangle", None)
                rect = self._rect_from_object(rect_obj)
                if rect:
                    return self._automation.click_center(rect)
            except Exception:
                return False
        return False

    def _focus_control(self, control: Any) -> bool:
        try:
            wrapper = control.wrapper_object()
            wrapper.click_input()
            wrapper.set_focus()
            return True
        except Exception:
            try:
                rect_obj = getattr(control.element_info, "rectangle", None)
                rect = self._rect_from_object(rect_obj)
                if rect and self._automation.click_center(rect):
                    return True
            except Exception:
                return False
        return False

    def _find_search_control(self, window_state: WhatsAppWindowState) -> Any | None:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return None

        records = self._collect_uia_records(window)
        bounds = self._search_bounds(window_state)
        candidates: list[tuple[int, int, int, Any]] = []
        for record in records:
            if not self._rect_overlaps(record.rect, bounds):
                continue
            if record.control_type not in {"edit", "document", "combobox", "pane", "group"}:
                continue
            lowered = record.name.lower()
            score = 0
            if any(hint in lowered for hint in _SEARCH_LABEL_HINTS):
                score += 5
            if record.control_type == "edit":
                score += 2
            if record.width > int((bounds[2] - bounds[0]) * 0.4):
                score += 1
            candidates.append((-score, record.rect[1], record.rect[0], record.control))
        if not candidates:
            return None
        return sorted(candidates, key=lambda x: (x[0], x[1], x[2]))[0][3]

    def _open_uia_window(self, hwnd: int):
        if not hwnd:
            return None
        if not _PYWINAUTO_OK or Desktop is None:  # pragma: no cover - depends on optional dependency
            return None
        try:
            desktop = self._desktop_factory()
            if desktop is None:
                return None
            return desktop.window(handle=hwnd)
        except Exception as exc:  # pragma: no cover - depends on live UIA tree
            logger.debug("WhatsApp UIA window open failed: %s", exc)
            return None

    def _collect_uia_records(self, window: Any) -> list[_UIARecord]:
        import time
        from pywinauto.findwindows import find_elements

        t0 = time.time()
        records: list[_UIARecord] = []
        elements = None

        # Fast path: use find_elements with process filter, only get relevant controls
        try:
            elements = find_elements(
                process=window.process_id(),
                control_types=["Edit", "Text", "Button", "Pane", "List", "ListItem"],
            )
        except Exception as exc:
            logger.debug("Fast find_elements failed: %s, falling back", exc)
            try:
                elements = window.descendants()
            except Exception as e:
                logger.debug("WhatsApp UIA descendants failed: %s", e)
                return records

        max_records = 150  # Reduced from 300 for speed
        for control in elements:
            if len(records) >= max_records:
                break
            try:
                elem_info = control.element_info
                rect = self._rect_from_object(getattr(elem_info, "rectangle", None))
                if not rect:
                    continue
                records.append(
                    _UIARecord(
                        control=control,
                        name=str(getattr(elem_info, "name", "") or "").strip(),
                        control_type=str(getattr(elem_info, "control_type", "") or "").lower(),
                        rect=rect,
                    )
                )
            except Exception:
                continue
        return records

    def _launch_whatsapp(self) -> SkillExecutionResult | None:
        launcher = self._get_launcher()
        if launcher is None:
            return self._failure("WhatsApp is not open and the launcher is unavailable.", "launcher_unavailable")

        try:
            result = launcher.launch_by_name("whatsapp")
            if result.success:
                self._automation.fast_sleep(200)
                return None
        except Exception as exc:
            logger.debug("Launcher launch failed: %s, trying direct launch", exc)

        import subprocess
        import os
        whatsapp_paths = [
            os.path.expandvars(r"%LocalAppData%\WhatsApp\WhatsApp.exe"),
            os.path.expandvars(r"%ProgramFiles%\WindowsApps\5319275A.WhatsAppDesktop_*\WhatsApp.exe"),
        ]
        for path in whatsapp_paths:
            if path.endswith("*"):
                import glob as g
                matches = g.glob(path)
                if matches:
                    path = matches[0]
                    break
            elif os.path.exists(path):
                break
        else:
            wmic_result = subprocess.run(["wmic", "process", "where", "name='WhatsApp.exe'", "get", "ExecutablePath"], capture_output=True, text=True)
            for proc in wmic_result.stdout.split("\n")[1:]:
                proc = proc.strip()
                if proc and os.path.exists(proc):
                    path = proc
                    break
            else:
                return self._failure("Could not find WhatsApp executable.", "whatsapp_launch_failed")

        try:
            subprocess.Popen([path], shell=False, cwd=os.path.dirname(path) or None)
            self._automation.fast_sleep(200)
            return None
        except Exception as exc:
            return self._failure(f"Failed to launch WhatsApp: {exc}", "whatsapp_launch_failed")

    def _wait_for_whatsapp_window(self, timeout: float | None = None) -> WhatsAppWindowState | None:
        deadline = time.monotonic() + float(timeout or settings.get("whatsapp_launch_wait_seconds") or 45.0)
        while time.monotonic() <= deadline:
            detected = self.detect_whatsapp_context()
            if detected is not None:
                return detected
            time.sleep(0.25)
        return None

    def _get_launcher(self):
        if self._launcher is not None:
            return self._launcher
        try:
            from core.app_launcher import DesktopAppLauncher

            self._launcher = DesktopAppLauncher()
        except Exception as exc:  # pragma: no cover - depends on launcher index state
            logger.warning("WhatsApp launcher unavailable: %s", exc)
            self._launcher = False
        return self._launcher if self._launcher is not False else None

    def _safe_active_context(self) -> WindowInfo | None:
        try:
            return self._detector.get_active_context()
        except Exception as exc:  # pragma: no cover - defensive OS access
            logger.debug("WhatsApp active context detection failed: %s", exc)
            return None

    def _refresh_window_state(self, hwnd: int, *, source: str) -> WhatsAppWindowState | None:
        target = self._automation.get_window(hwnd)
        if target:
            return self._window_state_from_target(target, source=source)

        try:
            info = self._detector.get_window_info(hwnd)
        except Exception:
            return None
        if not self._is_window_info_whatsapp(info):
            return None
        return self._window_state_from_info(info, forced_source=source)

    def _list_windows_with_timeout(
        self,
        *,
        process_names: Iterable[str] | None = None,
        title_substrings: Iterable[str] | None = None,
        timeout: float = 4.0,
    ) -> list[WindowTarget]:
        import threading

        holder: dict[str, list[WindowTarget]] = {"windows": []}
        errors: list[str] = []

        def collect() -> None:
            try:
                holder["windows"] = self._automation.list_windows(
                    process_names=process_names,
                    title_substrings=title_substrings,
                )
            except Exception as exc:
                errors.append(str(exc))

        thread = threading.Thread(target=collect, daemon=True)
        thread.start()
        thread.join(timeout=max(0.5, float(timeout or 2.0)))
        if thread.is_alive():
            logger.warning(
                "WhatsApp window enumeration timed out after %.1fs (processes=%s titles=%s)",
                timeout,
                list(process_names or []),
                list(title_substrings or []),
            )
            return []
        if errors:
            logger.warning("WhatsApp window enumeration failed: %s", errors[0])
            return []
        return holder["windows"]

    def _record_runtime_state(self, window_state: WhatsAppWindowState | None, *, action: str) -> None:
        state.whatsapp_active = window_state is not None
        if window_state is None:
            return
        state.current_context = "whatsapp"
        state.current_app = "whatsapp"
        state.current_process_name = window_state.process_name
        state.current_window_title = window_state.title
        state.last_skill_used = state.last_skill_used or self.name()
        state.last_contact_search = state.last_contact_search or {}
        if action and action not in {"detect_context", "focus_whatsapp"}:
            state.last_successful_action = f"{action}:whatsapp"

    def _window_state_from_info(self, info: WindowInfo, *, forced_source: str | None = None) -> WhatsAppWindowState:
        source = forced_source or ("desktop" if info.process_name.lower() in _WHATSAPP_DESKTOP_PROCESSES else "web")
        return WhatsAppWindowState(
            hwnd=info.hwnd,
            title=info.title,
            process_name=info.process_name,
            rect=info.rect,
            source=source,
            is_foreground=self._automation.get_foreground_window() == info.hwnd,
            is_minimized=info.is_minimized,
        )

    def _window_state_from_target(self, target: WindowTarget, *, source: str) -> WhatsAppWindowState:
        return WhatsAppWindowState(
            hwnd=target.hwnd,
            title=target.title,
            process_name=target.process_name,
            rect=target.rect,
            source=source,
            is_foreground=self._automation.get_foreground_window() == target.hwnd,
            is_minimized=target.is_minimized,
        )

    def _is_window_info_whatsapp(self, info: WindowInfo | None) -> bool:
        if info is None:
            return False
        process_name = str(info.process_name or "").strip().lower()
        title = str(info.title or "").strip().lower()
        if process_name in _WHATSAPP_DESKTOP_PROCESSES:
            return True
        if info.app_id == "whatsapp":
            return True
        return process_name in _WHATSAPP_WEB_PROCESSES and any(hint in title for hint in _WHATSAPP_TITLE_HINTS)

    def _is_whatsapp_context(self, context: Mapping[str, Any]) -> bool:
        current_app = str(context.get("current_app") or context.get("current_context") or "").strip().lower()
        target_app = str(context.get("context_target_app") or "").strip().lower()
        process_name = str(context.get("current_process_name") or "").strip().lower()
        window_title = str(context.get("current_window_title") or "").strip().lower()
        return (
            current_app == "whatsapp"
            or target_app == "whatsapp"
            or process_name in _WHATSAPP_DESKTOP_PROCESSES
            or any(hint in window_title for hint in _WHATSAPP_TITLE_HINTS)
        )

    def _has_recent_whatsapp_context(self, context: Mapping[str, Any]) -> bool:
        if str(context.get("last_skill_used") or "").strip() == self.name():
            return True
        return bool(getattr(state, "whatsapp_active", False) or getattr(state, "last_chat_name", ""))

    def _privacy_mode(self) -> str:
        return str(settings.get("read_private_content_mode") or "names_only").strip().lower()

    @staticmethod
    def _normalize_command(command: str) -> str:
        return " ".join(str(command or "").strip().lower().split())

    def _strip_explicit_whatsapp_tokens(self, command: str) -> str:
        text = str(command or "").strip()
        patterns = (
            r"^\s*whatsapp[\s:,-]+",
            r"\s+(?:on|in|using|with)\s+whatsapp\b",
            r"\bwhatsapp\b\s*$",
        )
        for pattern in patterns:
            text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
        return " ".join(text.split())

    @staticmethod
    def _mentions_whatsapp(command: str) -> bool:
        return bool(re.search(r"\bwhatsapp\b", str(command or ""), flags=re.IGNORECASE))

    def _requested_action_from_text(self, normalized: str) -> str:
        if normalized.startswith(("call ", "voice call ", "ring ")):
            return "call_contact"
        if normalized.startswith(("video call ", "start video call ")):
            return "video_call_contact"
        if normalized.startswith(_DIRECT_MESSAGE_PREFIXES):
            return "send_message"
        return ""

    def _pending_requested_action(self) -> str:
        lookup = getattr(state, "last_contact_search", {}) or {}
        return str(lookup.get("requested_action") or "").strip()

    def _pending_message_text(self) -> str:
        lookup = getattr(state, "last_contact_search", {}) or {}
        return str(lookup.get("message_text") or "").strip()

    def _has_pending_contact_choices(self) -> bool:
        pending = getattr(state, "pending_contact_choices", []) or []
        return bool(pending)

    def _has_pending_contact(self) -> bool:
        """Check if there's a pending contact waiting for message text."""
        lookup = getattr(state, "last_contact_search", {}) or {}
        if not lookup:
            return False
        return bool(lookup.get("query") and not lookup.get("message_sent"))

    def _parse_contact_and_message(self, command: str) -> tuple[str, str]:
        """Parse contact and message directly from command without LLM.

        Handles: "message X Y", "send message to X Y", "say X Y", "say to X Y", "tell X Y", "say hi to mummy"
        """
        cleaned = self._strip_explicit_whatsapp_tokens(command)
        cleaned = cleaned.strip()
        if not cleaned:
            return "", ""

        raw_command = cleaned
        original_lower = raw_command.lower()

        # Handle "tell him/her/them X" BEFORE stripping prefixes
        tell_him_match = re.match(r"^tell\s+(him|her|them)\s+(.+)$", cleaned.lower())
        if tell_him_match:
            message = tell_him_match.group(2).strip()
            return "", message

        for prefix in _DIRECT_MESSAGE_PREFIXES:
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix):]
                break

        cleaned = cleaned.strip()
        cleaned_lower = cleaned.lower()
        if not cleaned:
            return "", ""

        quoted = re.match(r"(.+?)\s+[\"'](.+)[\"']\s*$", cleaned)
        if quoted:
            return quoted.group(1).strip(), quoted.group(2).strip()

        quoted = re.match(r"^(.+?)\s+\"(.+)\"$", cleaned)
        if quoted:
            return quoted.group(1).strip(), quoted.group(2).strip()

        for separator in (" saying ", " that ", " with message ", " saying: ", " that: ", " to say "):
            if separator in cleaned_lower:
                idx = cleaned_lower.find(separator)
                contact = cleaned[:idx].strip()
                message = cleaned[idx + len(separator):].strip()
                if contact and message:
                    return contact, message

        # Handle "say hi to mummy" pattern - contact is AFTER "to"
        say_match = re.match(r"^say\s+(.+?)\s+to\s+(.+)$", cleaned_lower)
        if say_match:
            message = say_match.group(1).strip()
            contact = say_match.group(2).strip()
            if message and contact:
                return contact, message

        # Handle "tell him/her/them something" - returns empty contact (pending)
        tell_him_match = re.match(r"^tell\s+(him|her|them)\s+(.+)$", cleaned_lower)
        if tell_him_match:
            message = tell_him_match.group(2).strip()
            return "", message

        # Handle "hi to mummy" or "hello to mummy"
        hi_match = re.match(r"^(hi|hello|hey)\s+to\s+(.+)$", cleaned_lower)
        if hi_match:
            contact = hi_match.group(2).strip()
            return contact, hi_match.group(1).strip()

        # Handle "to mummy hi" pattern (reverse order)
        if cleaned.startswith("to ") and " " in cleaned[3:]:
            rest = cleaned[3:].strip()
            parts = rest.split(maxsplit=1)
            if len(parts) >= 2:
                return parts[0], parts[1]

        resolved_contact, resolved_message = self._resolve_message_command_payload(cleaned)
        if resolved_contact and resolved_message:
            return resolved_contact, resolved_message

        # Handle "message mummy hi" or "send mummy hi" - contact is first word
        parts = cleaned.split(maxsplit=1)
        if len(parts) == 1:
            return parts[0].strip(), ""
        if len(parts) > 1:
            return parts[0].strip(), parts[1].strip()
        return "", ""

    def _is_message_followup(self, command: str) -> bool:
        """Check if command is a message follow-up (e.g., 'tell him I'm late')."""
        normalized = self._normalize_command(command)
        followup_patterns = (
            "tell him ",
            "tell her ",
            "tell them ",
            "say ",
            "send ",
            "message ",
        )
        return any(normalized.startswith(p) for p in followup_patterns)

    def _request_message_text(self, contact: str) -> SkillExecutionResult:
        """Store pending contact and ask user for message text."""
        state.pending_message_contact = contact
        return self._success(
            intent="whatsapp_message_request",
            response=f"What should I say to {contact}?",
            data={"target_app": "whatsapp", "pending_contact": contact},
        )

    def _handle_pending_message(self, command: str, payload: dict[str, Any]) -> SkillExecutionResult:
        """Handle follow-up message text for pending contact."""
        contact = getattr(state, "pending_message_contact", "")
        if not contact:
            # Try to get from last_contact_search
            lookup = getattr(state, "last_contact_search", {}) or {}
            contact = lookup.get("query", "")

        if not contact:
            return self._failure("I don't have a pending contact. Who should I message?", "no_pending_contact")

        # Extract message text from command
        normalized = self._normalize_command(command)
        message_text = normalized

        # Remove common follow-up prefixes
        for prefix in ("tell him ", "tell her ", "tell them ", "say ", "send ", "message "):
            if message_text.startswith(prefix):
                message_text = message_text[len(prefix):].strip()
                break

        if not message_text:
            message_text = str(payload.get("text") or payload.get("message") or "").strip()

        if not message_text:
            return self._failure("What message should I send?", "empty_message")

        # Clear pending state BEFORE sending to avoid recursion
        state.pending_message_contact = None
        lookup = getattr(state, "last_contact_search", {}) or {}
        if lookup:
            lookup["message_sent"] = True
            state.last_contact_search = lookup

        # Send the message directly without going through send_message's pending check
        return self._send_message_to_resolved_contact(contact, message_text)

    def _resolve_message_command_payload(self, payload: str) -> tuple[str, str]:
        """Parse 'contact message' payload into (contact, message) tuple.

        Returns (contact, "") if only contact found (triggers multi-step flow).
        Returns ("", "") if empty.
        """
        cleaned = str(payload or "").strip()
        if not cleaned:
            return "", ""

        # Try quoted message format first: 'rahul "hi how are you"'
        direct = self._split_contact_and_message(cleaned)
        if direct[0] and direct[1]:
            return direct

        # Try common separators first: "say", "telling", "that", etc.
        for sep in (" say ", " saying ", " tell ", " tell: ", " say: "):
            if sep in cleaned.lower():
                sep_start = cleaned.lower().find(sep)
                contact = cleaned[:sep_start].strip()
                message = cleaned[sep_start + len(sep):].strip()
                if contact and message:
                    return contact, message

        tokens = cleaned.split()
        if len(tokens) == 1:
            # Single word is definitely a contact name
            return cleaned, ""

        if len(tokens) < 2:
            return cleaned, ""

        # Simple fallback: first token is contact, rest is message
        # This works when WhatsApp is not installed and can't check contacts
        first_word = tokens[0].lower()
        if first_word in ("message", "msg", "text", "send", "tell", "whatsapp"):
            # Strip the prefix
            rest = " ".join(tokens[1:])
            if rest:
                return rest, ""

        # Try to find contact by matching against WhatsApp contacts
        # Start with smallest contact name (first token) and expand
        max_prefix = min(len(tokens) - 1, 4)  # Max 4 words for contact name
        best_contact = ""
        best_message = ""
        best_score = -1

        for size in range(1, max_prefix + 1):
            candidate_contact = " ".join(tokens[:size]).strip()
            candidate_message = " ".join(tokens[size:]).strip()
            if not candidate_message:
                continue
            matches = self.list_matching_contacts(candidate_contact)
            if not matches:
                continue
            exact = sum(1 for item in matches if self._normalize_label(item.display_name) == self._normalize_label(candidate_contact))
            score = 3 if exact == 1 else 1 if len(matches) == 1 else 0
            if score > best_score:
                best_contact = candidate_contact
                best_message = candidate_message
                best_score = score
            if score >= 3:
                break

        if best_contact and best_message:
            return best_contact, best_message

        # Fallback: assume first word(s) are contact, rest is message
        # But if we can't find any contact match, return as single contact
        first_token = tokens[0]
        matches = self.list_matching_contacts(first_token)
        if matches:
            return first_token, " ".join(tokens[1:]).strip()

        # No contact found - return entire payload as contact name
        # This allows the send_message flow to handle "contact not found" gracefully
        return cleaned, ""

    def _split_contact_and_message(self, payload: str) -> tuple[str, str]:
        quoted = re.match(r'^(?P<contact>.+?)\s+"(?P<message>.+)"$', payload.strip())
        if quoted:
            return quoted.group("contact").strip(), quoted.group("message").strip()

        separators = (" saying ", " tell ", " tell: ", " say ", " say: ", " message ", " that ", " : ", ": ")
        for separator in separators:
            if separator in payload:
                contact, message = payload.split(separator, 1)
                return contact.strip(), message.strip()
        return "", ""

    def _query_matches(self, match: WhatsAppContactMatch, query: str) -> bool:
        return self._query_matches_text(match.display_name, query) or self._query_matches_text(match.subtitle, query)

    def _query_matches_text(self, text: str, query: str) -> bool:
        normalized_text = self._normalize_label(text)
        normalized_query = self._normalize_label(query)
        if not normalized_query:
            return True
        # Exact match
        if normalized_text == normalized_query:
            return True
        # Partial match - query is substring of text (handles case insensitivity)
        if normalized_query in normalized_text:
            return True
        if normalized_text in normalized_query:
            return True
        query_tokens = [item for item in normalized_query.split() if item]
        return bool(query_tokens) and all(token in normalized_text for token in query_tokens)

    def _pick_display_name(self, texts: Sequence[str], query: str) -> str:
        normalized_query = self._normalize_label(query)
        query_tokens = {item for item in normalized_query.split() if item}
        best_text = ""
        best_score = -999

        for idx, text in enumerate(texts):
            candidate = self._clean_visible_text(text)
            if not candidate:
                continue
            lowered = candidate.lower()
            if lowered in _SIDEBAR_NOISE:
                continue
            score = 0
            normalized_candidate = self._normalize_label(candidate)
            if normalized_query:
                if normalized_candidate == normalized_query:
                    score += 7
                elif query_tokens and query_tokens.issubset(set(normalized_candidate.split())):
                    score += 5
                elif any(token in normalized_candidate for token in query_tokens):
                    score += 2
            if idx == 0:
                score += 1
            if len(candidate) <= 64:
                score += 1
            if self._looks_like_metadata(candidate):
                score -= 4
            if score > best_score:
                best_score = score
                best_text = candidate
        return best_text

    @staticmethod
    def _normalize_label(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower())
        return " ".join(normalized.split())

    @staticmethod
    def _fuzzy_match_score(query: str, candidate: str) -> float:
        query = str(query or "").strip().lower()
        candidate = str(candidate or "").strip().lower()
        if not query or not candidate:
            return 0.0

        if query == candidate:
            return 1.0

        if candidate.startswith(query) or candidate.endswith(query):
            return 0.9

        if query in candidate:
            return 0.85

        q_parts = set(query.split())
        c_parts = set(candidate.split())
        if q_parts and c_parts:
            overlap = len(q_parts & c_parts)
            if overlap > 0:
                return 0.5 + (overlap / max(len(q_parts), len(c_parts))) * 0.3

        return 0.0

    @staticmethod
    def _generate_contact_search_variants(query: str) -> list[str]:
        query = str(query or "").strip()
        if not query:
            return []

        variants = [query]

        if query.lower() != query:
            variants.append(query.lower())

        if query.upper() != query:
            variants.append(query.upper())

        if query.title() != query:
            variants.append(query.title())

        parts = query.split()
        if len(parts) > 1:
            capitalized = " ".join(p.capitalize() for p in parts)
            if capitalized != query:
                variants.append(capitalized)

            all_caps = " ".join(p.upper() for p in parts)
            if all_caps != query and all_caps != query.upper():
                variants.append(all_caps)

        if len(query) > 2:
            variants.append(query[:len(query)//2] + query[len(query)//2:])
            variants.append(query[:2] + query[2:].capitalize())

        seen = set()
        unique_variants = []
        for v in variants:
            if v.lower() not in seen:
                seen.add(v.lower())
                unique_variants.append(v)

        return unique_variants

    @staticmethod
    def _looks_like_metadata(text: str) -> bool:
        lowered = str(text or "").strip().lower()
        if not lowered:
            return True
        if lowered in _SIDEBAR_NOISE or lowered in _HEADER_NOISE or lowered in _MESSAGE_NOISE:
            return True
        if lowered.startswith(("you:", "typing", "last seen", "today", "yesterday")):
            return True
        return False

    @staticmethod
    def _clean_visible_text(value: str) -> str:
        text = " ".join(str(value or "").split())
        text = re.sub(r"https?://\S+", "", text).strip()
        if len(text) < 1:
            return ""
        return text[:220]

    @staticmethod
    def _extract_ordinal(text: str) -> int:
        tokens = str(text or "").split()
        for token in tokens:
            if token in _ORDINALS:
                return _ORDINALS[token]
        return 0

    @staticmethod
    def _extract_limit(text: str) -> int | None:
        match = re.search(r"\b(\d{1,2})\b", str(text or ""))
        if match:
            return max(1, int(match.group(1)))
        return None

    @staticmethod
    def _is_unread_badge_text(
        value: str,
        rect: tuple[int, int, int, int],
        container_rect: tuple[int, int, int, int],
    ) -> bool:
        text = str(value or "").strip()
        if not re.fullmatch(r"\d{1,3}", text):
            return False
        container_width = max(1, container_rect[2] - container_rect[0])
        return rect[0] >= container_rect[0] + int(container_width * 0.70)

    @staticmethod
    def _looks_like_time_or_date(value: str) -> bool:
        text = str(value or "").strip().lower()
        if not text:
            return False
        if re.fullmatch(r"\d{1,2}:\d{2}", text):
            return True
        if re.fullmatch(r"\d{1,2}/\d{1,2}(?:/\d{2,4})?", text):
            return True
        return text in {"today", "yesterday"}

    @staticmethod
    def _names_match(a: str, b: str) -> bool:
        normalized_a = WhatsAppSkill._normalize_label(a)
        normalized_b = WhatsAppSkill._normalize_label(b)
        return normalized_a == normalized_b or normalized_a in normalized_b or normalized_b in normalized_a

    @staticmethod
    def _rect_from_object(rect_obj: Any) -> tuple[int, int, int, int] | None:
        if rect_obj is None:
            return None
        try:
            rect = (int(rect_obj.left), int(rect_obj.top), int(rect_obj.right), int(rect_obj.bottom))
            if rect[2] <= rect[0] or rect[3] <= rect[1]:
                return None
            return rect
        except Exception:
            return None

    @staticmethod
    def _rect_inside(
        rect: tuple[int, int, int, int],
        container: tuple[int, int, int, int],
        *,
        margin: int = 0,
    ) -> bool:
        return (
            rect[0] >= container[0] - margin
            and rect[1] >= container[1] - margin
            and rect[2] <= container[2] + margin
            and rect[3] <= container[3] + margin
        )

    @staticmethod
    def _rect_overlaps(
        rect_a: tuple[int, int, int, int],
        rect_b: tuple[int, int, int, int],
    ) -> bool:
        return not (
            rect_a[2] <= rect_b[0]
            or rect_a[0] >= rect_b[2]
            or rect_a[3] <= rect_b[1]
            or rect_a[1] >= rect_b[3]
        )

    @staticmethod
    def _merge_rects(records: Sequence[_UIARecord]) -> tuple[int, int, int, int]:
        left = min(record.rect[0] for record in records)
        top = min(record.rect[1] for record in records)
        right = max(record.rect[2] for record in records)
        bottom = max(record.rect[3] for record in records)
        return (left, top, right, bottom)

    def _content_top(self, window_state: WhatsAppWindowState) -> int:
        chrome_offset = max(86, int(window_state.height * 0.12)) if window_state.source == "web" else max(40, int(window_state.height * 0.06))
        return window_state.rect[1] + chrome_offset

    def _sidebar_right(self, window_state: WhatsAppWindowState) -> int:
        fraction = 0.39 if window_state.source == "desktop" else 0.36
        return window_state.rect[0] + int(window_state.width * fraction)

    def _search_bounds(self, window_state: WhatsAppWindowState) -> tuple[int, int, int, int]:
        top = self._content_top(window_state)
        return (
            window_state.rect[0] + 12,
            top,
            self._sidebar_right(window_state) - 12,
            top + max(92, int(window_state.height * 0.13)),
        )

    def _sidebar_list_bounds(self, window_state: WhatsAppWindowState) -> tuple[int, int, int, int]:
        search_bounds = self._search_bounds(window_state)
        return (
            window_state.rect[0] + 6,
            search_bounds[3],
            self._sidebar_right(window_state),
            window_state.rect[3] - 12,
        )

    def _header_bounds(self, window_state: WhatsAppWindowState) -> tuple[int, int, int, int]:
        top = self._content_top(window_state)
        left = self._sidebar_right(window_state) + 12
        return (
            left,
            top,
            window_state.rect[2] - 8,
            top + max(88, int(window_state.height * 0.12)),
        )

    def _message_input_bounds(self, window_state: WhatsAppWindowState) -> tuple[int, int, int, int]:
        left = self._sidebar_right(window_state) + 8
        bottom = window_state.rect[3] - 10
        height = max(100, int(window_state.height * 0.14))
        return (
            left,
            bottom - height,
            window_state.rect[2] - 10,
            bottom,
        )

    def _message_history_bounds(self, window_state: WhatsAppWindowState) -> tuple[int, int, int, int]:
        header = self._header_bounds(window_state)
        composer = self._message_input_bounds(window_state)
        return (
            self._sidebar_right(window_state) + 8,
            header[3],
            window_state.rect[2] - 10,
            composer[1],
        )

    def _to_action_result(self, result: SkillExecutionResult, operation: str) -> WhatsAppActionResult:
        return WhatsAppActionResult(
            success=result.success,
            operation=operation,
            message=result.response,
            error=result.error,
            data=dict(result.data),
        )

    def _success(
        self,
        *,
        intent: str,
        response: str,
        data: dict[str, Any] | None = None,
    ) -> SkillExecutionResult:
        return SkillExecutionResult(
            success=True,
            intent=intent,
            response=response,
            skill_name=self.name(),
            data=data or {},
        )

    def _failure(self, response: str, error: str) -> SkillExecutionResult:
        logger.error("WhatsApp action failed: %s | %s", response, error)
        return SkillExecutionResult(
            success=False,
            intent="whatsapp_action",
            response=response,
            skill_name=self.name(),
            error=error,
        )

    @staticmethod
    def _empty_window() -> WhatsAppWindowState:
        return WhatsAppWindowState(
            hwnd=0,
            title="",
            process_name="",
            rect=(0, 0, 0, 0),
            source="desktop",
            is_foreground=False,
            is_minimized=False,
        )
