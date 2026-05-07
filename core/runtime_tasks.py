"""
Threaded runtime helpers for bounded assistant operations.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Callable

_RUNTIME_THREADS: set[threading.Thread] = set()
_RUNTIME_THREADS_LOCK = threading.Lock()


@dataclass(slots=True)
class TimedInvocation:
    completed: bool
    value: Any = None
    error: BaseException | None = None
    timed_out: bool = False
    cancelled: bool = False
    duration_ms: int = 0
    background_thread_running: bool = False


def _register_runtime_thread(thread: threading.Thread) -> None:
    with _RUNTIME_THREADS_LOCK:
        _RUNTIME_THREADS.add(thread)


def _unregister_runtime_thread(thread: threading.Thread) -> None:
    with _RUNTIME_THREADS_LOCK:
        _RUNTIME_THREADS.discard(thread)


def drain_runtime_threads(timeout_seconds: float = 2.0) -> int:
    """Best-effort join of active runtime helper threads."""
    timeout_seconds = max(0.0, float(timeout_seconds or 0.0))
    deadline = time.monotonic() + timeout_seconds
    joined = 0
    while True:
        with _RUNTIME_THREADS_LOCK:
            active = [thread for thread in list(_RUNTIME_THREADS) if thread.is_alive()]
        if not active:
            return joined
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0.0:
            return joined
        per_thread = min(0.2, remaining)
        for thread in active:
            thread.join(timeout=per_thread)
            if not thread.is_alive():
                joined += 1


def invoke_with_timeout(
    callback: Callable[[], Any],
    *,
    timeout_seconds: float,
    cancel_event: threading.Event | None = None,
    poll_interval: float = 0.05,
) -> TimedInvocation:
    """
    Execute *callback* on a daemon thread and bound the caller wait time.

    The worker thread is intentionally daemonised because Python cannot
    forcefully terminate an arbitrary blocked thread. Callers should pair this
    helper with time-bounded OS/backend operations whenever possible.
    """
    timeout_seconds = max(0.1, float(timeout_seconds or 0.1))
    poll_interval = max(0.01, float(poll_interval or 0.05))

    result_holder: list[Any] = []
    error_holder: list[BaseException] = []
    finished = threading.Event()
    started_at = time.perf_counter()

    def _runner() -> None:
        try:
            result_holder.append(callback())
        except BaseException as exc:  # pragma: no cover - exercised via callers
            error_holder.append(exc)
        finally:
            finished.set()
            _unregister_runtime_thread(threading.current_thread())

    worker = threading.Thread(target=_runner, daemon=True, name="assistant-timed-call")
    _register_runtime_thread(worker)
    worker.start()

    deadline = time.monotonic() + timeout_seconds
    while True:
        if finished.wait(timeout=min(poll_interval, max(0.0, deadline - time.monotonic()))):
            duration_ms = int(round((time.perf_counter() - started_at) * 1000.0))
            if error_holder:
                return TimedInvocation(
                    completed=True,
                    error=error_holder[0],
                    duration_ms=duration_ms,
                )
            value = result_holder[0] if result_holder else None
            return TimedInvocation(
                completed=True,
                value=value,
                duration_ms=duration_ms,
            )

        duration_ms = int(round((time.perf_counter() - started_at) * 1000.0))
        if cancel_event is not None and cancel_event.is_set():
            return TimedInvocation(
                completed=False,
                cancelled=True,
                duration_ms=duration_ms,
                background_thread_running=worker.is_alive(),
            )
        if time.monotonic() >= deadline:
            return TimedInvocation(
                completed=False,
                timed_out=True,
                duration_ms=duration_ms,
                background_thread_running=worker.is_alive(),
            )
