"""
Memory database schema definitions and migration logic.

Defines the canonical schema for the local memory database and provides
forward-only migrations so that the database structure can evolve across
releases without data loss.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Schema version — increment this when adding migrations
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# DDL statements executed for a brand-new database (version 1)
# ---------------------------------------------------------------------------
INITIAL_TABLES: list[str] = [
    # ── Schema version bookkeeping ────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version   INTEGER NOT NULL,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,

    # ── User preferences (key-value) ─────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS preferences (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,

    # ── Frequently used commands ──────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS frequent_commands (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        command_text        TEXT NOT NULL,
        normalized_command  TEXT NOT NULL,
        count               INTEGER NOT NULL DEFAULT 1,
        last_used_at        TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_freq_cmd_norm ON frequent_commands(normalized_command)",
    "CREATE INDEX IF NOT EXISTS idx_freq_cmd_count ON frequent_commands(count DESC)",

    # ── People / names ────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS people (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL,
        aliases      TEXT NOT NULL DEFAULT '[]',
        category     TEXT NOT NULL DEFAULT '',
        notes        TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        last_used_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_people_name ON people(name COLLATE NOCASE)",

    # ── Contacts (platform-linked) ────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS contacts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL,
        platform     TEXT NOT NULL DEFAULT '',
        external_id  TEXT NOT NULL DEFAULT '',
        aliases      TEXT NOT NULL DEFAULT '[]',
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        last_used_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts(name COLLATE NOCASE)",
    "CREATE INDEX IF NOT EXISTS idx_contacts_platform ON contacts(platform COLLATE NOCASE)",

    # ── Interaction history ───────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS interaction_history (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_input    TEXT NOT NULL DEFAULT '',
        intent        TEXT NOT NULL DEFAULT '',
        action_taken  TEXT NOT NULL DEFAULT '',
        success       INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_history_created ON interaction_history(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_history_intent ON interaction_history(intent)",

    # ── Reusable workflows ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS workflows (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL DEFAULT '',
        trigger_phrase  TEXT NOT NULL,
        steps_json      TEXT NOT NULL DEFAULT '[]',
        use_count       INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_workflows_trigger ON workflows(trigger_phrase COLLATE NOCASE)",

    # ── Preference signals (Phase 33 — behavioural observations) ──────────
    """
    CREATE TABLE IF NOT EXISTS preference_signals (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        category   TEXT NOT NULL,
        key        TEXT NOT NULL DEFAULT '',
        value      TEXT NOT NULL DEFAULT '',
        weight     REAL NOT NULL DEFAULT 1.0,
        source     TEXT NOT NULL DEFAULT 'auto',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_signals_cat_key ON preference_signals(category, key)",
    "CREATE INDEX IF NOT EXISTS idx_signals_created ON preference_signals(created_at DESC)",

    # ── Aggregated user preferences (Phase 33 — inferred + explicit) ──────
    """
    CREATE TABLE IF NOT EXISTS user_preferences (
        category       TEXT NOT NULL,
        key            TEXT NOT NULL DEFAULT '',
        value          TEXT NOT NULL DEFAULT '',
        confidence     REAL NOT NULL DEFAULT 0.0,
        evidence_count INTEGER NOT NULL DEFAULT 0,
        is_explicit    INTEGER NOT NULL DEFAULT 0,
        updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (category, key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_uprefs_cat ON user_preferences(category)",
]

# ---------------------------------------------------------------------------
# Forward-only migrations: list of (from_version, to_version, SQL list)
# ---------------------------------------------------------------------------
MIGRATIONS: list[tuple[int, int, list[str]]] = [
    # v1 → v2: add Phase 33 personalization tables
    (1, 2, [
        """
        CREATE TABLE IF NOT EXISTS preference_signals (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            category   TEXT NOT NULL,
            key        TEXT NOT NULL DEFAULT '',
            value      TEXT NOT NULL DEFAULT '',
            weight     REAL NOT NULL DEFAULT 1.0,
            source     TEXT NOT NULL DEFAULT 'auto',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_signals_cat_key ON preference_signals(category, key)",
        "CREATE INDEX IF NOT EXISTS idx_signals_created ON preference_signals(created_at DESC)",
        """
        CREATE TABLE IF NOT EXISTS user_preferences (
            category       TEXT NOT NULL,
            key            TEXT NOT NULL DEFAULT '',
            value          TEXT NOT NULL DEFAULT '',
            confidence     REAL NOT NULL DEFAULT 0.0,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            is_explicit    INTEGER NOT NULL DEFAULT 0,
            updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (category, key)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_uprefs_cat ON user_preferences(category)",
    ]),
]
