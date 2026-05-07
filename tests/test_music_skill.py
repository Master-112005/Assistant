from __future__ import annotations

import pytest

from core import settings, state
from core.media import MediaActionResult, MediaMetadata
from skills.music import MusicSkill


def _track(name: str, artist: str) -> MediaMetadata:
    return MediaMetadata(provider="spotify", track_name=name, artist_name=artist, is_playing=True, source="test")


class FakeSpotifyProvider:
    def __init__(self) -> None:
        self.detected = True
        self.open_error = ""
        self.playing = False
        self.calls: list[tuple[str, object]] = []
        self.search_queries: list[str] = []
        self.search_catalog: list[MediaMetadata] = [
            _track("Believer", "Imagine Dragons"),
            _track("Shape of You", "Ed Sheeran"),
            _track("Numb", "Linkin Park"),
        ]
        self.current_track: MediaMetadata | None = None
        self.queue: list[MediaMetadata] = [
            _track("Believer", "Imagine Dragons"),
            _track("Thunder", "Imagine Dragons"),
            _track("Demons", "Imagine Dragons"),
        ]
        self.queue_index = 0

    def detect(self):
        return object() if self.detected else None

    def play_pause(self):
        self.calls.append(("play_pause", None))
        if self.open_error:
            return MediaActionResult(False, "play_pause", self.open_error, error="spotify_not_open")
        self.playing = not self.playing
        if self.playing and self.current_track is None:
            self.current_track = self.queue[self.queue_index]
        return MediaActionResult(True, "play_pause", "Toggled Spotify", data=self._payload("play_pause"))

    def play(self):
        self.calls.append(("play", None))
        if self.open_error:
            return MediaActionResult(False, "play", self.open_error, error="spotify_not_open")
        self.playing = True
        if self.current_track is None:
            self.current_track = self.queue[self.queue_index]
        return MediaActionResult(True, "play", f"Playing Spotify: {self.current_track.label()}", data=self._payload("play"))

    def pause(self):
        self.calls.append(("pause", None))
        if self.open_error:
            return MediaActionResult(False, "pause", self.open_error, error="spotify_not_open")
        self.playing = False
        return MediaActionResult(True, "pause", "Paused Spotify", data=self._payload("pause"))

    def next_track(self):
        self.calls.append(("next_track", None))
        if self.open_error:
            return MediaActionResult(False, "next_track", self.open_error, error="spotify_not_open")
        self.playing = True
        self.queue_index = min(len(self.queue) - 1, self.queue_index + 1)
        self.current_track = self.queue[self.queue_index]
        return MediaActionResult(True, "next_track", f"Skipping to next track. Now playing: {self.current_track.label()}", data=self._payload("next_track"))

    def previous_track(self):
        self.calls.append(("previous_track", None))
        if self.open_error:
            return MediaActionResult(False, "previous_track", self.open_error, error="spotify_not_open")
        self.playing = True
        self.queue_index = max(0, self.queue_index - 1)
        self.current_track = self.queue[self.queue_index]
        return MediaActionResult(True, "previous_track", f"Going to previous track. Now playing: {self.current_track.label()}", data=self._payload("previous_track"))

    def play_liked_songs(self):
        self.calls.append(("play_liked_songs", None))
        if self.open_error:
            return MediaActionResult(False, "play_liked_songs", self.open_error, error="spotify_not_open")
        self.playing = True
        self.current_track = self.queue[0]
        self.queue_index = 0
        return MediaActionResult(True, "play_liked_songs", "Playing your liked songs in Spotify", data=self._payload("play_liked_songs"))

    def search_song(self, query: str):
        self.calls.append(("search_song", query))
        if self.open_error:
            return MediaActionResult(False, "search_song", self.open_error, error="spotify_not_open")
        self.search_queries.append(query)
        return MediaActionResult(
            True,
            "search_song",
            f"Found {len(self.search_catalog)} visible Spotify results for {query}.",
            data={
                "provider": "spotify",
                "target_app": "spotify",
                "query": query,
                "results": [self._track_to_dict(item) for item in self.search_catalog],
            },
        )

    def play_search_result(self, index: int = 1):
        self.calls.append(("play_search_result", index))
        if self.open_error:
            return MediaActionResult(False, "play_search_result", self.open_error, error="spotify_not_open")
        self.playing = True
        self.current_track = self.search_catalog[index - 1]
        return MediaActionResult(True, "play_search_result", f"Playing {self.current_track.label()} in Spotify", data=self._payload("play_search_result"))

    def set_volume_up(self):
        self.calls.append(("volume_up", None))
        return MediaActionResult(True, "volume_up", "Sent volume-up to Spotify.", data={"provider": "spotify", "target_app": "spotify"})

    def set_volume_down(self):
        self.calls.append(("volume_down", None))
        return MediaActionResult(True, "volume_down", "Sent volume-down to Spotify.", data={"provider": "spotify", "target_app": "spotify"})

    def mute(self):
        self.calls.append(("mute", None))
        return MediaActionResult(True, "mute", "Sent mute to Spotify.", data={"provider": "spotify", "target_app": "spotify"})

    def read_current_track(self):
        self.calls.append(("read_current_track", None))
        return self.current_track

    def is_playing(self):
        self.calls.append(("is_playing", None))
        return self.playing

    def _payload(self, action: str) -> dict[str, object]:
        payload: dict[str, object] = {"provider": "spotify", "target_app": "spotify", "action": action}
        if self.current_track is not None:
            payload.update(self._track_to_dict(self.current_track))
        return payload

    @staticmethod
    def _track_to_dict(track: MediaMetadata) -> dict[str, object]:
        return {
            "provider": "spotify",
            "track_name": track.track_name,
            "artist_name": track.artist_name,
            "label": track.label(),
        }


