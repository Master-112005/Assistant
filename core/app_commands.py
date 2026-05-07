"""
Deterministic parsing for explicit desktop app commands.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from core.app_index import STATIC_APP_INDEX, get_app_matcher
from core.app_launcher import APP_PROFILES, is_known_website
from core.apps_registry import canonicalize_app_name
from core.normalizer import normalize_command


_VERB_TO_INTENT = {
    "open": "open_app",
    "close": "close_app",
    "quit": "close_app",
    "exit": "close_app",
    "minimize": "minimize_app",
    "maximize": "maximize_app",
    "focus": "focus_app",
    "restore": "restore_app",
    "toggle": "toggle_app",
}
_COMMAND_RE = re.compile(
    r"^(?P<verb>open|close|quit|exit|minimize|maximize|focus|restore|toggle)\s+(?P<target>.+?)$",
    flags=re.IGNORECASE,
)
_EXPLICIT_BROWSER_RE = re.compile(
    r"\s+(?:in|on|using)\s+(?:google chrome|chrome|default browser|web browser|browser|microsoft edge|edge|mozilla firefox|firefox|brave browser|brave)$",
    flags=re.IGNORECASE,
)
_APP_SUFFIX_RE = re.compile(r"\s+(?:app|application|window)\s*$", flags=re.IGNORECASE)
_FILE_EXTENSION_RE = re.compile(
    r"\.(?:7z|avi|bat|bmp|csv|doc|docm|docx|gif|jpeg|jpg|json|lnk|md|mov|mp3|mp4|msi|odp|ods|odt|pdf|png|ppt|pptx|ps1|py|rtf|sql|svg|tar|txt|wav|xlsx|xls|xml|zip)$",
    flags=re.IGNORECASE,
)
_IGNORED_BROWSER_TARGETS = {
    "new tab",
    "tab",
    "current tab",
    "this tab",
    "home page",
}
_REFERENTIAL_APP_TARGETS = {
    "it",
    "this",
    "that",
    "this app",
    "that app",
    "current app",
    "this window",
    "that window",
    "current window",
}
_TAB_TARGET_RE = re.compile(r"^(?:tab\s+\d+|\d+(?:st|nd|rd|th)?\s+tab|first\s+tab|second\s+tab|third\s+tab|fourth\s+tab|fifth\s+tab|sixth\s+tab|seventh\s+tab|eighth\s+tab|ninth\s+tab|next\s+tab|previous\s+tab)$", flags=re.IGNORECASE)
_APP_STYLE_FILE_TARGETS = {"explorer", "file explorer", "windows explorer", "file manager"}
_FILE_HINT_TOKENS = {
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


@dataclass(frozen=True, slots=True)
class ParsedAppCommand:
    intent: str
    requested_app: str
    app_name: str
    normalized_text: str
    confidence: float
    corrected: bool = False


def parse_app_command(text: str) -> ParsedAppCommand | None:
    original = " ".join(str(text or "").strip().split()).lower()
    raw_match = _COMMAND_RE.match(original)
    if raw_match is not None:
        original_target = _clean_target(raw_match.group("target"))
        raw_target = " ".join(raw_match.group("target").strip().split()).lower()
        if raw_target in _REFERENTIAL_APP_TARGETS or original_target in _REFERENTIAL_APP_TARGETS:
            return None

    normalized = normalize_command(text)
    if not normalized:
        return None

    match = _COMMAND_RE.match(normalized)
    if match is None:
        return None

    if _EXPLICIT_BROWSER_RE.search(normalized):
        return None

    verb = match.group("verb").lower()
    raw_target = " ".join(match.group("target").strip().split()).lower()
    target = _clean_target(match.group("target"))
    if not target or target in _IGNORED_BROWSER_TARGETS or _TAB_TARGET_RE.fullmatch(target):
        return None
    if raw_target in _REFERENTIAL_APP_TARGETS or target in _REFERENTIAL_APP_TARGETS:
        return None
    if _looks_like_file_target(target):
        return None
    if verb == "open" and is_known_website(target) and canonicalize_app_name(target) not in APP_PROFILES:
        return None

    requested_app = _normalize_requested_app(target)
    app_name, confidence, corrected = _resolve_app_name(requested_app)
    if not app_name:
        app_name = requested_app
        confidence = 0.72
        corrected = False

    return ParsedAppCommand(
        intent=_VERB_TO_INTENT[verb],
        requested_app=requested_app,
        app_name=app_name,
        normalized_text=f"{'close' if verb in {'quit', 'exit'} else verb} {app_name}".strip(),
        confidence=confidence,
        corrected=corrected or app_name != requested_app,
    )


def _clean_target(value: str) -> str:
    target = " ".join(str(value or "").strip().split())
    target = re.sub(r"^(?:the|this|current)\s+", "", target, flags=re.IGNORECASE)
    target = _APP_SUFFIX_RE.sub("", target).strip()
    return target


def _normalize_requested_app(target: str) -> str:
    normalized = target.strip().lower()
    if normalized.endswith(".exe") and len(normalized) > 4:
        normalized = normalized[:-4]
    return normalized


def _resolve_app_name(target: str) -> tuple[str, float, bool]:
    if not target:
        return "", 0.0, False

    canonical = canonicalize_app_name(target)
    if canonical in APP_PROFILES:
        corrected = canonical != target
        return canonical, 0.99 if not corrected else 0.97, corrected

    match = get_app_matcher().match(target, include_web=False)
    if match is None or match.canonical_name not in STATIC_APP_INDEX or match.launch_target == "web":
        return "", 0.0, False
    return match.canonical_name, float(match.confidence), match.canonical_name != target


def _looks_like_file_target(target: str) -> bool:
    normalized = str(target or "").strip().lower()
    if not normalized:
        return False
    if normalized in _APP_STYLE_FILE_TARGETS:
        return False
    if normalized.startswith(('"', "'")):
        return True
    if any(marker in normalized for marker in ("\\", "/", ":")):
        return True
    if set(normalized.split()) & _FILE_HINT_TOKENS:
        return True
    return bool(_FILE_EXTENSION_RE.search(normalized))
