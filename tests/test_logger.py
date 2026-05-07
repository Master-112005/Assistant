import json
import threading
from pathlib import Path

import pytest

from core import logger as logger_module


def _logging_settings(**overrides):
    values = {
        "logging_enabled": True,
        "log_level": "INFO",
        "log_rotation_mb": 1,
        "log_retention_days": 14,
        "log_redact_message_content": False,
        "analytics_redact_command_text": False,
    }
    values.update(overrides)
    return values


@pytest.fixture()
def isolated_logging(tmp_path, monkeypatch):
    settings_values = _logging_settings()
    monkeypatch.setattr(logger_module.settings, "get", lambda key, default=None: settings_values.get(key, default))
    logger_module.AppLogger.initialize(force=True, log_dir=tmp_path)
    yield tmp_path, settings_values
    logger_module.AppLogger.shutdown()


def _read_json_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_logger_writes_structured_json_and_redacts_fields(isolated_logging):
    log_dir, _ = isolated_logging
    logger = logger_module.get_logger("tests.logger")

    logger.info("Structured event", user="rakes", password="secret123", action="run")
    logger_module.AppLogger.shutdown()

    entries = _read_json_lines(log_dir / "app.log")
    assert entries
    last = entries[-1]
    assert last["message"] == "Structured event"
    assert last["session_id"]
    assert last["fields"]["user"] == "rakes"
    assert last["fields"]["password"] == "***REDACTED***"
    assert last["fields"]["action"] == "run"


def test_logger_writes_exceptions_to_error_log(isolated_logging):
    log_dir, _ = isolated_logging
    logger = logger_module.get_logger("tests.error")

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        logger.exception("Command failed", exc=exc, command="open chrome")

    logger_module.AppLogger.shutdown()

    entries = _read_json_lines(log_dir / "error.log")
    assert entries
    last = entries[-1]
    assert last["level"] == "ERROR"
    assert last["message"] == "Command failed"
    assert last["exception"]["type"] == "RuntimeError"
    assert "boom" in last["exception"]["message"]
    assert "stacktrace" in last["exception"]


def test_logger_warning_supports_exc_keyword_without_collision(isolated_logging):
    log_dir, _ = isolated_logging
    logger = logger_module.get_logger("tests.timeout")

    try:
        raise TimeoutError("timed out")
    except TimeoutError as exc:
        logger.warning("Command timed out", exc=exc, raw_input="open youtube")

    logger_module.AppLogger.shutdown()

    entries = _read_json_lines(log_dir / "app.log")
    assert entries
    last = entries[-1]
    assert last["message"] == "Command timed out"
    assert last["fields"]["raw_input"] == "open youtube"
    assert last["exception"]["type"] == "TimeoutError"


def test_logger_handles_threaded_messages(isolated_logging):
    log_dir, _ = isolated_logging
    logger = logger_module.get_logger("tests.threaded")

    def _worker(index: int) -> None:
        logger.info("threaded event", worker=index)

    threads = [threading.Thread(target=_worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    logger_module.AppLogger.shutdown()

    entries = _read_json_lines(log_dir / "app.log")
    threaded_entries = [entry for entry in entries if entry["message"] == "threaded event"]
    assert len(threaded_entries) == 8
    assert sorted(entry["fields"]["worker"] for entry in threaded_entries) == list(range(8))


def test_get_logger_returns_cached_wrapper(isolated_logging):
    logger1 = logger_module.get_logger("tests.same")
    logger2 = logger_module.get_logger("tests.same")
    assert logger1 is logger2


def test_log_rotation_creates_backups(tmp_path, monkeypatch):
    settings_values = _logging_settings(log_rotation_mb=0.0002)
    monkeypatch.setattr(logger_module.settings, "get", lambda key, default=None: settings_values.get(key, default))
    logger_module.AppLogger.initialize(force=True, log_dir=tmp_path)
    logger = logger_module.get_logger("tests.rotation")

    for index in range(200):
        logger.info("rotation test %s", index, payload="x" * 120)

    logger_module.AppLogger.shutdown()

    rotated = list(tmp_path.glob("app.log.*"))
    assert rotated
