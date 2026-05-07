"""
Tests for PHASE 34 — Follow-Up Conversation Memory.

Covers:
  - ConversationMemoryManager (turns, entities, expiry, reset)
  - ReferenceResolver pronouns (him→person, it→file/app)
  - Ordinal resolution (first/second/last one)
  - Deictic resolution ("that app", "same file")
  - Slot carryover from previous turn
  - Ambiguity detection and clarification
  - Confidence gating
  - Empty session handling
  - Multi-turn stability
  - Data class serialisation
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from core.conversation_memory import (
    ConversationMemoryManager,
    ConversationTurn,
    EntityType,
    TrackedEntity,
)
from core.resolver import (
    ReferenceResolver,
    ResolutionResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings():
    return {
        "conversation_memory_enabled": True,
        "conversation_max_turns": 10,
        "conversation_expiry_minutes": 30,
        "clarify_low_confidence_references": True,
    }


def _make_cm(max_turns: int = 10) -> ConversationMemoryManager:
    cm = ConversationMemoryManager(max_turns=max_turns)
    with patch("core.conversation_memory.settings") as ms, \
         patch("core.conversation_memory.state"):
        ms.get = lambda key, default=None: _settings().get(key, default)
        cm.init()
    return cm


def _make_resolver(cm: ConversationMemoryManager | None = None) -> ReferenceResolver:
    return ReferenceResolver(memory=cm or _make_cm())


class _patched:
    def __enter__(self):
        self._patches = [
            patch("core.conversation_memory.settings"),
            patch("core.conversation_memory.state"),
            patch("core.resolver.settings"),
            patch("core.resolver.state"),
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
# ConversationMemoryManager Tests
# ===========================================================================

class TestTurnManagement(unittest.TestCase):
    def test_add_turn(self):
        cm = _make_cm()
        with _patched():
            turn = cm.add_turn("open chrome", intent="open_app")
        self.assertEqual(turn.user_input, "open chrome")
        self.assertEqual(turn.intent, "open_app")
        self.assertEqual(cm.turn_count, 1)

    def test_recent_turns_order(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("first")
            cm.add_turn("second")
            cm.add_turn("third")
            recent = cm.recent_turns()
        self.assertEqual(recent[0].user_input, "third")
        self.assertEqual(recent[2].user_input, "first")

    def test_max_turns_eviction(self):
        cm = ConversationMemoryManager(max_turns=3)
        with _patched():
            cm._ready = True
            for i in range(5):
                cm.add_turn(f"turn_{i}")
        self.assertEqual(cm.turn_count, 3)

    def test_last_turn(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("first")
            cm.add_turn("last")
            lt = cm.last_turn()
        self.assertEqual(lt.user_input, "last")

    def test_empty_last_turn(self):
        cm = _make_cm()
        self.assertIsNone(cm.last_turn())


class TestEntityTracking(unittest.TestCase):
    def test_remember_entity(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("message hemanth")
            entity = cm.remember_entity(EntityType.PERSON, "Hemanth")
        self.assertEqual(entity.name, "Hemanth")
        self.assertEqual(entity.type, EntityType.PERSON)
        self.assertEqual(cm.entity_count, 1)

    def test_entity_in_turn(self):
        cm = _make_cm()
        with _patched():
            entity = TrackedEntity(type=EntityType.FILE, name="report.pdf", value="report.pdf")
            cm.add_turn("open report.pdf", entities=[entity])
            last = cm.last_turn()
        self.assertEqual(len(last.entities), 1)
        self.assertEqual(last.entities[0].name, "report.pdf")

    def test_recent_entities_by_type(self):
        cm = _make_cm()
        with _patched():
            cm.remember_entity(EntityType.PERSON, "Hemanth")
            cm.remember_entity(EntityType.FILE, "report.pdf")
            cm.remember_entity(EntityType.PERSON, "Rakesh")
            people = cm.recent_entities(EntityType.PERSON)
        self.assertEqual(len(people), 2)
        self.assertEqual(people[0].name, "Rakesh")

    def test_last_entity_of_type(self):
        cm = _make_cm()
        with _patched():
            cm.remember_entity(EntityType.APP, "chrome")
            cm.remember_entity(EntityType.APP, "notepad")
            last_app = cm.last_entity_of_type(EntityType.APP)
        self.assertEqual(last_app.name, "notepad")


class TestExpiration(unittest.TestCase):
    def test_expired_turns_removed(self):
        cm = _make_cm()
        with _patched():
            # Add a turn with old timestamp
            turn = cm.add_turn("old command")
            turn.timestamp = time.time() - 3600  # 1 hour ago

            # add_turn calls clear_expired internally, so the old turn
            # is evicted before the new one is added
            cm.add_turn("new command")
        # Only the new turn should remain
        self.assertEqual(cm.turn_count, 1)

    def test_entities_removed_with_turns(self):
        cm = _make_cm()
        with _patched():
            entity = TrackedEntity(type=EntityType.PERSON, name="OldPerson")
            turn = cm.add_turn("old", entities=[entity])
            turn.timestamp = time.time() - 3600
            entity.source_turn_id = turn.id

            cm.clear_expired()
        # Entity should be gone
        self.assertEqual(cm.entity_count, 0)

    def test_reset_session(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("a")
            cm.add_turn("b")
            cm.remember_entity(EntityType.PERSON, "Test")
            cm.reset_session()
        self.assertEqual(cm.turn_count, 0)
        self.assertEqual(cm.entity_count, 0)


class TestChoices(unittest.TestCase):
    def test_store_and_retrieve_choices(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("search hemanth", choices=["Hemanth A", "Hemanth B", "Hemanth C"])
            choices = cm.get_choices()
        self.assertEqual(len(choices), 3)
        self.assertEqual(choices[1], "Hemanth B")


# ===========================================================================
# Pronoun Resolution Tests
# ===========================================================================

class TestPronounResolution(unittest.TestCase):
    def test_him_resolves_to_person(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("message Hemanth", entities=[
                TrackedEntity(type=EntityType.PERSON, name="Hemanth"),
            ])
            resolver = _make_resolver(cm)
            result = resolver.resolve_pronouns("tell him I'm late")
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, "Hemanth")
        self.assertIn("pronoun:him", result.source)
        self.assertGreater(result.confidence, 0.5)

    def test_it_resolves_to_file(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("open report.pdf", entities=[
                TrackedEntity(type=EntityType.FILE, name="report.pdf"),
            ])
            resolver = _make_resolver(cm)
            result = resolver.resolve_pronouns("move it to Documents")
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, "report.pdf")

    def test_it_resolves_to_app(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("open chrome", entities=[
                TrackedEntity(type=EntityType.APP, name="Chrome"),
            ])
            resolver = _make_resolver(cm)
            result = resolver.resolve_pronouns("close it")
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, "Chrome")

    def test_no_context_asks_clarification(self):
        cm = _make_cm()
        resolver = _make_resolver(cm)
        with _patched():
            result = resolver.resolve_pronouns("tell him hello")
        self.assertFalse(result.resolved)
        self.assertTrue(result.needs_clarification)

    def test_multiple_people_asks_clarification(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("message Hemanth", entities=[
                TrackedEntity(type=EntityType.PERSON, name="Hemanth", confidence=0.7),
            ])
            cm.add_turn("call Rakesh", entities=[
                TrackedEntity(type=EntityType.PERSON, name="Rakesh", confidence=0.7),
            ])
            resolver = _make_resolver(cm)
            result = resolver.resolve_pronouns("tell him hello")
        self.assertTrue(result.needs_clarification)
        self.assertIn("Hemanth", result.message)
        self.assertIn("Rakesh", result.message)


class TestPronounNotDetected(unittest.TestCase):
    def test_normal_text_no_pronoun(self):
        cm = _make_cm()
        resolver = _make_resolver(cm)
        with _patched():
            result = resolver.resolve_pronouns("open chrome")
        self.assertFalse(result.resolved)
        self.assertFalse(result.needs_clarification)


# ===========================================================================
# Ordinal Resolution Tests
# ===========================================================================

class TestOrdinalResolution(unittest.TestCase):
    def test_second_one_selects_candidate_2(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("search hemanth", choices=["Hemanth A", "Hemanth B", "Hemanth C"])
            resolver = _make_resolver(cm)
            result = resolver.resolve_ordinals("call the second one")
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, "Hemanth B")
        self.assertGreater(result.confidence, 0.9)

    def test_first_one(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("results", choices=["Option A", "Option B"])
            resolver = _make_resolver(cm)
            result = resolver.resolve_ordinals("open the first one")
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, "Option A")

    def test_last_one(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("results", choices=["A", "B", "C"])
            resolver = _make_resolver(cm)
            result = resolver.resolve_ordinals("pick the last one")
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, "C")

    def test_ordinal_out_of_range(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("results", choices=["A"])
            resolver = _make_resolver(cm)
            result = resolver.resolve_ordinals("open the third one")
        self.assertFalse(result.resolved)
        self.assertTrue(result.needs_clarification)
        self.assertIn("1", result.message)

    def test_no_choices_available(self):
        cm = _make_cm()
        resolver = _make_resolver(cm)
        with _patched():
            result = resolver.resolve_ordinals("open the second one")
        # Should either clarify or fall back to entities
        self.assertFalse(result.resolved) or self.assertTrue(result.needs_clarification)


# ===========================================================================
# Deictic Resolution Tests
# ===========================================================================

class TestDeicticResolution(unittest.TestCase):
    def test_that_app(self):
        cm = _make_cm()
        with _patched():
            cm.remember_entity(EntityType.APP, "Chrome")
            resolver = _make_resolver(cm)
            result = resolver.resolve_deictic("close that app")
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, "Chrome")

    def test_same_file(self):
        cm = _make_cm()
        with _patched():
            cm.remember_entity(EntityType.FILE, "report.pdf")
            resolver = _make_resolver(cm)
            result = resolver.resolve_deictic("copy same file")
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, "report.pdf")

    def test_no_entity_for_deictic(self):
        cm = _make_cm()
        resolver = _make_resolver(cm)
        with _patched():
            result = resolver.resolve_deictic("open that app")
        self.assertFalse(result.resolved)
        self.assertTrue(result.needs_clarification)


# ===========================================================================
# Slot Carryover Tests
# ===========================================================================

class TestSlotCarryover(unittest.TestCase):
    def test_carryover_from_previous_turn(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("search IPL score", entities=[
                TrackedEntity(type=EntityType.QUERY, name="IPL score"),
            ])
            resolver = _make_resolver(cm)
            result = resolver.resolve_slots("open first result")
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, "IPL score")
        self.assertEqual(result.source, "slot_carryover")

    def test_no_carryover_empty_session(self):
        cm = _make_cm()
        resolver = _make_resolver(cm)
        with _patched():
            result = resolver.resolve_slots("open first result")
        self.assertFalse(result.resolved)


# ===========================================================================
# resolve_all Integration Tests
# ===========================================================================

class TestResolveAll(unittest.TestCase):
    def test_pronoun_resolved_in_all(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("message Hemanth", entities=[
                TrackedEntity(type=EntityType.PERSON, name="Hemanth"),
            ])
            resolver = _make_resolver(cm)
            result = resolver.resolve_all("tell him I'm late")
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, "Hemanth")

    def test_ordinal_before_pronoun(self):
        cm = _make_cm()
        with _patched():
            cm.add_turn("contacts", choices=["A", "B"], entities=[
                TrackedEntity(type=EntityType.PERSON, name="A"),
            ])
            resolver = _make_resolver(cm)
            result = resolver.resolve_all("call the second one")
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, "B")

    def test_no_references_passthrough(self):
        cm = _make_cm()
        resolver = _make_resolver(cm)
        with _patched():
            result = resolver.resolve_all("open chrome")
        self.assertFalse(result.resolved)
        self.assertFalse(result.needs_clarification)
        self.assertEqual(result.enriched_text, "open chrome")

    def test_empty_input(self):
        resolver = _make_resolver()
        result = resolver.resolve_all("")
        self.assertFalse(result.resolved)


# ===========================================================================
# has_references Tests
# ===========================================================================

class TestHasReferences(unittest.TestCase):
    def test_detects_pronoun(self):
        resolver = _make_resolver()
        self.assertTrue(resolver.has_references("tell him hello"))

    def test_detects_ordinal(self):
        resolver = _make_resolver()
        self.assertTrue(resolver.has_references("open the second one"))

    def test_detects_deictic(self):
        resolver = _make_resolver()
        self.assertTrue(resolver.has_references("close that app"))

    def test_normal_text(self):
        resolver = _make_resolver()
        self.assertFalse(resolver.has_references("open chrome"))


# ===========================================================================
# Repeated Multi-Turn Stability
# ===========================================================================

class TestMultiTurnStability(unittest.TestCase):
    def test_multi_turn_conversation(self):
        cm = _make_cm()
        resolver = _make_resolver(cm)
        with _patched():
            # Turn 1
            cm.add_turn("message Hemanth", intent="message", entities=[
                TrackedEntity(type=EntityType.PERSON, name="Hemanth"),
            ])
            # Turn 2 — follow-up
            r1 = resolver.resolve_all("tell him I'm late")
            self.assertTrue(r1.resolved)
            self.assertEqual(r1.value, "Hemanth")

            cm.add_turn("tell him I'm late", intent="message")

            # Turn 3 — new entity
            cm.add_turn("open report.pdf", entities=[
                TrackedEntity(type=EntityType.FILE, name="report.pdf"),
            ])
            # Turn 4 — follow-up
            r2 = resolver.resolve_all("move it to Documents")
            self.assertTrue(r2.resolved)
            self.assertEqual(r2.value, "report.pdf")

            # Ensure no cross-contamination
            self.assertNotEqual(r1.value, r2.value)


# ===========================================================================
# Serialisation Tests
# ===========================================================================

class TestSerialisation(unittest.TestCase):
    def test_entity_roundtrip(self):
        e = TrackedEntity(type=EntityType.PERSON, name="Hemanth", value="Hemanth", confidence=0.9)
        d = e.to_dict()
        restored = TrackedEntity.from_dict(d)
        self.assertEqual(restored.name, "Hemanth")
        self.assertEqual(restored.type, EntityType.PERSON)

    def test_turn_to_dict(self):
        t = ConversationTurn(
            user_input="test",
            intent="open_app",
            entities=[TrackedEntity(type=EntityType.APP, name="Chrome")],
        )
        d = t.to_dict()
        self.assertEqual(d["user_input"], "test")
        self.assertEqual(len(d["entities"]), 1)

    def test_resolution_to_dict(self):
        r = ResolutionResult(resolved=True, value="Chrome", confidence=0.9)
        d = r.to_dict()
        self.assertTrue(d["resolved"])
        self.assertEqual(d["value"], "Chrome")


if __name__ == "__main__":
    unittest.main()
