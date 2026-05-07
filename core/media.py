"""
Shared media-control primitives and metadata models.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from core import settings
from core.automation import DesktopAutomation, WindowTarget
from core.logger import get_logger
from core.window_context import ActiveWindowDetector, WindowInfo

logger = get_logger(__name__)

_MEDIA_SESSION_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.ToString() -eq 'System.Threading.Tasks.Task`1[TResult] AsTask[TResult](Windows.Foundation.IAsyncOperation`1[TResult])'
  } | Select-Object -First 1
  if (-not $asTask) {
    throw 'Windows Runtime task bridge is unavailable.'
  }

  $managerType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
  $manager = $asTask.MakeGenericMethod($managerType).Invoke($null, @($managerType::RequestAsync())).GetAwaiter().GetResult()
  $sessionType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSession, Windows.Media.Control, ContentType=WindowsRuntime]
  $propsType = $sessionType.GetMethod('TryGetMediaPropertiesAsync').ReturnType.GenericTypeArguments[0]
  $currentSession = $manager.GetCurrentSession()
  $currentSource = if ($currentSession) { [string]$currentSession.SourceAppUserModelId } else { '' }
  $sessions = @()

  foreach ($session in $manager.GetSessions()) {
    $props = $null
    try {
      $props = $asTask.MakeGenericMethod($propsType).Invoke($null, @($session.TryGetMediaPropertiesAsync())).GetAwaiter().GetResult()
    } catch {}
    $info = $session.GetPlaybackInfo()
    $timeline = $session.GetTimelineProperties()
    $sessions += [ordered]@{
      sourceApp = [string]$session.SourceAppUserModelId
      status = [string]$info.PlaybackStatus
      title = if ($props) { [string]$props.Title } else { '' }
      artist = if ($props) { [string]$props.Artist } else { '' }
      album = if ($props) { [string]$props.AlbumTitle } else { '' }
      isCurrent = [bool]($currentSource -and [string]$session.SourceAppUserModelId -eq $currentSource)
      positionTicks = if ($timeline) { [int64]$timeline.Position.Ticks } else { 0 }
    }
  }

  [ordered]@{
    ok = $true
    sessions = $sessions
  } | ConvertTo-Json -Compress -Depth 4
} catch {
  [ordered]@{
    ok = $false
    error = $_.Exception.Message
  } | ConvertTo-Json -Compress -Depth 4
  exit 1
}
"""


@dataclass
class MediaMetadata:
    provider: str
    track_name: str = ""
    artist_name: str = ""
    album_name: str = ""
    is_playing: bool | None = None
    source: str = ""
    hwnd: int = 0
    process_name: str = ""
    window_title: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        if self.track_name and self.artist_name:
            return f"{self.track_name} - {self.artist_name}"
        return self.track_name or self.artist_name or ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "track_name": self.track_name,
            "artist_name": self.artist_name,
            "album_name": self.album_name,
            "is_playing": self.is_playing,
            "source": self.source,
            "hwnd": self.hwnd,
            "process_name": self.process_name,
            "window_title": self.window_title,
            "label": self.label(),
            "data": dict(self.data),
        }


@dataclass
class MediaActionResult:
    success: bool
    operation: str
    message: str
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class MediaWindowState:
    provider: str
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


