"""
Deterministic backend routing for the direct command pipeline.

HARD PRIORITY HIERARCHY (enforced strictly):
1. System commands (volume, brightness, power)
2. App open/close/minimize/maximize/focus/toggle commands
3. Explicit skill commands (WhatsApp, YouTube, Spotify)
4. Context-based commands
5. Search commands

CRITICAL: App commands (open X, close X, etc.) MUST route to "launcher"
and MUST NOT be intercepted by browser/search skills.
"""
from __future__ import annotations

import re
from typing import Any

from core.app_commands import parse_app_command
from core.logger import get_logger

logger = get_logger(__name__)


_OCR_PHRASES = {"read screen", "what is on screen", "what's on screen"}
_CLICK_PREFIXES = ("click ", "press ", "tap ", "select ")
_SYSTEM_INTENTS = {
    "volume_up",
    "volume_down",
    "set_volume",
    "mute",
    "unmute",
    "brightness_up",
    "brightness_down",
    "set_brightness",
    "lock_pc",
    "shutdown_pc",
    "restart_pc",
    "sleep_pc",
    "system_control",
}
_APP_COMMAND_INTENTS = {
    "open_app",
    "close_app",
    "minimize_app",
    "maximize_app",
    "focus_app",
    "restore_app",
    "toggle_app",
}
_BROWSER_INTENTS = {
    "browser_action",
    "browser_tab_new",
    "search_web",
    "open_website",
    "open_youtube",
    "browser_tab_close",
    "browser_tab_next",
    "browser_tab_previous",
    "browser_tab_switch",
}
_MEDIA_INTENTS = {
    "play_media",
    "pause_media",
    "next_track",
    "previous_track",
    "set_media_volume",
}
_FILE_INTENTS = {
    "create_file",
    "open_file",
    "delete_file",
    "move_file",
    "rename_file",
    "search_file",
    "open_folder",
    "file_action",
}
_COMMUNICATION_INTENTS = {"send_message", "call_contact"}
_MULTI_ACTION_INTENTS = {"multi_action"}
_REMINDER_INTENTS = {"reminder_create", "reminder_list", "reminder_delete", "reminder_enable", "reminder_disable"}

# Apps that have dedicated skills/routes - these MUST go to their specific skill
_DEDICATED_APP_ROUTES = {
    "whatsapp": "whatsapp",
    "youtube": "youtube",
    "spotify": "spotify",
}

# Apps that are browsers - these go to browser skill
_BROWSER_APPS = {"chrome", "edge", "firefox", "brave", "opera", "vivaldi", "safari"}


def route_command(intent: str, entities: dict[str, Any], text: str) -> str:
    """
    Route a normalized command to the correct backend family.

    HARD PRIORITY (first match wins, no fallback override):
    1. OCR commands
    2. Click commands
    3. System commands
    4. App commands (open/close/minimize/etc.) - CRITICAL: routes to launcher or dedicated app
    5. Browser commands
    6. Media commands
    7. File commands
    8. Reminder commands
    9. Communication commands
    10. Unknown -> fallback
    """
    normalized_intent = " ".join(str(intent or "").strip().lower().split())
    lowered = " ".join(str(text or "").strip().lower().split())
    payload = dict(entities or {})

    # Structured debug logging - START OF ROUTING DECISION
    logger.debug(
        "[ROUTER:START] Routing decision: intent='%s', text='%s'",
        normalized_intent,
        lowered,
    )
    logger.debug(
        "[ROUTER:ENTITIES] app=%s, app_name=%s, target_app=%s, query=%s",
        payload.get("app", ""),
        payload.get("app_name", ""),
        payload.get("target_app", ""),
        payload.get("query", "")[:50] if payload.get("query") else "",
    )

    route = "unknown"

    # Priority 1: OCR
    if normalized_intent in {"read_screen", "ocr"} or lowered in _OCR_PHRASES:
        route = "ocr"

    # Priority 2: Click
    elif normalized_intent in {"click", "click_by_text"}:
        route = "click" if lowered.startswith(_CLICK_PREFIXES) else "unknown"

    # Priority 3: System commands
    elif normalized_intent in _SYSTEM_INTENTS:
        route = "system"

    # Priority 4: Communication commands (MUST come before APP commands)
    # This ensures "message hemanth" goes to WhatsApp, not launcher
    elif normalized_intent in _COMMUNICATION_INTENTS:
        route = "whatsapp"

    # Priority 5: App commands (open/close/minimize/etc.)
    elif normalized_intent in _APP_COMMAND_INTENTS:
        route = _route_app_command(normalized_intent, lowered, payload)

    # Priority 6: Browser commands
    elif normalized_intent in _BROWSER_INTENTS:
        route = "browser"

    # Priority 7: Media commands
    elif normalized_intent in _MEDIA_INTENTS:
        route = _route_media_command(normalized_intent, lowered, payload)

    # Priority 8: File commands
    elif normalized_intent in _FILE_INTENTS:
        route = "files"

    # Priority 9: Reminder commands
    elif normalized_intent in _REMINDER_INTENTS:
        route = "reminders"

    # Priority 10: Multi-action commands (forward to planner for execution)
    elif normalized_intent in _MULTI_ACTION_INTENTS:
        route = "planner"

    # Priority 11: WhatsApp fallback (check text for patterns)
    elif route == "unknown" and "whatsapp" in lowered:
        route = "whatsapp"

    # Structured debug logging - output
    logger.debug(
        "[ROUTER:END] Route selected: '%s' for intent '%s'",
        route,
        normalized_intent,
    )

    return route


