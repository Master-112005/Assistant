"""
Comprehensive tests for the action planner.

Tests cover:
- Single-step commands
- Multi-step commands with connectors
- Dependency resolution
- Edge cases and robustness
- Validation
"""
import pytest

from core.planner import ActionPlanner
from core.plan_models import ActionType, ExecutionPlan, PlannerContext


class TestActionPlannerBasics:
    """Basic planner functionality tests."""

    @pytest.fixture
    def planner(self):
        """Create a planner instance for tests."""
        return ActionPlanner()

    def test_empty_input(self, planner):
        """Test handling of empty input."""
        plan = planner.plan("")
        assert plan.step_count == 0
        assert plan.confidence == 0.0

    def test_whitespace_only_input(self, planner):
        """Test handling of whitespace-only input."""
        plan = planner.plan("   \t\n  ")
        assert plan.step_count == 0

    def test_single_app_open(self, planner):
        """Test opening a single app."""
        plan = planner.plan("open chrome")
        assert plan.step_count == 1
        assert plan.steps[0].action == ActionType.OPEN_APP
        assert plan.steps[0].target.lower() == "chrome"

    def test_open_app_variations(self, planner):
        """Test various app open phrasings."""
        test_cases = [
            "open chrome",
            "launch firefox",
            "start notepad",
            "run explorer",
        ]
        for text in test_cases:
            plan = planner.plan(text)
            assert plan.step_count == 1
            assert plan.steps[0].action == ActionType.OPEN_APP

    def test_search_query(self, planner):
        """Test search command."""
        plan = planner.plan("search IPL score")
        assert plan.step_count == 1
        assert plan.steps[0].action == ActionType.SEARCH
        assert "ipl score" in plan.steps[0].params.get("query", "").lower()

    def test_search_variations(self, planner):
        """Test various search phrasings."""
        test_cases = [
            "search weather",
            "look for cricket news",
            "find information about python",
            "google best restaurants",
        ]
        for text in test_cases:
            plan = planner.plan(text)
            assert plan.step_count == 1
            assert plan.steps[0].action == ActionType.SEARCH


class TestMultiStepPlanning:
    """Tests for multi-step command planning."""

    @pytest.fixture
    def planner(self):
        return ActionPlanner()

    def test_open_and_search_with_and(self, planner):
        """Test 'open chrome and search IPL score'."""
        plan = planner.plan("open chrome and search IPL score")
        
        assert plan.step_count == 2
        assert plan.steps[0].action == ActionType.OPEN_APP
        assert plan.steps[0].target.lower() == "chrome"
        
        assert plan.steps[1].action == ActionType.SEARCH
        assert "ipl score" in plan.steps[1].params.get("query", "").lower()
        
        assert plan.steps[0].order == 1
        assert plan.steps[1].order == 2

    def test_multiple_connectors(self, planner):
        """Test multiple connector variations."""
        plan1 = planner.plan("open chrome and search weather")
        assert plan1.step_count == 2

        plan2 = planner.plan("open chrome, search weather")
        assert plan2.step_count == 2

        plan3 = planner.plan("open chrome then search weather")
        assert plan3.step_count == 2

    def test_implicit_open_then_message_without_connector(self, planner):
        """Implicit chained commands should split into executable steps."""
        plan = planner.plan("open whatsapp say hi to hemanth")

        assert plan.step_count == 2
        assert plan.steps[0].action == ActionType.OPEN_APP
        assert plan.steps[0].target.lower() == "whatsapp"
        assert plan.steps[1].action == ActionType.SEND_MESSAGE
        assert plan.steps[1].target.lower() == "hemanth"
        assert plan.steps[1].params["message"].lower() == "hi"

    def test_implicit_open_then_message_binds_to_whatsapp_dependency(self, planner):
        plan = planner.plan("open whatsapp say hello to hemanth")

        assert plan.step_count == 2
        assert plan.steps[1].depends_on == [plan.steps[0].id]
        assert plan.steps[1].params["app"] == "whatsapp"
        assert plan.steps[1].params["target_app"] == "whatsapp"

    def test_three_step_sequence(self, planner):
        """Test three-step command sequence."""
        plan = planner.plan("open youtube, search dulaunder song, then play first result")
        
        assert plan.step_count == 3
        assert plan.steps[0].action == ActionType.OPEN_APP
        assert plan.steps[0].target.lower() == "youtube"
        
        assert plan.steps[1].action == ActionType.SEARCH
        
        assert plan.steps[2].action == ActionType.PLAY

    def test_command_ordering(self, planner):
        """Verify steps are in correct order."""
        plan = planner.plan("open spotify and play first song then open discord")
        
        assert plan.step_count == 3
        assert plan.steps[0].action == ActionType.OPEN_APP
        assert plan.steps[1].action == ActionType.PLAY
        assert plan.steps[2].action == ActionType.OPEN_APP


