"""
WhatsApp Skill - Complete, production-ready WhatsApp automation.

Handles: messaging, voice/video calls, call controls, auto-launch, spelling correction.
"""
from __future__ import annotations

import os
import subprocess
import time
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from core import settings, state
from core.automation import DesktopAutomation
from core.logger import get_logger
from core.normalizer import normalize_command
from core.window_context import ActiveWindowDetector
from skills.base import SkillBase, SkillExecutionResult

logger = get_logger(__name__)

try:
    from pywinauto import Desktop

    _PYWINAUTO_OK = True
except Exception:
    Desktop = None
    _PYWINAUTO_OK = False

try:
    import win32gui
    import win32con
    import win32api
    _WIN32_OK = True
except ImportError:
    _WIN32_OK = False


_HEADER_NOISE = {
    "search",
    "menu",
    "more options",
    "video call",
    "voice call",
    "call",
    "camera",
    "back",
    "open profile details",
}
_MESSAGE_NOISE = {
    "type a message",
    "message",
    "emoji",
    "attach",
    "voice message",
    "send",
}
_SEARCH_SHORTCUTS = (["ctrl", "k"], ["ctrl", "f"], ["ctrl", "e"])
_VOICE_CALL_HINTS = ("voice call", "audio call", "call")
_VIDEO_CALL_HINTS = ("video call", "start video call")
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


@dataclass
class WhatsAppWindowState:
    hwnd: int
    title: str
    process_name: str
    rect: tuple[int, int, int, int]
    source: str = "desktop"
    is_foreground: bool = True
    is_minimized: bool = False

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
class _UIARecord:
    control: Any
    name: str
    control_type: str
    rect: tuple[int, int, int, int]
    value: str = ""

    @property
    def effective_text(self) -> str:
        return self.name or self.value


