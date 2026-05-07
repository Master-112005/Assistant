"""
Priority background task queue for non-UI assistant work.

The queue isolates low-priority maintenance from user commands.  It runs a
fixed worker pool, preserves priority ordering, records task outcomes, and
keeps failures contained so a background crash never takes the app down.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
import itertools
import queue
import threading
import time
import uuid
from typing import Any, Callable

from core import settings, state
from core.logger import get_logger

logger = get_logger(__name__)


class TaskPriority(IntEnum):
    HIGH = 0
    MEDIUM = 10
    LOW = 20


@dataclass(order=True)
class _QueuedTask:
    priority: int
    sequence: int
    task_id: str = field(compare=False)
    callback: Callable[..., Any] = field(compare=False)
    args: tuple[Any, ...] = field(default_factory=tuple, compare=False)
    kwargs: dict[str, Any] = field(default_factory=dict, compare=False)
    submitted_at: float = field(default_factory=time.monotonic, compare=False)
    timeout: float | None = field(default=None, compare=False)


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    priority: int
    status: str
    submitted_at: float
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "priority": self.priority,
            "status": self.status,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 3),
            "error": self.error,
        }


class TaskQueue:
    """Fixed-size priority worker pool with cancellation flags."""

    def __init__(self, *, max_workers: int | None = None, name: str = "nova-background") -> None:
        self.name = name
        self.max_workers = max(1, int(max_workers or settings.get("max_worker_threads", 4) or 4))
        self._queue: queue.PriorityQueue[_QueuedTask | None] = queue.PriorityQueue()
        self._records: dict[str, TaskRecord] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._sequence = itertools.count()
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._stop.clear()
            for index in range(self.max_workers):
                worker = threading.Thread(
                    target=self.worker_loop,
                    name=f"{self.name}-{index + 1}",
                    daemon=True,
                )
                worker.start()
                self._workers.append(worker)
            self._started = True
            self._sync_state()
        logger.info("TaskQueue started: %s workers=%d", self.name, self.max_workers)

    def submit(
        self,
        task: Callable[..., Any],
        priority: str | int | TaskPriority = TaskPriority.MEDIUM,
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> str:
        if not callable(task):
            raise TypeError("task must be callable")
        self.start()
        task_id = uuid.uuid4().hex
        resolved_priority = self._resolve_priority(priority)
        queued = _QueuedTask(
            priority=resolved_priority,
            sequence=next(self._sequence),
            task_id=task_id,
            callback=task,
            args=args,
            kwargs=kwargs,
            timeout=timeout,
        )
        with self._lock:
            self._records[task_id] = TaskRecord(
                task_id=task_id,
                priority=resolved_priority,
                status="queued",
                submitted_at=queued.submitted_at,
            )
            self._sync_state()
        self._queue.put(queued)
        logger.info("Task queued: %s priority=%s", task_id, resolved_priority)
        return task_id

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            record = self._records.get(task_id)
            if record is None or record.status in {"completed", "failed", "cancelled"}:
                return False
            self._cancelled.add(task_id)
            if record.status == "queued":
                record.status = "cancelled"
            self._sync_state()
        logger.info("Task cancellation requested: %s", task_id)
        return True

    def worker_loop(self) -> None:
        """Run tasks until shutdown; exposed for tests and diagnostics."""
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None:
                self._queue.task_done()
                break
            try:
                self._run_task(item)
            finally:
                self._queue.task_done()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            counts: dict[str, int] = {}
            for record in self._records.values():
                counts[record.status] = counts.get(record.status, 0) + 1
            return {
                "name": self.name,
                "max_workers": self.max_workers,
                "workers_alive": sum(1 for worker in self._workers if worker.is_alive()),
                "queue_size": self._queue.qsize(),
                "counts": counts,
                "active": counts.get("running", 0),
            }

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            records = sorted(self._records.values(), key=lambda item: item.submitted_at, reverse=True)
            return [record.to_dict() for record in records[:limit]]

    def shutdown(self, *, wait: bool = False, timeout: float = 5.0) -> None:
        with self._lock:
            if not self._started:
                return
            self._stop.set()
            worker_count = len(self._workers)
        for _ in range(worker_count):
            self._queue.put(None)
        if wait:
            deadline = time.monotonic() + max(0.0, timeout)
            for worker in list(self._workers):
                remaining = max(0.0, deadline - time.monotonic())
                worker.join(timeout=remaining)
        with self._lock:
            self._workers = [worker for worker in self._workers if worker.is_alive()]
            self._started = bool(self._workers)
            self._sync_state()
        logger.info("TaskQueue shutdown requested: %s", self.name)

    def _run_task(self, item: _QueuedTask) -> None:
        with self._lock:
            record = self._records.get(item.task_id)
            if item.task_id in self._cancelled:
                if record:
                    record.status = "cancelled"
                    record.finished_at = time.monotonic()
                self._sync_state()
                return
            if record:
                record.status = "running"
                record.started_at = time.monotonic()
            self._sync_state()

        start = time.perf_counter()
        try:
            item.callback(*item.args, **item.kwargs)
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000.0
            with self._lock:
                record = self._records.get(item.task_id)
                if record:
                    record.status = "failed"
                    record.error = f"{type(exc).__name__}: {exc}"
                    record.finished_at = time.monotonic()
                    record.duration_ms = duration_ms
                self._sync_state()
            logger.exception("Background task failed", exc=exc, task_id=item.task_id)
            return

        duration_ms = (time.perf_counter() - start) * 1000.0
        with self._lock:
            record = self._records.get(item.task_id)
            if record:
                record.status = "cancelled" if item.task_id in self._cancelled else "completed"
                record.finished_at = time.monotonic()
                record.duration_ms = duration_ms
            self._sync_state()
        logger.info("Task completed: %s %.1f ms", item.task_id, duration_ms)

    def _sync_state(self) -> None:
        running = 0
        for record in self._records.values():
            if record.status == "running":
                running += 1
        state.workers_active = running

    @staticmethod
    def _resolve_priority(priority: str | int | TaskPriority) -> int:
        if isinstance(priority, TaskPriority):
            return int(priority)
        if isinstance(priority, str):
            text = priority.strip().upper()
            if text in TaskPriority.__members__:
                return int(TaskPriority[text])
            try:
                return int(text)
            except ValueError:
                return int(TaskPriority.MEDIUM)
        return int(priority)


task_queue = TaskQueue()

