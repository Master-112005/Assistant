import time
from pathlib import Path
import sqlite3

import pytest

from core import analytics as analytics_module
from core import logger as logger_module
from core import metrics as metrics_module


def _settings(**overrides):
    values = {
        "logging_enabled": True,
        "log_level": "INFO",
        "log_rotation_mb": 1,
        "log_retention_days": 14,
        "log_redact_message_content": False,
        "analytics_enabled": True,
        "performance_metrics_enabled": True,
        "analytics_record_raw_input": True,
        "analytics_redact_command_text": False,
    }
    values.update(overrides)
    return values


@pytest.fixture()
def observability_env(tmp_path, monkeypatch):
    config = _settings()
    monkeypatch.setattr(logger_module.settings, "get", lambda key, default=None: config.get(key, default))
    monkeypatch.setattr(analytics_module.settings, "get", lambda key, default=None: config.get(key, default))
    monkeypatch.setattr(metrics_module.settings, "get", lambda key, default=None: config.get(key, default))
    logger_module.AppLogger.initialize(force=True, log_dir=tmp_path)
    metrics_module.metrics.reset()
    metrics_module.metrics.init()
    manager = analytics_module.AnalyticsManager(tmp_path / "analytics.db")
    manager.init()
    yield tmp_path, manager, config
    manager.shutdown()
    metrics_module.metrics.reset()
    metrics_module.metrics.shutdown()
    logger_module.AppLogger.shutdown()


def test_command_events_persist_and_query(observability_env):
    _, manager, _ = observability_env

    manager.record_command(
        "open chrome",
        normalized_input="open chrome",
        detected_intent="open_app",
        selected_skill="ChromeSkill",
        action_taken="launch",
        success=True,
        latency_ms=123.4,
        source="text",
    )
    manager.record_command(
        "open chrome",
        normalized_input="open chrome",
        detected_intent="open_app",
        selected_skill="ChromeSkill",
        action_taken="launch",
        success=False,
        latency_ms=250.0,
        source="speech",
        failure_reason="not_found",
    )
    manager.flush()

    recent = manager.recent_commands(2)
    top = manager.top_commands(1)

    assert len(recent) == 2
    assert recent[0]["selected_skill"] == "ChromeSkill"
    assert recent[0]["action_taken"] == "launch"
    assert top[0]["command_text"] == "open chrome"
    assert top[0]["intent"] == "open_app"
    assert top[0]["cnt"] == 2


def test_error_summary_and_recent_errors_include_traceback(observability_env):
    _, manager, _ = observability_env

    try:
        raise ValueError("bad input")
    except ValueError as exc:
        manager.record_error(
            "ValueError",
            str(exc),
            exc=exc,
            module="processor",
            context="open chrome",
            command_context='{"raw_input":"open chrome"}',
            source="processor",
        )

    manager.flush()
    recent = manager.recent_errors(1)
    summary = manager.error_summary(1)

    assert recent[0]["error_type"] == "ValueError"
    assert recent[0]["module"] == "processor"
    assert summary[0]["error_type"] == "ValueError"
    assert summary[0]["cnt"] == 1


def test_metrics_sink_records_performance_events(observability_env):
    _, manager, _ = observability_env

    metrics_module.metrics.record_duration("intent_detection", 42.0, source="test")
    manager.flush()

    slowest = manager.slowest_actions(5)
    assert any(item["name"] == "intent_detection" for item in slowest)


def test_feature_counters_and_stats(observability_env):
    _, manager, _ = observability_env

    manager.increment_feature("skill:ChromeSkill")
    manager.increment_feature("skill:ChromeSkill")
    manager.increment_feature("intent:file_search")
    manager.record_command("search file", detected_intent="file_search", success=True, latency_ms=88.0)
    manager.flush()

    features = manager.feature_stats(5, prefix="skill:")
    stats = manager.stats()

    assert features[0]["feature"] == "skill:ChromeSkill"
    assert features[0]["count"] == 2
    assert stats["total_commands"] == 1
    assert stats["top_skill"] == "ChromeSkill"


def test_data_survives_restart(tmp_path, monkeypatch):
    config = _settings()
    monkeypatch.setattr(logger_module.settings, "get", lambda key, default=None: config.get(key, default))
    monkeypatch.setattr(analytics_module.settings, "get", lambda key, default=None: config.get(key, default))
    monkeypatch.setattr(metrics_module.settings, "get", lambda key, default=None: config.get(key, default))
    logger_module.AppLogger.initialize(force=True, log_dir=tmp_path)
    db_path = tmp_path / "analytics.db"

    first = analytics_module.AnalyticsManager(db_path)
    first.init()
    first.record_command("persistent cmd", normalized_input="persistent cmd", detected_intent="test", success=True)
    first.shutdown()

    second = analytics_module.AnalyticsManager(db_path)
    second.init()
    second.flush()
    assert second.total_commands() >= 1
    second.shutdown()
    logger_module.AppLogger.shutdown()


def test_crash_recovery_marks_previous_session(tmp_path, monkeypatch):
    config = _settings()
    monkeypatch.setattr(logger_module.settings, "get", lambda key, default=None: config.get(key, default))
    monkeypatch.setattr(analytics_module.settings, "get", lambda key, default=None: config.get(key, default))
    monkeypatch.setattr(metrics_module.settings, "get", lambda key, default=None: config.get(key, default))
    logger_module.AppLogger.initialize(force=True, log_dir=tmp_path)
    db_path = tmp_path / "analytics.db"

    monkeypatch.setattr(analytics_module, "SESSION_ID", "session_one")
    first = analytics_module.AnalyticsManager(db_path)
    first.init()
    first.record_command("open chrome", detected_intent="open_app", success=True)
    first.flush()
    first._writer_stop.set()
    first._queue.put(analytics_module._STOP_EVENT)
    first._writer_thread.join(timeout=5.0)
    first._ready = False

    monkeypatch.setattr(analytics_module, "SESSION_ID", "session_two")
    second = analytics_module.AnalyticsManager(db_path)
    second.init()
    second.flush()
    sessions = second.session_history(5)

    recovered = next(item for item in sessions if item["id"] == "session_one")
    assert recovered["status"] == "crashed"
    assert recovered["crash_recovered"] == 1

    second.shutdown()
    logger_module.AppLogger.shutdown()


def test_db_write_failure_falls_back_to_error_log(observability_env, monkeypatch):
    log_dir, manager, _ = observability_env

    def explode(_connection, _batch):
        raise sqlite3.OperationalError("db down")

    monkeypatch.setattr(manager, "_write_batch", explode)
    manager.record_command("open chrome", detected_intent="open_app", success=True)
    manager.flush()
    time.sleep(0.1)
    manager.shutdown()
    logger_module.AppLogger.shutdown()

    error_log = log_dir / "error.log"
    text = error_log.read_text(encoding="utf-8")
    assert "Analytics write failure" in text
