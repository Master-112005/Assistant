"""
Spotify provider for the Phase 19 music skill.

The provider is verification-first:

* Playback state is read from Windows media sessions when available.
* Track metadata is read from media sessions, then Spotify UIA, then window title.
* Transport actions only report success when the resulting state can be verified.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import quote

from core import settings, state
from core.automation import DesktopAutomation, WindowTarget
from core.logger import get_logger
from core.media import MediaActionResult, MediaController, MediaMetadata, MediaWindowState, parse_track_label
from core.window_context import ActiveWindowDetector, WindowInfo

logger = get_logger(__name__)

try:  # pragma: no cover - optional dependency
    from pywinauto import Desktop

    _PYWINAUTO_OK = True
except Exception:  # pragma: no cover - optional dependency
    Desktop = None
    _PYWINAUTO_OK = False


_SPOTIFY_DESKTOP_PROCESSES = {"spotify.exe"}
_SPOTIFY_WEB_PROCESSES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}
_SPOTIFY_TITLE_HINTS = ("spotify", "spotify premium", "spotify free", "open.spotify.com")
_PLAY_BUTTON_HINTS = ("play", "resume", "start playback")
_PAUSE_BUTTON_HINTS = ("pause",)
_NEXT_BUTTON_HINTS = ("next", "skip forward")
_PREVIOUS_BUTTON_HINTS = ("previous", "skip back")
_SEARCH_BOX_HINTS = ("search", "find", "what do you want to play", "search input")
_SEARCH_SHORTCUTS = (["ctrl", "l"], ["ctrl", "k"])
_LIKED_SONGS_SHORTCUT = ["alt", "shift", "s"]
_ROW_NOISE = {
    "top result",
    "songs",
    "artists",
    "albums",
    "playlists",
    "podcasts & shows",
    "episodes",
    "audiobooks",
    "profiles",
    "genres & moods",
    "queue",
    "lyrics",
    "now playing view",
    "friend activity",
    "home",
    "search",
    "your library",
    "liked songs",
    "create playlist",
    "create your first playlist",
    "show all",
}
_FOOTER_NOISE = {
    "shuffle",
    "repeat",
    "queue",
    "lyrics",
    "devices available",
    "connect to a device",
    "picture-in-picture",
    "mini player",
    "play",
    "pause",
    "next",
    "previous",
    "skip forward",
    "skip back",
    "mute",
    "volume",
}
_BROWSER_NAMES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}


@dataclass
class SpotifySearchResult:
    title: str
    subtitle: str = ""
    index: int = 0
    source: str = "uia"
    rect: tuple[int, int, int, int] | None = None
    raw_text: str = ""
    control: Any = field(default=None, repr=False, compare=False)

    def label(self) -> str:
        if self.subtitle:
            return f"{self.title} - {self.subtitle}"
        return self.title

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "index": self.index,
            "source": self.source,
            "rect": self.rect,
            "raw_text": self.raw_text,
            "label": self.label(),
        }


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
    if not _PYWINAUTO_OK or Desktop is None:  # pragma: no cover - optional dependency
        return None
    return Desktop(backend="uia")


class SpotifyProvider:
    """Real Spotify Desktop/Web controller with extensible provider semantics."""

    def __init__(
        self,
        *,
        controller: MediaController | None = None,
        automation: DesktopAutomation | None = None,
        detector: ActiveWindowDetector | None = None,
        launcher=None,
        desktop_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._automation = automation or DesktopAutomation()
        self._detector = detector or ActiveWindowDetector()
        self._launcher = launcher
        self._controller = controller or MediaController(
            automation=self._automation,
            detector=self._detector,
            launcher=launcher,
        )
        self._desktop_factory = desktop_factory or _default_desktop_factory
        self._last_search_query: str = ""
        self._last_search_results: list[SpotifySearchResult] = []

    def detect(self) -> MediaWindowState | None:
        active = self._safe_active_context()
        if self._is_window_info_spotify(active):
            detected = self._window_state_from_info(active)
            self._record_runtime_state(detected, action="detect_context")
            return detected

        desktop_windows = self._automation.list_windows(process_names=sorted(_SPOTIFY_DESKTOP_PROCESSES))
        if desktop_windows:
            detected = self._window_state_from_target(desktop_windows[0], source="desktop")
            self._record_runtime_state(detected, action="detect_context")
            return detected

        web_windows = self._automation.list_windows(title_substrings=list(_SPOTIFY_TITLE_HINTS))
        for window in web_windows:
            process_name = str(window.process_name or "").strip().lower()
            if process_name in _SPOTIFY_WEB_PROCESSES:
                detected = self._window_state_from_target(window, source="web")
                self._record_runtime_state(detected, action="detect_context")
                return detected

        state.music_active = False
        return None

    def ensure_open(self) -> MediaWindowState | MediaActionResult:
        detected = self.detect()
        if detected is not None:
            return detected

        if not settings.get("auto_open_music_app_if_needed"):
            return self._failure("Spotify is not open.", "spotify_not_open")

        launch_error = self._launch_spotify()
        if launch_error is not None:
            return launch_error

        detected = self._wait_for_spotify_window(timeout=8.0)
        if detected is None:
            return self._failure("Spotify was launched, but no usable window was detected.", "spotify_window_not_found")
        return detected

    def focus(self) -> MediaWindowState | MediaActionResult:
        detected = self.ensure_open()
        if isinstance(detected, MediaActionResult):
            return detected

        focused = self._automation.focus_window(hwnd=detected.hwnd, timeout=float(settings.get("browser_focus_timeout") or 5))
        if not focused:
            return self._failure("Could not focus Spotify.", "spotify_focus_failed")

        refreshed = self._window_state_from_target(focused, source=detected.source)
        self._record_runtime_state(refreshed, action="focus")
        return refreshed

    def play_pause(self) -> MediaActionResult:
        return self._toggle_playback(operation="play_pause", desired_state=None)

    def play(self) -> MediaActionResult:
        return self._toggle_playback(operation="play", desired_state=True)

    def pause(self) -> MediaActionResult:
        return self._toggle_playback(operation="pause", desired_state=False)

    def next_track(self) -> MediaActionResult:
        return self._change_track("next_track", "Skipping to next track")

    def previous_track(self) -> MediaActionResult:
        return self._change_track("previous_track", "Going to previous track")

    def play_liked_songs(self) -> MediaActionResult:
        logger.info("Provider: Spotify")
        logger.info("Action: play_liked_songs")
        focused = self.focus()
        if isinstance(focused, MediaActionResult):
            return focused

        before = self.read_current_track()
        if not self._open_liked_songs_view(focused):
            return self._failure("I couldn't open Liked Songs in Spotify.", "liked_songs_navigation_failed")

        liked_view = self._wait_for_view(lambda item: self._is_liked_songs_view(item), hwnd=focused.hwnd, source=focused.source)
        if liked_view is None:
            return self._failure("Spotify did not show the Liked Songs view.", "liked_songs_view_not_visible")

        visible_rows = self._read_visible_track_rows(liked_view)
        if not self._click_content_play_button(liked_view):
            if not visible_rows or not self._click_search_result(visible_rows[0], double=True):
                return self._failure("I opened Liked Songs, but I could not start playback.", "liked_songs_play_button_unavailable")

        after = self._wait_for_track_change(before, timeout=4.0)
        if after is None:
            playing = self._wait_for_playback_state(True, timeout=3.0)
            if not playing:
                return self._failure("I opened Liked Songs, but I could not verify playback.", "liked_songs_playback_unverified")
            after = self.read_current_track()

        if after and visible_rows and not self._track_matches_any_result(after, visible_rows):
            if before and before.label() == after.label():
                return self._failure(
                    "Spotify stayed on the same track after opening Liked Songs, so I could not verify a library change.",
                    "liked_songs_unverified",
                )

        self._record_metadata(after, action="play_liked_songs")
        track_hint = after.label() if after else ""
        return self._success(
            operation="play_liked_songs",
            message="Playing your liked songs in Spotify" if not track_hint else f"Playing your liked songs in Spotify. Current track: {track_hint}",
            data=self._metadata_payload(after, action="play_liked_songs"),
        )

    def search_song(self, query: str) -> MediaActionResult:
        query_text = " ".join(str(query or "").split()).strip()
        if not query_text:
            return self._failure("Search query is empty.", "empty_query")

        logger.info("Provider: Spotify")
        logger.info("Search query: %s", query_text)
        focused = self.focus()
        if isinstance(focused, MediaActionResult):
            return focused

        if not self._show_search_results(focused, query_text):
            return self._failure(f"I couldn't search Spotify for {query_text}.", "spotify_search_failed")

        results = self._wait_for_search_results(focused.hwnd, focused.source, query=query_text)
        if not results:
            return self._failure(f"I opened Spotify search, but no visible results were found for {query_text}.", "spotify_search_results_unavailable")

        self._last_search_query = query_text
        self._last_search_results = results
        return self._success(
            operation="search_song",
            message=f"Found {len(results)} visible Spotify results for {query_text}.",
            data={
                "provider": "spotify",
                "query": query_text,
                "results": [item.to_dict() for item in results[:8]],
            },
        )

    def play_search_result(self, index: int = 1) -> MediaActionResult:
        result_index = max(1, int(index or 1))
        focused = self.focus()
        if isinstance(focused, MediaActionResult):
            return focused

        visible = self._read_visible_search_results(focused, query=self._last_search_query)
        if visible:
            self._last_search_results = visible

        if result_index > len(self._last_search_results):
            return self._failure("Spotify search results are not available for that selection.", "search_result_unavailable")

        selected = self._last_search_results[result_index - 1]
        before = self.read_current_track()
        if not self._click_search_result(selected, double=True):
            return self._failure(f"I found {selected.title}, but I could not activate it in Spotify.", "search_result_click_failed")

        after = self._wait_for_track_change(before, timeout=4.0, expected=selected)
        if after is None:
            return self._failure(
                f"I clicked {selected.title}, but I could not verify that Spotify changed to that track.",
                "track_change_unverified",
            )

        self._record_metadata(after, action="play_search_result")
        return self._success(
            operation="play_search_result",
            message=f"Playing {after.label() or selected.label()} in Spotify",
            data=self._metadata_payload(after, action="play_search_result", extra={"selection": selected.to_dict()}),
        )

    def set_volume_up(self) -> MediaActionResult:
        return self._send_unverified_media_action("volume_up", "Sent volume-up to Spotify.")

    def set_volume_down(self) -> MediaActionResult:
        return self._send_unverified_media_action("volume_down", "Sent volume-down to Spotify.")

    def mute(self) -> MediaActionResult:
        return self._send_unverified_media_action("mute", "Sent mute to Spotify.")

    def read_current_track(self) -> MediaMetadata | None:
        session_metadata = self._controller.read_media_session_metadata(source_hints=["spotify"])
        if session_metadata and session_metadata.label():
            detected = self.detect()
            if detected is not None:
                session_metadata.hwnd = detected.hwnd
                session_metadata.window_title = detected.title
                session_metadata.process_name = detected.process_name
            session_metadata.provider = "spotify"
            self._record_metadata(session_metadata, action="read_current_track")
            logger.info("Track: %s", session_metadata.label())
            return session_metadata

        detected = self.detect()
        if detected is None:
            return None

        ui_metadata = self._read_track_from_ui(detected)
        if ui_metadata and ui_metadata.label():
            self._record_metadata(ui_metadata, action="read_current_track")
            logger.info("Track: %s", ui_metadata.label())
            return ui_metadata

        title_metadata = self._read_track_from_title(detected)
        if title_metadata and title_metadata.label():
            self._record_metadata(title_metadata, action="read_current_track")
            logger.info("Track: %s", title_metadata.label())
            return title_metadata

        return None

    def is_playing(self) -> bool | None:
        session_metadata = self._controller.read_media_session_metadata(source_hints=["spotify"])
        if session_metadata is not None and session_metadata.is_playing is not None:
            if session_metadata.label():
                self._record_metadata(session_metadata, action="playback_state")
            return session_metadata.is_playing

        detected = self.detect()
        if detected is None:
            return None

        button = self._read_playback_button(detected)
        if button is None:
            return None
        lowered = button.name.lower()
        if any(hint in lowered for hint in _PAUSE_BUTTON_HINTS):
            return True
        if any(hint in lowered for hint in _PLAY_BUTTON_HINTS):
            return False
        return None

    def _toggle_playback(self, *, operation: str, desired_state: bool | None) -> MediaActionResult:
        logger.info("Provider: Spotify")
        logger.info("Action: %s", operation)
        focused = self.focus()
        if isinstance(focused, MediaActionResult):
            return focused

        before = self.is_playing()
        if desired_state is not None and before is desired_state:
            metadata = self.read_current_track()
            message = "Spotify is already playing." if desired_state else "Spotify is already paused."
            return self._success(operation=operation, message=message, data=self._metadata_payload(metadata, action=operation))

        sent = self._controller.send_media_action("play_pause" if desired_state is None else operation)
        if not sent and not self._click_playback_button(focused, desired_state=desired_state):
            return self._failure("Spotify playback controls were not available.", "spotify_playback_control_unavailable")

        expected_state = desired_state if desired_state is not None else (None if before is None else not before)
        if expected_state is None:
            after_state = self._wait_for_known_playback_state(timeout=3.0)
            if after_state is None:
                return self._failure("Spotify received the command, but playback state could not be verified.", "spotify_playback_unverified")
            expected_state = after_state
        elif not self._wait_for_playback_state(expected_state, timeout=3.0):
            return self._failure("Spotify did not reach the expected playback state.", "spotify_playback_state_unverified")

        metadata = self.read_current_track()
        verb = "Playing Spotify" if expected_state else "Paused Spotify"
        self._record_metadata(metadata, action=operation)
        return self._success(
            operation=operation,
            message=verb if not metadata or not metadata.label() else f"{verb}: {metadata.label()}",
            data=self._metadata_payload(metadata, action=operation),
        )

    def _change_track(self, operation: str, response: str) -> MediaActionResult:
        logger.info("Provider: Spotify")
        logger.info("Action: %s", operation)
        focused = self.focus()
        if isinstance(focused, MediaActionResult):
            return focused

        before = self.read_current_track()
        previous_label = (before.label() if before else "") or _state_track_label()

        sent = self._controller.send_media_action(operation)
        if not sent and not self._click_transport_button(focused, transport=operation):
            return self._failure("Spotify track controls were not available.", "spotify_track_control_unavailable")

        after = self._wait_for_track_change(before, timeout=4.5, previous_label=previous_label)
        if after is None:
            return self._failure(
                f"I sent the {operation.replace('_', ' ')} command to Spotify, but I could not verify a track change.",
                "track_change_unverified",
            )

        self._record_metadata(after, action=operation)
        logger.info("Track change verified: %s", after.label())
        return self._success(
            operation=operation,
            message=response if not after.label() else f"{response}. Now playing: {after.label()}",
            data=self._metadata_payload(after, action=operation),
        )

    def _send_unverified_media_action(self, action: str, message: str) -> MediaActionResult:
        focused = self.focus()
        if isinstance(focused, MediaActionResult):
            return focused

        if not self._controller.send_media_action(action):
            return self._failure(f"Spotify {action.replace('_', ' ')} control is unavailable.", f"{action}_unavailable")
        state.last_media_action = action
        return self._success(operation=action, message=message, data={"provider": "spotify", "target_app": "spotify"})

    def _show_search_results(self, window_state: MediaWindowState, query: str) -> bool:
        if window_state.source == "desktop" and self._open_spotify_uri(f"spotify:search:{quote(query)}"):
            self._automation.safe_sleep(700)
            return True

        if self._focus_search_box(window_state):
            self._automation.safe_sleep(100)
            return self._automation.type_text(query, clear=True)
        return False

    def _focus_search_box(self, window_state: MediaWindowState) -> bool:
        control = self._find_search_control(window_state)
        if control is not None and self._click_control(control, double=False):
            return True

        for shortcut in _SEARCH_SHORTCUTS:
            if self._automation.hotkey(shortcut):
                self._automation.safe_sleep(120)
                return True
        return False

    def _open_liked_songs_view(self, window_state: MediaWindowState) -> bool:
        if window_state.source == "desktop" and self._open_spotify_uri("spotify:collection:tracks"):
            self._automation.safe_sleep(700)
            return True

        liked_item = self._find_sidebar_item(window_state, "Liked Songs")
        if liked_item is not None and self._click_control(liked_item.control, rect=liked_item.rect):
            return True

        if self._automation.hotkey(_LIKED_SONGS_SHORTCUT):
            self._automation.safe_sleep(140)
            return True
        return False

    def _open_spotify_uri(self, uri: str) -> bool:
        try:
            if hasattr(os, "startfile"):
                os.startfile(uri)  # type: ignore[attr-defined]
                return True
        except Exception as exc:  # pragma: no cover - depends on host Spotify install
            logger.debug("Spotify URI open failed for %s: %s", uri, exc)
        return False

    def _launch_spotify(self) -> MediaActionResult | None:
        launcher = self._get_launcher()
        launch_errors: list[str] = []

        if launcher is not None:
            try:
                result = launcher.launch_by_name("spotify")
                if result.success:
                    self._automation.safe_sleep(900)
                    return None
                launch_errors.append(result.message or "launcher reported failure")
            except Exception as exc:
                launch_errors.append(str(exc))

        if self._open_spotify_uri("spotify:"):
            self._automation.safe_sleep(900)
            return None

        reason = "; ".join(part for part in launch_errors if part) or "launcher unavailable"
        return self._failure(f"Failed to launch Spotify: {reason}", "spotify_launch_failed")

    def _wait_for_spotify_window(self, timeout: float | None = None) -> MediaWindowState | None:
        deadline = time.monotonic() + float(timeout or settings.get("browser_focus_timeout") or 5.0)
        while time.monotonic() <= deadline:
            detected = self.detect()
            if detected is not None:
                return detected
            time.sleep(0.15)
        return None

    def _wait_for_search_results(self, hwnd: int, source: str, *, query: str, timeout: float = 4.0) -> list[SpotifySearchResult]:
        deadline = time.monotonic() + timeout
        latest: list[SpotifySearchResult] = []
        while time.monotonic() <= deadline:
            refreshed = self._refresh_window_state(hwnd, source=source)
            if refreshed is not None:
                latest = self._read_visible_search_results(refreshed, query=query)
                if latest:
                    return latest
            time.sleep(0.12)
        return latest

    def _wait_for_view(
        self,
        predicate: Callable[[MediaWindowState], bool],
        *,
        hwnd: int,
        source: str,
        timeout: float = 4.0,
    ) -> MediaWindowState | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() <= deadline:
            refreshed = self._refresh_window_state(hwnd, source=source)
            if refreshed is not None and predicate(refreshed):
                return refreshed
            time.sleep(0.12)
        return None

    def _wait_for_playback_state(self, expected: bool, *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() <= deadline:
            current = self.is_playing()
            if current is expected:
                return True
            time.sleep(0.12)
        return False

    def _wait_for_known_playback_state(self, *, timeout: float) -> bool | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() <= deadline:
            current = self.is_playing()
            if current is not None:
                return current
            time.sleep(0.12)
        return None

    def _wait_for_track_change(
        self,
        before: MediaMetadata | None,
        *,
        timeout: float,
        previous_label: str = "",
        expected: SpotifySearchResult | None = None,
    ) -> MediaMetadata | None:
        before_label = (before.label() if before else "") or previous_label
        deadline = time.monotonic() + timeout
        while time.monotonic() <= deadline:
            after = self.read_current_track()
            if after is None or not after.label():
                time.sleep(0.15)
                continue
            if expected is not None and self._metadata_matches_result(after, expected):
                return after
            if before_label and after.label() != before_label:
                return after
            time.sleep(0.15)
        return None

    def _read_track_from_ui(self, window_state: MediaWindowState) -> MediaMetadata | None:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return None

        records = self._collect_uia_records(window)
        footer_bounds = self._footer_left_bounds(window_state)
        candidates = [
            record
            for record in records
            if self._rect_overlaps(record.rect, footer_bounds)
            and record.control_type in {"text", "hyperlink", "button"}
            and record.name
            and _clean_label(record.name).lower() not in _FOOTER_NOISE
        ]
        if not candidates:
            return None

        candidates.sort(key=lambda item: (item.rect[1], item.rect[0], item.control_type))
        names = [_clean_label(item.name) for item in candidates if _clean_label(item.name)]
        names = _dedupe_preserve_order(names)
        if not names:
            return None

        track_name = names[0]
        artist_name = names[1] if len(names) > 1 else ""
        if not artist_name:
            parsed_track, parsed_artist = parse_track_label(track_name)
            track_name = parsed_track
            artist_name = parsed_artist

        return MediaMetadata(
            provider="spotify",
            track_name=track_name,
            artist_name=artist_name,
            is_playing=self._playback_state_from_records(records, window_state),
            source="uia_footer",
            hwnd=window_state.hwnd,
            process_name=window_state.process_name,
            window_title=window_state.title,
        )

    def _read_track_from_title(self, window_state: MediaWindowState) -> MediaMetadata | None:
        title = " ".join(str(window_state.title or "").split()).strip()
        if not title:
            return None

        lowered = title.lower()
        if lowered in {"spotify", "spotify premium", "spotify free"}:
            return None
        if lowered.endswith(" - spotify"):
            title = title[:-10].strip()
        track_name, artist_name = parse_track_label(title)
        if not track_name:
            return None

        return MediaMetadata(
            provider="spotify",
            track_name=track_name,
            artist_name=artist_name,
            source="window_title",
            hwnd=window_state.hwnd,
            process_name=window_state.process_name,
            window_title=window_state.title,
        )

    def _read_playback_button(self, window_state: MediaWindowState) -> _UIARecord | None:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return None
        records = self._collect_uia_records(window)
        footer_bounds = self._footer_controls_bounds(window_state)
        candidates: list[tuple[int, int, int, _UIARecord]] = []
        for record in records:
            if record.control_type != "button":
                continue
            if not self._rect_overlaps(record.rect, footer_bounds):
                continue
            name_l = record.name.lower()
            score = 0
            if any(hint in name_l for hint in _PLAY_BUTTON_HINTS + _PAUSE_BUTTON_HINTS):
                score += 5
            if record.width >= 26 and record.height >= 26:
                score += 1
            candidates.append((-score, abs(_rect_center_x(record.rect) - _rect_center_x(footer_bounds)), record.rect[1], record))
        if not candidates:
            return None
        return sorted(candidates)[0][3]

    def _click_playback_button(self, window_state: MediaWindowState, *, desired_state: bool | None) -> bool:
        button = self._read_playback_button(window_state)
        if button is None:
            return False
        name_l = button.name.lower()
        if desired_state is True and not any(hint in name_l for hint in _PLAY_BUTTON_HINTS):
            return False
        if desired_state is False and not any(hint in name_l for hint in _PAUSE_BUTTON_HINTS):
            return False
        return self._click_control(button.control, rect=button.rect)

    def _click_transport_button(self, window_state: MediaWindowState, *, transport: str) -> bool:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return False
        records = self._collect_uia_records(window)
        footer_bounds = self._footer_controls_bounds(window_state)
        hints = _NEXT_BUTTON_HINTS if transport == "next_track" else _PREVIOUS_BUTTON_HINTS
        candidates: list[tuple[int, int, int, _UIARecord]] = []
        for record in records:
            if record.control_type != "button" or not self._rect_overlaps(record.rect, footer_bounds):
                continue
            name_l = record.name.lower()
            score = 0
            if any(hint in name_l for hint in hints):
                score += 5
            if record.width >= 18 and record.height >= 18:
                score += 1
            candidates.append((-score, abs(_rect_center_x(record.rect) - _rect_center_x(footer_bounds)), record.rect[1], record))
        if not candidates:
            return False
        target = sorted(candidates)[0][3]
        return self._click_control(target.control, rect=target.rect)

    def _click_content_play_button(self, window_state: MediaWindowState) -> bool:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return False
        records = self._collect_uia_records(window)
        bounds = self._content_header_bounds(window_state)
        candidates: list[tuple[int, int, int, _UIARecord]] = []
        for record in records:
            if record.control_type != "button":
                continue
            if not self._rect_overlaps(record.rect, bounds):
                continue
            name_l = record.name.lower()
            score = 0
            if any(hint in name_l for hint in _PLAY_BUTTON_HINTS):
                score += 5
            if "shuffle" in name_l:
                score -= 2
            if record.width >= 28 and record.height >= 28:
                score += 1
            candidates.append((-score, record.rect[1], record.rect[0], record))
        if not candidates:
            return False
        return self._click_control(sorted(candidates)[0][3].control, rect=sorted(candidates)[0][3].rect)

    def _read_visible_search_results(self, window_state: MediaWindowState, *, query: str = "") -> list[SpotifySearchResult]:
        return self._read_visible_track_rows(window_state, query=query)

    def _read_visible_track_rows(self, window_state: MediaWindowState, *, query: str = "") -> list[SpotifySearchResult]:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return []

        records = self._collect_uia_records(window)
        bounds = self._results_bounds(window_state)
        filtered = [
            record
            for record in records
            if self._rect_overlaps(record.rect, bounds)
            and record.control_type in {"text", "hyperlink", "button", "listitem", "dataitem", "group", "pane"}
            and record.name
        ]
        if not filtered:
            return []

        rows = self._group_row_records(filtered)
        query_tokens = _query_tokens(query)
        results: list[tuple[int, int, SpotifySearchResult]] = []
        seen: set[str] = set()
        for row_y, row_records in rows:
            texts = [_clean_label(record.name) for record in row_records if _clean_label(record.name)]
            texts = [text for text in _dedupe_preserve_order(texts) if text.lower() not in _ROW_NOISE and not _looks_like_duration(text)]
            if not texts:
                continue

            title = texts[0]
            subtitle = ", ".join(texts[1:3]) if len(texts) > 1 else ""
            label_l = f"{title} {subtitle}".strip().lower()
            if label_l in seen:
                continue

            merged_rect = self._merge_rects(row_records)
            clickable = next((item.control for item in row_records if item.control_type in {"hyperlink", "button", "listitem", "dataitem"}), row_records[0].control)
            score = 0
            if query_tokens:
                score += sum(1 for token in query_tokens if token in label_l)
            if title.lower() not in _ROW_NOISE:
                score += 1
            seen.add(label_l)
            results.append(
                (
                    -score,
                    row_y,
                    SpotifySearchResult(
                        title=title,
                        subtitle=subtitle,
                        source="uia",
                        rect=merged_rect,
                        raw_text=" | ".join(texts),
                        control=clickable,
                    ),
                )
            )

        ordered = [item[2] for item in sorted(results)]
        for index, item in enumerate(ordered, 1):
            item.index = index
        return ordered[:12]

    def _click_search_result(self, result: SpotifySearchResult, *, double: bool) -> bool:
        return self._click_control(result.control, rect=result.rect, double=double)

    def _metadata_matches_result(self, metadata: MediaMetadata, result: SpotifySearchResult) -> bool:
        track_l = metadata.track_name.lower()
        artist_l = metadata.artist_name.lower()
        title_l = result.title.lower()
        subtitle_l = result.subtitle.lower()
        if track_l and title_l and (track_l == title_l or title_l in track_l or track_l in title_l):
            if not artist_l or not subtitle_l:
                return True
            return subtitle_l in artist_l or artist_l in subtitle_l
        label_tokens = _query_tokens(result.label())
        metadata_l = f"{track_l} {artist_l}".strip()
        return bool(label_tokens and all(token in metadata_l for token in list(label_tokens)[:2]))

    def _track_matches_any_result(self, metadata: MediaMetadata, results: Sequence[SpotifySearchResult]) -> bool:
        return any(self._metadata_matches_result(metadata, item) for item in results[:8])

    def _is_liked_songs_view(self, window_state: MediaWindowState) -> bool:
        title = str(window_state.title or "").lower()
        if "liked songs" in title:
            return True

        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return False
        records = self._collect_uia_records(window)
        bounds = self._content_header_bounds(window_state)
        return any(self._rect_overlaps(record.rect, bounds) and record.name.lower() == "liked songs" for record in records)

    def _find_sidebar_item(self, window_state: MediaWindowState, label: str) -> _UIARecord | None:
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return None
        bounds = self._sidebar_bounds(window_state)
        label_l = label.lower()
        candidates: list[tuple[int, int, int, _UIARecord]] = []
        for record in self._collect_uia_records(window):
            if not self._rect_overlaps(record.rect, bounds):
                continue
            if record.control_type not in {"button", "text", "hyperlink", "listitem", "dataitem"}:
                continue
            name_l = record.name.lower()
            if label_l not in name_l:
                continue
            score = 0
            if name_l == label_l:
                score += 3
            if record.width > 80:
                score += 1
            candidates.append((-score, record.rect[1], record.rect[0], record))
        return sorted(candidates)[0][3] if candidates else None

    def _find_search_control(self, window_state: MediaWindowState):
        window = self._open_uia_window(window_state.hwnd)
        if window is None:
            return None
        bounds = self._search_bounds(window_state)
        candidates: list[tuple[int, int, int, Any]] = []
        for record in self._collect_uia_records(window):
            if not self._rect_overlaps(record.rect, bounds):
                continue
            if record.control_type not in {"edit", "document", "combobox", "pane", "group"}:
                continue
            lowered = record.name.lower()
            score = 0
            if any(hint in lowered for hint in _SEARCH_BOX_HINTS):
                score += 5
            if record.control_type == "edit":
                score += 2
            if record.width >= int((bounds[2] - bounds[0]) * 0.4):
                score += 1
            candidates.append((-score, record.rect[1], record.rect[0], record.control))
        return sorted(candidates)[0][3] if candidates else None

    def _open_uia_window(self, hwnd: int):
        if not hwnd or not _PYWINAUTO_OK or Desktop is None:  # pragma: no cover - optional dependency
            return None
        try:
            desktop = self._desktop_factory()
            if desktop is None:
                return None
            return desktop.window(handle=hwnd)
        except Exception as exc:  # pragma: no cover - depends on live UIA tree
            logger.debug("Spotify UIA window open failed: %s", exc)
            return None

    def _collect_uia_records(self, window: Any) -> list[_UIARecord]:
        records: list[_UIARecord] = []
        try:
            descendants = window.descendants()
        except Exception as exc:  # pragma: no cover - depends on live UIA tree
            logger.debug("Spotify UIA descendants failed: %s", exc)
            return records

        for control in descendants:
            try:
                element = control.element_info
                rect = self._rect_from_object(getattr(element, "rectangle", None))
                if not rect:
                    continue
                records.append(
                    _UIARecord(
                        control=control,
                        name=str(getattr(element, "name", "") or "").strip(),
                        control_type=str(getattr(element, "control_type", "") or "").lower(),
                        rect=rect,
                    )
                )
            except Exception:
                continue
        return records

    def _click_control(
        self,
        control: Any,
        *,
        rect: tuple[int, int, int, int] | None = None,
        double: bool = False,
    ) -> bool:
        for method_name in ("invoke", "click_input", "select"):
            method = getattr(control, method_name, None)
            if callable(method):
                try:
                    if method_name == "click_input" and double:
                        method(double=True)
                    else:
                        method()
                    self._automation.safe_sleep(100)
                    return True
                except Exception:
                    continue

        if rect is not None:
            clicked = self._automation.click_center(rect) if not double else self._click_rect_double(rect)
            if clicked:
                self._automation.safe_sleep(100)
                return True
        return False

    def _click_rect_double(self, rect: tuple[int, int, int, int]) -> bool:
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            return False
        center_x = left + (right - left) // 2
        center_y = top + (bottom - top) // 2
        return self._automation.click_point(center_x, center_y, double=True)

    def _refresh_window_state(self, hwnd: int, *, source: str) -> MediaWindowState | None:
        target = self._automation.get_window(hwnd)
        if target:
            return self._window_state_from_target(target, source=source)

        try:
            info = self._detector.get_window_info(hwnd)
        except Exception:
            return None
        if not self._is_window_info_spotify(info):
            return None
        return self._window_state_from_info(info, forced_source=source)

    def _safe_active_context(self) -> WindowInfo | None:
        try:
            return self._detector.get_active_context()
        except Exception as exc:  # pragma: no cover - defensive OS access
            logger.debug("Spotify active context detection failed: %s", exc)
            return None

    def _window_state_from_info(self, info: WindowInfo, *, forced_source: str | None = None) -> MediaWindowState:
        source = forced_source or ("desktop" if info.process_name.lower() in _SPOTIFY_DESKTOP_PROCESSES else "web")
        return MediaWindowState(
            provider="spotify",
            hwnd=info.hwnd,
            title=info.title,
            process_name=info.process_name,
            rect=info.rect,
            source=source,
            is_foreground=self._automation.get_foreground_window() == info.hwnd,
            is_minimized=info.is_minimized,
        )

    def _window_state_from_target(self, target: WindowTarget, *, source: str) -> MediaWindowState:
        return MediaWindowState(
            provider="spotify",
            hwnd=target.hwnd,
            title=target.title,
            process_name=target.process_name,
            rect=target.rect,
            source=source,
            is_foreground=self._automation.get_foreground_window() == target.hwnd,
            is_minimized=target.is_minimized,
        )

    def _record_runtime_state(self, window_state: MediaWindowState | None, *, action: str) -> None:
        state.music_active = window_state is not None
        state.active_music_provider = "spotify" if window_state is not None else ""
        if window_state is None:
            return
        state.current_context = "spotify"
        state.current_app = "spotify"
        state.current_process_name = window_state.process_name
        state.current_window_title = window_state.title
        if action and action not in {"detect_context", "focus"}:
            state.last_media_action = action
            state.last_successful_action = f"{action}:spotify"

    def _record_metadata(self, metadata: MediaMetadata | None, *, action: str) -> None:
        if metadata is None:
            return
        state.music_active = True
        state.active_music_provider = "spotify"
        if metadata.track_name:
            state.last_track_name = metadata.track_name
        if metadata.artist_name:
            state.last_artist_name = metadata.artist_name
        if action:
            state.last_media_action = action

    def _is_window_info_spotify(self, info: WindowInfo | None) -> bool:
        if info is None:
            return False
        process_name = str(info.process_name or "").strip().lower()
        title = str(info.title or "").strip().lower()
        if process_name in _SPOTIFY_DESKTOP_PROCESSES:
            return True
        if info.app_id == "spotify":
            return True
        return process_name in _SPOTIFY_WEB_PROCESSES and any(hint in title for hint in _SPOTIFY_TITLE_HINTS)

    def _metadata_payload(
        self,
        metadata: MediaMetadata | None,
        *,
        action: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": "spotify",
            "target_app": "spotify",
            "action": action,
        }
        if metadata is not None:
            payload.update(metadata.to_dict())
        if extra:
            payload.update(extra)
        return payload

    def _success(self, *, operation: str, message: str, data: dict[str, Any] | None = None) -> MediaActionResult:
        return MediaActionResult(
            success=True,
            operation=operation,
            message=message,
            data=data or {"provider": "spotify", "target_app": "spotify"},
        )

    def _failure(self, response: str, error: str) -> MediaActionResult:
        logger.error("Spotify action failed: %s | %s", response, error)
        return MediaActionResult(success=False, operation="spotify", message=response, error=error)

    def _get_launcher(self):
        if self._launcher is not None:
            return self._launcher
        return self._controller.get_launcher()

    def _sidebar_bounds(self, window_state: MediaWindowState) -> tuple[int, int, int, int]:
        sidebar_width = int(window_state.width * (0.26 if window_state.source == "desktop" else 0.22))
        return (
            window_state.rect[0],
            window_state.rect[1] + int(window_state.height * 0.08),
            window_state.rect[0] + sidebar_width,
            window_state.rect[3] - int(window_state.height * 0.14),
        )

    def _search_bounds(self, window_state: MediaWindowState) -> tuple[int, int, int, int]:
        sidebar = self._sidebar_bounds(window_state)
        return (
            sidebar[0] + 10,
            sidebar[1],
            sidebar[2] - 10,
            sidebar[1] + max(90, int(window_state.height * 0.12)),
        )

    def _content_header_bounds(self, window_state: MediaWindowState) -> tuple[int, int, int, int]:
        sidebar = self._sidebar_bounds(window_state)
        return (
            sidebar[2] + 8,
            window_state.rect[1] + int(window_state.height * 0.08),
            window_state.rect[2] - 12,
            window_state.rect[1] + int(window_state.height * 0.28),
        )

    def _results_bounds(self, window_state: MediaWindowState) -> tuple[int, int, int, int]:
        header = self._content_header_bounds(window_state)
        footer_top = window_state.rect[3] - max(120, int(window_state.height * 0.16))
        return (
            header[0],
            header[3],
            window_state.rect[2] - 12,
            footer_top,
        )

    def _footer_left_bounds(self, window_state: MediaWindowState) -> tuple[int, int, int, int]:
        footer_height = max(112, int(window_state.height * 0.16))
        top = window_state.rect[3] - footer_height
        return (
            window_state.rect[0] + 8,
            top,
            window_state.rect[0] + int(window_state.width * 0.42),
            window_state.rect[3] - 4,
        )

    def _footer_controls_bounds(self, window_state: MediaWindowState) -> tuple[int, int, int, int]:
        footer_height = max(112, int(window_state.height * 0.16))
        top = window_state.rect[3] - footer_height
        return (
            window_state.rect[0] + int(window_state.width * 0.32),
            top,
            window_state.rect[0] + int(window_state.width * 0.68),
            window_state.rect[3] - 4,
        )

    def _playback_state_from_records(self, records: Sequence[_UIARecord], window_state: MediaWindowState) -> bool | None:
        footer_bounds = self._footer_controls_bounds(window_state)
        for record in records:
            if record.control_type != "button" or not self._rect_overlaps(record.rect, footer_bounds):
                continue
            lowered = record.name.lower()
            if any(hint in lowered for hint in _PAUSE_BUTTON_HINTS):
                return True
            if any(hint in lowered for hint in _PLAY_BUTTON_HINTS):
                return False
        return None

    def _group_row_records(self, records: Sequence[_UIARecord]) -> list[tuple[int, list[_UIARecord]]]:
        rows: list[tuple[int, list[_UIARecord]]] = []
        for record in sorted(records, key=lambda item: (item.rect[1], item.rect[0], item.control_type)):
            row_y = record.rect[1]
            if rows and abs(row_y - rows[-1][0]) <= 18:
                rows[-1][1].append(record)
            else:
                rows.append((row_y, [record]))
        return rows

    @staticmethod
    def _merge_rects(records: Sequence[_UIARecord]) -> tuple[int, int, int, int]:
        left = min(record.rect[0] for record in records)
        top = min(record.rect[1] for record in records)
        right = max(record.rect[2] for record in records)
        bottom = max(record.rect[3] for record in records)
        return (left, top, right, bottom)

    @staticmethod
    def _rect_from_object(value: Any) -> tuple[int, int, int, int] | None:
        if value is None:
            return None
        for attr_names in (("left", "top", "right", "bottom"), ("Left", "Top", "Right", "Bottom")):
            try:
                rect = tuple(int(getattr(value, attr)) for attr in attr_names)
            except Exception:
                rect = ()
            if len(rect) == 4 and rect[2] > rect[0] and rect[3] > rect[1]:
                return rect  # type: ignore[return-value]
        return None

    @staticmethod
    def _rect_overlaps(rect_a: tuple[int, int, int, int], rect_b: tuple[int, int, int, int]) -> bool:
        return not (
            rect_a[2] <= rect_b[0]
            or rect_a[0] >= rect_b[2]
            or rect_a[3] <= rect_b[1]
            or rect_a[1] >= rect_b[3]
        )


def _state_track_label() -> str:
    if state.last_track_name and state.last_artist_name:
        return f"{state.last_track_name} - {state.last_artist_name}"
    return state.last_track_name or state.last_artist_name or ""


def _query_tokens(query: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", str(query or "").lower()) if len(token) >= 2}


def _clean_label(value: str) -> str:
    cleaned = " ".join(str(value or "").split()).strip()
    cleaned = re.sub(r"\s*[•|]\s*", " - ", cleaned)
    return cleaned.strip(" -")


def _looks_like_duration(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}:\d{2}", str(value or "").strip()))


def _dedupe_preserve_order(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


def _rect_center_x(rect: tuple[int, int, int, int]) -> int:
    return rect[0] + (rect[2] - rect[0]) // 2
