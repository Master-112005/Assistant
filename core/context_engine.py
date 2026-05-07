"""
Phase 14 - Context reasoning engine.

Uses active app context, window metadata, recent command history, and the last
successful action to resolve short commands into explicit, app-targeted plans.
"""
from __future__ import annotations

from collections import Counter, deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from core import settings, state as runtime_state_module
from core.context_models import ContextDecision, NULL_DECISION, RecentCommand
from core.logger import get_logger
from skills.browser import BrowserSkill
from skills.explorer import ExplorerSkill
from skills.whatsapp import WhatsAppSkill
from skills.youtube import YouTubeSkill

logger = get_logger(__name__)


_BROWSER_APPS = {"browser", "chrome", "edge", "firefox", "brave", "opera", "vivaldi", "ie"}
_GENERIC_CONTEXTS = {"", "unknown", "browser"} | _BROWSER_APPS
_TITLE_HINTS: tuple[tuple[str, str], ...] = (
    ("youtube", "youtube"),
    ("whatsapp web", "whatsapp"),
    ("whatsapp", "whatsapp"),
    ("spotify", "spotify"),
)
_ORDINALS = {
    "first": 1,
    "1st": 1,
    "1": 1,
    "one": 1,
    "second": 2,
    "2nd": 2,
    "2": 2,
    "two": 2,
    "third": 3,
    "3rd": 3,
    "3": 3,
    "three": 3,
    "fourth": 4,
    "4th": 4,
    "4": 4,
    "fifth": 5,
    "5th": 5,
    "5": 5,
}
_MEDIA_WORDS = {"song", "music", "track", "video", "playlist", "album"}
_REFERENTIAL_WORDS = {
    "this",
    "that",
    "it",
    "selected",
    "current",
    "first",
    "second",
    "third",
    "next",
    "previous",
    "back",
}
_YOUTUBE_QUERY_PREFIXES = (
    "play ",
    "watch ",
    "listen to ",
    "listen ",
    "search for ",
    "search ",
    "find ",
)
_WHATSAPP_CALL_PREFIXES = ("call ", "ring ", "voice call ", "video call ")
_WHATSAPP_MESSAGE_PREFIXES = ("message ", "msg ", "text ", "send ", "reply ", "chat ")
_BROWSER_SEARCH_PREFIXES = ("search for ", "search ", "look up ", "find ", "google ")
_EXPLORER_ACTION_PREFIXES = ("delete ", "remove ", "open ", "rename ", "move ", "copy ", "cut ", "paste ")
_APP_RELEVANCE_BY_SOURCE = {
    "current_context": 1.00,
    "window_title": 0.95,
    "last_action": 0.82,
    "recent_history": 0.75,
    "generic": 0.48,
}


