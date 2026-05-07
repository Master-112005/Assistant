"""
Performance metrics for Nova Assistant.

Provides lightweight timers, counters, gauges, and optional process-resource
sampling. Metrics are retained in memory for summaries and mirrored to the
metrics log plus the analytics database when a sink is attached.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Generator

from core import settings, state
from core.logger import get_correlation_id, get_metrics_logger

try:
    import psutil
except Exception:  # pragma: no cover - optional dependency.
    psutil = None

_metrics_logger = get_metrics_logger("metrics.performance")
_MetricSink = Callable[[dict[str, Any]], None]


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class TimerMetric:
    """Represents a single timer measurement."""

    name: str
    started_at: str
    ended_at: str = ""
    duration_ms: float = 0.0
    tags: dict[str, Any] = field(default_factory=dict)
    _started_perf: float = field(default=0.0, repr=False)

    @property
    def is_running(self) -> bool:
        return bool(self.started_at) and not self.ended_at

    def finish(self) -> float:
        if self.ended_at:
            return self.duration_ms
        self.ended_at = _utc_iso()
        self.duration_ms = max(0.0, (time.perf_counter() - self._started_perf) * 1000.0)
        return self.duration_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": round(self.duration_ms, 3),
            "tags": dict(self.tags),
        }


class MetricsManager:
    """Thread-safe metrics manager with optional analytics sink."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._timers: list[TimerMetric] = []
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._event_sink: _MetricSink | None = None
        self._ready = False
        self._process = psutil.Process() if psutil is not None else None

    def init(self) -> None:
        self._ready = True
        state.metrics_ready = True

    def shutdown(self) -> None:
        self._ready = False
        state.metrics_ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def set_event_sink(self, sink: _MetricSink | None) -> None:
        self._event_sink = sink

    def start_timer(self, name: str, **tags: Any) -> TimerMetric:
        return TimerMetric(
            name=name,
            started_at=_utc_iso(),
            tags=dict(tags),
            _started_perf=time.perf_counter(),
        )

    def end_timer(self, timer: TimerMetric) -> float:
        duration_ms = timer.finish()
        with self._lock:
            self._timers.append(timer)
        self._emit_timer(timer)
        return duration_ms

    @contextmanager
    def measure(self, name: str, **tags: Any) -> Generator[TimerMetric, None, None]:
        timer = self.start_timer(name, **tags)
        try:
            yield timer
        finally:
            self.end_timer(timer)

    def record_duration(self, name: str, ms: float, **tags: Any) -> None:
        timer = TimerMetric(
            name=name,
            started_at=_utc_iso(),
            ended_at=_utc_iso(),
            duration_ms=float(ms),
            tags=dict(tags),
            _started_perf=time.perf_counter(),
        )
        with self._lock:
            self._timers.append(timer)
        self._emit_timer(timer)

    def record_counter(self, name: str, value: int = 1, **tags: Any) -> int:
        key = self._tagged_key(name, tags)
        with self._lock:
            new_value = self._counters.get(key, 0) + int(value)
            self._counters[key] = new_value
        self._emit_metric_event(
            name=name,
            metric_type="counter",
            value=float(new_value),
            tags={**tags, "delta": int(value)},
        )
        return new_value

    def get_counter(self, name: str, **tags: Any) -> int:
        key = self._tagged_key(name, tags)
        with self._lock:
            return self._counters.get(key, 0)

    def record_gauge(self, name: str, value: float, **tags: Any) -> None:
        key = self._tagged_key(name, tags)
        with self._lock:
            self._gauges[key] = float(value)
        self._emit_metric_event(
            name=name,
            metric_type="gauge",
            value=float(value),
            tags=tags,
        )

    def get_gauge(self, name: str, **tags: Any) -> float:
        key = self._tagged_key(name, tags)
        with self._lock:
            return self._gauges.get(key, 0.0)

    def capture_process_metrics(self, **tags: Any) -> dict[str, float]:
        if not self._ready or self._process is None:
            return {}
        try:
            memory = self._process.memory_info()
            snapshot = {
                "process_rss_mb": round(memory.rss / (1024 * 1024), 3),
                "process_vms_mb": round(memory.vms / (1024 * 1024), 3),
                "process_threads": float(self._process.num_threads()),
            }
            try:
                snapshot["process_cpu_pct"] = round(self._process.cpu_percent(interval=None), 3)
            except Exception:
                snapshot["process_cpu_pct"] = 0.0
        except Exception:
            return {}

        for name, value in snapshot.items():
            self.record_gauge(name, value, **tags)
        return snapshot

    def recent_timers(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [timer.to_dict() for timer in self._timers[-limit:]]

    def average_duration(self, name: str) -> float:
        with self._lock:
            matching = [timer.duration_ms for timer in self._timers if timer.name == name]
        if not matching:
            return 0.0
        return sum(matching) / len(matching)

    def slowest_timers(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            ranked = sorted(self._timers, key=lambda timer: timer.duration_ms, reverse=True)
        return [timer.to_dict() for timer in ranked[:limit]]

    def all_counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def all_gauges(self) -> dict[str, float]:
        with self._lock:
            return dict(self._gauges)

    def flush_timers(self) -> list[TimerMetric]:
        with self._lock:
            timers = list(self._timers)
            self._timers.clear()
        return timers

    def reset(self) -> None:
        with self._lock:
            self._timers.clear()
            self._counters.clear()
            self._gauges.clear()

    def _emit_timer(self, timer: TimerMetric) -> None:
        payload = {
            "name": timer.name,
            "metric_type": "timer",
            "duration_ms": round(timer.duration_ms, 3),
            "value": round(timer.duration_ms, 3),
            "started_at": timer.started_at,
            "ended_at": timer.ended_at,
            "tags": dict(timer.tags),
            "correlation_id": get_correlation_id(),
        }
        self._metrics_log("timer_recorded", payload)
        self._emit_sink(payload)

    def _emit_metric_event(self, *, name: str, metric_type: str, value: float, tags: dict[str, Any]) -> None:
        if not self._ready:
            return
        payload = {
            "name": name,
            "metric_type": metric_type,
            "value": value,
            "duration_ms": value if metric_type == "timer" else 0.0,
            "started_at": "",
            "ended_at": _utc_iso(),
            "tags": dict(tags),
            "correlation_id": get_correlation_id(),
        }
        self._metrics_log("metric_recorded", payload)
        self._emit_sink(payload)

    def _emit_sink(self, payload: dict[str, Any]) -> None:
        if not self._ready:
            return
        if not bool(settings.get("performance_metrics_enabled")):
            return
        if self._event_sink is None:
            return
        try:
            self._event_sink(payload)
        except Exception:
            _metrics_logger.warning(
                "Metrics sink rejected event",
                extra={"structured_fields": {"metric": payload.get("name"), "metric_type": payload.get("metric_type")}},
            )

    @staticmethod
    def _metrics_log(event: str, payload: dict[str, Any]) -> None:
        if not bool(settings.get("performance_metrics_enabled")):
            return
        _metrics_logger.info(
            event,
            extra={
                "structured_fields": payload,
                "session_id": state.current_session_id or "",
                "correlation_id": get_correlation_id(),
                "log_channel": "metrics",
            },
        )

    @staticmethod
    def _tagged_key(name: str, tags: dict[str, Any]) -> str:
        if not tags:
            return name
        tag_string = ",".join(f"{key}={value}" for key, value in sorted(tags.items()))
        return f"{name}[{tag_string}]"


metrics = MetricsManager()