@pytest.fixture(autouse=True)
def reset_music_state():
    settings.reset_defaults()
    state.music_active = False
    state.active_music_provider = ""
    state.last_track_name = ""
    state.last_artist_name = ""
    state.last_media_action = ""
    state.last_skill_used = ""
    yield
    settings.reset_defaults()


def test_can_handle_spotify_context_routes_pause_command():
    provider = FakeSpotifyProvider()
    skill = MusicSkill(providers={"spotify": provider})

    can_handle = skill.can_handle(
        {
            "current_app": "spotify",
            "current_process_name": "spotify.exe",
            "current_window_title": "Spotify Premium",
            "active_music_provider": "spotify",
        },
        "unknown",
        "pause",
    )

    assert can_handle is True


def test_can_handle_play_query_when_spotify_is_preferred():
    provider = FakeSpotifyProvider()
    skill = MusicSkill(providers={"spotify": provider})

    can_handle = skill.can_handle({"current_app": "unknown", "current_process_name": ""}, "unknown", "play believer")

    assert can_handle is True


def test_play_command_routes_to_provider():
    provider = FakeSpotifyProvider()
    skill = MusicSkill(providers={"spotify": provider})

    result = skill.execute("play", context={"current_app": "spotify"})

    assert result.success is True
    assert result.intent == "music_action"
    assert "Playing Spotify" in result.response
    assert provider.calls[0][0] == "play"


def test_pause_command_routes_to_provider():
    provider = FakeSpotifyProvider()
    provider.playing = True
    provider.current_track = provider.queue[0]
    skill = MusicSkill(providers={"spotify": provider})

    result = skill.execute("pause music", context={"current_app": "spotify"})

    assert result.success is True
    assert result.response == "Paused Spotify"
    assert provider.calls[0][0] == "pause"


def test_next_track_updates_runtime_track_state():
    provider = FakeSpotifyProvider()
    provider.current_track = provider.queue[0]
    provider.playing = True
    skill = MusicSkill(providers={"spotify": provider})

    result = skill.execute("next track", context={"current_app": "spotify"})

    assert result.success is True
    assert result.intent == "music_action"
    assert state.last_track_name == "Thunder"
    assert state.last_artist_name == "Imagine Dragons"


def test_previous_track_updates_runtime_track_state():
    provider = FakeSpotifyProvider()
    provider.queue_index = 2
    provider.current_track = provider.queue[2]
    provider.playing = True
    skill = MusicSkill(providers={"spotify": provider})

    result = skill.execute("previous track", context={"current_app": "spotify"})

    assert result.success is True
    assert "Believer" not in result.response
    assert state.last_track_name == "Thunder"


def test_play_liked_songs_routes_to_provider():
    provider = FakeSpotifyProvider()
    skill = MusicSkill(providers={"spotify": provider})

    result = skill.execute("play liked songs", context={"current_app": "unknown"})

    assert result.success is True
    assert result.intent == "music_play_liked"
    assert result.response == "Playing your liked songs in Spotify"
    assert provider.calls[0][0] == "play_liked_songs"


def test_search_song_returns_real_query_result():
    provider = FakeSpotifyProvider()
    skill = MusicSkill(providers={"spotify": provider})

    result = skill.execute("search song shape of you", context={"current_app": "unknown"})

    assert result.success is True
    assert result.intent == "music_search"
    assert provider.search_queries == ["shape of you"]


def test_play_query_searches_then_plays_first_result():
    provider = FakeSpotifyProvider()
    skill = MusicSkill(providers={"spotify": provider})

    result = skill.execute("play believer", context={"current_app": "unknown"})

    assert result.success is True
    assert result.intent == "music_play_track"
    assert provider.search_queries == ["believer"]
    assert provider.current_track is not None
    assert provider.current_track.track_name == "Believer"


def test_read_current_track_returns_detected_track():
    provider = FakeSpotifyProvider()
    provider.current_track = _track("Shape of You", "Ed Sheeran")
    provider.playing = True
    skill = MusicSkill(providers={"spotify": provider})

    result = skill.execute("what song is this", context={"current_app": "spotify"})

    assert result.success is True
    assert result.intent == "music_current_track"
    assert result.response == "Currently playing: Shape of You - Ed Sheeran"


def test_spotify_closed_is_handled_honestly():
    provider = FakeSpotifyProvider()
    provider.open_error = "Spotify is not open."
    skill = MusicSkill(providers={"spotify": provider})

    result = skill.execute("play liked songs", context={"current_app": "unknown"})

    assert result.success is False
    assert result.response == "Spotify is not open."
    assert result.error == "spotify_not_open"


def test_repeated_transport_commands_remain_stable():
    provider = FakeSpotifyProvider()
    provider.current_track = provider.queue[0]
    provider.playing = True
    skill = MusicSkill(providers={"spotify": provider})

    first = skill.execute("next track", context={"current_app": "spotify"})
    second = skill.execute("next track", context={"current_app": "spotify"})

    assert first.success is True
    assert second.success is True
    assert provider.current_track is not None
    assert provider.current_track.track_name == "Demons"
