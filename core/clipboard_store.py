"""
Persistent clipboard history storage backed by SQLite.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from core import settings
from core.logger import get_logger
from core.paths import DATA_DIR

logger = get_logger(__name__)


class ClipboardStoreError(RuntimeError):
    """Raised when the clipboard history database cannot be accessed safely."""


class ClipboardStore:
    """SQLite-backed storage for clipboard history."""

    def __init__(self, db_path: str | Path | None = None, *, history_limit: int | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DATA_DIR / "clipboard.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_limit = history_limit
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.init_db()

    def init_db(self) -> None:
        """Create required tables and indexes if they do not exist."""
        schema = """
        CREATE TABLE IF NOT EXISTS clipboard_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_type TEXT NOT NULL,
            text_preview TEXT NOT NULL,
            full_text TEXT,
            content_hash TEXT NOT NULL,
            created_at REAL NOT NULL,
            source_app TEXT,
            is_sensitive INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_clipboard_created_at
            ON clipboard_items(created_at DESC, id DESC);

        CREATE INDEX IF NOT EXISTS idx_clipboard_hash
            ON clipboard_items(content_hash);
        """
        try:
            with self._lock:
                self._conn.executescript(schema)
        except sqlite3.DatabaseError as exc:
            logger.error("Clipboard database initialization failed: %s", exc)
            raise ClipboardStoreError(f"Failed to initialize clipboard database: {exc}") from exc

    def insert_item(self, item) -> Any:
        """Insert a clipboard item and return the same item with its database id populated."""
        sql = """
        INSERT INTO clipboard_items (
            content_type,
            text_preview,
            full_text,
            content_hash,
            created_at,
            source_app,
            is_sensitive
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        try:
            with self._lock:
                cursor = self._conn.execute(
                    sql,
                    (
                        str(item.content_type),
                        str(item.text_preview),
                        item.full_text,
                        str(item.hash),
                        float(item.created_at),
                        item.source_app,
                        1 if item.is_sensitive else 0,
                    ),
                )
                item.id = int(cursor.lastrowid)
                self._trim_history_locked()
                return item
        except sqlite3.DatabaseError as exc:
            logger.error("Clipboard insert failed: %s", exc)
            raise ClipboardStoreError(f"Failed to insert clipboard item: {exc}") from exc

    def list_recent(self, limit: int) -> list[Any]:
        """Return the most recent clipboard items, newest first."""
        safe_limit = max(1, int(limit or 1))
        sql = """
        SELECT id, content_type, text_preview, full_text, content_hash, created_at, source_app, is_sensitive
        FROM clipboard_items
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """
        try:
            with self._lock:
                rows = self._conn.execute(sql, (safe_limit,)).fetchall()
            return [self._row_to_item(row) for row in rows]
        except sqlite3.DatabaseError as exc:
            logger.error("Clipboard history query failed: %s", exc)
            raise ClipboardStoreError(f"Failed to read clipboard history: {exc}") from exc

    def get_by_id(self, item_id: int) -> Any | None:
        """Return a single clipboard item by id, or None when not found."""
        sql = """
        SELECT id, content_type, text_preview, full_text, content_hash, created_at, source_app, is_sensitive
        FROM clipboard_items
        WHERE id = ?
        """
        try:
            with self._lock:
                row = self._conn.execute(sql, (int(item_id),)).fetchone()
            return self._row_to_item(row) if row is not None else None
        except sqlite3.DatabaseError as exc:
            logger.error("Clipboard item lookup failed: %s", exc)
            raise ClipboardStoreError(f"Failed to load clipboard item {item_id}: {exc}") from exc

    def delete_all(self) -> int:
        """Delete all clipboard history rows and return the number removed."""
        try:
            with self._lock:
                count_row = self._conn.execute("SELECT COUNT(*) AS count FROM clipboard_items").fetchone()
                removed = int(count_row["count"]) if count_row is not None else 0
                self._conn.execute("DELETE FROM clipboard_items")
                return removed
        except sqlite3.DatabaseError as exc:
            logger.error("Clipboard history delete failed: %s", exc)
            raise ClipboardStoreError(f"Failed to clear clipboard history: {exc}") from exc

    def remove_duplicates(self) -> int:
        """Remove older rows that share the same content hash, keeping the newest copy."""
        sql = """
        DELETE FROM clipboard_items
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM clipboard_items
            GROUP BY content_hash
        )
        """
        try:
            with self._lock:
                before = self.stats()["count"]
                self._conn.execute(sql)
                after = self.stats()["count"]
                return max(0, before - after)
        except sqlite3.DatabaseError as exc:
            logger.error("Clipboard duplicate cleanup failed: %s", exc)
            raise ClipboardStoreError(f"Failed to remove duplicate clipboard items: {exc}") from exc

    def stats(self) -> dict[str, Any]:
        """Return basic clipboard history statistics."""
        sql = """
        SELECT
            COUNT(*) AS count,
            COALESCE(SUM(is_sensitive), 0) AS sensitive_count,
            COALESCE(MAX(created_at), 0) AS newest_created_at
        FROM clipboard_items
        """
        try:
            with self._lock:
                row = self._conn.execute(sql).fetchone()
            if row is None:
                return {"count": 0, "sensitive_count": 0, "newest_created_at": 0.0}
            return {
                "count": int(row["count"] or 0),
                "sensitive_count": int(row["sensitive_count"] or 0),
                "newest_created_at": float(row["newest_created_at"] or 0.0),
            }
        except sqlite3.DatabaseError as exc:
            logger.error("Clipboard stats query failed: %s", exc)
            raise ClipboardStoreError(f"Failed to compute clipboard stats: {exc}") from exc

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.DatabaseError as exc:
                logger.warning("Clipboard database close failed: %s", exc)

    def _trim_history_locked(self) -> None:
        limit = int(self.history_limit if self.history_limit is not None else settings.get("clipboard_history_limit") or 100)
        if limit <= 0:
            return

        trim_sql = """
        DELETE FROM clipboard_items
        WHERE id IN (
            SELECT id
            FROM clipboard_items
            ORDER BY created_at DESC, id DESC
            LIMIT -1 OFFSET ?
        )
        """
        self._conn.execute(trim_sql, (limit,))

    @staticmethod
    def _row_to_item(row) -> Any:
        from core.clipboard import ClipboardItem

        return ClipboardItem(
            id=int(row["id"]),
            content_type=str(row["content_type"]),
            text_preview=str(row["text_preview"]),
            full_text=row["full_text"],
            hash=str(row["content_hash"]),
            created_at=float(row["created_at"]),
            source_app=str(row["source_app"]) if row["source_app"] else None,
            is_sensitive=bool(row["is_sensitive"]),
        )
