"""
Tests for PHASE 31 — Local Memory Database.

Covers:
  - MemoryStore low-level operations (init, query, backup, corrupt recovery)
  - MemoryManager preferences (CRUD, list, normalize)
  - MemoryManager frequent commands (record, increment, top)
  - MemoryManager people/names (save, find, alias lookup, delete)
  - MemoryManager contacts (save, find by platform, alias, delete)
  - MemoryManager history (record, retrieve, clear, retention)
  - MemoryManager workflows (save, find, use-count, delete)
  - Cross-category search
  - Stats and export
  - Unicode names
  - Thread safety (concurrent writes)
  - DB persistence across restart
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.memory_schema import SCHEMA_VERSION
from core.memory_store import MemoryStore
from core.memory import MemoryManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_db() -> Path:
    """Return a unique temp DB path."""
    d = tempfile.mkdtemp()
    return Path(d) / "test_memory.db"


def _make_manager(db_path: Path | None = None) -> MemoryManager:
    """Create a MemoryManager backed by a temp database with mocked settings."""
    store = MemoryStore(db_path or _tmp_db())
    mgr = MemoryManager(store=store)
    with patch("core.memory.settings") as mock_s, \
         patch("core.memory.state"):
        mock_s.get = lambda key, default=None: {
            "memory_enabled": True,
            "history_retention_days": 90,
            "auto_learn_preferences": True,
            "store_interaction_history": True,
            "memory_backup_on_exit": False,
        }.get(key, default)
        mgr.init()
    mgr._settings_patch = patch("core.memory.settings")
    mgr._state_patch = patch("core.memory.state")
    return mgr


def _patched(mgr: MemoryManager):
    """Return a context-manager pair for settings+state."""
    return _multi_patch()


class _multi_patch:
    """Simple combined patch for settings and state in memory module."""
    def __enter__(self):
        self._s = patch("core.memory.settings")
        self._st = patch("core.memory.state")
        mock_s = self._s.__enter__()
        self._st.__enter__()
        mock_s.get = lambda key, default=None: {
            "memory_enabled": True,
            "history_retention_days": 90,
            "auto_learn_preferences": True,
            "store_interaction_history": True,
            "memory_backup_on_exit": False,
        }.get(key, default)
        return mock_s

    def __exit__(self, *args):
        self._s.__exit__(*args)
        self._st.__exit__(*args)


# ===========================================================================
# MemoryStore Tests
# ===========================================================================

class TestMemoryStoreInit(unittest.TestCase):
    def test_creates_db_file(self):
        db_path = _tmp_db()
        store = MemoryStore(db_path)
        store.init_db()
        self.assertTrue(db_path.exists())
        store.close()
        shutil.rmtree(db_path.parent, ignore_errors=True)

    def test_tables_created(self):
        db_path = _tmp_db()
        store = MemoryStore(db_path)
        store.init_db()
        for table in ["preferences", "frequent_commands", "people", "contacts",
                       "interaction_history", "workflows", "schema_version"]:
            self.assertTrue(store.table_exists(table), f"Table {table} missing")
        store.close()
        shutil.rmtree(db_path.parent, ignore_errors=True)

    def test_schema_version_stamped(self):
        db_path = _tmp_db()
        store = MemoryStore(db_path)
        store.init_db()
        ver = store.query_scalar("SELECT MAX(version) FROM schema_version")
        self.assertEqual(ver, SCHEMA_VERSION)
        store.close()
        shutil.rmtree(db_path.parent, ignore_errors=True)

    def test_double_init_safe(self):
        db_path = _tmp_db()
        store = MemoryStore(db_path)
        store.init_db()
        store.init_db()  # Should not crash
        store.close()
        shutil.rmtree(db_path.parent, ignore_errors=True)


class TestMemoryStoreBackup(unittest.TestCase):
    def test_backup_creates_file(self):
        db_path = _tmp_db()
        store = MemoryStore(db_path)
        store.init_db()
        store.execute("INSERT INTO preferences (key, value) VALUES ('test', 'val')")
        backup_path = store.backup()
        self.assertTrue(backup_path.exists())
        # Verify backup has data
        conn = sqlite3.connect(str(backup_path))
        row = conn.execute("SELECT value FROM preferences WHERE key='test'").fetchone()
        self.assertEqual(row[0], "val")
        conn.close()
        store.close()
        shutil.rmtree(db_path.parent, ignore_errors=True)


class TestMemoryStoreCorruptRecovery(unittest.TestCase):
    def test_corrupt_db_recovered(self):
        db_path = _tmp_db()
        # Write garbage to simulate corruption
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(b"THIS IS NOT A SQLITE DATABASE\x00" * 100)
        store = MemoryStore(db_path)
        store.init_db()
        # Should recover with a fresh DB
        self.assertTrue(store.is_open)
        self.assertTrue(store.table_exists("preferences"))
        store.close()
        shutil.rmtree(db_path.parent, ignore_errors=True)


# ===========================================================================
# Preferences Tests
# ===========================================================================

class TestPreferences(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def tearDown(self):
        self.mgr.shutdown()

    def test_set_and_get(self):
        with _patched(self.mgr):
            self.mgr.set_preference("assistant_name", "Nova")
            self.assertEqual(self.mgr.get_preference("assistant_name"), "Nova")

    def test_get_default(self):
        with _patched(self.mgr):
            self.assertEqual(self.mgr.get_preference("nonexistent", "fallback"), "fallback")

    def test_upsert(self):
        with _patched(self.mgr):
            self.mgr.set_preference("browser", "chrome")
            self.mgr.set_preference("browser", "firefox")
            self.assertEqual(self.mgr.get_preference("browser"), "firefox")

    def test_list_preferences(self):
        with _patched(self.mgr):
            self.mgr.set_preference("key1", "val1")
            self.mgr.set_preference("key2", "val2")
            prefs = self.mgr.list_preferences()
            keys = {p["key"] for p in prefs}
            self.assertIn("key1", keys)
            self.assertIn("key2", keys)

    def test_delete_preference(self):
        with _patched(self.mgr):
            self.mgr.set_preference("temp", "data")
            self.assertTrue(self.mgr.delete_preference("temp"))
            self.assertIsNone(self.mgr.get_preference("temp"))

    def test_key_normalization(self):
        with _patched(self.mgr):
            self.mgr.set_preference("  Preferred Browser  ", "edge")
            self.assertEqual(self.mgr.get_preference("preferred_browser"), "edge")


# ===========================================================================
# Frequent Commands Tests
# ===========================================================================

class TestFrequentCommands(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def tearDown(self):
        self.mgr.shutdown()

    def test_record_and_top(self):
        with _patched(self.mgr):
            self.mgr.record_command("open chrome")
            self.mgr.record_command("open youtube")
            self.mgr.record_command("open chrome")
            top = self.mgr.top_commands(limit=5)
            self.assertEqual(top[0]["normalized_command"], "open chrome")
            self.assertEqual(top[0]["count"], 2)

    def test_count_increments(self):
        with _patched(self.mgr):
            for _ in range(5):
                self.mgr.record_command("play music")
            top = self.mgr.top_commands(limit=1)
            self.assertEqual(top[0]["count"], 5)

    def test_empty_command_ignored(self):
        with _patched(self.mgr):
            self.mgr.record_command("")
            self.mgr.record_command("   ")
            top = self.mgr.top_commands()
            self.assertEqual(len(top), 0)


# ===========================================================================
# People Tests
# ===========================================================================

class TestPeople(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def tearDown(self):
        self.mgr.shutdown()

    def test_save_and_find(self):
        with _patched(self.mgr):
            self.mgr.remember_person("Hemanth", category="friend")
            person = self.mgr.find_person("Hemanth")
            self.assertIsNotNone(person)
            self.assertEqual(person["name"], "Hemanth")
            self.assertEqual(person["category"], "friend")

    def test_find_by_alias(self):
        with _patched(self.mgr):
            self.mgr.remember_person("Mom", aliases=["mother", "mama"])
            person = self.mgr.find_person("mother")
            self.assertIsNotNone(person)
            self.assertEqual(person["name"], "Mom")

    def test_case_insensitive_find(self):
        with _patched(self.mgr):
            self.mgr.remember_person("Boss")
            person = self.mgr.find_person("boss")
            self.assertIsNotNone(person)

    def test_update_existing(self):
        with _patched(self.mgr):
            self.mgr.remember_person("Hemanth", notes="old")
            self.mgr.remember_person("Hemanth", notes="updated")
            person = self.mgr.find_person("Hemanth")
            self.assertEqual(person["notes"], "updated")

    def test_list_people(self):
        with _patched(self.mgr):
            self.mgr.remember_person("Alice")
            self.mgr.remember_person("Bob")
            people = self.mgr.list_people()
            self.assertEqual(len(people), 2)

    def test_delete_person(self):
        with _patched(self.mgr):
            self.mgr.remember_person("Temp")
            self.assertTrue(self.mgr.delete_person("Temp"))
            self.assertIsNone(self.mgr.find_person("Temp"))

    def test_unicode_names(self):
        with _patched(self.mgr):
            self.mgr.remember_person("रमेश", aliases=["Ramesh"])
            person = self.mgr.find_person("रमेश")
            self.assertIsNotNone(person)
            self.assertEqual(person["name"], "रमेश")

    def test_not_found_returns_none(self):
        with _patched(self.mgr):
            self.assertIsNone(self.mgr.find_person("Nobody"))


# ===========================================================================
# Contacts Tests
# ===========================================================================

class TestContacts(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def tearDown(self):
        self.mgr.shutdown()

    def test_save_and_find(self):
        with _patched(self.mgr):
            self.mgr.save_contact("Rakesh", "whatsapp")
            results = self.mgr.find_contact("Rakesh", "whatsapp")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["platform"], "whatsapp")

    def test_find_without_platform(self):
        with _patched(self.mgr):
            self.mgr.save_contact("Hemanth", "whatsapp")
            self.mgr.save_contact("Hemanth", "telegram")
            results = self.mgr.find_contact("Hemanth")
            self.assertEqual(len(results), 2)

    def test_find_by_alias(self):
        with _patched(self.mgr):
            self.mgr.save_contact("Mom", "whatsapp", aliases=["mother"])
            results = self.mgr.find_contact("mother")
            self.assertEqual(len(results), 1)

    def test_update_existing(self):
        with _patched(self.mgr):
            self.mgr.save_contact("Rakesh", "whatsapp", external_id="old_id")
            self.mgr.save_contact("Rakesh", "whatsapp", external_id="new_id")
            results = self.mgr.find_contact("Rakesh", "whatsapp")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["external_id"], "new_id")

    def test_delete_contact(self):
        with _patched(self.mgr):
            self.mgr.save_contact("Temp", "whatsapp")
            self.assertTrue(self.mgr.delete_contact("Temp", "whatsapp"))
            self.assertEqual(len(self.mgr.find_contact("Temp")), 0)


# ===========================================================================
# History Tests
# ===========================================================================

class TestHistory(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def tearDown(self):
        self.mgr.shutdown()

    def test_record_and_retrieve(self):
        with _patched(self.mgr):
            self.mgr.record_interaction("open chrome", "open_app", "launched chrome", True)
            history = self.mgr.recent_history(limit=5)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["intent"], "open_app")
            self.assertTrue(history[0]["success"])

    def test_order_newest_first(self):
        with _patched(self.mgr):
            self.mgr.record_interaction("first", "a", "a", True)
            self.mgr.record_interaction("second", "b", "b", True)
            history = self.mgr.recent_history()
            self.assertEqual(history[0]["user_input"], "second")

    def test_clear_history(self):
        with _patched(self.mgr):
            self.mgr.record_interaction("test", "t", "t", True)
            deleted = self.mgr.clear_history()
            self.assertGreater(deleted, 0)
            self.assertEqual(len(self.mgr.recent_history()), 0)


# ===========================================================================
# Workflow Tests
# ===========================================================================

class TestWorkflows(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def tearDown(self):
        self.mgr.shutdown()

    def test_save_and_find(self):
        with _patched(self.mgr):
            steps = [{"action": "open_app", "target": "youtube"}, {"action": "search", "query": "music"}]
            self.mgr.save_workflow("open youtube and play songs", steps, name="Music Flow")
            wf = self.mgr.find_workflow("open youtube and play songs")
            self.assertIsNotNone(wf)
            self.assertEqual(len(wf["steps"]), 2)
            self.assertEqual(wf["name"], "Music Flow")

    def test_use_count_increments(self):
        with _patched(self.mgr):
            self.mgr.save_workflow("test workflow", [{"action": "test"}])
            self.mgr.find_workflow("test workflow")  # reads 0, bumps to 1
            self.mgr.find_workflow("test workflow")  # reads 1, bumps to 2
            wf = self.mgr.find_workflow("test workflow")  # reads 2, bumps to 3
            # The returned row shows the count *before* this call's bump
            self.assertEqual(wf["use_count"], 2)

    def test_update_existing(self):
        with _patched(self.mgr):
            self.mgr.save_workflow("update me", [{"old": True}])
            self.mgr.save_workflow("update me", [{"new": True}])
            wf = self.mgr.find_workflow("update me")
            self.assertTrue(wf["steps"][0].get("new"))

    def test_list_workflows(self):
        with _patched(self.mgr):
            self.mgr.save_workflow("wf1", [])
            self.mgr.save_workflow("wf2", [])
            wfs = self.mgr.list_workflows()
            self.assertEqual(len(wfs), 2)

    def test_delete_workflow(self):
        with _patched(self.mgr):
            self.mgr.save_workflow("temp wf", [])
            self.assertTrue(self.mgr.delete_workflow("temp wf"))
            self.assertIsNone(self.mgr.find_workflow("temp wf"))


# ===========================================================================
# Search Tests
# ===========================================================================

class TestSearchMemory(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def tearDown(self):
        self.mgr.shutdown()

    def test_search_across_categories(self):
        with _patched(self.mgr):
            self.mgr.set_preference("browser", "chrome")
            self.mgr.remember_person("Chrome Dev")
            results = self.mgr.search_memory("chrome")
            categories = {r["category"] for r in results}
            self.assertIn("preference", categories)
            self.assertIn("person", categories)

    def test_empty_query_returns_empty(self):
        with _patched(self.mgr):
            self.assertEqual(self.mgr.search_memory(""), [])


# ===========================================================================
# Stats & Export Tests
# ===========================================================================

class TestStatsAndExport(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def tearDown(self):
        self.mgr.shutdown()

    def test_stats(self):
        with _patched(self.mgr):
            self.mgr.set_preference("k", "v")
            self.mgr.remember_person("Test")
            s = self.mgr.stats()
            self.assertEqual(s["preferences"], 1)
            self.assertEqual(s["people"], 1)
            self.assertEqual(s["contacts"], 0)

    def test_export_json(self):
        with _patched(self.mgr):
            self.mgr.set_preference("key", "val")
            export = self.mgr.export_json()
            self.assertIn("preferences", export)
            self.assertIn("stats", export)


# ===========================================================================
# Persistence Test
# ===========================================================================

class TestPersistence(unittest.TestCase):
    def test_data_survives_restart(self):
        db_path = _tmp_db()
        mgr1 = _make_manager(db_path)
        with _patched(mgr1):
            mgr1.set_preference("persist_key", "persist_val")
            mgr1.remember_person("Persistent Person")
        mgr1._store.close()

        # Re-open same DB
        mgr2 = _make_manager(db_path)
        with _patched(mgr2):
            self.assertEqual(mgr2.get_preference("persist_key"), "persist_val")
            person = mgr2.find_person("Persistent Person")
            self.assertIsNotNone(person)
        mgr2.shutdown()
        shutil.rmtree(db_path.parent, ignore_errors=True)


# ===========================================================================
# Thread Safety Test
# ===========================================================================

class TestThreadSafety(unittest.TestCase):
    def test_concurrent_writes(self):
        mgr = _make_manager()
        errors: list[str] = []

        def worker(n):
            try:
                with _patched(mgr):
                    for i in range(20):
                        mgr.record_command(f"thread_{n}_cmd_{i}")
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")
        with _patched(mgr):
            top = mgr.top_commands(limit=100)
            self.assertEqual(len(top), 80)  # 4 threads × 20 unique commands
        mgr.shutdown()


# ===========================================================================
# Delete Memory Tests
# ===========================================================================

class TestDeleteMemory(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def tearDown(self):
        self.mgr.shutdown()

    def test_delete_by_category(self):
        with _patched(self.mgr):
            self.mgr.set_preference("k", "v")
            self.mgr.remember_person("Test")
            self.mgr.delete_memory(category="preferences")
            self.assertEqual(self.mgr.stats()["preferences"], 0)
            self.assertEqual(self.mgr.stats()["people"], 1)

    def test_delete_all(self):
        with _patched(self.mgr):
            self.mgr.set_preference("k", "v")
            self.mgr.remember_person("Test")
            self.mgr.record_command("test cmd")
            self.mgr.delete_memory()
            s = self.mgr.stats()
            self.assertEqual(sum(s.values()), 0)


if __name__ == "__main__":
    unittest.main()