class TestDependencies:
    """Tests for dependency resolution."""

    @pytest.fixture
    def planner(self):
        return ActionPlanner()

    def test_search_depends_on_app_open(self, planner):
        """Search should depend on having the target app open."""
        plan = planner.plan("open chrome and search weather")
        
        search_step = plan.steps[1]
        open_step = plan.steps[0]
        
        assert len(search_step.depends_on) > 0

    def test_play_depends_on_search(self, planner):
        """Play action should depend on search results."""
        plan = planner.plan("search music and play first song")
        
        if plan.step_count >= 2:
            play_step = None
            search_step = None
            
            for step in plan.steps:
                if step.action == ActionType.SEARCH:
                    search_step = step
                elif step.action == ActionType.PLAY:
                    play_step = step
            
            if play_step and search_step:
                assert search_step.order < play_step.order

    def test_no_circular_dependencies(self, planner):
        """Plans should never have circular dependencies."""
        plan = planner.plan("open chrome and search weather and play result")
        
        for step in plan.steps:
            visited = set()
            
            def has_cycle(step_id, visited_set):
                if step_id in visited_set:
                    return True
                visited_set.add(step_id)
                for step_obj in [s for s in plan.steps if s.id == step_id]:
                    for dep in step_obj.depends_on:
                        if has_cycle(dep, visited_set):
                            return True
                return False
            
            assert not has_cycle(step.id, visited)


class TestRobustness:
    """Tests for handling edge cases and various inputs."""

    @pytest.fixture
    def planner(self):
        return ActionPlanner()

    def test_extra_whitespace(self, planner):
        """Handle commands with extra whitespace."""
        plan1 = planner.plan("open   chrome")
        plan2 = planner.plan("open chrome")
        
        assert plan1.step_count == plan2.step_count
        assert plan1.steps[0].action == plan2.steps[0].action

    def test_mixed_case(self, planner):
        """Handle mixed case input."""
        plan1 = planner.plan("OPEN CHROME")
        plan2 = planner.plan("open chrome")
        plan3 = planner.plan("Open Chrome")
        
        assert plan1.step_count == plan2.step_count == plan3.step_count

    def test_punctuation(self, planner):
        """Handle punctuation in commands."""
        test_cases = [
            "open chrome.",
            "open chrome!",
            "open chrome?",
            "open chrome,",
        ]
        
        for text in test_cases:
            plan = planner.plan(text)
            assert plan.step_count >= 1

    def test_unknown_command(self, planner):
        """Handle completely unknown commands."""
        plan = planner.plan("blabla xyzabc nonsense")
        assert plan.step_count >= 1

    def test_partial_command(self, planner):
        """Handle partial/incomplete commands."""
        plan = planner.plan("open")
        assert plan.step_count >= 1


class TestValidation:
    """Tests for plan validation."""

    @pytest.fixture
    def planner(self):
        return ActionPlanner()

    def test_valid_plan_is_recognized(self, planner):
        """Valid plans pass validation."""
        plan = planner.plan("open chrome and search weather")
        assert plan.is_valid

    def test_empty_plan_is_invalid(self, planner):
        """Empty plans are marked invalid."""
        plan = planner.plan("")
        assert not plan.is_valid

    def test_step_order_correctness(self, planner):
        """Step orders are sequential and correct."""
        plan = planner.plan("open chrome and search weather and play result")
        
        for i, step in enumerate(plan.steps, 1):
            assert step.order == i

    def test_dependencies_reference_valid_steps(self, planner):
        """All dependencies reference existing steps."""
        plan = planner.plan("open chrome and search weather and play result")
        
        all_step_ids = {step.id for step in plan.steps}
        
        for step in plan.steps:
            for dep_id in step.depends_on:
                assert dep_id in all_step_ids


class TestConfidence:
    """Tests for confidence scoring."""

    @pytest.fixture
    def planner(self):
        return ActionPlanner()

    def test_confidence_in_valid_range(self, planner):
        """Confidence scores are between 0 and 1."""
        plans = [
            planner.plan("open chrome"),
            planner.plan("open chrome and search weather"),
            planner.plan("nonsense gibberish"),
            planner.plan(""),
        ]
        
        for plan in plans:
            assert 0.0 <= plan.confidence <= 1.0

    def test_known_commands_higher_confidence(self, planner):
        """Known commands have higher confidence."""
        plan_known = planner.plan("open chrome")
        plan_unknown = planner.plan("xyzabc 123 blah")
        
        assert plan_known.confidence > plan_unknown.confidence


