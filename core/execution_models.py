"""
Execution engine data models.

Defines the data types for tracking real-time execution state, step outcomes,
and complete plan results across the multi-command execution pipeline.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class StepStatus(Enum):
    """Lifecycle status of a single execution step."""
    PENDING   = "pending"    # Waiting to run
    RUNNING   = "running"    # Currently executing
    SUCCESS   = "success"    # Completed successfully
    FAILED    = "failed"     # Completed with error
    SKIPPED   = "skipped"    # Skipped due to dependency failure
    CANCELLED = "cancelled"  # Cancelled by user or safety check

    def __str__(self) -> str:
        return self.value

    @property
    def is_terminal(self) -> bool:
        """True if status will not change further."""
        return self in (
            StepStatus.SUCCESS,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
            StepStatus.CANCELLED,
        )

    @property
    def is_success_like(self) -> bool:
        """True if the step did not fail (success or skipped)."""
        return self in (StepStatus.SUCCESS, StepStatus.SKIPPED)


@dataclass
class StepResult:
    """
    Complete result record for a single execution step.

    Attributes:
        step_id:      Matches the PlanStep.id this result belongs to.
        status:       Final StepStatus.
        started_at:   Unix timestamp when execution began (0 = not started).
        finished_at:  Unix timestamp when execution ended (0 = not finished).
        duration:     Wall-clock seconds from start to finish.
        message:      Human-readable summary message.
        data:         Arbitrary return data from the handler.
        error:        Error string if status == FAILED, else empty.
    """
    step_id:     str
    status:      StepStatus = StepStatus.PENDING
    started_at:  float = 0.0
    finished_at: float = 0.0
    duration:    float = 0.0
    message:     str = ""
    data:        Dict[str, Any] = field(default_factory=dict)
    error:       str = ""

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def mark_running(self) -> None:
        """Record the start time and flip status to RUNNING."""
        self.status     = StepStatus.RUNNING
        self.started_at = time.monotonic()

    def mark_success(self, message: str = "", data: Optional[Dict[str, Any]] = None) -> None:
        """Finalise as SUCCESS."""
        self._finish(StepStatus.SUCCESS, message=message, data=data)

    def mark_failed(self, error: str, message: str = "") -> None:
        """Finalise as FAILED."""
        self.error = error
        self._finish(StepStatus.FAILED, message=message or f"Failed: {error}")

    def mark_skipped(self, reason: str = "") -> None:
        """Finalise as SKIPPED."""
        self._finish(StepStatus.SKIPPED, message=reason or "Skipped due to dependency failure")

    def mark_cancelled(self, reason: str = "") -> None:
        """Finalise as CANCELLED."""
        self._finish(StepStatus.CANCELLED, message=reason or "Cancelled")

    def _finish(
        self,
        status: StepStatus,
        message: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.status      = status
        self.finished_at = time.monotonic()
        self.duration    = max(0.0, self.finished_at - self.started_at) if self.started_at else 0.0
        if message:
            self.message = message
        if data:
            self.data.update(data)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id":     self.step_id,
            "status":      self.status.value,
            "started_at":  self.started_at,
            "finished_at": self.finished_at,
            "duration":    round(self.duration, 3),
            "message":     self.message,
            "data":        self.data,
            "error":       self.error,
        }

    def __str__(self) -> str:
        dur = f"{self.duration:.2f}s" if self.duration else "-"
        return f"[{self.status.value.upper()}] step={self.step_id} duration={dur} msg={self.message!r}"


@dataclass
class ExecutionResult:
    """
    Aggregate result for an entire ExecutionPlan.

    Attributes:
        plan_id:          Identifier of the plan (original_text or UUID).
        success:          True if every non-skipped step succeeded.
        total_steps:      Number of steps in the plan.
        completed_steps:  Steps that reached SUCCESS.
        failed_steps:     Steps that reached FAILED.
        skipped_steps:    Steps that were SKIPPED.
        cancelled_steps:  Steps that were CANCELLED.
        results:          Ordered StepResult list.
        started_at:       Wall-clock monotonic start time.
        finished_at:      Wall-clock monotonic end time.
        duration:         Total wall-clock seconds.
        summary:          Human-readable outcome summary.
    """
    plan_id:         str
    success:         bool = False
    total_steps:     int = 0
    completed_steps: int = 0
    failed_steps:    int = 0
    skipped_steps:   int = 0
    cancelled_steps: int = 0
    results:         List[StepResult] = field(default_factory=list)
    started_at:      float = 0.0
    finished_at:     float = 0.0
    duration:        float = 0.0
    summary:         str = ""

    @classmethod
    def start_new(cls, plan_id: str, total_steps: int) -> "ExecutionResult":
        """Factory: create a fresh result object for a new execution run."""
        obj = cls(plan_id=plan_id, total_steps=total_steps)
        obj.started_at = time.monotonic()
        return obj

    def finalise(self) -> None:
        """Calculate aggregate stats and mark completion time."""
        self.finished_at     = time.monotonic()
        self.duration        = max(0.0, self.finished_at - self.started_at)
        self.completed_steps = sum(1 for r in self.results if r.status == StepStatus.SUCCESS)
        self.failed_steps    = sum(1 for r in self.results if r.status == StepStatus.FAILED)
        self.skipped_steps   = sum(1 for r in self.results if r.status == StepStatus.SKIPPED)
        self.cancelled_steps = sum(1 for r in self.results if r.status == StepStatus.CANCELLED)
        self.success         = self.failed_steps == 0 and self.cancelled_steps == 0

        if self.success:
            self.summary = (
                f"Completed {self.completed_steps} of {self.total_steps} steps successfully."
            )
        else:
            parts: List[str] = []
            if self.completed_steps:
                parts.append(f"{self.completed_steps} succeeded")
            if self.failed_steps:
                parts.append(f"{self.failed_steps} failed")
            if self.skipped_steps:
                parts.append(f"{self.skipped_steps} skipped")
            if self.cancelled_steps:
                parts.append(f"{self.cancelled_steps} cancelled")
            self.summary = f"Plan finished: {', '.join(parts)}."

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id":         self.plan_id,
            "success":         self.success,
            "total_steps":     self.total_steps,
            "completed_steps": self.completed_steps,
            "failed_steps":    self.failed_steps,
            "skipped_steps":   self.skipped_steps,
            "cancelled_steps": self.cancelled_steps,
            "duration":        round(self.duration, 3),
            "summary":         self.summary,
            "results":         [r.to_dict() for r in self.results],
        }

    def __str__(self) -> str:
        return (
            f"ExecutionResult plan={self.plan_id!r} success={self.success} "
            f"steps={self.completed_steps}/{self.total_steps} "
            f"failed={self.failed_steps} skipped={self.skipped_steps} "
            f"duration={self.duration:.2f}s"
        )
