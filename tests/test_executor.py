"""
Phase 12 — Execution Engine Test Suite

Covers all 10 test scenarios from the spec:

 1. Single-step app launch executes (real + mocked)
 2. Multi-step sequence executes in order
 3. Failed first step skips dependent second step
 4. Unknown action handled honestly
 5. Timeout handled
 6. Progress updates correctly
 7. Final counts correct
 8. No freeze during execution (thread-based test)
 9. Cancel mid-plan works
10. Full execution trace saved in ExecutionResult

Also covers:
 - stop_on_error vs continue_on_error policies
 - Safety gate (dangerous action blocked)
 - BrowserSkill search
 - SystemSkill structure
 - ExecutionResult / StepResult model correctness
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch, PropertyMock
import pytest

from core.execution_models import ExecutionResult, StepResult, StepStatus
from core.executor import ExecutionEngine, _fail
from core.plan_models import ActionType, ExecutionPlan, PlanStep
from core.safety import SafetyGate, is_dangerous
from skills.base import SkillExecutionResult


# =========================================================================== #
# Fixtures and helpers                                                          #
# =========================================================================== #

def _make_step(
    step_id: str,
    action: ActionType,
    target: str = "",
    params: dict | None = None,
    depends_on: list | None = None,
    risk: str = "low",
) -> PlanStep:
    """Build a PlanStep for tests."""
    return PlanStep(
        id=step_id,
        order=int(step_id.split("_")[-1]) if "_" in step_id else 1,
        action=action,
        target=target,
        params=params or {},
        depends_on=depends_on or [],
        estimated_risk=risk,
    )


def _make_plan(steps: list[PlanStep], original_text: str = "test plan") -> ExecutionPlan:
    """Build an ExecutionPlan for tests."""
    plan = ExecutionPlan(
        original_text=original_text,
        normalized_text=original_text,
        steps=steps,
        confidence=0.9,
        planner_used="rules",
    )
    # Assign sequential orders
    for i, s in enumerate(plan.steps, 1):
        s.order = i
    return plan


def _engine_with_mock_launcher(
    launch_success: bool = True,
    launch_message: str = "Opened App",
) -> ExecutionEngine:
    """Create an ExecutionEngine whose AppLauncher is fully mocked."""
    mock_launcher = MagicMock()
    launch_result = MagicMock()
    launch_result.success  = launch_success
    launch_result.message  = launch_message if launch_success else f"Could not launch: {launch_message}"
    launch_result.app_name = "TestApp"
    launch_result.matched_name = "TestApp"
    launch_result.path     = "C:\\fake\\app.exe"
    launch_result.pid      = 1234 if launch_success else -1
    launch_result.verified = launch_success
    launch_result.data     = {}
    launch_result.error    = "" if launch_success else "launch_failed"
    mock_launcher.launch_app.return_value = launch_result
    mock_launcher.launch_by_name.return_value = launch_result

    with patch("core.executor.ExecutionEngine.__init__", lambda self, **kw: None):
        engine = ExecutionEngine.__new__(ExecutionEngine)

    engine._launcher    = mock_launcher
    engine._browser_controller = MagicMock()
    engine._browser     = MagicMock()
    engine._youtube     = MagicMock()
    engine._music       = MagicMock()
    engine._whatsapp    = MagicMock()
    engine._explorer    = MagicMock()
    engine._files       = MagicMock()
    engine._system      = MagicMock()
    engine._safety_gate = SafetyGate(confirm_callback=lambda _: True)
    engine._progress_cb = None
    engine._cancel_event = threading.Event()
    engine._pause_event  = threading.Event()
    engine._pause_event.set()
    engine._running      = False
    return engine


def test_app_action_routes_to_music_skill():
    engine = _engine_with_mock_launcher()
    engine._music.execute_operation.return_value = MagicMock(
        success=True,
        message="Skipping to next track",
        error="",
        data={"provider": "spotify", "target_app": "spotify"},
    )
    step = _make_step(
        "step_1",
        ActionType.APP_ACTION,
        target="spotify",
        params={"operation": "next_track"},
    )

    result = engine.dispatch(step)

    assert result["success"] is True
    assert result["message"] == "Skipping to next track"
    engine._music.execute_operation.assert_called_once_with("next_track")


def test_file_action_routes_to_file_manager():
    engine = _engine_with_mock_launcher()
    engine._files.find_matches.return_value = [Path("C:/Users/User/Desktop/notes.txt")]
    engine._files.open_file.return_value = MagicMock(
        success=True,
        message="Opening Desktop\\notes.txt.",
        error="",
        action="open",
        source_path="C:\\Users\\User\\Desktop\\notes.txt",
        target_path="",
        timestamp="2026-04-23T00:00:00+00:00",
    )
    step = _make_step(
        "step_1",
        ActionType.FILE_ACTION,
        target="notes.txt",
        params={"action": "open"},
    )

    result = engine.dispatch(step)

    assert result["success"] is True
    assert result["message"] == "Opening Desktop\\notes.txt."
    assert result["data"]["target_app"] == "files"


# =========================================================================== #
# 1 — Single-step app launch                                                    #
# =========================================================================== #

class TestSingleStepLaunch:
    """Test 1: Single step app launch with real mock."""

    def test_open_app_success(self):
        engine = _engine_with_mock_launcher(launch_success=True)
        step   = _make_step("step_1", ActionType.OPEN_APP, target="chrome")
        result = engine.execute_step(step, idx=1, total=1)

        assert result.status == StepStatus.SUCCESS
        assert result.step_id == "step_1"
        assert result.duration >= 0
        assert result.error == ""

    def test_open_app_failure(self):
        engine = _engine_with_mock_launcher(launch_success=False, launch_message="Not found")
        step   = _make_step("step_1", ActionType.OPEN_APP, target="notarealapp")
        result = engine.execute_step(step, idx=1, total=1)

        assert result.status == StepStatus.FAILED
        assert result.error != ""

    def test_open_app_empty_target(self):
        engine = _engine_with_mock_launcher()
        step   = _make_step("step_1", ActionType.OPEN_APP, target="")
        result = engine.execute_step(step, idx=1, total=1)

        assert result.status == StepStatus.FAILED
        assert "app" in result.message.lower()


def test_send_message_uses_whatsapp_skill_result():
    engine = _engine_with_mock_launcher()
    engine._whatsapp.send_message.return_value = SkillExecutionResult(
        success=True,
        intent="whatsapp_message",
        response="I sent 'hi' to Charan.",
        skill_name="WhatsAppSkill",
        data={"contact": "charan", "message": "hi", "target_app": "whatsapp", "verified": True},
    )
    step = _make_step(
        "step_1",
        ActionType.SEND_MESSAGE,
        target="charan",
        params={"message": "hi", "target_app": "whatsapp"},
    )

    result = engine.execute_step(step, idx=1, total=1)

    assert result.status == StepStatus.SUCCESS
    assert result.message == "I sent 'hi' to Charan."
    assert result.data["target_app"] == "whatsapp"
    engine._whatsapp.send_message.assert_called_once_with("charan", "hi")


def test_send_message_returns_skill_failure_instead_of_manual_fallback():
    engine = _engine_with_mock_launcher()
    engine._whatsapp.send_message.return_value = SkillExecutionResult(
        success=False,
        intent="whatsapp_action",
        response="Couldn't send to Charan. Check if they exist.",
        skill_name="WhatsAppSkill",
        error="send_failed",
        data={"target_app": "whatsapp"},
    )
    step = _make_step(
        "step_1",
        ActionType.SEND_MESSAGE,
        target="charan",
        params={"message": "hi", "target_app": "whatsapp"},
    )

    result = engine.execute_step(step, idx=1, total=1)

    assert result.status == StepStatus.FAILED
    assert "Find Charan and send" not in result.message
    assert result.error == "send_failed"


# =========================================================================== #
# 2 — Multi-step sequence in order                                              #
# =========================================================================== #

class TestMultiStepSequenceOrder:
    """Test 2: Multi-step plan executes in order and records correct statuses."""

    def test_three_step_order(self, tmp_path):
        """open_app → search → open_app sequence."""
        execution_order: List[str] = []

        engine = _engine_with_mock_launcher(launch_success=True)

        # Intercept dispatch to record order
        original_dispatch = engine.dispatch

        def tracking_dispatch(step):
            execution_order.append(step.id)
            return original_dispatch(step)

        engine.dispatch = tracking_dispatch

        # Mock browser search
        browser_mock = MagicMock()
        browser_mock.search.return_value = MagicMock(
            success=True, message="Searched", error="", url="http://google.com", engine="google"
        )
        engine._browser = browser_mock

        steps = [
            _make_step("step_1", ActionType.OPEN_APP, "chrome"),
            _make_step("step_2", ActionType.SEARCH, params={"query": "IPL score"}),
            _make_step("step_3", ActionType.OPEN_APP, "youtube"),
        ]
        plan   = _make_plan(steps)
        result = engine.execute_plan(plan)

        assert execution_order == ["step_1", "step_2", "step_3"]
        assert result.total_steps == 3
        assert result.completed_steps == 3
        assert result.failed_steps == 0

    def test_results_list_matches_step_count(self):
        engine = _engine_with_mock_launcher()
        steps  = [
            _make_step("step_1", ActionType.OPEN_APP, "chrome"),
            _make_step("step_2", ActionType.OPEN_APP, "youtube"),
        ]
        plan   = _make_plan(steps)
        result = engine.execute_plan(plan)

        assert len(result.results) == 2
        assert all(r.status.is_terminal for r in result.results)


# =========================================================================== #
# 3 — Dependency: failed step skips dependent                                   #
# =========================================================================== #

class TestDependencySkipping:
    """Test 3: If step 1 fails, step 2 (which depends on step 1) is skipped."""

    def test_dependent_step_skipped_when_dependency_fails(self):
        engine = _engine_with_mock_launcher(launch_success=False)
        steps  = [
            _make_step("step_1", ActionType.OPEN_APP, "chrome"),
            _make_step("step_2", ActionType.SEARCH,
                       params={"query": "IPL"}, depends_on=["step_1"]),
        ]
        # stop_on_error must be False so engine continues to step 2
        with patch("core.settings.get", side_effect=lambda k: (
            False if k == "stop_on_error" else 20 if k == "execution_timeout" else True
        )):
            plan   = _make_plan(steps)
            result = engine.execute_plan(plan)

        statuses = {r.step_id: r.status for r in result.results}
        assert statuses["step_1"] == StepStatus.FAILED
        assert statuses["step_2"] == StepStatus.SKIPPED

    def test_independent_step_not_skipped_despite_sibling_failure(self):
        """Step 3 has no dependency on step 1 — should still execute."""
        engine = _engine_with_mock_launcher(launch_success=False)

        browser_mock = MagicMock()
        browser_mock.search.return_value = MagicMock(
            success=True, message="OK", error="", url="http://x.com", engine="google"
        )
        engine._browser = browser_mock

        steps = [
            _make_step("step_1", ActionType.OPEN_APP, "chrome"),
            _make_step("step_2", ActionType.OPEN_APP, "youtube"),   # also open_app, also fails
            _make_step("step_3", ActionType.SEARCH, params={"query": "news"}),  # no dep
        ]
        with patch("core.settings.get", side_effect=lambda k: (
            False if k == "stop_on_error" else 20 if k == "execution_timeout" else True
        )):
            plan   = _make_plan(steps)
            result = engine.execute_plan(plan)

        step3_res = next(r for r in result.results if r.step_id == "step_3")
        assert step3_res.status == StepStatus.SUCCESS


# =========================================================================== #
# 4 — Unknown action handled honestly                                           #
# =========================================================================== #

class TestUnknownAction:
    """Test 4: UNKNOWN action returns FAILED with honest error, never success."""

    def test_unknown_action_fails_honestly(self):
        engine = _engine_with_mock_launcher()
        step   = _make_step("step_1", ActionType.UNKNOWN, target="do the thing")
        result = engine.execute_step(step, idx=1, total=1)

        assert result.status == StepStatus.FAILED
        assert result.error == "unknown_action"
        assert "Unknown action" in result.message

    def test_unknown_plan_produces_failed_result(self):
        engine = _engine_with_mock_launcher()
        steps  = [_make_step("step_1", ActionType.UNKNOWN, target="nonsense")]
        plan   = _make_plan(steps)
        result = engine.execute_plan(plan)

        assert result.failed_steps == 1
        assert result.success is False


# =========================================================================== #
# 5 — Timeout handled                                                           #
# =========================================================================== #

class TestTimeoutHandling:
    """Test 5: Steps that exceed the timeout are marked FAILED with 'timeout' error."""

    def test_step_times_out(self):
        engine = _engine_with_mock_launcher()

        # Use an Event so the "slow" dispatch blocks until the test terminates it
        # This avoids hanging the thread pool since the daemon thread will exit
        # once the test process ends or the event is set.
        release_event = threading.Event()

        def slow_dispatch(step):
            # Block until the test signals us (or daemon thread is cleaned up)
            release_event.wait(timeout=60)  # daemon — won't block process exit
            return {"success": True, "message": "OK", "error": "", "data": {}}

        engine.dispatch = slow_dispatch

        step = _make_step("step_1", ActionType.OPEN_APP, "chrome")

        with patch("core.settings.get", side_effect=lambda k: (
            1 if k == "execution_timeout" else True   # 1-second timeout
        )):
            result = engine.execute_step(step, idx=1, total=1)

        # Release the background thread so it can exit cleanly
        release_event.set()

        assert result.status == StepStatus.FAILED
        assert result.error == "timeout"
        assert "timed out" in result.message.lower()



# =========================================================================== #
# 6 — Progress updates correctly                                                #
# =========================================================================== #

class TestProgressUpdates:
    """Test 6: Progress callback is called with the right messages."""

    def test_progress_callback_fired_per_step(self):
        progress_messages: List[str] = []

        engine = _engine_with_mock_launcher(launch_success=True)
        engine._progress_cb = progress_messages.append

        steps  = [
            _make_step("step_1", ActionType.OPEN_APP, "chrome"),
            _make_step("step_2", ActionType.OPEN_APP, "youtube"),
        ]
        plan   = _make_plan(steps)

        with patch("core.settings.get", side_effect=lambda k: (
            True if k == "show_step_progress" else
            False if k == "stop_on_error" else
            20
        )):
            engine.execute_plan(plan)

        # Should have at minimum "Executing step 1/2: ...", "Executing step 2/2: ...", "summary"
        assert any("step 1" in m.lower() or "1/2" in m for m in progress_messages)
        assert any("step 2" in m.lower() or "2/2" in m for m in progress_messages)

    def test_no_callback_does_not_crash(self):
        engine          = _engine_with_mock_launcher()
        engine._progress_cb = None
        step            = _make_step("step_1", ActionType.OPEN_APP, "chrome")
        result          = engine.execute_step(step)
        # Should not raise despite no callback
        assert result.status in (StepStatus.SUCCESS, StepStatus.FAILED)


# =========================================================================== #
# 7 — Final counts correct                                                      #
# =========================================================================== #

class TestFinalCounts:
    """Test 7: ExecutionResult counts match actual step outcomes."""

    def test_all_success_counts(self):
        engine = _engine_with_mock_launcher(launch_success=True)
        steps  = [_make_step(f"step_{i}", ActionType.OPEN_APP, "chrome") for i in range(1, 4)]
        plan   = _make_plan(steps)
        result = engine.execute_plan(plan)

        assert result.total_steps     == 3
        assert result.completed_steps == 3
        assert result.failed_steps    == 0
        assert result.skipped_steps   == 0
        assert result.success         is True

    def test_mixed_counts(self):
        engine = _engine_with_mock_launcher(launch_success=False)

        browser_mock = MagicMock()
        browser_mock.search.return_value = MagicMock(
            success=True, message="OK", error="", url="http://g.com", engine="google"
        )
        engine._browser = browser_mock

        steps = [
            _make_step("step_1", ActionType.OPEN_APP, "chrome"),          # FAILED (mock)
            _make_step("step_2", ActionType.SEARCH, params={"query": "x"}),  # SUCCESS (browser)
        ]
        with patch("core.settings.get", side_effect=lambda k: (
            False if k == "stop_on_error" else 20 if k == "execution_timeout" else True
        )):
            plan   = _make_plan(steps)
            result = engine.execute_plan(plan)

        assert result.total_steps     == 2
        assert result.failed_steps    == 1
        assert result.completed_steps == 1
        assert result.success         is False

    def test_finalise_sets_summary(self):
        result = ExecutionResult.start_new("test_plan", total_steps=2)
        sr1 = StepResult(step_id="step_1")
        sr1.mark_running(); sr1.mark_success("OK")
        sr2 = StepResult(step_id="step_2")
        sr2.mark_running(); sr2.mark_failed("oops", "Failed")
        result.results = [sr1, sr2]
        result.finalise()

        assert result.failed_steps    == 1
        assert result.completed_steps == 1
        assert result.summary         != ""
        assert result.duration        >= 0


# =========================================================================== #
# 8 — No freeze during execution (threading check)                              #
# =========================================================================== #

class TestNonBlocking:
    """Test 8: execute_plan runs correctly from a separate thread."""

    def test_execute_plan_in_thread(self):
        engine = _engine_with_mock_launcher(launch_success=True)
        steps  = [_make_step("step_1", ActionType.OPEN_APP, "chrome")]
        plan   = _make_plan(steps)

        result_holder: List[ExecutionResult] = []

        def run():
            result_holder.append(engine.execute_plan(plan))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=10)

        assert not t.is_alive(), "Thread did not finish within 10 seconds"
        assert len(result_holder) == 1
        assert result_holder[0].total_steps == 1


# =========================================================================== #
# 9 — Cancel mid-plan                                                           #
# =========================================================================== #

class TestCancelMidPlan:
    """Test 9: Cancelling the engine stops execution after the current step."""

    def test_cancel_stops_remaining_steps(self):
        executed: List[str] = []

        engine = _engine_with_mock_launcher(launch_success=True)

        original_dispatch = engine.dispatch

        def tracking_dispatch(step):
            executed.append(step.id)
            if step.id == "step_1":
                # Simulate cancel after first step starts
                engine.cancel()
            return original_dispatch(step)

        engine.dispatch = tracking_dispatch

        steps = [
            _make_step("step_1", ActionType.OPEN_APP, "chrome"),
            _make_step("step_2", ActionType.OPEN_APP, "youtube"),
            _make_step("step_3", ActionType.OPEN_APP, "discord"),
        ]
        with patch("core.settings.get", side_effect=lambda k: (
            False if k == "stop_on_error" else 20 if k == "execution_timeout" else True
        )):
            plan   = _make_plan(steps)
            result = engine.execute_plan(plan)

        # step_1 was dispatched; step_2 and step_3 should be cancelled
        assert "step_1" in executed
        cancelled = [r for r in result.results if r.status == StepStatus.CANCELLED]
        assert len(cancelled) >= 1

    def test_cancel_when_idle_does_not_crash(self):
        engine = _engine_with_mock_launcher()
        engine.cancel()  # Should not raise
        assert engine._cancel_event.is_set()


# =========================================================================== #
# 10 — Full execution trace logged in ExecutionResult                           #
# =========================================================================== #

class TestExecutionTrace:
    """Test 10: ExecutionResult contains complete per-step trace."""

    def test_full_trace_recorded(self):
        engine = _engine_with_mock_launcher(launch_success=True)
        steps  = [
            _make_step("step_1", ActionType.OPEN_APP, "chrome"),
            _make_step("step_2", ActionType.OPEN_APP, "youtube"),
        ]
        plan   = _make_plan(steps)
        result = engine.execute_plan(plan)

        assert len(result.results) == 2
        for sr in result.results:
            assert sr.step_id in {"step_1", "step_2"}
            assert sr.started_at > 0
            assert sr.finished_at >= sr.started_at
            assert sr.duration >= 0
            assert sr.status.is_terminal

    def test_to_dict_contains_all_fields(self):
        engine = _engine_with_mock_launcher(launch_success=True)
        step   = _make_step("step_1", ActionType.OPEN_APP, "chrome")
        plan   = _make_plan([step])
        result = engine.execute_plan(plan)

        d = result.to_dict()
        required = {
            "plan_id", "success", "total_steps", "completed_steps",
            "failed_steps", "skipped_steps", "cancelled_steps",
            "duration", "summary", "results",
        }
        assert required.issubset(d.keys())
        assert isinstance(d["results"], list)
        assert len(d["results"]) == 1


# =========================================================================== #
# Safety Gate Tests                                                              #
# =========================================================================== #

class TestSafetyGate:
    """Validate the safety gate blocks dangerous steps correctly."""

    def test_shutdown_step_is_dangerous(self):
        step = _make_step("step_1", ActionType.SYSTEM_CONTROL, "shutdown")
        assert is_dangerous(step)

    def test_delete_file_is_dangerous(self):
        step = _make_step(
            "step_1", ActionType.FILE_ACTION, "important.docx",
            params={"action": "delete"}
        )
        assert is_dangerous(step)

    def test_high_risk_step_is_dangerous(self):
        step = _make_step("step_1", ActionType.OPEN_APP, "calc", risk="high")
        assert is_dangerous(step)

    def test_normal_step_is_not_dangerous(self):
        step = _make_step("step_1", ActionType.OPEN_APP, "chrome")
        assert not is_dangerous(step)

    def test_gate_blocks_when_callback_returns_false(self):
        gate   = SafetyGate(confirm_callback=lambda _: False)
        step   = _make_step("step_1", ActionType.SYSTEM_CONTROL, "shutdown")
        allowed, reason = gate.check(step)
        assert not allowed
        assert reason != ""

    def test_gate_allows_when_callback_returns_true(self):
        gate   = SafetyGate(confirm_callback=lambda _: True)
        step   = _make_step("step_1", ActionType.SYSTEM_CONTROL, "shutdown")
        allowed, _ = gate.check(step)
        assert allowed

    def test_gate_blocks_when_no_callback(self):
        gate = SafetyGate(confirm_callback=None)
        step = _make_step("step_1", ActionType.SYSTEM_CONTROL, "shutdown")
        with patch("core.settings.get", return_value=True):
            allowed, reason = gate.check(step)
        assert not allowed

    def test_dangerous_step_cancelled_in_engine(self):
        engine = _engine_with_mock_launcher()
        # Safety gate will deny
        engine._safety_gate = SafetyGate(confirm_callback=lambda _: False)
        step   = _make_step("step_1", ActionType.SYSTEM_CONTROL, "shutdown", risk="high")
        result = engine.execute_step(step, idx=1, total=1)
        assert result.status == StepStatus.CANCELLED


# =========================================================================== #
# StepResult / ExecutionResult Model Tests                                      #
# =========================================================================== #

class TestModels:
    """Validate model behaviour and transitions."""

    def test_step_result_lifecycle(self):
        sr = StepResult(step_id="test_1")
        assert sr.status == StepStatus.PENDING

        sr.mark_running()
        assert sr.status == StepStatus.RUNNING
        assert sr.started_at > 0

        sr.mark_success("done", data={"key": "val"})
        assert sr.status == StepStatus.SUCCESS
        assert sr.duration >= 0
        assert sr.data.get("key") == "val"
        assert sr.error == ""

    def test_step_result_failure(self):
        sr = StepResult(step_id="test_2")
        sr.mark_running()
        sr.mark_failed(error="crash", message="it blew up")
        assert sr.status == StepStatus.FAILED
        assert sr.error == "crash"
        assert "blew up" in sr.message

    def test_step_result_skipped(self):
        sr = StepResult(step_id="test_3")
        sr.mark_skipped("dep failed")
        assert sr.status == StepStatus.SKIPPED
        assert sr.started_at == 0  # never ran

    def test_step_result_to_dict(self):
        sr = StepResult(step_id="test_4")
        sr.mark_running(); sr.mark_success("OK")
        d  = sr.to_dict()
        assert d["step_id"] == "test_4"
        assert d["status"]  == "success"
        assert isinstance(d["duration"], float)

    def test_step_status_is_terminal(self):
        for status in (StepStatus.SUCCESS, StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.CANCELLED):
            assert status.is_terminal
        for status in (StepStatus.PENDING, StepStatus.RUNNING):
            assert not status.is_terminal

    def test_execution_result_start_new(self):
        er = ExecutionResult.start_new("plan_abc", total_steps=5)
        assert er.plan_id == "plan_abc"
        assert er.total_steps == 5
        assert er.started_at > 0

    def test_execution_result_empty_finalise(self):
        er = ExecutionResult.start_new("empty", total_steps=0)
        er.finalise()
        assert er.success is True   # no failures = success
        assert er.summary != ""


# =========================================================================== #
# BrowserSkill Tests                                                            #
# =========================================================================== #

class TestBrowserSkill:
    """Validate BrowserSkill search routing logic."""

    def test_search_routes_into_browser_controller(self):
        from core.browser import BrowserOperationResult
        from skills.browser import BrowserSkill

        controller = MagicMock()
        controller.search.return_value = BrowserOperationResult(
            success=True,
            action="search",
            message="Searching IPL score in Chrome.",
            browser_id="chrome",
            query="IPL score",
            verified=True,
        )
        skill = BrowserSkill(controller=controller)

        result = skill.search("IPL score", target_app="chrome")

        assert result.success
        assert result.browser == "chrome"
        controller.search.assert_called_once_with("IPL score", browser_name="chrome", engine=None)

    def test_search_empty_query_fails(self):
        from skills.browser import BrowserSkill
        skill  = BrowserSkill()
        result = skill.search("")
        assert not result.success
        assert result.error != ""

    def test_youtube_engine_resolves_from_target_app(self):
        from core.browser import BrowserOperationResult
        from skills.browser import BrowserSkill
        controller = MagicMock()
        controller.search.return_value = BrowserOperationResult(
            success=True,
            action="search",
            message="Searching dulaunder song in Chrome.",
            browser_id="chrome",
            query="dulaunder song",
            url="https://www.youtube.com/results?search_query=dulaunder+song",
            verified=True,
        )
        skill = BrowserSkill(controller=controller)

        result = skill.search("dulaunder song", target_app="youtube")

        assert result.engine == "youtube"
        assert "youtube" in result.url
        controller.search.assert_called_once_with("dulaunder song", browser_name="", engine="youtube")

    def test_generic_search_defaults_to_google_engine(self):
        from core.browser import BrowserOperationResult
        from skills.browser import BrowserSkill

        controller = MagicMock()
        controller.search.return_value = BrowserOperationResult(
            success=True,
            action="search",
            message="Searching apple music in Chrome.",
            browser_id="chrome",
            query="apple music",
            url="https://www.google.com/search?q=apple+music",
            verified=True,
        )
        skill = BrowserSkill(controller=controller)

        result = skill.search("apple music")

        assert result.success
        assert result.engine == "google"
        controller.search.assert_called_once_with("apple music", browser_name="", engine="google")

    def test_browser_failure_reflected_honestly(self):
        from core.browser import BrowserOperationResult
        from skills.browser import BrowserSkill
        controller = MagicMock()
        controller.search.return_value = BrowserOperationResult(
            success=False,
            action="search",
            message="No supported browser window found.",
            error="browser_not_found",
        )
        skill = BrowserSkill(controller=controller)

        result = skill.search("test query")

        assert not result.success


# =========================================================================== #
# Failure policy: stop_on_error                                                 #
# =========================================================================== #

class TestFailurePolicy:
    """Validate stop_on_error vs continue_on_error policies."""

    def test_stop_on_error_halts_after_first_failure(self):
        executed: List[str] = []
        engine = _engine_with_mock_launcher(launch_success=False)

        original_dispatch = engine.dispatch
        def tracking_dispatch(step):
            executed.append(step.id)
            return original_dispatch(step)
        engine.dispatch = tracking_dispatch

        steps = [
            _make_step("step_1", ActionType.OPEN_APP, "chrome"),
            _make_step("step_2", ActionType.OPEN_APP, "youtube"),
            _make_step("step_3", ActionType.OPEN_APP, "discord"),
        ]
        with patch("core.settings.get", side_effect=lambda k: (
            True if k == "stop_on_error" else 20 if k == "execution_timeout" else True
        )):
            plan   = _make_plan(steps)
            result = engine.execute_plan(plan)

        # With stop_on_error, only step_1 should have been dispatched
        assert "step_1" in executed
        assert "step_2" not in executed
        assert "step_3" not in executed

    def test_continue_on_error_executes_all_steps(self):
        executed: List[str] = []
        engine = _engine_with_mock_launcher(launch_success=False)

        original_dispatch = engine.dispatch
        def tracking_dispatch(step):
            executed.append(step.id)
            return original_dispatch(step)
        engine.dispatch = tracking_dispatch

        steps = [
            _make_step("step_1", ActionType.OPEN_APP, "chrome"),
            _make_step("step_2", ActionType.OPEN_APP, "youtube"),
        ]
        with patch("core.settings.get", side_effect=lambda k: (
            False if k == "stop_on_error" else 20 if k == "execution_timeout" else True
        )):
            plan   = _make_plan(steps)
            result = engine.execute_plan(plan)

        assert "step_1" in executed
        assert "step_2" in executed


# =========================================================================== #
# get_status / pause / resume                                                   #
# =========================================================================== #

class TestEngineControl:
    """Validate engine control methods."""

    def test_get_status_when_idle(self):
        engine = _engine_with_mock_launcher()
        status = engine.get_status()
        assert "running" in status
        assert "cancel_requested" in status
        assert "paused" in status

    def test_pause_and_resume(self):
        engine = _engine_with_mock_launcher()
        engine.pause()
        assert not engine._pause_event.is_set()

        engine.resume()
        assert engine._pause_event.is_set()

    def test_cancel_sets_event(self):
        engine = _engine_with_mock_launcher()
        assert not engine._cancel_event.is_set()
        engine.cancel()
        assert engine._cancel_event.is_set()


# =========================================================================== #
# _fail helper                                                                  #
# =========================================================================== #

class TestFailHelper:
    def test_fail_dict_structure(self):
        d = _fail("oops", "some_error")
        assert d["success"] is False
        assert d["message"] == "oops"
        assert d["error"]   == "some_error"
        assert d["data"]    == {}
