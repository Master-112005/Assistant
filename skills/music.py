"""
Music skill router for Spotify and future media providers.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from core import settings, state
from core.logger import get_logger
from core.media import MediaActionResult, MediaMetadata
from skills.base import SkillBase, SkillExecutionResult
from skills.spotify import SpotifyProvider

logger = get_logger(__name__)

_READ_TRACK_COMMANDS = {
    "what song is this",
    "what's playing",
    "what is playing",
    "what track is this",
    "current track",
    "current song",
    "read current track",
    "read current song",
    "what am i listening to",
}
_PLAY_COMMANDS = {"play", "resume", "resume music", "resume spotify", "play music", "play spotify"}
_PAUSE_COMMANDS = {"pause", "pause music", "pause spotify"}
_TOGGLE_COMMANDS = {"play pause", "toggle play", "toggle music", "toggle spotify"}
_NEXT_COMMANDS = {"next track", "next song", "skip song", "skip track", "skip", "next music", "spotify next"}
_PREVIOUS_COMMANDS = {"previous track", "previous song", "last track", "back track", "spotify previous"}
_LIKED_SONGS_COMMANDS = {
    "play liked songs",
    "play my liked songs",
    "open liked songs",
    "play spotify liked songs",
}
_SEARCH_PREFIXES = (
    "search song ",
    "search track ",
    "search spotify for ",
    "search spotify ",
)
_PLAY_QUERY_PREFIXES = (
    "play song ",
    "play track ",
    "play spotify ",
    "play ",
)


class MusicSkill(SkillBase):
    """High-level router for Spotify and future music providers."""

    def __init__(self, *, launcher=None, providers: dict[str, Any] | None = None) -> None:
        self._launcher = launcher
        self._providers = providers or {"spotify": SpotifyProvider(launcher=launcher)}

    def can_handle(self, context: Mapping[str, Any], intent: str, command: str) -> bool:
        if not settings.get("music_skill_enabled"):
            return False

        normalized = self._normalize_command(command)
        if not normalized or self._mentions_youtube(command):
            return False

        action, _payload = self._classify_command(command, context)
        if action == "unsupported":
            return False

        explicit_provider = self._mentions_music(command)
        active_music = self._is_music_context(context)
        recent_music = self._has_recent_music_context(context)
        query_specific = bool(re.search(r"\b(song|track|playlist|album|liked songs)\b", command, flags=re.IGNORECASE))
        resolved_intent = str(context.get("context_resolved_intent") or "").strip().lower()
        target_app = str(context.get("context_target_app") or "").strip().lower()

        if resolved_intent == "clarification" and not explicit_provider:
            return False
        if target_app == "youtube" and not explicit_provider:
            return False
        if resolved_intent.startswith("youtube") and not explicit_provider:
            return False

        if action in {"play_liked_songs", "search_song", "play_search_result"}:
            return True if query_specific or explicit_provider or active_music or recent_music else self._preferred_provider_name() == "spotify"
        if action in {"next_track", "previous_track"}:
            return True
        if action == "play_query":
            return explicit_provider or active_music or recent_music or self._preferred_provider_name() == "spotify"
        if action in {"play", "pause", "play_pause", "read_current_track"}:
            return explicit_provider or active_music or recent_music
        return False

    def execute(
        self,
        command: str,
        context: Mapping[str, Any] | None = None,
        **params: Any,
    ) -> SkillExecutionResult | MediaActionResult:
        if context is not None:
            return self._execute_skill_command(command, context)
        return self.execute_operation(command, **params)

    def execute_operation(self, operation: str, **params: Any) -> MediaActionResult:
        action = self._normalize_command(operation)
        provider_name = str(params.get("provider") or "").strip().lower() or self.detect_provider()
        provider = self._provider_for_name(provider_name)
        if provider is None:
            return self._failure(f"No supported music provider is available for '{provider_name or 'music'}'.", "music_provider_unavailable")

        logger.info("Provider selected: %s", provider_name)
        query = str(params.get("query") or "").strip()
        control = self._normalize_command(str(params.get("control") or "").replace("_", " "))

        if action in {"search", "search_song"}:
            return provider.search_song(query)
        if action in {"play_query", "play_track"}:
            search_result = provider.search_song(query)
            if not search_result.success:
                return search_result
            return provider.play_search_result(index=max(1, int(params.get("result_index", 1) or 1)))
        if action in {"play_liked_songs", "liked_songs"}:
            return provider.play_liked_songs()
        if action in {"play", "resume"}:
            return provider.play()
        if action == "pause":
            return provider.pause()
        if action in {"play_pause", "toggle_play"}:
            return provider.play_pause()
        if action in {"next", "next_track", "skip"}:
            return provider.next_track()
        if action in {"previous", "previous_track", "back_track"}:
            return provider.previous_track()
        if action in {"read_current_track", "current_track", "what_is_playing"}:
            metadata = provider.read_current_track()
            if metadata is None or not metadata.label():
                return self._failure("I couldn't detect the current Spotify track.", "track_metadata_unavailable")
            return MediaActionResult(
                success=True,
                operation="read_current_track",
                message=f"Currently playing: {metadata.label()}",
                data=self._metadata_payload(metadata, "read_current_track"),
            )
        if action == "play_search_result":
            return provider.play_search_result(index=max(1, int(params.get("result_index", 1) or 1)))
        if action == "control":
            if control in {"play", "resume"}:
                return provider.play()
            if control == "pause":
                return provider.pause()
            if control in {"play pause", "toggle play"}:
                return provider.play_pause()
            if control in {"next", "next track", "skip"}:
                return provider.next_track()
            if control in {"previous", "previous track", "back track"}:
                return provider.previous_track()

        return self._failure(f"Unsupported music operation: {operation or 'unknown'}.", "unsupported_music_operation")

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "target": "music",
            "supports": [
                "play_liked_songs",
                "play",
                "pause",
                "play_pause",
                "next_track",
                "previous_track",
                "search_song",
                "play_search_result",
                "read_current_track",
                "provider_detection",
            ],
            "preferred_provider": self._preferred_provider_name(),
            "providers": sorted(self._providers),
        }

    def health_check(self) -> dict[str, Any]:
        active = self.detect_provider()
        return {
            "enabled": bool(settings.get("music_skill_enabled")),
            "preferred_provider": self._preferred_provider_name(),
            "active_provider": active,
            "music_active": bool(getattr(state, "music_active", False)),
            "provider_count": len(self._providers),
        }

    def describe_current_playback(self) -> dict[str, Any]:
        provider = self.get_active_provider()
        if provider is None:
            return {}

        try:
            metadata = provider.read_current_track()
        except Exception as exc:
            logger.debug("Music awareness metadata failed: %s", exc)
            return {}

        if metadata is None or not metadata.label():
            return {}

        playing = metadata.is_playing
        if playing is None and hasattr(provider, "is_playing"):
            try:
                playing = provider.is_playing()
            except Exception as exc:
                logger.debug("Music awareness playback-state failed: %s", exc)

        provider_name = str(metadata.provider or self.detect_provider() or self._preferred_provider_name()).strip().lower()
        label = "Spotify" if provider_name == "spotify" else provider_name.replace("_", " ").title() or "Music"
        summary = f"{label} is playing {metadata.label()}." if playing is not False else f"{label} is open on {metadata.label()}."
        confidence = 0.96 if metadata.source == "media_session" else 0.90 if metadata.source else 0.84
        return {
            "summary": summary,
            "confidence": confidence,
            "details": self._metadata_payload(metadata, "awareness"),
        }

    def detect_provider(self) -> str:
        current_app = str(state.current_context or state.current_app or "").strip().lower()
        if current_app in self._providers:
            return current_app

        active_provider = str(getattr(state, "active_music_provider", "") or "").strip().lower()
        if active_provider in self._providers and getattr(state, "music_active", False):
            return active_provider

        for name, provider in self._providers.items():
            try:
                if provider.detect() is not None:
                    return name
            except Exception as exc:
                logger.debug("Music provider detection failed for %s: %s", name, exc)

        preferred = self._preferred_provider_name()
        return preferred if preferred in self._providers else next(iter(self._providers), "")

    def get_active_provider(self):
        provider_name = self.detect_provider()
        return self._provider_for_name(provider_name)

    def _execute_skill_command(self, command: str, context: Mapping[str, Any]) -> SkillExecutionResult:
        action, payload = self._classify_command(command, context)
        if action == "unsupported":
            return self._skill_failure("I couldn't map that to a supported music action.", "unsupported_music_command")

        provider_name = str(payload.get("provider") or self.detect_provider()).strip().lower()
        provider = self._provider_for_name(provider_name)
        if provider is None:
            return self._skill_failure("No supported music provider is available.", "music_provider_unavailable")

        logger.info("Skill: MusicSkill")
        logger.info("Provider: %s", provider_name)

        if action == "play_liked_songs":
            return self._from_action_result("music_play_liked", provider.play_liked_songs())
        if action == "play":
            return self._from_action_result("music_action", provider.play())
        if action == "pause":
            return self._from_action_result("music_action", provider.pause())
        if action == "play_pause":
            return self._from_action_result("music_action", provider.play_pause())
        if action == "next_track":
            return self._from_action_result("music_action", provider.next_track())
        if action == "previous_track":
            return self._from_action_result("music_action", provider.previous_track())
        if action == "search_song":
            return self._from_action_result("music_search", provider.search_song(str(payload.get("query") or "").strip()))
        if action == "play_query":
            query = str(payload.get("query") or "").strip()
            search_result = provider.search_song(query)
            if not search_result.success:
                return self._from_action_result("music_search", search_result)
            return self._from_action_result("music_play_track", provider.play_search_result(1))
        if action == "read_current_track":
            metadata = provider.read_current_track()
            if metadata is None or not metadata.label():
                return self._skill_failure("I couldn't detect the current Spotify track.", "track_metadata_unavailable")
            payload = self._metadata_payload(metadata, "read_current_track")
            self._sync_state_from_data(payload)
            return SkillExecutionResult(
                success=True,
                intent="music_current_track",
                response=f"Currently playing: {metadata.label()}",
                skill_name=self.name(),
                data=payload,
            )

        return self._skill_failure("Music action is not implemented.", "unsupported_music_action")

    def _classify_command(self, command: str, context: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        normalized = self._normalize_command(command)
        stripped = self._strip_explicit_music_tokens(command)
        stripped_normalized = self._normalize_command(stripped)

        if normalized in _READ_TRACK_COMMANDS or stripped_normalized in _READ_TRACK_COMMANDS:
            return "read_current_track", {}
        if normalized in _LIKED_SONGS_COMMANDS or "liked songs" in stripped_normalized:
            return "play_liked_songs", {}
        if normalized in _NEXT_COMMANDS or stripped_normalized in _NEXT_COMMANDS:
            return "next_track", {}
        if normalized in _PREVIOUS_COMMANDS or stripped_normalized in _PREVIOUS_COMMANDS:
            return "previous_track", {}
        if normalized in _PAUSE_COMMANDS or stripped_normalized in _PAUSE_COMMANDS:
            return "pause", {}
        if normalized in _TOGGLE_COMMANDS or stripped_normalized in _TOGGLE_COMMANDS:
            return "play_pause", {}
        if normalized in _PLAY_COMMANDS or stripped_normalized in _PLAY_COMMANDS:
            return "play", {}

        prefix, query = self._extract_after_prefixes(command, _SEARCH_PREFIXES)
        if query:
            return "search_song", {"query": query, "matched_prefix": prefix}

        if re.search(r"\bon spotify\b", command, flags=re.IGNORECASE):
            plain_query = re.sub(r"\bon spotify\b", " ", stripped, flags=re.IGNORECASE)
            plain_query = self._normalize_command(plain_query)
            if plain_query.startswith("search "):
                return "search_song", {"query": plain_query[7:].strip()}

        prefix, query = self._extract_after_prefixes(command, _PLAY_QUERY_PREFIXES)
        if query:
            query_l = self._normalize_command(query)
            if query_l not in {"music", "spotify"} and not self._looks_like_control_phrase(query_l):
                return "play_query", {"query": query}

        if self._normalize_command(command) in {"what song", "what song is playing"}:
            return "read_current_track", {}

        return "unsupported", {}

    def _provider_for_name(self, provider_name: str):
        name = str(provider_name or "").strip().lower()
        return self._providers.get(name)

    def _from_action_result(self, intent: str, result: MediaActionResult) -> SkillExecutionResult:
        data = dict(result.data)
        if result.success and "target_app" not in data:
            data["target_app"] = data.get("provider") or self.detect_provider() or self._preferred_provider_name()
        if result.success:
            self._sync_state_from_data(data)
        return SkillExecutionResult(
            success=result.success,
            intent=intent,
            response=result.message,
            skill_name=self.name(),
            error=result.error,
            data=data,
        )

    def _metadata_payload(self, metadata: MediaMetadata, action: str) -> dict[str, Any]:
        payload = metadata.to_dict()
        payload["target_app"] = payload.get("provider") or "spotify"
        payload["action"] = action
        return payload

    def _sync_state_from_data(self, data: Mapping[str, Any]) -> None:
        provider = str(data.get("provider") or data.get("target_app") or "").strip().lower()
        if provider:
            state.music_active = True
            state.active_music_provider = provider
        track_name = str(data.get("track_name") or "").strip()
        artist_name = str(data.get("artist_name") or "").strip()
        if track_name:
            state.last_track_name = track_name
        if artist_name:
            state.last_artist_name = artist_name
        action = str(data.get("action") or "").strip()
        if action:
            state.last_media_action = action

    def _skill_failure(self, response: str, error: str) -> SkillExecutionResult:
        logger.error("Music skill failed: %s | %s", response, error)
        return SkillExecutionResult(
            success=False,
            intent="music_action",
            response=response,
            skill_name=self.name(),
            error=error,
        )

    def _failure(self, response: str, error: str) -> MediaActionResult:
        logger.error("Music action failed: %s | %s", response, error)
        return MediaActionResult(success=False, operation="music", message=response, error=error)

    def _preferred_provider_name(self) -> str:
        return str(settings.get("preferred_music_app") or "spotify").strip().lower()

    def _is_music_context(self, context: Mapping[str, Any]) -> bool:
        current_app = str(context.get("current_app") or context.get("current_context") or "").strip().lower()
        target_app = str(context.get("context_target_app") or "").strip().lower()
        process_name = str(context.get("current_process_name") or "").strip().lower()
        window_title = str(context.get("current_window_title") or "").strip().lower()
        active_provider = str(context.get("active_music_provider") or "").strip().lower()
        return (
            current_app in self._providers
            or target_app in self._providers
            or active_provider in self._providers
            or process_name in {"spotify.exe", "wmplayer.exe", "itunes.exe"}
            or "spotify" in window_title
        )

    def _has_recent_music_context(self, context: Mapping[str, Any]) -> bool:
        if str(context.get("last_skill_used") or "").strip() == self.name():
            return True
        if str(context.get("active_music_provider") or "").strip().lower() in self._providers:
            return True
        return bool(getattr(state, "music_active", False) or getattr(state, "last_track_name", ""))

    @staticmethod
    def _normalize_command(command: str) -> str:
        return " ".join(str(command or "").strip().lower().split())

    @staticmethod
    def _mentions_youtube(command: str) -> bool:
        return bool(re.search(r"\byoutube\b", str(command or ""), flags=re.IGNORECASE))

    @staticmethod
    def _mentions_music(command: str) -> bool:
        return bool(re.search(r"\b(?:spotify|music)\b", str(command or ""), flags=re.IGNORECASE))

    def _strip_explicit_music_tokens(self, command: str) -> str:
        text = str(command or "").strip()
        patterns = (
            r"^\s*spotify[\s:,-]+",
            r"^\s*music[\s:,-]+",
            r"\s+(?:on|in|using|with)\s+spotify\b",
            r"\s+(?:on|in|using|with)\s+music\b",
            r"\bspotify\b\s*$",
        )
        for pattern in patterns:
            text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
        return " ".join(text.split())

    @staticmethod
    def _extract_after_prefixes(text: str, prefixes: tuple[str, ...]) -> tuple[str, str]:
        source = str(text or "").strip()
        for prefix in prefixes:
            pattern = re.compile(rf"^\s*{re.escape(prefix)}", flags=re.IGNORECASE)
            if pattern.match(source):
                return prefix, pattern.sub("", source, count=1).strip()
        return "", ""

    @staticmethod
    def _looks_like_control_phrase(value: str) -> bool:
        normalized = " ".join(str(value or "").strip().lower().split())
        return normalized in (_PLAY_COMMANDS | _PAUSE_COMMANDS | _TOGGLE_COMMANDS | _NEXT_COMMANDS | _PREVIOUS_COMMANDS)