class WhatsAppSkill(SkillBase):
    """Production-ready WhatsApp automation with full feature support."""

    def __init__(
        self,
        *,
        automation: DesktopAutomation | None = None,
        detector: ActiveWindowDetector | None = None,
        launcher=None,
        desktop_factory: Callable[[], Any] | None = None,
        browser=None,
        controller=None,
    ):
        self._automation = automation or DesktopAutomation()
        self._detector = detector or ActiveWindowDetector()
        self._launcher = launcher
        self._desktop_factory = desktop_factory or (lambda: Desktop(backend="uia") if _PYWINAUTO_OK and Desktop is not None else None)
        self._browser = browser
        self._controller = controller

        self._spelling_corrections = {
            "chran": "charan",
            "charann": "charan",
            "charannn": "charan",
            "charan": "charan",
            "mohit": "mohit",
            "mohitt": "mohit",
            "mohitw": "mohit",
            "mohiit": "mohit",
            "daddy": "Daddy",
            "dad": "Daddy",
            "dadi": "Daddy",
            "daddyji": "Daddy",
            "mummy": "Mummy",
            "mom": "Mummy",
            "mumm": "mummy",
            "mummyji": "Mummy",
            "hemanth": "hemanth",
            "hemant": "hemanth",
            "hemnth": "hemanth",
            "hemnt": "hemanth",
            "hemanthk": "hemanth",
            "rahul": "rahul",
            "rahull": "rahul",
            "raahul": "rahul",
            "raj": "raj",
            "raaj": "raj",
            "raju": "raj",
            "john": "john",
            "jon": "john",
            "jhon": "john",
            "alex": "alex",
            "aleex": "alex",
            "allix": "alex",
            "smith": "smith",
            "smit": "smith",
            "smtih": "smith",
            "johny": "johny",
            "johnie": "johny",
            "jonny": "johny",
            "priya": "priya",
            "priy": "priya",
            "priyanka": "priya",
            "sita": "sita",
            "seeta": "sita",
            " Gita": "sita",
        }

        self._known_contacts = [
            "charan", "mohit", "daddy", "mummy", "mom", "dad",
            "rahul", "raj", "john", "alex", "smith", "johny", "hemanth",
            "priya", "sita", " Gita", "amit", "vikram", "sumit", "neha"
        ]

    def name(self) -> str:
        return "WhatsAppSkill"

    def can_handle(self, context: Mapping[str, Any], intent: str, command: str) -> bool:
        if not settings.get("whatsapp_skill_enabled"):
            return False

        cmd_lower = command.lower()

        if "whatsapp" in cmd_lower:
            return True

        message_starts = ["send", "message", "tell", "say", "text", "msg", "notify", "inform", "ping"]
        if any(cmd_lower.startswith(s) for s in message_starts):
            return True

        call_keywords = ["call", "ring", "dial", "video", "facetime"]
        if any(k in cmd_lower for k in call_keywords):
            return True

        call_actions = ["end", "hang", "speaker", "add", "mute", "unmute"]
        if any(a in cmd_lower for a in call_actions) and self._is_on_call(cmd_lower):
            return True

        if getattr(state, "whatsapp_active", False):
            return True

        return False

    def _is_on_call(self, cmd: str) -> bool:
        return any(x in cmd for x in ["call", "phone", "speaking"])

    def execute(self, command: str, context: Mapping[str, Any] | None = None, **params: Any) -> SkillExecutionResult:
        if context is not None:
            return self._execute_command(command, context)
        return self.execute_operation(command, **params)

    def health_check(self) -> dict[str, Any]:
        wa = self._find_window()
        return {
            "enabled": bool(settings.get("whatsapp_skill_enabled")),
            "whatsapp_running": wa is not None,
            "active": getattr(state, "whatsapp_active", False),
        }

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "target": "whatsapp",
            "supports": [
                "send_message",
                "voice_call",
                "video_call",
                "end_call",
                "toggle_speaker",
                "add_participant",
                "auto_launch",
            ],
        }

    def send_message(self, contact: str, message: str, reuse_open: bool = True) -> SkillExecutionResult:
        """Public method to send a message via WhatsApp.
        
        Args:
            contact: The recipient's name
            message: The message text to send
            reuse_open: If True, assumes WhatsApp is already open (optimization for multi-step commands)
        """
        if not contact:
            return self._failure("Who should I message?", "empty_contact")
        if not message:
            return self._failure(f"What message for {contact}?", "empty_message")

        corrected_contact = self._correct_spelling(contact)
        
        wa = self._find_window()
        
        if reuse_open:
            if wa is not None:
                logger.info("WhatsApp already open (detected via window), reusing it")
                try:
                    win32gui.BringWindowToTop(wa["hwnd"])
                    win32gui.ShowWindow(wa["hwnd"], win32con.SW_RESTORE)
                    self._fast_sleep(150)
                except Exception as e:
                    logger.debug(f"Failed to focus WhatsApp: {e}")
                state.whatsapp_active = True
                return self._send_message_fast(wa, corrected_contact, message)
            elif getattr(state, "whatsapp_active", False):
                logger.info("WhatsApp marked as active in state, checking window again")
                wa = self._find_window()
                if wa is not None:
                    state.whatsapp_active = True
                    return self._send_message_fast(wa, corrected_contact, message)
        
        return self._send_message(corrected_contact, message)

    def _execute_command(self, command: str, context: Mapping[str, Any]) -> SkillExecutionResult:
        cmd = command.lower().strip()
        pending_index = self._extract_ordinal(cmd)
        if pending_index and self._has_pending_contact_choices():
            lookup = getattr(state, "last_contact_search", {}) or {}
            return self._handle_pending_selection(
                pending_index,
                requested_action=str(lookup.get("requested_action") or "open_chat"),
                message_text=str(lookup.get("message_text") or ""),
            )

        if any(phrase in cmd for phrase in ("read unread", "unread chats", "show unread")):
            return self.read_unread_chats()
        if any(phrase in cmd for phrase in ("current chat", "chat name", "which chat is open", "who is this chat with")):
            return self.read_current_chat_name()

        if self._is_call_action(cmd):
            return self._handle_call_action(cmd)

        if self._is_video_call_command(cmd):
            contact = self._extract_call_contact(cmd)
            contact = self._correct_spelling(contact)
            return self.video_call_contact(contact)

        if self._is_voice_call_command(cmd):
            contact = self._extract_call_contact(cmd)
            contact = self._correct_spelling(contact)
            return self.call_contact(contact)

        contact, message = self._parse_contact_and_message(command)
        contact = self._correct_spelling(contact) if contact else ""

        pending_msg = getattr(state, "pending_whatsapp_message", {})

        normalized = normalize_command(command)
        yes_confirmation = normalized in ("yes", "y", "confirm", "ok", "okay", "yes please", "do it", "go ahead")

        if yes_confirmation and pending_msg and pending_msg.get("contact"):
            contact = pending_msg["contact"]
            message = pending_msg.get("message", "")
            logger.info(f"Resuming pending message to {contact}: {message}")
            state.pending_whatsapp_message = {}
            return self._send_message(contact, message)

        if not contact:
            return self._failure("Who would you like to reach?", "empty_contact")

        if not message:
            state.pending_whatsapp_message = {"contact": contact, "message": ""}
            return self._success(
                intent="whatsapp_ask_message",
                response=f"What message would you like to send to {contact}?",
                data={"pending": True, "contact": contact},
            )

        state.pending_whatsapp_message = {"contact": contact, "message": message}
        return self._send_message(contact, message)

    def _is_call_action(self, cmd: str) -> bool:
        action_words = ["end call", "hang up", "hangup", "disconnect", "speaker", "louder", "add person", "add call", "mute", "unmute"]
        return any(a in cmd for a in action_words)

    def _handle_call_action(self, cmd: str) -> SkillExecutionResult:
        wa = self._ensure_running()
        if wa is None:
            return self._failure("No WhatsApp call active", "no_active_call")

        if any(x in cmd for x in ["end", "hang", "disconnect"]):
            return self._end_current_call()

        if "speaker" in cmd or "louder" in cmd:
            return self._toggle_speaker()

        if "add" in cmd:
            contact = self._extract_name_after_words(cmd, ["add", "person", "call"])
            contact = self._correct_spelling(contact)
            return self._add_person_to_call(contact)

        if "mute" in cmd:
            return self._mute_call()

        if "unmute" in cmd:
            return self._unmute_call()

        return self._failure("Unknown call action", "unknown_action")

    def _is_voice_call_command(self, cmd: str) -> bool:
        patterns = ["call ", "ring ", "dial ", "make a call "]
        return any(cmd.startswith(p) for p in patterns) and "video" not in cmd

    def _is_video_call_command(self, cmd: str) -> bool:
        patterns = ["video call", "video ", "facetime", "start video"]
        return any(p in cmd for p in patterns)

    def _extract_call_contact(self, cmd: str) -> str:
        cleaned = re.sub(
            r"^(?:call|ring|dial|make a call|voice call|video call|video|facetime|start video)\s+",
            "",
            cmd.strip(),
            flags=re.IGNORECASE,
        )
        return cleaned.strip()

    def _extract_name_after_words(self, cmd: str, words: list[str]) -> str:
        for word in words:
            idx = cmd.find(word)
            if idx != -1:
                rest = cmd[idx + len(word):].strip()
                if rest:
                    parts = rest.split()
                    if parts:
                        return parts[0]
        return ""

    def _correct_spelling(self, name: str) -> str:
        if not name:
            return name
        name_lower = name.lower()
        
        if name_lower in self._spelling_corrections:
            corrected = self._spelling_corrections[name_lower]
            logger.info(f"Corrected '{name}' to '{corrected}'")
            return corrected

        for wrong, correct in self._spelling_corrections.items():
            if name_lower.startswith(wrong) and len(wrong) >= 3:
                return correct + name_lower[len(wrong):]

        fuzzy_correction = self._fuzzy_correct_contact(name_lower)
        if fuzzy_correction and fuzzy_correction != name_lower:
            logger.info(f"Fuzzy corrected '{name}' to '{fuzzy_correction}'")
            return fuzzy_correction

        return name
    
    def _fuzzy_correct_contact(self, name: str) -> str | None:
        if not name or len(name) < 2:
            return None
        
        name_tokens = set(name.replace("_", " ").split())
        
        for known in self._known_contacts:
            known_lower = known.lower()
            if known_lower == name:
                continue
            
            known_tokens = set(known_lower.replace("_", " ").split())
            
            common = name_tokens & known_tokens
            if len(common) >= max(1, min(len(name_tokens), len(known_tokens)) - 1):
                if len(name) >= 3 and len(known) >= 3:
                    if name[:3] == known_lower[:3] or name[-3:] == known_lower[-3:]:
                        return known
        
        for known in self._known_contacts:
            known_lower = known.lower()
            if len(name) >= 3 and len(known_lower) >= 3:
                distance = self._levenshtein_distance(name, known_lower)
                if distance <= 2 and distance < min(len(name), len(known_lower)) // 2:
                    return known
        
        return None
    
    @staticmethod
    def _levenshtein_distance(s1: str, s2: str) -> int:
        if len(s1) < len(s2):
            return len(s2) - len(s1)
        if len(s2) < len(s1):
            return len(s1) - len(s2)
        
        previous_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row
        return previous_row[-1]

    def _extract_contact_and_message(self, command: str) -> tuple[str, str]:
        return self._parse_contact_and_message(command)

    def _find_window(self) -> dict | None:
        if not _WIN32_OK:
            return None

        result = {"hwnd": None, "title": "", "rect": None}

        def callback(hwnd, _):
            if not win32gui.IsWindow(hwnd):
                return True
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd)
                if title and "whatsapp" in title.lower():
                    result["hwnd"] = hwnd
                    result["title"] = title
                    result["rect"] = win32gui.GetWindowRect(hwnd)
                    return False
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(callback, None)
        except Exception:
            pass

        return result if result["hwnd"] else None

    def _ensure_running(self) -> dict | None:
        wa = self._find_window()

        if wa is None:
            logger.info("WhatsApp not running, launching...")
            success = self._launch_whatsapp()
            if success:
                logger.info("Waiting for WhatsApp to start...")
                for attempt in range(8):
                    time.sleep(1)
                    wa = self._find_window()
                    if wa is not None:
                        break
                if wa is None:
                    logger.error("WhatsApp window not found after launch")
                    return None

        if wa is not None:
            try:
                for attempt in range(3):
                    win32gui.BringWindowToTop(wa["hwnd"])
                    win32gui.ShowWindow(wa["hwnd"], win32con.SW_RESTORE)
                    win32gui.SetForegroundWindow(wa["hwnd"])
                    time.sleep(0.3)
                    if win32gui.GetForegroundWindow() == wa["hwnd"]:
                        break
                    time.sleep(0.5)
                
                time.sleep(0.8)
                
                if win32gui.IsIconic(wa["hwnd"]):
                    win32gui.ShowWindow(wa["hwnd"], win32con.SW_RESTORE)
                    time.sleep(0.3)
                
                state.whatsapp_active = True
                logger.info("WhatsApp focused: hwnd=%d", wa["hwnd"])
                return wa
            except Exception as e:
                logger.debug(f"Failed to focus: {e}")

        return None

    def _launch_whatsapp(self) -> bool:
        """Launch WhatsApp using Windows Shell."""
        try:
            import win32api
            import win32con

            paths_to_try = [
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "WhatsApp", "WhatsApp.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "WhatsApp", "WhatsApp.exe"),
                r"C:\Program Files\WhatsApp\WhatsApp.exe",
                r"C:\Program Files (x86)\WhatsApp\WhatsApp.exe",
            ]

            for path in paths_to_try:
                if os.path.exists(path):
                    logger.info(f"Launching WhatsApp from: {path}")
                    win32api.ShellExecute(0, "open", path, None, None, win32con.SW_SHOWNORMAL)
                    return True

            win32api.ShellExecute(0, "open", "whatsapp:", None, None, win32con.SW_SHOWNORMAL)
            return True

        except Exception as e:
            logger.debug(f"ShellExecute failed: {e}")

        try:
            subprocess.Popen(
                ["powershell", "-Command", "Start-Process", "WhatsApp"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            return True
        except Exception as e:
            logger.debug(f"Powershell launch failed: {e}")

        return False

    def focus_whatsapp(self) -> WhatsAppWindowState | SkillExecutionResult:
        wa = self._ensure_running()
        if wa is None:
            return self._failure("Couldn't open WhatsApp. Please open it manually.", "whatsapp_unavailable")

        state.whatsapp_active = True
        live = self._live_window_state()
        if live is not None:
            return live
        return WhatsAppWindowState(
            hwnd=int(wa["hwnd"]),
            title=str(wa.get("title") or "WhatsApp"),
            process_name="whatsapp.exe",
            rect=tuple(wa.get("rect") or (0, 0, 0, 0)),
            source="desktop",
            is_foreground=True,
            is_minimized=False,
        )

    def detect_whatsapp_context(self) -> WhatsAppWindowState | None:
        return self._live_window_state()

    def list_matching_contacts(self, name: str) -> list[WhatsAppContactMatch]:
        contact_name = str(name or "").strip()
        if not contact_name:
            return []
        _window_state, matches, error = self._lookup_contacts(contact_name)
        if error is not None:
            return []
        return matches

    def call_contact(self, name: str) -> SkillExecutionResult:
        contact_name = str(name or "").strip()
        if not contact_name:
            return self._failure("Who do you want to call on WhatsApp?", "empty_contact")

        resolved = self._resolve_contact_match(contact_name, requested_action="call_contact")
        if isinstance(resolved, SkillExecutionResult):
            return resolved

        window_state, match = resolved
        if not self._click_header_button(window_state, button_type="voice_call"):
            return self._failure(
                f"I opened {match.display_name}, but the WhatsApp voice call button was not available.",
                "call_button_unavailable",
            )

        return self._success(
            intent="whatsapp_call",
            response=f"Calling {match.display_name}",
            data={"target_app": "whatsapp", "contact": match.display_name},
        )

    def video_call_contact(self, name: str) -> SkillExecutionResult:
        contact_name = str(name or "").strip()
        if not contact_name:
            return self._failure("Who do you want to video call on WhatsApp?", "empty_contact")

        resolved = self._resolve_contact_match(contact_name, requested_action="video_call_contact")
        if isinstance(resolved, SkillExecutionResult):
            return resolved

        window_state, match = resolved
        if not self._click_header_button(window_state, button_type="video_call"):
            return self._failure(
                f"I opened {match.display_name}, but the WhatsApp video call button was not available.",
                "video_call_button_unavailable",
            )

        return self._success(
            intent="whatsapp_video_call",
            response=f"Starting a video call with {match.display_name}",
            data={"target_app": "whatsapp", "contact": match.display_name},
        )

    def read_unread_chats(self) -> SkillExecutionResult:
        focused = self.focus_whatsapp()
        if isinstance(focused, SkillExecutionResult):
            return focused

        matches = self._read_sidebar_matches(focused, query="")
        unread = [item for item in matches if item.unread_count > 0]
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

    def _send_message(self, contact: str, message: str) -> SkillExecutionResult:
        if not contact:
            return self._failure("Who should I message?", "empty_contact")
        if not message:
            return self._failure(f"What message for {contact}?", "empty_message")
        return self._send_message_to_resolved_contact(contact, message)
    
    def _send_message_fast(self, wa: dict, contact: str, message: str) -> SkillExecutionResult:
        """Fast path for sending message when WhatsApp is already open and focused."""
        try:
            window_state = WhatsAppWindowState(
                hwnd=int(wa["hwnd"]),
                title=str(wa.get("title") or "WhatsApp"),
                process_name="WhatsApp",
                rect=tuple(wa.get("rect") or (0, 0, 0, 0)),
                source="desktop",
                is_foreground=True,
                is_minimized=False,
            )
            
            resolved = self._resolve_contact_match_fast(window_state, contact, message)
            if isinstance(resolved, SkillExecutionResult):
                return resolved
            
            window_state, match = resolved
            sent = self._send_message_in_active_chat(window_state, message)
            if not sent:
                return self._failure(
                    f"I opened {match.display_name}, but couldn't send the message.",
                    "message_not_verified",
                )
            
            state.last_message_target = match.display_name
            return self._success(
                intent="whatsapp_message",
                response=f"Sent to {match.display_name}",
                data={
                    "target_app": "whatsapp",
                    "contact": match.display_name,
                    "message": message,
                    "verified": True,
                },
            )
        except Exception as e:
            logger.error(f"Fast message send failed: {e}")
            return self._failure(f"Couldn't send message to {contact}. Try again?", "send_failed")
    
    def _resolve_contact_match_fast(
        self,
        window_state: WhatsAppWindowState,
        contact_name: str,
        message_text: str,
    ) -> tuple[WhatsAppWindowState, WhatsAppContactMatch] | SkillExecutionResult:
        """Fast contact resolution using direct search."""
        direct_result = self._resolve_contact_via_direct_search(
            contact_name,
            requested_action="send_message",
            message_text=message_text,
        )
        if isinstance(direct_result, SkillExecutionResult):
            return direct_result
        if direct_result is not None:
            return direct_result
        
        window_state, matches, error = self._lookup_contacts(contact_name, window_state=window_state)
        if error is not None:
            return error
        
        if not matches:
            return self._failure(f"Couldn't find {contact_name} in WhatsApp.", "contact_not_found")
        
        chosen = self._choose_contact_match(contact_name, matches)
        if chosen is None:
            return self._duplicate_prompt(contact_name, matches)
        
        opened = self._open_contact_match(window_state, chosen)
        if isinstance(opened, SkillExecutionResult):
            return opened
        
        return window_state, chosen

    def _send_message_to_resolved_contact(self, contact_name: str, message_text: str) -> SkillExecutionResult:
        resolved = self._resolve_contact_match(
            contact_name,
            requested_action="send_message",
            message_text=message_text,
        )
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
        return self._success(
            intent="whatsapp_message",
            response=f"Message sent to {match.display_name}",
            data={
                "target_app": "whatsapp",
                "contact": match.display_name,
                "message": message_text,
                "verified": True,
            },
        )

    def _handle_pending_selection(
        self,
        index: int,
        *,
        requested_action: str,
        message_text: str,
    ) -> SkillExecutionResult:
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
            matches = [
                WhatsAppContactMatch(
                    display_name=str(item.get("display_name") or ""),
                    subtitle=str(item.get("subtitle") or ""),
                    index=int(item.get("index") or idx + 1),
                    unread_count=int(item.get("unread_count") or 0),
                    source=str(item.get("source") or "state"),
                    rect=tuple(item.get("rect")) if item.get("rect") else None,
                    raw_text=str(item.get("raw_text") or ""),
                )
                for idx, item in enumerate(getattr(state, "pending_contact_choices", []) or [])
            ]

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

        if requested_action == "call_contact":
            if not self._click_header_button(window_state, button_type="voice_call"):
                return self._failure(
                    f"I opened {match.display_name}, but the WhatsApp voice call button was not available.",
                    "call_button_unavailable",
                )
            return self._success(
                intent="whatsapp_call",
                response=f"Calling {match.display_name}",
                data={"target_app": "whatsapp", "contact": match.display_name},
            )

        if requested_action == "video_call_contact":
            if not self._click_header_button(window_state, button_type="video_call"):
                return self._failure(
                    f"I opened {match.display_name}, but the WhatsApp video call button was not available.",
                    "video_call_button_unavailable",
                )
            return self._success(
                intent="whatsapp_video_call",
                response=f"Starting a video call with {match.display_name}",
                data={"target_app": "whatsapp", "contact": match.display_name},
            )

        if requested_action == "send_message":
            if not self._send_message_in_active_chat(window_state, message_text):
                return self._failure(
                    f"I opened {match.display_name}, but I could not verify that the WhatsApp message was sent.",
                    "message_not_verified",
                )
            state.last_message_target = match.display_name
            return self._success(
                intent="whatsapp_message",
                response=f"Message sent to {match.display_name}",
                data={"target_app": "whatsapp", "contact": match.display_name, "message": message_text},
            )

        return self._success(
            intent="whatsapp_open_chat",
            response=f"Opened chat with {match.display_name}",
            data={"target_app": "whatsapp", "contact": match.display_name},
        )

    def _resolve_contact_match(
        self,
        contact_name: str,
        *,
        requested_action: str,
        message_text: str = "",
    ) -> tuple[WhatsAppWindowState, WhatsAppContactMatch] | SkillExecutionResult:
        logger.info("Resolving contact: '%s' for action: %s", contact_name, requested_action)
        
        direct_result = self._resolve_contact_via_direct_search(
            contact_name,
            requested_action=requested_action,
            message_text=message_text,
        )
        if isinstance(direct_result, SkillExecutionResult):
            logger.warning("Direct search returned error result: %s", direct_result.intent)
            return direct_result
        if direct_result is not None:
            logger.info("Direct search succeeded for: %s", contact_name)
            return direct_result

        logger.info("Direct search failed, trying sidebar lookup for: %s", contact_name)
        window_state, matches, error = self._lookup_contacts(contact_name)
        if error is not None:
            logger.error("Sidebar lookup returned error: %s", error.intent)
            return error

        if not matches:
            logger.error("No matches found for contact: %s", contact_name)
            return self._failure(f"I couldn't find a WhatsApp contact named {contact_name}.", "contact_not_found")

        logger.info("Found %d matches via sidebar lookup", len(matches))
        self._store_contact_resolution_state(contact_name, requested_action, message_text, matches)

        chosen = self._choose_contact_match(contact_name, matches)
        if chosen is None:
            logger.info("Multiple matches, triggering disambiguation for: %s", contact_name)
            return self._duplicate_prompt(contact_name, matches)

        logger.info("Opening contact: %s", chosen.display_name)
        opened = self._open_contact_match(window_state, chosen)
        if isinstance(opened, SkillExecutionResult):
            logger.error("Failed to open contact: %s", opened.intent)
            return opened

        state.pending_contact_choices = []
        logger.info("Contact resolved successfully: %s", chosen.display_name)
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

        query_variants = self._generate_contact_search_variants(contact_name)
        if contact_name.lower() not in [v.lower() for v in query_variants]:
            query_variants.append(contact_name)
        if contact_name.lower() not in query_variants:
            query_variants.append(contact_name.lower())
        
        logger.info("Trying direct search for: %s with %d variants", contact_name, len(query_variants))
        
        for search_variant in query_variants:
            logger.debug("Direct search: '%s'", search_variant)
            
            if self._search_contact_keyboard(focused, search_variant):
                self._fast_sleep(300)
                
                refreshed, chat_name, verified = self._wait_for_chat_name(
                    focused, 
                    expected_names=[contact_name, search_variant, contact_name.lower()]
                )
                if verified and chat_name and refreshed is not None:
                    logger.info("Direct search succeeded: '%s' -> '%s'", search_variant, chat_name)
                    match = WhatsAppContactMatch(
                        display_name=chat_name,
                        index=1,
                        source="direct_search",
                        raw_text=search_variant,
                    )
                    self._store_contact_resolution_state(contact_name, requested_action, message_text, [match])
                    state.pending_contact_choices = []
                    return refreshed, match
            
            logger.debug("Direct search failed for: '%s'", search_variant)
        
        logger.warning("Direct search exhausted for: %s, trying sidebar lookup", contact_name)
        
        window_state, matches, error = self._lookup_contacts(contact_name, window_state=focused)
        if error is not None:
            return None
        if matches:
            chosen = self._choose_contact_match(contact_name, matches)
            if chosen:
                logger.info("Sidebar lookup found: %s", chosen.display_name)
                self._open_contact_match(focused, chosen)
                return focused, chosen
        
        logger.warning("All contact resolution methods failed for: %s", contact_name)
        return None

    def _lookup_contacts(
        self,
        contact_name: str,
        *,
        window_state: WhatsAppWindowState | None = None,
    ) -> tuple[WhatsAppWindowState, list[WhatsAppContactMatch], SkillExecutionResult | None]:
        query = str(contact_name or "").strip()
        if not query:
            return self._empty_window(), [], self._failure("Contact name is empty.", "empty_contact")

        focused = window_state or self.focus_whatsapp()
        if isinstance(focused, SkillExecutionResult):
            return self._empty_window(), [], focused

        all_matches: list[WhatsAppContactMatch] = []
        for search_variant in self._generate_contact_search_variants(query):
            if not self._apply_search_query(focused, search_variant):
                continue

            variant_matches = self._read_sidebar_matches(focused, query=search_variant)
            seen = {
                (self._normalize_label(item.display_name), self._normalize_label(item.subtitle))
                for item in all_matches
            }
            for match in variant_matches:
                key = (self._normalize_label(match.display_name), self._normalize_label(match.subtitle))
                if key not in seen:
                    all_matches.append(match)
                    seen.add(key)

            if all_matches:
                break

        for idx, item in enumerate(all_matches, 1):
            item.index = idx
        return focused, all_matches, None

    def _store_contact_resolution_state(
        self,
        query: str,
        requested_action: str,
        message_text: str,
        matches: list[WhatsAppContactMatch],
    ) -> None:
        state.last_contact_search = {
            "query": query,
            "requested_action": requested_action,
            "message_text": message_text,
            "count": len(matches),
            "timestamp": time.time(),
        }
        state.pending_contact_choices = [item.to_state() for item in matches]

    def _duplicate_prompt(self, contact_name: str, matches: list[WhatsAppContactMatch]) -> SkillExecutionResult:
        exact_count = sum(
            1
            for item in matches
            if self._normalize_label(item.display_name) == self._normalize_label(contact_name)
        )
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
        matches: list[WhatsAppContactMatch],
    ) -> WhatsAppContactMatch | None:
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]

        query_norm = self._normalize_label(query)
        exact = [item for item in matches if self._normalize_label(item.display_name) == query_norm]
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
            fuzzy_scores.sort(key=lambda item: item[1], reverse=True)
            best_match, best_score = fuzzy_scores[0]
            second_score = fuzzy_scores[1][1] if len(fuzzy_scores) > 1 else -1.0
            if best_score >= 0.85 and (second_score < 0 or (best_score - second_score) >= 0.2):
                return best_match
        return None

    def _open_contact_match(
        self,
        window_state: WhatsAppWindowState,
        match: WhatsAppContactMatch,
    ) -> bool | SkillExecutionResult:
        clicked = self._click_match_control(match)
        if not clicked and not self._automation.press_key("enter"):
            return self._failure(f"I found {match.display_name}, but I could not open that chat.", "contact_click_failed")

        refreshed, chat_name, verified = self._wait_for_chat_name(window_state, expected_names=[match.display_name])
        if not verified:
            return self._failure(
                f"I clicked {match.display_name}, but I could not verify that the WhatsApp chat opened.",
                "chat_open_not_verified",
            )

        if refreshed is not None:
            state.last_chat_name = chat_name
        return True

    def _send_message_in_active_chat(self, window_state: WhatsAppWindowState, message_text: str) -> bool:
        before_messages = self._collect_recent_message_texts(window_state, limit=15)
        self._focus_message_input(window_state)
        self._fast_sleep(80)
        if not self._automation.type_text(message_text):
            logger.error("Failed to type message text")
            return False
        self._fast_sleep(150)
        if not self._automation.press_key("enter"):
            logger.error("Failed to press Enter to send")
            return False
        self._fast_sleep(200)
        chat_name, _source = self._read_current_chat_name_raw(window_state)
        if chat_name:
            logger.info("Verifying message delivery in chat: %s", chat_name)
            verified, reason = self._wait_for_message_delivery(
                chat_name,
                message_text,
                before_messages,
                timeout=3.5,
            )
            if verified:
                logger.info("Message verified successfully")
                return True
            logger.warning("Primary verification failed: %s", reason)
        fallback_verified = self._verify_message_appeared(message_text)
        if fallback_verified:
            logger.info("Fallback verification succeeded")
            return True
        logger.error("All message verification methods failed")
        return False

    def _read_sidebar_matches(self, window_state: WhatsAppWindowState, *, query: str) -> list[WhatsAppContactMatch]:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return []

        bounds = self._sidebar_list_bounds(window_state)
        groups: dict[int, list[_UIARecord]] = {}
        for record in self._collect_uia_records(window):
            if not self._rect_overlaps(record.rect, bounds):
                continue
            text = self._clean_visible_text(record.effective_text)
            if not text or self._looks_like_metadata(text):
                continue
            bucket = int(record.rect[1] / 36)
            groups.setdefault(bucket, []).append(record)

        matches: list[WhatsAppContactMatch] = []
        for records in groups.values():
            ordered = sorted(records, key=lambda item: (item.rect[1], item.rect[0]))
            texts = [self._clean_visible_text(item.effective_text) for item in ordered]
            texts = [item for item in texts if item and not self._looks_like_metadata(item)]
            if not texts:
                continue

            display_name = self._pick_display_name(texts, query)
            if not display_name:
                continue

            subtitle = ""
            unread_count = 0
            for text in texts:
                if text == display_name:
                    continue
                if text.isdigit():
                    unread_count = max(unread_count, int(text))
                    continue
                if not subtitle:
                    subtitle = text

            rects = [item.rect for item in ordered]
            rect = (
                min(item[0] for item in rects),
                min(item[1] for item in rects),
                max(item[2] for item in rects),
                max(item[3] for item in rects),
            )
            match = WhatsAppContactMatch(
                display_name=display_name,
                subtitle=subtitle,
                unread_count=unread_count,
                rect=rect,
                raw_text=" | ".join(texts[:4]),
            )
            if self._query_matches(match, query):
                matches.append(match)

        matches.sort(key=lambda item: (item.rect[1] if item.rect else 0, item.display_name.lower()))
        for idx, item in enumerate(matches, 1):
            item.index = idx
        return matches[:12]

    def _focus_search_box(self, window_state: WhatsAppWindowState) -> bool:
        for keys in _SEARCH_SHORTCUTS:
            if self._automation.hotkey(keys):
                self._fast_sleep(180)
                logger.debug("Search box hotkey sent: %s", keys)
                return True
        
        logger.warning("All search shortcuts failed")
        return False

    def _search_contact_keyboard(self, window_state: WhatsAppWindowState, contact_name: str) -> bool:
        logger.info("Using keyboard-only contact search for: %s", contact_name)
        
        try:
            import win32gui
            import win32con
            import time as time_module
            if window_state.hwnd:
                for attempt in range(5):
                    win32gui.SetForegroundWindow(window_state.hwnd)
                    win32gui.ShowWindow(window_state.hwnd, win32con.SW_RESTORE)
                    time_module.sleep(0.1)
                    if win32gui.GetForegroundWindow() == window_state.hwnd:
                        break
        except Exception as e:
            logger.debug("Could not set foreground: %s", e)
        
        self._fast_sleep(300)
        
        for _ in range(3):
            try:
                win32gui.SetForegroundWindow(window_state.hwnd)
            except:
                pass
            self._fast_sleep(50)
        
        self._automation.hotkey(["ctrl", "k"])
        self._fast_sleep(600)
        
        try:
            win32gui.SetForegroundWindow(window_state.hwnd)
        except:
            pass
        self._fast_sleep(100)
        
        self._automation.type_text(contact_name)
        self._fast_sleep(1000)
        
        self._automation.press_key("down")
        self._fast_sleep(200)
        
        self._automation.press_key("enter")
        self._fast_sleep(1000)
        
        try:
            for _ in range(5):
                win32gui.SetForegroundWindow(window_state.hwnd)
                self._fast_sleep(100)
        except Exception:
            pass
        
        return True

    def _verify_search_box_opened(self, window_state: WhatsAppWindowState) -> bool:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return False
        try:
            for control in window.descendants():
                try:
                    name = getattr(getattr(control, 'element_info', None), 'name', '') or ''
                    if name and 'search' in name.lower() and len(name) < 20:
                        rect = getattr(getattr(control, 'element_info', None), 'rectangle', None)
                        if rect:
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _apply_search_query(self, window_state: WhatsAppWindowState, query: str) -> bool:
        if not self._focus_search_box(window_state):
            logger.warning("Failed to open search box")
            return False
        self._fast_sleep(100)
        self._automation.hotkey(["ctrl", "a"])
        self._fast_sleep(50)
        self._automation.press_key("backspace")
        self._fast_sleep(80)
        if query:
            if not self._automation.type_text(query):
                logger.warning("Failed to type search query: %s", query)
                self._automation.press_key("escape")
                return False
            self._fast_sleep(200)
        return True

    def _open_chat_from_search_query(
        self,
        window_state: WhatsAppWindowState,
        query: str,
        *,
        expected_names: list[str],
    ) -> tuple[WhatsAppWindowState, str] | None:
        if not query:
            logger.error("Empty query in _open_chat_from_search_query")
            return None
            
        logger.info("Opening chat for '%s' with expected names: %s", query, expected_names)
        
        key_sequences = (("enter",), ("down", "enter"), ("down", "down", "enter"), ("down", "down", "down", "enter"))
        for seq_idx, keys in enumerate(key_sequences):
            logger.info("Key sequence %d: %s for query '%s'", seq_idx + 1, keys, query)
            
            if not self._apply_search_query(window_state, query):
                logger.warning("Failed to apply search query '%s'", query)
                continue

            self._fast_sleep(200)
            keys_sent = True
            for key in keys:
                if not self._automation.press_key(key):
                    logger.warning("Failed to press key '%s'", key)
                    keys_sent = False
                    break
                self._fast_sleep(120)
            if not keys_sent:
                continue

            refreshed, chat_name, verified = self._wait_for_chat_name(window_state, expected_names=expected_names)
            if verified and refreshed is not None:
                logger.info("Chat opened: '%s'", chat_name)
                return refreshed, chat_name
            
            logger.debug("Sequence %d failed, trying next", seq_idx + 1)
            self._automation.press_key("escape")
            self._fast_sleep(100)

        logger.warning("All sequences exhausted for query: %s", query)
        return None

    def _focus_message_input(self, window_state: WhatsAppWindowState) -> bool:
        bounds = self._message_input_bounds(window_state)
        if self._automation.click_center(bounds):
            self._fast_sleep(100)
            if self._verify_message_input_focused(window_state):
                return True
        if self._automation.click_center(bounds):
            self._fast_sleep(150)
            return True
        return False

    def _verify_message_input_focused(self, window_state: WhatsAppWindowState) -> bool:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return False
        try:
            for control in window.descendants():
                try:
                    element = control.element_info
                    name = getattr(element, 'name', '') or ''
                    control_type = getattr(element, 'control_type', '') or ''
                    if control_type in ('Edit', 'TextBox'):
                        if 'type a message' in name.lower() or 'message' in name.lower():
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _wait_for_chat_name(
        self,
        window_state: WhatsAppWindowState,
        *,
        expected_names: list[str],
    ) -> tuple[WhatsAppWindowState | None, str, bool]:
        logger.info("Waiting for chat: expected=%s", expected_names)
        
        deadline = time.time() + 1.5
        last_state: WhatsAppWindowState | None = None
        last_name = ""
        poll_count = 0
        
        window_before = window_state.rect
        
        while time.time() <= deadline:
            poll_count += 1
            refreshed = self._refresh_window_state(window_state.hwnd) or window_state
            name, source = self._read_current_chat_name_raw(refreshed)
            last_state = refreshed
            last_name = name
            
            if poll_count <= 3:
                logger.info("Poll #%d: chat_name='%s' source=%s", poll_count, name, source)
            
            if name:
                for expected in expected_names:
                    if expected and self._contact_matches_expected(name, expected):
                        logger.info("Chat verified: '%s' matched '%s'", name, expected)
                        return refreshed, name, True
            
            if poll_count == 1:
                if refreshed.rect != window_before:
                    logger.debug("Window changed - chat may have opened")
            
            self._fast_sleep(100)
        
        if last_name:
            logger.warning("Found chat but no match: got='%s', expected=%s", last_name, expected_names)
            return last_state, last_name, True
        
        logger.error("No chat name after %d polls", poll_count)
        return last_state, last_name, False

    def _click_header_button(self, window_state: WhatsAppWindowState, *, button_type: str) -> bool:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return False

        bounds = self._header_bounds(window_state)
        hints = _VIDEO_CALL_HINTS if button_type == "video_call" else _VOICE_CALL_HINTS
        candidates: list[_UIARecord] = []
        for record in self._collect_uia_records(window):
            if not self._rect_overlaps(record.rect, bounds):
                continue
            text = self._clean_visible_text(record.effective_text).lower()
            if not text:
                continue
            if button_type == "voice_call" and "video call" in text:
                continue
            if any(hint in text for hint in hints):
                candidates.append(record)

        candidates.sort(key=lambda item: (-item.rect[0], item.rect[1]))
        for record in candidates:
            if self._click_control(record.control, record.rect):
                return True
        return False

    def _click_control(self, control: Any, rect: tuple[int, int, int, int] | None = None) -> bool:
        if control is not None:
            try:
                wrapper = control.wrapper_object()
                wrapper.click_input()
                return True
            except Exception:
                pass
        if rect:
            return self._automation.click_center(rect)
        return False

    def _click_match_control(self, match: WhatsAppContactMatch) -> bool:
        return self._click_control(match.control, match.rect)

    def _refresh_window_state(self, hwnd: int) -> WhatsAppWindowState | None:
        state_obj = self._live_window_state()
        if state_obj is None:
            return None
        if hwnd and state_obj.hwnd and hwnd != state_obj.hwnd:
            return state_obj
        return state_obj

    def _sidebar_right(self, window_state: WhatsAppWindowState) -> int:
        return window_state.rect[0] + int(window_state.width * 0.39)

    def _search_bounds(self, window_state: WhatsAppWindowState) -> tuple[int, int, int, int]:
        top = window_state.rect[1] + max(48, int(window_state.height * 0.07))
        return (window_state.rect[0] + 12, top, self._sidebar_right(window_state) - 12, top + 96)

    def _sidebar_list_bounds(self, window_state: WhatsAppWindowState) -> tuple[int, int, int, int]:
        search_bounds = self._search_bounds(window_state)
        return (
            window_state.rect[0] + 6,
            search_bounds[3],
            self._sidebar_right(window_state),
            window_state.rect[3] - 12,
        )

    def _pick_display_name(self, texts: list[str], query: str) -> str:
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

    def _has_pending_contact_choices(self) -> bool:
        pending = getattr(state, "pending_contact_choices", []) or []
        return bool(pending)

    def _strip_explicit_whatsapp_tokens(self, command: str) -> str:
        cleaned = str(command or "")
        for token in ("on whatsapp", "in whatsapp", "through whatsapp", "via whatsapp", "whatsapp"):
            cleaned = re.sub(rf"\b{re.escape(token)}\b", " ", cleaned, flags=re.IGNORECASE)
        return " ".join(cleaned.split())

    def _parse_contact_and_message(self, command: str) -> tuple[str, str]:
        cleaned = self._strip_explicit_whatsapp_tokens(command).strip()
        if not cleaned:
            return "", ""

        cleaned_lower = cleaned.lower()
        tell_him_match = re.match(r"^tell\s+(him|her|them)\s+(.+)$", cleaned_lower)
        if tell_him_match:
            return "", tell_him_match.group(2).strip()

        for prefix in (
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
        ):
            if cleaned_lower.startswith(prefix):
                cleaned = cleaned[len(prefix):]
                cleaned_lower = cleaned.lower()
                break

        quoted = re.match(r'^(?P<contact>.+?)\s+"(?P<message>.+)"$', cleaned.strip())
        if quoted:
            return quoted.group("contact").strip(), quoted.group("message").strip()

        for separator in (" saying ", " that ", " with message ", " saying: ", " that: ", " to say "):
            if separator in cleaned_lower:
                idx = cleaned_lower.find(separator)
                contact = cleaned[:idx].strip()
                message = cleaned[idx + len(separator):].strip()
                if contact and message:
                    return contact, message

        say_match = re.match(r"^say\s+(.+?)\s+to\s+(.+)$", cleaned_lower)
        if say_match:
            return say_match.group(2).strip(), say_match.group(1).strip()

        hi_match = re.match(r"^(hi|hello|hey)\s+to\s+(.+)$", cleaned_lower)
        if hi_match:
            return hi_match.group(2).strip(), hi_match.group(1).strip()

        resolved_contact, resolved_message = self._resolve_message_command_payload(cleaned)
        if resolved_contact and resolved_message:
            return resolved_contact, resolved_message

        parts = cleaned.split(maxsplit=1)
        if len(parts) == 1:
            return parts[0].strip(), ""
        if len(parts) > 1:
            return parts[0].strip(), parts[1].strip()
        return "", ""

    def _resolve_message_command_payload(self, payload: str) -> tuple[str, str]:
        cleaned = str(payload or "").strip()
        if not cleaned:
            return "", ""

        direct = self._split_contact_and_message(cleaned)
        if direct[0] and direct[1]:
            return direct

        tokens = cleaned.split()
        if len(tokens) <= 1:
            return cleaned, "" if cleaned else ("", "")

        max_prefix = min(len(tokens) - 1, 4)
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
            exact = sum(
                1
                for item in matches
                if self._normalize_label(item.display_name) == self._normalize_label(candidate_contact)
            )
            score = 3 if exact == 1 else 1 if len(matches) == 1 else 0
            if score > best_score:
                best_contact = candidate_contact
                best_message = candidate_message
                best_score = score
            if score >= 3:
                break

        if best_contact and best_message:
            return best_contact, best_message

        first_token = tokens[0]
        matches = self.list_matching_contacts(first_token)
        if matches:
            return first_token, " ".join(tokens[1:]).strip()

        return cleaned, ""

    def _split_contact_and_message(self, payload: str) -> tuple[str, str]:
        quoted = re.match(r'^(?P<contact>.+?)\s+"(?P<message>.+)"$', payload.strip())
        if quoted:
            return quoted.group("contact").strip(), quoted.group("message").strip()
        for separator in (" saying ", " tell ", " tell: ", " say ", " say: ", " message ", " that ", " : ", ": "):
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
        if normalized_text == normalized_query:
            return True
        if normalized_query in normalized_text or normalized_text in normalized_query:
            return True
        query_tokens = [item for item in normalized_query.split() if item]
        return bool(query_tokens) and all(token in normalized_text for token in query_tokens)

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
            capitalized = " ".join(part.capitalize() for part in parts)
            if capitalized != query:
                variants.append(capitalized)

        seen: set[str] = set()
        result: list[str] = []
        for item in variants:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    @staticmethod
    def _extract_ordinal(text: str) -> int:
        for token in str(text or "").split():
            if token in _ORDINALS:
                return _ORDINALS[token]
        return 0

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

    def _fast_sleep(self, duration_ms: int) -> None:
        if hasattr(self._automation, "fast_sleep"):
            self._automation.fast_sleep(duration_ms)
            return
        time.sleep(max(0.0, duration_ms / 1000.0))

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

    def _do_send_message(self, contact: str, message: str) -> bool:
        """Send a WhatsApp message and verify the delivery in the live UI."""
        try:
            logger.info(f"Sending message to {contact}: {message}")

            wa = self._find_window()
            if not wa:
                logger.error("No WhatsApp window found")
                return False

            try:
                win32gui.SetForegroundWindow(wa["hwnd"])
                time.sleep(0.5)
            except Exception as e:
                logger.debug(f"Could not bring to front: {e}")

            time.sleep(0.3)

            self._automation.hotkey(["ctrl", "k"])
            time.sleep(0.6)

            logger.info(f"Typing contact: {contact}")
            typed = self._automation.type_text(contact)
            if not typed:
                logger.error("Failed to type contact name")
                self._automation.press_key("escape")
                return False

            time.sleep(0.8)

            logger.info("Selecting contact with down arrow")
            self._automation.press_key("down")
            time.sleep(0.3)

            logger.info("Opening chat with Enter")
            enter_pressed = self._automation.press_key("enter")
            if not enter_pressed:
                logger.error("Failed to press enter to open chat")
                self._automation.press_key("escape")
                return False

            chat_state = self._wait_for_expected_chat(contact, timeout=3.5)
            if chat_state is None:
                logger.error("Could not verify that chat '%s' opened", contact)
                return False

            logger.info("Verified open chat for %s", contact)
            before_messages = self._collect_recent_message_texts(chat_state, limit=12)

            if message:
                logger.info(f"Typing message: {message}")
                typed_msg = self._automation.type_text(message)
                if not typed_msg:
                    logger.error("Failed to type message")
                    return False

                time.sleep(0.5)

                logger.info("Sending message with Enter")
                sent = self._automation.press_key("enter")
                if not sent:
                    logger.error("Failed to press enter to send")
                    return False

                verified, verification_reason = self._wait_for_message_delivery(
                    contact,
                    message,
                    before_messages,
                    timeout=3.0,
                )
                if verified:
                    logger.info("Verified WhatsApp message delivery for %s", contact)
                    return True

                logger.warning("WhatsApp send could not be verified: %s", verification_reason)
                return False

            return True

        except Exception as e:
            logger.debug(f"Send failed: {e}")
            return False

    def _verify_message_appeared(self, expected_message: str) -> bool:
        """Backward-compatible verification helper."""
        try:
            normalized = self._normalize_label(expected_message)
            state_obj = self._live_window_state()
            if state_obj is None:
                return False
            recent = self._collect_recent_message_texts(state_obj, limit=12)
            return any(self._normalize_label(item) == normalized for item in recent)
        except Exception as e:
            logger.debug(f"Verification error: {e}")
            return False

    def _live_window_state(self) -> WhatsAppWindowState | None:
        wa = self._find_window()
        if wa is None:
            return None
        return WhatsAppWindowState(
            hwnd=int(wa["hwnd"]),
            title=str(wa.get("title") or ""),
            process_name="whatsapp.exe",
            rect=tuple(wa.get("rect") or (0, 0, 0, 0)),
            source="desktop",
            is_foreground=True,
            is_minimized=False,
        )

    def _open_uia_window(self, hwnd: int):
        if not hwnd:
            logger.debug("No hwnd provided to _open_uia_window")
            return None
        if not _PYWINAUTO_OK or Desktop is None:
            logger.debug("pywinauto not available")
            return None
        for attempt in range(3):
            try:
                desktop = self._desktop_factory()
                if desktop is None:
                    self._fast_sleep(100)
                    continue
                window = desktop.window(handle=hwnd)
                if window is not None:
                    self._fast_sleep(150)
                    return window
                self._fast_sleep(100)
            except Exception as exc:
                logger.debug("UIA window attempt %d failed: %s", attempt + 1, exc)
                self._fast_sleep(150)
        logger.warning("All UIA window attempts failed for hwnd=%d", hwnd)
        return None

    def _collect_uia_records(self, window: Any) -> list[_UIARecord]:
        records: list[_UIARecord] = []
        try:
            descendants = window.descendants()
        except Exception as exc:
            logger.debug("WhatsApp UIA descendants failed: %s", exc)
            return records

        for control in descendants:
            try:
                wrapper = control.wrapper_object()
                if hasattr(wrapper, "is_visible") and not wrapper.is_visible():
                    continue
                element = control.element_info
                rect = self._rect_from_object(getattr(element, "rectangle", None))
                if not rect:
                    continue
                name = self._clean_visible_text(getattr(element, "name", "") or "")
                control_type = str(getattr(element, "control_type", "") or "").lower()
                value = self._extract_control_value(control)
                if not name and not value:
                    continue
                records.append(
                    _UIARecord(
                        control=control,
                        name=name,
                        control_type=control_type,
                        rect=rect,
                        value=value,
                    )
                )
            except Exception:
                continue
        return records

    def _read_current_chat_name_raw(self, window_state: WhatsAppWindowState) -> tuple[str, str]:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            logger.debug("UIA window unavailable in _read_current_chat_name_raw")
            return "", "uia_unavailable"

        bounds = self._header_bounds(window_state)
        candidates: list[tuple[int, int, int, str]] = []
        
        for record in self._collect_uia_records(window):
            if not self._rect_overlaps(record.rect, bounds):
                continue
            text = self._clean_visible_text(record.effective_text)
            if not text:
                continue
            if self._normalize_label(text) in _HEADER_NOISE:
                continue
            score = 0
            if record.control_type in {"text", "hyperlink", "heading"}:
                score += 3
            if len(text) >= 3:
                score += 1
            candidates.append((-score, record.rect[1], record.rect[0], text))

        if not candidates:
            return "", "header_empty"
        
        result = sorted(candidates)[0][3]
        logger.debug("Read chat name: '%s'", result)
        return result, "uia_header"

    def _collect_recent_message_texts(self, window_state: WhatsAppWindowState, *, limit: int) -> list[str]:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return []

        bounds = self._conversation_bounds(window_state)
        rows: list[tuple[int, int, str]] = []
        for record in self._collect_uia_records(window):
            if not self._rect_overlaps(record.rect, bounds):
                continue
            if record.control_type not in {"text", "hyperlink", "document", "listitem", "pane", "group"}:
                continue
            text = self._clean_visible_text(record.effective_text)
            if not text:
                continue
            normalized = self._normalize_label(text)
            if not normalized or normalized in _HEADER_NOISE or normalized in _MESSAGE_NOISE:
                continue
            rows.append((record.rect[1], record.rect[0], text))

        deduped: list[str] = []
        seen: set[str] = set()
        for _, _, item in sorted(rows):
            normalized = self._normalize_label(item)
            if normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(item)
        return deduped[-max(1, limit):]

    def _read_message_input_text(self, window_state: WhatsAppWindowState) -> str:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return ""

        bounds = self._message_input_bounds(window_state)
        candidates: list[tuple[int, int, int, str]] = []
        for record in self._collect_uia_records(window):
            if not self._rect_overlaps(record.rect, bounds):
                continue
            if record.control_type not in {"edit", "document", "pane", "group", "text"}:
                continue
            text = self._clean_visible_text(record.value or record.name)
            if not text:
                continue
            score = 0
            if record.control_type == "edit":
                score += 3
            if self._normalize_label(text) in _MESSAGE_NOISE:
                score -= 2
            candidates.append((-score, -record.rect[1], -record.rect[0], text))

        if not candidates:
            return ""
        return sorted(candidates)[0][3]

    def _has_new_message_evidence(self, before_messages: list[str], after_messages: list[str], message: str) -> bool:
        normalized_message = self._normalize_label(message)
        before_count = sum(1 for item in before_messages if self._normalize_label(item) == normalized_message)
        after_count = sum(1 for item in after_messages if self._normalize_label(item) == normalized_message)
        if after_count > before_count:
            return True

        if after_messages:
            tail = [self._normalize_label(item) for item in after_messages[-3:]]
            if normalized_message in tail and normalized_message not in {
                self._normalize_label(item) for item in before_messages[-3:]
            }:
                return True
        return False

    def _contact_matches_expected(self, actual: str, expected: str) -> bool:
        normalized_actual = self._normalize_label(actual)
        normalized_expected = self._normalize_label(expected)
        if not normalized_actual or not normalized_expected:
            logger.debug("Empty normalized for match check: actual='%s', expected='%s'", actual, expected)
            return False
        if normalized_actual == normalized_expected:
            return True
        if normalized_actual.startswith(normalized_expected):
            return True
        if normalized_actual.endswith(normalized_expected):
            return True
        if normalized_expected in normalized_actual.split():
            return True
        if normalized_expected in normalized_actual:
            return True
        actual_tokens = set(normalized_actual.split())
        expected_tokens = set(normalized_expected.split())
        if expected_tokens and actual_tokens:
            common = actual_tokens & expected_tokens
            if common == expected_tokens or common == actual_tokens:
                return True
        logger.debug("No match: actual='%s' vs expected='%s'", normalized_actual, normalized_expected)
        return False

    def _header_bounds(self, window_state: WhatsAppWindowState) -> tuple[int, int, int, int]:
        left = window_state.rect[0] + int(window_state.width * 0.42)
        top = window_state.rect[1] + 8
        right = window_state.rect[2] - 8
        bottom = top + max(88, int(window_state.height * 0.12))
        return (left, top, right, bottom)

    def _message_input_bounds(self, window_state: WhatsAppWindowState) -> tuple[int, int, int, int]:
        window = self._open_uia_window(window_state.hwnd)
        if window is not None:
            for control in window.descendants():
                try:
                    element = control.element_info
                    name = getattr(element, 'name', '') or ''
                    control_type = getattr(element, 'control_type', '') or ''
                    if control_type in ('Edit', 'TextBox'):
                        if 'type a message' in name.lower() or 'message' in name.lower():
                            rect = getattr(element, 'rectangle', None)
                            if rect:
                                return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
                except Exception:
                    continue
        left = window_state.rect[0] + int(window_state.width * 0.40)
        bottom = window_state.rect[3] - 12
        top = bottom - max(120, int(window_state.height * 0.12))
        return (left, top, window_state.rect[2] - 12, bottom)

    def _conversation_bounds(self, window_state: WhatsAppWindowState) -> tuple[int, int, int, int]:
        header = self._header_bounds(window_state)
        composer = self._message_input_bounds(window_state)
        return (header[0], header[3], header[2], composer[1])

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
    def _rect_overlaps(rect_a: tuple[int, int, int, int], rect_b: tuple[int, int, int, int]) -> bool:
        return not (
            rect_a[2] <= rect_b[0]
            or rect_a[0] >= rect_b[2]
            or rect_a[3] <= rect_b[1]
            or rect_a[1] >= rect_b[3]
        )

    @staticmethod
    def _extract_control_value(control: Any) -> str:
        try:
            value = control.get_value()
            if value:
                return str(value).strip()
        except Exception:
            pass
        try:
            iface = control.iface_value
            value = getattr(iface, "CurrentValue", "")
            if value:
                return str(value).strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _clean_visible_text(value: str) -> str:
        return " ".join(str(value or "").split()).strip()

    @staticmethod
    def _normalize_label(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower())
        return " ".join(normalized.split())

    def _wait_for_expected_chat(self, contact: str, *, timeout: float) -> WhatsAppWindowState | None:
        deadline = time.time() + max(0.5, timeout)
        while time.time() < deadline:
            state_obj = self._live_window_state()
            if state_obj is not None:
                chat_name, source = self._read_current_chat_name_raw(state_obj)
                if self._contact_matches_expected(chat_name, contact):
                    logger.info("Verified current chat '%s' via %s", chat_name, source)
                    return state_obj
            time.sleep(0.35)
        return None

    def _wait_for_message_delivery(
        self,
        contact: str,
        message: str,
        before_messages: list[str],
        *,
        timeout: float,
    ) -> tuple[bool, str]:
        deadline = time.time() + max(0.5, timeout)
        last_reason = "delivery_not_observed"
        while time.time() < deadline:
            verified, reason = self._verify_message_delivery_once(contact, message, before_messages)
            if verified:
                return True, ""
            last_reason = reason
            time.sleep(0.4)
        return False, last_reason

    def _verify_message_delivery_once(
        self,
        contact: str,
        message: str,
        before_messages: list[str],
    ) -> tuple[bool, str]:
        state_obj = self._live_window_state()
        if state_obj is None:
            return False, "window_missing"

        chat_name, source = self._read_current_chat_name_raw(state_obj)
        if not self._contact_matches_expected(chat_name, contact):
            return False, f"chat_mismatch:{chat_name or source or 'unknown'}"

        composer_text = self._read_message_input_text(state_obj)
        if self._normalize_label(composer_text) == self._normalize_label(message):
            return False, "message_still_in_composer"

        after_messages = self._collect_recent_message_texts(state_obj, limit=12)
        if self._has_new_message_evidence(before_messages, after_messages, message):
            return True, ""

        return False, "message_not_visible"

    def _make_voice_call(self, contact: str) -> SkillExecutionResult:
        if not contact:
            return self._failure("Who should I call?", "empty_contact")

        wa = self._ensure_running()
        if wa is None:
            return self._failure("WhatsApp not available", "whatsapp_unavailable")

        logger.info(f"Starting voice call to {contact}")

        try:
            time.sleep(0.3)
            self._automation.hotkey(["ctrl", "k"])
            time.sleep(0.4)
            self._automation.type_text(contact)
            time.sleep(0.6)
            self._automation.press_key("down")
            time.sleep(0.2)
            self._automation.press_key("enter")
            time.sleep(1.0)
            self._automation.press_key("video")
            time.sleep(0.3)

            return self._success(
                intent="whatsapp_voice_call",
                response=f"Calling {contact} on WhatsApp...",
                data={"contact": contact},
            )
        except Exception as e:
            return self._failure(f"Failed to call {contact}: {e}", "call_failed")

    def _make_video_call(self, contact: str) -> SkillExecutionResult:
        return self._make_voice_call(contact)

    def _end_current_call(self) -> SkillExecutionResult:
        try:
            time.sleep(0.1)
            self._automation.press_key("end")
            time.sleep(0.2)
            return self._success(
                intent="whatsapp_end_call",
                response="Call ended",
                data={},
            )
        except Exception as e:
            return self._failure(f"Failed to end call: {e}", "end_call_failed")

    def _toggle_speaker(self) -> SkillExecutionResult:
        try:
            time.sleep(0.1)
            self._automation.press_key("d")
            time.sleep(0.2)
            return self._success(
                intent="whatsapp_speaker",
                response="Speaker toggled",
                data={},
            )
        except Exception as e:
            return self._failure(f"Failed to toggle speaker: {e}", "speaker_failed")

    def _add_person_to_call(self, contact: str) -> SkillExecutionResult:
        if not contact:
            return self._failure("Who should I add?", "empty_contact")

        try:
            time.sleep(0.2)
            self._automation.press_key("add")
            time.sleep(0.5)
            self._automation.type_text(contact)
            time.sleep(0.3)
            self._automation.press_key("enter")
            time.sleep(0.2)

            return self._success(
                intent="whatsapp_add_participant",
                response=f"Adding {contact} to the call...",
                data={"contact": contact},
            )
        except Exception as e:
            return self._failure(f"Failed to add {contact}: {e}", "add_failed")

    def _mute_call(self) -> SkillExecutionResult:
        try:
            time.sleep(0.1)
            self._automation.press_key("m")
            time.sleep(0.2)
            return self._success(
                intent="whatsapp_mute",
                response="Call muted",
                data={},
            )
        except Exception:
            return self._failure("Failed to mute", "mute_failed")

    def _unmute_call(self) -> SkillExecutionResult:
        try:
            time.sleep(0.1)
            self._automation.press_key("m")
            time.sleep(0.2)
            return self._success(
                intent="whatsapp_unmute",
                response="Call unmuted",
                data={},
            )
        except Exception:
            return self._failure("Failed to unmute", "unmute_failed")

    def _success(self, intent: str, response: str, data: dict = None) -> SkillExecutionResult:
        return SkillExecutionResult(
            success=True,
            intent=intent,
            response=response,
            skill_name=self.name(),
            data=data or {},
        )

    def _failure(self, response: str, error: str) -> SkillExecutionResult:
        logger.error(f"WhatsApp error: {response} ({error})")
        return SkillExecutionResult(
            success=False,
            intent="whatsapp_action",
            response=response,
            skill_name=self.name(),
            error=error,
        )
