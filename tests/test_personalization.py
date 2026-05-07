"""
Tests for PHASE 33 — Personalization Engine.

Covers:
  - Signal recording (basic, weighted, source tracking)
  - Interaction-based learning (apps, browser, music, contacts)
  - Preference inference with confidence scoring
  - Explicit override always wins
  - Preference retrieval and listing
  - Option ranking by preference
  - Personalised defaults application
  - Forget / privacy controls
  - Drift handling (old preference decays)
  - Recomputation from raw signals
  - Edge cases (empty, disabled, no evidence)
  - Profile summary
  - Stats
  - Schema migration (v1 → v2 tables exist)
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.memory_store import MemoryStore
from core.memory import MemoryManager
from core.personalization import (
    PersonalizationEngine,
    UserPreference,
    PreferenceSignal,
    Category,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_db() -> Path:
    d = tempfile.mkdtemp()
    return Path(d) / "test_pers.db"


def _settings():
    return {
        "memory_enabled": True,
        "history_retention_days": 90,
        "auto_learn_preferences": True,
        "store_interaction_history": True,
        "memory_backup_on_exit": False,
        "personalization_enabled": True,
        "preference_decay_enabled": True,
        "explicit_preferences_override_inferred": True,
        "allow_personalized_defaults": True,
        "workflow_memory_enabled": False,
    }


def _make_engine(db_path: Path | None = None) -> PersonalizationEngine:
    store = MemoryStore(db_path or _tmp_db())
    mem = MemoryManager(store=store)
    with patch("core.memory.settings") as ms, patch("core.memory.state"):
        ms.get = lambda key, default=None: _settings().get(key, default)
        mem.init()

    engine = PersonalizationEngine(memory=mem)
    with patch("core.personalization.settings") as ps, patch("core.personalization.state"):
        ps.get = lambda key, default=None: _settings().get(key, default)
        engine.init()
    return engine


class _patched:
    def __enter__(self):
        self._patches = [
            patch("core.memory.settings"),
            patch("core.memory.state"),
            patch("core.personalization.settings"),
            patch("core.personalization.state"),
        ]
        mocks = [p.__enter__() for p in self._patches]
        for m in mocks:
            if hasattr(m, "get"):
                m.get = lambda key, default=None: _settings().get(key, default)
        return mocks

    def __exit__(self, *args):
        for p in reversed(self._patches):
            p.__exit__(*args)


# ===========================================================================
# Signal Recording Tests
# ===========================================================================

class TestSignalRecording(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()

    def test_record_basic_signal(self):
        with _patched():
            self.engine.record_signal(Category.BROWSER, "default", "chrome")
            signals = self.engine._get_signals("browser", "default")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["value"], "chrome")

    def test_record_weighted_signal(self):
        with _patched():
            self.engine.record_signal(Category.BROWSER, "default", "chrome", weight=2.0)
            signals = self.engine._get_signals("browser", "default")
        self.assertEqual(float(signals[0]["weight"]), 2.0)

    def test_record_multiple_signals(self):
        with _patched():
            for _ in range(5):
                self.engine.record_signal(Category.APP, "default", "chrome")
            signals = self.engine._get_signals("app", "default")
        self.assertEqual(len(signals), 5)

    def test_empty_value_ignored(self):
        with _patched():
            self.engine.record_signal(Category.BROWSER, "default", "")
            signals = self.engine._get_signals("browser", "default")
        self.assertEqual(len(signals), 0)


# ===========================================================================
# Interaction Learning Tests
# ===========================================================================

class TestInteractionLearning(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()

    def test_learn_browser_from_open_app(self):
        with _patched():
            for _ in range(5):
                self.engine.learn_from_interaction("open chrome", "open_app", "chrome")
            pref = self.engine.get_preference(Category.BROWSER)
        self.assertIsNotNone(pref)
        self.assertEqual(pref.value, "chrome")

    def test_learn_music_app(self):
        with _patched():
            for _ in range(5):
                self.engine.learn_from_interaction("play music", "open_app", "spotify")
            pref = self.engine.get_preference(Category.MUSIC_APP)
        self.assertIsNotNone(pref)
        self.assertEqual(pref.value, "spotify")

    def test_learn_frequent_contact(self):
        with _patched():
            for _ in range(4):
                self.engine.learn_from_interaction("call hemanth", "call", "hemanth")
            pref = self.engine.get_preference(Category.CONTACT, "frequent")
        self.assertIsNotNone(pref)
        self.assertEqual(pref.value, "hemanth")

    def test_failed_interaction_not_learned(self):
        with _patched():
            self.engine.learn_from_interaction("open chrome", "open_app", "chrome", success=False)
            signals = self.engine._get_signals("browser", "default")
        self.assertEqual(len(signals), 0)


# ===========================================================================
# Preference Inference Tests
# ===========================================================================

class TestPreferenceInference(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()

    def test_confidence_increases_with_evidence(self):
        with _patched():
            self.engine.record_signal(Category.BROWSER, "default", "chrome")
            p1 = self.engine.get_preference(Category.BROWSER)
            for _ in range(4):
                self.engine.record_signal(Category.BROWSER, "default", "chrome")
            p2 = self.engine.get_preference(Category.BROWSER)
        self.assertGreater(p2.confidence, p1.confidence)

    def test_dominant_value_wins(self):
        with _patched():
            for _ in range(7):
                self.engine.record_signal(Category.BROWSER, "default", "chrome")
            for _ in range(3):
                self.engine.record_signal(Category.BROWSER, "default", "edge")
            pref = self.engine.get_preference(Category.BROWSER)
        self.assertEqual(pref.value, "chrome")

    def test_no_preference_without_signals(self):
        with _patched():
            pref = self.engine.get_preference(Category.BROWSER)
        self.assertIsNone(pref)


# ===========================================================================
# Explicit Override Tests
# ===========================================================================

class TestExplicitOverride(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()

    def test_explicit_overrides_inferred(self):
        with _patched():
            for _ in range(10):
                self.engine.record_signal(Category.BROWSER, "default", "chrome")
            self.engine.set_explicit_preference(Category.BROWSER, "default", "edge")
            pref = self.engine.get_preference(Category.BROWSER)
        self.assertEqual(pref.value, "edge")
        self.assertTrue(pref.is_explicit)
        self.assertEqual(pref.confidence, 1.0)

    def test_explicit_not_overwritten_by_signals(self):
        with _patched():
            self.engine.set_explicit_preference(Category.BROWSER, "default", "edge")
            for _ in range(15):
                self.engine.record_signal(Category.BROWSER, "default", "chrome")
            pref = self.engine.get_preference(Category.BROWSER)
        # Explicit should still be edge
        self.assertEqual(pref.value, "edge")
        self.assertTrue(pref.is_explicit)


# ===========================================================================
# Ranking Tests
# ===========================================================================

class TestRanking(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()

    def test_rank_by_usage(self):
        with _patched():
            for _ in range(8):
                self.engine.record_signal(Category.CONTACT, "frequent", "hemanth")
            for _ in range(3):
                self.engine.record_signal(Category.CONTACT, "frequent", "mom")
            ranked = self.engine.rank_options(Category.CONTACT, ["mom", "hemanth", "boss"], key="frequent")
        # Hemanth should rank first
        self.assertEqual(ranked[0][0], "hemanth")

    def test_rank_empty_candidates(self):
        with _patched():
            result = self.engine.rank_options(Category.APP, [])
        self.assertEqual(result, [])


# ===========================================================================
# Apply Defaults Tests
# ===========================================================================

class TestApplyDefaults(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()

    def test_search_gets_preferred_browser(self):
        with _patched():
            for _ in range(5):
                self.engine.record_signal(Category.BROWSER, "default", "chrome")
            result = self.engine.apply_defaults("search", {"query": "IPL score"})
        self.assertEqual(result.get("browser"), "chrome")

    def test_play_gets_preferred_app(self):
        with _patched():
            for _ in range(5):
                self.engine.record_signal(Category.MUSIC_APP, "default", "spotify")
            result = self.engine.apply_defaults("play_music", {})
        self.assertEqual(result.get("app"), "spotify")

    def test_existing_value_not_overwritten(self):
        with _patched():
            for _ in range(5):
                self.engine.record_signal(Category.BROWSER, "default", "chrome")
            result = self.engine.apply_defaults("search", {"browser": "edge"})
        self.assertEqual(result["browser"], "edge")


# ===========================================================================
# Forget Tests
# ===========================================================================

class TestForget(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()

    def test_forget_preference(self):
        with _patched():
            for _ in range(5):
                self.engine.record_signal(Category.BROWSER, "default", "chrome")
            self.assertIsNotNone(self.engine.get_preference(Category.BROWSER))
            self.engine.forget_preference(Category.BROWSER, "default")
            self.assertIsNone(self.engine.get_preference(Category.BROWSER))

    def test_forget_category(self):
        with _patched():
            self.engine.record_signal(Category.BROWSER, "default", "chrome")
            self.engine.record_signal(Category.BROWSER, "search", "firefox")
            self.engine.forget_preference(Category.BROWSER)
            prefs = self.engine.get_all_preferences(Category.BROWSER)
        self.assertEqual(len(prefs), 0)

    def test_forget_all(self):
        with _patched():
            self.engine.record_signal(Category.BROWSER, "default", "chrome")
            self.engine.record_signal(Category.MUSIC_APP, "default", "spotify")
            self.engine.forget_all()
            stats = self.engine.stats()
        self.assertEqual(stats["total_preferences"], 0)
        self.assertEqual(stats["total_signals"], 0)


# ===========================================================================
# Drift Handling Tests
# ===========================================================================

class TestDriftHandling(unittest.TestCase):
    def test_old_preference_decays(self):
        """Newer dominant usage should overtake an old preference."""
        engine = _make_engine()
        with _patched():
            # Simulate old signals for chrome (manually insert with old timestamp)
            for _ in range(5):
                engine._store.execute(
                    "INSERT INTO preference_signals (category, key, value, weight, source, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("browser", "default", "chrome", 1.0, "auto", "2025-01-01T00:00:00+00:00"),
                )
            # Add fresh signals for edge
            for _ in range(5):
                engine.record_signal(Category.BROWSER, "default", "edge")

            pref = engine.get_preference(Category.BROWSER)

        # Edge should now be preferred due to decay on old chrome signals
        self.assertEqual(pref.value, "edge")


# ===========================================================================
# Recomputation Tests
# ===========================================================================

class TestRecomputation(unittest.TestCase):
    def test_recompute_all(self):
        engine = _make_engine()
        with _patched():
            for _ in range(5):
                engine.record_signal(Category.BROWSER, "default", "chrome")
            for _ in range(3):
                engine.record_signal(Category.MUSIC_APP, "default", "spotify")

            # Clear computed preferences but keep signals
            engine._store.execute("DELETE FROM user_preferences")
            self.assertIsNone(engine.get_preference(Category.BROWSER))

            count = engine.recompute_preferences()
            self.assertEqual(count, 2)
            self.assertIsNotNone(engine.get_preference(Category.BROWSER))
            self.assertIsNotNone(engine.get_preference(Category.MUSIC_APP))


# ===========================================================================
# Stats & Profile Tests
# ===========================================================================

class TestStatsAndProfile(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()

    def test_stats(self):
        with _patched():
            for _ in range(3):
                self.engine.record_signal(Category.BROWSER, "default", "chrome")
            self.engine.set_explicit_preference(Category.MUSIC_APP, "default", "spotify")
            s = self.engine.stats()
        self.assertGreater(s["total_signals"], 0)
        self.assertEqual(s["explicit_count"], 1)

    def test_profile_summary(self):
        with _patched():
            self.engine.set_explicit_preference(Category.BROWSER, "default", "chrome")
            summary = self.engine.profile_summary()
        self.assertGreater(len(summary), 0)
        self.assertEqual(summary[0]["value"], "chrome")
        self.assertEqual(summary[0]["source"], "set by you")


# ===========================================================================
# Disabled Engine Test
# ===========================================================================

class TestDisabled(unittest.TestCase):
    def test_disabled_skips_signals(self):
        store = MemoryStore(_tmp_db())
        mem = MemoryManager(store=store)
        with patch("core.memory.settings") as ms, patch("core.memory.state"):
            ms.get = lambda key, default=None: _settings().get(key, default)
            mem.init()

        engine = PersonalizationEngine(memory=mem)
        disabled_settings = {**_settings(), "auto_learn_preferences": False}
        with patch("core.personalization.settings") as ps, patch("core.personalization.state"):
            ps.get = lambda key, default=None: disabled_settings.get(key, default)
            engine.init()
            engine.record_signal(Category.BROWSER, "default", "chrome")
            signals = engine._get_signals("browser", "default")
        self.assertEqual(len(signals), 0)


# ===========================================================================
# Schema Migration Test
# ===========================================================================

class TestSchemaMigration(unittest.TestCase):
    def test_new_tables_exist(self):
        engine = _make_engine()
        self.assertTrue(engine._store.table_exists("preference_signals"))
        self.assertTrue(engine._store.table_exists("user_preferences"))


# ===========================================================================
# Data Class Tests
# ===========================================================================

class TestDataClasses(unittest.TestCase):
    def test_signal_to_dict(self):
        s = PreferenceSignal("browser", "default", "chrome", 1.5, "auto", "2025-01-01")
        d = s.to_dict()
        self.assertEqual(d["category"], "browser")
        self.assertEqual(d["weight"], 1.5)

    def test_preference_to_dict(self):
        p = UserPreference("browser", "default", "chrome", 0.85, 5, False, "2025-01-01")
        d = p.to_dict()
        self.assertEqual(d["confidence"], 0.85)
        self.assertFalse(d["is_explicit"])


if __name__ == "__main__":
    unittest.main()