def _route_app_command(intent: str, text: str, entities: dict[str, Any]) -> str:
    """
    Route app open/close/minimize/etc. commands deterministically.

    CRITICAL: This function enforces hard routing:
    - whatsapp -> "whatsapp" route (NOT browser, NOT search)
    - youtube -> "youtube" route (NOT browser)
    - spotify -> "spotify" route (NOT search)
    - browsers -> "browser" route
    - everything else -> "launcher" route
    """
    app_name = _extract_app_name(entities, text)

    logger.debug(
        "[ROUTER:APP_COMMAND] Processing app command: intent=%s, app_name='%s', text='%s'",
        intent,
        app_name,
        text,
    )

    if not app_name:
        logger.debug("[ROUTER:APP_COMMAND] No app name extracted, defaulting to 'launcher'")
        return "launcher"

    # Check for dedicated app routes first (whatsapp, youtube, spotify)
    if app_name in _DEDICATED_APP_ROUTES:
        route = _DEDICATED_APP_ROUTES[app_name]
        logger.debug(
            "[ROUTER:APP_COMMAND:DEDICATED] Matched dedicated app: '%s' -> route '%s'",
            app_name,
            route,
        )
        return route

    # Check for browser apps
    if app_name in _BROWSER_APPS:
        logger.debug(
            "[ROUTER:APP_COMMAND:BROWSER] Matched browser app: '%s' -> route 'browser'",
            app_name,
        )
        return "browser"

    # Everything else goes to launcher
    logger.debug(
        "[ROUTER:APP_COMMAND:GENERIC] Generic app: '%s' -> route 'launcher'",
        app_name,
    )
    return "launcher"


def _route_browser_command(intent: str, text: str, entities: dict[str, Any]) -> str:
    """Route browser-specific commands."""
    if intent == "open_youtube" or "youtube" in text:
        return "youtube"
    return "browser"


def _route_media_command(intent: str, text: str, entities: dict[str, Any]) -> str:
    """Route media playback commands."""
    app_name = _extract_app_name(entities, text)

    if app_name == "spotify" or "spotify" in text:
        return "spotify"
    if app_name == "youtube" or "youtube" in text:
        return "youtube"

    # Default media routing based on context
    if "song" in text or "track" in text:
        return "spotify"
    return "youtube"


def _extract_app_name(entities: dict[str, Any], text: str) -> str:
    """
    Extract app name from entities or text.

    Priority:
    1. entities["app"]
    2. entities["app_name"]
    3. entities["target_app"]
    4. Parse from text after verb (open X, close X, etc.)
    """
    # Try entities first
    for key in ("app", "app_name", "target_app"):
        val = entities.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()

    parsed = parse_app_command(text)
    if parsed is not None:
        return parsed.app_name

    # Parse from text: "open X" -> X
    match = re.match(r"^(?:open|close|minimize|maximize|focus|restore|toggle)\s+(.+)$", text)
    if match:
        target = match.group(1).strip().lower()
        # Remove common suffixes
        target = re.sub(r"\s+(?:window|app|application)$", "", target).strip()
        return target

    return ""
