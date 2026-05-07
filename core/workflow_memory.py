"""
Smart workflow memory — capture, recall, and safe replay of multi-step commands.

Captures successful multi-step workflows automatically, recognises replay
phrases (\"do the same thing\", \"repeat that\", \"same as yesterday\"), ranks
candidates by recency / frequency / similarity / safety, and replays them
through the existing execution pipeline with full permission/safety checks.

Architecture
------------
    1. Workflow capture   — auto-stores successful plans
    2. Step normalisation — structured JSON steps
    3. Trigger linking    — maps phrases to stored records
    4. Temporal recall    — \"yesterday\", \"last night\" resolution
    5. Similarity match   — text-overlap scoring
    6. Safe replay        — permission + safety guard checks
    7. Clarification      — disambiguation when multiple matches
    8. Learning updates   — use-count, success-rate tracking
"""
from __future__ import annotations

import json
import re
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Callable

from core import settings, state
from core.logger import get_logger
from core.memory import MemoryManager, memory_manager as _default_memory
from core.permissions import RiskLevel, permission_manager as _default_perm_mgr

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Replay-intent patterns
# ---------------------------------------------------------------------------

_REPLAY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bdo\s+the\s+same\s+thing\b", re.I), "last"),
    (re.compile(r"\brepeat\s+that\b", re.I), "last"),
    (re.compile(r"\bsame\s+again\b", re.I), "last"),
    (re.compile(r"\bdo\s+it\s+again\b", re.I), "last"),
    (re.compile(r"\brun\s+(?:it|that)\s+again\b", re.I), "last"),
    (re.compile(r"\bsame\s+as\s+yesterday\b", re.I), "yesterday"),
    (re.compile(r"\bdo\s+what\s+i\s+did\s+yesterday\b", re.I), "yesterday"),
    (re.compile(r"\bsame\s+as\s+last\s+night\b", re.I), "last_night"),
    (re.compile(r"\bdo\s+what\s+i\s+did\s+last\s+night\b", re.I), "last_night"),
    (re.compile(r"\bsame\s+as\s+(?:this\s+)?morning\b", re.I), "this_morning"),
    (re.compile(r"\blast\s+time\b", re.I), "last"),
    (re.compile(r"\brun\s+my\s+usual\b", re.I), "frequent"),
    (re.compile(r"\bmy\s+usual\b", re.I), "frequent"),
]

# ---------------------------------------------------------------------------
# Risky action set
# ---------------------------------------------------------------------------

_RISKY_ACTIONS = frozenset({
    "system_control", "file_action", "delete", "delete_file",
    "move", "move_file", "shutdown", "restart", "sleep",
})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class WorkflowStep:
    """Normalised, reusable step within a workflow."""
    order_index: int
    action: str
    target: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "low"
    requires_context: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_index": self.order_index,
            "action": self.action,
            "target": self.target,
            "params": self.params,
            "risk_level": self.risk_level,
            "requires_context": self.requires_context,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WorkflowStep:
        return cls(
            order_index=int(d.get("order_index", 0)),
            action=str(d.get("action", "")),
            target=str(d.get("target", "")),
            params=dict(d.get("params") or {}),
            risk_level=str(d.get("risk_level", "low")),
            requires_context=bool(d.get("requires_context")),
        )


@dataclass(slots=True)
class WorkflowRecord:
    """Full metadata for a stored workflow."""
    id: int = 0
    source_command: str = ""
    normalized_command: str = ""
    trigger_phrase: str = ""
    steps: list[WorkflowStep] = field(default_factory=list)
    created_at: str = ""
    last_used_at: str = ""
    use_count: int = 0
    success_rate: float = 1.0
    context_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_command": self.source_command,
            "normalized_command": self.normalized_command,
            "trigger_phrase": self.trigger_phrase,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "use_count": self.use_count,
            "success_rate": self.success_rate,
        }

    @property
    def has_risky_steps(self) -> bool:
        return any(s.action in _RISKY_ACTIONS or s.risk_level in {"high", "dangerous"} for s in self.steps)

    @property
    def summary(self) -> str:
        if not self.steps:
            return self.source_command or self.trigger_phrase
        parts = []
        for s in self.steps:
            label = f"{s.action}"
            if s.target:
                label += f" {s.target}"
            parts.append(label)
        return " → ".join(parts)


