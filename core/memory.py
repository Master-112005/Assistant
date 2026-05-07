"""
High-level memory API for the Nova Assistant.

Provides typed, thread-safe methods for storing and retrieving:
  - User preferences
  - Frequently used commands
  - People / names
  - Platform-linked contacts
  - Interaction history
  - Reusable workflows
  - Cross-category search

All data lives in a local SQLite database (``data/memory.db``).
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import settings, state
from core.logger import get_logger
from core.memory_store import MemoryStore

logger = get_logger(__name__)


class MemoryManager:
    """
    Central API for assistant memory operations.

    Thread-safe — every method acquires the store's internal lock.
    """

    def __init__(self, store: MemoryStore | None = None) -> None:
        self._store = store or MemoryStore()
        self._ready = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def init(self) -> None:
        """Initialise the memory database."""
        if self._ready:
            return
        if not settings.get("memory_enabled"):
            logger.info("Memory system disabled by settings")
            return
        self._store.init_db()
        self._ready = True
        state.memory_ready = True
        logger.info("MemoryManager initialised")

    def shutdown(self) -> None:
        """Flush and close the database."""
        if not self._ready:
            return
        if settings.get("memory_backup_on_exit"):
            try:
                self.backup()
            except Exception as exc:
                logger.warning("Backup on shutdown failed: %s", exc)
        self._store.close()
        self._ready = False
        state.memory_ready = False
        logger.info("MemoryManager shut down")

    @property
    def ready(self) -> bool:
        return self._ready

    # ================================================================== #
    # Preferences                                                        #
    # ================================================================== #

    def set_preference(self, key: str, value: str) -> None:
        """Store or update a user preference."""
        self._ensure_ready()
        key = self._normalise_key(key)
        now = self._utc_iso()
        self._store.execute(
            """
            INSERT INTO preferences (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, str(value), now),
        )
        state.last_saved_preference = {"key": key, "value": value}
        logger.info("Preference saved %s=%s", key, value)

    def get_preference(self, key: str, default: str | None = None) -> str | None:
        """Retrieve a preference by key."""
        self._ensure_ready()
        row = self._store.query_one(
            "SELECT value FROM preferences WHERE key = ?",
            (self._normalise_key(key),),
        )
        result = row["value"] if row else default
        if result is not None:
            state.last_memory_hit = {"type": "preference", "key": key, "value": result}
        return result

    def list_preferences(self) -> list[dict[str, Any]]:
        """Return all stored preferences."""
        self._ensure_ready()
        return self._store.query("SELECT key, value, updated_at FROM preferences ORDER BY key")

    def delete_preference(self, key: str) -> bool:
        """Delete a single preference. Returns True if removed."""
        self._ensure_ready()
        cursor = self._store.execute(
            "DELETE FROM preferences WHERE key = ?",
            (self._normalise_key(key),),
        )
        return cursor.rowcount > 0

    # ================================================================== #
    # Frequent Commands                                                  #
    # ================================================================== #

    def record_command(self, text: str) -> None:
        """Record or increment a user command."""
        self._ensure_ready()
        normalised = self._normalise_command(text)
        if not normalised:
            return
        now = self._utc_iso()

        existing = self._store.query_one(
            "SELECT id, count FROM frequent_commands WHERE normalized_command = ?",
            (normalised,),
        )
        if existing:
            self._store.execute(
                "UPDATE frequent_commands SET count = count + 1, last_used_at = ? WHERE id = ?",
                (now, existing["id"]),
            )
        else:
            self._store.execute(
                "INSERT INTO frequent_commands (command_text, normalized_command, count, last_used_at) VALUES (?, ?, 1, ?)",
                (text.strip(), normalised, now),
            )
        logger.debug("Command recorded: %s", normalised)

    def top_commands(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return the most frequently used commands."""
        self._ensure_ready()
        return self._store.query(
            "SELECT command_text, normalized_command, count, last_used_at "
            "FROM frequent_commands ORDER BY count DESC, last_used_at DESC LIMIT ?",
            (limit,),
        )

    # ================================================================== #
    # People / Names                                                     #
    # ================================================================== #

    def remember_person(
        self,
        name: str,
        *,
        aliases: list[str] | None = None,
        category: str = "",
        notes: str = "",
    ) -> int:
        """
        Store a person.  Returns the row id.

        If a person with the same name exists, updates aliases/category/notes.
        """
        self._ensure_ready()
        name = name.strip()
        aliases_json = json.dumps(aliases or [], ensure_ascii=False)
        now = self._utc_iso()

        existing = self._store.query_one(
            "SELECT id FROM people WHERE name = ? COLLATE NOCASE",
            (name,),
        )
        if existing:
            self._store.execute(
                "UPDATE people SET aliases = ?, category = ?, notes = ?, last_used_at = ? WHERE id = ?",
                (aliases_json, category, notes, now, existing["id"]),
            )
            logger.info("Person updated: %s", name)
            return existing["id"]

        cursor = self._store.execute(
            "INSERT INTO people (name, aliases, category, notes, created_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, aliases_json, category, notes, now, now),
        )
        logger.info("Person saved: %s", name)
        return cursor.lastrowid

    def find_person(self, name: str) -> dict[str, Any] | None:
        """Find a person by name or alias (case-insensitive)."""
        self._ensure_ready()
        name = name.strip()

        # Direct match
        row = self._store.query_one(
            "SELECT * FROM people WHERE name = ? COLLATE NOCASE",
            (name,),
        )
        if row:
            row["aliases"] = self._parse_json_list(row.get("aliases", "[]"))
            state.last_memory_hit = {"type": "person", "name": row["name"]}
            return row

        # Alias search
        rows = self._store.query("SELECT * FROM people")
        needle = name.lower()
        for r in rows:
            aliases = self._parse_json_list(r.get("aliases", "[]"))
            if any(a.lower() == needle for a in aliases):
                r["aliases"] = aliases
                state.last_memory_hit = {"type": "person", "name": r["name"]}
                return r
        return None

    def list_people(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return all known people, most recent first."""
        self._ensure_ready()
        rows = self._store.query(
            "SELECT * FROM people ORDER BY last_used_at DESC LIMIT ?",
            (limit,),
        )
        for r in rows:
            r["aliases"] = self._parse_json_list(r.get("aliases", "[]"))
        return rows

    def delete_person(self, name: str) -> bool:
        """Delete a person by name."""
        self._ensure_ready()
        cursor = self._store.execute(
            "DELETE FROM people WHERE name = ? COLLATE NOCASE",
            (name.strip(),),
        )
        return cursor.rowcount > 0

    # ================================================================== #
    # Contacts                                                           #
    # ================================================================== #

    def save_contact(
        self,
        name: str,
        platform: str = "",
        *,
        external_id: str = "",
        aliases: list[str] | None = None,
    ) -> int:
        """Save or update a platform-linked contact."""
        self._ensure_ready()
        name = name.strip()
        platform = platform.strip().lower()
        aliases_json = json.dumps(aliases or [], ensure_ascii=False)
        now = self._utc_iso()

        existing = self._store.query_one(
            "SELECT id FROM contacts WHERE name = ? COLLATE NOCASE AND platform = ? COLLATE NOCASE",
            (name, platform),
        )
        if existing:
            self._store.execute(
                "UPDATE contacts SET external_id = ?, aliases = ?, last_used_at = ? WHERE id = ?",
                (external_id, aliases_json, now, existing["id"]),
            )
            return existing["id"]

        cursor = self._store.execute(
            "INSERT INTO contacts (name, platform, external_id, aliases, created_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, platform, external_id, aliases_json, now, now),
        )
        logger.info("Contact saved: %s on %s", name, platform)
        return cursor.lastrowid

    def find_contact(
        self, name: str, platform: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find contacts by name, optionally filtering by platform."""
        self._ensure_ready()
        name = name.strip()
        if platform:
            rows = self._store.query(
                "SELECT * FROM contacts WHERE name = ? COLLATE NOCASE AND platform = ? COLLATE NOCASE",
                (name, platform.strip().lower()),
            )
        else:
            rows = self._store.query(
                "SELECT * FROM contacts WHERE name = ? COLLATE NOCASE",
                (name,),
            )

        if not rows:
            # Alias fallback
            all_contacts = self._store.query("SELECT * FROM contacts")
            needle = name.lower()
            rows = [
                c for c in all_contacts
                if any(a.lower() == needle for a in self._parse_json_list(c.get("aliases", "[]")))
            ]

        for r in rows:
            r["aliases"] = self._parse_json_list(r.get("aliases", "[]"))
        return rows

    def delete_contact(self, name: str, platform: str = "") -> bool:
        """Delete a contact by name and optional platform."""
        self._ensure_ready()
        if platform:
            cursor = self._store.execute(
                "DELETE FROM contacts WHERE name = ? COLLATE NOCASE AND platform = ? COLLATE NOCASE",
                (name.strip(), platform.strip().lower()),
            )
        else:
            cursor = self._store.execute(
                "DELETE FROM contacts WHERE name = ? COLLATE NOCASE",
                (name.strip(),),
            )
        return cursor.rowcount > 0

    # ================================================================== #
    # Interaction History                                                 #
    # ================================================================== #

    def record_interaction(
        self,
        user_input: str,
        intent: str,
        action_taken: str,
        success: bool,
    ) -> None:
        """Record a user interaction in history."""
        if not settings.get("store_interaction_history"):
            return
        self._ensure_ready()
        self._store.execute(
            "INSERT INTO interaction_history (user_input, intent, action_taken, success, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_input, intent, action_taken, int(success), self._utc_iso()),
        )
        self._enforce_retention()

    def recent_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent interaction history, newest first."""
        self._ensure_ready()
        rows = self._store.query(
            "SELECT * FROM interaction_history ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        for r in rows:
            r["success"] = bool(r.get("success"))
        return rows

    def clear_history(self) -> int:
        """Delete all interaction history. Returns count deleted."""
        self._ensure_ready()
        cursor = self._store.execute("DELETE FROM interaction_history")
        logger.info("Interaction history cleared (%d rows)", cursor.rowcount)
        return cursor.rowcount

    # ================================================================== #
    # Workflows                                                          #
    # ================================================================== #

    def save_workflow(
        self,
        trigger_phrase: str,
        steps: list[dict[str, Any]],
        *,
        name: str = "",
    ) -> int:
        """Save or update a reusable workflow."""
        self._ensure_ready()
        trigger_phrase = trigger_phrase.strip().lower()
        steps_json = json.dumps(steps, ensure_ascii=False)
        now = self._utc_iso()

        existing = self._store.query_one(
            "SELECT id FROM workflows WHERE trigger_phrase = ? COLLATE NOCASE",
            (trigger_phrase,),
        )
        if existing:
            self._store.execute(
                "UPDATE workflows SET steps_json = ?, name = ?, updated_at = ? WHERE id = ?",
                (steps_json, name, now, existing["id"]),
            )
            logger.info("Workflow updated: %s", trigger_phrase)
            return existing["id"]

        cursor = self._store.execute(
            "INSERT INTO workflows (name, trigger_phrase, steps_json, use_count, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?)",
            (name, trigger_phrase, steps_json, now, now),
        )
        wf_id = cursor.lastrowid
        state.recent_workflow_id = wf_id
        logger.info("Workflow saved id=%d trigger=%s", wf_id, trigger_phrase)
        return wf_id

    def find_workflow(self, trigger_phrase: str) -> dict[str, Any] | None:
        """Find a workflow by trigger phrase."""
        self._ensure_ready()
        row = self._store.query_one(
            "SELECT * FROM workflows WHERE trigger_phrase = ? COLLATE NOCASE",
            (trigger_phrase.strip().lower(),),
        )
        if row:
            row["steps"] = self._parse_json_list(row.get("steps_json", "[]"))
            # Bump use count
            self._store.execute(
                "UPDATE workflows SET use_count = use_count + 1, updated_at = ? WHERE id = ?",
                (self._utc_iso(), row["id"]),
            )
            state.last_memory_hit = {"type": "workflow", "id": row["id"], "trigger": row["trigger_phrase"]}
            state.recent_workflow_id = row["id"]
        return row

    def list_workflows(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return workflows ordered by use count (most used first)."""
        self._ensure_ready()
        rows = self._store.query(
            "SELECT * FROM workflows ORDER BY use_count DESC, updated_at DESC LIMIT ?",
            (limit,),
        )
        for r in rows:
            r["steps"] = self._parse_json_list(r.get("steps_json", "[]"))
        return rows

    def delete_workflow(self, trigger_phrase: str) -> bool:
        """Delete a workflow by trigger phrase."""
        self._ensure_ready()
        cursor = self._store.execute(
            "DELETE FROM workflows WHERE trigger_phrase = ? COLLATE NOCASE",
            (trigger_phrase.strip().lower(),),
        )
        return cursor.rowcount > 0

    # ================================================================== #
    # Cross-category search                                              #
    # ================================================================== #

    def search_memory(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Search across all memory categories for matches.

        Returns a flat list of hits with a ``category`` field.
        """
        self._ensure_ready()
        query = query.strip()
        if not query:
            return []

        results: list[dict[str, Any]] = []
        like = f"%{query}%"

        # Preferences
        for row in self._store.query(
            "SELECT key, value FROM preferences WHERE key LIKE ? OR value LIKE ? LIMIT ?",
            (like, like, limit),
        ):
            results.append({**row, "category": "preference"})

        # People
        for row in self._store.query(
            "SELECT name, aliases, category FROM people WHERE name LIKE ? OR aliases LIKE ? LIMIT ?",
            (like, like, limit),
        ):
            row["aliases"] = self._parse_json_list(row.get("aliases", "[]"))
            results.append({**row, "category": "person"})

        # Contacts
        for row in self._store.query(
            "SELECT name, platform, aliases FROM contacts WHERE name LIKE ? OR aliases LIKE ? LIMIT ?",
            (like, like, limit),
        ):
            row["aliases"] = self._parse_json_list(row.get("aliases", "[]"))
            results.append({**row, "category": "contact"})

        # Workflows
        for row in self._store.query(
            "SELECT name, trigger_phrase FROM workflows WHERE trigger_phrase LIKE ? OR name LIKE ? LIMIT ?",
            (like, like, limit),
        ):
            results.append({**row, "category": "workflow"})

        return results[:limit]

    # ================================================================== #
    # Bulk operations                                                    #
    # ================================================================== #

    def delete_memory(self, *, category: str | None = None) -> int:
        """
        Delete memory data.  If ``category`` is given, clear that table.
        If None, clear all memory tables.
        """
        self._ensure_ready()
        tables = {
            "preferences": "preferences",
            "commands": "frequent_commands",
            "people": "people",
            "contacts": "contacts",
            "history": "interaction_history",
            "workflows": "workflows",
        }
        total = 0
        if category:
            table = tables.get(category.lower())
            if table:
                cursor = self._store.execute(f"DELETE FROM [{table}]")
                total = cursor.rowcount
        else:
            for table in tables.values():
                cursor = self._store.execute(f"DELETE FROM [{table}]")
                total += cursor.rowcount
        logger.info("Memory cleared: category=%s rows=%d", category or "all", total)
        return total

    def stats(self) -> dict[str, int]:
        """Return row counts per memory table."""
        self._ensure_ready()
        return {
            "preferences": self._store.row_count("preferences"),
            "frequent_commands": self._store.row_count("frequent_commands"),
            "people": self._store.row_count("people"),
            "contacts": self._store.row_count("contacts"),
            "interaction_history": self._store.row_count("interaction_history"),
            "workflows": self._store.row_count("workflows"),
        }

    def backup(self, target_path: str | Path | None = None) -> Path:
        """Create a hot backup of the memory database."""
        self._ensure_ready()
        return self._store.backup(target_path)

    def export_json(self) -> dict[str, Any]:
        """Export all memory data as a JSON-serialisable dict."""
        self._ensure_ready()
        return {
            "preferences": self.list_preferences(),
            "top_commands": self.top_commands(limit=100),
            "people": self.list_people(limit=500),
            "contacts": self.find_contact("", platform=None) if False else self._store.query("SELECT * FROM contacts"),
            "recent_history": self.recent_history(limit=200),
            "workflows": self.list_workflows(limit=100),
            "stats": self.stats(),
        }

    # ================================================================== #
    # Internal helpers                                                   #
    # ================================================================== #

    def _ensure_ready(self) -> None:
        if not self._ready:
            self.init()
            if not self._ready:
                raise RuntimeError("Memory system is not available.")

    def _enforce_retention(self) -> None:
        """Remove history entries older than the configured retention period."""
        days = int(settings.get("history_retention_days") or 90)
        if days <= 0:
            return
        self._store.execute(
            "DELETE FROM interaction_history WHERE created_at < datetime('now', ?)",
            (f"-{days} days",),
        )

    @staticmethod
    def _normalise_key(key: str) -> str:
        return re.sub(r"\s+", "_", key.strip().lower())

    @staticmethod
    def _normalise_command(text: str) -> str:
        return " ".join(text.strip().lower().split())

    @staticmethod
    def _utc_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _parse_json_list(raw: str | list) -> list:
        if isinstance(raw, list):
            return raw
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

memory_manager = MemoryManager()
