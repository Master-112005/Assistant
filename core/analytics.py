"""
Persistent analytics and telemetry store for Nova Assistant.

The analytics manager writes command, error, performance, feature, and session
events to a local SQLite database using a background writer thread so request
processing does not block on disk IO.
"""
from __future__ import annotations

import json
import queue
import sqlite3
import threading
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core import settings, state
from core.logger import (
    SESSION_ID,
    get_correlation_id,
    get_error_logger,
    get_logger,
    sanitize_text,
)
from core.metrics import metrics
from core.paths import DATA_DIR

logger = get_logger(__name__)
ANALYTICS_DB_PATH = DATA_DIR / "analytics.db"
_STOP_EVENT = "__STOP__"
_WRITER_BATCH_SIZE = 64
_WRITER_POLL_SECONDS = 0.5


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso() -> str:
    return _utc_now().isoformat()


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _event_id() -> str:
    return uuid.uuid4().hex


def _serialize_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _coalesce_str(value: Any) -> str:
    return str(value or "").strip()


def _analytics_content(value: Any, *, is_command_text: bool = False) -> str:
    text = _coalesce_str(value)
    if not text:
        return ""
    if is_command_text and not bool(settings.get("analytics_record_raw_input")):
        return ""
    return sanitize_text(text, channel="analytics")


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA foreign_keys=OFF")


_TABLE_DEFINITIONS: dict[str, str] = {
    "command_events": """
        CREATE TABLE IF NOT EXISTS command_events (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL DEFAULT '',
            raw_input TEXT NOT NULL DEFAULT '',
            normalized_input TEXT NOT NULL DEFAULT '',
            intent TEXT NOT NULL DEFAULT '',
            detected_intent TEXT NOT NULL DEFAULT '',
            skill TEXT NOT NULL DEFAULT '',
            selected_skill TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT '',
            action_taken TEXT NOT NULL DEFAULT '',
            success INTEGER NOT NULL DEFAULT 0,
            failure_reason TEXT NOT NULL DEFAULT '',
            latency_ms REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
    """,
    "error_events": """
        CREATE TABLE IF NOT EXISTS error_events (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL DEFAULT '',
            error_type TEXT NOT NULL DEFAULT '',
            exception_type TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            stacktrace TEXT NOT NULL DEFAULT '',
            module TEXT NOT NULL DEFAULT '',
            context TEXT NOT NULL DEFAULT '',
            command_context TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
    """,
    "performance_events": """
        CREATE TABLE IF NOT EXISTS performance_events (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            metric_type TEXT NOT NULL DEFAULT 'timer',
            value REAL NOT NULL DEFAULT 0,
            duration_ms REAL NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL DEFAULT '',
            ended_at TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
    """,
    "feature_usage": """
        CREATE TABLE IF NOT EXISTS feature_usage (
            feature TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0,
            last_used TEXT NOT NULL DEFAULT '',
            last_session_id TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )
    """,
    "sessions": """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            crash_recovered INTEGER NOT NULL DEFAULT 0,
            recovered_by_session_id TEXT NOT NULL DEFAULT '',
            crash_reason TEXT NOT NULL DEFAULT '',
            command_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            last_command_at TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )
    """,
}

