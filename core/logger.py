"""
Central structured logging system for Nova Assistant.

Features:
  - JSON structured file logs with session and correlation ids
  - Queue-backed, non-blocking logging for app/error/metrics/audit channels
  - Rotating file handlers with retention cleanup
  - Rich console output with plain-stream fallback
  - Privacy-aware redaction of sensitive fields and message content
  - Safe sink wrappers so logging failures never crash the app
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import queue
import re
import sys
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator, Mapping

from core import settings, state
from core.paths import LOG_DIR

try:
    from rich.logging import RichHandler
except Exception:  # pragma: no cover - Rich is optional at runtime.
    RichHandler = None

LOG_FILE = LOG_DIR / "app.log"
ERROR_LOG_FILE = LOG_DIR / "error.log"
METRICS_LOG_FILE = LOG_DIR / "metrics.log"
AUDIT_LOG_FILE = LOG_DIR / "permission_audit.log"
SESSION_ID = uuid.uuid4().hex

_BASE_LEVEL = logging.DEBUG
_LOGGER_CACHE: dict[str, "AppLogger"] = {}
_CORRELATION_ID: contextvars.ContextVar[str] = contextvars.ContextVar("nova_correlation_id", default="")
_LISTENER: QueueListener | None = None
_QUEUE_HANDLER: QueueHandler | None = None
_QUEUE: queue.Queue[logging.LogRecord] | None = None
_INITIALIZED = False
_CONFIGURED_LOGGERS: set[str] = set()

_SENSITIVE_KEYWORDS = frozenset(
    {
        "password",
        "passcode",
        "secret",
        "token",
        "api_key",
        "apikey",
        "auth",
        "credential",
        "private_key",
        "authorization",
        "cookie",
        "access_key",
        "client_secret",
    }
)
_CONTENT_KEYS = frozenset(
    {
        "message",
        "content",
        "raw_input",
        "normalized_input",
        "prompt",
        "response_text",
        "transcript",
        "text",
        "command",
        "command_context",
    }
)
_MASK = "***REDACTED***"
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]+)"),
    re.compile(r"(?i)\b(sk-[A-Za-z0-9]{8,})\b"),
    re.compile(r"(?i)\b(gh[pousr]_[A-Za-z0-9]{8,})\b"),
    re.compile(r"(?i)\b(api[_-]?key\s*[:=]\s*)(\S+)"),
    re.compile(r"(?i)\b(token\s*[:=]\s*)(\S+)"),
    re.compile(r"(?i)\b(password\s*[:=]\s*)(\S+)"),
)


def _settings_get(key: str, default: Any) -> Any:
    try:
        return settings.get(key)
    except Exception:
        return default


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso() -> str:
    return _utc_now().isoformat()


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sanitize_text(text: str, *, redact_content: bool) -> str:
    if not text:
        return ""
    if redact_content:
        return _MASK

    sanitized = text
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub(lambda match: f"{match.group(1)}{_MASK}" if match.lastindex and match.lastindex >= 2 else _MASK, sanitized)
    return sanitized


def sanitize_text(text: str, *, channel: str = "app", force_redact_content: bool = False) -> str:
    redact_content = force_redact_content or bool(_settings_get("log_redact_message_content", False))
    if channel == "analytics" and bool(_settings_get("analytics_redact_command_text", False)):
        redact_content = True
    return _sanitize_text(str(text), redact_content=redact_content)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in _SENSITIVE_KEYWORDS)


def _sanitize_value(value: Any, *, key: str = "", channel: str = "app") -> Any:
    if key and _is_sensitive_key(key):
        return _MASK
    if isinstance(value, str):
        redact_content = key.lower() in _CONTENT_KEYS and bool(_settings_get("log_redact_message_content", False))
        if channel == "analytics" and key.lower() in _CONTENT_KEYS and bool(
            _settings_get("analytics_redact_command_text", False)
        ):
            redact_content = True
        return _sanitize_text(value, redact_content=redact_content)
    if isinstance(value, bytes):
        return f"<binary:{len(value)} bytes>"
    if isinstance(value, dict):
        return {str(k): _sanitize_value(v, key=str(k), channel=channel) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_value(item, key=key, channel=channel) for item in value]
    return value


def _redact(fields: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive values recursively for log-safe structured fields."""
    return {str(key): _sanitize_value(value, key=str(key), channel="app") for key, value in (fields or {}).items()}


