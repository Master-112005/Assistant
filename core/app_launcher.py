"""
Windows-first desktop app catalog and launcher wrapper.

This module centralizes:

1. Canonical desktop app aliases
2. Process-name metadata for window/process matching
3. Website aliases that should open in a browser instead of as desktop apps
4. A resilient launcher wrapper built on the legacy AppLauncher
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Iterable

from core.app_index import canonicalize_app_name as _canonicalize_index_app_name, get_web_aliases
from core import settings, state
from core.launcher import AppLauncher, LaunchResult
from core.logger import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - optional fuzzy matching
    from rapidfuzz import fuzz, process as rf_process

    _RAPIDFUZZ_OK = True
except Exception:  # pragma: no cover
    fuzz = None
    rf_process = None
    _RAPIDFUZZ_OK = False


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_START_APPS_LOOKUP_TIMEOUT_SECONDS = 4.0


def _normalize_name(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


@dataclass(frozen=True, slots=True)
class AppProfile:
    app_id: str
    display_name: str
    aliases: tuple[str, ...]
    process_names: tuple[str, ...]
    known_paths: tuple[str, ...] = ()
    fallback_url: str = ""


APP_PROFILES: dict[str, AppProfile] = {
    "chrome": AppProfile(
        app_id="chrome",
        display_name="Chrome",
        aliases=("chrome", "google chrome", "chrom"),
        process_names=("chrome.exe",),
        known_paths=(
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        ),
    ),
    "edge": AppProfile(
        app_id="edge",
        display_name="Edge",
        aliases=("edge", "microsoft edge", "edgy"),
        process_names=("msedge.exe",),
        known_paths=(
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        ),
    ),
    "spotify": AppProfile(
        app_id="spotify",
        display_name="Spotify",
        aliases=("spotify", "spotify app"),
        process_names=("spotify.exe",),
        known_paths=(
            r"%AppData%\Spotify\Spotify.exe",
            r"%LocalAppData%\Microsoft\WindowsApps\Spotify.exe",
        ),
    ),
    "whatsapp": AppProfile(
        app_id="whatsapp",
        display_name="WhatsApp",
        aliases=("whatsapp", "whatsapp desktop", "wahtsapp", "watsapp", "whatssap"),
        process_names=("whatsapp.exe", "whatsapp.root.exe"),
        known_paths=(
            r"%LocalAppData%\WhatsApp\WhatsApp.exe",
            r"%ProgramFiles%\WindowsApps\5319275A.WhatsAppDesktop_*",
        ),
    ),
    "instagram": AppProfile(
        app_id="instagram",
        display_name="Instagram",
        aliases=("instagram", "insta", "ig"),
        process_names=("msedge.exe",),
        known_paths=(),
    ),
    "vscode": AppProfile(
        app_id="vscode",
        display_name="VS Code",
        aliases=("vscode", "vs code", "code", "visual studio code"),
        process_names=("code.exe", "code-insiders.exe"),
        known_paths=(
            r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
            r"%ProgramFiles%\Microsoft VS Code\Code.exe",
            r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe",
        ),
    ),
    "telegram": AppProfile(
        app_id="telegram",
        display_name="Telegram",
        aliases=("telegram",),
        process_names=("telegram.exe",),
        known_paths=(r"%AppData%\Telegram Desktop\Telegram.exe",),
    ),
    "discord": AppProfile(
        app_id="discord",
        display_name="Discord",
        aliases=("discord",),
        process_names=("discord.exe",),
        known_paths=(r"%LocalAppData%\Discord\Update.exe",),
    ),
    "zoom": AppProfile(
        app_id="zoom",
        display_name="Zoom",
        aliases=("zoom",),
        process_names=("zoom.exe",),
        known_paths=(r"%AppData%\Zoom\bin\Zoom.exe",),
    ),
    "notepad": AppProfile(
        app_id="notepad",
        display_name="Notepad",
        aliases=("notepad",),
        process_names=("notepad.exe",),
        known_paths=(r"%WINDIR%\System32\notepad.exe",),
    ),
    "calculator": AppProfile(
        app_id="calculator",
        display_name="Calculator",
        aliases=("calculator", "calc"),
        process_names=("calculatorapp.exe", "CalculatorApp.exe", "calc.exe"),
        known_paths=(r"%WINDIR%\System32\calc.exe",),
    ),
    "explorer": AppProfile(
        app_id="explorer",
        display_name="File Explorer",
        aliases=("explorer", "file explorer", "windows explorer", "file manager"),
        process_names=("explorer.exe",),
        known_paths=(r"%WINDIR%\explorer.exe",),
    ),
    "phone link": AppProfile(
        app_id="phone link",
        display_name="Phone Link",
        aliases=("phone link", "your phone", "link to phone"),
        process_names=("PhoneLinkApp.exe",),
    ),
    "instagram": AppProfile(
        app_id="instagram",
        display_name="Instagram",
        aliases=("instagram", "insta"),
        process_names=("Instagram.exe",),
    ),
}

WEBSITE_ALIASES: dict[str, str] = {
    "gmail": "https://mail.google.com",
    "google": "https://www.google.com",
    "github": "https://github.com",
    "chatgpt": "https://chat.openai.com",
    "claude": "https://claude.ai",
    "netflix": "https://www.netflix.com",
    "outlook": "https://outlook.live.com",
    "instagram": "https://www.instagram.com",
    "whatsapp": "https://web.whatsapp.com",
    "youtube": "https://www.youtube.com",
    "spotify": "https://open.spotify.com",
    "twitter": "https://twitter.com",
    "facebook": "https://www.facebook.com",
    "linkedin": "https://www.linkedin.com",
}
WEBSITE_ALIASES.update(get_web_aliases())

WEBSITE_DISPLAY_NAMES: dict[str, str] = {
    "youtube": "YouTube",
    "you tube": "YouTube",
    "gmail": "Gmail",
    "google": "Google",
    "github": "GitHub",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "netflix": "Netflix",
    "outlook": "Outlook",
    "instagram": "Instagram",
    "insta": "Instagram",
    "whatsapp": "WhatsApp",
    "twitter": "Twitter",
    "facebook": "Facebook",
    "linkedin": "LinkedIn",
}
_LIKELY_WEB_TLDS = {"ai", "app", "co", "com", "dev", "gg", "in", "io", "me", "net", "org", "tv"}
_FILE_EXTENSION_RE = re.compile(
    r"\.(?:7z|avi|bat|bmp|csv|doc|docm|docx|gif|jpeg|jpg|json|lnk|md|mov|mp3|mp4|msi|odp|ods|odt|pdf|png|ppt|pptx|ps1|py|rtf|sql|svg|tar|txt|wav|xlsx|xls|xml|zip)$",
    flags=re.IGNORECASE,
)

def preferred_browser_id() -> str:
    preferred = _normalize_name(settings.get("preferred_browser") or "")
    if preferred in APP_PROFILES:
        return preferred
    return "chrome"


def canonicalize_app_name(name: str, *, resolve_browser_alias: bool = True) -> str:
    normalized = _normalize_name(name)
    if not normalized:
        return ""
    if resolve_browser_alias and normalized in {"browser", "default browser", "web browser"}:
        return preferred_browser_id()
    canonical = _canonicalize_index_app_name(normalized, resolve_browser_alias=False)
    return canonical or normalized


def app_display_name(name: str) -> str:
    canonical = canonicalize_app_name(name)
    profile = APP_PROFILES.get(canonical)
    if profile is not None:
        return profile.display_name
    website_label = WEBSITE_DISPLAY_NAMES.get(_normalize_name(name))
    if website_label:
        return website_label
    raw = str(name or "").strip()
    if not raw:
        return "the app"
    return raw.replace("_", " ").title()


def app_process_names(name: str) -> tuple[str, ...]:
    canonical = canonicalize_app_name(name)
    profile = APP_PROFILES.get(canonical)
    if profile is not None:
        return profile.process_names
    normalized = _normalize_name(name)
    if not normalized:
        return ()
    if normalized.endswith(".exe"):
        return (normalized,)
    return (f"{normalized}.exe",)


def app_aliases() -> list[str]:
    ordered: list[str] = []
    for profile in APP_PROFILES.values():
        ordered.extend(profile.aliases)
        ordered.append(profile.display_name)
    return ordered


def website_url_for(name: str) -> str:
    normalized = _normalize_name(name)
    if not normalized:
        return ""
    if normalized.startswith(("http://", "https://")):
        return normalized
    if "." in normalized and " " not in normalized:
        if _FILE_EXTENSION_RE.search(normalized):
            return ""
        suffix = normalized.rsplit(".", 1)[-1].split("/", 1)[0].lower()
        if suffix not in _LIKELY_WEB_TLDS and not re.match(r"^[a-z]+://", normalized, flags=re.IGNORECASE):
            return ""
        return normalized if normalized.startswith(("http://", "https://")) else f"https://{normalized}"
    return WEBSITE_ALIASES.get(normalized, "")


def is_app_running(app_name: str) -> bool:
    """Check if an app is currently running (process OR window)."""
    process_names = app_process_names(app_name)
    if not process_names:
        normalized = _normalize_name(app_name)
        if normalized and not normalized.endswith(".exe"):
            process_names = (f"{normalized}.exe",)
        else:
            process_names = (normalized,)
    
    import subprocess
    try:
        # Check process first
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {process_names[0]}", "/NH"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if process_names[0].lower() in result.stdout.lower():
            return True
            
        # Check for window with app name (for Windows Store apps like WhatsApp)
        window_result = subprocess.run(
            ["tasklist", "/FI", f"WINDOWTITLE eq *{app_name}*"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if app_name.lower() in window_result.stdout.lower():
            return True
            
    except Exception:
        pass
    
    # Try PowerShell for better window detection
    try:
        ps_script = f'''
$windows = Get-Process | Where-Object {{ $_.MainWindowTitle -match "{app_name}" -or $_.ProcessName -match "{process_names[0].replace('.exe', '')}" }}
if ($windows) {{ exit 0 }} else {{ exit 1 }}
'''
        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True,
            timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


def is_app_installed(app_name: str) -> bool:
    """Check if an app is installed (file exists or running)."""
    if is_app_running(app_name):
        return True
    
    process_names = app_process_names(app_name)
    if not process_names:
        return False
    
    import os
    import subprocess
    
    profile = APP_PROFILES.get(app_name.lower())
    if profile:
        for path in profile.known_paths:
            expanded = os.path.expandvars(path)
            if os.path.exists(expanded):
                return True
    
    try:
        result = subprocess.run(
            ["where", process_names[0]],
            capture_output=True,
            timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


def is_known_website(name: str) -> bool:
    return bool(website_url_for(name))


def app_fallback_url_for(name: str) -> str:
    canonical = canonicalize_app_name(name)
    profile = APP_PROFILES.get(canonical)
    return str(profile.fallback_url).strip() if profile is not None else ""


class DesktopAppLauncher:
    """Richer app launcher facade used by the command pipeline."""

    def __init__(self, legacy_launcher: AppLauncher | None = None) -> None:
        self._legacy = legacy_launcher or AppLauncher()
        # External/injected launchers (tests, alternate backends) should remain
        # authoritative and must not probe the real machine for installed apps.
        self._use_local_catalog = isinstance(self._legacy, AppLauncher)
        self._start_apps_cache: dict[str, str] | None = None

    def launch_by_name(self, query: str) -> LaunchResult:
        return self.launch_app(query)

    def launch_app(self, query: str) -> LaunchResult:
        requested = str(query or "").strip()
        if not requested:
            return LaunchResult(
                False,
                requested,
                message="No application name was provided.",
                error="missing_app_name",
                data={"requested_app": requested},
            )

        canonical = canonicalize_app_name(requested)
        profile = APP_PROFILES.get(canonical)
        errors: list[str] = []

        if profile is not None and self._use_local_catalog:
            executable = self._resolve_executable(profile)
            if executable:
                launched = self._launch_executable(executable, requested, canonical, profile)
                if launched is not None:
                    return launched

            start_app_id = self._resolve_start_app_id(profile)
            if start_app_id:
                launched = self._launch_start_app(start_app_id, requested, canonical, profile)
                if launched is not None:
                    return launched

            result = self._try_legacy_launch(profile, requested, canonical, errors)
            if result is not None:
                return result

        # Fall back to the indexed launcher with the raw query when the app is
        # not in the built-in catalog or the targeted lookup failed.
        try:
            result = self._legacy.launch_by_name(requested)
            if result.success:
                return self._decorate_result(result, requested, canonical or requested)
            if result.message:
                errors.append(result.message)
        except Exception as exc:  # pragma: no cover - defensive integration
            errors.append(str(exc))

        suggestions = self.suggest_apps(requested)
        message = f"I couldn't find an installed app named {requested}."
        if canonical == "chrome" and self.is_installed("edge"):
            message = "Chrome is not installed. Open Edge instead?"
        elif suggestions:
            message = f"I couldn't find that app. Did you mean {suggestions[0]}?"

        return LaunchResult(
            False,
            requested,
            matched_name=profile.display_name if profile else "",
            message=message,
            error="app_not_found",
            data={
                "requested_app": requested,
                "canonical_app": canonical,
                "suggestions": suggestions,
                "errors": errors[:3],
            },
        )

    def is_installed(self, query: str) -> bool:
        canonical = canonicalize_app_name(query)
        profile = APP_PROFILES.get(canonical)
        if profile is not None:
            if self._resolve_executable(profile):
                return True
            if self._resolve_start_app_id(profile):
                return True
        try:
            record = self._legacy.find_best_match(query)
        except Exception as exc:
            logger.debug("Legacy app lookup failed for %s: %s", query, exc)
            return False
        return record is not None

    def suggest_apps(self, query: str, *, limit: int = 3) -> list[str]:
        requested = str(query or "").strip()
        if not requested:
            return []

        candidates: dict[str, str] = {}
        for profile in APP_PROFILES.values():
            candidates[profile.display_name] = profile.display_name
            for alias in profile.aliases:
                candidates[alias] = profile.display_name

        try:
            for record in self._legacy.indexer.get_all_records():
                candidates[record.name] = record.name
        except Exception as exc:
            logger.debug("Legacy app suggestions unavailable for %s: %s", requested, exc)

        if _RAPIDFUZZ_OK and rf_process is not None and fuzz is not None:
            matches = rf_process.extract(
                requested,
                list(candidates.keys()),
                scorer=fuzz.WRatio,
                limit=max(1, limit * 2),
            )
            ordered: list[str] = []
            seen: set[str] = set()
            for choice, score, _ in matches:
                if score < 68:
                    continue
                label = candidates.get(choice, choice)
                lowered = label.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                ordered.append(label)
                if len(ordered) >= limit:
                    break
            return ordered

        lowered = requested.lower()
        ordered = sorted(
            {value for value in candidates.values()},
            key=lambda item: (lowered not in item.lower(), item.lower()),
        )
        return ordered[:limit]

    def process_names_for(self, query: str) -> tuple[str, ...]:
        return app_process_names(query)

    def display_name_for(self, query: str) -> str:
        return app_display_name(query)

    def website_url_for(self, query: str) -> str:
        return website_url_for(query)

    def _try_legacy_launch(
        self,
        profile: AppProfile,
        requested: str,
        canonical: str,
        errors: list[str],
    ) -> LaunchResult | None:
        for candidate in self._launch_candidates(profile):
            try:
                result = self._legacy.launch_by_name(candidate)
            except Exception as exc:  # pragma: no cover - defensive integration
                errors.append(str(exc))
                continue
            if result.success:
                return self._decorate_result(result, requested, canonical)
            if result.message:
                errors.append(result.message)
        return None

    def _launch_candidates(self, profile: AppProfile) -> Iterable[str]:
        yield profile.display_name
        for alias in profile.aliases:
            yield alias

    def _resolve_executable(self, profile: AppProfile) -> str:
        for process_name in profile.process_names:
            stem = process_name[:-4] if process_name.lower().endswith(".exe") else process_name
            which_match = shutil.which(stem)
            if which_match:
                return which_match
        for raw_path in profile.known_paths:
            expanded = os.path.expandvars(raw_path)
            try:
                if glob.has_magic(expanded):
                    for match in glob.glob(expanded):
                        resolved = self._resolve_executable_candidate(match, profile.process_names)
                        if resolved:
                            return resolved
                    continue
                resolved = self._resolve_executable_candidate(expanded, profile.process_names)
                if resolved:
                    return resolved
            except OSError as exc:
                logger.debug("Skipping inaccessible app path %s: %s", expanded, exc)
        return ""

    def _resolve_executable_candidate(self, candidate: str, process_names: tuple[str, ...]) -> str:
        if not candidate:
            return ""
        if os.path.isfile(candidate):
            return candidate
        if not os.path.isdir(candidate):
            return ""

        for process_name in process_names:
            exact_match = os.path.join(candidate, process_name)
            if os.path.exists(exact_match):
                return exact_match

        try:
            exe_children = [
                entry.path
                for entry in os.scandir(candidate)
                if entry.is_file() and entry.name.lower().endswith(".exe")
            ]
        except OSError as exc:
            logger.debug("Skipping inaccessible directory %s: %s", candidate, exc)
            return ""

        if len(exe_children) == 1:
            return exe_children[0]
        return ""

    def _resolve_start_app_id(self, profile: AppProfile) -> str:
        start_apps = self._list_start_apps()
        if not start_apps:
            return ""

        normalized_candidates = {
            _normalize_name(profile.display_name),
            *(_normalize_name(alias) for alias in profile.aliases),
        }
        normalized_candidates.discard("")

        for candidate in normalized_candidates:
            app_id = start_apps.get(candidate)
            if app_id:
                return app_id

        for name, app_id in start_apps.items():
            if any(candidate in name for candidate in normalized_candidates):
                return app_id
        return ""

    def _list_start_apps(self) -> dict[str, str]:
        if self._start_apps_cache is not None:
            return dict(self._start_apps_cache)

        try:
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                timeout=max(1.0, float(settings.get("start_apps_lookup_timeout_seconds") or _START_APPS_LOOKUP_TIMEOUT_SECONDS)),
                creationflags=CREATE_NO_WINDOW if CREATE_NO_WINDOW else 0,
            )
        except Exception as exc:
            logger.debug("Start Apps catalog lookup failed: %s", exc)
            self._start_apps_cache = {}
            return {}

        if completed.returncode != 0:
            logger.debug("Start Apps catalog command failed: %s", (completed.stderr or completed.stdout or "").strip())
            self._start_apps_cache = {}
            return {}

        payload_text = (completed.stdout or "").strip()
        if not payload_text:
            self._start_apps_cache = {}
            return {}

        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            logger.debug("Start Apps catalog JSON decode failed: %s", exc)
            self._start_apps_cache = {}
            return {}

        if isinstance(payload, dict):
            payload = [payload]

        catalog: dict[str, str] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = _normalize_name(item.get("Name") or "")
            app_id = str(item.get("AppID") or "").strip()
            if name and app_id:
                catalog[name] = app_id

        self._start_apps_cache = catalog
        return dict(catalog)

    def _launch_executable(
        self,
        executable: str,
        requested: str,
        canonical: str,
        profile: AppProfile,
    ) -> LaunchResult | None:
        try:
            process = subprocess.Popen(
                [executable],
                creationflags=CREATE_NO_WINDOW if CREATE_NO_WINDOW else 0,
            )
        except Exception as exc:
            logger.warning("Known-path launch failed for %s: %s", executable, exc)
            return None

        state.last_launched_app = profile.display_name
        state.last_launch_success = True
        state.last_launch_pid = process.pid
        return LaunchResult(
            True,
            requested,
            matched_name=profile.display_name,
            path=executable,
            pid=int(process.pid),
            message=f"Opening {profile.display_name}.",
            verified=bool(process.pid),
            data={
                "requested_app": requested,
                "canonical_app": canonical,
                "matched_name": profile.display_name,
                "launch_source": "known_path",
            },
        )

    def _launch_start_app(
        self,
        start_app_id: str,
        requested: str,
        canonical: str,
        profile: AppProfile,
    ) -> LaunchResult | None:
        shell_target = f"shell:AppsFolder\\{start_app_id}"
        try:
            subprocess.Popen(
                ["explorer.exe", shell_target],
                creationflags=CREATE_NO_WINDOW if CREATE_NO_WINDOW else 0,
            )
        except Exception as exc:
            logger.warning("Start Apps launch failed for %s: %s", start_app_id, exc)
            return None

        state.last_launched_app = profile.display_name
        state.last_launch_success = True
        state.last_launch_pid = -1
        return LaunchResult(
            True,
            requested,
            matched_name=profile.display_name,
            path=shell_target,
            pid=-1,
            message=f"Opening {profile.display_name}.",
            verified=False,
            data={
                "requested_app": requested,
                "canonical_app": canonical,
                "matched_name": profile.display_name,
                "launch_source": "start_apps",
                "start_app_id": start_app_id,
            },
        )

    def _decorate_result(self, result: LaunchResult, requested: str, canonical: str) -> LaunchResult:
        message = result.message or f"Opening {app_display_name(canonical)}."
        data = dict(result.data or {})
        data.update(
            {
                "requested_app": requested,
                "canonical_app": canonical,
                "matched_name": result.matched_name or app_display_name(canonical),
                "launch_source": data.get("launch_source") or "indexed_launcher",
            }
        )
        return LaunchResult(
            success=result.success,
            app_name=result.app_name,
            matched_name=result.matched_name or app_display_name(canonical),
            path=result.path,
            pid=result.pid,
            message=message,
            error=result.error,
            data=data,
            launch_time=result.launch_time,
            verified=bool(getattr(result, "verified", False)),
            duration_ms=int(getattr(result, "duration_ms", 0) or 0),
        )
