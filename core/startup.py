"""
Startup and lazy-resource helpers for Nova Assistant.

This module keeps heavyweight services out of import time and out of the UI
thread.  Resources are initialized once, reused safely, and can be unloaded
after an idle period when they are optional.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Callable

from core import settings
from core.logger import get_logger
from core.task_queue import TaskPriority, task_queue

logger = get_logger(__name__)


@dataclass(slots=True)
class ResourceStats:
    name: str
    loaded: bool
    load_count: int
    last_load_ms: float
    last_used_at: float
    idle_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "loaded": self.loaded,
            "load_count": self.load_count,
            "last_load_ms": round(self.last_load_ms, 3),
            "last_used_at": self.last_used_at,
            "idle_seconds": round(self.idle_seconds, 3),
        }


class LazyResource:
    """Thread-safe lazy singleton with optional idle unload."""

    def __init__(
        self,
        name: str,
        factory: Callable[[], Any],
        *,
        unload: Callable[[Any], None] | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> None:
        self.name = name
        self._factory = factory
        self._unload = unload
        self._idle_timeout_seconds = idle_timeout_seconds
        self._lock = threading.RLock()
        self._instance: Any = None
        self._load_count = 0
        self._last_load_ms = 0.0
        self._last_used_at = 0.0

    def get(self) -> Any:
        with self._lock:
            if self._instance is not None:
                self._last_used_at = time.monotonic()
                return self._instance

            start = time.perf_counter()
            logger.info("Lazy loading resource: %s", self.name)
            instance = self._factory()
            self._last_load_ms = (time.perf_counter() - start) * 1000.0
            self._load_count += 1
            self._last_used_at = time.monotonic()
            self._instance = instance
            logger.info("Resource loaded: %s %.1f ms", self.name, self._last_load_ms)
            return instance

    def warm(self) -> Any:
        return self.get()

    def unload(self) -> bool:
        with self._lock:
            if self._instance is None:
                return False
            instance = self._instance
            self._instance = None
        if self._unload is not None:
            try:
                self._unload(instance)
            except Exception as exc:
                logger.warning("Resource unload callback failed for %s: %s", self.name, exc)
        logger.info("Resource unloaded: %s", self.name)
        return True

    def unload_if_idle(self, now: float | None = None) -> bool:
        timeout = self._idle_timeout_seconds
        if timeout is None or timeout <= 0:
            return False
        current = now or time.monotonic()
        with self._lock:
            if self._instance is None or self._last_used_at <= 0:
                return False
            if current - self._last_used_at < timeout:
                return False
        return self.unload()

    def is_loaded(self) -> bool:
        with self._lock:
            return self._instance is not None

    def stats(self) -> ResourceStats:
        now = time.monotonic()
        with self._lock:
            return ResourceStats(
                name=self.name,
                loaded=self._instance is not None,
                load_count=self._load_count,
                last_load_ms=self._last_load_ms,
                last_used_at=self._last_used_at,
                idle_seconds=(now - self._last_used_at) if self._last_used_at else 0.0,
            )


class LazyProxy:
    """Proxy object that loads the wrapped resource on first attribute access."""

    def __init__(self, resource: LazyResource) -> None:
        object.__setattr__(self, "_resource", resource)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._resource.get(), item)

    def __setattr__(self, key: str, value: Any) -> None:
        setattr(self._resource.get(), key, value)

    def __bool__(self) -> bool:
        return True

    def _is_loaded(self) -> bool:
        return self._resource.is_loaded()

    def _get_loaded(self) -> Any | None:
        return self._resource._instance

    def _stats(self) -> dict[str, Any]:
        return self._resource.stats().to_dict()


class ModelLifecycleManager:
    """Registry for long-lived optional models and heavy clients."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._resources: dict[str, LazyResource] = {}

    def register(
        self,
        name: str,
        factory: Callable[[], Any],
        *,
        unload: Callable[[Any], None] | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> LazyResource:
        with self._lock:
            existing = self._resources.get(name)
            if existing is not None:
                return existing
            resource = LazyResource(
                name,
                factory,
                unload=unload,
                idle_timeout_seconds=idle_timeout_seconds,
            )
            self._resources[name] = resource
            return resource

    def get(self, name: str) -> Any:
        with self._lock:
            resource = self._resources[name]
        return resource.get()

    def warm(self, name: str, *, background: bool = False, priority: TaskPriority = TaskPriority.LOW) -> str | None:
        with self._lock:
            resource = self._resources.get(name)
        if resource is None:
            return None
        if background:
            return task_queue.submit(resource.warm, priority=priority)
        resource.warm()
        return None

    def unload_idle(self) -> int:
        now = time.monotonic()
        count = 0
        with self._lock:
            resources = list(self._resources.values())
        for resource in resources:
            try:
                if resource.unload_if_idle(now):
                    count += 1
            except Exception as exc:
                logger.warning("Idle unload failed for %s: %s", resource.name, exc)
        return count

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {name: resource.stats().to_dict() for name, resource in self._resources.items()}


class StartupOptimizer:
    """Coordinates safe background startup work after the UI becomes visible."""

    def __init__(self, lifecycle: ModelLifecycleManager | None = None) -> None:
        self.lifecycle = lifecycle or model_lifecycle

    def schedule_post_ui_warmup(self, *, processor: Any | None = None, stt_engine: Any | None = None) -> list[str]:
        task_ids: list[str] = []
        if not bool(settings.get("background_warmup", True)):
            return task_ids

        if processor is not None and hasattr(processor, "warmup_background"):
            task_id = task_queue.submit(processor.warmup_background, TaskPriority.LOW)
            task_ids.append(task_id)

        if stt_engine is not None and hasattr(stt_engine, "warmup_async"):
            try:
                task_id = stt_engine.warmup_async()
                if task_id:
                    task_ids.append(task_id)
            except Exception as exc:
                logger.warning("STT warmup scheduling failed: %s", exc)

        return task_ids

    def optimize_now(self) -> dict[str, Any]:
        unloaded = self.lifecycle.unload_idle()
        return {
            "idle_models_unloaded": unloaded,
            "task_queue": task_queue.stats(),
            "models": self.lifecycle.stats(),
        }


def idle_unload_seconds() -> float:
    try:
        return float(settings.get("idle_model_unload_minutes", 30) or 30) * 60.0
    except Exception:
        return 30.0 * 60.0


model_lifecycle = ModelLifecycleManager()
startup_optimizer = StartupOptimizer(model_lifecycle)

