"""
Short-term conversational memory for the Nova Assistant.

Maintains a sliding window of recent conversation turns and the typed
entities mentioned in each turn.  This in-memory store powers the
reference resolver (``core.resolver``) so that follow-up commands like
"tell *him* I'm late" can be connected to a previously mentioned person.

Architecture
------------
    1. Turn memory        — fixed-size deque of ``ConversationTurn``
    2. Entity tracking    — typed ``TrackedEntity`` objects linked to turns
    3. Expiration policy  — time-based + turn-count eviction
    4. Session lifecycle  — ``reset_session`` / ``clear_expired``
"""
from __future__ import annotations

import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Sequence

from core import settings, state
from core.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Entity types
# ---------------------------------------------------------------------------

class EntityType(str, Enum):
    PERSON = "person"
    FILE = "file"
    APP = "app"
    CONTACT = "contact"
    URL = "url"
    QUERY = "query"
    CHOICE = "choice"       # ordinal selection from a list
    DIRECTORY = "directory"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class TrackedEntity:
    """A typed entity mentioned in a conversation turn."""
    entity_id: str = ""
    type: EntityType = EntityType.UNKNOWN
    name: str = ""
    value: str = ""
    source_turn_id: str = ""
    confidence: float = 1.0
    aliases: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.entity_id:
            self.entity_id = uuid.uuid4().hex[:12]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "type": str(self.type),
            "name": self.name,
            "value": self.value,
            "source_turn_id": self.source_turn_id,
            "confidence": round(self.confidence, 3),
            "aliases": self.aliases,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TrackedEntity:
        return cls(
            entity_id=str(d.get("entity_id", "")),
            type=EntityType(d.get("type", "unknown")),
            name=str(d.get("name", "")),
            value=str(d.get("value", "")),
            source_turn_id=str(d.get("source_turn_id", "")),
            confidence=float(d.get("confidence", 1.0)),
            aliases=list(d.get("aliases") or []),
            metadata=dict(d.get("metadata") or {}),
        )


@dataclass(slots=True)
class ConversationTurn:
    """A single turn in the conversation."""
    id: str = ""
    user_input: str = ""
    intent: str = ""
    entities: list[TrackedEntity] = field(default_factory=list)
    action_result: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0
    choices: list[str] = field(default_factory=list)  # disambiguation options

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_input": self.user_input,
            "intent": self.intent,
            "entities": [e.to_dict() for e in self.entities],
            "action_result": self.action_result,
            "timestamp": self.timestamp,
            "choices": self.choices,
        }

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.timestamp)


# ---------------------------------------------------------------------------
# ConversationMemoryManager
# ---------------------------------------------------------------------------