class _ChannelFilter(logging.Filter):
    def __init__(self, channel: str) -> None:
        super().__init__()
        self._channel = channel

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "log_channel"):
            record.log_channel = self._channel
        if not hasattr(record, "session_id"):
            record.session_id = SESSION_ID
        if not hasattr(record, "correlation_id"):
            record.correlation_id = get_correlation_id()
        if not hasattr(record, "structured_fields"):
            record.structured_fields = {}
        return True


class _OnlyChannelFilter(logging.Filter):
    def __init__(self, *channels: str) -> None:
        super().__init__()
        self._channels = set(channels)

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "log_channel", "app") in self._channels


class _ExcludeChannelFilter(logging.Filter):
    def __init__(self, *channels: str) -> None:
        super().__init__()
        self._channels = set(channels)

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "log_channel", "app") not in self._channels


class StructuredFormatter(logging.Formatter):
    """Render log records as single-line JSON documents."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: D401
        return datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "structured_fields", {}) or {}
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "message": sanitize_text(record.getMessage(), channel=getattr(record, "log_channel", "app")),
            "session_id": getattr(record, "session_id", SESSION_ID),
            "correlation_id": getattr(record, "correlation_id", ""),
            "channel": getattr(record, "log_channel", "app"),
            "thread": record.threadName,
            "process": record.process,
        }
        if fields:
            payload["fields"] = _redact(fields)
        if record.exc_info:
            exc_type = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else "Exception"
            exc_message = str(record.exc_info[1]) if record.exc_info and record.exc_info[1] else ""
            payload["exception"] = {
                "type": exc_type,
                "message": sanitize_text(exc_message, channel=getattr(record, "log_channel", "app")),
                "stacktrace": self.formatException(record.exc_info),
            }
        if record.stack_info:
            payload["stack_info"] = record.stack_info
        return json.dumps(payload, ensure_ascii=False, default=str)


class _FallbackFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        message = sanitize_text(record.getMessage(), channel=getattr(record, "log_channel", "app"))
        correlation_id = getattr(record, "correlation_id", "")
        suffix = f" [{correlation_id[:8]}]" if correlation_id else ""
        return f"{timestamp} {record.levelname:<8} {record.name}{suffix} {message}"


class SafeHandler(logging.Handler):
    """Wrap a handler so sink failures degrade safely to a fallback handler."""

    def __init__(self, inner: logging.Handler, fallback: logging.Handler | None = None) -> None:
        super().__init__(level=inner.level)
        self._inner = inner
        self._fallback = fallback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._inner.handle(record)
        except Exception as exc:  # pragma: no cover - hard to trigger reliably in tests.
            self._emit_fallback(record, exc)

    def _emit_fallback(self, original: logging.LogRecord, exc: Exception) -> None:
        if self._fallback is None:
            return
        failure_record = logging.makeLogRecord(
            {
                "name": "core.logger",
                "levelno": logging.ERROR,
                "levelname": "ERROR",
                "msg": "Logging sink failure",
                "args": (),
                "log_channel": "error",
                "session_id": SESSION_ID,
                "correlation_id": get_correlation_id(),
                "structured_fields": {
                    "sink": type(self._inner).__name__,
                    "original_logger": original.name,
                    "failure": str(exc),
                },
                "exc_info": (type(exc), exc, exc.__traceback__),
            }
        )
        try:
            self._fallback.handle(failure_record)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            super().close()


class PreservingQueueHandler(QueueHandler):
    """Queue handler that preserves exception data for in-process listeners."""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:  # noqa: D401
        prepared = logging.makeLogRecord(record.__dict__.copy())
        prepared.args = record.args
        return prepared


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def get_correlation_id() -> str:
    return _CORRELATION_ID.get()


def set_correlation_id(value: str) -> contextvars.Token[str]:
    return _CORRELATION_ID.set(value)


@contextlib.contextmanager
def correlation_context(value: str | None = None) -> Iterator[str]:
    correlation_id = value or new_correlation_id()
    token = set_correlation_id(correlation_id)
    try:
        yield correlation_id
    finally:
        _CORRELATION_ID.reset(token)


def _cleanup_log_files(directory: Path, retention_days: int) -> None:
    if retention_days <= 0:
        return
    cutoff = _utc_now() - timedelta(days=retention_days)
    for pattern in ("app.log*", "error.log*", "metrics.log*", "permission_audit.log*"):
        for candidate in directory.glob(pattern):
            if not candidate.is_file():
                continue
            try:
                modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)
                if modified < cutoff:
                    candidate.unlink(missing_ok=True)
            except Exception:
                continue


def _prefer_plain_console() -> bool:
    qpa_platform = str(os.environ.get("QT_QPA_PLATFORM") or "").strip().lower()
    if qpa_platform in {"offscreen", "minimal", "headless"}:
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    try:
        return not sys.stderr.isatty()
    except Exception:
        return True


def _build_console_handler(level: int) -> logging.Handler:
    if RichHandler is not None and not _prefer_plain_console():
        handler = RichHandler(rich_tracebacks=True, markup=False, show_path=False)
    else:  # pragma: no cover - Rich is installed in the project.
        handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(_FallbackFormatter())
    return handler


def _build_file_handler(
    path: Path,
    *,
    level: int,
    formatter: logging.Formatter,
    filters: list[logging.Filter] | None = None,
) -> logging.Handler:
    max_bytes = max(1024, int(_safe_float(_settings_get("log_rotation_mb", 10), 10.0) * 1024 * 1024))
    backup_count = max(5, _safe_int(_settings_get("log_retention_days", 14), 14))
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(formatter)
    for filter_obj in filters or []:
        handler.addFilter(filter_obj)
    return handler


def _parse_level(raw_level: Any) -> int:
    if isinstance(raw_level, int):
        return raw_level
    text = str(raw_level or "INFO").strip().upper()
    return getattr(logging, text, logging.INFO)


def initialize(*, force: bool = False, log_dir: Path | None = None) -> None:
    """Initialize the global queue-backed logging runtime."""
    global _INITIALIZED, _LISTENER, _QUEUE_HANDLER, _QUEUE

    if _INITIALIZED and not force:
        return
    if force:
        shutdown()

    log_directory = Path(log_dir) if log_dir else LOG_DIR
    log_directory.mkdir(parents=True, exist_ok=True)
    _cleanup_log_files(log_directory, _safe_int(_settings_get("log_retention_days", 14), 14))

    level = _parse_level(_settings_get("log_level", "INFO"))
    logging.raiseExceptions = False
    _QUEUE = queue.Queue()
    _QUEUE_HANDLER = PreservingQueueHandler(_QUEUE)
    _QUEUE_HANDLER.setLevel(_BASE_LEVEL)

    console_handler = _build_console_handler(level)
    structured_formatter = StructuredFormatter()
    error_fallback = _build_console_handler(logging.ERROR)

    handlers: list[logging.Handler] = [SafeHandler(console_handler)]

    if bool(_settings_get("logging_enabled", True)):
        handlers.extend(
            [
                SafeHandler(
                    _build_file_handler(
                        log_directory / LOG_FILE.name,
                        level=logging.DEBUG,
                        formatter=structured_formatter,
                        filters=[_ExcludeChannelFilter("metrics", "audit")],
                    ),
                    fallback=error_fallback,
                ),
                SafeHandler(
                    _build_file_handler(
                        log_directory / ERROR_LOG_FILE.name,
                        level=logging.ERROR,
                        formatter=structured_formatter,
                        filters=[_OnlyChannelFilter("app", "error", "metrics", "audit")],
                    ),
                    fallback=error_fallback,
                ),
                SafeHandler(
                    _build_file_handler(
                        log_directory / METRICS_LOG_FILE.name,
                        level=logging.INFO,
                        formatter=structured_formatter,
                        filters=[_OnlyChannelFilter("metrics")],
                    ),
                    fallback=error_fallback,
                ),
                SafeHandler(
                    _build_file_handler(
                        log_directory / AUDIT_LOG_FILE.name,
                        level=logging.INFO,
                        formatter=structured_formatter,
                        filters=[_OnlyChannelFilter("audit")],
                    ),
                    fallback=error_fallback,
                ),
            ]
        )

    _LISTENER = QueueListener(_QUEUE, *handlers, respect_handler_level=True)
    _LISTENER.start()
    _INITIALIZED = True
    state.current_session_id = SESSION_ID


def shutdown() -> None:
    """Stop the logging listener and detach queue handlers from configured loggers."""
    global _INITIALIZED, _LISTENER, _QUEUE_HANDLER, _QUEUE

    if _LISTENER is not None:
        try:
            _LISTENER.stop()
        except Exception:
            pass
    _LISTENER = None
    _QUEUE = None

    if _QUEUE_HANDLER is not None:
        for logger_name in list(_CONFIGURED_LOGGERS):
            logger_obj = logging.getLogger(logger_name)
            for handler in list(logger_obj.handlers):
                if handler is _QUEUE_HANDLER:
                    logger_obj.removeHandler(handler)
            logger_obj.propagate = False
    _QUEUE_HANDLER = None
    _CONFIGURED_LOGGERS.clear()
    _LOGGER_CACHE.clear()
    _INITIALIZED = False


def _ensure_logger(name: str, *, channel: str) -> logging.Logger:
    initialize()
    logger_obj = logging.getLogger(name)
    logger_obj.setLevel(_BASE_LEVEL)
    logger_obj.propagate = False

    if _QUEUE_HANDLER is not None and _QUEUE_HANDLER not in logger_obj.handlers:
        logger_obj.addHandler(_QUEUE_HANDLER)

    filter_key = f"_nova_channel_filter_{channel}"
    if not getattr(logger_obj, filter_key, False):
        logger_obj.addFilter(_ChannelFilter(channel))
        setattr(logger_obj, filter_key, True)

    _CONFIGURED_LOGGERS.add(name)
    return logger_obj


class AppLogger:
    """Structured application logger wrapper used throughout the codebase."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._logger = _ensure_logger(name, channel="app")

    @staticmethod
    def initialize(*, force: bool = False, log_dir: Path | None = None) -> None:
        initialize(force=force, log_dir=log_dir)

    @staticmethod
    def shutdown() -> None:
        shutdown()

    def _log(
        self,
        level: int,
        msg: str,
        *args: Any,
        exc: BaseException | tuple[type[BaseException], BaseException, Any] | bool | None = None,
        extra: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        merged_fields = dict(fields)
        if extra:
            for key, value in dict(extra).items():
                merged_fields.setdefault(str(key), value)
        record_extra = {
            "session_id": SESSION_ID,
            "correlation_id": get_correlation_id(),
            "structured_fields": _redact(merged_fields),
            "log_channel": "app",
        }
        kwargs: dict[str, Any] = {"extra": record_extra}
        exc_info = self._coerce_exc_info(exc)
        if exc_info is not None:
            kwargs["exc_info"] = exc_info
        try:
            self._logger.log(level, msg, *args, **kwargs)
        except Exception as log_exc:  # pragma: no cover - defensive fallback
            fallback_message = f"{_utc_iso()} LOGGING FAILURE [{self.name}] {msg}: {type(log_exc).__name__}: {log_exc}"
            try:
                sys.stderr.write(f"{fallback_message}\n")
            except Exception:
                pass

    @staticmethod
    def _coerce_exc_info(
        value: BaseException | tuple[type[BaseException], BaseException, Any] | bool | None,
    ) -> tuple[type[BaseException], BaseException, Any] | bool | None:
        if value is None or value is False:
            return None
        if value is True:
            return True
        if isinstance(value, tuple) and len(value) == 3:
            return value
        if isinstance(value, BaseException):
            return (type(value), value, value.__traceback__)
        return None

    @staticmethod
    def _extract_exception(
        fields: dict[str, Any],
        explicit_exc: BaseException | tuple[type[BaseException], BaseException, Any] | bool | None = None,
    ) -> BaseException | tuple[type[BaseException], BaseException, Any] | bool | None:
        def _is_exception_payload(value: Any) -> bool:
            return (
                value is True
                or value is False
                or isinstance(value, BaseException)
                or (isinstance(value, tuple) and len(value) == 3)
            )

        if explicit_exc is not None:
            raw_exc = fields.pop("exc", None)
            if raw_exc is not None and not _is_exception_payload(raw_exc):
                fields["exc"] = raw_exc
        else:
            raw_exc = fields.pop("exc", None)
            if _is_exception_payload(raw_exc):
                explicit_exc = raw_exc
            elif raw_exc is not None:
                fields["exc"] = raw_exc
        exc_info = fields.pop("exc_info", None)
        if explicit_exc is not None:
            return explicit_exc
        if _is_exception_payload(exc_info):
            return exc_info
        if exc_info is not None:
            fields["exc_info"] = exc_info
        return None

    def debug(self, msg: str, *args: Any, **fields: Any) -> None:
        exc = self._extract_exception(fields)
        self._log(logging.DEBUG, msg, *args, exc=exc, **fields)

    def info(self, msg: str, *args: Any, **fields: Any) -> None:
        exc = self._extract_exception(fields)
        self._log(logging.INFO, msg, *args, exc=exc, **fields)

    def warning(self, msg: str, *args: Any, **fields: Any) -> None:
        exc = self._extract_exception(fields)
        self._log(logging.WARNING, msg, *args, exc=exc, **fields)

    def error(self, msg: str, *args: Any, exc: BaseException | None = None, **fields: Any) -> None:
        resolved_exc = self._extract_exception(fields, explicit_exc=exc)
        self._log(logging.ERROR, msg, *args, exc=resolved_exc, **fields)

    def critical(self, msg: str, *args: Any, exc: BaseException | None = None, **fields: Any) -> None:
        resolved_exc = self._extract_exception(fields, explicit_exc=exc)
        self._log(logging.CRITICAL, msg, *args, exc=resolved_exc, **fields)

    def exception(self, msg: str, *args: Any, exc: BaseException | None = None, **fields: Any) -> None:
        resolved_exc = self._extract_exception(fields, explicit_exc=exc)
        self._log(logging.ERROR, msg, *args, exc=resolved_exc if resolved_exc is not None else True, **fields)


def get_logger(name: str) -> AppLogger:
    """Return an AppLogger configured for the application queue runtime."""
    cached = _LOGGER_CACHE.get(name)
    if cached is not None:
        return cached
    logger_obj = AppLogger(name)
    _LOGGER_CACHE[name] = logger_obj
    return logger_obj


def _get_structured_channel_logger(name: str, channel: str) -> logging.Logger:
    return _ensure_logger(name, channel=channel)


def get_error_logger(name: str = "errors") -> logging.Logger:
    return _get_structured_channel_logger(name, "error")


def get_metrics_logger(name: str = "metrics") -> logging.Logger:
    return _get_structured_channel_logger(name, "metrics")


def get_audit_logger(name: str = "audit") -> logging.Logger:
    return _get_structured_channel_logger(name, "audit")


__all__ = [
    "AUDIT_LOG_FILE",
    "AppLogger",
    "ERROR_LOG_FILE",
    "LOG_FILE",
    "METRICS_LOG_FILE",
    "SESSION_ID",
    "StructuredFormatter",
    "_redact",
    "correlation_context",
    "get_audit_logger",
    "get_correlation_id",
    "get_error_logger",
    "get_logger",
    "get_metrics_logger",
    "initialize",
    "new_correlation_id",
    "sanitize_text",
    "set_correlation_id",
    "shutdown",
]