class ContextEngine:
    """Rules-first context resolution engine."""

    _HISTORY_LIMIT = 20

    def __init__(self) -> None:
        self._history: Deque[RecentCommand] = deque(maxlen=self._HISTORY_LIMIT)
        self._browser = BrowserSkill()
        self._youtube = YouTubeSkill(browser=self._browser)
        self._whatsapp = WhatsAppSkill()
        self._explorer = ExplorerSkill()

    def resolve(
        self,
        text: str,
        current_context: Optional[Any] = None,
        runtime_state: Any = None,
        current_app: Optional[str] = None,
        window_title: Optional[str] = None,
    ) -> ContextDecision:
        """
        Resolve text against the current runtime context.

        Supports both:
            resolve(text, current_context=<dict>, runtime_state=state)
            resolve(text, current_app="youtube", window_title="YouTube - Chrome")
        """
        if not settings.get("context_engine_enabled"):
            return NULL_DECISION

        raw_text = str(text or "").strip()
        if not raw_text:
            return NULL_DECISION

        state_obj = runtime_state or runtime_state_module
        context_data = self._coerce_context(
            current_context=current_context,
            runtime_state=state_obj,
            current_app=current_app,
            window_title=window_title,
        )
        recent_history = self.use_recent_history(state_obj)
        last_action = self._safe_text(getattr(state_obj, "last_successful_action", ""))
        effective_app, app_source = self._resolve_effective_app(
            text=raw_text,
            context_data=context_data,
            recent_history=recent_history,
            last_action=last_action,
        )

        try:
            decision = self.resolve_by_app(
                raw_text,
                effective_app,
                context_data=context_data,
                recent_history=recent_history,
                last_action=last_action,
                app_source=app_source,
            )
        except Exception as exc:
            logger.error("ContextEngine.resolve() error: %s", exc)
            return NULL_DECISION

        self._update_state(decision, state_obj)
        self._log_decision(raw_text, context_data, decision)
        return decision

    def resolve_by_app(
        self,
        text: str,
        app: str,
        *,
        context_data: Optional[dict[str, Any]] = None,
        recent_history: Optional[list[RecentCommand]] = None,
        last_action: str = "",
        app_source: str = "current_context",
    ) -> ContextDecision:
        """Route resolution to the correct app-specific resolver."""
        context = context_data or {}
        history = recent_history or []
        normalized_app = self._normalize_app(app)
        text_l = self._normalize_text(text)
        app_score = _APP_RELEVANCE_BY_SOURCE.get(app_source, 0.55)
        matched_layers = [app_source] if app_source else []

        if normalized_app == "youtube":
            return self.resolve_youtube(text, text_l, history, last_action, app_score, matched_layers)
        if normalized_app == "whatsapp":
            return self.resolve_whatsapp(text, text_l, history, last_action, app_score, matched_layers)
        if normalized_app in _BROWSER_APPS:
            return self.resolve_browser(text, text_l, normalized_app, history, last_action, app_score, matched_layers)
        if normalized_app == "explorer":
            return self.resolve_explorer(text, text_l, context, history, last_action, app_score, matched_layers)
        if normalized_app == "spotify":
            return self.resolve_generic(
                text,
                text_l,
                current_app=normalized_app,
                context_data=context,
                recent_history=history,
                last_action=last_action,
                matched_layers=matched_layers,
            )
        return self.resolve_generic(
            text,
            text_l,
            current_app=normalized_app,
            context_data=context,
            recent_history=history,
            last_action=last_action,
            matched_layers=matched_layers,
        )

    def resolve_youtube(
        self,
        text: str,
        text_l: str,
        recent_history: list[RecentCommand],
        last_action: str,
        app_score: float,
        matched_layers: list[str],
    ) -> ContextDecision:
        """Resolve text when the effective context is YouTube."""
        history_score = self._history_alignment("youtube", recent_history, last_action)

        control_map = {
            "pause": "pause",
            "resume": "resume",
            "play now": "play",
            "next video": "next_video",
            "next": "next_video",
            "previous video": "previous_video",
            "previous": "previous_video",
            "fullscreen": "fullscreen",
            "full screen": "fullscreen",
            "exit fullscreen": "exit_fullscreen",
            "exit full screen": "exit_fullscreen",
            "theater mode": "theater_mode",
            "theatre mode": "theater_mode",
            "captions on": "captions_on",
            "captions off": "captions_off",
            "subtitles on": "captions_on",
            "subtitles off": "captions_off",
            "mute": "mute",
            "unmute": "unmute",
            "volume up": "volume_up",
            "increase volume": "volume_up",
            "volume down": "volume_down",
            "decrease volume": "volume_down",
            "read results": "read_results",
            "read page": "read_results",
            "summarize page": "read_results",
            "summarize this page": "read_results",
            "read title": "read_title",
            "read current title": "read_title",
            "what is playing": "read_title",
            "what's playing": "read_title",
            "replay": "replay",
            "rewind": "seek_backward",
            "forward": "seek_forward",
        }
        for phrase, control in control_map.items():
            if text_l == phrase or text_l.startswith(f"{phrase} "):
                return self._decision(
                    original_text=text,
                    resolved_intent="youtube_control",
                    target_app="youtube",
                    reason=f"Matched YouTube control '{phrase}'.",
                    rewritten_command=f"{control.replace('_', ' ')} the active YouTube playback",
                    entities={"control": control},
                    matched_layers=matched_layers,
                    plan_steps=[self._youtube.build_control_step(control)],
                    context_used="youtube",
                    app_relevance=app_score,
                    verb_match=1.0,
                    history_consistency=history_score,
                    entity_clarity=0.80,
                )

        ordinal = self._find_ordinal(text_l)
        if ordinal and self._references_search_results(text_l):
            return self._decision(
                original_text=text,
                resolved_intent="youtube_select_result",
                target_app="youtube",
                reason=f"Matched YouTube ordinal result selection: {ordinal}.",
                rewritten_command=f"Open YouTube result number {ordinal}",
                entities={"result_index": ordinal},
                matched_layers=matched_layers + ["last_action"],
                plan_steps=[self._youtube.build_select_step(ordinal, autoplay=True)],
                context_used="youtube",
                app_relevance=app_score,
                verb_match=0.95,
                history_consistency=max(history_score, 0.90),
                entity_clarity=0.88,
            )

        matched_prefix, query = self._extract_after_prefixes(text_l, _YOUTUBE_QUERY_PREFIXES)
        if matched_prefix and query:
            autoplay = matched_prefix.startswith(("play", "watch", "listen"))
            result_index = ordinal or 1
            rewritten = f"Search for {query} on YouTube"
            if autoplay:
                rewritten = f"{rewritten} and play result {result_index}"
            return self._decision(
                original_text=text,
                resolved_intent="youtube_search",
                target_app="youtube",
                reason=f"Matched YouTube query command '{matched_prefix.strip()}'.",
                rewritten_command=rewritten,
                entities={"query": query, "result_index": result_index if autoplay else None},
                matched_layers=matched_layers,
                plan_steps=self._youtube.build_search_steps(query, autoplay=autoplay, result_index=result_index),
                context_used="youtube",
                app_relevance=app_score,
                verb_match=1.0,
                history_consistency=history_score,
                entity_clarity=0.96,
            )

        if text_l == "play" and self._last_recent_query(recent_history, "youtube"):
            query = self._last_recent_query(recent_history, "youtube")
            return self._decision(
                original_text=text,
                resolved_intent="youtube_search",
                target_app="youtube",
                reason="Reused the most recent YouTube query for a bare play command.",
                rewritten_command=f"Search for {query} on YouTube and play result 1",
                entities={"query": query, "result_index": 1},
                matched_layers=matched_layers + ["recent_history"],
                plan_steps=self._youtube.build_search_steps(query, autoplay=True, result_index=1),
                context_used="youtube",
                app_relevance=app_score,
                verb_match=0.88,
                history_consistency=1.0,
                entity_clarity=0.82,
            )

        return self._low_confidence(
            text=text,
            target_app="youtube",
            resolved_intent="youtube_ambiguous",
            reason="YouTube is active, but the command did not match a supported short-form rule.",
            matched_layers=matched_layers,
        )

    def resolve_whatsapp(
        self,
        text: str,
        text_l: str,
        recent_history: list[RecentCommand],
        last_action: str,
        app_score: float,
        matched_layers: list[str],
    ) -> ContextDecision:
        """Resolve text when the effective context is WhatsApp."""
        history_score = self._history_alignment("whatsapp", recent_history, last_action)

        matched_prefix, contact = self._extract_after_prefixes(text_l, _WHATSAPP_CALL_PREFIXES)
        if matched_prefix:
            if contact:
                return self._decision(
                    original_text=text,
                    resolved_intent="whatsapp_call",
                    target_app="whatsapp",
                    reason="Matched a WhatsApp call command.",
                    rewritten_command=f"Call {contact} on WhatsApp",
                    entities={"contact": contact},
                    matched_layers=matched_layers,
                    plan_steps=[self._whatsapp.build_call_step(contact)],
                    context_used="whatsapp",
                    app_relevance=app_score,
                    verb_match=1.0,
                    history_consistency=history_score,
                    entity_clarity=0.95,
                )
            return self._low_confidence(
                text=text,
                target_app="whatsapp",
                resolved_intent="whatsapp_call",
                reason="WhatsApp call command is missing a contact.",
                matched_layers=matched_layers,
                clarification_prompt="Who do you want to call on WhatsApp?",
            )

        matched_prefix, payload = self._extract_after_prefixes(text_l, _WHATSAPP_MESSAGE_PREFIXES)
        if matched_prefix:
            contact, message = self._extract_contact_and_message(payload)
            if contact:
                rewritten = f"Send a WhatsApp message to {contact}"
                if message:
                    rewritten = f"{rewritten}: {message}"
                return self._decision(
                    original_text=text,
                    resolved_intent="whatsapp_message",
                    target_app="whatsapp",
                    reason="Matched a WhatsApp message command.",
                    rewritten_command=rewritten,
                    entities={"contact": contact, "message": message},
                    matched_layers=matched_layers,
                    plan_steps=[self._whatsapp.build_message_step(contact, message)],
                    context_used="whatsapp",
                    app_relevance=app_score,
                    verb_match=1.0,
                    history_consistency=history_score,
                    entity_clarity=0.92 if contact else 0.50,
                )
            return self._low_confidence(
                text=text,
                target_app="whatsapp",
                resolved_intent="whatsapp_message",
                reason="WhatsApp message command is missing a contact.",
                matched_layers=matched_layers,
                clarification_prompt="Who should I message on WhatsApp?",
            )

        ordinal = self._find_ordinal(text_l)
        if ordinal and ("chat" in text_l or "contact" in text_l or text_l.startswith(("open", "show"))):
            return self._decision(
                original_text=text,
                resolved_intent="whatsapp_open_chat",
                target_app="whatsapp",
                reason=f"Matched WhatsApp ordinal chat selection: {ordinal}.",
                rewritten_command=f"Open WhatsApp chat number {ordinal}",
                entities={"chat_index": ordinal},
                matched_layers=matched_layers,
                plan_steps=[self._whatsapp.build_open_chat_step(chat_index=ordinal)],
                context_used="whatsapp",
                app_relevance=app_score,
                verb_match=0.92,
                history_consistency=history_score,
                entity_clarity=0.85,
            )

        return self._low_confidence(
            text=text,
            target_app="whatsapp",
            resolved_intent="whatsapp_ambiguous",
            reason="WhatsApp is active, but the command did not match a supported short-form rule.",
            matched_layers=matched_layers,
        )

    def resolve_browser(
        self,
        text: str,
        text_l: str,
        app: str,
        recent_history: list[RecentCommand],
        last_action: str,
        app_score: float,
        matched_layers: list[str],
    ) -> ContextDecision:
        """Resolve text when the effective context is a browser."""
        history_score = self._history_alignment(app, recent_history, last_action)

        navigation_phrases = {
            "go back": "back",
            "back": "back",
            "forward": "forward",
            "refresh": "refresh",
            "reload": "refresh",
            "new tab": "new_tab",
            "close tab": "close_tab",
            "next tab": "next_tab",
            "previous tab": "previous_tab",
            "prev tab": "previous_tab",
            "scroll down": "scroll_down",
            "scroll up": "scroll_up",
        }
        for phrase, action in navigation_phrases.items():
            if text_l == phrase or text_l.startswith(f"{phrase} "):
                step = (
                    self._browser.build_navigation_step(action.replace("scroll_", "scroll "), target_app=app)
                    if action.startswith("scroll_")
                    else self._browser.build_navigation_step(action, target_app=app)
                )
                params = step.setdefault("params", {})
                if action.startswith("scroll_"):
                    params["operation"] = "scroll"
                    params["direction"] = "down" if action.endswith("down") else "up"
                    params["action"] = params["direction"]
                return self._decision(
                    original_text=text,
                    resolved_intent="browser_navigation",
                    target_app=app,
                    reason=f"Matched browser navigation phrase '{phrase}'.",
                    rewritten_command=f"Browser action: {phrase}",
                    entities={"action": action},
                    matched_layers=matched_layers,
                    plan_steps=[step],
                    context_used="browser",
                    app_relevance=app_score,
                    verb_match=1.0,
                    history_consistency=history_score,
                    entity_clarity=0.78,
                )

        ordinal = self._find_ordinal(text_l)
        if ordinal and ("result" in text_l or text_l.startswith(("open", "click"))):
            return self._decision(
                original_text=text,
                resolved_intent="browser_open_result",
                target_app=app,
                reason=f"Matched browser search-result selection: {ordinal}.",
                rewritten_command=f"Open browser result number {ordinal}",
                entities={"result_index": ordinal},
                matched_layers=matched_layers,
                plan_steps=[self._browser.build_open_result_step(ordinal, target_app=app)],
                context_used="browser",
                app_relevance=app_score,
                verb_match=0.95,
                history_consistency=max(history_score, 0.70 if self._references_search_results(text_l) else history_score),
                entity_clarity=0.84,
            )

        matched_prefix, query = self._extract_after_prefixes(text_l, _BROWSER_SEARCH_PREFIXES)
        if matched_prefix and query:
            return self._decision(
                original_text=text,
                resolved_intent="browser_search",
                target_app=app,
                reason=f"Matched browser search command '{matched_prefix.strip()}'.",
                rewritten_command=f"Search for {query} in {app}",
                entities={"query": query},
                matched_layers=matched_layers,
                plan_steps=self._browser.build_search_steps(query, target_app=app),
                context_used="browser",
                app_relevance=app_score,
                verb_match=1.0,
                history_consistency=history_score,
                entity_clarity=0.96,
            )

        return self._low_confidence(
            text=text,
            target_app=app,
            resolved_intent="browser_ambiguous",
            reason="Browser is active, but the command did not match a supported short-form rule.",
            matched_layers=matched_layers,
        )

    def resolve_explorer(
        self,
        text: str,
        text_l: str,
        context_data: dict[str, Any],
        recent_history: list[RecentCommand],
        last_action: str,
        app_score: float,
        matched_layers: list[str],
    ) -> ContextDecision:
        """Resolve text when the effective context is File Explorer."""
        history_score = self._history_alignment("explorer", recent_history, last_action)
        selected_item = self._selection_target(context_data.get("selected_item"))
        matched_prefix, remainder = self._extract_after_prefixes(text_l, _EXPLORER_ACTION_PREFIXES)
        if not matched_prefix:
            return self._low_confidence(
                text=text,
                target_app="explorer",
                resolved_intent="explorer_ambiguous",
                reason="Explorer is active, but the command did not match a supported short-form rule.",
                matched_layers=matched_layers,
            )

        action = matched_prefix.strip()
        normalized_action = action.replace(" ", "_")
        destination = ""
        new_name = ""
        target = selected_item or "selected item"
        if remainder:
            if action in {"move", "copy"} and " to " in remainder:
                base_target, destination = remainder.split(" to ", 1)
                target = self._normalize_deictic_target(base_target, selected_item)
            elif action == "rename" and " to " in remainder:
                base_target, new_name = remainder.split(" to ", 1)
                target = self._normalize_deictic_target(base_target, selected_item)
            else:
                target = self._normalize_deictic_target(remainder, selected_item)

        requires_confirmation = action in {"delete", "remove"}
        step = self._explorer.build_file_action_step(
            action=action,
            target=target,
            destination=destination,
            selected=not bool(selected_item),
            new_name=new_name,
        )
        return self._decision(
            original_text=text,
            resolved_intent=f"explorer_{normalized_action}",
            target_app="explorer",
            reason=f"Matched Explorer action '{action}'.",
            rewritten_command=self._build_explorer_rewrite(action, target, destination, new_name),
            entities={
                "action": action,
                "target": target,
                "destination": destination,
                "new_name": new_name,
                "selected_item": selected_item,
            },
            matched_layers=matched_layers + (["selected_item"] if selected_item else []),
            plan_steps=[step],
            context_used="explorer",
            app_relevance=app_score,
            verb_match=1.0,
            history_consistency=history_score,
            entity_clarity=0.88 if target else 0.62,
            requires_confirmation=requires_confirmation,
        )

    def resolve_generic(
        self,
        text: str,
        text_l: str,
        *,
        current_app: str,
        context_data: dict[str, Any],
        recent_history: list[RecentCommand],
        last_action: str,
        matched_layers: list[str],
    ) -> ContextDecision:
        """Fallback resolver for weak or unknown context."""
        history_app = self._dominant_recent_app(recent_history)
        last_action_app = self._extract_app_from_action(last_action)

        if settings.get("use_recent_history_for_context"):
            hint_app = ""
            if self._is_contextual_followup(text_l) and last_action_app:
                hint_app = last_action_app
                source = "last_action"
            elif self._is_contextual_followup(text_l) and history_app:
                hint_app = history_app
                source = "recent_history"
            else:
                hint_app = ""
                source = "generic"

            if hint_app and hint_app not in {current_app, "unknown"}:
                return self.resolve_by_app(
                    text,
                    hint_app,
                    context_data=context_data,
                    recent_history=recent_history,
                    last_action=last_action,
                    app_source=source,
                )

        if text_l.startswith("play "):
            _, query = self._extract_after_prefixes(text_l, ("play ",))
            if query:
                return self._clarification(
                    text=text,
                    reason="Play command is ambiguous without a strong media context.",
                    prompt="Do you want to play this on YouTube or Spotify?",
                    entities={"query": query, "options": ["youtube", "spotify"]},
                    matched_layers=matched_layers,
                )

        if text_l.startswith(("search ", "search for ")) and current_app in {"unknown", "browser"}:
            _, query = self._extract_after_prefixes(text_l, ("search for ", "search "))
            if query and history_app == "youtube":
                return self._clarification(
                    text=text,
                    reason="Search target is ambiguous between the browser and recent app context.",
                    prompt="Do you want to search in Chrome or YouTube?",
                    entities={"query": query, "options": ["chrome", "youtube"]},
                    matched_layers=matched_layers + ["recent_history"],
                )

        return ContextDecision(
            resolved_intent="passthrough",
            target_app=current_app or "unknown",
            reason="No strong context match. Using the standard intent and planner pipeline.",
            confidence=0.40,
            rewritten_command=text,
            original_text=text,
            context_used="generic",
            matched_layers=matched_layers + ["generic"],
            plan_hints={"steps": []},
        )

    def use_recent_history(self, runtime_state: Any = None) -> list[RecentCommand]:
        """Return a sanitized recent-command history list, newest first."""
        state_obj = runtime_state or runtime_state_module
        history: list[RecentCommand] = []
        seen: set[tuple[str, str, str, float]] = set()

        for item in reversed(list(self._history)):
            key = (item.text, item.intent, item.target_app, item.timestamp)
            if key not in seen:
                seen.add(key)
                history.append(item)

        raw_state_history = getattr(state_obj, "recent_commands", [])
        if isinstance(raw_state_history, list):
            for entry in reversed(raw_state_history):
                command = self._coerce_recent_command(entry)
                if not command:
                    continue
                key = (command.text, command.intent, command.target_app, command.timestamp)
                if key in seen:
                    continue
                seen.add(key)
                history.append(command)

        return history[: self._HISTORY_LIMIT]

    def rewrite_command(self, text: str, decision: ContextDecision) -> str:
        """Return the best command text for the planner to consume."""
        return decision.rewritten_command or text

    def score_confidence(
        self,
        app_relevance: float,
        verb_match: float,
        history_consistency: float = 0.0,
        entity_clarity: float = 0.0,
        ambiguity_level: float = 0.0,
    ) -> float:
        """
        Blend context signals into a normalized 0.0-1.0 confidence score.

        Higher ambiguity lowers the score.
        """
        score = (
            0.38 * self._clamp(app_relevance)
            + 0.30 * self._clamp(verb_match)
            + 0.12 * self._clamp(history_consistency)
            + 0.16 * self._clamp(entity_clarity)
            + 0.04 * (1.0 - self._clamp(ambiguity_level))
        )
        return round(self._clamp(score), 3)

    def record_command(
        self,
        text: str,
        intent: str,
        target_app: str,
        success: bool,
        *,
        rewritten_command: str = "",
        entities: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record a completed command into rolling history and shared state."""
        command = RecentCommand(
            text=text,
            intent=intent,
            target_app=target_app or "unknown",
            success=bool(success),
            rewritten_command=rewritten_command,
            entities=entities or {},
        )
        self._history.append(command)
        runtime_state_module.recent_commands = [
            {
                "text": item.text,
                "intent": item.intent,
                "target_app": item.target_app,
                "success": item.success,
                "rewritten_command": item.rewritten_command,
                "entities": item.entities,
                "timestamp": item.timestamp,
            }
            for item in self._history
        ]

    def _decision(
        self,
        *,
        original_text: str,
        resolved_intent: str,
        target_app: str,
        reason: str,
        rewritten_command: str,
        entities: Optional[dict[str, Any]],
        matched_layers: list[str],
        plan_steps: list[dict[str, Any]],
        context_used: str,
        app_relevance: float,
        verb_match: float,
        history_consistency: float,
        entity_clarity: float,
        ambiguity_level: float = 0.0,
        requires_confirmation: bool = False,
        clarification_prompt: str = "",
    ) -> ContextDecision:
        confidence = self.score_confidence(
            app_relevance=app_relevance,
            verb_match=verb_match,
            history_consistency=history_consistency,
            entity_clarity=entity_clarity,
            ambiguity_level=ambiguity_level,
        )
        threshold = float(settings.get("context_confidence_threshold") or 0.70)
        needs_confirmation = requires_confirmation or bool(clarification_prompt) or confidence < threshold
        cleaned_entities = {k: v for k, v in (entities or {}).items() if v not in ("", None, [], {})}
        return ContextDecision(
            resolved_intent=resolved_intent,
            target_app=target_app,
            reason=reason,
            confidence=confidence,
            rewritten_command=rewritten_command,
            entities=cleaned_entities,
            requires_confirmation=needs_confirmation,
            original_text=original_text,
            context_used=context_used,
            clarification_prompt=clarification_prompt,
            matched_layers=matched_layers,
            plan_hints={
                "steps": plan_steps,
                "preferred_app": target_app,
                "resolved_intent": resolved_intent,
            },
        )

    def _clarification(
        self,
        *,
        text: str,
        reason: str,
        prompt: str,
        entities: Optional[dict[str, Any]],
        matched_layers: list[str],
    ) -> ContextDecision:
        return ContextDecision(
            resolved_intent="clarification",
            target_app="unknown",
            reason=reason,
            confidence=0.45,
            rewritten_command=text,
            entities=entities or {},
            requires_confirmation=True,
            original_text=text,
            context_used="generic",
            clarification_prompt=prompt,
            matched_layers=matched_layers + ["ambiguity_resolution"],
            plan_hints={"steps": []},
        )

    def _low_confidence(
        self,
        *,
        text: str,
        target_app: str,
        resolved_intent: str,
        reason: str,
        matched_layers: list[str],
        clarification_prompt: str = "",
    ) -> ContextDecision:
        return ContextDecision(
            resolved_intent=resolved_intent,
            target_app=target_app,
            reason=reason,
            confidence=0.35,
            rewritten_command=text,
            requires_confirmation=True,
            original_text=text,
            context_used=target_app or "generic",
            clarification_prompt=clarification_prompt,
            matched_layers=matched_layers,
            plan_hints={"steps": []},
        )

    def _coerce_context(
        self,
        *,
        current_context: Optional[Any],
        runtime_state: Any,
        current_app: Optional[str],
        window_title: Optional[str],
    ) -> dict[str, Any]:
        app = ""
        title = ""
        process_name = ""
        selected_item: dict[str, Any] | str = {}

        if isinstance(current_context, dict):
            app = self._safe_text(
                current_context.get("current_context")
                or current_context.get("app_id")
                or current_context.get("current_app")
                or current_app
            )
            title = self._safe_text(current_context.get("window_title") or current_context.get("title") or window_title)
            process_name = self._safe_text(current_context.get("process_name"))
            selected_item = current_context.get("selected_item") or current_context.get("selection") or {}
        elif isinstance(current_context, str):
            app = self._safe_text(current_context or current_app)
            title = self._safe_text(window_title)
        else:
            app = self._safe_text(current_app)
            title = self._safe_text(window_title)

        app = app or self._safe_text(getattr(runtime_state, "current_context", "")) or self._safe_text(getattr(runtime_state, "current_app", ""))
        title = title or self._safe_text(getattr(runtime_state, "current_window_title", ""))
        process_name = process_name or self._safe_text(getattr(runtime_state, "current_process_name", ""))
        if not selected_item:
            selected_item = getattr(runtime_state, "selected_item_context", {})

        return {
            "app": self._normalize_app(app),
            "title": title,
            "process_name": process_name,
            "selected_item": selected_item,
        }

    def _resolve_effective_app(
        self,
        *,
        text: str,
        context_data: dict[str, Any],
        recent_history: list[RecentCommand],
        last_action: str,
    ) -> tuple[str, str]:
        """Resolve effective app context with explicit command priority.

        CRITICAL: Explicit app mentions in the command MUST override context bias.
        """
        app = self._normalize_app(context_data.get("app"))
        title = self._normalize_text(context_data.get("title", ""))
        text_l = self._normalize_text(text)

        # CRITICAL: Check for explicit app mentions FIRST
        # This prevents context from overriding explicit commands like "open chrome"
        # when the user was previously in YouTube
        explicit_app = self._detect_explicit_app_mention(text_l)
        if explicit_app:
            return explicit_app, "explicit_command"

        if app and app not in _GENERIC_CONTEXTS:
            return app, "current_context"

        title_hint = self._infer_app_from_title(title)
        if title_hint:
            return title_hint, "window_title"

        # Only apply contextual followup if NO explicit app was mentioned
        if self._is_contextual_followup(text_l):
            last_action_app = self._extract_app_from_action(last_action)
            if last_action_app:
                return last_action_app, "last_action"

            if settings.get("use_recent_history_for_context"):
                history_app = self._dominant_recent_app(recent_history)
                if history_app:
                    return history_app, "recent_history"

        if app:
            return app, "current_context"

        return "unknown", "generic"

    def _detect_explicit_app_mention(self, text_l: str) -> str:
        """Detect if the command explicitly mentions a specific app.

        Returns the app name if found, empty string otherwise.
        This is used to prevent context bias from overriding explicit commands.
        """
        # Known apps that can be explicitly mentioned
        known_apps = {
            "chrome": "chrome",
            "edge": "edge",
            "firefox": "firefox",
            "brave": "brave",
            "opera": "opera",
            "vivaldi": "vivaldi",
            "youtube": "youtube",
            "spotify": "spotify",
            "whatsapp": "whatsapp",
            "telegram": "telegram",
            "discord": "discord",
            "slack": "slack",
            "teams": "teams",
            "zoom": "zoom",
            "explorer": "explorer",
            "file explorer": "explorer",
            "vscode": "vscode",
            "pycharm": "pycharm",
            "notepad": "notepad",
            "word": "word",
            "excel": "excel",
            "powerpoint": "powerpoint",
            "terminal": "terminal",
            "command prompt": "cmd",
            "cmd": "cmd",
            "powershell": "powershell",
            "gmail": "gmail",
            "outlook": "outlook",
            "github": "github",
            "chatgpt": "chatgpt",
            "claude": "claude",
            "task manager": "task_manager",
            "netflix": "netflix",
            "twitch": "twitch",
        }

        # Check for app mentions in the text
        for app_key, app_value in known_apps.items():
            if app_key in text_l:
                # Make sure it's not just a tiny substring (e.g., "word" in "password")
                # Require word boundary or common phrase patterns
                if app_key in text_l.split():
                    return app_value
                # Also check for common patterns like "open X", "close X", "in X"
                for prefix in ("open ", "close ", "in ", "on ", "at ", "with ", "using "):
                    if text_l.startswith(prefix + app_key) or f" {app_key} " in text_l:
                        return app_value

        return ""

    def _infer_app_from_title(self, title: str) -> str:
        for fragment, app in _TITLE_HINTS:
            if fragment in title:
                return app
        return ""

    def _history_alignment(
        self,
        app: str,
        recent_history: Sequence[RecentCommand],
        last_action: str,
    ) -> float:
        if not app:
            return 0.0

        score = 0.0
        last_action_app = self._extract_app_from_action(last_action)
        if last_action_app == app:
            score = max(score, 1.0)

        recent_apps = [item.target_app for item in recent_history[:5] if item.success]
        if recent_apps:
            count = sum(1 for item in recent_apps if item == app)
            score = max(score, min(1.0, count / max(1, len(recent_apps))))

        return round(score, 3)

    def _last_recent_query(self, recent_history: Sequence[RecentCommand], app: str) -> str:
        for item in recent_history:
            if item.target_app != app:
                continue
            query = item.entities.get("query") if isinstance(item.entities, dict) else None
            if isinstance(query, str) and query.strip():
                return query.strip()
        return ""

    def _dominant_recent_app(self, recent_history: Sequence[RecentCommand]) -> str:
        if not recent_history:
            return ""
        counts = Counter(item.target_app for item in recent_history[:5] if item.target_app and item.success)
        if not counts:
            return ""
        app, _ = counts.most_common(1)[0]
        return app

    def _extract_contact_and_message(self, payload: str) -> tuple[str, str]:
        trimmed = payload.strip()
        if not trimmed:
            return "", ""
        separators = (" saying ", " that ", " with ", " : ", ": ")
        for separator in separators:
            if separator in trimmed:
                contact, message = trimmed.split(separator, 1)
                return contact.strip(), message.strip()
        parts = trimmed.split(maxsplit=1)
        if len(parts) == 1:
            return parts[0].strip(), ""
        return parts[0].strip(), parts[1].strip() if parts[1].startswith('"') else ""

    def _extract_after_prefixes(self, text_l: str, prefixes: Iterable[str]) -> tuple[str, str]:
        for prefix in sorted(prefixes, key=len, reverse=True):
            if text_l.startswith(prefix):
                return prefix, text_l[len(prefix) :].strip()
        return "", ""

    def _find_ordinal(self, text_l: str) -> int:
        for token, ordinal in _ORDINALS.items():
            if token in text_l.split() or f"{token} " in text_l or text_l.endswith(token):
                return ordinal
        return 0

    def _references_search_results(self, text_l: str) -> bool:
        tokens = set(text_l.split())
        return bool(tokens & {"result", "results", "link", "links", "button", "buttons", "song", "video", "track"}) or text_l.startswith(("open", "click", "play first", "play second"))

    def _normalize_deictic_target(self, raw_target: str, selected_item: str) -> str:
        cleaned = raw_target.strip() or "selected item"
        if cleaned in {"this", "this file", "this folder", "selected", "selected file", "selected folder", "it"}:
            return selected_item or "selected item"
        return cleaned

    def _selection_target(self, selection: Any) -> str:
        if isinstance(selection, str):
            return selection.strip()
        if isinstance(selection, dict):
            for key in ("display_name", "name", "path", "label"):
                value = selection.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _build_explorer_rewrite(self, action: str, target: str, destination: str, new_name: str) -> str:
        if action in {"move", "copy"} and destination:
            return f"{action.title()} {target} to {destination} in Explorer"
        if action == "rename" and new_name:
            return f"Rename {target} to {new_name} in Explorer"
        return f"{action.title()} {target} in Explorer"

    def _is_contextual_followup(self, text_l: str) -> bool:
        tokens = text_l.split()
        token_set = set(tokens)
        if token_set & _REFERENTIAL_WORDS:
            return True
        if text_l in {
            "pause",
            "resume",
            "next",
            "next video",
            "previous",
            "previous video",
            "back",
            "forward",
            "refresh",
            "reload",
            "first result",
            "second result",
            "third result",
        }:
            return True
        return text_l.startswith(("open first", "open second", "delete this", "rename this", "scroll "))

    def _extract_app_from_action(self, action_text: str) -> str:
        lowered = self._normalize_text(action_text)
        for app in ("youtube", "whatsapp", "explorer", "spotify", "chrome", "edge", "firefox", "browser"):
            if app in lowered:
                return app
        return ""

    def _coerce_recent_command(self, entry: Any) -> Optional[RecentCommand]:
        if isinstance(entry, RecentCommand):
            return entry
        if not isinstance(entry, dict):
            return None
        text = self._safe_text(entry.get("text"))
        intent = self._safe_text(entry.get("intent"))
        target_app = self._normalize_app(entry.get("target_app"))
        if not text and not intent:
            return None
        try:
            timestamp = float(entry.get("timestamp", 0.0) or 0.0)
        except (TypeError, ValueError):
            timestamp = 0.0
        return RecentCommand(
            text=text,
            intent=intent or "unknown",
            target_app=target_app or "unknown",
            success=bool(entry.get("success", False)),
            rewritten_command=self._safe_text(entry.get("rewritten_command")),
            entities=entry.get("entities") if isinstance(entry.get("entities"), dict) else {},
            timestamp=timestamp or 0.0,
        )

    def _update_state(self, decision: ContextDecision, state_obj: Any) -> None:
        state_obj.last_context_decision = decision.to_dict()
        state_obj.context_resolution_count = int(getattr(state_obj, "context_resolution_count", 0) or 0) + 1
        if decision.target_app and decision.target_app != "unknown" and decision.confidence >= 0.55:
            state_obj.current_context = decision.target_app

    def _log_decision(
        self,
        text: str,
        context_data: dict[str, Any],
        decision: ContextDecision,
    ) -> None:
        logger.info("Context: %s", context_data.get("app") or "unknown")
        logger.info("Input: %s", self._normalize_text(text))
        logger.info("Decision: %s -> %s", decision.resolved_intent, decision.target_app)
        logger.info("Rewritten: %s", decision.rewritten_command or text)
        logger.info("Confidence: %.2f", decision.confidence)
        if decision.context_used == "generic" or decision.resolved_intent == "passthrough":
            logger.info("Fallback used: standard pipeline")
        if decision.requires_confirmation and decision.clarification_prompt:
            logger.info("Clarification required: %s", decision.clarification_prompt)

    @staticmethod
    def _normalize_app(value: Any) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @staticmethod
    def _safe_text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _clamp(value: float) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0


context_engine = ContextEngine()