class ConversationMemoryManager:
    """
    In-memory short-term conversation context.

    Stores the last N turns (configurable via ``conversation_max_turns``)
    and all entities extracted from those turns.  Entities are expired
    along with their source turn.
    """

    def __init__(self, *, max_turns: int | None = None) -> None:
        self._max_turns = max_turns or 10
        self._turns: deque[ConversationTurn] = deque(maxlen=self._max_turns)
        self._entities: list[TrackedEntity] = []
        self._ready = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def init(self) -> None:
        if self._ready:
            return
        if not settings.get("conversation_memory_enabled"):
            logger.info("Conversation memory disabled by settings")
            return
        max_cfg = settings.get("conversation_max_turns")
        if max_cfg and int(max_cfg) != self._max_turns:
            self._max_turns = int(max_cfg)
            old = list(self._turns)
            self._turns = deque(old, maxlen=self._max_turns)
        self._ready = True
        state.conversation_ready = True
        logger.info("ConversationMemoryManager initialised (max_turns=%d)", self._max_turns)

    @property
    def ready(self) -> bool:
        return self._ready

    # ================================================================== #
    # Turn management                                                    #
    # ================================================================== #

    def add_turn(
        self,
        user_input: str,
        intent: str = "",
        entities: list[TrackedEntity] | None = None,
        action_result: dict[str, Any] | None = None,
        choices: list[str] | None = None,
    ) -> ConversationTurn:
        """
        Record a new conversation turn.

        Returns the created turn object.
        """
        self._ensure_ready()
        self.clear_expired()

        turn = ConversationTurn(
            user_input=user_input,
            intent=intent,
            entities=entities or [],
            action_result=action_result or {},
            choices=choices or [],
        )
        self._turns.append(turn)

        # Register entities
        for entity in turn.entities:
            entity.source_turn_id = turn.id
            self._entities.append(entity)

        state.session_turn_count = len(self._turns)
        logger.info("Turn stored id=%s intent=%s entities=%d", turn.id, intent, len(turn.entities))
        return turn

    def remember_entity(
        self,
        entity_type: EntityType | str,
        name: str,
        value: str = "",
        *,
        confidence: float = 1.0,
        aliases: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TrackedEntity:
        """
        Register an entity outside a formal turn (e.g. from result parsing).
        """
        self._ensure_ready()
        if isinstance(entity_type, str):
            try:
                entity_type = EntityType(entity_type)
            except ValueError:
                entity_type = EntityType.UNKNOWN

        last_turn_id = self._turns[-1].id if self._turns else ""

        entity = TrackedEntity(
            type=entity_type,
            name=name,
            value=value or name,
            source_turn_id=last_turn_id,
            confidence=confidence,
            aliases=aliases or [],
            metadata=metadata or {},
        )
        self._entities.append(entity)

        # Also attach to last turn if one exists
        if self._turns:
            self._turns[-1].entities.append(entity)

        state.recent_entities = [e.to_dict() for e in self._entities[-5:]]
        logger.info("Entity stored type=%s name=%s", entity.type, name)
        return entity

    # ================================================================== #
    # Retrieval                                                          #
    # ================================================================== #

    def recent_turns(self, limit: int = 10) -> list[ConversationTurn]:
        """Return the most recent turns, newest first."""
        self._ensure_ready()
        return list(reversed(list(self._turns)))[:limit]

    def recent_entities(self, etype: EntityType | str | None = None) -> list[TrackedEntity]:
        """
        Return tracked entities, newest first.

        Optionally filter by entity type.
        """
        self._ensure_ready()
        if etype:
            if isinstance(etype, str):
                try:
                    etype = EntityType(etype)
                except ValueError:
                    return []
            return [e for e in reversed(self._entities) if e.type == etype]
        return list(reversed(self._entities))

    def last_entity_of_type(self, etype: EntityType | str) -> TrackedEntity | None:
        """Return the most recently tracked entity of a given type."""
        matches = self.recent_entities(etype)
        return matches[0] if matches else None

    def last_turn(self) -> ConversationTurn | None:
        """Return the most recent turn, or None."""
        return self._turns[-1] if self._turns else None

    def get_choices(self) -> list[str]:
        """Return the most recent disambiguation choices, if any."""
        lt = self.last_turn()
        return lt.choices if lt else []

    @property
    def turn_count(self) -> int:
        return len(self._turns)

    @property
    def entity_count(self) -> int:
        return len(self._entities)

    # ================================================================== #
    # Expiration                                                         #
    # ================================================================== #

    def clear_expired(self) -> int:
        """Remove turns and entities older than the expiry window."""
        expiry_minutes = int(settings.get("conversation_expiry_minutes") or 30)
        cutoff = time.time() - (expiry_minutes * 60)

        # Identify expired turn IDs
        expired_ids: set[str] = set()
        remaining_turns: list[ConversationTurn] = []
        for turn in self._turns:
            if turn.timestamp < cutoff:
                expired_ids.add(turn.id)
            else:
                remaining_turns.append(turn)

        if not expired_ids:
            return 0

        # Remove expired entities
        self._entities = [e for e in self._entities if e.source_turn_id not in expired_ids]

        # Rebuild deque
        self._turns = deque(remaining_turns, maxlen=self._max_turns)

        state.session_turn_count = len(self._turns)
        logger.info("Expired %d turns, %d entities remain", len(expired_ids), len(self._entities))
        return len(expired_ids)

    def reset_session(self) -> None:
        """Clear all conversation state — fresh start."""
        self._turns.clear()
        self._entities.clear()
        state.session_turn_count = 0
        state.recent_entities = []
        state.last_resolved_reference = {}
        logger.info("Conversation session reset")

    # ================================================================== #
    # Internal                                                           #
    # ================================================================== #

    def _ensure_ready(self) -> None:
        if not self._ready:
            self.init()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

conversation_memory = ConversationMemoryManager()