_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "command_events": {
        "correlation_id": "TEXT NOT NULL DEFAULT ''",
        "normalized_input": "TEXT NOT NULL DEFAULT ''",
        "detected_intent": "TEXT NOT NULL DEFAULT ''",
        "selected_skill": "TEXT NOT NULL DEFAULT ''",
        "action_taken": "TEXT NOT NULL DEFAULT ''",
        "failure_reason": "TEXT NOT NULL DEFAULT ''",
        "source": "TEXT NOT NULL DEFAULT ''",
        "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "error_events": {
        "correlation_id": "TEXT NOT NULL DEFAULT ''",
        "exception_type": "TEXT NOT NULL DEFAULT ''",
        "command_context": "TEXT NOT NULL DEFAULT ''",
        "source": "TEXT NOT NULL DEFAULT ''",
        "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "performance_events": {
        "correlation_id": "TEXT NOT NULL DEFAULT ''",
        "metric_type": "TEXT NOT NULL DEFAULT 'timer'",
        "value": "REAL NOT NULL DEFAULT 0",
        "started_at": "TEXT NOT NULL DEFAULT ''",
        "ended_at": "TEXT NOT NULL DEFAULT ''",
    },
    "feature_usage": {
        "last_session_id": "TEXT NOT NULL DEFAULT ''",
        "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "sessions": {
        "status": "TEXT NOT NULL DEFAULT 'running'",
        "crash_recovered": "INTEGER NOT NULL DEFAULT 0",
        "recovered_by_session_id": "TEXT NOT NULL DEFAULT ''",
        "crash_reason": "TEXT NOT NULL DEFAULT ''",
        "command_count": "INTEGER NOT NULL DEFAULT 0",
        "error_count": "INTEGER NOT NULL DEFAULT 0",
        "last_command_at": "TEXT NOT NULL DEFAULT ''",
        "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
    },
}

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_command_events_created_at ON command_events(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_command_events_detected_intent ON command_events(detected_intent, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_command_events_success ON command_events(success, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_error_events_created_at ON error_events(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_error_events_type ON error_events(exception_type, error_type, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_performance_events_name ON performance_events(name, metric_type, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at DESC)",
)


@dataclass(slots=True)
class _QueuedEvent:
    kind: str
    payload: dict[str, Any]


class AnalyticsManager:
    """Local analytics/telemetry store with queued writes and recovery."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path else ANALYTICS_DB_PATH
        self._lock = threading.RLock()
        self._queue: queue.Queue[_QueuedEvent | str] = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._writer_stop = threading.Event()
        self._ready = False

    def init(self) -> None:
        with self._lock:
            if self._ready:
                return
            state.current_session_id = SESSION_ID
            if not bool(settings.get("analytics_enabled")):
                state.analytics_ready = False
                logger.info("Analytics disabled by settings", db_path=str(self._db_path))
                return

            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as connection:
                _configure_connection(connection)
                self._ensure_schema(connection)
                recovered = self._recover_incomplete_sessions(connection)
                connection.execute(
                    """
                    INSERT OR REPLACE INTO sessions (
                        id, started_at, ended_at, status, crash_recovered, recovered_by_session_id,
                        crash_reason, command_count, error_count, last_command_at, metadata_json
                    ) VALUES (?, ?, NULL, 'running', ?, '', '', 0, 0, '', ?)
                    """,
                    (
                        SESSION_ID,
                        _utc_iso(),
                        int(recovered > 0),
                        _serialize_json({"app": "Nova Assistant"}),
                    ),
                )
                connection.commit()

            self._writer_stop.clear()
            self._writer_thread = threading.Thread(
                target=self._writer_loop,
                name="analytics-writer",
                daemon=True,
            )
            self._writer_thread.start()
            self._ready = True
            state.analytics_ready = True
            state.current_session_id = SESSION_ID
            metrics.set_event_sink(self.record_metric_event)
            self.cleanup_old_data()
            logger.info(
                "Analytics initialized",
                db_path=str(self._db_path),
                recovered_sessions=recovered,
                session_id=SESSION_ID,
            )

    def shutdown(self) -> None:
        with self._lock:
            if not self._ready:
                return
            metrics.set_event_sink(None)
            self.flush()
            self._writer_stop.set()
            self._queue.put(_STOP_EVENT)
            if self._writer_thread is not None:
                self._writer_thread.join(timeout=5.0)
            self._writer_thread = None
            self._update_session_end(status="ended", crash_reason="")
            self._ready = False
            state.analytics_ready = False
            logger.info("Analytics shut down", db_path=str(self._db_path), session_id=SESSION_ID)

    def mark_session_crashed(self, reason: str, exc: BaseException | None = None) -> None:
        if not self._ready:
            return
        details = reason
        if exc is not None:
            details = f"{reason}: {type(exc).__name__}: {exc}"
        self.flush()
        self._update_session_end(status="crashed", crash_reason=details)

    @property
    def ready(self) -> bool:
        return self._ready

    def flush(self) -> None:
        if not self._ready:
            return
        self._queue.join()

    def record_command(
        self,
        raw_input: str,
        *,
        normalized_input: str = "",
        intent: str = "",
        detected_intent: str = "",
        skill: str = "",
        selected_skill: str = "",
        action: str = "",
        action_taken: str = "",
        success: bool = True,
        latency_ms: float = 0.0,
        source: str = "",
        failure_reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        if not self._ready:
            return None
        event_id = _event_id()
        state.last_command_latency_ms = float(latency_ms or 0.0)
        payload = {
            "id": event_id,
            "session_id": SESSION_ID,
            "correlation_id": get_correlation_id(),
            "raw_input": _analytics_content(raw_input, is_command_text=True),
            "normalized_input": _analytics_content(normalized_input or raw_input, is_command_text=True),
            "intent": _coalesce_str(intent or detected_intent),
            "detected_intent": _coalesce_str(detected_intent or intent),
            "skill": _coalesce_str(skill or selected_skill),
            "selected_skill": _coalesce_str(selected_skill or skill),
            "action": _coalesce_str(action or action_taken),
            "action_taken": _coalesce_str(action_taken or action),
            "success": int(bool(success)),
            "failure_reason": sanitize_text(_coalesce_str(failure_reason), channel="analytics"),
            "latency_ms": float(latency_ms or 0.0),
            "source": _coalesce_str(source),
            "metadata_json": _serialize_json(metadata or {}),
            "created_at": _utc_iso(),
        }
        self._enqueue("command", payload)
        return event_id

    def record_error(
        self,
        error_type: str,
        message: str,
        *,
        exc: BaseException | None = None,
        module: str = "",
        context: str = "",
        command_context: str = "",
        source: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        event_id = _event_id()
        state.last_error_id = event_id
        state.last_error = _coalesce_str(message)
        stacktrace = ""
        exception_type = _coalesce_str(error_type)
        if exc is not None:
            exception_type = type(exc).__name__
            stacktrace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

        payload = {
            "id": event_id,
            "session_id": SESSION_ID,
            "correlation_id": get_correlation_id(),
            "error_type": _coalesce_str(error_type),
            "exception_type": exception_type,
            "message": sanitize_text(message, channel="analytics"),
            "stacktrace": stacktrace,
            "module": _coalesce_str(module),
            "context": sanitize_text(context, channel="analytics", force_redact_content=False),
            "command_context": sanitize_text(command_context or context, channel="analytics", force_redact_content=False),
            "source": _coalesce_str(source),
            "metadata_json": _serialize_json(metadata or {}),
            "created_at": _utc_iso(),
        }

        if not self._ready:
            self._fallback_log("error_event", payload, None)
            return None

        self._enqueue("error", payload)
        return event_id

    def record_performance(
        self,
        name: str,
        duration_ms: float = 0.0,
        *,
        metric_type: str = "timer",
        value: float | None = None,
        started_at: str = "",
        ended_at: str = "",
        correlation_id: str = "",
        tags: dict[str, Any] | None = None,
        **tag_fields: Any,
    ) -> str | None:
        if not self._ready:
            return None
        payload = {
            "id": _event_id(),
            "session_id": SESSION_ID,
            "correlation_id": correlation_id or get_correlation_id(),
            "name": _coalesce_str(name),
            "metric_type": _coalesce_str(metric_type or "timer") or "timer",
            "value": float(value if value is not None else duration_ms or 0.0),
            "duration_ms": float(duration_ms or 0.0),
            "started_at": _coalesce_str(started_at),
            "ended_at": _coalesce_str(ended_at),
            "tags_json": _serialize_json(tags if tags is not None else tag_fields),
            "created_at": _utc_iso(),
        }
        self._enqueue("performance", payload)
        return payload["id"]

    def record_metric_event(self, metric_event: dict[str, Any]) -> None:
        self.record_performance(
            metric_event.get("name", ""),
            float(metric_event.get("duration_ms") or 0.0),
            metric_type=str(metric_event.get("metric_type") or "timer"),
            value=float(metric_event.get("value") or 0.0),
            started_at=str(metric_event.get("started_at") or ""),
            ended_at=str(metric_event.get("ended_at") or ""),
            correlation_id=str(metric_event.get("correlation_id") or ""),
            tags=dict(metric_event.get("tags") or {}),
        )

    def increment_feature(self, feature: str, count: int = 1, metadata: dict[str, Any] | None = None) -> None:
        if not self._ready:
            return
        payload = {
            "feature": _coalesce_str(feature),
            "count": int(count),
            "last_used": _utc_iso(),
            "last_session_id": SESSION_ID,
            "metadata_json": _serialize_json(metadata or {}),
        }
        self._enqueue("feature", payload)

    def recent_commands(self, limit: int = 10) -> list[dict[str, Any]]:
        self.flush()
        return self._query(
            """
            SELECT
                raw_input,
                normalized_input,
                COALESCE(NULLIF(detected_intent, ''), intent) AS intent,
                COALESCE(NULLIF(selected_skill, ''), skill) AS selected_skill,
                COALESCE(NULLIF(action_taken, ''), action) AS action_taken,
                success,
                failure_reason,
                latency_ms,
                source,
                created_at
            FROM command_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    def top_commands(self, limit: int = 10) -> list[dict[str, Any]]:
        self.flush()
        return self._query(
            """
            SELECT
                COALESCE(NULLIF(normalized_input, ''), raw_input) AS command_text,
                COALESCE(NULLIF(detected_intent, ''), intent) AS intent,
                COUNT(*) AS cnt,
                ROUND(AVG(latency_ms), 1) AS avg_ms,
                ROUND(AVG(CASE WHEN success = 1 THEN 100.0 ELSE 0.0 END), 1) AS success_rate
            FROM command_events
            GROUP BY command_text, intent
            ORDER BY cnt DESC, avg_ms DESC
            LIMIT ?
            """,
            (limit,),
        )

    def slowest_actions(self, limit: int = 10) -> list[dict[str, Any]]:
        self.flush()
        return self._query(
            """
            SELECT name, metric_type, duration_ms, value, tags_json, created_at
            FROM performance_events
            WHERE metric_type = 'timer'
            ORDER BY duration_ms DESC, created_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    def recent_errors(self, limit: int = 10) -> list[dict[str, Any]]:
        self.flush()
        return self._query(
            """
            SELECT
                COALESCE(NULLIF(exception_type, ''), error_type) AS error_type,
                message,
                module,
                source,
                created_at
            FROM error_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    def error_summary(self, limit: int = 10) -> list[dict[str, Any]]:
        self.flush()
        return self._query(
            """
            SELECT
                COALESCE(NULLIF(exception_type, ''), error_type) AS error_type,
                message,
                COUNT(*) AS cnt,
                MAX(created_at) AS last_seen
            FROM error_events
            GROUP BY error_type, message
            ORDER BY cnt DESC, last_seen DESC
            LIMIT ?
            """,
            (limit,),
        )

    def daily_usage(self, days: int = 7) -> list[dict[str, Any]]:
        self.flush()
        cutoff = (_utc_now() - timedelta(days=max(1, int(days)))).isoformat()
        return self._query(
            """
            SELECT
                substr(created_at, 1, 10) AS day,
                COUNT(*) AS total,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
                ROUND(AVG(latency_ms), 1) AS avg_latency_ms
            FROM command_events
            WHERE created_at >= ?
            GROUP BY substr(created_at, 1, 10)
            ORDER BY day DESC
            """,
            (cutoff,),
        )

    def feature_stats(self, limit: int = 20, prefix: str | None = None) -> list[dict[str, Any]]:
        self.flush()
        if prefix:
            return self._query(
                """
                SELECT feature, count, last_used, last_session_id
                FROM feature_usage
                WHERE feature LIKE ?
                ORDER BY count DESC, last_used DESC
                LIMIT ?
                """,
                (f"{prefix}%", limit),
            )
        return self._query(
            """
            SELECT feature, count, last_used, last_session_id
            FROM feature_usage
            ORDER BY count DESC, last_used DESC
            LIMIT ?
            """,
            (limit,),
        )

    def session_history(self, limit: int = 10) -> list[dict[str, Any]]:
        self.flush()
        return self._query(
            """
            SELECT id, started_at, ended_at, status, crash_recovered, recovered_by_session_id,
                   crash_reason, command_count, error_count, last_command_at
            FROM sessions
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    def success_rate(self) -> float:
        row = self._query_one(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS ok FROM command_events"
        )
        if not row or not row["total"]:
            return 1.0
        return float(row["ok"] or 0) / float(row["total"])

    def avg_latency(self) -> float:
        row = self._query_one("SELECT AVG(latency_ms) AS avg_ms FROM command_events")
        return float(row["avg_ms"] or 0.0) if row else 0.0

    def total_commands(self) -> int:
        row = self._query_one("SELECT COUNT(*) AS cnt FROM command_events")
        return int(row["cnt"]) if row else 0

    def total_errors(self) -> int:
        row = self._query_one("SELECT COUNT(*) AS cnt FROM error_events")
        return int(row["cnt"]) if row else 0

    def stats(self) -> dict[str, Any]:
        self.flush()
        today_prefix = _utc_now().date().isoformat()
        summary = self._query_one(
            """
            SELECT
                COUNT(*) AS total_commands,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS ok,
                AVG(latency_ms) AS avg_latency_ms
            FROM command_events
            WHERE substr(created_at, 1, 10) = ?
            """,
            (today_prefix,),
        ) or {}
        errors_today = self._query_one(
            "SELECT COUNT(*) AS cnt FROM error_events WHERE substr(created_at, 1, 10) = ?",
            (today_prefix,),
        ) or {}
        top_skill_rows = self.feature_stats(limit=1, prefix="skill:")
        top_skill = ""
        if top_skill_rows:
            top_skill = top_skill_rows[0]["feature"].split(":", 1)[1]
        return {
            "session_id": SESSION_ID,
            "total_commands": int(summary.get("total_commands") or 0),
            "total_errors": int(errors_today.get("cnt") or 0),
            "success_rate": round(
                (float(summary.get("ok") or 0) / float(summary.get("total_commands") or 1))
                if summary.get("total_commands")
                else 1.0,
                3,
            ),
            "avg_latency_ms": round(float(summary.get("avg_latency_ms") or 0.0), 1),
            "top_skill": top_skill,
        }

    def cleanup_old_data(self, days: int | None = None) -> int:
        if not self._ready:
            return 0
        retention_days = max(1, _safe_int(days if days is not None else settings.get("log_retention_days"), 14))
        cutoff = (_utc_now() - timedelta(days=retention_days)).isoformat()
        total = 0
        with sqlite3.connect(self._db_path) as connection:
            _configure_connection(connection)
            for table in ("command_events", "error_events", "performance_events"):
                cursor = connection.execute(f"DELETE FROM {table} WHERE created_at < ?", (cutoff,))
                total += cursor.rowcount
            session_cursor = connection.execute(
                "DELETE FROM sessions WHERE ended_at IS NOT NULL AND ended_at < ?",
                (cutoff,),
            )
            total += session_cursor.rowcount
            connection.commit()
        if total:
            logger.info("Analytics cleanup completed", removed_rows=total, retention_days=retention_days)
        return total

    def clear_all(self) -> int:
        if not self._ready:
            return 0
        self.flush()
        total = 0
        with sqlite3.connect(self._db_path) as connection:
            _configure_connection(connection)
            for table in ("command_events", "error_events", "performance_events", "feature_usage", "sessions"):
                cursor = connection.execute(f"DELETE FROM {table}")
                total += cursor.rowcount
            connection.execute(
                """
                INSERT OR REPLACE INTO sessions (
                    id, started_at, ended_at, status, crash_recovered, recovered_by_session_id,
                    crash_reason, command_count, error_count, last_command_at, metadata_json
                ) VALUES (?, ?, NULL, 'running', 0, '', '', 0, 0, '', '{}')
                """,
                (SESSION_ID, _utc_iso()),
            )
            connection.commit()
        logger.warning("Analytics store cleared", deleted_rows=total)
        return total

    def _enqueue(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(_QueuedEvent(kind=kind, payload=payload))
        except Exception as exc:
            self._fallback_log(kind, payload, exc)

    def _writer_loop(self) -> None:
        connection = sqlite3.connect(self._db_path, check_same_thread=False)
        _configure_connection(connection)
        try:
            while True:
                batch: list[_QueuedEvent] = []
                try:
                    item = self._queue.get(timeout=_WRITER_POLL_SECONDS)
                except queue.Empty:
                    if self._writer_stop.is_set():
                        break
                    continue

                if item == _STOP_EVENT:
                    self._queue.task_done()
                    if self._writer_stop.is_set():
                        break
                    continue

                if isinstance(item, _QueuedEvent):
                    batch.append(item)

                while len(batch) < _WRITER_BATCH_SIZE:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item == _STOP_EVENT:
                        self._writer_stop.set()
                        self._queue.task_done()
                        break
                    if isinstance(item, _QueuedEvent):
                        batch.append(item)

                if not batch:
                    continue

                try:
                    self._write_batch(connection, batch)
                except Exception as exc:
                    for queued in batch:
                        self._fallback_log(queued.kind, queued.payload, exc)
                finally:
                    for _ in batch:
                        self._queue.task_done()
        finally:
            connection.close()

    def _write_batch(self, connection: sqlite3.Connection, batch: list[_QueuedEvent]) -> None:
        with connection:
            for queued in batch:
                payload = queued.payload
                if queued.kind == "command":
                    connection.execute(
                        """
                        INSERT INTO command_events (
                            id, session_id, correlation_id, raw_input, normalized_input, intent, detected_intent,
                            skill, selected_skill, action, action_taken, success, failure_reason, latency_ms,
                            source, metadata_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            payload["id"],
                            payload["session_id"],
                            payload["correlation_id"],
                            payload["raw_input"],
                            payload["normalized_input"],
                            payload["intent"],
                            payload["detected_intent"],
                            payload["skill"],
                            payload["selected_skill"],
                            payload["action"],
                            payload["action_taken"],
                            payload["success"],
                            payload["failure_reason"],
                            payload["latency_ms"],
                            payload["source"],
                            payload["metadata_json"],
                            payload["created_at"],
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE sessions
                        SET command_count = command_count + 1, last_command_at = ?
                        WHERE id = ?
                        """,
                        (payload["created_at"], SESSION_ID),
                    )
                elif queued.kind == "error":
                    connection.execute(
                        """
                        INSERT INTO error_events (
                            id, session_id, correlation_id, error_type, exception_type, message, stacktrace,
                            module, context, command_context, source, metadata_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            payload["id"],
                            payload["session_id"],
                            payload["correlation_id"],
                            payload["error_type"],
                            payload["exception_type"],
                            payload["message"],
                            payload["stacktrace"],
                            payload["module"],
                            payload["context"],
                            payload["command_context"],
                            payload["source"],
                            payload["metadata_json"],
                            payload["created_at"],
                        ),
                    )
                    connection.execute(
                        "UPDATE sessions SET error_count = error_count + 1 WHERE id = ?",
                        (SESSION_ID,),
                    )
                elif queued.kind == "performance":
                    connection.execute(
                        """
                        INSERT INTO performance_events (
                            id, session_id, correlation_id, name, metric_type, value, duration_ms,
                            started_at, ended_at, tags_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            payload["id"],
                            payload["session_id"],
                            payload["correlation_id"],
                            payload["name"],
                            payload["metric_type"],
                            payload["value"],
                            payload["duration_ms"],
                            payload["started_at"],
                            payload["ended_at"],
                            payload["tags_json"],
                            payload["created_at"],
                        ),
                    )
                elif queued.kind == "feature":
                    connection.execute(
                        """
                        INSERT INTO feature_usage (feature, count, last_used, last_session_id, metadata_json)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(feature) DO UPDATE SET
                            count = count + excluded.count,
                            last_used = excluded.last_used,
                            last_session_id = excluded.last_session_id,
                            metadata_json = excluded.metadata_json
                        """,
                        (
                            payload["feature"],
                            payload["count"],
                            payload["last_used"],
                            payload["last_session_id"],
                            payload["metadata_json"],
                        ),
                    )

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        for ddl in _TABLE_DEFINITIONS.values():
            connection.execute(ddl)
        for table, column_map in _COLUMN_MIGRATIONS.items():
            existing_columns = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for column_name, column_sql in column_map.items():
                if column_name not in existing_columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_sql}")
        for ddl in _INDEXES:
            connection.execute(ddl)

    def _recover_incomplete_sessions(self, connection: sqlite3.Connection) -> int:
        cursor = connection.execute(
            """
            UPDATE sessions
            SET ended_at = COALESCE(ended_at, ?),
                status = CASE WHEN status = 'running' THEN 'crashed' ELSE status END,
                crash_recovered = 1,
                recovered_by_session_id = ?,
                crash_reason = CASE
                    WHEN crash_reason = '' THEN 'Recovered on next startup'
                    ELSE crash_reason
                END
            WHERE ended_at IS NULL AND status = 'running'
            """,
            (_utc_iso(), SESSION_ID),
        )
        return cursor.rowcount

    def _update_session_end(self, *, status: str, crash_reason: str) -> None:
        with sqlite3.connect(self._db_path) as connection:
            _configure_connection(connection)
            connection.execute(
                """
                UPDATE sessions
                SET ended_at = ?, status = ?, crash_reason = CASE WHEN ? = '' THEN crash_reason ELSE ? END
                WHERE id = ?
                """,
                (_utc_iso(), status, crash_reason, crash_reason, SESSION_ID),
            )
            connection.commit()

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if not self._db_path.exists():
            return []
        with sqlite3.connect(self._db_path) as connection:
            _configure_connection(connection)
            rows = connection.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def _query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def _fallback_log(self, kind: str, payload: dict[str, Any], exc: Exception | None) -> None:
        try:
            get_error_logger("analytics.fallback").error(
                "Analytics write failure",
                extra={
                    "structured_fields": {
                        "kind": kind,
                        "payload": payload,
                        "failure": str(exc) if exc else "analytics_not_ready",
                    },
                    "correlation_id": get_correlation_id(),
                    "log_channel": "error",
                },
            )
        except Exception:
            pass


analytics = AnalyticsManager()
