"""
Reference resolver for follow-up commands.

Resolves pronouns ("him", "it"), ordinals ("the second one"),
deictic references ("that", "same thing"), and carries over missing
slots from the previous turn.  Every resolution is scored with a
confidence value; low-confidence resolutions trigger clarification
instead of silent assumptions.

Architecture
------------
    1. Pronoun resolution   — him/her/it/them/that → typed entity
    2. Ordinal resolution   — first/second/third → choice index
    3. Deictic resolution   — "that app", "same file" → recent entity
    4. Slot carryover       — fill missing params from prior turn
    5. Confidence gating    — block unsafe low-confidence resolutions
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core import settings, state
from core.conversation_memory import (
    ConversationMemoryManager,
    EntityType,
    TrackedEntity,
    conversation_memory as _default_cm,
)
from core.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Confidence threshold below which we ask for clarification
CLARIFY_THRESHOLD = 0.55

# Pronoun → compatible entity types
_PRONOUN_MAP: dict[str, list[EntityType]] = {
    "him":   [EntityType.PERSON, EntityType.CONTACT],
    "her":   [EntityType.PERSON, EntityType.CONTACT],
    "them":  [EntityType.PERSON, EntityType.CONTACT],
    "it":    [EntityType.FILE, EntityType.APP, EntityType.URL, EntityType.DIRECTORY, EntityType.QUERY],
    "that":  [EntityType.FILE, EntityType.APP, EntityType.URL, EntityType.DIRECTORY, EntityType.QUERY, EntityType.PERSON],
    "this":  [EntityType.FILE, EntityType.APP, EntityType.URL, EntityType.DIRECTORY],
}

# Pronoun detection patterns (word-boundary, case-insensitive)
_PRONOUN_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\btell\s+him\b", re.I), "him"),
    (re.compile(r"\btell\s+her\b", re.I), "her"),
    (re.compile(r"\btell\s+them\b", re.I), "them"),
    (re.compile(r"\bmessage\s+him\b", re.I), "him"),
    (re.compile(r"\bmessage\s+her\b", re.I), "her"),
    (re.compile(r"\bcall\s+him\b", re.I), "him"),
    (re.compile(r"\bcall\s+her\b", re.I), "her"),
    (re.compile(r"\bcall\s+them\b", re.I), "them"),
    (re.compile(r"\bsend\s+(?:to\s+)?him\b", re.I), "him"),
    (re.compile(r"\bsend\s+(?:to\s+)?her\b", re.I), "her"),
    (re.compile(r"\bsend\s+(?:to\s+)?them\b", re.I), "them"),
    (re.compile(r"\bmove\s+it\b", re.I), "it"),
    (re.compile(r"\bdelete\s+it\b", re.I), "it"),
    (re.compile(r"\bopen\s+it\b", re.I), "it"),
    (re.compile(r"\bclose\s+it\b", re.I), "it"),
    (re.compile(r"\bcopy\s+it\b", re.I), "it"),
    (re.compile(r"\brename\s+it\b", re.I), "it"),
    (re.compile(r"\bshare\s+it\b", re.I), "it"),
    (re.compile(r"\bopen\s+that\b", re.I), "that"),
    (re.compile(r"\bclose\s+that\b", re.I), "that"),
    (re.compile(r"\bdelete\s+that\b", re.I), "that"),
]

# Ordinal words → index (1-based)
_ORDINAL_MAP: dict[str, int] = {
    "first": 1, "1st": 1,
    "second": 2, "2nd": 2,
    "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5,
    "last": -1,
}

_ORDINAL_PATTERN = re.compile(
    r"\b(?:the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|last)\s+one\b",
    re.I,
)

# Deictic patterns
_DEICTIC_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bthat\s+app\b", re.I), "app"),
    (re.compile(r"\bthat\s+file\b", re.I), "file"),
    (re.compile(r"\bthat\s+person\b", re.I), "person"),
    (re.compile(r"\bthat\s+contact\b", re.I), "contact"),
    (re.compile(r"\bsame\s+file\b", re.I), "file"),
    (re.compile(r"\bsame\s+app\b", re.I), "app"),
    (re.compile(r"\bsame\s+person\b", re.I), "person"),
]


# ---------------------------------------------------------------------------
# Result data class
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ResolutionResult:
    """Outcome of a reference resolution attempt."""
    resolved: bool = False
    value: str = ""
    entity_type: str = ""
    source: str = ""             # e.g. "pronoun:him", "ordinal:2"
    confidence: float = 0.0
    needs_clarification: bool = False
    message: str = ""
    enriched_text: str = ""      # input text with references substituted

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "value": self.value,
            "entity_type": self.entity_type,
            "source": self.source,
            "confidence": round(self.confidence, 3),
            "needs_clarification": self.needs_clarification,
            "message": self.message,
            "enriched_text": self.enriched_text,
        }


# ---------------------------------------------------------------------------
# ReferenceResolver
# ---------------------------------------------------------------------------

class ReferenceResolver:
    """
    Resolves follow-up references against conversational memory.

    Each ``resolve_*`` method returns a ``ResolutionResult``.
    ``resolve_all`` runs all resolvers in priority order and returns
    the aggregate result.
    """

    def __init__(
        self,
        *,
        memory: ConversationMemoryManager | None = None,
    ) -> None:
        self._memory = memory or _default_cm

    # ================================================================== #
    # Public API                                                         #
    # ================================================================== #

    def resolve_all(self, text: str) -> ResolutionResult:
        """
        Run all resolution strategies and return the best result.

        Priority: ordinals → pronouns → deictic → slot carryover.
        """
        if not text or not text.strip():
            return ResolutionResult(message="Empty input.")

        # 1. Ordinals first (most explicit)
        ordinal = self.resolve_ordinals(text)
        if ordinal.resolved or ordinal.needs_clarification:
            state.last_resolved_reference = ordinal.to_dict()
            return ordinal

        # 2. Pronouns
        pronoun = self.resolve_pronouns(text)
        if pronoun.resolved or pronoun.needs_clarification:
            state.last_resolved_reference = pronoun.to_dict()
            return pronoun

        # 3. Deictic references
        deictic = self.resolve_deictic(text)
        if deictic.resolved or deictic.needs_clarification:
            state.last_resolved_reference = deictic.to_dict()
            return deictic

        # 4. Slot carryover (implicit)
        slot = self.resolve_slots(text)
        if slot.resolved:
            state.last_resolved_reference = slot.to_dict()
            return slot

        return ResolutionResult(enriched_text=text, message="No references detected.")

    # ------------------------------------------------------------------ #
    # Pronoun resolution                                                 #
    # ------------------------------------------------------------------ #

    def resolve_pronouns(self, text: str) -> ResolutionResult:
        """
        Resolve pronouns (him/her/it/them/that) to recent entities.
        """
        pronoun = self._detect_pronoun(text)
        if not pronoun:
            return ResolutionResult()

        compatible_types = _PRONOUN_MAP.get(pronoun, [])
        if not compatible_types:
            return ResolutionResult()

        # Find candidates from recent entities matching type
        candidates = self._find_typed_candidates(compatible_types)

        if not candidates:
            return ResolutionResult(
                needs_clarification=True,
                source=f"pronoun:{pronoun}",
                message=f"I need a bit more detail — what does \"{pronoun}\" refer to?",
            )

        if len(candidates) == 1:
            entity = candidates[0]
            confidence = min(entity.confidence, 0.95)
            enriched = self._substitute_pronoun(text, pronoun, entity.name)
            logger.info("Resolved pronoun '%s' → %s conf=%.2f", pronoun, entity.name, confidence)
            return ResolutionResult(
                resolved=True,
                value=entity.name,
                entity_type=str(entity.type),
                source=f"pronoun:{pronoun}",
                confidence=confidence,
                enriched_text=enriched,
            )

        # Multiple candidates — check if one is clearly most recent
        if candidates[0].confidence >= 0.8 and (
            len(candidates) < 2 or candidates[0].confidence - candidates[1].confidence > 0.15
        ):
            entity = candidates[0]
            confidence = min(entity.confidence, 0.85)
            enriched = self._substitute_pronoun(text, pronoun, entity.name)
            logger.info("Resolved pronoun '%s' → %s (best of %d) conf=%.2f",
                        pronoun, entity.name, len(candidates), confidence)
            return ResolutionResult(
                resolved=True,
                value=entity.name,
                entity_type=str(entity.type),
                source=f"pronoun:{pronoun}",
                confidence=confidence,
                enriched_text=enriched,
            )

        # Ambiguous
        should_clarify = settings.get("clarify_low_confidence_references")
        names = [c.name for c in candidates[:4]]
        return ResolutionResult(
            needs_clarification=bool(should_clarify),
            resolved=not should_clarify,
            value=candidates[0].name if not should_clarify else "",
            entity_type=str(candidates[0].type),
            source=f"pronoun:{pronoun}",
            confidence=0.4,
            message=f"I'm not sure who \"{pronoun}\" refers to. Did you mean {self._format_or(names)}?",
        )

    # ------------------------------------------------------------------ #
    # Ordinal resolution                                                 #
    # ------------------------------------------------------------------ #

    def resolve_ordinals(self, text: str) -> ResolutionResult:
        """
        Resolve ordinal references ("the second one") against recent choices.
        """
        match = _ORDINAL_PATTERN.search(text)
        if not match:
            return ResolutionResult()

        word = match.group(1).lower()
        index = _ORDINAL_MAP.get(word, 0)
        if index == 0:
            return ResolutionResult()

        choices = self._memory.get_choices()
        if not choices:
            # Also check for recent entities as choices
            recent = self._memory.recent_entities()
            if recent:
                choices = [e.name for e in recent[:5]]

        if not choices:
            return ResolutionResult(
                needs_clarification=True,
                source=f"ordinal:{word}",
                message="There's nothing to select from. Could you be more specific?",
            )

        if index == -1:
            index = len(choices)

        if index < 1 or index > len(choices):
            return ResolutionResult(
                needs_clarification=True,
                source=f"ordinal:{word}",
                message=f"I only have {len(choices)} option(s) available.",
            )

        selected = choices[index - 1]
        enriched = _ORDINAL_PATTERN.sub(selected, text)
        logger.info("Resolved ordinal '%s' → %s (index %d)", word, selected, index)

        return ResolutionResult(
            resolved=True,
            value=selected,
            entity_type="choice",
            source=f"ordinal:{word}",
            confidence=0.95,
            enriched_text=enriched,
        )

    # ------------------------------------------------------------------ #
    # Deictic resolution                                                 #
    # ------------------------------------------------------------------ #

    def resolve_deictic(self, text: str) -> ResolutionResult:
        """
        Resolve deictic references ("that app", "same file") to recent entities.
        """
        for pattern, etype_str in _DEICTIC_PATTERNS:
            m = pattern.search(text)
            if not m:
                continue

            try:
                etype = EntityType(etype_str)
            except ValueError:
                continue

            entity = self._memory.last_entity_of_type(etype)
            if not entity:
                return ResolutionResult(
                    needs_clarification=True,
                    source=f"deictic:{etype_str}",
                    message=f"Which {etype_str} are you referring to?",
                )

            enriched = pattern.sub(entity.name, text)
            logger.info("Resolved deictic '%s' → %s", etype_str, entity.name)
            return ResolutionResult(
                resolved=True,
                value=entity.name,
                entity_type=etype_str,
                source=f"deictic:{etype_str}",
                confidence=min(entity.confidence, 0.90),
                enriched_text=enriched,
            )

        return ResolutionResult()

    # ------------------------------------------------------------------ #
    # Slot carryover                                                     #
    # ------------------------------------------------------------------ #

    def resolve_slots(self, text: str) -> ResolutionResult:
        """
        Carry over missing entity slots from the previous turn.

        Example: after "Search IPL score", the user says "open first result"
        — the search query slot carries over.
        """
        last_turn = self._memory.last_turn()
        if not last_turn or not last_turn.entities:
            return ResolutionResult()

        # Check if the current input is very short (likely a follow-up)
        words = text.strip().split()
        if len(words) > 8:
            return ResolutionResult()

        # Find the most relevant entity from the last turn
        for entity in last_turn.entities:
            # Only carry over non-choice entities
            if entity.type == EntityType.CHOICE:
                continue
            return ResolutionResult(
                resolved=True,
                value=entity.name,
                entity_type=str(entity.type),
                source="slot_carryover",
                confidence=0.65,
                enriched_text=text,
                message=f"Using {entity.type.value}: {entity.name} from previous command.",
            )

        return ResolutionResult()

    # ================================================================== #
    # Utility                                                            #
    # ================================================================== #

    def has_references(self, text: str) -> bool:
        """Quick check whether text contains any resolvable references."""
        if self._detect_pronoun(text):
            return True
        if _ORDINAL_PATTERN.search(text):
            return True
        for pattern, _ in _DEICTIC_PATTERNS:
            if pattern.search(text):
                return True
        return False

    # ================================================================== #
    # Internal                                                           #
    # ================================================================== #

    def _detect_pronoun(self, text: str) -> str:
        """Detect the first pronoun reference in text."""
        for pattern, pronoun in _PRONOUN_PATTERNS:
            if pattern.search(text):
                return pronoun
        return ""

    def _find_typed_candidates(self, types: list[EntityType]) -> list[TrackedEntity]:
        """Find recent entities matching any of the given types, most recent first."""
        results: list[TrackedEntity] = []
        seen_names: set[str] = set()
        for entity in self._memory.recent_entities():
            if entity.type in types and entity.name.lower() not in seen_names:
                results.append(entity)
                seen_names.add(entity.name.lower())
        return results

    def _substitute_pronoun(self, text: str, pronoun: str, replacement: str) -> str:
        """Replace the pronoun occurrence in text with the resolved value."""
        return re.sub(rf"\b{re.escape(pronoun)}\b", replacement, text, count=1, flags=re.I)

    @staticmethod
    def _format_or(names: list[str]) -> str:
        if len(names) <= 1:
            return names[0] if names else "?"
        return ", ".join(names[:-1]) + f" or {names[-1]}"


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

reference_resolver = ReferenceResolver()