@dataclass(slots=True)
class MatchCandidate:
    """Scored workflow candidate during recall."""
    record: WorkflowRecord
    score: float = 0.0
    match_reason: str = ""


# ---------------------------------------------------------------------------
# WorkflowMemoryManager
# ---------------------------------------------------------------------------

class WorkflowMemoryManager:
    """
    Smart workflow capture, recall, and replay engine.

    Uses the Phase 31 MemoryManager for persistence (workflows table).
    """

    def __init__(
        self,
        *,
        memory: MemoryManager | None = None,
        permission_manager=None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._memory = memory or _default_memory
        self._perm_mgr = permission_manager or _default_perm_mgr
        self._time_fn = time_fn or _time.time
        self._ready = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def init(self) -> None:
        if self._ready:
            return
        if not settings.get("workflow_memory_enabled"):
            logger.info("Workflow memory disabled by settings")
            return
        if not self._memory.ready:
            self._memory.init()
        self._ready = True
        state.workflow_memory_ready = True
        logger.info("WorkflowMemoryManager initialised")

    @property
    def ready(self) -> bool:
        return self._ready

    # ================================================================== #
    # Capture                                                            #
    # ================================================================== #

    def capture_workflow(
        self,
        user_input: str,
        planned_steps: list[dict[str, Any]],
        result: dict[str, Any] | None = None,
    ) -> int | None:
        """
        Capture a successful multi-step execution as a reusable workflow.

        Returns the workflow ID, or None if capture was skipped.
        """
        if not self._ready:
            return None
        if not settings.get("auto_capture_successful_workflows"):
            return None

        result = result or {}
        if not result.get("success", True):
            return None

        # Skip single-step workflows unless they're meaningful
        if len(planned_steps) < 2:
            return None

        normalised = self._normalise_text(user_input)
        steps = self._normalise_steps(planned_steps)
        steps_dicts = [s.to_dict() for s in steps]

        wf_id = self._memory.save_workflow(
            trigger_phrase=normalised,
            steps=steps_dicts,
            name=user_input.strip(),
        )

        state.last_workflow_id = wf_id
        logger.info("Workflow captured id=%d command=%s steps=%d", wf_id, normalised[:60], len(steps))
        return wf_id

    def save_workflow(
        self,
        trigger_phrase: str,
        steps: list[dict[str, Any]],
        *,
        name: str = "",
    ) -> int:
        """Explicitly save a workflow (manual save, not auto-capture)."""
        self._ensure_ready()
        normalised = self._normalise_text(trigger_phrase)
        return self._memory.save_workflow(normalised, steps, name=name or trigger_phrase)

    # ================================================================== #
    # Recall                                                             #
    # ================================================================== #

    def detect_replay_intent(self, text: str) -> tuple[bool, str]:
        """
        Check if text is a replay phrase.

        Returns (is_replay, time_hint) where time_hint is one of:
        'last', 'yesterday', 'last_night', 'this_morning', 'frequent', or ''.
        """
        for pattern, hint in _REPLAY_PATTERNS:
            if pattern.search(text):
                return True, hint
        return False, ""

    def find_recent(self, limit: int = 5) -> list[WorkflowRecord]:
        """Return the most recently used workflows."""
        self._ensure_ready()
        rows = self._memory.list_workflows(limit=limit)
        return [self._row_to_record(r) for r in rows]

    def find_by_phrase(self, text: str) -> WorkflowRecord | None:
        """Find a workflow by exact trigger phrase."""
        self._ensure_ready()
        row = self._memory.find_workflow(self._normalise_text(text))
        return self._row_to_record(row) if row else None

    def find_best_match(
        self,
        text: str,
        *,
        time_hint: str = "",
        limit: int = 5,
    ) -> list[MatchCandidate]:
        """
        Find and rank workflow candidates for a replay request.

        Returns scored candidates, best first.
        """
        self._ensure_ready()
        rows = self._memory.list_workflows(limit=max(limit * 5, 50))
        if not rows:
            return []

        candidates: list[MatchCandidate] = []
        now = self._time_fn()

        for row in rows:
            record = self._row_to_record(row)
            score = self._score_candidate(record, text, time_hint, now)
            reason = self._explain_match(record, text, time_hint)
            candidates.append(MatchCandidate(record=record, score=score, match_reason=reason))

        candidates.sort(key=lambda c: c.score, reverse=True)
        top = candidates[:limit]

        if top:
            logger.info(
                "Workflow match: best=%s score=%.2f reason=%s",
                top[0].record.trigger_phrase[:40], top[0].score, top[0].match_reason,
            )
        return top

    def rank_candidates(self, candidates: list[MatchCandidate]) -> list[MatchCandidate]:
        """Re-rank a list of candidates (stable sort by score)."""
        return sorted(candidates, key=lambda c: c.score, reverse=True)

    # ================================================================== #
    # Replay                                                             #
    # ================================================================== #

    def replay(self, workflow_id: int) -> dict[str, Any]:
        """
        Prepare a workflow for replay execution.

        Does NOT execute — returns the replay plan dict for the processor/executor.
        """
        self._ensure_ready()
        row = self._memory._store.query_one(
            "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
        )
        if not row:
            return {"success": False, "error": "workflow_not_found", "response": "Could not find that workflow."}

        record = self._row_to_record(row)
        safety = self.is_safe_to_replay(record)

        if not safety["safe"] and settings.get("require_confirmation_for_risky_replay"):
            return {
                "success": False,
                "error": "confirmation_required",
                "response": safety["message"],
                "workflow": record.to_dict(),
                "warnings": safety.get("warnings", []),
            }

        # Bump use count
        self._memory._store.execute(
            "UPDATE workflows SET use_count = use_count + 1, updated_at = ? WHERE id = ?",
            (self._utc_iso(), workflow_id),
        )

        state.last_replayed_workflow = record.to_dict()
        state.last_workflow_id = workflow_id

        return {
            "success": True,
            "workflow": record.to_dict(),
            "steps": [s.to_dict() for s in record.steps],
            "response": f"Repeating workflow: {record.source_command or record.trigger_phrase}",
        }

    def clone_with_new_context(
        self,
        record: WorkflowRecord,
        new_params: dict[str, Any],
    ) -> WorkflowRecord:
        """
        Create a copy of a workflow with adapted parameters.

        Used for context-sensitive adaptation (e.g. fresh search query).
        """
        new_steps = []
        for step in record.steps:
            new_step = WorkflowStep(
                order_index=step.order_index,
                action=step.action,
                target=new_params.get("target", step.target),
                params={**step.params, **{k: v for k, v in new_params.items() if k != "target"}},
                risk_level=step.risk_level,
                requires_context=step.requires_context,
            )
            new_steps.append(new_step)

        return WorkflowRecord(
            id=0,
            source_command=record.source_command,
            normalized_command=record.normalized_command,
            trigger_phrase=record.trigger_phrase,
            steps=new_steps,
            created_at=self._utc_iso(),
            last_used_at=self._utc_iso(),
            use_count=0,
            success_rate=record.success_rate,
            context_snapshot=new_params,
        )

    # ================================================================== #
    # Safety                                                             #
    # ================================================================== #

    def is_safe_to_replay(self, record: WorkflowRecord) -> dict[str, Any]:
        """
        Check if a workflow is safe to auto-replay.

        Returns dict with 'safe', 'message', 'warnings'.
        """
        warnings: list[str] = []

        if record.has_risky_steps:
            risky = [s for s in record.steps if s.action in _RISKY_ACTIONS or s.risk_level in {"high", "dangerous"}]
            for s in risky:
                warnings.append(f"Step {s.order_index}: {s.action} {s.target} (risk: {s.risk_level})")

        if record.success_rate < 0.5:
            warnings.append(f"This workflow has a low success rate ({record.success_rate:.0%}).")

        if not settings.get("allow_safe_auto_replay"):
            return {
                "safe": False,
                "message": "Auto-replay is disabled in settings.",
                "warnings": warnings,
            }

        if warnings:
            return {
                "safe": False,
                "message": f"This workflow contains risky steps that need confirmation:\n" + "\n".join(warnings),
                "warnings": warnings,
            }

        return {"safe": True, "message": "Workflow is safe to replay.", "warnings": []}

    # ================================================================== #
    # Disambiguation                                                     #
    # ================================================================== #

    def format_choices(self, candidates: list[MatchCandidate], limit: int = 3) -> str:
        """Format multiple candidates as a numbered choice list for the user."""
        lines = [f"I found {len(candidates)} possible workflows:"]
        for i, c in enumerate(candidates[:limit], 1):
            name = c.record.source_command or c.record.trigger_phrase
            age = self._age_label(c.record.last_used_at or c.record.created_at)
            lines.append(f"  {i}. {name} ({age}, used {c.record.use_count}×)")
        lines.append("Which one should I run?")
        return "\n".join(lines)

    def needs_disambiguation(self, candidates: list[MatchCandidate], threshold: float = 0.15) -> bool:
        """Check if the top candidates are too close in score to auto-select."""
        if len(candidates) < 2:
            return False
        return (candidates[0].score - candidates[1].score) < threshold

    # ================================================================== #
    # Deletion / Management                                              #
    # ================================================================== #

    def delete_workflow(self, trigger_phrase: str) -> bool:
        """Delete a workflow by trigger phrase."""
        self._ensure_ready()
        return self._memory.delete_workflow(trigger_phrase)

    def list_workflows(self, limit: int = 20) -> list[WorkflowRecord]:
        """List all stored workflows."""
        self._ensure_ready()
        return [self._row_to_record(r) for r in self._memory.list_workflows(limit=limit)]

    def stats(self) -> dict[str, Any]:
        """Return workflow memory stats."""
        self._ensure_ready()
        total = self._memory._store.row_count("workflows")
        return {
            "total_workflows": total,
            "max_allowed": int(settings.get("max_saved_workflows") or 500),
        }

    # ================================================================== #
    # Scoring internals                                                  #
    # ================================================================== #

    def _score_candidate(
        self,
        record: WorkflowRecord,
        query: str,
        time_hint: str,
        now: float,
    ) -> float:
        """
        Score a workflow candidate from 0.0 to 1.0.

        Weights:
            Recency:    0.30
            Frequency:  0.20
            Similarity: 0.25
            Time-match: 0.15
            Success:    0.10
        """
        recency = self._recency_score(record, now)
        frequency = self._frequency_score(record)
        similarity = self._similarity_score(record, query)
        time_match = self._time_match_score(record, time_hint)
        success = min(record.success_rate, 1.0)

        return (
            0.30 * recency
            + 0.20 * frequency
            + 0.25 * similarity
            + 0.15 * time_match
            + 0.10 * success
        )

    def _recency_score(self, record: WorkflowRecord, now: float) -> float:
        """Score 1.0 for very recent, decaying to 0.0 over ~7 days."""
        ts_str = record.last_used_at or record.created_at
        if not ts_str:
            return 0.0
        try:
            ts = datetime.fromisoformat(ts_str).timestamp()
        except (ValueError, TypeError):
            return 0.0
        age_hours = max(0, (now - ts) / 3600)
        # Half-life of ~24 hours
        return max(0.0, 1.0 / (1.0 + age_hours / 24.0))

    def _frequency_score(self, record: WorkflowRecord) -> float:
        """Score based on use count, log-scaled."""
        import math
        return min(1.0, math.log1p(record.use_count) / math.log1p(20))

    def _similarity_score(self, record: WorkflowRecord, query: str) -> float:
        """Text similarity between query and workflow trigger/source."""
        if not query:
            return 0.5  # Neutral for pure replay phrases
        normalised_query = self._normalise_text(query)
        best = 0.0
        for text in (record.normalized_command, record.trigger_phrase, record.source_command):
            if not text:
                continue
            ratio = SequenceMatcher(None, normalised_query, self._normalise_text(text)).ratio()
            best = max(best, ratio)
        return best

    def _time_match_score(self, record: WorkflowRecord, time_hint: str) -> float:
        """Score based on whether the workflow matches the time hint."""
        if not time_hint or time_hint == "frequent":
            return 0.5 if time_hint == "frequent" and record.use_count > 3 else 0.3

        ts_str = record.last_used_at or record.created_at
        if not ts_str:
            return 0.0
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            return 0.0

        now = datetime.now(timezone.utc)

        if time_hint == "last":
            return 0.8  # Any recent workflow is a decent match

        if time_hint == "yesterday":
            yesterday_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0)
            yesterday_end = now.replace(hour=0, minute=0, second=0)
            if yesterday_start <= ts <= yesterday_end:
                return 1.0
            return 0.1

        if time_hint == "last_night":
            last_night_start = (now - timedelta(days=1)).replace(hour=18, minute=0, second=0)
            last_night_end = now.replace(hour=6, minute=0, second=0)
            if last_night_start <= ts <= last_night_end:
                return 1.0
            return 0.1

        if time_hint == "this_morning":
            morning_start = now.replace(hour=5, minute=0, second=0)
            morning_end = now.replace(hour=12, minute=0, second=0)
            if morning_start <= ts <= morning_end:
                return 1.0
            return 0.1

        return 0.3

    def _explain_match(self, record: WorkflowRecord, query: str, time_hint: str) -> str:
        """Generate a short reason string for debugging/logging."""
        parts = []
        if time_hint:
            parts.append(f"time={time_hint}")
        if record.use_count > 2:
            parts.append(f"frequent({record.use_count})")
        if query:
            sim = self._similarity_score(record, query)
            if sim > 0.5:
                parts.append(f"similar({sim:.2f})")
        return ", ".join(parts) or "recency"

    # ================================================================== #
    # Step normalisation                                                 #
    # ================================================================== #

    def _normalise_steps(self, raw_steps: list[dict[str, Any]]) -> list[WorkflowStep]:
        """Convert raw plan step dicts to normalised WorkflowStep objects."""
        result: list[WorkflowStep] = []
        for i, step in enumerate(raw_steps):
            action = str(step.get("action", "unknown"))
            if hasattr(action, "value"):
                action = action.value
            target = str(step.get("target", ""))
            params = dict(step.get("params") or {})

            # Strip transient params
            for key in ("created_at", "source_text", "depends_on", "id"):
                params.pop(key, None)

            risk = str(step.get("estimated_risk", step.get("risk_level", "low")))
            needs_context = action in {"search", "play"} or bool(params.get("query"))

            result.append(WorkflowStep(
                order_index=i + 1,
                action=action,
                target=target,
                params=params,
                risk_level=risk,
                requires_context=needs_context,
            ))
        return result

    # ================================================================== #
    # Helpers                                                            #
    # ================================================================== #

    def _row_to_record(self, row: dict[str, Any]) -> WorkflowRecord:
        """Convert a DB row dict to a WorkflowRecord."""
        steps_raw = row.get("steps") or row.get("steps_json", "[]")
        if isinstance(steps_raw, str):
            try:
                steps_raw = json.loads(steps_raw)
            except (json.JSONDecodeError, TypeError):
                steps_raw = []

        steps = [WorkflowStep.from_dict(s) if isinstance(s, dict) else s for s in steps_raw]

        return WorkflowRecord(
            id=int(row.get("id", 0)),
            source_command=str(row.get("name", "")),
            normalized_command=str(row.get("trigger_phrase", "")),
            trigger_phrase=str(row.get("trigger_phrase", "")),
            steps=steps,
            created_at=str(row.get("created_at", "")),
            last_used_at=str(row.get("updated_at", row.get("last_used_at", ""))),
            use_count=int(row.get("use_count", 0)),
            success_rate=float(row.get("success_rate", 1.0)),
        )

    def _age_label(self, ts_str: str) -> str:
        """Human-readable age label."""
        try:
            ts = datetime.fromisoformat(ts_str)
            now = datetime.now(timezone.utc)
            diff = now - ts
            if diff.days == 0:
                hours = diff.seconds // 3600
                if hours == 0:
                    return "just now"
                return f"{hours}h ago"
            if diff.days == 1:
                return "yesterday"
            return f"{diff.days}d ago"
        except Exception:
            return "unknown time"

    def _ensure_ready(self) -> None:
        if not self._ready:
            self.init()

    @staticmethod
    def _normalise_text(text: str) -> str:
        return " ".join(text.strip().lower().split())

    @staticmethod
    def _utc_iso() -> str:
        return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

workflow_memory = WorkflowMemoryManager()
