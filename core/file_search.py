"""
Scoped, ranked file search with SQLite index and live scan fallback.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from rapidfuzz import fuzz

from core import settings, state
from core.file_index import FileIndex, IndexedFile
from core.files import FileManager
from core.logger import get_logger
from core.path_resolver import PathResolver
from core.query_parser import SearchQuery, parse_natural_query

logger = get_logger(__name__)

_TYPE_ROOT_PREFERENCE = {
    "image": ("pictures", "downloads", "desktop"),
    "video": ("videos", "downloads", "desktop"),
    "music": ("music", "downloads", "desktop"),
    "pdf": ("documents", "downloads", "desktop"),
    "word": ("documents", "downloads", "desktop"),
    "doc": ("documents", "downloads", "desktop"),
    "excel": ("documents", "downloads", "desktop"),
    "xls": ("documents", "downloads", "desktop"),
    "powerpoint": ("documents", "downloads", "desktop"),
    "ppt": ("documents", "downloads", "desktop"),
    "txt": ("documents", "desktop", "downloads"),
    "zip": ("downloads", "documents", "desktop"),
}


@dataclass(slots=True)
class SearchResult:
    path: str
    name: str
    score: float
    size: int
    modified_time: float
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "name": self.name,
            "score": round(self.score, 2),
            "size": self.size,
            "modified_time": self.modified_time,
            "reason": self.reason,
        }


@dataclass(slots=True)
class SearchResponse:
    query: SearchQuery
    results: list[SearchResult] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    used_index: bool = False
    live_scanned: bool = False
    partial: bool = False
    message: str = ""


class SmartFileSearch:
    """Natural-language file discovery over safe Windows user scopes."""
    _global_index_lock = threading.Lock()
    _global_index_running = False

    def __init__(
        self,
        *,
        file_index: FileIndex | None = None,
        file_manager: FileManager | None = None,
        path_resolver: PathResolver | None = None,
        max_live_candidates: int = 200,
        max_files_examined: int = 50000,
    ) -> None:
        self._files = file_manager or FileManager(path_resolver=path_resolver)
        self._resolver = path_resolver or self._files.resolver
        self._index = file_index or FileIndex()
        self._max_live_candidates = max_live_candidates
        self._max_files_examined = max_files_examined
        self._index_lock = threading.Lock()
        self._index_thread: threading.Thread | None = None

    def search(self, text: str) -> SearchResponse:
        query = parse_natural_query(text)
        logger.info("File search: %s", query.raw_text)
        logger.info("Parsed query: %s", query.to_dict())

        if query.whole_drive:
            return SearchResponse(
                query=query,
                message="Scanning the entire drive is disabled by default. Tell me which folder to search, or confirm a full-drive scan explicitly.",
            )

        scope_roots = self._resolve_scope_roots(query)
        scope_labels = [self._resolver.describe_path(root) for root in scope_roots]
        logger.info("File search scope: %s", scope_labels)

        indexed_results: list[SearchResult] = []
        used_index = False
        if settings.get("use_file_index"):
            if not getattr(state, "file_index_ready", False):
                self.ensure_index_ready_async()
            indexed_results = self.search_indexed(query, roots=scope_roots)
            used_index = bool(indexed_results)

        live_results: list[SearchResult] = []
        partial = False
        need_live_scan = (
            len(indexed_results) < query.limit
            or query.sort_by in {"latest", "oldest", "largest", "smallest"}
            or not getattr(state, "file_index_ready", False)
        )
        if need_live_scan:
            live_results, partial = self.search_live(query, roots=scope_roots)

        merged = self.merge_results(indexed_results, live_results, query)
        top_result = merged[0].name if merged else ""
        logger.info("Results found: %d", len(merged))
        if top_result:
            logger.info("Top result: %s", top_result)

        return SearchResponse(
            query=query,
            results=merged[: query.limit],
            scope=scope_labels,
            used_index=used_index,
            live_scanned=need_live_scan,
            partial=partial,
            message=self._build_message(merged[: query.limit], scope_labels, partial),
        )

    def search_live(
        self,
        query: SearchQuery,
        *,
        roots: Iterable[str | Path] | None = None,
    ) -> tuple[list[SearchResult], bool]:
        candidates: list[SearchResult] = []
        files_examined = 0
        partial = False
        roots_to_scan = [Path(root).resolve(strict=False) for root in (roots or self._resolve_scope_roots(query))]

        for root in roots_to_scan:
            queue: list[Path] = [root]
            while queue:
                current = queue.pop()
                try:
                    with os.scandir(current) as entries:
                        for entry in entries:
                            entry_path = Path(entry.path)
                            if self._is_hidden(entry_path):
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                queue.append(entry_path)
                                continue

                            files_examined += 1
                            if files_examined > self._max_files_examined:
                                partial = True
                                logger.warning("Live file search stopped early after %d files", files_examined)
                                return self.rank(candidates, query), partial

                            record = self._record_from_dir_entry(entry)
                            if record is None or not self._coarse_match(record, query):
                                continue

                            ranked = self._score_record(record, query)
                            if ranked is None:
                                continue
                            candidates.append(ranked)
                            if len(candidates) > self._max_live_candidates:
                                candidates = self.rank(candidates, query)[: self._max_live_candidates]
                except OSError as exc:
                    logger.debug("Skipping %s during live search: %s", current, exc)

        return self.rank(candidates, query), partial

    def search_indexed(
        self,
        query: SearchQuery,
        *,
        roots: Iterable[str | Path] | None = None,
    ) -> list[SearchResult]:
        indexed = self._index.search_index(query, roots=roots, limit=max(query.limit, 10))
        return self.rank(indexed, query)

    def rank(self, results: Iterable[SearchResult | IndexedFile | dict[str, object]], query: SearchQuery) -> list[SearchResult]:
        ranked: list[SearchResult] = []
        for result in results:
            if isinstance(result, SearchResult):
                ranked.append(result)
                continue
            scored = self._score_record(result, query)
            if scored is not None:
                ranked.append(scored)

        if query.sort_by == "latest":
            ranked.sort(key=lambda item: (item.score, item.modified_time), reverse=True)
        elif query.sort_by == "oldest":
            ranked.sort(key=lambda item: (item.score, -item.modified_time), reverse=True)
        elif query.sort_by == "largest":
            ranked.sort(key=lambda item: (item.score, item.size, item.modified_time), reverse=True)
        elif query.sort_by == "smallest":
            ranked.sort(key=lambda item: (item.score, -item.size, item.modified_time), reverse=True)
        else:
            ranked.sort(key=lambda item: (item.score, item.modified_time), reverse=True)
        return ranked

    def merge_results(
        self,
        indexed_results: Iterable[SearchResult],
        live_results: Iterable[SearchResult],
        query: SearchQuery,
    ) -> list[SearchResult]:
        deduped: dict[str, SearchResult] = {}
        for result in [*indexed_results, *live_results]:
            existing = deduped.get(result.path.lower())
            if existing is None or result.score > existing.score:
                deduped[result.path.lower()] = result
        return self.rank(deduped.values(), query)

    def open_result(self, result: SearchResult) -> object:
        logger.info("Open file search result: %s", result.path)
        return self._files.open_file(result.path)

    def choose_best(self, results: list[SearchResult], query: SearchQuery | None = None) -> SearchResult | None:
        if not results:
            return None
        if len(results) == 1:
            return results[0]
        top = results[0]
        second = results[1]
        gap = top.score - second.score
        if top.score >= 82 and gap >= 8:
            return top
        if query and query.sort_by == "latest" and top.modified_time > second.modified_time:
            return top
        if query and query.sort_by == "oldest" and top.modified_time < second.modified_time:
            return top
        if query and query.sort_by == "largest" and top.size > second.size:
            return top
        if query and query.sort_by == "smallest" and top.size < second.size:
            return top
        if query and query.sort_by in {"latest", "largest", "smallest", "oldest"} and gap >= 5:
            return top
        return None

    def ensure_index_ready_async(self, *, force: bool = False) -> None:
        if not settings.get("use_file_index"):
            state.file_index_ready = False
            return

        with self._index_lock:
            if self._index_thread and self._index_thread.is_alive():
                return
            if getattr(state, "file_index_ready", False) and not force:
                return
            with SmartFileSearch._global_index_lock:
                if SmartFileSearch._global_index_running:
                    return
                SmartFileSearch._global_index_running = True

            def _worker() -> None:
                try:
                    self._update_index_with_retry(self._default_roots())
                    state.file_index_ready = True
                except Exception as exc:
                    state.file_index_ready = False
                    logger.warning("Background file indexing failed: %s", exc)
                finally:
                    with SmartFileSearch._global_index_lock:
                        SmartFileSearch._global_index_running = False

            self._index_thread = threading.Thread(
                target=_worker,
                name="nova-file-index",
                daemon=True,
            )
            self._index_thread.start()

    def _update_index_with_retry(self, roots: list[Path]) -> None:
        max_attempts = max(1, int(settings.get("file_index_retry_attempts") or 3))
        base_delay_seconds = float(settings.get("file_index_retry_delay_seconds") or 0.25)
        attempt = 0
        while True:
            attempt += 1
            try:
                self._index.update_index(roots)
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= max_attempts:
                    raise
                delay = base_delay_seconds * attempt
                logger.warning(
                    "File index busy (attempt %d/%d). Retrying in %.2fs.",
                    attempt,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)

    def _resolve_scope_roots(self, query: SearchQuery) -> list[Path]:
        if query.folders:
            roots = self._resolve_folder_terms(query.folders)
            if roots:
                return roots

        default_scope = str(settings.get("search_default_scope") or "user_folders").strip().lower()
        if default_scope == "user_folders":
            roots = self._preferred_roots_for_query(query)
            if roots:
                return roots
        return self._default_roots()

    def _preferred_roots_for_query(self, query: SearchQuery) -> list[Path]:
        preferred: list[Path] = []
        known_locations = self._resolver.known_locations
        selected_keys: list[str] = []
        for file_type in query.file_types:
            selected_keys.extend(_TYPE_ROOT_PREFERENCE.get(file_type, ()))
        selected_keys.extend(("documents", "downloads", "desktop", "pictures", "videos", "music"))

        seen: set[str] = set()
        for key in selected_keys:
            root = known_locations.get(key)
            if root is None or not root.exists():
                continue
            lowered = str(root).lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            preferred.append(root)
        return preferred

    def _default_roots(self) -> list[Path]:
        known_locations = self._resolver.known_locations
        roots = [known_locations[key] for key in ("desktop", "documents", "downloads", "pictures", "videos", "music") if key in known_locations]
        return [root for root in roots if root.exists() and root.is_dir()]

    def _resolve_folder_terms(self, folders: Iterable[str]) -> list[Path]:
        resolved: list[Path] = []
        seen: set[str] = set()
        for folder in folders:
            text = str(folder or "").strip()
            if not text:
                continue
            try:
                path = self._resolver.resolve(text)
            except Exception:
                continue
            if not path.exists() or not path.is_dir():
                continue
            lowered = str(path).lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            resolved.append(path)
        return resolved

    def _coarse_match(self, record: IndexedFile, query: SearchQuery) -> bool:
        if not self._matches_filters(record, query):
            return False
        if not query.keywords:
            return True

        name = record.name.lower()
        folder = record.folder.lower()
        phrase = query.phrase.lower()
        if phrase and (phrase in name or phrase in folder):
            return True

        for keyword in query.keywords:
            lowered = keyword.lower()
            if lowered in name or lowered in folder:
                return True

        if phrase:
            return fuzz.partial_ratio(phrase, Path(name).stem.lower()) >= 65
        return False

    def _score_record(self, record: SearchResult | IndexedFile | dict[str, object], query: SearchQuery) -> SearchResult | None:
        if isinstance(record, SearchResult):
            return record

        candidate = self._normalize_candidate(record)
        if candidate is None or not self._matches_filters(candidate, query):
            return None

        score = 0.0
        reasons: list[str] = []
        name = candidate.name.lower()
        stem = Path(candidate.name).stem.lower()
        phrase = query.phrase.lower()

        if phrase:
            if stem == phrase or name == phrase or name == f"{phrase}.{candidate.extension}":
                score += 45
                reasons.append("exact filename")
            elif phrase in stem:
                score += 32
                reasons.append("phrase match")

            fuzzy_score = max(
                fuzz.token_set_ratio(phrase, stem),
                fuzz.partial_ratio(phrase, stem),
            )
            score += fuzzy_score * 0.28
            if fuzzy_score >= 80 and "phrase match" not in reasons and "exact filename" not in reasons:
                reasons.append("fuzzy filename match")

        keyword_hits = 0
        for keyword in query.keywords:
            lowered = keyword.lower()
            if lowered in name:
                keyword_hits += 1
        if keyword_hits:
            score += min(18.0, keyword_hits * 7.0)
            reasons.append("keyword hit")

        if query.file_types:
            if candidate.extension in query.extensions:
                score += 18
                reasons.append(candidate.extension.upper() if candidate.extension else "type match")
            else:
                return None

        explicit_roots = self._resolve_folder_terms(query.folders)
        if explicit_roots and any(self._is_relative_to(Path(candidate.path), root) for root in explicit_roots):
            score += 15
            reasons.append("requested folder")
        else:
            preferred_roots = self._preferred_roots_for_query(query)
            if any(self._is_relative_to(Path(candidate.path), root) for root in preferred_roots[:3]):
                score += 4

        age_days = max(0.0, (time.time() - candidate.modified_time) / 86400.0)
        recency_boost = max(0.0, 12.0 - min(age_days, 12.0))
        if query.sort_by in {"latest", "relevance"} or query.date_from or query.date_to:
            score += recency_boost
            if age_days <= 1.0:
                reasons.append("recent")

        depth_bonus = max(0.0, 5.0 - min(self._depth_from_scope(Path(candidate.path), explicit_roots or self._default_roots()), 5.0))
        score += depth_bonus

        if query.sort_by == "largest":
            score += min(candidate.size / (1024 * 1024 * 20), 10.0)
        elif query.sort_by == "smallest":
            score += max(0.0, 10.0 - min(candidate.size / (1024 * 1024 * 20), 10.0))

        reason = ", ".join(dict.fromkeys(reasons)) or "metadata match"
        return SearchResult(
            path=candidate.path,
            name=candidate.name,
            score=round(score, 2),
            size=candidate.size,
            modified_time=candidate.modified_time,
            reason=reason,
        )

    def _matches_filters(self, record: IndexedFile, query: SearchQuery) -> bool:
        if query.extensions and record.extension not in query.extensions:
            return False
        if query.date_from is not None and record.modified_time < query.date_from.timestamp():
            return False
        if query.date_to is not None and record.modified_time >= query.date_to.timestamp():
            return False
        if query.min_size_bytes is not None and record.size < query.min_size_bytes:
            return False
        if query.max_size_bytes is not None and record.size > query.max_size_bytes:
            return False
        return True

    def _normalize_candidate(self, record: IndexedFile | dict[str, object]) -> IndexedFile | None:
        if isinstance(record, IndexedFile):
            return record
        try:
            path = str(record["path"])
            name = str(record.get("name") or Path(path).name)
            extension = str(record.get("extension") or Path(name).suffix.lower().lstrip("."))
            size = int(record.get("size") or 0)
            modified_time = float(record.get("modified_time") or 0.0)
            folder = str(record.get("folder") or Path(path).parent)
        except Exception:
            return None
        return IndexedFile(
            path=path,
            name=name,
            extension=extension.lower(),
            size=size,
            modified_time=modified_time,
            folder=folder,
        )

    @staticmethod
    def _record_from_dir_entry(entry: os.DirEntry[str]) -> IndexedFile | None:
        try:
            stats = entry.stat(follow_symlinks=False)
        except OSError:
            return None
        path = Path(entry.path).resolve(strict=False)
        return IndexedFile(
            path=str(path),
            name=path.name,
            extension=path.suffix.lower().lstrip("."),
            size=int(stats.st_size),
            modified_time=float(stats.st_mtime),
            folder=str(path.parent),
        )

    @staticmethod
    def _is_hidden(path: Path) -> bool:
        if path.name.startswith("."):
            return True
        try:
            stats = path.stat()
        except OSError:
            return False
        attributes = getattr(stats, "st_file_attributes", 0)
        return bool(attributes & 0x2 or attributes & 0x4)

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            return False

    def _depth_from_scope(self, path: Path, roots: Iterable[Path]) -> int:
        for root in roots:
            try:
                return len(path.resolve(strict=False).relative_to(root.resolve(strict=False)).parts)
            except ValueError:
                continue
        return len(path.parts)

    def _build_message(self, results: list[SearchResult], scope: list[str], partial: bool) -> str:
        if results:
            prefix = "I found matching files."
            if partial:
                return f"{prefix} The live scan was capped early, so the list may be incomplete."
            return prefix
        if partial:
            return "I did not find a match before the live scan hit its safety limit."
        if scope:
            return f"I couldn't find a matching file in {', '.join(scope)}."
        return "I couldn't find a matching file."
