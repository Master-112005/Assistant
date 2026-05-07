"""
Central app index and deterministic app-name matcher.

This module is the single source of truth for:
1. Canonical app names and aliases used by normalization/routing
2. Fast confidence-scored correction of noisy app phrases
3. Backward-compatible installed-app indexing for the legacy launcher
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from core import state
from core.logger import get_logger
from core.paths import DATA_DIR

logger = get_logger(__name__)

APP_CACHE_FILE = DATA_DIR / "app_cache.json"

try:  # pragma: no cover - optional fast path
    from rapidfuzz import fuzz, process as rf_process

    _RAPIDFUZZ_OK = True
except Exception:  # pragma: no cover
    fuzz = None
    rf_process = None
    _RAPIDFUZZ_OK = False


@dataclass(frozen=True, slots=True)
class AppIndexEntry:
    canonical_name: str
    aliases: tuple[str, ...] = ()
    executable_name: str = ""
    keywords: tuple[str, ...] = ()
    launch_target: str = "desktop"
    url: str = ""

    def all_names(self) -> tuple[str, ...]:
        ordered = [self.canonical_name, *self.aliases]
        return tuple(dict.fromkeys(_normalize_alias(name) for name in ordered if name))


@dataclass(frozen=True, slots=True)
class AppMatch:
    canonical_name: str
    confidence: float
    matched_alias: str
    layer: str
    launch_target: str
    executable_name: str = ""
    url: str = ""

    @property
    def requires_confirmation(self) -> bool:
        return 0.65 <= self.confidence <= 0.85

    @property
    def should_autocorrect(self) -> bool:
        return self.confidence > 0.85


@dataclass
class AppRecord:
    name: str
    normalized_name: str
    path: str
    type: str
    source: str
    aliases: list[str] = field(default_factory=list)
    exists: bool = True


def _normalize_alias(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _compact_alias(value: str) -> str:
    return _normalize_alias(value).replace(" ", "")


def _squash_repeated_letters(value: str) -> str:
    return re.sub(r"(.)\1{1,}", r"\1", value)


def _damerau_levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    len_left = len(left)
    len_right = len(right)
    table = [[0] * (len_right + 1) for _ in range(len_left + 1)]
    for i in range(len_left + 1):
        table[i][0] = i
    for j in range(len_right + 1):
        table[0][j] = j

    for i in range(1, len_left + 1):
        for j in range(1, len_right + 1):
            cost = 0 if left[i - 1] == right[j - 1] else 1
            table[i][j] = min(
                table[i - 1][j] + 1,
                table[i][j - 1] + 1,
                table[i - 1][j - 1] + cost,
            )
            if i > 1 and j > 1 and left[i - 1] == right[j - 2] and left[i - 2] == right[j - 1]:
                table[i][j] = min(table[i][j], table[i - 2][j - 2] + cost)
    return table[len_left][len_right]


STATIC_APP_INDEX: dict[str, AppIndexEntry] = {
    "chrome": AppIndexEntry(
        canonical_name="chrome",
        aliases=("google chrome",),
        executable_name="chrome.exe",
        keywords=("browser", "web", "google"),
    ),
    "edge": AppIndexEntry(
        canonical_name="edge",
        aliases=("microsoft edge",),
        executable_name="msedge.exe",
        keywords=("browser", "web", "microsoft"),
    ),
    "whatsapp": AppIndexEntry(
        canonical_name="whatsapp",
        aliases=("whats app", "watsapp", "wa", "whatsapp desktop"),
        executable_name="whatsapp.exe",
        keywords=("chat", "message", "messaging"),
    ),
    "youtube": AppIndexEntry(
        canonical_name="youtube",
        aliases=("you tube", "yt"),
        executable_name="youtube.exe",
        keywords=("video", "music", "stream"),
        launch_target="either",
        url="https://www.youtube.com",
    ),
    "spotify": AppIndexEntry(
        canonical_name="spotify",
        aliases=(),
        executable_name="spotify.exe",
        keywords=("music", "songs", "audio"),
    ),
    "vscode": AppIndexEntry(
        canonical_name="vscode",
        aliases=("vs code", "visual studio code", "code"),
        executable_name="code.exe",
        keywords=("editor", "coding", "development"),
    ),
    "notepad": AppIndexEntry(
        canonical_name="notepad",
        aliases=("note pad",),
        executable_name="notepad.exe",
        keywords=("text", "editor"),
    ),
    "explorer": AppIndexEntry(
        canonical_name="explorer",
        aliases=("file explorer", "windows explorer", "file manager"),
        executable_name="explorer.exe",
        keywords=("files", "folders", "windows"),
    ),
    "telegram": AppIndexEntry(
        canonical_name="telegram",
        aliases=(),
        executable_name="telegram.exe",
        keywords=("chat", "message", "messaging"),
    ),
    "discord": AppIndexEntry(
        canonical_name="discord",
        aliases=(),
        executable_name="discord.exe",
        keywords=("chat", "gaming", "voice"),
    ),
    "zoom": AppIndexEntry(
        canonical_name="zoom",
        aliases=(),
        executable_name="zoom.exe",
        keywords=("meetings", "video", "calls"),
    ),
    "calculator": AppIndexEntry(
        canonical_name="calculator",
        aliases=("calc",),
        executable_name="calc.exe",
        keywords=("math", "numbers"),
    ),
}


class AppNameMatcher:
    """Fast exact/rule/fuzzy matcher for app phrases."""

    def __init__(self, entries: Iterable[AppIndexEntry]) -> None:
        self._entries = {entry.canonical_name: entry for entry in entries}
        self._alias_lookup: dict[str, str] = {}
        self._compact_aliases: dict[str, list[tuple[str, str]]] = {}
        self._simplified_aliases: dict[str, list[tuple[str, str]]] = {}
        self._aliases_by_initial: dict[str, list[tuple[str, str, str]]] = {}
        fuzzy_aliases: list[str] = []

        for entry in self._entries.values():
            for alias in entry.all_names():
                self._alias_lookup[alias] = entry.canonical_name
                compact = alias.replace(" ", "")
                self._compact_aliases.setdefault(compact, []).append((alias, entry.canonical_name))
                simplified = _squash_repeated_letters(compact)
                self._simplified_aliases.setdefault(simplified, []).append((alias, entry.canonical_name))
                if compact:
                    self._aliases_by_initial.setdefault(compact[0], []).append((alias, compact, entry.canonical_name))
                if len(compact) >= 4:
                    fuzzy_aliases.append(alias)

        self._fuzzy_aliases = tuple(dict.fromkeys(fuzzy_aliases))

    @lru_cache(maxsize=2048)
    def match(
        self,
        raw_name: str,
        *,
        allowed_targets: tuple[str, ...] = (),
        include_web: bool = True,
    ) -> AppMatch | None:
        normalized = _normalize_alias(raw_name)
        if not normalized:
            return None

        allowed = set(allowed_targets or ())
        exact = self._match_exact(normalized, allowed=allowed, include_web=include_web)
        if exact is not None:
            return exact

        rule_based = self._match_normalized_rules(normalized, allowed=allowed, include_web=include_web)
        if rule_based is not None:
            return rule_based

        return self._match_fuzzy(normalized, allowed=allowed, include_web=include_web)

    def _match_exact(self, normalized: str, *, allowed: set[str], include_web: bool) -> AppMatch | None:
        canonical = self._alias_lookup.get(normalized)
        if not canonical:
            return None
        entry = self._entries[canonical]
        if not include_web and entry.launch_target == "web":
            return None
        if allowed and canonical not in allowed:
            return None
        confidence = 1.0 if normalized == canonical else 0.99
        return AppMatch(
            canonical_name=canonical,
            confidence=confidence,
            matched_alias=normalized,
            layer="exact",
            launch_target=entry.launch_target,
            executable_name=entry.executable_name,
            url=entry.url,
        )

    def _match_normalized_rules(self, normalized: str, *, allowed: set[str], include_web: bool) -> AppMatch | None:
        compact = normalized.replace(" ", "")
        simplified = _squash_repeated_letters(compact)
        candidates: list[AppMatch] = []

        for alias, canonical in self._compact_aliases.get(compact, ()):
            entry = self._entries[canonical]
            if (not include_web and entry.launch_target == "web") or (allowed and canonical not in allowed):
                continue
            confidence = 0.84 if alias != normalized else 0.99
            candidates.append(
                AppMatch(
                    canonical_name=canonical,
                    confidence=confidence,
                    matched_alias=alias,
                    layer="normalized_compact",
                    launch_target=entry.launch_target,
                    executable_name=entry.executable_name,
                    url=entry.url,
                )
            )

        for alias, canonical in self._simplified_aliases.get(simplified, ()):
            entry = self._entries[canonical]
            if (not include_web and entry.launch_target == "web") or (allowed and canonical not in allowed):
                continue
            confidence = 0.78 if " " in normalized and alias != normalized else 0.88
            candidates.append(
                AppMatch(
                    canonical_name=canonical,
                    confidence=confidence,
                    matched_alias=alias,
                    layer="normalized_repeat",
                    launch_target=entry.launch_target,
                    executable_name=entry.executable_name,
                    url=entry.url,
                )
            )

        initial = compact[:1]
        for alias, alias_compact, canonical in self._aliases_by_initial.get(initial, ()):
            if abs(len(alias_compact) - len(compact)) > 2:
                continue
            distance = _damerau_levenshtein(compact, alias_compact)
            if distance > 1:
                continue
            entry = self._entries[canonical]
            if (not include_web and entry.launch_target == "web") or (allowed and canonical not in allowed):
                continue
            confidence = 0.78 if " " in normalized else (0.91 if len(compact) >= 5 else 0.86)
            candidates.append(
                AppMatch(
                    canonical_name=canonical,
                    confidence=confidence,
                    matched_alias=alias,
                    layer="normalized_typo",
                    launch_target=entry.launch_target,
                    executable_name=entry.executable_name,
                    url=entry.url,
                )
            )

        return _pick_best_match(candidates)

    def _match_fuzzy(self, normalized: str, *, allowed: set[str], include_web: bool) -> AppMatch | None:
        compact = normalized.replace(" ", "")
        if len(compact) < 4:
            return None

        candidates = self._fuzzy_aliases
        if not candidates:
            return None

        if _RAPIDFUZZ_OK and rf_process is not None and fuzz is not None:
            matches = rf_process.extract(normalized, candidates, scorer=fuzz.WRatio, limit=3)
        else:
            matches = []
            for alias in candidates:
                score = _fallback_similarity(normalized, alias)
                matches.append((alias, score, None))
            matches.sort(key=lambda item: item[1], reverse=True)
            matches = matches[:3]

        viable: list[AppMatch] = []
        for alias, score, _ in matches:
            if score < 84:
                continue
            canonical = self._alias_lookup.get(alias)
            if not canonical:
                continue
            entry = self._entries[canonical]
            if (not include_web and entry.launch_target == "web") or (allowed and canonical not in allowed):
                continue

            confidence = _fuzzy_score_to_confidence(score)
            viable.append(
                AppMatch(
                    canonical_name=canonical,
                    confidence=confidence,
                    matched_alias=alias,
                    layer="fuzzy",
                    launch_target=entry.launch_target,
                    executable_name=entry.executable_name,
                    url=entry.url,
                )
            )

        best = _pick_best_match(viable)
        if best is None:
            return None

        competing = [
            match
            for match in viable
            if match.canonical_name != best.canonical_name and abs(match.confidence - best.confidence) <= 0.04
        ]
        if competing:
            downgraded = max(0.0, round(best.confidence - 0.08, 3))
            best = AppMatch(
                canonical_name=best.canonical_name,
                confidence=downgraded,
                matched_alias=best.matched_alias,
                layer=f"{best.layer}_ambiguous",
                launch_target=best.launch_target,
                executable_name=best.executable_name,
                url=best.url,
            )
        return best if best.confidence >= 0.65 else None


def _fallback_similarity(left: str, right: str) -> int:
    left_compact = left.replace(" ", "")
    right_compact = right.replace(" ", "")
    distance = _damerau_levenshtein(left_compact, right_compact)
    max_len = max(len(left_compact), len(right_compact), 1)
    return max(0, int(round((1.0 - (distance / max_len)) * 100)))


def _fuzzy_score_to_confidence(score: float) -> float:
    if score >= 96:
        return 0.90
    if score >= 92:
        return 0.86
    if score >= 88:
        return 0.80
    return 0.70


def _pick_best_match(matches: Iterable[AppMatch]) -> AppMatch | None:
    ordered = sorted(matches, key=lambda item: item.confidence, reverse=True)
    if not ordered:
        return None
    best = ordered[0]
    if len(ordered) == 1:
        return best
    runner_up = ordered[1]
    if runner_up.canonical_name != best.canonical_name and abs(best.confidence - runner_up.confidence) <= 0.03:
        downgraded = max(0.0, round(best.confidence - 0.06, 3))
        return AppMatch(
            canonical_name=best.canonical_name,
            confidence=downgraded,
            matched_alias=best.matched_alias,
            layer=f"{best.layer}_ambiguous",
            launch_target=best.launch_target,
            executable_name=best.executable_name,
            url=best.url,
        )
    return best


_MATCHER = AppNameMatcher(STATIC_APP_INDEX.values())


def get_app_matcher() -> AppNameMatcher:
    return _MATCHER


def get_app_entry(name: str) -> AppIndexEntry | None:
    canonical = canonicalize_app_name(name)
    return STATIC_APP_INDEX.get(canonical)


def canonicalize_app_name(name: str, *, resolve_browser_alias: bool = True) -> str:
    normalized = _normalize_alias(name)
    if not normalized:
        return ""
    if resolve_browser_alias and normalized in {"browser", "default browser", "web browser"}:
        return "chrome"
    match = _MATCHER.match(normalized)
    return match.canonical_name if match is not None else normalized


def get_all_aliases() -> list[str]:
    ordered: list[str] = []
    for entry in STATIC_APP_INDEX.values():
        ordered.append(entry.canonical_name)
        ordered.extend(entry.aliases)
    ordered.extend(["browser", "default browser", "web browser"])
    return list(dict.fromkeys(_normalize_alias(alias) for alias in ordered if alias))


def get_web_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for entry in STATIC_APP_INDEX.values():
        if entry.launch_target != "web" or not entry.url:
            continue
        for alias in entry.all_names():
            aliases[alias] = entry.url
    return aliases


class AppIndexer:
    """Discovers and caches installed Windows applications."""

    def __init__(self) -> None:
        self.apps: dict[str, AppRecord] = {}

    def load_cache(self) -> bool:
        if not APP_CACHE_FILE.exists():
            return False

        try:
            with open(APP_CACHE_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)

            self.apps = {}
            for item in data:
                record = AppRecord(**item)
                if os.path.exists(record.path) or record.type == "uwp":
                    record.exists = True
                    self.apps[record.normalized_name] = record

            state.app_index_count = len(self.apps)
            logger.info("Loaded %s apps from cache", len(self.apps))
            return True
        except Exception as exc:
            logger.error("Failed to load app cache: %s", exc)
            return False

    def save_cache(self) -> None:
        try:
            data = [asdict(record) for record in self.apps.values() if record.exists]
            with open(APP_CACHE_FILE, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=4)
            logger.info("App cache saved")
        except Exception as exc:
            logger.error("Failed to save app cache: %s", exc)

    def refresh(self) -> None:
        logger.info("Starting app index refresh...")
        self.apps.clear()
        self.scan_start_menu()
        self.scan_path_apps()
        self.save_cache()
        state.app_index_count = len(self.apps)
        logger.info("Indexed %s apps", state.app_index_count)

    def _aliases_for(self, normalized_name: str) -> list[str]:
        canonical = canonicalize_app_name(normalized_name, resolve_browser_alias=False)
        entry = STATIC_APP_INDEX.get(canonical)
        if entry is None:
            return []
        return list(dict.fromkeys(entry.all_names()))

    def _add_record(self, name: str, path: str, type: str, source: str) -> None:
        clean_name = Path(name).stem
        normalized_name = _normalize_alias(clean_name)
        if not normalized_name:
            return

        existing = self.apps.get(normalized_name)
        if existing is not None and not (existing.type == "executable" and type == "shortcut"):
            return

        self.apps[normalized_name] = AppRecord(
            name=clean_name,
            normalized_name=normalized_name,
            path=path,
            type=type,
            source=source,
            aliases=self._aliases_for(normalized_name),
        )

    def scan_start_menu(self) -> None:
        directories = [
            os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
            os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
        ]
        for directory in directories:
            if not os.path.exists(directory):
                continue
            for root, _, files in os.walk(directory):
                for file_name in files:
                    if file_name.lower().endswith(".lnk"):
                        self._add_record(file_name, os.path.join(root, file_name), "shortcut", "start_menu")

    def scan_path_apps(self) -> None:
        common_commands = [
            "notepad",
            "calc",
            "cmd",
            "mspaint",
            "explorer",
            "powershell",
            "taskmgr",
            "control",
        ]
        for command in common_commands:
            path = shutil.which(command)
            if path:
                self._add_record(command, path, "executable", "path")

    def get_all_records(self) -> list[AppRecord]:
        return list(self.apps.values())


__all__ = [
    "APP_CACHE_FILE",
    "AppIndexEntry",
    "AppIndexer",
    "AppMatch",
    "AppNameMatcher",
    "AppRecord",
    "STATIC_APP_INDEX",
    "canonicalize_app_name",
    "get_all_aliases",
    "get_app_entry",
    "get_app_matcher",
    "get_web_aliases",
]
