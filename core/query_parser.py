"""
Natural-language query parsing for smart Windows file discovery.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from functools import lru_cache
from pathlib import Path

from core import settings

FILE_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "pdf": ("pdf",),
    "word": ("doc", "docx", "rtf"),
    "doc": ("doc", "docx"),
    "excel": ("xls", "xlsx", "csv"),
    "xls": ("xls", "xlsx", "csv"),
    "powerpoint": ("ppt", "pptx"),
    "ppt": ("ppt", "pptx"),
    "image": ("png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff"),
    "video": ("mp4", "mkv", "avi", "mov", "wmv", "webm"),
    "music": ("mp3", "wav", "flac", "aac", "m4a", "ogg"),
    "txt": ("txt", "md", "log"),
    "zip": ("zip", "rar", "7z", "tar", "gz"),
}

FILE_TYPE_TOKEN_MAP: dict[str, str] = {
    "pdf": "pdf",
    "document": "word",
    "doc": "doc",
    "docx": "doc",
    "word": "word",
    "excel": "excel",
    "spreadsheet": "excel",
    "spreadsheets": "excel",
    "workbook": "excel",
    "workbooks": "excel",
    "xls": "xls",
    "xlsx": "xls",
    "ppt": "ppt",
    "pptx": "ppt",
    "powerpoint": "powerpoint",
    "presentation": "powerpoint",
    "presentations": "powerpoint",
    "image": "image",
    "images": "image",
    "photo": "image",
    "photos": "image",
    "picture": "image",
    "pictures": "image",
    "screenshot": "image",
    "screenshots": "image",
    "video": "video",
    "videos": "video",
    "movie": "video",
    "movies": "video",
    "music": "music",
    "song": "music",
    "songs": "music",
    "audio": "music",
    "txt": "txt",
    "text": "txt",
    "note": "txt",
    "notes": "txt",
    "zip": "zip",
    "archive": "zip",
    "archives": "zip",
}

FOLDER_ALIASES: dict[str, str] = {
    "desktop": "desktop",
    "desktop folder": "desktop",
    "documents": "documents",
    "document": "documents",
    "docs": "documents",
    "downloads": "downloads",
    "download": "downloads",
    "pictures": "pictures",
    "picture": "pictures",
    "photos": "pictures",
    "images": "pictures",
    "music": "music",
    "songs": "music",
    "videos": "videos",
    "video": "videos",
}

FILE_NOUN_HINTS = {
    "archive",
    "audio",
    "cv",
    "document",
    "documents",
    "file",
    "files",
    "image",
    "images",
    "invoice",
    "music",
    "note",
    "notes",
    "pdf",
    "photo",
    "photos",
    "picture",
    "pictures",
    "presentation",
    "presentations",
    "report",
    "reports",
    "resume",
    "resumes",
    "screenshot",
    "screenshots",
    "sheet",
    "sheets",
    "song",
    "songs",
    "spreadsheet",
    "spreadsheet",
    "video",
    "videos",
    "workbook",
}

FILE_EVIDENCE_HINTS = {
    "cv",
    "doc",
    "docx",
    "document",
    "documents",
    "file",
    "files",
    "folder",
    "folders",
    "pdf",
    "txt",
    "xlsx",
    "zip",
}

MEDIA_HINTS = {
    "audio",
    "movie",
    "music",
    "player",
    "playback",
    "podcast",
    "song",
    "songs",
    "spotify",
    "track",
    "tracks",
    "video",
    "videos",
    "youtube",
}

PLAYBACK_VERBS = {
    "continue",
    "mute",
    "next",
    "pause",
    "play",
    "previous",
    "resume",
    "skip",
    "stop",
    "unmute",
}

STOPWORDS = {
    "a",
    "all",
    "an",
    "and",
    "any",
    "by",
    "file",
    "files",
    "find",
    "for",
    "from",
    "in",
    "last",
    "latest",
    "list",
    "me",
    "modified",
    "my",
    "newest",
    "of",
    "on",
    "open",
    "recent",
    "recently",
    "search",
    "show",
    "that",
    "the",
    "under",
    "with",
}

_SIZE_PATTERN = re.compile(
    r"\b(?:over|above|greater than|larger than|bigger than|under|below|less than|smaller than|at least|at most)\s+(\d+(?:\.\d+)?)\s*(b|kb|mb|gb|tb)\b",
    flags=re.IGNORECASE,
)


@dataclass(slots=True)
class SearchQuery:
    raw_text: str
    keywords: list[str] = field(default_factory=list)
    file_types: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)
    date_from: datetime | None = None
    date_to: datetime | None = None
    min_size_bytes: int | None = None
    max_size_bytes: int | None = None
    sort_by: str = "relevance"
    limit: int = 5
    intent_action: str = "find"
    whole_drive: bool = False

    @property
    def phrase(self) -> str:
        return " ".join(self.keywords).strip()

    @property
    def extensions(self) -> set[str]:
        extensions: set[str] = set()
        for file_type in self.file_types:
            extensions.update(FILE_TYPE_ALIASES.get(file_type, (file_type,)))
        return extensions

    @property
    def open_after_search(self) -> bool:
        return self.intent_action == "open"

    def to_dict(self) -> dict[str, object]:
        return {
            "raw_text": self.raw_text,
            "keywords": list(self.keywords),
            "file_types": list(self.file_types),
            "folders": list(self.folders),
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "min_size_bytes": self.min_size_bytes,
            "max_size_bytes": self.max_size_bytes,
            "sort_by": self.sort_by,
            "limit": self.limit,
            "intent_action": self.intent_action,
            "whole_drive": self.whole_drive,
        }


def parse_natural_query(text: str, *, now: datetime | None = None) -> SearchQuery:
    return _parse_query_cached(text)


@lru_cache(maxsize=256)
def _parse_query_cached(text: str) -> SearchQuery:
    cleaned = " ".join(str(text or "").strip().split())
    lowered = cleaned.lower()
    now = datetime.now()
    query = SearchQuery(
        raw_text=cleaned,
        limit=int(settings.get("search_default_limit") or 5),
    )

    query.intent_action = _extract_action(lowered)
    query.sort_by = _extract_sort(lowered)
    query.limit = _extract_limit(lowered, default=query.limit)
    query.file_types = _extract_file_types(lowered)
    query.folders = _extract_folders(cleaned)
    query.date_from, query.date_to = _extract_date_range(lowered, now)
    query.min_size_bytes, query.max_size_bytes = _extract_size_bounds(lowered)
    query.whole_drive = _mentions_whole_drive(lowered)
    query.keywords = _extract_keywords(cleaned, query)

    if "downloaded" in lowered and "downloads" not in query.folders:
        query.folders.append("downloads")
    if "screenshot" in lowered and "image" not in query.file_types:
        query.file_types.append("image")
    if query.sort_by == "relevance" and any(token in lowered for token in ("latest", "newest", "recent", "recently")):
        query.sort_by = "latest"

    query.file_types = _dedupe(query.file_types)
    query.folders = _dedupe(query.folders)
    query.keywords = _dedupe(query.keywords)
    return query


def is_probable_file_search(text: str) -> bool:
    query = parse_natural_query(text)
    lowered = query.raw_text.lower()
    tokens = set(_tokenize(lowered))
    token_set = set(tokens)

    if _looks_like_playback_command(token_set):
        if not _has_strong_file_evidence(query, token_set):
            return False

    file_signal_score = 0
    if query.file_types:
        file_signal_score += 2
    if query.folders:
        file_signal_score += 2
    if query.date_from or query.date_to:
        file_signal_score += 1
    if query.min_size_bytes is not None or query.max_size_bytes is not None:
        file_signal_score += 1
    if query.whole_drive:
        file_signal_score += 1
    if any(token in FILE_NOUN_HINTS for token in tokens):
        file_signal_score += 2
    if any(re.fullmatch(r"[a-z0-9_\-]+\.[a-z0-9]{1,6}", token) for token in tokens):
        file_signal_score += 2
    if lowered.startswith(("find my ", "open my ", "show my ", "list my ")):
        file_signal_score += 1
    if query.intent_action == "open" and any(
        marker in lowered for marker in ("latest", "newest", "downloaded", "yesterday", "today", "recent")
    ):
        file_signal_score += 1

    return file_signal_score >= 2


def _extract_action(lowered: str) -> str:
    if lowered.startswith(("open ", "launch ", "start ")):
        return "open"
    if lowered.startswith(("show ", "list ")):
        return "show"
    return "find"


def _extract_sort(lowered: str) -> str:
    if any(token in lowered for token in ("largest", "biggest")):
        return "largest"
    if "smallest" in lowered:
        return "smallest"
    if any(token in lowered for token in ("oldest", "earliest")):
        return "oldest"
    if "last downloaded" in lowered:
        return "latest"
    if any(token in lowered for token in ("latest", "newest", "recent", "recently")):
        return "latest"
    return "relevance"


def _extract_limit(lowered: str, *, default: int) -> int:
    match = re.search(r"\b(?:top|first|show|list)\s+(\d{1,2})\b", lowered)
    if match:
        return max(1, min(int(match.group(1)), 25))
    return max(1, default)


def _extract_file_types(lowered: str) -> list[str]:
    found: list[str] = []
    for token in _tokenize(lowered):
        canonical = FILE_TYPE_TOKEN_MAP.get(token)
        if canonical:
            found.append(canonical)
            continue
        if token.startswith("."):
            token = token[1:]
        if token in FILE_TYPE_ALIASES:
            found.append(token)
    return found


def _extract_folders(text: str) -> list[str]:
    lowered = text.lower()
    folders: list[str] = []
    for alias, canonical in sorted(FOLDER_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b(?:in|from|under|on)\s+{re.escape(alias)}\b", lowered):
            folders.append(canonical)

    path_match = re.search(r"\b(?:in|from|under|on)\s+([A-Za-z]:\\[^\n]+)$", text)
    if path_match:
        folders.append(path_match.group(1).strip())
    return folders


def _extract_date_range(lowered: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    today_start = datetime.combine(now.date(), time.min)
    tomorrow_start = today_start + timedelta(days=1)

    if "today" in lowered:
        return today_start, tomorrow_start
    if "yesterday" in lowered:
        return today_start - timedelta(days=1), today_start
    if "last week" in lowered:
        week_end = today_start - timedelta(days=today_start.weekday())
        week_start = week_end - timedelta(days=7)
        return week_start, week_end
    if "this week" in lowered:
        week_start = today_start - timedelta(days=today_start.weekday())
        return week_start, tomorrow_start
    if any(token in lowered for token in ("recent", "recently")):
        return today_start - timedelta(days=14), tomorrow_start
    return None, None


def _extract_size_bounds(lowered: str) -> tuple[int | None, int | None]:
    min_size: int | None = None
    max_size: int | None = None
    for match in _SIZE_PATTERN.finditer(lowered):
        amount = float(match.group(1))
        unit = match.group(2).lower()
        bound = _size_to_bytes(amount, unit)
        clause = match.group(0).lower()
        if any(token in clause for token in ("over", "above", "greater", "larger", "bigger", "at least")):
            min_size = max(min_size or 0, bound)
        else:
            max_size = min(max_size, bound) if max_size is not None else bound
    return min_size, max_size


def _extract_keywords(text: str, query: SearchQuery) -> list[str]:
    lowered = text.lower()
    lowered = re.sub(r"^(?:find|search|show|list|open|launch|start)\s+", "", lowered)
    lowered = re.sub(r"\b(?:modified|updated|created|downloaded)\b", " ", lowered)
    lowered = re.sub(r"\b(?:today|yesterday|recent|recently|latest|newest|oldest|largest|smallest|last week|this week)\b", " ", lowered)
    lowered = re.sub(r"\b(?:in|from|under|on)\s+[a-z]:\\[^\n]+$", " ", lowered)

    for alias in sorted(FOLDER_ALIASES, key=len, reverse=True):
        lowered = re.sub(rf"\b{re.escape(alias)}\b", " ", lowered)
    for token in sorted(FILE_TYPE_TOKEN_MAP, key=len, reverse=True):
        lowered = re.sub(rf"\b{re.escape(token)}\b", " ", lowered)

    lowered = _SIZE_PATTERN.sub(" ", lowered)
    lowered = re.sub(r"[^a-z0-9._\-\\/: ]+", " ", lowered)

    keywords: list[str] = []
    for token in lowered.split():
        normalized = token.strip().strip(".")
        if not normalized or normalized in STOPWORDS:
            continue
        if normalized.isdigit():
            keywords.append(normalized)
            continue
        if normalized in FOLDER_ALIASES or normalized in FILE_TYPE_TOKEN_MAP:
            continue
        if re.fullmatch(r"[a-z0-9_\-]+\.[a-z0-9]{1,6}", normalized):
            stem = Path(normalized).stem.lower()
            if stem and stem not in STOPWORDS:
                keywords.append(stem)
            continue
        if re.fullmatch(r"[a-z0-9]{1,6}", normalized) and normalized in query.extensions:
            continue
        keywords.append(normalized)
    return keywords


def _mentions_whole_drive(lowered: str) -> bool:
    return any(
        phrase in lowered
        for phrase in (
            "entire drive",
            "whole drive",
            "entire disk",
            "whole computer",
            "everywhere on c:",
            "search everything",
        )
    )


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9._\\:-]+", text.lower())


def _size_to_bytes(amount: float, unit: str) -> int:
    multipliers = {
        "b": 1,
        "kb": 1024,
        "mb": 1024**2,
        "gb": 1024**3,
        "tb": 1024**4,
    }
    return int(amount * multipliers[unit])


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item:
            continue
        lowered = item.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(item)
    return ordered


def _looks_like_playback_command(tokens: set[str]) -> bool:
    return bool(tokens & PLAYBACK_VERBS)


def _has_strong_file_evidence(query: SearchQuery, tokens: set[str]) -> bool:
    if query.folders:
        return True
    if any(token in FILE_EVIDENCE_HINTS for token in tokens):
        return True
    if any(re.fullmatch(r"[a-z0-9_\-]+\.[a-z0-9]{1,6}", token) for token in tokens):
        return True
    if query.date_from or query.date_to or query.min_size_bytes is not None or query.max_size_bytes is not None:
        return True
    return False
