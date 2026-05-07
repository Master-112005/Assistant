"""
Hybrid desktop-intent detection for Nova Assistant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
import re
from typing import Any, Dict, List, Mapping, Optional

from core.app_commands import parse_app_command
from core.browser_commands import parse_browser_command
from core.logger import get_logger
from core.normalizer import normalize_command, normalize_command_result
from core.nlu import (
    detect_system_intent,
    extract_entities,
    looks_like_open_file_request,
    looks_like_search_file_request,
    resolve_context_entities,
)
from core.app_launcher import is_known_website, website_url_for
from core.apps_registry import canonicalize_app_name

logger = get_logger(__name__)


class IntentType(Enum):
    OPEN_APP = "open_app"
    CLOSE_APP = "close_app"
    MINIMIZE_APP = "minimize_app"
    MAXIMIZE_APP = "maximize_app"
    FOCUS_APP = "focus_app"
    RESTORE_APP = "restore_app"
    TOGGLE_APP = "toggle_app"

    SEARCH_WEB = "search_web"
    OPEN_WEBSITE = "open_website"
    BROWSER_ACTION = "browser_action"
    BROWSER_TAB_NEW = "browser_tab_new"
    BROWSER_TAB_CLOSE = "browser_tab_close"
    BROWSER_TAB_NEXT = "browser_tab_next"
    BROWSER_TAB_PREVIOUS = "browser_tab_previous"
    BROWSER_TAB_SWITCH = "browser_tab_switch"

    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    MUTE = "mute"
    UNMUTE = "unmute"
    SET_VOLUME = "set_volume"
    BRIGHTNESS_UP = "brightness_up"
    BRIGHTNESS_DOWN = "brightness_down"
    SET_BRIGHTNESS = "set_brightness"
    LOCK_PC = "lock_pc"
    SHUTDOWN_PC = "shutdown_pc"
    RESTART_PC = "restart_pc"
    SLEEP_PC = "sleep_pc"

    CREATE_FILE = "create_file"
    OPEN_FILE = "open_file"
    DELETE_FILE = "delete_file"
    MOVE_FILE = "move_file"
    RENAME_FILE = "rename_file"
    SEARCH_FILE = "search_file"

    SEND_MESSAGE = "send_message"
    CALL_CONTACT = "call_contact"

    PLAY_MEDIA = "play_media"
    PAUSE_MEDIA = "pause_media"
    NEXT_TRACK = "next_track"
    PREVIOUS_TRACK = "previous_track"

    REMINDER_CREATE = "reminder_create"
    CLIPBOARD_QUERY = "clipboard_query"
    REPEAT_LAST_COMMAND = "repeat_last_command"

    QUESTION = "question"
    GREETING = "greeting"
    HELP = "help"
    MULTI_ACTION = "multi_action"
    SYSTEM_CONTROL = "system_control"
    FILE_ACTION = "file_action"
    SEARCH = "search"
    UNKNOWN = "unknown"


@dataclass
class IntentResult:
    intent: IntentType
    confidence: float
    cleaned_text: str
    original_text: str
    entities: Dict[str, Any] = field(default_factory=dict)
    matched_rules: List[str] = field(default_factory=list)
    candidate_scores: Dict[str, float] = field(default_factory=dict)
    decision_reason: str = ""
    requires_confirmation: bool = False
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "intent": self.intent.value,
            "confidence": self.confidence,
            "entities": self.entities,
            "cleaned_text": self.cleaned_text,
            "original_text": self.original_text,
            "candidate_scores": self.candidate_scores,
            "decision_reason": self.decision_reason,
            "requires_confirmation": self.requires_confirmation,
            "timestamp": self.timestamp,
        }


class IntentDetector:
    """Rules-first desktop intent detector with optional LLM fallback."""

    def __init__(self, llm_client=None) -> None:
        self._llm = llm_client

    def detect(
        self,
        original_text: str,
        cleaned_text: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> IntentResult:
        normalization = normalize_command_result(cleaned_text)
        normalized = normalization.normalized_text
        if not normalized:
            return self._unknown(original_text, normalized, "empty")

        if normalization.requires_confirmation and normalization.confirmation_prompt:
            return IntentResult(
                intent=IntentType.UNKNOWN,
                confidence=normalization.confidence,
                cleaned_text=normalized,
                original_text=original_text,
                entities={
                    "clarification_prompt": normalization.confirmation_prompt,
                    "suggested_app": normalization.matched_app,
                },
                candidate_scores={"app_correction": normalization.confidence},
                decision_reason="App correction requires confirmation",
                requires_confirmation=True,
            )

        context_data = self._normalize_context(context)

        multi_action = self._detect_multi_action(original_text, normalized)
        if multi_action is not None:
            return multi_action

        priority_result = self._resolve_priority_intent(original_text, normalized, context_data)
        if priority_result is not None:
            return priority_result

        matched = self._match_rules(normalized)
        if matched is not None:
            intent, confidence, rules = matched
            entities = extract_entities(intent.value, normalized)
            entities = resolve_context_entities(intent.value, entities, normalized, context=context_data)
            result = IntentResult(
                intent=intent,
                confidence=confidence,
                cleaned_text=normalized,
                original_text=original_text,
                entities=entities,
                matched_rules=rules,
                decision_reason=f"Matched rule set: {', '.join(rules)}",
            )
            logger.info(
                "Intent detected",
                request_id=context_data.get("request_id", ""),
                raw_command=original_text,
                normalized_command=normalized,
                intent=result.intent.value,
                confidence=result.confidence,
                entities=result.entities,
            )
            return result

        llm_result = self._llm_fallback(original_text, normalized)
        if llm_result is not None:
            return llm_result

        return self._unknown(original_text, normalized, "no_rule_match")

    def _match_rules(self, text: str) -> tuple[IntentType, float, list[str]] | None:
        lowered = text.lower()

        if lowered in {"do the same thing", "do that again", "same thing again", "repeat that", "repeat last command"}:
            return IntentType.REPEAT_LAST_COMMAND, 0.98, ["rule:repeat_last_command"]

        if lowered.startswith(("how are you", "howr you", "how do you do")):
            return IntentType.QUESTION, 0.95, ["rule:status_question"]

        browser_command = None if looks_like_open_file_request(lowered) else parse_browser_command(lowered)
        if browser_command is not None and browser_command.explicit_browser and self._should_prefer_browser_command(lowered, browser_command):
            browser_intent = {
                "new_tab": (IntentType.BROWSER_TAB_NEW, 0.99, ["rule:browser_new_tab"]),
                "close_tab": (IntentType.BROWSER_TAB_CLOSE, 0.99, ["rule:browser_close_tab"]),
                "next_tab": (IntentType.BROWSER_TAB_NEXT, 0.99, ["rule:browser_next_tab"]),
                "previous_tab": (IntentType.BROWSER_TAB_PREVIOUS, 0.99, ["rule:browser_previous_tab"]),
                "switch_tab": (IntentType.BROWSER_TAB_SWITCH, 0.99, ["rule:browser_switch_tab"]),
                "go_back": (IntentType.BROWSER_ACTION, 0.98, ["rule:browser_back"]),
                "go_forward": (IntentType.BROWSER_ACTION, 0.98, ["rule:browser_forward"]),
                "refresh": (IntentType.BROWSER_ACTION, 0.98, ["rule:browser_refresh"]),
                "home": (IntentType.BROWSER_ACTION, 0.97, ["rule:browser_home"]),
                "scroll_down": (IntentType.BROWSER_ACTION, 0.97, ["rule:browser_scroll_down"]),
                "scroll_up": (IntentType.BROWSER_ACTION, 0.97, ["rule:browser_scroll_up"]),
                "read_page_title": (IntentType.BROWSER_ACTION, 0.97, ["rule:browser_read_page_title"]),
                "read_page": (IntentType.BROWSER_ACTION, 0.97, ["rule:browser_read_page"]),
                "copy_page_url": (IntentType.BROWSER_ACTION, 0.97, ["rule:browser_copy_page_url"]),
                "open_url": (IntentType.OPEN_WEBSITE, 0.97, ["rule:open_website_browser"]),
                "search": (IntentType.SEARCH_WEB, 0.95, ["rule:search_web_browser"]),
                "launch_browser": (IntentType.OPEN_APP, 0.96, ["rule:open_browser"]),
                "close_browser": (IntentType.CLOSE_APP, 0.96, ["rule:close_browser"]),
            }.get(browser_command.action)
            if browser_intent is not None:
                return browser_intent

        app_command = parse_app_command(lowered)
        if app_command is not None:
            mapped_intent = {
                "open_app": IntentType.OPEN_APP,
                "close_app": IntentType.CLOSE_APP,
                "minimize_app": IntentType.MINIMIZE_APP,
                "maximize_app": IntentType.MAXIMIZE_APP,
                "focus_app": IntentType.FOCUS_APP,
                "restore_app": IntentType.RESTORE_APP,
                "toggle_app": IntentType.TOGGLE_APP,
            }.get(app_command.intent)
            if mapped_intent is not None:
                return mapped_intent, max(0.93, float(app_command.confidence)), [f"rule:{app_command.intent}"]

        if browser_command is not None and self._should_prefer_browser_command(lowered, browser_command):
            browser_intent = {
                "new_tab": (IntentType.BROWSER_TAB_NEW, 0.99, ["rule:browser_new_tab"]),
                "close_tab": (IntentType.BROWSER_TAB_CLOSE, 0.99, ["rule:browser_close_tab"]),
                "next_tab": (IntentType.BROWSER_TAB_NEXT, 0.99, ["rule:browser_next_tab"]),
                "previous_tab": (IntentType.BROWSER_TAB_PREVIOUS, 0.99, ["rule:browser_previous_tab"]),
                "switch_tab": (IntentType.BROWSER_TAB_SWITCH, 0.99, ["rule:browser_switch_tab"]),
                "go_back": (IntentType.BROWSER_ACTION, 0.98, ["rule:browser_back"]),
                "go_forward": (IntentType.BROWSER_ACTION, 0.98, ["rule:browser_forward"]),
                "refresh": (IntentType.BROWSER_ACTION, 0.98, ["rule:browser_refresh"]),
                "home": (IntentType.BROWSER_ACTION, 0.97, ["rule:browser_home"]),
                "scroll_down": (IntentType.BROWSER_ACTION, 0.97, ["rule:browser_scroll_down"]),
                "scroll_up": (IntentType.BROWSER_ACTION, 0.97, ["rule:browser_scroll_up"]),
                "read_page_title": (IntentType.BROWSER_ACTION, 0.97, ["rule:browser_read_page_title"]),
                "read_page": (IntentType.BROWSER_ACTION, 0.97, ["rule:browser_read_page"]),
                "copy_page_url": (IntentType.BROWSER_ACTION, 0.97, ["rule:browser_copy_page_url"]),
                "open_url": (IntentType.OPEN_WEBSITE, 0.97, ["rule:open_website_browser"]),
                "search": (IntentType.SEARCH_WEB, 0.95, ["rule:search_web_browser"]),
                "launch_browser": (IntentType.OPEN_APP, 0.96, ["rule:open_browser"]),
                "close_browser": (IntentType.CLOSE_APP, 0.96, ["rule:close_browser"]),
            }.get(browser_command.action)
            if browser_intent is not None:
                return browser_intent

        if lowered in {"close tab", "close current tab", "close this tab"}:
            return IntentType.BROWSER_TAB_CLOSE, 0.99, ["rule:browser_close_tab"]
        if lowered in {"next tab", "go to next tab", "switch to next tab"}:
            return IntentType.BROWSER_TAB_NEXT, 0.99, ["rule:browser_next_tab"]
        if lowered in {"previous tab", "prev tab", "go to previous tab", "switch to previous tab"}:
            return IntentType.BROWSER_TAB_PREVIOUS, 0.99, ["rule:browser_previous_tab"]

        system_intent = self._match_system_intent(lowered)
        if system_intent is not None:
            return system_intent

        if self._looks_like_reminder_command(lowered):
            return IntentType.REMINDER_CREATE, 0.97, ["rule:reminder_create"]

        if lowered in {"clipboard", "what is on clipboard", "what's on clipboard", "clipboard history", "show clipboard"}:
            return IntentType.CLIPBOARD_QUERY, 0.97, ["rule:clipboard_query"]

        # CRITICAL: Check "open X" commands BEFORE communication commands
        # This ensures "open whatsapp" routes to OPEN_APP not SEND_MESSAGE
        if lowered.startswith("open "):
            target = lowered[5:].strip()
            if target and is_known_website(target):
                return IntentType.OPEN_WEBSITE, 0.97, ["rule:open_website"]
            if self._looks_like_file_target(target):
                return IntentType.OPEN_FILE, 0.84, ["rule:open_file_target"]
            return IntentType.OPEN_APP, 0.93, ["rule:open_app"]

        # WhatsApp message commands - explicit routing for WhatsApp skill
        # Only match if NOT an "open" command (already handled above)
        if lowered.startswith(("message ", "msg ", "text ", "send ", "tell ", "say ")) or "whatsapp" in lowered:
            if "whatsapp" in lowered or any(name in lowered for name in ("hemant", "hemanth")):
                return IntentType.SEND_MESSAGE, 0.96, ["rule:send_message_whatsapp"]
            return IntentType.SEND_MESSAGE, 0.95, ["rule:send_message"]

        # WhatsApp call commands
        if lowered.startswith(("call ", "ring ", "dial ")) or ("whatsapp" in lowered and "call" in lowered):
            if "whatsapp" in lowered or "video" in lowered:
                return IntentType.CALL_CONTACT, 0.96, ["rule:call_contact_whatsapp"]
            return IntentType.CALL_CONTACT, 0.95, ["rule:call_contact"]

        if lowered.startswith("close "):
            return IntentType.CLOSE_APP, 0.96, ["rule:close_app"]

        if lowered.startswith("minimize "):
            return IntentType.MINIMIZE_APP, 0.96, ["rule:minimize_app"]

        if lowered.startswith("maximize "):
            return IntentType.MAXIMIZE_APP, 0.96, ["rule:maximize_app"]

        if lowered.startswith(("focus ", "switch to ", "activate ", "bring to front ")):
            return IntentType.FOCUS_APP, 0.94, ["rule:focus_app"]

        if lowered.startswith("restore ") and "clipboard" not in lowered:
            return IntentType.RESTORE_APP, 0.95, ["rule:restore_app"]

        if lowered.startswith("toggle "):
            return IntentType.TOGGLE_APP, 0.93, ["rule:toggle_app"]

        if lowered.startswith(("search ", "search for ", "google ", "look up ", "lookup ")):
            if looks_like_search_file_request(lowered):
                return IntentType.SEARCH_FILE, 0.92, ["rule:search_file"]
            return IntentType.SEARCH_WEB, 0.95, ["rule:search_web"]

        if lowered.startswith("find "):
            if looks_like_search_file_request(lowered):
                return IntentType.SEARCH_FILE, 0.93, ["rule:search_file_find"]
            return IntentType.SEARCH_WEB, 0.82, ["rule:search_web_find"]

        media_intent = self._match_media_intent(lowered)
        if media_intent is not None:
            return media_intent

        file_intent = self._match_file_intent(lowered)
        if file_intent is not None:
            return file_intent

        # Greeting detection - be permissive for casual speech
        # Matches: "hi", "hi ra", "hello", "hello there", "hey", "hey buddy", etc.
        if re.match(r"^(hi+|hello+|hey+|yo+)(\s+\w+)?$", lowered):
            return IntentType.GREETING, 0.95, ["rule:greeting"]
        if lowered in {"help", "what can you do", "show commands", "commands"}:
            return IntentType.HELP, 0.98, ["rule:help"]
        if re.match(r"^(?:what|who|where|why|when|how)\b", lowered):
            return IntentType.QUESTION, 0.88, ["rule:question"]

        return None

    @staticmethod
    def _looks_like_reminder_command(text: str) -> bool:
        normalized = " ".join(str(text or "").strip().lower().split())
        if not normalized:
            return False

        if normalized.startswith(("remind me", "set reminder", "set a reminder", "create reminder", "add reminder")):
            return True

        if normalized.startswith(
            (
                "in ",
                "tomorrow ",
                "next monday ",
                "next tuesday ",
                "next wednesday ",
                "next thursday ",
                "next friday ",
                "next saturday ",
                "next sunday ",
                "every ",
            )
        ):
            return "remind me" in normalized

        return bool(
            re.fullmatch(
                r"(?:(?:set|start)(?:\s+a)?\s+)?timer(?:\s+for)?\s+\d+\s*(?:seconds?|secs?|sec|minutes?|mins?|min|hours?|hrs?|hr)\b",
                normalized,
            )
        )

    def _match_system_intent(self, text: str) -> tuple[IntentType, float, list[str]] | None:
        intent_name, _entities = detect_system_intent(text)
        if intent_name:
            mapped = {
                "volume_up": IntentType.VOLUME_UP,
                "volume_down": IntentType.VOLUME_DOWN,
                "mute": IntentType.MUTE,
                "unmute": IntentType.UNMUTE,
                "set_volume": IntentType.SET_VOLUME,
                "brightness_up": IntentType.BRIGHTNESS_UP,
                "brightness_down": IntentType.BRIGHTNESS_DOWN,
                "set_brightness": IntentType.SET_BRIGHTNESS,
                "lock_pc": IntentType.LOCK_PC,
                "shutdown_pc": IntentType.SHUTDOWN_PC,
                "restart_pc": IntentType.RESTART_PC,
                "sleep_pc": IntentType.SLEEP_PC,
                "system_control": IntentType.SYSTEM_CONTROL,
            }.get(intent_name, IntentType.SYSTEM_CONTROL)
            return mapped, 0.98 if mapped != IntentType.SYSTEM_CONTROL else 0.90, [f"rule:{mapped.value}"]

        if text.startswith("sleep ") or text == "sleep pc" or text == "sleep computer":
            return IntentType.SLEEP_PC, 0.96, ["rule:sleep_pc"]
        return None

    def _match_file_intent(self, text: str) -> tuple[IntentType, float, list[str]] | None:
        if re.match(
            r"^(?:create|make)(?:\s+(?:a|an|new))?\s+(?:(?:text|blank)\s+)?(?:file|document)\b",
            text,
        ):
            return IntentType.CREATE_FILE, 0.97, ["rule:create_file"]
        if looks_like_open_file_request(text) or text.startswith(("open file ", "open folder ", "open directory ")):
            return IntentType.OPEN_FILE, 0.95, ["rule:open_file"]
        if text.startswith(("delete ", "remove ", "trash ")):
            return IntentType.DELETE_FILE, 0.95, ["rule:delete_file"]
        if text.startswith("move "):
            return IntentType.MOVE_FILE, 0.95, ["rule:move_file"]
        if text.startswith("rename "):
            return IntentType.RENAME_FILE, 0.95, ["rule:rename_file"]
        if looks_like_search_file_request(text):
            return IntentType.SEARCH_FILE, 0.90, ["rule:search_file_generic"]
        return None

    @staticmethod
    def _looks_like_file_target(target: str) -> bool:
        lowered = " ".join(str(target or "").strip().lower().split())
        if not lowered:
            return False
        if lowered in {"explorer", "file explorer", "windows explorer", "file manager"}:
            return False
        if re.search(r"\.[a-z0-9]{1,6}\b", lowered):
            return True
        file_tokens = {
            "archive",
            "cv",
            "desktop",
            "document",
            "documents",
            "download",
            "downloads",
            "excel",
            "file",
            "folder",
            "image",
            "invoice",
            "music",
            "note",
            "pdf",
            "photo",
            "picture",
            "presentation",
            "report",
            "resume",
            "sheet",
            "spreadsheet",
            "txt",
            "video",
            "workbook",
        }
        return bool(set(lowered.split()) & file_tokens)

    def _match_media_intent(self, text: str) -> tuple[IntentType, float, list[str]] | None:
        text_l = " ".join(text.split())
        if text_l in {"pause", "pause media", "pause music", "pause song", "pause track", "pause playback"}:
            return IntentType.PAUSE_MEDIA, 0.92, ["rule:pause_media"]
        if text_l in {"next", "next one", "next track", "next song", "skip", "skip song", "skip track", "skip one"}:
            return IntentType.NEXT_TRACK, 0.92, ["rule:next_track"]
        if text_l in {"previous", "previous one", "previous track", "previous song", "last track", "back track", "skip back"}:
            return IntentType.PREVIOUS_TRACK, 0.92, ["rule:previous_track"]
        if text_l in {"play", "play media", "play music", "play song", "play track", "resume", "resume media", "resume video", "resume playback", "continue playback"}:
            return IntentType.PLAY_MEDIA, 0.92, ["rule:play_media"]
        return None

    def _resolve_priority_intent(
        self,
        original_text: str,
        normalized_text: str,
        context: dict[str, Any],
    ) -> IntentResult | None:
        candidates = self._score_priority_candidates(normalized_text, context)
        if not candidates:
            return None

        candidate_map = {candidate["name"]: round(float(candidate["score"]), 3) for candidate in candidates}
        winner = max(candidates, key=lambda item: item["score"])
        if winner["score"] < 0.55:
            if not any(candidate["name"] == "clarify" for candidate in candidates):
                return None
            return self._clarify_priority_intent(original_text, normalized_text, candidate_map, context)

        intent = winner["intent"]
        entities = extract_entities(intent.value, normalized_text, context=context)
        entities = resolve_context_entities(intent.value, entities, normalized_text, context=context)
        matched_rules = list(winner["rules"])
        result = IntentResult(
            intent=intent,
            confidence=round(float(winner["score"]), 3),
            cleaned_text=normalized_text,
            original_text=original_text,
            entities=entities,
            matched_rules=matched_rules,
            candidate_scores=candidate_map,
            decision_reason=str(winner["reason"]),
        )
        logger.info(
            "Intent routed",
            request_id=context.get("request_id", ""),
            raw_command=original_text,
            normalized_command=normalized_text,
            context_app=context.get("context_app", "unknown"),
            candidates=candidate_map,
            chosen_intent=intent.value,
            decision_reason=winner["reason"],
        )
        return result

    def _score_priority_candidates(self, normalized_text: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        text = " ".join(str(normalized_text or "").strip().split())
        if not text:
            return []

        lower = text.lower()
        tokens = set(lower.split())
        media_context = self._media_context_score(context)
        file_context = self._file_context_score(context)
        playback_context = max(media_context, file_context)

        media_candidate = self._score_media_candidate(lower, tokens, playback_context)
        file_candidate = self._score_file_candidate(lower, tokens, file_context, media_context)
        clarification_candidate = self._score_clarification_candidate(lower, media_candidate, file_candidate, context)

        candidates = [candidate for candidate in (media_candidate, file_candidate, clarification_candidate) if candidate is not None]
        return candidates

    def _score_media_candidate(
        self,
        text: str,
        tokens: set[str],
        context_score: float,
    ) -> dict[str, Any] | None:
        media_words = {"video", "music", "song", "track", "playback", "player", "youtube", "spotify", "movie"}
        verbs = {"resume", "continue", "pause", "play", "next", "previous", "skip", "mute", "unmute", "stop"}
        if not (tokens & verbs):
            return None

        score = 0.0
        reason_bits: list[str] = []
        intent = IntentType.PLAY_MEDIA
        rules = ["stage:media_fast_path"]
        candidate_name = "media_play"
        if "resume" in tokens or "continue" in tokens:
            candidate_name = "media_resume"

        if any(
            phrase in text
            for phrase in (
                "continue playback",
                "continue video",
                "resume playback",
                "resume video",
                "resume the video",
                "resume song",
                "resume track",
                "pause video",
                "pause the video",
                "pause song",
                "pause track",
                "play video",
                "play music",
                "open file",
                "open folder",
                "open document",
                "next song",
                "next one",
                "previous track",
                "previous one",
                "stop music",
                "stop playback",
                "mute video",
                "unmute video",
            )
        ):
            score += 0.58
            reason_bits.append("phrase_match")
        if text in {"play", "resume", "continue", "pause", "next", "next one", "previous", "previous one", "skip", "skip one", "mute", "unmute"}:
            score += 0.34 if context_score > 0 else 0.12
            reason_bits.append("short_command")
        if tokens & media_words:
            score += 0.24
            reason_bits.append("media_terms")
        if "resume" in tokens or "continue" in tokens or "play" in tokens:
            score += 0.10
            reason_bits.append("playback_verb")
        if {"next", "previous", "skip"} & tokens:
            intent = IntentType.NEXT_TRACK if {"next", "skip"} & tokens else IntentType.PREVIOUS_TRACK
            candidate_name = "media_next" if intent == IntentType.NEXT_TRACK else "media_previous"
            score += 0.10
            reason_bits.append("transport_verb")
        if {"pause"} & tokens:
            intent = IntentType.PAUSE_MEDIA
            candidate_name = "media_pause"
            score += 0.10
            reason_bits.append("pause_verb")
        if {"mute", "unmute"} & tokens:
            intent = IntentType.MUTE if "mute" in tokens else IntentType.UNMUTE
            candidate_name = "media_mute" if intent == IntentType.MUTE else "media_unmute"
            score += 0.10
            reason_bits.append("mute_verb")
        if context_score > 0:
            score += min(0.22, 0.22 * context_score)
            reason_bits.append("active_media_context")
        if len(tokens) <= 2 and tokens & {"resume", "play", "pause", "next", "previous", "skip"}:
            score += 0.12
            reason_bits.append("short_imperative")

        return {
            "name": candidate_name,
            "intent": intent,
            "score": round(min(1.0, score), 3),
            "reason": ", ".join(reason_bits) or "media heuristic",
            "rules": rules,
        }

    def _score_file_candidate(
        self,
        text: str,
        tokens: set[str],
        file_context: float,
        media_context: float,
    ) -> dict[str, Any] | None:
        if text in {"open explorer", "open file explorer", "open windows explorer", "open file manager"}:
            return None
        file_terms = {"file", "folder", "document", "doc", "docx", "pdf", "cv", "resume", "excel", "xlsx", "txt", "image", "photo", "png", "jpg", "download", "desktop", "documents"}
        action_words = {"find", "search", "open", "show", "list"}
        playback_words = {"resume", "continue", "play", "pause", "next", "previous", "skip", "stop", "mute", "unmute"}

        strong_file_terms = tokens & file_terms
        if not strong_file_terms:
            return None

        score = 0.0
        reason_bits: list[str] = []
        intent = IntentType.SEARCH_FILE
        rules = ["stage:file_safety_gate"]

        if any(phrase in text for phrase in ("find my resume pdf", "search resume docx", "open resume document", "open excel file", "search video files")):
            score += 0.55
            reason_bits.append("phrase_match")
        if strong_file_terms & {"pdf", "doc", "docx", "excel", "xlsx", "txt", "image", "photo", "png", "jpg"}:
            score += 0.22
            reason_bits.append("file_type")
        if any(re.search(rf"\.{extension}\b", text) for extension in ("pdf", "doc", "docx", "xlsx", "xls", "txt", "csv", "json")):
            score += 0.26
            reason_bits.append("file_extension")
        if strong_file_terms & {"file", "folder", "document", "resume", "cv", "desktop", "documents"}:
            score += 0.16
            reason_bits.append("file_noun")
        if tokens & action_words:
            score += 0.22 if strong_file_terms else 0.12
            reason_bits.append("file_action")
        if strong_file_terms and tokens & {"search", "find"}:
            score += 0.18
            reason_bits.append("search_bias")
        if strong_file_terms and "open" in tokens:
            score += 0.12
            reason_bits.append("open_bias")
        if text.startswith(("open file ", "open folder ", "open directory ", "open document ")):
            score += 0.25
            reason_bits.append("explicit_file_open")
        if strong_file_terms & {"resume", "cv", "pdf", "doc", "docx", "xlsx", "txt"}:
            score += 0.12
            reason_bits.append("strong_file_reference")
        if media_context > 0 and not file_context:
            score -= 0.18
            reason_bits.append("media_context_penalty")
        if tokens & playback_words and not (strong_file_terms & {"file", "folder", "document", "doc", "docx", "pdf", "cv", "resume", "excel", "xlsx", "txt"}):
            return None

        intent = IntentType.OPEN_FILE if "open" in tokens and not {"find", "search", "show", "list"} & tokens else IntentType.SEARCH_FILE
        return {
            "name": "file_open" if intent == IntentType.OPEN_FILE else "file_search",
            "intent": intent,
            "score": round(max(0.0, min(1.0, score)), 3),
            "reason": ", ".join(reason_bits) or "file heuristic",
            "rules": rules,
        }

    def _score_clarification_candidate(
        self,
        text: str,
        media_candidate: dict[str, Any] | None,
        file_candidate: dict[str, Any] | None,
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        if media_candidate is None and file_candidate is None:
            return None

        ambiguous = {"resume", "play", "continue", "open", "search", "find"} & set(text.split())
        if not ambiguous:
            return None
        if file_candidate is not None and media_candidate is None:
            return None
        if (media_candidate and media_candidate["score"] >= 0.72) or (file_candidate and file_candidate["score"] >= 0.72):
            return None

        prompt = "Do you want me to continue media playback or open a file?"
        if file_candidate and not media_candidate:
            prompt = "Which file should I open?"
        elif media_candidate and not file_candidate:
            prompt = "Do you want me to continue media playback?"

        return {
            "name": "clarify",
            "intent": IntentType.UNKNOWN,
            "score": 0.5,
            "reason": "ambiguous_short_command",
            "rules": ["stage:clarification"],
            "prompt": prompt,
        }

    def _clarify_priority_intent(
        self,
        original_text: str,
        normalized_text: str,
        candidate_scores: dict[str, float],
        context: dict[str, Any],
    ) -> IntentResult:
        prompt = "Do you mean media playback or a file command?"
        if candidate_scores.get("file_search", 0.0) > candidate_scores.get("media_resume", 0.0):
            prompt = "Do you want me to open or search for a file?"
        elif candidate_scores.get("media_resume", 0.0) > candidate_scores.get("file_search", 0.0):
            prompt = "Do you want me to continue playback?"

        return IntentResult(
            intent=IntentType.UNKNOWN,
            confidence=0.5,
            cleaned_text=normalized_text,
            original_text=original_text,
            matched_rules=["stage:clarification"],
            candidate_scores=candidate_scores,
            decision_reason=prompt,
            requires_confirmation=True,
            entities={"clarification_prompt": prompt, "context_app": context.get("context_app", "unknown")},
        )

    def _normalize_context(self, context: Mapping[str, Any] | None) -> dict[str, Any]:
        payload = dict(context or {})
        current_app = self._normalize_app(payload.get("current_app") or payload.get("current_context") or payload.get("context_target_app"))
        title = self._safe_text(payload.get("current_window_title") or payload.get("window_title"))
        process_name = self._safe_text(payload.get("current_process_name") or payload.get("process_name"))
        context_app = current_app or self._infer_app_from_title(self._normalize_text(title)) or self._infer_app_from_process(process_name)
        payload["context_app"] = context_app or "unknown"
        payload["request_id"] = self._safe_text(payload.get("request_id") or payload.get("correlation_id"))
        return payload

    def _media_context_score(self, context: Mapping[str, Any]) -> float:
        app = self._normalize_app(context.get("context_app") or context.get("current_app") or context.get("current_context"))
        title = self._normalize_text(context.get("current_window_title") or "")
        process = self._normalize_app(context.get("current_process_name") or "")
        last_action = self._normalize_text(context.get("last_media_action") or context.get("last_successful_action") or "")
        if app in {"youtube", "spotify", "vlc", "wmp", "itunes", "musicbee", "foobar", "browser"}:
            return 1.0
        if "youtube" in title or "spotify" in title or "video" in title or "music" in title:
            return 0.9
        if process in {"spotify.exe", "vlc.exe", "wmplayer.exe", "itunes.exe"}:
            return 0.85
        if last_action in {"resume", "play", "pause", "next_video", "previous_video", "next_track", "previous_track"}:
            return 0.65
        if bool(context.get("youtube_active")) or bool(context.get("music_active")):
            return 0.7
        return 0.0

    def _file_context_score(self, context: Mapping[str, Any]) -> float:
        app = self._normalize_app(context.get("context_app") or context.get("current_app") or context.get("current_context"))
        title = self._normalize_text(context.get("current_window_title") or "")
        process = self._normalize_app(context.get("current_process_name") or "")
        if app == "explorer" or process == "explorer.exe":
            return 1.0
        if "explorer" in title or "file explorer" in title:
            return 0.9
        if app in {"word", "excel", "notepad", "acrobat", "obsidian"}:
            return 0.65
        return 0.0

    @staticmethod
    def _infer_app_from_title(title: str) -> str:
        lowered = str(title or "").strip().lower()
        for fragment, app in (
            ("youtube", "youtube"),
            ("spotify", "spotify"),
            ("whatsapp", "whatsapp"),
            ("explorer", "explorer"),
            ("file explorer", "explorer"),
            ("google chrome", "chrome"),
            ("microsoft edge", "edge"),
            ("chrome", "chrome"),
            ("edge", "edge"),
            ("firefox", "firefox"),
            ("brave", "brave"),
        ):
            if fragment in lowered:
                return app
        return ""

    @staticmethod
    def _infer_app_from_process(process_name: str) -> str:
        lowered = str(process_name or "").strip().lower()
        if lowered.endswith(".exe"):
            lowered = lowered[:-4]
        for app in ("youtube", "spotify", "explorer", "vlc", "wmp", "itunes", "musicbee", "foobar"):
            if app in lowered:
                return app
        return ""

    def _detect_multi_action(self, original_text: str, normalized_text: str) -> Optional[IntentResult]:
        connectors = re.findall(r"\b(and|then|after that|afterwards|followed by)\b", normalized_text)
        if not connectors:
            return None
        action_markers = (
            "open",
            "close",
            "minimize",
            "maximize",
            "focus",
            "restore",
            "search",
            "mute",
            "unmute",
            "set volume",
            "set brightness",
            "lock",
            "create",
            "delete",
            "move",
            "rename",
            "play",
            "pause",
            "next",
            "previous",
        )
        verb_matches = sum(1 for marker in action_markers if marker in normalized_text)
        if verb_matches < 2:
            return None
        segments = [
            segment.strip()
            for segment in re.split(r"\b(?:and|then|after that|afterwards|followed by)\b", normalized_text)
            if segment.strip()
        ]
        return IntentResult(
            intent=IntentType.MULTI_ACTION,
            confidence=0.72,
            cleaned_text=normalized_text,
            original_text=original_text,
            entities={"segments": segments, "connector_count": len(connectors)},
            matched_rules=["rule:multi_action"],
        )

    def _llm_fallback(self, original_text: str, normalized_text: str) -> IntentResult | None:
        if self._llm is None:
            return None
        if self._is_low_information_unknown(normalized_text):
            return None
        if not hasattr(self._llm, "is_available") or not self._llm.is_available():
            return None
        if not hasattr(self._llm, "extract_intent"):
            return None

        try:
            llm_result = self._llm.extract_intent(normalized_text)
        except Exception as exc:  # pragma: no cover - defensive integration
            logger.warning("LLM intent fallback failed: %s", exc)
            return None
        if llm_result is None:
            return None

        intent_name = str(getattr(llm_result, "intent", "") or "").strip().lower()
        enum_value = self._intent_from_name(intent_name)
        if enum_value is None:
            return None
        entities = extract_entities(enum_value.value, normalized_text)
        return IntentResult(
            intent=enum_value,
            confidence=max(0.0, min(1.0, float(getattr(llm_result, "confidence", 0.55) or 0.55))),
            cleaned_text=normalized_text,
            original_text=original_text,
            entities=entities,
            matched_rules=["llm:fallback"],
        )

    def _intent_from_name(self, name: str) -> IntentType | None:
        for intent in IntentType:
            if intent.value == name:
                return intent
        if name == "search":
            return IntentType.SEARCH_WEB
        return None

    @staticmethod
    def _should_prefer_browser_command(text: str, browser_command) -> bool:
        lowered = " ".join(str(text or "").strip().lower().split())
        if not lowered or browser_command is None:
            return False

        if browser_command.explicit_browser:
            return True

        if browser_command.action == "search" and lowered.startswith(("open ", "launch ", "start ")):
            return False

        return True

    @staticmethod
    def _is_low_information_unknown(text: str) -> bool:
        normalized = " ".join(str(text or "").strip().lower().split())
        if not normalized:
            return True
        tokens = normalized.split()
        if len(tokens) != 1:
            return False
        token = tokens[0]
        return bool(re.fullmatch(r"[a-z]+", token)) and len(token) <= 12

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @staticmethod
    def _normalize_app(value: Any) -> str:
        text = " ".join(str(value or "").strip().lower().split())
        return text if text not in {"", "unknown"} else text

    @staticmethod
    def _safe_text(value: Any) -> str:
        return str(value or "").strip()

    def _unknown(self, original_text: str, cleaned_text: str, reason: str) -> IntentResult:
        return IntentResult(
            intent=IntentType.UNKNOWN,
            confidence=0.0,
            cleaned_text=cleaned_text,
            original_text=original_text,
            matched_rules=[f"fallback:{reason}"],
        )