class MediaController:
    """Reusable media helper for window targeting and media-key dispatch."""

    _ACTION_TO_KEY = {
        "play_pause": "media play pause",
        "play": "media play pause",
        "pause": "media play pause",
        "resume": "media play pause",
        "next_track": "media next",
        "previous_track": "media previous",
        "mute": "media mute",
        "volume_up": "media volume up",
        "volume_down": "media volume down",
    }

    def __init__(
        self,
        *,
        automation: DesktopAutomation | None = None,
        detector: ActiveWindowDetector | None = None,
        launcher=None,
    ) -> None:
        self._automation = automation or DesktopAutomation()
        self._detector = detector or ActiveWindowDetector()
        self._launcher = launcher

    def send_media_action(self, action: str) -> bool:
        if not settings.get("media_key_control_enabled"):
            return False
        key = self._ACTION_TO_KEY.get(str(action or "").strip().lower())
        if not key:
            return False
        sent = self._automation.press_key(key)
        if sent:
            logger.info("Media key dispatched: %s", action)
        return sent

    def list_windows(
        self,
        *,
        process_names: Iterable[str] | None = None,
        title_substrings: Iterable[str] | None = None,
    ) -> list[WindowTarget]:
        return self._automation.list_windows(process_names=process_names, title_substrings=title_substrings)

    def focus_window(self, hwnd: int, *, timeout: float | None = None) -> WindowTarget | None:
        return self._automation.focus_window(hwnd=hwnd, timeout=timeout)

    def wait_for(
        self,
        condition: Callable[[], bool],
        timeout: float,
        *,
        interval: float = 0.1,
    ) -> bool:
        return self._automation.wait_for(condition, timeout=timeout, interval=interval)

    def safe_sleep(self, duration_ms: int | None = None) -> None:
        self._automation.safe_sleep(duration_ms)

    def get_window(self, hwnd: int) -> WindowTarget | None:
        return self._automation.get_window(hwnd)

    def get_active_window_info(self) -> WindowInfo | None:
        try:
            return self._detector.get_active_context()
        except Exception as exc:  # pragma: no cover - defensive OS access
            logger.debug("Media active context lookup failed: %s", exc)
            return None

    def get_window_info(self, hwnd: int) -> WindowInfo | None:
        try:
            return self._detector.get_window_info(hwnd)
        except Exception as exc:  # pragma: no cover - defensive OS access
            logger.debug("Media window info lookup failed: %s", exc)
            return None

    def read_media_sessions(self) -> list[MediaMetadata]:
        if os.name != "nt":
            return []

        command = ["powershell", "-NoProfile", "-Command", _MEDIA_SESSION_SCRIPT]
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": 4,
        }
        startupinfo = _hidden_startupinfo()
        if startupinfo is not None:
            kwargs["startupinfo"] = startupinfo

        try:
            completed = subprocess.run(command, check=False, **kwargs)
        except Exception as exc:  # pragma: no cover - depends on local Windows host
            logger.debug("Media session probe failed: %s", exc)
            return []

        output = str(completed.stdout or completed.stderr or "").strip()
        if not output:
            return []

        try:
            payload = json.loads(output)
        except Exception as exc:  # pragma: no cover - defensive parsing
            logger.debug("Media session JSON parse failed: %s | %r", exc, output[:160])
            return []

        if not isinstance(payload, dict) or not payload.get("ok"):
            logger.debug("Media session probe returned error: %s", payload)
            return []

        raw_sessions = payload.get("sessions") or []
        if isinstance(raw_sessions, dict):
            raw_sessions = [raw_sessions]

        sessions: list[MediaMetadata] = []
        for item in raw_sessions:
            if not isinstance(item, dict):
                continue
            source_app = str(item.get("sourceApp") or "").strip()
            status = str(item.get("status") or "").strip()
            provider = _provider_from_source_app(source_app)
            metadata = MediaMetadata(
                provider=provider,
                track_name=str(item.get("title") or "").strip(),
                artist_name=str(item.get("artist") or "").strip(),
                album_name=str(item.get("album") or "").strip(),
                is_playing=status.lower() == "playing" if status else None,
                source="media_session",
                process_name=source_app,
                data={
                    "source_app": source_app,
                    "status": status,
                    "is_current": bool(item.get("isCurrent")),
                    "position_ticks": int(item.get("positionTicks") or 0),
                },
            )
            sessions.append(metadata)
        return sessions

    def read_media_session_metadata(
        self,
        *,
        source_hints: Iterable[str] | None = None,
    ) -> MediaMetadata | None:
        sessions = self.read_media_sessions()
        if not sessions:
            return None

        hints = [str(hint or "").strip().lower() for hint in (source_hints or []) if str(hint or "").strip()]
        candidates = sessions
        if hints:
            filtered = [
                item
                for item in sessions
                if any(
                    hint in str(item.data.get("source_app") or "").lower()
                    or hint == str(item.provider or "").lower()
                    for hint in hints
                )
            ]
            if filtered:
                candidates = filtered

        def _sort_key(item: MediaMetadata) -> tuple[int, int, int]:
            status = str(item.data.get("status") or "").strip().lower()
            return (
                0 if item.data.get("is_current") else 1,
                0 if status == "playing" else 1,
                0 if item.label() else 1,
            )

        return sorted(candidates, key=_sort_key)[0] if candidates else None

    def get_launcher(self):
        if self._launcher is not None:
            return self._launcher
        try:
            from core.launcher import AppLauncher

            self._launcher = AppLauncher()
        except Exception as exc:  # pragma: no cover - depends on launcher index state
            logger.warning("Media launcher unavailable: %s", exc)
            self._launcher = False
        return self._launcher if self._launcher is not False else None


def parse_track_label(value: str) -> tuple[str, str]:
    cleaned = " ".join(str(value or "").split()).strip()
    if not cleaned:
        return "", ""
    parts = [part.strip() for part in cleaned.split(" - ") if part.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return cleaned, ""


def _provider_from_source_app(source_app: str) -> str:
    source_l = str(source_app or "").strip().lower()
    if "spotify" in source_l:
        return "spotify"
    if "applemusic" in source_l or "itunes" in source_l:
        return "apple_music"
    if "wmplayer" in source_l or "mediaplayer" in source_l:
        return "windows_media_player"
    if any(token in source_l for token in ("chrome", "edge", "firefox", "brave", "opera")):
        return "browser"
    return "media"


def _hidden_startupinfo():
    if os.name != "nt" or not hasattr(subprocess, "STARTUPINFO"):
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return info
