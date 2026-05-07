"""
SQLite-backed metadata index for scoped file discovery.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from core.logger import get_logger
from core.paths import DATA_DIR
from core.query_parser import SearchQuery

logger = get_logger(__name__)

INDEX_DB_PATH = DATA_DIR / "file_index.db"


@dataclass(slots=True)
class IndexedFile:
    path: str
    name: str
    extension: str
    size: int
    modified_time: float
    folder: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "IndexedFile":
        return cls(
            path=str(row["path"]),
            name=str(row["name"]),
            extension=str(row["extension"]),
            size=int(row["size"]),
            modified_time=float(row["modified_time"]),
            folder=str(row["folder"]),
        )


class FileIndex:
    """Maintain a safe, queryable metadata index for user folders."""
    _write_lock = threading.Lock()

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path or INDEX_DB_PATH)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def build_index(self, paths: Iterable[str | Path]) -> dict[str, int]:
        roots = self._normalize_roots(paths)
        if not roots:
            return {"roots": 0, "files": 0, "removed": 0}

        indexed = 0
        removed = 0
        seen: set[str] = set()

        with self._write_lock:
            with self._connect() as conn:
                now = time.time()
                conn.execute("BEGIN")
                self._replace_roots(conn, roots, now)
                for root in roots:
                    removed += self._delete_root_records(conn, root)
                    batch: list[tuple[str, str, str, int, float, str, int, float]] = []
                    for record in self._scan_root(root):
                        normalized_path = record[0].lower()
                        if normalized_path in seen:
                            continue
                        seen.add(normalized_path)
                        batch.append(record)
                        indexed += 1
                        if len(batch) >= 500:
                            conn.executemany(
                                """
                                INSERT OR REPLACE INTO files
                                    (path, name, extension, size, modified_time, folder, hidden, indexed_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                batch,
                            )
                            batch.clear()
                    if batch:
                        conn.executemany(
                            """
                            INSERT OR REPLACE INTO files
                                (path, name, extension, size, modified_time, folder, hidden, indexed_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            batch,
                        )
                conn.commit()

        logger.info("File index rebuilt for %d roots with %d files", len(roots), indexed)
        return {"roots": len(roots), "files": indexed, "removed": removed}

    def update_index(self, paths: Iterable[str | Path] | None = None) -> dict[str, int]:
        roots = self._normalize_roots(paths or self._load_roots())
        if not roots:
            return {"roots": 0, "files": 0, "removed": 0}
        result = self.build_index(roots)
        result["removed"] += self.remove_missing(paths=roots)
        return result

    def search_index(
        self,
        query: SearchQuery,
        *,
        roots: Iterable[str | Path] | None = None,
        limit: int | None = None,
    ) -> list[IndexedFile]:
        candidate_limit = max(int(limit or query.limit or 5) * 25, 50)
        clauses = ["hidden = 0"]
        params: list[object] = []

        extensions = sorted(query.extensions)
        if extensions:
            placeholders = ",".join("?" for _ in extensions)
            clauses.append(f"extension IN ({placeholders})")
            params.extend(extensions)

        if query.date_from is not None:
            clauses.append("modified_time >= ?")
            params.append(query.date_from.timestamp())
        if query.date_to is not None:
            clauses.append("modified_time < ?")
            params.append(query.date_to.timestamp())
        if query.min_size_bytes is not None:
            clauses.append("size >= ?")
            params.append(query.min_size_bytes)
        if query.max_size_bytes is not None:
            clauses.append("size <= ?")
            params.append(query.max_size_bytes)

        scope_roots = self._normalize_roots(roots or [])
        if scope_roots:
            scope_clauses: list[str] = []
            for root in scope_roots:
                root_text = str(root)
                scope_clauses.append("(path = ? OR path LIKE ?)")
                params.extend([root_text, root_text + os.sep + "%"])
            clauses.append("(" + " OR ".join(scope_clauses) + ")")

        if query.keywords:
            keyword_clauses: list[str] = []
            for keyword in query.keywords:
                keyword_clauses.append("(LOWER(name) LIKE ? OR LOWER(folder) LIKE ?)")
                fuzzy = f"%{keyword.lower()}%"
                params.extend([fuzzy, fuzzy])
            clauses.append("(" + " AND ".join(keyword_clauses) + ")")

        order_by = {
            "latest": "modified_time DESC, name ASC",
            "oldest": "modified_time ASC, name ASC",
            "largest": "size DESC, modified_time DESC",
            "smallest": "size ASC, modified_time DESC",
        }.get(query.sort_by, "modified_time DESC, name ASC")

        sql = f"""
            SELECT path, name, extension, size, modified_time, folder
            FROM files
            WHERE {' AND '.join(clauses)}
            ORDER BY {order_by}
            LIMIT ?
        """
        params.append(candidate_limit)

        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.DatabaseError as exc:
            logger.warning("File index search failed, recreating index database: %s", exc)
            self._recreate_database()
            return []

        return [IndexedFile.from_row(row) for row in rows]

    def remove_missing(self, paths: Iterable[str | Path] | None = None) -> int:
        scope_roots = self._normalize_roots(paths or [])
        deleted = 0
        with self._write_lock:
            with self._connect() as conn:
                sql = "SELECT path FROM files"
                params: list[object] = []
                if scope_roots:
                    root_clauses: list[str] = []
                    for root in scope_roots:
                        root_text = str(root)
                        root_clauses.append("(path = ? OR path LIKE ?)")
                        params.extend([root_text, root_text + os.sep + "%"])
                    sql += " WHERE " + " OR ".join(root_clauses)
                rows = conn.execute(sql, params).fetchall()
                for row in rows:
                    file_path = Path(str(row["path"]))
                    if file_path.exists():
                        continue
                    conn.execute("DELETE FROM files WHERE path = ?", (str(file_path),))
                    deleted += 1
                conn.commit()
        if deleted:
            logger.info("File index removed %d missing records", deleted)
        return deleted

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            total_files = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
            total_roots = int(conn.execute("SELECT COUNT(*) FROM index_roots").fetchone()[0])
        return {"files": total_files, "roots": total_roots}

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            self._ensure_schema_with_conn(conn)

    def _ensure_schema_with_conn(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                extension TEXT NOT NULL,
                size INTEGER NOT NULL,
                modified_time REAL NOT NULL,
                folder TEXT NOT NULL,
                hidden INTEGER NOT NULL DEFAULT 0,
                indexed_at REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_extension ON files(extension)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_modified_time ON files(modified_time)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS index_roots (
                root_path TEXT PRIMARY KEY,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._ensure_schema_with_conn(conn)
            return conn
        except sqlite3.DatabaseError:
            self._recreate_database()
            conn = sqlite3.connect(self._db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._ensure_schema_with_conn(conn)
            return conn

    def _recreate_database(self) -> None:
        try:
            if self._db_path.exists():
                self._db_path.unlink()
        except OSError as exc:
            logger.warning("Failed to remove corrupt file index %s: %s", self._db_path, exc)

    def _replace_roots(self, conn: sqlite3.Connection, roots: list[Path], indexed_at: float) -> None:
        conn.execute("DELETE FROM index_roots")
        conn.executemany(
            "INSERT INTO index_roots (root_path, updated_at) VALUES (?, ?)",
            [(str(root), indexed_at) for root in roots],
        )

    def _delete_root_records(self, conn: sqlite3.Connection, root: Path) -> int:
        root_text = str(root)
        cursor = conn.execute(
            "DELETE FROM files WHERE path = ? OR path LIKE ?",
            (root_text, root_text + os.sep + "%"),
        )
        return int(cursor.rowcount or 0)

    def _load_roots(self) -> list[Path]:
        with self._connect() as conn:
            rows = conn.execute("SELECT root_path FROM index_roots ORDER BY root_path").fetchall()
        return [Path(str(row["root_path"])).resolve(strict=False) for row in rows]

    def _normalize_roots(self, paths: Iterable[str | Path]) -> list[Path]:
        seen: set[str] = set()
        roots: list[Path] = []
        for candidate in paths:
            path = Path(candidate).resolve(strict=False)
            if not path.exists() or not path.is_dir():
                continue
            lowered = str(path).lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            roots.append(path)
        return roots

    def _scan_root(self, root: Path) -> Iterable[tuple[str, str, str, int, float, str, int, float]]:
        queue: list[Path] = [root]
        indexed_at = time.time()
        while queue:
            current = queue.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        entry_path = Path(entry.path)
                        if self._is_hidden(entry_path):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            # Skip Windows junction points / reparse points
                            # (e.g. "My Videos", "My Pictures", "My Music")
                            if self._is_reparse_point(entry):
                                continue
                            queue.append(entry_path)
                            continue
                        try:
                            stats = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        yield (
                            str(entry_path.resolve(strict=False)),
                            entry.name,
                            entry_path.suffix.lower().lstrip("."),
                            int(stats.st_size),
                            float(stats.st_mtime),
                            str(entry_path.parent.resolve(strict=False)),
                            0,
                            indexed_at,
                        )
            except OSError as exc:
                logger.debug("Skipping %s during index build: %s", current, exc)

    @staticmethod
    def _is_reparse_point(entry: os.DirEntry) -> bool:
        """Return True if the directory entry is a Windows junction / reparse point."""
        try:
            attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
            return bool(attrs & stat_file_attributes.FILE_ATTRIBUTE_REPARSE_POINT)
        except OSError:
            return True  # Can't stat → treat as inaccessible

    @staticmethod
    def _is_hidden(path: Path) -> bool:
        name = path.name
        if not name:
            return False
        if name.startswith("."):
            return True
        try:
            stats = path.stat()
        except OSError:
            return False
        attributes = getattr(stats, "st_file_attributes", 0)
        hidden_flag = stat_file_attributes.FILE_ATTRIBUTE_HIDDEN
        system_flag = stat_file_attributes.FILE_ATTRIBUTE_SYSTEM
        return bool(attributes & hidden_flag or attributes & system_flag)


class stat_file_attributes:
    FILE_ATTRIBUTE_HIDDEN = 0x2
    FILE_ATTRIBUTE_SYSTEM = 0x4
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
