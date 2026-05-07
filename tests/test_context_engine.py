from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core import settings, state
from core.context_engine import ContextEngine, context_engine
from core.plan_models import ActionType, ExecutionPlan, PlanStep
from core.planner import ActionPlanner
from core.processor import CommandProcessor


@pytest.fixture(autouse=True)
def reset_runtime_state():
    settings.reset_defaults()
    context_engine._history.clear()
    state.current_context = "unknown"
    state.current_app = "unknown"
    state.current_window_title = ""
    state.current_process_name = ""
    state.selected_item_context = {}
    state.last_context_decision = {}
    state.recent_commands = []
    state.last_successful_action = ""
    state.context_resolution_count = 0
    state.last_entities = {}
    state.last_youtube_query = ""
    state.last_video_title = ""
    state.youtube_active = False
    state.last_media_action = ""
    yield
    settings.reset_defaults()


def _snapshot(app: str, title: str = "", selected_item=None):
    return {
        "app_id": app,
        "current_context": app,
        "window_title": title,
        "process_name": "chrome.exe" if app in {"youtube", "chrome"} else f"{app}.exe",
        "selected_item": selected_item or {},
    }


class TestContextEngineResolution:
    def test_youtube_play_song_rewrites_into_search_and_play(self):
        engine = ContextEngine()

        decision = engine.resolve(
            "Play Dulaander song",
            _snapshot("youtube", "YouTube - Google Chrome"),
            state,
        )

        assert decision.target_app == "youtube"
        assert decision.resolved_intent == "youtube_search"
        assert decision.confidence >= 0.85
        assert "search for dulaander song on youtube" in decision.rewritten_command.lower()
        assert decision.plan_hints["steps"][0]["action"] == "search"
        assert decision.plan_hints["steps"][1]["action"] == "app_action"

    def test_youtube_first_result_resolves_to_result_selection(self):
        engine = ContextEngine()
        state.last_successful_action = "search:youtube"

        decision = engine.resolve("first result", _snapshot("youtube", "YouTube - Google Chrome"), state)

        assert decision.target_app == "youtube"
        assert decision.resolved_intent == "youtube_select_result"
        assert decision.entities["result_index"] == 1

    def test_youtube_next_video_resolves_to_control(self):
        engine = ContextEngine()

        decision = engine.resolve("next video", _snapshot("youtube", "YouTube - Google Chrome"), state)

        assert decision.target_app == "youtube"
        assert decision.resolved_intent == "youtube_control"
        assert decision.entities["control"] == "next_video"

    def test_youtube_fullscreen_resolves_to_control(self):
        engine = ContextEngine()

        decision = engine.resolve("fullscreen", _snapshot("youtube", "YouTube - Google Chrome"), state)

        assert decision.target_app == "youtube"
        assert decision.resolved_intent == "youtube_control"
        assert decision.entities["control"] == "fullscreen"

    def test_youtube_read_results_resolves_to_control(self):
        engine = ContextEngine()

        decision = engine.resolve("read results", _snapshot("youtube", "YouTube - Google Chrome"), state)

        assert decision.target_app == "youtube"
        assert decision.resolved_intent == "youtube_control"
        assert decision.entities["control"] == "read_results"

    def test_whatsapp_call_contact_resolves(self):
        engine = ContextEngine()

        decision = engine.resolve("call hemanth", _snapshot("whatsapp", "WhatsApp"), state)

        assert decision.target_app == "whatsapp"
        assert decision.resolved_intent == "whatsapp_call"
        assert decision.entities["contact"] == "hemanth"
        assert decision.confidence >= 0.85

    def test_whatsapp_second_contact_resolves_to_chat_selection(self):
        engine = ContextEngine()

        decision = engine.resolve("second contact", _snapshot("whatsapp", "WhatsApp"), state)

        assert decision.target_app == "whatsapp"
        assert decision.resolved_intent == "whatsapp_open_chat"
        assert decision.entities["chat_index"] == 2

    def test_browser_search_routes_to_current_browser(self):
        engine = ContextEngine()

        decision = engine.resolve("search IPL score", _snapshot("chrome", "Google Chrome"), state)

        assert decision.target_app == "chrome"
        assert decision.resolved_intent == "browser_search"
        assert decision.entities["query"] == "ipl score"
        assert decision.plan_hints["steps"][0]["action"] == "search"

    def test_browser_open_first_result_routes_to_result_action(self):
        engine = ContextEngine()
        state.last_successful_action = "search:chrome"

        decision = engine.resolve("open first result", _snapshot("chrome", "Google Chrome"), state)

        assert decision.target_app == "chrome"
        assert decision.resolved_intent == "browser_open_result"
        assert decision.entities["result_index"] == 1
        assert decision.plan_hints["steps"][0]["action"] == "app_action"

    def test_browser_click_second_link_routes_to_result_action(self):
        engine = ContextEngine()
        state.last_successful_action = "search:chrome"

        decision = engine.resolve("click second link", _snapshot("chrome", "Google Chrome"), state)

        assert decision.target_app == "chrome"
        assert decision.resolved_intent == "browser_open_result"
        assert decision.entities["result_index"] == 2

    def test_explorer_delete_this_file_uses_selected_item_hook(self):
        engine = ContextEngine()

        decision = engine.resolve(
            "delete this file",
            _snapshot("explorer", "Documents", selected_item={"name": "notes.txt"}),
            state,
        )

        assert decision.target_app == "explorer"
        assert decision.resolved_intent == "explorer_delete"
        assert decision.requires_confirmation is True
        assert decision.entities["target"] == "notes.txt"
        assert decision.plan_hints["steps"][0]["action"] == "file_action"

    def test_unknown_context_falls_back_to_standard_pipeline(self):
        engine = ContextEngine()

        decision = engine.resolve("open calculator", _snapshot("unknown", ""), state)

        assert decision.resolved_intent == "passthrough"
        assert decision.context_used == "generic"

    def test_ambiguous_media_command_asks_for_clarification(self):
        engine = ContextEngine()

        decision = engine.resolve("play dulaander song", _snapshot("unknown", ""), state)

        assert decision.resolved_intent == "clarification"
        assert decision.requires_confirmation is True
        assert "youtube or spotify" in decision.clarification_prompt.lower()

    def test_recent_history_resolves_followup_when_context_is_missing(self):
        engine = ContextEngine()
        engine.record_command(
            "search lofi beats",
            "youtube_search",
            "youtube",
            True,
            rewritten_command="Search for lofi beats on YouTube",
            entities={"query": "lofi beats"},
        )
        state.last_successful_action = "search:youtube"

        decision = engine.resolve("first result", _snapshot("unknown", ""), state)

        assert decision.target_app == "youtube"
        assert decision.resolved_intent == "youtube_select_result"
        assert "last_action" in decision.matched_layers or "recent_history" in decision.matched_layers


