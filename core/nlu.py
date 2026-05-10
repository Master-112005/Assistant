"""
Structured entity extraction and lightweight context resolution.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from core.app_launcher import (
    preferred_browser_id,
    website_url_for,
)
from core.app_commands import parse_app_command
from core.browser_commands import parse_browser_command
from core.apps_registry import canonicalize_app_name
from core.entities import extract_file_action, looks_like_file_reference
from core.normalizer import normalize_command
from core.query_parser import is_probable_file_search, parse_natural_query
from core.system_controls import parse_system_command
from core.nlp_cache import cached_call, _make_key, _global_parse_cache


APP_WINDOW_INTENTS = {
    "open_app",
    "close_app",
    "minimize_app",
    "maximize_app",
    "focus_app",
    "restore_app",
    "toggle_app",
}
BROWSER_INTENTS = {
    "browser_action",
    "browser_tab_new",
    "search_web",
    "open_website",
    "browser_tab_close",
    "browser_tab_next",
    "browser_tab_previous",
    "browser_tab_switch",
}
MEDIA_INTENTS = {"play_media", "pause_media", "next_track", "previous_track"}
_REFERENTIAL_TOKENS = {"it", "this", "that", "current", "current app", "this app", "this window"}
_BROWSER_APPS = {"chrome", "edge", "firefox", "brave", "opera", "vivaldi", "browser"}

SYSTEM_ACTION_TO_INTENT = {
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "mute": "mute",
    "unmute": "unmute",
    "set_volume": "set_volume",
    "brightness_up": "brightness_up",
    "brightness_down": "brightness_down",
    "set_brightness": "set_brightness",
    "lock_pc": "lock_pc",
    "shutdown": "shutdown_pc",
    "restart": "restart_pc",
    "sleep": "sleep_pc",
}


@dataclass(slots=True)
class ParsedCommand:
    raw_text: str
    normalized_text: str
    intent: str
    confidence: float
    entities: dict[str, Any] = field(default_factory=dict)
    matched_rules: list[str] = field(default_factory=list)


def parse_command(
    text: str,
    *,
    intent: str,
    confidence: float = 0.0,
    matched_rules: list[str] | None = None,
    context: Mapping[str, Any] | None = None,
) -> ParsedCommand:
    normalized = normalize_command(text)
    entities = extract_entities(intent, normalized, context=context)
    return ParsedCommand(
        raw_text=str(text or ""),
        normalized_text=normalized,
        intent=_normalize_intent(intent),
        confidence=float(confidence or 0.0),
        entities=entities,
        matched_rules=list(matched_rules or []),
    )


def extract_entities(intent: str, text: str, *, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    normalized_text = normalize_command(text)
    normalized_intent = _normalize_intent(intent)
    entities = _extract_entities(normalized_intent, normalized_text)
    return resolve_context_entities(normalized_intent, entities, normalized_text, context=context)


def resolve_context_entities(
    intent: str,
    entities: Mapping[str, Any] | None,
    normalized_text: str,
    *,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(entities or {})
    normalized_intent = _normalize_intent(intent)
    context = context or {}
    current_app = _normalize_app(
        context.get("current_app")
        or context.get("current_context")
        or context.get("context_target_app")
    )
    preferred_browser = _normalize_app(context.get("preferred_browser")) or preferred_browser_id()
    last_target_app = _normalize_app(context.get("last_target_app") or _target_from_last_action(context.get("last_successful_action")))
    last_contact = str(context.get("last_contact") or context.get("last_message_target") or "").strip()
    token_set = set(normalized_text.split())
    is_referential = bool(token_set & {"it", "this", "that"}) or normalized_text in {"open it", "close it", "minimize this", "maximize this"}

    if normalized_intent in APP_WINDOW_INTENTS:
        app = _normalize_app(payload.get("app"))
        if not app:
            if is_referential and current_app and current_app != "unknown":
                app = current_app
            elif last_target_app:
                app = last_target_app
        if app == "browser":
            app = preferred_browser
        if app:
            payload["app"] = app
            payload["app_name"] = app

    if normalized_intent in BROWSER_INTENTS:
        browser = _normalize_app(payload.get("browser"))
        if not browser:
            if current_app in _BROWSER_APPS:
                browser = preferred_browser if current_app == "browser" else current_app
            else:
                browser = preferred_browser
        payload["browser"] = browser

    if normalized_intent in MEDIA_INTENTS and not payload.get("app"):
        if current_app in {"spotify", "youtube"}:
            payload["app"] = current_app

    if normalized_intent in {"send_message", "call_contact"}:
        contact_value = str(payload.get("contact") or "").strip().lower()
        if contact_value in {"him", "her", "them"} and last_contact:
            payload["contact"] = last_contact
        elif not payload.get("contact") and any(token in token_set for token in {"him", "her", "them"}) and last_contact:
            payload["contact"] = last_contact

    return {key: value for key, value in payload.items() if value not in ("", None, [], {})}


def detect_system_intent(text: str) -> tuple[str, dict[str, Any]]:
    parsed = parse_system_command(text)
    if parsed is None:
        return "", {}
    intent = SYSTEM_ACTION_TO_INTENT.get(parsed.action, "system_control")
    return intent, parsed.to_entities()


def _extract_entities(intent: str, text: str) -> dict[str, Any]:
    if intent in APP_WINDOW_INTENTS:
        return _extract_app_entities(intent, text)
    if intent == "browser_action":
        return _extract_browser_action_entities(text)
    if intent == "browser_tab_new":
        return _extract_browser_tab_entities(text, include_index=False)
    if intent == "search_web":
        return _extract_search_entities(text)
    if intent == "open_website":
        return _extract_website_entities(text)
    if intent in {"browser_tab_close", "browser_tab_next", "browser_tab_previous"}:
        return _extract_browser_tab_entities(text, include_index=False)
    if intent == "browser_tab_switch":
        return _extract_browser_tab_entities(text, include_index=True)
    if intent == "clipboard_query":
        return {}
    if intent in {
        "volume_up",
        "volume_down",
        "mute",
        "unmute",
        "set_volume",
        "brightness_up",
        "brightness_down",
        "set_brightness",
        "lock_pc",
        "shutdown_pc",
        "restart_pc",
        "sleep_pc",
        "system_control",
    }:
        _intent, payload = detect_system_intent(text)
        return payload
    if intent in {"create_file", "open_file", "delete_file", "move_file", "rename_file"}:
        details = extract_file_action(text)
        return {
            "action": details.get("action"),
            "reference": details.get("filename"),
            "filename": details.get("filename"),
            "destination": details.get("destination"),
            "new_name": details.get("new_name"),
            "location": details.get("location"),
            "source_location": details.get("source_location"),
            "permanent": bool(details.get("permanent")),
            "content": details.get("content"),
        }
    if intent == "search_file":
        query = parse_natural_query(text)
        return {"query": query.phrase or query.raw_text, "search_query": query.to_dict()}
    if intent == "send_message":
        cleaned = text.lower()
        # Handle "send message to X" pattern specially
        if cleaned.startswith("send message to "):
            remaining = text[len("send message to "):]
            # Extract contact (first word) and message (rest)
            parts = remaining.strip().split(None, 1)
            contact = parts[0] if parts else ""
            message = parts[1] if len(parts) > 1 else ""
            return {"contact": contact, "message": message}
        # Standard processing
        cleaned = _strip_prefix(text, ("message ", "msg ", "text ", "send ", "tell "))
        contact, message = _extract_contact_and_message(cleaned)
        return {"contact": contact, "message": message}
    if intent == "call_contact":
        contact = _strip_prefix(text, ("call ", "ring ", "dial ")).strip()
        return {"contact": contact}
    if intent == "play_media":
        return {"query": _strip_prefix(text, ("play ", "resume ")).strip()}
    if intent in {"pause_media", "next_track", "previous_track", "reminder_create"}:
        if intent == "reminder_create":
            return {"reminder_text": _strip_prefix(text, ("remind me to ", "set reminder ", "create reminder ")).strip()}
        return {}
    if intent == "repeat_last_command":
        return {}
    return {}


def _extract_app_entities(intent: str, text: str) -> dict[str, Any]:
    parsed_app = parse_app_command(text)
    if parsed_app is not None and parsed_app.intent == intent:
        return {
            "app": parsed_app.app_name,
            "app_name": parsed_app.app_name,
            "requested_app": parsed_app.requested_app,
        }

    parsed = parse_browser_command(text)
    if parsed is not None and parsed.action in {"launch_browser", "close_browser"}:
        app_name = parsed.browser or preferred_browser_id()
        return {
            "app": app_name,
            "app_name": app_name,
            "requested_app": app_name,
        }

    prefixes = {
        "open_app": ("open ",),
        "close_app": ("close ",),
        "minimize_app": ("minimize ",),
        "maximize_app": ("maximize ",),
        "focus_app": ("focus ",),
        "restore_app": ("restore ",),
        "toggle_app": ("toggle ",),
    }
    target = _strip_prefix(text, prefixes.get(intent, ("",))).strip()
    target = re.sub(r"^(?:the\s+|current\s+|this\s+)", "", target).strip()
    target = re.sub(r"\s+window$", "", target).strip()
    if target in {"", "it", "this", "that", "current app", "current window"}:
        return {}
    canonical = canonicalize_app_name(target)
    return {
        "app": canonical or target,
        "app_name": canonical or target,
        "requested_app": target,
    }


def _extract_search_entities(text: str) -> dict[str, Any]:
    parsed = parse_browser_command(text)
    if parsed is not None and parsed.action == "search":
        entities: dict[str, Any] = {"query": parsed.query.strip()}
        if parsed.browser:
            entities["browser"] = parsed.browser
        return entities
    query = _strip_prefix(text, ("search for ", "search ", "google ", "look up ", "lookup "))
    return {"query": query.strip()}


def _extract_website_entities(text: str) -> dict[str, Any]:
    parsed = parse_browser_command(text)
    if parsed is not None and parsed.action == "open_url":
        entities: dict[str, Any] = {"website": parsed.website or parsed.url, "url": parsed.url}
        if parsed.browser:
            entities["browser"] = parsed.browser
        return entities
    target = _strip_prefix(text, ("open ",)).strip()
    url = website_url_for(target)
    return {"website": target, "url": url}


def _extract_browser_action_entities(text: str) -> dict[str, Any]:
    parsed = parse_browser_command(text)
    if parsed is None:
        return {}
    entities: dict[str, Any] = {"action": parsed.action}
    if parsed.browser:
        entities["browser"] = parsed.browser
    return entities


def _extract_browser_tab_entities(text: str, *, include_index: bool) -> dict[str, Any]:
    parsed = parse_browser_command(text)
    if parsed is None:
        return {}
    entities: dict[str, Any] = {}
    if parsed.browser:
        entities["browser"] = parsed.browser
    if include_index and parsed.tab_index:
        entities["tab_index"] = parsed.tab_index
    return entities


def _extract_contact_and_message(payload: str) -> tuple[str, str]:
    cleaned = payload.strip()
    if not cleaned:
        return "", ""

    say_match = re.match(r"^say\s+(.+?)\s+to\s+(.+)$", cleaned, flags=re.IGNORECASE)
    if say_match:
        return say_match.group(2).strip(), say_match.group(1).strip()

    tell_match = re.match(r"^tell\s+(.+?)\s+(?:that\s+)?(.+)$", cleaned, flags=re.IGNORECASE)
    if tell_match:
        return tell_match.group(1).strip(), tell_match.group(2).strip()

    send_colon_match = re.match(
        r"^(?:send\s+(?:a\s+)?message\s+to|message|text|msg|notify|inform|ping)\s+(.+?)\s*:\s*(.+)$",
        cleaned,
        flags=re.IGNORECASE,
    )
    if send_colon_match:
        return send_colon_match.group(1).strip(), send_colon_match.group(2).strip()

    quoted = re.match(r"(.+?)\s+\"(.+)\"$", cleaned)
    if quoted:
        return quoted.group(1).strip(), quoted.group(2).strip()

    for separator in (" saying ", " that ", " with ", ": "):
        if separator in cleaned:
            contact, message = cleaned.split(separator, 1)
            return contact.strip(), message.strip()

    parts = cleaned.split(maxsplit=1)
    if len(parts) == 1:
        return parts[0].strip(), ""
    return parts[0].strip(), parts[1].strip()


def _strip_prefix(text: str, prefixes: tuple[str, ...]) -> str:
    for prefix in sorted(prefixes, key=len, reverse=True):
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def _target_from_last_action(value: Any) -> str:
    text = str(value or "").strip()
    if ":" not in text:
        return ""
    _intent, target = text.split(":", 1)
    return target.strip()


def _normalize_app(value: Any) -> str:
    normalized = " ".join(str(value or "").strip().lower().split())
    if normalized in {"", "unknown"}:
        return normalized
    return canonicalize_app_name(normalized, resolve_browser_alias=False) or normalized


def _normalize_intent(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split()).replace(" ", "_")


def looks_like_open_file_request(text: str) -> bool:
    normalized = normalize_command(text)
    if not normalized.startswith("open "):
        return False
    return looks_like_file_reference(normalized)


def looks_like_search_file_request(text: str) -> bool:
    return is_probable_file_search(normalize_command(text))
