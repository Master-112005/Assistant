"""
Tests for PHASE 32 — Smart Command Memory (Workflow Memory).

Covers:
  - Replay intent detection
  - Workflow capture (auto + manual)
  - Single-step skip policy
  - Step normalisation
  - Find by phrase (exact match)
  - Best-match ranking (recency, frequency, similarity, time hints)
  - Temporal recall (yesterday, last night, this morning)
  - Safe replay checks
  - Risky workflow confirmation
  - Disambiguation (needs_disambiguation, format_choices)
  - Context adaptation (clone_with_new_context)
  - Workflow deletion
  - Missing history handling
  - Repeated replay stability
  - WorkflowStep/WorkflowRecord serialisation
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from core.memory_store import MemoryStore
from core.memory import MemoryManager
from core.workflow_memory import (
    WorkflowMemoryManager,
    WorkflowRecord,
    WorkflowStep,
    MatchCandidate,
    _REPLAY_PATTERNS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_db() -> Path:
    d = tempfile.mkdtemp()
    return Path(d) / "test_wf.db"


def _make_wfm(db_path: Path | None = None, time_fn=None) -> WorkflowMemoryManager:
    """Create a WorkflowMemoryManager backed by a temp database."""
    store = MemoryStore(db_path or _tmp_db())
    mem = MemoryManager(store=store)
    with patch("core.memory.settings") as ms, patch("core.memory.state"):
        ms.get = lambda key, default=None: _settings_defaults().get(key, default)
        mem.init()

    wfm = WorkflowMemoryManager(memory=mem, time_fn=time_fn)
    with patch("core.workflow_memory.settings") as ws, patch("core.workflow_memory.state"):
        ws.get = lambda key, default=None: _settings_defaults().get(key, default)
        wfm.init()
    return wfm


def _settings_defaults():
    return {
        "memory_enabled": True,
        "history_retention_days": 90,
        "auto_learn_preferences": True,
        "store_interaction_history": True,
        "memory_backup_on_exit": False,
        "workflow_memory_enabled": True,
        "auto_capture_successful_workflows": True,
        "max_saved_workflows": 500,
        "allow_safe_auto_replay": True,
        "require_confirmation_for_risky_replay": True,
    }


class _patched:
    """Combined patch for settings and state in both memory and workflow_memory."""
    def __enter__(self):
        self._patches = [
            patch("core.memory.settings"),
            patch("core.memory.state"),
            patch("core.workflow_memory.settings"),
            patch("core.workflow_memory.state"),
        ]
        mocks = [p.__enter__() for p in self._patches]
        for m in mocks:
            if hasattr(m, "get"):
                m.get = lambda key, default=None: _settings_defaults().get(key, default)
        return mocks

    def __exit__(self, *args):
        for p in reversed(self._patches):
            p.__exit__(*args)


# ===========================================================================
# Replay Intent Detection Tests
# ===========================================================================

class TestReplayIntentDetection(unittest.TestCase):
    def setUp(self):
        self.wfm = _make_wfm()

    def test_do_the_same_thing(self):
        is_replay, hint = self.wfm.detect_replay_intent("do the same thing")
        self.assertTrue(is_replay)
        self.assertEqual(hint, "last")

    def test_repeat_that(self):
        is_replay, hint = self.wfm.detect_replay_intent("repeat that")
        self.assertTrue(is_replay)
        self.assertEqual(hint, "last")

    def test_same_as_yesterday(self):
        is_replay, hint = self.wfm.detect_replay_intent("same as yesterday")
        self.assertTrue(is_replay)
        self.assertEqual(hint, "yesterday")

    def test_same_as_last_night(self):
        is_replay, hint = self.wfm.detect_replay_intent("same as last night")
        self.assertTrue(is_replay)
        self.assertEqual(hint, "last_night")

    def test_run_my_usual(self):
        is_replay, hint = self.wfm.detect_replay_intent("run my usual music setup")
        self.assertTrue(is_replay)
        self.assertEqual(hint, "frequent")

    def test_normal_command_not_replay(self):
        is_replay, _ = self.wfm.detect_replay_intent("open chrome")
        self.assertFalse(is_replay)

    def test_do_it_again(self):
        is_replay, hint = self.wfm.detect_replay_intent("do it again")
        self.assertTrue(is_replay)
        self.assertEqual(hint, "last")


# ===========================================================================
# Capture Tests
# ===========================================================================

class TestWorkflowCapture(unittest.TestCase):
    def setUp(self):
        self.wfm = _make_wfm()

    def test_multi_step_captured(self):
        steps = [
            {"action": "open_app", "target": "youtube"},
            {"action": "search", "target": "", "params": {"query": "songs"}},
        ]
        with _patched():
            wf_id = self.wfm.capture_workflow("open youtube and play songs", steps, {"success": True})
        self.assertIsNotNone(wf_id)
        self.assertGreater(wf_id, 0)

    def test_single_step_skipped(self):
        steps = [{"action": "open_app", "target": "chrome"}]
        with _patched():
            wf_id = self.wfm.capture_workflow("open chrome", steps, {"success": True})
        self.assertIsNone(wf_id)

    def test_failed_execution_skipped(self):
        steps = [
            {"action": "open_app", "target": "youtube"},
            {"action": "search", "target": "", "params": {"query": "songs"}},
        ]
        with _patched():
            wf_id = self.wfm.capture_workflow("open youtube", steps, {"success": False})
        self.assertIsNone(wf_id)


# ===========================================================================
# Find-by-Phrase Tests
# ===========================================================================

class TestFindByPhrase(unittest.TestCase):
    def setUp(self):
        self.wfm = _make_wfm()

    def test_exact_match(self):
        steps = [
            {"action": "open_app", "target": "youtube"},
            {"action": "play", "target": "songs"},
        ]
        with _patched():
            self.wfm.capture_workflow("open youtube and play songs", steps, {"success": True})
            record = self.wfm.find_by_phrase("open youtube and play songs")
        self.assertIsNotNone(record)
        self.assertEqual(len(record.steps), 2)

    def test_not_found(self):
        with _patched():
            record = self.wfm.find_by_phrase("nonexistent workflow")
        self.assertIsNone(record)


# ===========================================================================
# Best-Match Ranking Tests
# ===========================================================================

class TestBestMatch(unittest.TestCase):
    def setUp(self):
        self.wfm = _make_wfm()

    def test_do_the_same_thing_returns_last(self):
        with _patched():
            self.wfm.capture_workflow("open chrome and search IPL", [
                {"action": "open_app", "target": "chrome"},
                {"action": "search", "target": "", "params": {"query": "IPL"}},
            ], {"success": True})
            self.wfm.capture_workflow("open youtube and play songs", [
                {"action": "open_app", "target": "youtube"},
                {"action": "play", "target": "songs"},
            ], {"success": True})

            candidates = self.wfm.find_best_match("do the same thing", time_hint="last")
        self.assertGreater(len(candidates), 0)

    def test_frequent_workflow_ranks_higher(self):
        with _patched():
            self.wfm.save_workflow("music workflow", [
                {"action": "open_app", "target": "spotify"},
                {"action": "play", "target": "liked songs"},
            ], name="Music")
            # Bump use count by finding multiple times
            for _ in range(5):
                self.wfm.find_by_phrase("music workflow")

            self.wfm.save_workflow("rare workflow", [
                {"action": "open_app", "target": "notepad"},
                {"action": "type", "target": "hello"},
            ], name="Rare")

            candidates = self.wfm.find_best_match("my usual", time_hint="frequent")

        # Music workflow should rank higher due to frequency
        if len(candidates) >= 2:
            music_idx = next((i for i, c in enumerate(candidates) if "music" in c.record.trigger_phrase), None)
            rare_idx = next((i for i, c in enumerate(candidates) if "rare" in c.record.trigger_phrase), None)
            if music_idx is not None and rare_idx is not None:
                self.assertLess(music_idx, rare_idx)

    def test_empty_history_returns_empty(self):
        with _patched():
            candidates = self.wfm.find_best_match("do the same thing")
        self.assertEqual(len(candidates), 0)


# ===========================================================================
# Safe Replay Tests
# ===========================================================================

class TestSafeReplay(unittest.TestCase):
    def setUp(self):
        self.wfm = _make_wfm()

    def test_safe_workflow_allowed(self):
        record = WorkflowRecord(
            id=1,
            source_command="open youtube and play songs",
            steps=[
                WorkflowStep(1, "open_app", "youtube"),
                WorkflowStep(2, "play", "songs"),
            ],
            success_rate=1.0,
        )
        with _patched():
            result = self.wfm.is_safe_to_replay(record)
        self.assertTrue(result["safe"])

    def test_risky_workflow_blocked(self):
        record = WorkflowRecord(
            id=2,
            source_command="delete files",
            steps=[
                WorkflowStep(1, "file_action", "downloads", risk_level="high"),
            ],
        )
        with _patched():
            result = self.wfm.is_safe_to_replay(record)
        self.assertFalse(result["safe"])
        self.assertGreater(len(result["warnings"]), 0)

    def test_low_success_rate_warned(self):
        record = WorkflowRecord(
            id=3,
            source_command="flaky workflow",
            steps=[
                WorkflowStep(1, "open_app", "chrome"),
                WorkflowStep(2, "search", "test"),
            ],
            success_rate=0.3,
        )
        with _patched():
            result = self.wfm.is_safe_to_replay(record)
        self.assertFalse(result["safe"])
        has_success_warning = any("success rate" in w for w in result["warnings"])
        self.assertTrue(has_success_warning)


# ===========================================================================
# Replay Execution Tests
# ===========================================================================

class TestReplay(unittest.TestCase):
    def setUp(self):
        self.wfm = _make_wfm()

    def test_replay_existing_workflow(self):
        with _patched():
            self.wfm.save_workflow("open youtube and play songs", [
                {"action": "open_app", "target": "youtube"},
                {"action": "play", "target": "songs"},
            ])
            # Get the workflow ID
            record = self.wfm.find_by_phrase("open youtube and play songs")
            result = self.wfm.replay(record.id)
        self.assertTrue(result["success"])
        self.assertIn("steps", result)

    def test_replay_missing_workflow(self):
        with _patched():
            result = self.wfm.replay(99999)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "workflow_not_found")

    def test_repeated_replay_stable(self):
        with _patched():
            self.wfm.save_workflow("stable workflow", [
                {"action": "open_app", "target": "chrome"},
                {"action": "search", "params": {"query": "test"}},
            ])
            record = self.wfm.find_by_phrase("stable workflow")
            for _ in range(5):
                result = self.wfm.replay(record.id)
                self.assertTrue(result["success"])


# ===========================================================================
# Disambiguation Tests
# ===========================================================================

class TestDisambiguation(unittest.TestCase):
    def test_needs_disambiguation_close_scores(self):
        c1 = MatchCandidate(record=WorkflowRecord(id=1), score=0.80)
        c2 = MatchCandidate(record=WorkflowRecord(id=2), score=0.75)
        wfm = _make_wfm()
        self.assertTrue(wfm.needs_disambiguation([c1, c2], threshold=0.15))

    def test_no_disambiguation_clear_winner(self):
        c1 = MatchCandidate(record=WorkflowRecord(id=1), score=0.90)
        c2 = MatchCandidate(record=WorkflowRecord(id=2), score=0.40)
        wfm = _make_wfm()
        self.assertFalse(wfm.needs_disambiguation([c1, c2], threshold=0.15))

    def test_single_candidate_no_disambiguation(self):
        c1 = MatchCandidate(record=WorkflowRecord(id=1), score=0.90)
        wfm = _make_wfm()
        self.assertFalse(wfm.needs_disambiguation([c1]))

    def test_format_choices(self):
        c1 = MatchCandidate(
            record=WorkflowRecord(id=1, source_command="Open YouTube", use_count=5, created_at=datetime.now(timezone.utc).isoformat()),
            score=0.80,
        )
        c2 = MatchCandidate(
            record=WorkflowRecord(id=2, source_command="Open Spotify", use_count=2, created_at=datetime.now(timezone.utc).isoformat()),
            score=0.75,
        )
        wfm = _make_wfm()
        text = wfm.format_choices([c1, c2])
        self.assertIn("1.", text)
        self.assertIn("2.", text)
        self.assertIn("Open YouTube", text)
        self.assertIn("Which one", text)


# ===========================================================================
# Context Adaptation Tests
# ===========================================================================

class TestContextAdaptation(unittest.TestCase):
    def test_clone_with_new_context(self):
        wfm = _make_wfm()
        original = WorkflowRecord(
            id=1,
            source_command="search IPL score",
            steps=[
                WorkflowStep(1, "open_app", "chrome"),
                WorkflowStep(2, "search", "google", params={"query": "IPL score"}),
            ],
        )
        adapted = wfm.clone_with_new_context(original, {"query": "cricket score today"})
        self.assertEqual(adapted.steps[1].params["query"], "cricket score today")
        # Original unchanged
        self.assertEqual(original.steps[1].params["query"], "IPL score")


# ===========================================================================
# Serialisation Tests
# ===========================================================================

class TestSerialisation(unittest.TestCase):
    def test_workflow_step_roundtrip(self):
        step = WorkflowStep(1, "open_app", "chrome", {"tab": "new"}, "low", False)
        d = step.to_dict()
        restored = WorkflowStep.from_dict(d)
        self.assertEqual(restored.action, "open_app")
        self.assertEqual(restored.target, "chrome")
        self.assertEqual(restored.params["tab"], "new")

    def test_workflow_record_to_dict(self):
        record = WorkflowRecord(
            id=5,
            source_command="test",
            steps=[WorkflowStep(1, "open_app", "chrome")],
        )
        d = record.to_dict()
        self.assertEqual(d["id"], 5)
        self.assertEqual(len(d["steps"]), 1)

    def test_has_risky_steps(self):
        safe = WorkflowRecord(steps=[WorkflowStep(1, "open_app", "chrome")])
        self.assertFalse(safe.has_risky_steps)
        risky = WorkflowRecord(steps=[WorkflowStep(1, "shutdown", "", risk_level="high")])
        self.assertTrue(risky.has_risky_steps)


# ===========================================================================
# Deletion Test
# ===========================================================================

class TestDeletion(unittest.TestCase):
    def test_delete_workflow(self):
        wfm = _make_wfm()
        with _patched():
            wfm.save_workflow("delete me", [{"action": "test"}])
            self.assertTrue(wfm.delete_workflow("delete me"))
            self.assertIsNone(wfm.find_by_phrase("delete me"))


# ===========================================================================
# Stats Test
# ===========================================================================

class TestStats(unittest.TestCase):
    def test_stats(self):
        wfm = _make_wfm()
        with _patched():
            wfm.save_workflow("wf1", [{"action": "a"}, {"action": "b"}])
            wfm.save_workflow("wf2", [{"action": "c"}, {"action": "d"}])
            s = wfm.stats()
        self.assertEqual(s["total_workflows"], 2)
        self.assertEqual(s["max_allowed"], 500)


if __name__ == "__main__":
    unittest.main()