class TestPlannerContext:
    """Tests for context-aware planning."""

    @pytest.fixture
    def planner(self):
        return ActionPlanner()

    def test_planning_with_empty_context(self, planner):
        """Planner works with no context."""
        plan = planner.plan("open chrome", context=None)
        assert plan.step_count >= 1

    def test_planning_with_current_app_context(self, planner):
        """Planner can use current app context."""
        context = PlannerContext(current_app="Chrome")
        plan = planner.plan("search weather", context=context)
        assert plan.step_count >= 1

    def test_planning_with_context_hints(self, planner):
        """Planner uses context hints when provided."""
        context_hints = {"steps": [{"order": 1, "action": "OPEN_APP", "target": "notepad"}]}
        plan = planner.plan("open notepad", context_hints=context_hints)
        
        assert plan.step_count == 1
        assert plan.planner_used == "context"


class TestPlanOutput:
    """Tests for plan output formats."""

    @pytest.fixture
    def planner(self):
        return ActionPlanner()

    def test_plan_to_dict(self, planner):
        """Plans can be converted to dictionaries."""
        plan = planner.plan("open chrome and search weather")
        plan_dict = plan.to_dict()
        
        assert "steps" in plan_dict
        assert "confidence" in plan_dict
        assert "planner_used" in plan_dict
        assert isinstance(plan_dict["steps"], list)

    def test_step_to_dict(self, planner):
        """Steps can be converted to dictionaries."""
        plan = planner.plan("open chrome")
        step = plan.steps[0]
        step_dict = step.to_dict()
        
        assert "action" in step_dict
        assert "target" in step_dict
        assert "order" in step_dict

    def test_plan_string_representation(self, planner):
        """Plans have readable string representation."""
        plan = planner.plan("open chrome and search weather")
        plan_str = str(plan)
        
        assert "Plan" in plan_str
        assert "open_app" in plan_str
        assert "search" in plan_str

    def test_step_string_representation(self, planner):
        """Steps have readable string representation."""
        plan = planner.plan("open chrome")
        step = plan.steps[0]
        step_str = str(step)
        
        assert "Step" in step_str
        assert "open_app" in step_str


class TestPlannerStatistics:
    """Tests for planning statistics and metrics."""

    @pytest.fixture
    def planner(self):
        return ActionPlanner()

    def test_plan_step_count(self, planner):
        """Step counts are accurate."""
        plan1 = planner.plan("open chrome")
        assert plan1.step_count == 1
        
        plan2 = planner.plan("open chrome and search weather")
        assert plan2.step_count == 2

    def test_planner_used_tracking(self, planner):
        """Planner type is correctly recorded."""
        plan = planner.plan("open chrome")
        assert plan.planner_used in ["rules", "context", "fallback"]


class TestPlaySelection:
    """Tests for play action parameter handling."""

    @pytest.fixture
    def planner(self):
        return ActionPlanner()

    def test_play_first_result(self, planner):
        """'Play first' correctly sets selection."""
        plan = planner.plan("play first song")
        if plan.step_count > 0:
            play_step = [s for s in plan.steps if s.action == ActionType.PLAY]
            if play_step:
                assert play_step[0].params.get("selection") == 1


class TestRealWorldExamples:
    """Tests with real-world command examples."""

    @pytest.fixture
    def planner(self):
        return ActionPlanner()

    def test_example_ipl_score(self, planner):
        """'Open Chrome and search IPL score'."""
        plan = planner.plan("Open Chrome and search IPL score")
        assert plan.step_count == 2
        assert plan.steps[0].action == ActionType.OPEN_APP
        assert plan.steps[1].action == ActionType.SEARCH

    def test_example_youtube_song(self, planner):
        """'Open YouTube and play Dulaunder song'."""
        plan = planner.plan("Open YouTube and play Dulaunder song")
        assert plan.step_count >= 2
        assert plan.steps[0].action == ActionType.OPEN_APP

    def test_example_spotify_discord(self, planner):
        """'Open Spotify and play first song, then open Discord'."""
        plan = planner.plan("Open Spotify and play first song, then open Discord")
        assert plan.step_count >= 3

    def test_example_search_weather(self, planner):
        """'Search weather and play spotify'."""
        plan = planner.plan("search weather and play spotify")
        assert plan.step_count >= 2
