"""
Low-level SQLite database manager for the local memory system.

Provides thread-safe connection management, schema initialisation,
migrations, backup, and vacuum operations.  All public methods are
safe to call from any thread.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Sequence

from core.logger import get_logger
from core.memory_schema import INITIAL_TABLES, MIGRATIONS, SCHEMA_VERSION
from core.paths import DATA_DIR

logger = get_logger(__name__)

MEMORY_DB_PATH = DATA_DIR / "memory.db"


class MemoryStore:
    """
    Thread-safe SQLite storage backend for the memory subsystem.

    * One shared connection per instance (WAL mode for concurrent reads).
    * All writes go through a single reentrant lock.
    * Schema bootstrap and migrations run automatically on first open.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path else MEMORY_DB_PATH
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._closed = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def init_db(self) -> None:
        """Open the database, apply schema and migrations."""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                timeout=10.0,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._apply_schema()
            logger.info("Memory DB initialised: %s", self._db_path)
        except sqlite3.DatabaseError as exc:
            logger.error("Memory DB corrupt or unreadable: %s", exc)
            self._handle_corrupt_db()

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
            self._closed = True
            logger.info("Memory DB closed")

    @property
    def is_open(self) -> bool:
        return self._conn is not None and not self._closed

    # ------------------------------------------------------------------ #
    # Query helpers                                                       #
    # ------------------------------------------------------------------ #

    def execute(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        commit: bool = True,
    ) -> sqlite3.Cursor:
        """Execute a single SQL statement under the write lock."""
        self._ensure_open()
        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)  # type: ignore[union-attr]
                if commit:
                    self._conn.commit()  # type: ignore[union-attr]
                return cursor
            except sqlite3.OperationalError as exc:
                if "database is locked" in str(exc).lower():
                    logger.warning("DB locked, retrying: %s", exc)
                    import time
                    time.sleep(0.1)
                    cursor = self._conn.execute(sql, params)  # type: ignore[union-attr]
                    if commit:
                        self._conn.commit()  # type: ignore[union-attr]
                    return cursor
                raise

    def query(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[dict[str, Any]]:
        """Execute a SELECT and return results as list of dicts."""
        self._ensure_open()
        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)  # type: ignore[union-attr]
                return [dict(row) for row in cursor.fetchall()]
            except sqlite3.Error as exc:
                logger.error("Query failed: %s | %s", sql[:120], exc)
                return []

    def query_one(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> dict[str, Any] | None:
        """Execute a SELECT and return the first row or None."""
        self._ensure_open()
        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)  # type: ignore[union-attr]
                row = cursor.fetchone()
                return dict(row) if row else None
            except sqlite3.Error as exc:
                logger.error("Query-one failed: %s | %s", sql[:120], exc)
                return None

    def query_scalar(
        self,
        sql: str,
        params: Sequence[Any] = (),
        default: Any = None,
    ) -> Any:
        """Return a single scalar value from a query."""
        self._ensure_open()
        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)  # type: ignore[union-attr]
                row = cursor.fetchone()
                return row[0] if row else default
            except sqlite3.Error as exc:
                logger.error("Scalar query failed: %s | %s", sql[:120], exc)
                return default

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for explicit transactions."""
        self._ensure_open()
        with self._lock:
            conn = self._conn  # type: ignore[assignment]
            try:
                conn.execute("BEGIN")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------ #
    # Maintenance                                                         #
    # ------------------------------------------------------------------ #

    def backup(self, target_path: str | Path | None = None) -> Path:
        """
        Create a hot backup of the database.

        Returns the path to the backup file.
        """
        self._ensure_open()
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        if target_path is None:
            target = self._db_path.parent / f"memory_backup_{ts}.db"
        else:
            target = Path(target_path)

        target.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            backup_conn = sqlite3.connect(str(target))
            try:
                self._conn.backup(backup_conn)  # type: ignore[union-attr]
            finally:
                backup_conn.close()

        logger.info("Memory DB backed up to: %s", target)
        return target

    def vacuum(self) -> None:
        """Reclaim space by vacuuming the database."""
        self._ensure_open()
        with self._lock:
            try:
                self._conn.execute("VACUUM")  # type: ignore[union-attr]
                logger.info("Memory DB vacuumed")
            except sqlite3.Error as exc:
                logger.warning("Vacuum failed: %s", exc)

    def table_exists(self, name: str) -> bool:
        """Check if a table exists in the database."""
        row = self.query_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        )
        return row is not None

    def row_count(self, table: str) -> int:
        """Return the number of rows in a table."""
        return self.query_scalar(f"SELECT COUNT(*) FROM [{table}]", default=0)

    # ------------------------------------------------------------------ #
    # Schema management                                                   #
    # ------------------------------------------------------------------ #

    def _apply_schema(self) -> None:
        """Bootstrap tables and run any pending migrations."""
        assert self._conn is not None

        # Create tables
        for ddl in INITIAL_TABLES:
            try:
                self._conn.execute(ddl)
            except sqlite3.Error as exc:
                logger.error("DDL failed: %s | %s", ddl[:80], exc)

        self._conn.commit()

        # Check current version
        current_version = self._get_schema_version()
        if current_version == 0:
            # First init — stamp version
            self._conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._conn.commit()
            logger.info("Memory schema initialised at version %d", SCHEMA_VERSION)
            return

        # Run pending migrations
        for from_ver, to_ver, sqls in MIGRATIONS:
            if current_version >= to_ver:
                continue
            if current_version != from_ver:
                continue
            logger.info("Migrating memory schema %d → %d", from_ver, to_ver)
            for sql in sqls:
                try:
                    self._conn.execute(sql)
                except sqlite3.Error as exc:
                    logger.error("Migration SQL failed: %s | %s", sql[:80], exc)
            self._conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (to_ver,),
            )
            self._conn.commit()
            current_version = to_ver

    def _get_schema_version(self) -> int:
        try:
            cursor = self._conn.execute(  # type: ignore[union-attr]
                "SELECT MAX(version) FROM schema_version"
            )
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except sqlite3.Error:
            return 0

    # ------------------------------------------------------------------ #
    # Error recovery                                                      #
    # ------------------------------------------------------------------ #

    def _handle_corrupt_db(self) -> None:
        """Move a corrupt database aside and create a fresh one."""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

        corrupt_path = self._db_path.with_suffix(".db.corrupt")
        try:
            if self._db_path.exists():
                shutil.move(str(self._db_path), str(corrupt_path))
                logger.warning("Corrupt DB moved to: %s", corrupt_path)
        except OSError as exc:
            logger.error("Failed to move corrupt DB: %s", exc)

        # Retry with fresh DB
        try:
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                timeout=10.0,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._apply_schema()
            logger.info("Fresh memory DB created after corruption recovery")
        except Exception as exc:
            logger.critical("Cannot create memory DB: %s", exc)
            self._conn = None

    def _ensure_open(self) -> None:
        if self._conn is None:
            self.init_db()
            if self._conn is None:
                raise RuntimeError("Memory database is not available.")