class TestPlannerContextHints:
    def test_planner_builds_app_action_steps_from_context_hints(self):
        planner = ActionPlanner(llm_client=MagicMock())

        plan = planner.plan(
            "pause",
            context_hints={
                "steps": [
                    {
                        "action": "app_action",
                        "target": "youtube",
                        "params": {"operation": "control", "control": "pause"},
                    }
                ]
            },
        )

        assert plan.step_count == 1
        assert plan.steps[0].action == ActionType.APP_ACTION
        assert plan.steps[0].params["control"] == "pause"


class TestProcessorIntegration:
    def test_processor_uses_context_engine_rewrite_before_planning(self):
        settings.set("youtube_skill_enabled", False)
        processor = CommandProcessor(llm_client=MagicMock())
        processor.context.refresh = MagicMock()
        processor.context.get_context_snapshot = MagicMock(
            return_value=_snapshot("youtube", "YouTube - Google Chrome")
        )

        captured = {}

        def fake_generate_action_plan(text, context_decision=None):
            captured["text"] = text
            captured["decision"] = context_decision
            return ExecutionPlan(
                original_text=text,
                normalized_text=text,
                confidence=0.95,
                planner_used="context",
                steps=[
                    PlanStep(
                        id="step_1",
                        order=1,
                        action=ActionType.SEARCH,
                        target="youtube",
                        params={"query": "dulaander song", "engine": "youtube"},
                    )
                ],
            )

        processor._generate_action_plan = fake_generate_action_plan
        processor._execute_action_plan = lambda _plan: {
            "success": True,
            "intent": "multi_action",
            "response": "Context-aware YouTube plan executed.",
        }

        result = processor.process("Play Dulaander song")

        assert result["success"] is True
        assert captured["decision"].target_app == "youtube"
        assert "youtube" in captured["text"].lower()

    def test_processor_returns_clarification_for_low_confidence_context(self):
        processor = CommandProcessor(llm_client=MagicMock())
        processor.context.refresh = MagicMock()
        processor.context.get_context_snapshot = MagicMock(return_value=_snapshot("unknown", ""))

        result = processor.process("Play Dulaander song")

        assert result["success"] is False
        assert result["intent"] == "clarification"
        assert "youtube or spotify" in result["response"].lower()
