"""
Layered natural-language router for high-confidence media commands.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence

from core.logger import get_logger

logger = get_logger(__name__)


class IntentType(str, Enum):
    MEDIA_RESUME = "media_resume"
    MEDIA_PAUSE = "media_pause"
    MEDIA_NEXT = "media_next"
    MEDIA_PREVIOUS = "media_previous"
    MEDIA_STOP = "media_stop"
    MEDIA_MUTE = "media_mute"
    MEDIA_UNMUTE = "media_unmute"
    GREETING = "greeting"
    UNKNOWN = "unknown"


class MediaTarget(str, Enum):
    YOUTUBE = "youtube"
    SPOTIFY = "spotify"
    VLC = "vlc"
    CHROME = "chrome"
    EDGE = "edge"
    GENERIC = "generic"


@dataclass(slots=True)
class NLUIntent:
    intent: IntentType
    confidence: float
    target: Optional[MediaTarget] = None
    entities: dict[str, Any] = field(default_factory=dict)
    normalized_text: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)


class NLURouter:
    """Rules-first media NLU with context-aware target scoring."""

    STT_CORRECTIONS = {
        "continew": "continue",
        "continute": "continue",
        "continu": "continue",
        "plae": "play",
        "pley": "play",
        "plya": "play",
        "serch": "search",
        "pausse": "pause",
        "previus": "previous",
        "preveous": "previous",
        "mutt": "mute",
        "untmute": "unmute",
        "youtub": "youtube",
    }
    FILLER_WORDS = {"uh", "um", "umm", "erm", "like", "please", "can", "you"}
    ACTION_SYNONYMS = {
        "resume": {"resume", "continue", "go on", "play again", "start again"},
        "pause": {"pause", "hold", "stop for now"},
        "next": {"next", "skip", "skip song", "skip track"},
        "previous": {"previous", "back", "back track"},
        "stop": {"stop playback", "stop media", "stop video"},
        "mute": {"mute", "turn sound off", "sound off"},
        "unmute": {"unmute", "turn sound on", "sound on"},
    }
    TARGET_HINTS = {
        MediaTarget.YOUTUBE: {"youtube", "youtube playback", "youtube video"},
        MediaTarget.SPOTIFY: {"spotify", "song", "music", "track"},
        MediaTarget.VLC: {"vlc", "vlc player"},
        MediaTarget.CHROME: {"chrome"},
        MediaTarget.EDGE: {"edge"},
    }
    MEDIA_WORDS = {"video", "song", "music", "track", "playback", "media"}
    FAST_PATTERNS = (
        (re.compile(r"^(continue|resume)\s+(the\s+)?youtube(\s+video)?$"), IntentType.MEDIA_RESUME, MediaTarget.YOUTUBE),
        (re.compile(r"^(continue|resume)\s+(the\s+)?(youtube\s+)?video$"), IntentType.MEDIA_RESUME, MediaTarget.YOUTUBE),
        (re.compile(r"^(continue|resume)\s+playback$"), IntentType.MEDIA_RESUME, MediaTarget.GENERIC),
        (re.compile(r"^play again$"), IntentType.MEDIA_RESUME, MediaTarget.GENERIC),
        (re.compile(r"^pause(\s+(the\s+)?(youtube\s+)?video)?$"), IntentType.MEDIA_PAUSE, MediaTarget.GENERIC),
        (re.compile(r"^pause playback$"), IntentType.MEDIA_PAUSE, MediaTarget.GENERIC),
        (re.compile(r"^(next|skip)(\s+(song|track))?$"), IntentType.MEDIA_NEXT, MediaTarget.SPOTIFY),
        (re.compile(r"^(previous|back)(\s+(song|track))?$"), IntentType.MEDIA_PREVIOUS, MediaTarget.SPOTIFY),
        (re.compile(r"^back track$"), IntentType.MEDIA_PREVIOUS, MediaTarget.SPOTIFY),
        (re.compile(r"^mute(\s+video)?$"), IntentType.MEDIA_MUTE, MediaTarget.GENERIC),
        (re.compile(r"^unmute(\s+video)?$"), IntentType.MEDIA_UNMUTE, MediaTarget.GENERIC),
        (re.compile(r"^stop playback$"), IntentType.MEDIA_STOP, MediaTarget.GENERIC),
    )

    def route(
        self,
        text: str,
        *,
        context_app: str = "",
        window_title: str = "",
        last_action: str = "",
        recent_commands: Sequence[dict[str, Any]] | Sequence[Any] | None = None,
    ) -> NLUIntent:
        normalized = self._normalize(text)
        corrected = self._apply_stt_corrections(normalized)
        canonical = self._apply_semantic_normalization(corrected)
        context_target = self._context_target(context_app, window_title, last_action, recent_commands)

        if not canonical:
            return NLUIntent(intent=IntentType.UNKNOWN, confidence=0.0, normalized_text=canonical)

        exact = self._match_fast_path(canonical, context_target)
        if exact is not None:
            return exact

        action = self._extract_action(canonical)
        entities = self._extract_entities(canonical)
        explicit_target = entities.get("target_app") or ""
        target = self._resolve_target(explicit_target, context_target, canonical)
        candidates = self._build_candidates(canonical, action, target, context_target)
        if not candidates:
            return NLUIntent(intent=IntentType.UNKNOWN, confidence=0.0, normalized_text=canonical)

        best = max(candidates, key=lambda item: item["confidence"])
        if best["confidence"] < 0.75:
            return NLUIntent(
                intent=IntentType.UNKNOWN,
                confidence=best["confidence"],
                normalized_text=canonical,
                entities=entities,
                candidates=candidates,
            )

        resolved_target = MediaTarget(best["target"]) if best["target"] else None
        entities.update(
            {
                "action": best["action"],
                "target_app": resolved_target.value if resolved_target else "",
                "media_type": entities.get("media_type") or ("video" if resolved_target == MediaTarget.YOUTUBE else "playback"),
            }
        )
        return NLUIntent(
            intent=IntentType(best["intent"]),
            confidence=best["confidence"],
            target=resolved_target,
            entities=entities,
            normalized_text=canonical,
            candidates=candidates,
        )

    def _normalize(self, text: str) -> str:
        value = unicodedata.normalize("NFKD", str(text or ""))
        value = "".join(ch for ch in value if not unicodedata.combining(ch))
        value = value.casefold().strip()
        value = re.sub(r"[^\w\s']", " ", value)
        value = re.sub(r"\s+", " ", value)
        tokens: list[str] = []
        previous = ""
        for token in value.split():
            if token in self.FILLER_WORDS:
                continue
            if token == previous:
                continue
            tokens.append(token)
            previous = token
        return " ".join(tokens)

    def _apply_stt_corrections(self, text: str) -> str:
        return " ".join(self.STT_CORRECTIONS.get(token, token) for token in text.split())

    def _apply_stts_corrections(self, text: str) -> str:
        return self._apply_stt_corrections(text)

    def _apply_semantic_normalization(self, text: str) -> str:
        normalized = text
        replacements = (
            ("turn sound off", "mute"),
            ("turn sound on", "unmute"),
            ("start again", "resume"),
            ("go on", "resume"),
            ("stop for now", "pause"),
            ("youtube video", "youtube video"),
            ("youtube playback", "youtube playback"),
        )
        for source, target in replacements:
            normalized = normalized.replace(source, target)
        return re.sub(r"\s+", " ", normalized).strip()

    def _match_fast_path(self, text: str, context_target: str) -> Optional[NLUIntent]:
        for pattern, intent, target in self.FAST_PATTERNS:
            if not pattern.match(text):
                continue
            resolved_target = target
            if target == MediaTarget.GENERIC and context_target in MediaTarget._value2member_map_:
                resolved_target = MediaTarget(context_target)
            entities = self._extract_entities(text)
            entities["action"] = intent.value.replace("media_", "")
            entities["target_app"] = resolved_target.value
            return NLUIntent(
                intent=intent,
                confidence=0.97 if resolved_target != MediaTarget.GENERIC else 0.94,
                target=resolved_target,
                entities=entities,
                normalized_text=text,
                candidates=[
                    {
                        "intent": intent.value,
                        "confidence": 0.97 if resolved_target != MediaTarget.GENERIC else 0.94,
                        "target": resolved_target.value,
                        "action": entities["action"],
                    }
                ],
            )
        if text in {"resume", "pause", "next", "previous", "back"} and context_target in MediaTarget._value2member_map_:
            mapped = {
                "resume": IntentType.MEDIA_RESUME,
                "pause": IntentType.MEDIA_PAUSE,
                "next": IntentType.MEDIA_NEXT,
                "previous": IntentType.MEDIA_PREVIOUS,
                "back": IntentType.MEDIA_PREVIOUS,
            }[text]
            resolved_target = MediaTarget(context_target)
            return NLUIntent(
                intent=mapped,
                confidence=0.84,
                target=resolved_target,
                entities={"action": mapped.value.replace("media_", ""), "target_app": resolved_target.value},
                normalized_text=text,
            )
        return None

    def _extract_action(self, text: str) -> str:
        tokens = set(text.split())
        if {"continue", "resume", "play"} & tokens and ("play again" not in text or "resume" in text):
            return "resume"
        if "pause" in tokens:
            return "pause"
        if {"next", "skip"} & tokens:
            return "next"
        if {"previous", "back"} & tokens:
            return "previous"
        if "mute" in tokens:
            return "mute"
        if "unmute" in tokens:
            return "unmute"
        if "stop" in tokens:
            return "stop"
        return ""

    def _extract_entities(self, text: str) -> dict[str, Any]:
        tokens = set(text.split())
        target = ""
        for candidate, terms in self.TARGET_HINTS.items():
            if tokens & set(" ".join(terms).split()) or any(term in text for term in terms):
                target = candidate.value
                break
        media_type = ""
        if "video" in tokens:
            media_type = "video"
        elif {"song", "track", "music"} & tokens:
            media_type = "track"
        elif "playback" in tokens:
            media_type = "playback"
        return {"target_app": target, "media_type": media_type}

    def _build_candidates(
        self,
        text: str,
        action: str,
        target: str,
        context_target: str,
    ) -> list[dict[str, Any]]:
        if not action:
            return []
        action_map = {
            "resume": IntentType.MEDIA_RESUME.value,
            "pause": IntentType.MEDIA_PAUSE.value,
            "next": IntentType.MEDIA_NEXT.value,
            "previous": IntentType.MEDIA_PREVIOUS.value,
            "stop": IntentType.MEDIA_STOP.value,
            "mute": IntentType.MEDIA_MUTE.value,
            "unmute": IntentType.MEDIA_UNMUTE.value,
        }
        confidence = 0.55
        if text in {"continue playback", "resume playback", "play again"}:
            confidence += 0.28
        if "youtube" in text or "spotify" in text or "vlc" in text:
            confidence += 0.20
        if any(word in text.split() for word in self.MEDIA_WORDS):
            confidence += 0.10
        if context_target and target == context_target:
            confidence += 0.12
        elif context_target and not target:
            confidence += 0.08
        resolved_target = target or context_target or MediaTarget.GENERIC.value
        if text in {"resume", "pause", "next", "previous", "back"} and not context_target:
            confidence -= 0.20
        return [
            {
                "intent": action_map[action],
                "confidence": round(max(0.0, min(0.99, confidence)), 3),
                "target": resolved_target,
                "action": action,
            }
        ]

    def _resolve_target(self, explicit_target: str, context_target: str, text: str) -> str:
        if explicit_target:
            return explicit_target
        if context_target:
            if context_target in {MediaTarget.YOUTUBE.value, MediaTarget.SPOTIFY.value, MediaTarget.VLC.value}:
                return context_target
            if context_target in {MediaTarget.CHROME.value, MediaTarget.EDGE.value} and "youtube" in text:
                return MediaTarget.YOUTUBE.value
        if "youtube" in text:
            return MediaTarget.YOUTUBE.value
        if "spotify" in text:
            return MediaTarget.SPOTIFY.value
        if "vlc" in text:
            return MediaTarget.VLC.value
        return ""

    def _context_target(
        self,
        context_app: str,
        window_title: str,
        last_action: str,
        recent_commands: Sequence[dict[str, Any]] | Sequence[Any] | None,
    ) -> str:
        app = str(context_app or "").strip().lower()
        title = str(window_title or "").strip().lower()
        last = str(last_action or "").strip().lower()
        if app in MediaTarget._value2member_map_:
            return app
        if "youtube" in title:
            return MediaTarget.YOUTUBE.value
        if "spotify" in title:
            return MediaTarget.SPOTIFY.value
        if "vlc" in title:
            return MediaTarget.VLC.value
        if ":youtube" in last or last.startswith("youtube_"):
            return MediaTarget.YOUTUBE.value
        if ":spotify" in last or last.startswith("music_"):
            return MediaTarget.SPOTIFY.value
        for item in list(recent_commands or [])[:3]:
            if isinstance(item, dict):
                target_app = str(item.get("target_app") or "").strip().lower()
                if target_app in MediaTarget._value2member_map_:
                    return target_app
        return ""


_nlu_router: Optional[NLURouter] = None


def get_nlu_router() -> NLURouter:
    global _nlu_router
    if _nlu_router is None:
        _nlu_router = NLURouter()
    return _nlu_router
