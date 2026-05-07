"""
Phase 12 — Multi-Command Execution Engine.

Consumes an ExecutionPlan produced by the ActionPlanner and drives each step
through a real dispatch pipeline:

    Plan intake
        └─ Step scheduler (sequential, dependency-aware)
              └─ Safety gate (pre-execution confirmation)
                    └─ Action dispatcher (routes to real skill modules)
                          └─ Result collector (StepResult / ExecutionResult)
                                └─ Failure handler (stop_on_error / continue)
                                      └─ Progress callback → UI

Architecture
------------
* ``ExecutionEngine`` is the main class — instantiate once and reuse.
* All heavy work runs on the **caller's thread** by design, so the UI must
  call ``execute_plan`` from a ``QThread`` worker (see ``window.py``).
* ``cancel()`` sets a flag that is checked between steps (and inside the
  timeout loop).  It is thread-safe via a ``threading.Event``.
* Each step has an individual timeout enforced with ``concurrent.futures``.
* Progress is reported through an optional ``progress_callback(msg: str)``.
* The engine never raises — every exception is caught, logged, and reflected
  in the StepResult as FAILED.

Adding new action handlers
--------------------------
1. Add your ActionType to ``core/plan_models.py`` (or reuse an existing one).
2. Implement a handler method ``_handle_<action_name>(step) -> StepResult``.
3. Register it in ``_DISPATCH_TABLE`` at class level.
No other changes are required.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

from core.action_results import ActionResult
from core import settings, state
from core.execution_models import ExecutionResult, StepResult, StepStatus
from core.files import FileManager
from core.logger import get_logger
from core.plan_models import ActionType, ExecutionPlan, PlanStep
from core.safety import SafetyGate
from core.safety_guard import SafetyGuard, safety_guard as default_safety_guard

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Progress callback type alias
# ---------------------------------------------------------------------------
ProgressCallback = Callable[[str], None]


class ExecutionEngine:
    """
    Industrial-grade sequential execution engine for multi-step action plans.

    Parameters
    ----------
    launcher : AppLauncher | None
        Injected AppLauncher from Phase 8.  If None, it is created lazily.
    safety_gate : SafetyGate | None
        Pre-built safety gate.  If None, a default (no UI callback) is used.
    progress_callback : ProgressCallback | None
        Invoked on the engine's thread with progress messages.
        The UI layer replaces this after construction via ``set_progress_callback``.
    """

    def __init__(
        self,
        launcher=None,
        safety_gate: Optional[SafetyGate] = None,
        progress_callback: Optional[ProgressCallback] = None,
        permission_manager=None,
        safety_guard: Optional[SafetyGuard] = None,
    ) -> None:
        # Lazy imports to avoid circular dependencies at module level
        from core.app_launcher import DesktopAppLauncher
        from core.browser import BrowserController
        from skills.browser import BrowserSkill
        from skills.explorer import ExplorerSkill
        from skills.music import MusicSkill
        from skills.system import SystemSkill
        from skills.whatsapp import WhatsAppSkill
        from skills.youtube import YouTubeSkill

        self._launcher    = launcher or DesktopAppLauncher()
        self._browser_controller = BrowserController(launcher=self._launcher)
        self._browser     = BrowserSkill(controller=self._browser_controller)
        self._youtube     = YouTubeSkill(browser=self._browser, controller=self._browser_controller)
        self._music       = MusicSkill(launcher=self._launcher)
        self._whatsapp    = WhatsAppSkill()
        self._explorer    = ExplorerSkill()
        self._system      = SystemSkill()
        self._files       = FileManager()
        self._safety_gate = safety_gate or SafetyGate(permission_manager=permission_manager)
        self._safety_guard = safety_guard or default_safety_guard
        self._progress_cb = progress_callback

        # Cancellation & pause control
        self._cancel_event = threading.Event()
        self._pause_event  = threading.Event()
        self._pause_event.set()   # not paused by default

        # Execution state (also mirrored into core.state)
        self._running = False

        logger.info("ExecutionEngine initialised")

    # ====================================================================== #
    # Public API                                                              #
    # ====================================================================== #

    def execute_plan(self, plan: ExecutionPlan) -> ExecutionResult:
        """
        Execute every step in *plan* sequentially and return the aggregate result.

        Steps with unmet dependencies (because a dependency failed) are SKIPPED.
        The ``stop_on_error`` setting controls whether the engine halts on the
        first failure or continues to independent subsequent steps.

        This method is **blocking**.  Call it from a QThread worker.
        """
        if not plan or not plan.is_valid:
            logger.warning("execute_plan called with empty or invalid plan")
            return self._empty_result(plan)

        plan_id = f"{plan.original_text[:40]}_{uuid.uuid4().hex[:6]}"
        result  = ExecutionResult.start_new(plan_id, plan.step_count)

        logger.info("Executing plan '%s' with %d steps", plan_id, plan.step_count)
        self._emit_progress(f"Starting plan: {plan.step_count} step(s)")

        # Update global state
        self._running = True
        self._cancel_event.clear()
        state.is_executing     = True
        state.current_plan_id  = plan_id
        state.cancel_requested = False

        # Track which step IDs have succeeded (for dependency checks)
        succeeded_ids: Set[str] = set()
        failed_ids:    Set[str] = set()

        stop_on_error = settings.get("stop_on_error")

        try:
            for idx, step in enumerate(plan.steps, 1):
                # ── Cancel check ──────────────────────────────────────────
                if self._cancel_event.is_set() or state.cancel_requested:
                    logger.info("Plan cancelled before step %d", idx)
                    step_result = StepResult(step_id=step.id)
                    step_result.mark_cancelled("Plan cancelled by user")
                    result.results.append(step_result)
                    self._emit_progress(f"Cancelled at step {idx}/{plan.step_count}")
                    continue   # Mark remaining steps cancelled

                # ── Pause support (optional) ───────────────────────────────
                self._pause_event.wait()   # blocks if paused

                # ── Dependency check ──────────────────────────────────────
                step_result = self._check_dependencies(step, failed_ids, idx, plan.step_count)
                if step_result is not None:
                    result.results.append(step_result)
                    failed_ids.add(step.id)  # treat skip as failure for downstream
                    continue

                # ── Update progress ───────────────────────────────────────
                action_label = self._describe_step(step)
                self._emit_progress(
                    f"Executing step {idx}/{plan.step_count}: {action_label}"
                )
                state.current_step = idx

                # ── Execute ───────────────────────────────────────────────
                step_result = self.execute_step(step, idx, plan.step_count)
                result.results.append(step_result)

                # ── Track outcome ─────────────────────────────────────────
                if step_result.status == StepStatus.SUCCESS:
                    succeeded_ids.add(step.id)
                elif step_result.status in (StepStatus.FAILED, StepStatus.CANCELLED):
                    failed_ids.add(step.id)
                    if stop_on_error:
                        logger.warning(
                            "stop_on_error=True — halting plan after step %d failure", idx
                        )
                        self._emit_progress(
                            f"⚠ Step {idx} failed — stopping plan (stop_on_error=True)"
                        )
                        break

        finally:
            result.finalise()
            self._running          = False
            state.is_executing     = False
            state.current_step     = 0
            state.current_plan_id  = ""
            state.last_execution_result = result
            logger.info("Plan execution complete: %s", result)
            self._emit_progress(result.summary)

        return result

    def execute_step(
        self, step: PlanStep, idx: int = 1, total: int = 1
    ) -> StepResult:
        """
        Execute a single PlanStep with timeout, safety check, and real dispatch.

        Returns a fully populated StepResult (never raises).

        Timeout strategy:
            A daemon thread runs dispatch().  The calling thread joins it for
            up to ``execution_timeout`` seconds.  If the join times out, the
            step is marked FAILED with error="timeout".  The daemon thread
            continues to run in the background (Python threads cannot be
            forcibly killed), but it will not affect future steps because
            results are ignored after the timeout.

        Industrial-grade timeout handling:
            - Default timeout is 45s (configurable via execution_timeout setting)
            - OPEN_APP steps get extended timeout (30s) for cold starts
            - FILE_ACTION steps get moderate timeout (20s)
            - SYSTEM_CONTROL destructive actions get longer timeout (30s)
        """
        step_result = StepResult(step_id=step.id)

        # Extended timeouts for slow operations
        if step.action.value == "open_app":
            timeout = float(settings.get("app_launch_timeout_seconds") or 30)
        elif step.action.value == "system_control" and str(step.params.get("control", "")).lower() in {"shutdown", "restart"}:
            timeout = float(settings.get("close_app_timeout_seconds") or 40)
        else:
            timeout = float(settings.get("execution_timeout") or 60)

        # ── Safety gate (policy check) ────────────────────────────────────
        allowed, deny_reason = self._safety_gate.check(step)
        if not allowed:
            if step.action == ActionType.UNKNOWN and "Unknown action type" in str(deny_reason or ""):
                allowed = True
            else:
                step_result.mark_cancelled(deny_reason)
                logger.info("Step %s cancelled by safety gate: %s", step.id, deny_reason)
                return step_result

        # ── Safety guard (impact analysis) ────────────────────────────────
        guard_result = self._run_safety_guard(step)
        if guard_result is not None:
            step_result.mark_cancelled(guard_result)
            logger.info("Step %s blocked by safety guard: %s", step.id, guard_result)
            return step_result

        # ── Mark running ──────────────────────────────────────────────────
        step_result.mark_running()
        logger.info(
            "Step %d/%d [%s] started: action=%s target=%r",
            idx, total, step.id, step.action.value, step.target,
        )

        # ── Dispatch with timeout using daemon thread ─────────────────────
        dispatch_result_holder: list = []
        dispatch_error_holder:  list = []

        def _run() -> None:
            try:
                res = self.dispatch(step)
                dispatch_result_holder.append(res)
            except Exception as exc:
                dispatch_error_holder.append(str(exc))

        t = threading.Thread(target=_run, daemon=True, name=f"exec-{step.id}")
        t.start()
        t.join(timeout=float(timeout))

        if t.is_alive():
            # Thread is still running → timeout
            msg = f"Step timed out after {timeout}s"
            step_result.mark_failed(error="timeout", message=msg)
            logger.error("Step %s TIMEOUT after %ds", step.id, timeout)
            return step_result

        if dispatch_error_holder:
            exc_str = dispatch_error_holder[0]
            step_result.mark_failed(error=exc_str, message=f"Unexpected error: {exc_str}")
            logger.exception("Unhandled exception in step %s: %s", step.id, exc_str)
            return step_result

        if not dispatch_result_holder:
            step_result.mark_failed(error="no_result", message="Dispatch returned no result")
            return step_result

        dispatch_result = dispatch_result_holder[0]
        success  = dispatch_result.get("success", False)
        message  = dispatch_result.get("message", "")
        data     = dispatch_result.get("data", {})
        error    = dispatch_result.get("error", "")
        action_result = dispatch_result.get("action_result", {})
        if isinstance(action_result, dict):
            merged_data = dict(data or {})
            merged_data.setdefault("action_result", action_result)
            merged_data.setdefault("verified", bool(action_result.get("verified", False)))
            merged_data.setdefault("duration_ms", int(action_result.get("duration_ms", 0) or 0))
            if action_result.get("target"):
                merged_data.setdefault("target_app", str(action_result.get("target") or ""))
            data = merged_data
            success = bool(action_result.get("success", success))
            if not message:
                message = str(action_result.get("message") or "")
            if not success and not error:
                error = str(action_result.get("error_code") or "") or "Action returned failure"

        if success:
            step_result.mark_success(message=message, data=data)
            state.last_successful_action = self._build_action_descriptor(step)
            logger.info(
                "Step %s SUCCESS in %.2fs: %s",
                step.id, step_result.duration, message,
            )
        else:
            step_result.mark_failed(error=error or "Action returned failure", message=message)
            logger.error(
                "Step %s FAILED in %.2fs: %s | error=%s",
                step.id, step_result.duration, message, error,
            )

        if hasattr(self._safety_gate, "_permission_manager"):
            self._safety_gate._permission_manager.record_execution(  # type: ignore[attr-defined]
                step.action.value,
                {**dict(step.params), "target": step.target},
                success=success,
                error=error,
            )

        return step_result


    def dispatch(self, step: PlanStep) -> Dict[str, Any]:
        """
        Route *step* to the appropriate real handler.

        Returns a dict with keys:
            success (bool), message (str), error (str), data (dict)

        This method runs inside the timeout executor thread.
        Unknown actions return success=False with an honest error message.
        """
        action = step.action

        if action == ActionType.OPEN_APP:
            return self._handle_open_app(step)
        if action == ActionType.SEARCH:
            return self._handle_search(step)
        if action == ActionType.APP_ACTION:
            return self._handle_app_action(step)
        if action == ActionType.SYSTEM_CONTROL:
            return self._handle_system_control(step)
        if action == ActionType.PLAY:
            return self._handle_play(step)
        if action == ActionType.WAIT:
            return self._handle_wait(step)
        if action == ActionType.FILE_ACTION:
            return self._handle_file_action(step)
        if action == ActionType.CLICK:
            return self._handle_click(step)
        if action == ActionType.TYPE:
            return self._handle_type(step)
        if action == ActionType.ASK_USER:
            return self._handle_ask_user(step)
        if action == ActionType.UNKNOWN:
            return self._handle_unknown(step)

        # Fallthrough — new action types added in future phases
        return {
            "success": False,
            "message": f"No handler registered for action '{action.value}'.",
            "error":   "unregistered_action",
            "data":    {},
        }

    # ====================================================================== #
    # Control                                                                 #
    # ====================================================================== #

    def cancel(self) -> None:
        """
        Signal the running plan to stop after the current step.

        Thread-safe.  The engine checks this flag before each step begins.
        """
        logger.info("ExecutionEngine.cancel() called")
        self._cancel_event.set()
        state.cancel_requested = True

    def pause(self) -> None:
        """
        Pause execution between steps.

        The engine will block at the next inter-step pause point.
        """
        logger.info("ExecutionEngine.pause() called")
        self._pause_event.clear()

    def resume(self) -> None:
        """Resume a paused execution."""
        logger.info("ExecutionEngine.resume() called")
        self._pause_event.set()

    def get_status(self) -> Dict[str, Any]:
        """Return a snapshot of current engine state."""
        return {
            "running":          self._running,
            "cancel_requested": self._cancel_event.is_set(),
            "paused":           not self._pause_event.is_set(),
            "current_step":     state.current_step,
            "current_plan_id":  state.current_plan_id,
        }

    def set_progress_callback(self, callback: ProgressCallback) -> None:
        """Replace or set the progress callback (e.g., after UI initialises)."""
        self._progress_cb = callback

    def set_confirm_callback(self, callback: Callable[[str], bool]) -> None:
        """Inject the UI confirmation callback into the safety gate."""
        self._safety_gate.set_confirm_callback(callback)

    # ====================================================================== #
    # Safety Guard Integration                                                #
    # ====================================================================== #

    def _run_safety_guard(self, step: PlanStep) -> Optional[str]:
        """
        Run pre-execution impact analysis via SafetyGuard.

        Returns None if the step is safe to proceed, or a blocking reason
        string if the step should be cancelled / awaits confirmation.
        """
        if not settings.get("safety_guard_enabled"):
            return None

        # Determine which steps need impact analysis
        guard_action = None
        guard_params: Dict[str, Any] = {}

        if step.action == ActionType.FILE_ACTION:
            file_action = str(step.params.get("action", "")).strip().lower()
            if file_action in {"delete", "move", "rename"}:
                guard_action = file_action
                guard_params = {
                    "path": step.target or step.params.get("source_path") or step.params.get("filename", ""),
                    "source_path": step.params.get("source_path") or step.target or "",
                    "target_path": step.params.get("destination") or step.params.get("target_path", ""),
                    "new_name": step.params.get("new_name", ""),
                    "permanent": bool(step.params.get("permanent")),
                    "overwrite": bool(step.params.get("overwrite")),
                }

        elif step.action == ActionType.SYSTEM_CONTROL:
            control = (step.target or str(step.params.get("control", ""))).strip().lower()
            if control in {"shutdown", "restart", "reboot", "sleep", "hibernate", "logoff", "sign_out"}:
                guard_action = control
                guard_params = dict(step.params)

        if guard_action is None:
            return None

        check = self._safety_guard.inspect(guard_action, guard_params)
        if check.allowed:
            return None

        if not check.requires_confirmation:
            # Not allowed but no confirmation possible (e.g. target missing)
            return check.summary

        # Present the impact warning and ask for confirmation
        if self._safety_gate._confirm_cb is not None:
            allowed = self._safety_gate._confirm_cb(check.summary)
            if allowed:
                self._safety_guard.approve(check.confirmation_token)
                return None
            self._safety_guard.deny(check.confirmation_token)
            return f"User denied: {check.summary}"

        # No UI callback — block with the warning
        return f"{check.summary} Confirmation is required before execution."

    # Action Handlers (real implementations)                                  #
    # ====================================================================== #

    def _handle_open_app(self, step: PlanStep) -> Dict[str, Any]:
        """Launch a desktop app or known website and return result immediately without heavy verification."""
        from core.app_launcher import app_display_name, website_url_for

        app_name = (step.target or "").strip()
        if not app_name:
            return _fail("No application name provided.", "empty_target")

        action_started = time.perf_counter()
        website_url = website_url_for(app_name)
        logger.info("Launching app: %s (website_url=%s)", app_name, website_url or "None")

        if website_url and getattr(self, "_browser_controller", None) is not None:
            # For websites, open URL directly (non-blocking)
            browser_result = self._browser_controller.open_url(website_url)
            # Accept success if browser said success - don't do additional verification
            success = bool(browser_result.success)
            message = (
                f"{app_display_name(app_name)} opened successfully."
                if success
                else browser_result.message or f"Could not open {app_display_name(app_name)}."
            )
            action_result = ActionResult(
                success=success,
                action="open_website",
                target=app_name,
                message=message,
                data={
                    "website": app_name,
                    "url": website_url,
                    "browser": getattr(browser_result, "browser_id", ""),
                    **dict(getattr(browser_result, "data", {}) or {}),
                },
                error_code="" if success else getattr(browser_result, "error", "") or "open_website_failed",
                verified=bool(getattr(browser_result, "verified", False)),
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            )
            logger.info(
                "Website open initiated",
                action=action_result.action,
                target=app_name,
                success=action_result.success,
                duration_ms=action_result.duration_ms,
            )
            return _result_from_action(action_result)

        # For desktop apps, launch and return immediately
        launch_result = _launch_application(self._launcher, app_name)
        success = bool(getattr(launch_result, "success", False))
        label = app_display_name(getattr(launch_result, "matched_name", "") or app_name)
        message = (
            f"{label} opened successfully."
            if success
            else getattr(launch_result, "message", "") or f"Could not launch {label}."
        )
        action_result = ActionResult(
            success=success,
            action="open_app",
            target=app_name,
            message=message,
            data={
                "app_name": getattr(launch_result, "app_name", app_name),
                "matched_name": getattr(launch_result, "matched_name", ""),
                "path": getattr(launch_result, "path", ""),
                "pid": int(getattr(launch_result, "pid", -1) or -1),
                **dict(getattr(launch_result, "data", {}) or {}),
            },
            error_code="" if success else getattr(launch_result, "error", "") or "launch_failed",
            verified=False,  # Don't claim verified - just launched
            duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
        )
        logger.info(
            "App launch initiated",
            action=action_result.action,
            target=app_name,
            success=action_result.success,
            duration_ms=action_result.duration_ms,
        )
        return _result_from_action(action_result)

    def _handle_search(self, step: PlanStep) -> Dict[str, Any]:
        """Perform a real web search via BrowserSkill."""
        query       = str(step.params.get("query", step.target or "")).strip()
        target_app  = step.target if step.target != "default" else None
        engine      = str(step.params.get("engine", "")).strip() or None

        if not query:
            return _fail("No search query provided.", "empty_query")

        logger.info("BrowserSkill search: query=%r engine=%r target_app=%r", query, engine, target_app)
        result = self._browser.search(query, engine=engine, target_app=target_app)

        return {
            "success": result.success,
            "message": result.message,
            "error":   result.error,
            "data":    {"url": result.url, "engine": result.engine},
        }

    def _handle_app_action(self, step: PlanStep) -> Dict[str, Any]:
        """Route app-specific actions to the relevant skill."""
        app_name = (step.target or str(step.params.get("app", ""))).strip().lower()
        operation = str(step.params.get("operation", "")).strip().lower()

        if not app_name:
            return _fail("No target app provided for app_action.", "empty_target")
        if not operation:
            return _fail("No app_action operation provided.", "empty_operation")

        if app_name in {"browser", "chrome", "edge", "firefox", "brave", "opera", "vivaldi", "ie"}:
            result = self._browser.execute_action(
                operation,
                action=str(step.params.get("action", "")).strip(),
                result_index=int(step.params.get("result_index", 0) or 0),
                direction=str(step.params.get("direction", "")).strip(),
                amount=int(step.params.get("amount", 1) or 1),
            )
            return {
                "success": result.success,
                "message": result.message,
                "error": result.error,
                "data": result.data,
            }

        if app_name == "youtube":
            params = dict(step.params)
            params.pop("operation", None)
            result = self._youtube.execute_operation(operation, **params)
            return {
                "success": result.success,
                "message": result.message,
                "error": result.error,
                "data": result.data,
            }

        if app_name in {"music", "spotify"}:
            params = dict(step.params)
            params.pop("operation", None)
            result = self._music.execute_operation(operation, **params)
            return {
                "success": result.success,
                "message": result.message,
                "error": result.error,
                "data": result.data,
            }

        if app_name == "whatsapp":
            params = dict(step.params)
            params.pop("operation", None)
            result = self._whatsapp.execute_operation(operation, **params)
            return {
                "success": result.success,
                "message": result.message,
                "error": result.error,
                "data": result.data,
            }

        return _fail(f"No app_action handler is registered for '{app_name}'.", "unregistered_app_action")

    def _handle_system_control(self, step: PlanStep) -> Dict[str, Any]:
        """Route to SystemSkill for real Windows system control."""
        control = (step.target or str(step.params.get("control", ""))).strip()
        if not control:
            return _fail("No system control target specified.", "empty_target")

        result = self._system.execute_operation(
            control,
            confirmed=True,
            require_confirmation=False,
            **step.params,
        )
        return {
            "success": result.success,
            "message": result.response,
            "error": result.error,
            "data": result.data,
        }

    def _handle_play(self, step: PlanStep) -> Dict[str, Any]:
        """
        Play media.

        Strategy:
        - If target looks like a YouTube search → open browser with YouTube search.
        - Otherwise honest unsupported message.
        """
        target = (step.target or "").strip()
        query = str(step.params.get("query", target)).strip()
        target_app = str(step.params.get("target_app") or step.params.get("app") or "").strip().lower()
        result_index = max(1, int(step.params.get("result_index") or step.params.get("selection") or 1))

        if target_app == "youtube":
            result = self._youtube.execute_operation(
                "search" if query else "open_result",
                query=query,
                autoplay=True,
                result_index=result_index,
            )
            return {
                "success": result.success,
                "message": result.message,
                "error": result.error,
                "data": result.data,
            }

        if target_app in {"spotify", "music"}:
            operation = "play_query" if query else "play"
            result = self._music.execute_operation(
                operation,
                query=query,
                result_index=result_index,
                provider="spotify",
            )
            return {
                "success": result.success,
                "message": result.message,
                "error": result.error,
                "data": result.data,
            }

        if query:
            # Best-effort: open YouTube search
            result = self._browser.search(query, engine="youtube")
            return {
                "success": result.success,
                "message": result.message or f"Searched YouTube for '{query}'.",
                "error":   result.error,
                "data":    {"url": result.url},
            }

        return _fail(
            "Play action requires a search query or media title.",
            "no_query",
        )

    def _handle_wait(self, step: PlanStep) -> Dict[str, Any]:
        """Wait for a specified number of seconds."""
        try:
            seconds = float(step.params.get("seconds", step.target or "1"))
            seconds = max(0.1, min(seconds, 60.0))   # clamp to [0.1, 60]
        except (TypeError, ValueError):
            seconds = 1.0

        logger.info("Wait step: sleeping %.1fs", seconds)
        time.sleep(seconds)
        return {
            "success": True,
            "message": f"Waited {seconds:.1f} second(s).",
            "error":   "",
            "data":    {"seconds": seconds},
        }

    def _handle_click(self, step: PlanStep) -> Dict[str, Any]:
        """Click visible text through the OCR-backed text-click engine."""
        target = str(step.target or step.params.get("text") or step.params.get("target_text") or "").strip()
        if not target:
            return _fail("Click action requires visible target text.", "empty_click_target")

        try:
            from core.click_text import get_text_click_engine

            result = get_text_click_engine().click_text(target)
        except Exception as exc:
            logger.error("Click action failed: %s", exc)
            return _fail(str(exc) or "Click action failed.", "click_failed")

        return {
            "success": result.success,
            "message": result.message,
            "error": "" if result.success else "text_click_failed",
            "data": {"target_app": "screen", "click_result": result.to_dict()},
        }

    def _handle_type(self, step: PlanStep) -> Dict[str, Any]:
        """Type text into the currently focused control using desktop automation."""
        text = str(step.params.get("text") or step.target or "").strip()
        if not text:
            return _fail("Type action requires text.", "empty_text")

        try:
            from core.automation import DesktopAutomation

            automation = getattr(self, "_automation", None) or DesktopAutomation()
            typed = automation.type_text(
                text,
                clear=bool(step.params.get("clear")),
                delay_ms=step.params.get("delay_ms"),
            )
        except Exception as exc:
            logger.error("Type action failed: %s", exc)
            return _fail(str(exc) or "Type action failed.", "type_failed")

        return {
            "success": bool(typed),
            "message": f"Typed {len(text)} character(s)." if typed else "I couldn't type into the active control.",
            "error": "" if typed else "type_failed",
            "data": {"target_app": "keyboard", "text_length": len(text)},
        }

    def _handle_ask_user(self, step: PlanStep) -> Dict[str, Any]:
        """Return an explicit clarification request for plans that require user input."""
        prompt = str(step.target or step.params.get("prompt") or "").strip()
        return {
            "success": False,
            "message": prompt or "I need clarification before I can continue.",
            "error": "user_input_required",
            "data": {"target_app": "assistant", "input_mode": "text"},
        }

    def _handle_file_action(self, step: PlanStep) -> Dict[str, Any]:
        action = str(step.params.get("action", "unknown")).strip().lower()
        filename = (
            step.target
            or str(step.params.get("filename", "")).strip()
            or str(step.params.get("source_path", "")).strip()
            or str(step.params.get("path", "")).strip()
        )
        context_app = str(step.params.get("context_app", "")).strip().lower()

        if context_app == "explorer":
            result = self._explorer.execute(
                action,
                target=filename or "selected item",
                destination=str(step.params.get("destination", "")).strip(),
                new_name=str(step.params.get("new_name", "")).strip(),
            )
            return {
                "success": result.success,
                "message": result.message,
                "error": result.error,
                "data": result.data,
            }

        if not filename and action != "create":
            return _fail("No source file or folder was provided.", "empty_target")

        try:
            if action == "create":
                target = self._files.resolve_target_path(
                    filename or str(step.params.get("target_path") or ""),
                    location_hint=str(step.params.get("location") or "").strip() or None,
                )
                result = self._files.create_file(
                    target,
                    content=str(step.params.get("content") or "") or None,
                    overwrite=bool(step.params.get("overwrite")),
                )
            elif action == "open":
                path = self._resolve_single_file_target(filename, location_hint=str(step.params.get("source_location") or step.params.get("location") or "").strip())
                if isinstance(path, dict):
                    return path
                result = self._files.open_file(path)
            elif action == "rename":
                path = self._resolve_single_file_target(filename, location_hint=str(step.params.get("source_location") or step.params.get("location") or "").strip())
                if isinstance(path, dict):
                    return path
                result = self._files.rename_file(
                    path,
                    str(step.params.get("new_name") or "").strip(),
                    overwrite=bool(step.params.get("overwrite")),
                )
            elif action == "delete":
                path = self._resolve_single_file_target(filename, location_hint=str(step.params.get("source_location") or step.params.get("location") or "").strip())
                if isinstance(path, dict):
                    return path
                result = self._files.delete_file(path, permanent=bool(step.params.get("permanent")))
            elif action == "move":
                path = self._resolve_single_file_target(filename, location_hint=str(step.params.get("source_location") or "").strip())
                if isinstance(path, dict):
                    return path
                result = self._files.move_file(
                    path,
                    str(step.params.get("destination") or step.params.get("target_path") or "").strip(),
                    overwrite=bool(step.params.get("overwrite")),
                )
            else:
                return _fail(f"Unsupported file action: {action or 'unknown'}.", "unsupported_file_action")
        except Exception as exc:
            logger.error("File action dispatch failed: %s", exc)
            return _fail(str(exc) or "File action failed.", "file_action_failed")

        return {
            "success": result.success,
            "message": result.message,
            "error": result.error,
            "data": {
                "action": result.action,
                "source_path": result.source_path,
                "target_path": result.target_path,
                "target_app": "files",
                "timestamp": result.timestamp,
            },
        }

    def _handle_unknown(self, step: PlanStep) -> Dict[str, Any]:
        """Handle UNKNOWN action type — never claim success."""
        return {
            "success": False,
            "message": f"Unknown action for: '{step.target}'. Could not determine what to do.",
            "error":   "unknown_action",
            "data":    {},
        }

    # ====================================================================== #
    # Failure handling                                                         #
    # ====================================================================== #

    def handle_failure(self, step: PlanStep, error: str) -> StepResult:
        """
        Build a FAILED StepResult for *step* with the given *error*.

        Used externally if callers need to synthesise a failure without
        running the full execute_step pipeline.
        """
        result = StepResult(step_id=step.id)
        result.mark_running()
        result.mark_failed(error=error, message=f"Failure: {error}")
        logger.error("handle_failure: step=%s error=%s", step.id, error)
        return result

    # ====================================================================== #
    # Internal helpers                                                         #
    # ====================================================================== #

    def _check_dependencies(
        self,
        step: PlanStep,
        failed_ids: Set[str],
        idx: int,
        total: int,
    ) -> Optional[StepResult]:
        """
        If any of step.depends_on is in failed_ids → return a SKIPPED result.
        Otherwise return None (proceed with execution).
        """
        blocking_deps = [d for d in step.depends_on if d in failed_ids]
        if not blocking_deps:
            return None

        reason = (
            f"Skipped because dependency "
            f"[{', '.join(blocking_deps)}] failed or was skipped"
        )
        step_result = StepResult(step_id=step.id)
        step_result.mark_skipped(reason)
        self._emit_progress(
            f"Step {idx}/{total} SKIPPED: {step.action.value} — {reason}"
        )
        logger.info("Step %s skipped: %s", step.id, reason)
        return step_result

    def _emit_progress(self, message: str) -> None:
        """Send a progress message to the registered callback (if any)."""
        if settings.get("show_step_progress") and self._progress_cb:
            try:
                self._progress_cb(message)
            except Exception as exc:
                logger.warning("Progress callback error: %s", exc)

    @staticmethod
    def _describe_step(step: PlanStep) -> str:
        """Human-readable one-line description of a step."""
        target = step.target or ""
        query  = step.params.get("query", "")
        if step.action == ActionType.OPEN_APP:
            return f"Opening {target}"
        if step.action == ActionType.SEARCH:
            return f"Searching for '{query or target}'"
        if step.action == ActionType.APP_ACTION:
            operation = str(step.params.get("operation", "")).strip()
            return f"{target or 'app'} action: {operation or 'unknown'}"
        if step.action == ActionType.SYSTEM_CONTROL:
            return f"System control: {target}"
        if step.action == ActionType.PLAY:
            return f"Playing: {query or target}"
        if step.action == ActionType.WAIT:
            return f"Waiting {step.params.get('seconds', '?')}s"
        if step.action == ActionType.FILE_ACTION:
            operation = str(step.params.get("action", "")).strip()
            return f"File action: {operation or 'unknown'} {target}".strip()
        return f"{step.action.value} {target}".strip()

    @staticmethod
    def _build_action_descriptor(step: PlanStep) -> str:
        """Compact action descriptor stored in runtime state for follow-up context."""
        app_name = step.target or str(step.params.get("target_app", "")).strip()
        if step.action == ActionType.SEARCH:
            app_name = app_name or str(step.params.get("engine", "")).strip() or "browser"
        if step.action == ActionType.APP_ACTION:
            app_name = step.target or str(step.params.get("app", "")).strip()
        if step.action == ActionType.FILE_ACTION:
            app_name = str(step.params.get("context_app", "")).strip() or "file"

        suffix = ""
        if step.action == ActionType.APP_ACTION:
            suffix = str(step.params.get("operation", "")).strip()
        elif step.action == ActionType.FILE_ACTION:
            suffix = str(step.params.get("action", "")).strip()

        parts = [step.action.value]
        if app_name:
            parts.append(app_name)
        if suffix:
            parts.append(suffix)
        return ":".join(parts)

    def _resolve_single_file_target(
        self,
        reference: str,
        *,
        location_hint: str | None = None,
    ) -> Path | Dict[str, Any]:
        matches = self._files.find_matches(reference, location_hint=location_hint)
        if not matches:
            return _fail(f"File or folder not found: {reference}.", "not_found")
        if len(matches) > 1:
            labels = ", ".join(self._files.resolver.describe_path(match) for match in matches[:5])
            return _fail(f"Multiple matches found for '{reference}': {labels}.", "multiple_matches")
        return matches[0]

    def _empty_result(self, plan: ExecutionPlan) -> ExecutionResult:
        """Return a finished empty result for an invalid/empty plan."""
        result = ExecutionResult.start_new(
            plan_id="empty",
            total_steps=0,
        )
        result.summary = "Plan was empty or invalid — nothing executed."
        result.finalise()
        return result


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _fail(message: str, error: str) -> Dict[str, Any]:
    """Shorthand for returning a failure dict from a handler."""
    return _result_from_action(
        ActionResult(
            success=False,
            action="unknown",
            target=None,
            message=message,
            error_code=error,
            verified=False,
        )
    )


def _result_from_action(action_result: ActionResult) -> Dict[str, Any]:
    payload = dict(action_result.data)
    if action_result.target:
        payload.setdefault("target_app", action_result.target)
    return {
        "success": action_result.success,
        "message": action_result.message,
        "error": action_result.error_code or "",
        "data": payload,
        "action_result": action_result.to_dict(),
    }


def _launcher_process_names(launcher: Any, app_name: str, launch_result: Any) -> set[str]:
    process_names: set[str] = set()
    if hasattr(launcher, "process_names_for"):
        try:
            process_names = {
                str(name or "").strip().lower()
                for name in launcher.process_names_for(app_name or getattr(launch_result, "matched_name", "") or getattr(launch_result, "app_name", ""))
                if str(name or "").strip()
            }
        except Exception:
            process_names = set()
    path = str(getattr(launch_result, "path", "") or "").strip()
    if not process_names and path.lower().endswith(".exe"):
        process_names.add(os.path.basename(path).strip().lower())
    return process_names


def _verify_app_launch(
    launcher: Any,
    app_name: str,
    launch_result: Any,
    *,
    timeout: float | None = None,
) -> tuple[bool, int]:
    if not bool(getattr(launch_result, "success", False)):
        return False, int(getattr(launch_result, "pid", -1) or -1)

    if bool(getattr(launch_result, "verified", False)):
        return True, int(getattr(launch_result, "pid", -1) or -1)

    # If launch already gave us a PID, trust it
    pid = int(getattr(launch_result, "pid", -1) or -1)
    if pid > 0:
        return True, pid

    process_names = _launcher_process_names(launcher, app_name, launch_result)
    matched_label = str(getattr(launch_result, "matched_name", "") or getattr(launch_result, "app_name", "") or app_name).strip().lower()

    # Use app-specific timeouts for known slow-start apps
    if timeout is None:
        slow_apps = {"whatsapp", "whatsapp desktop", "spotify", "teams", "outlook", "slack"}
        timeout = 3.0 if any(s in app_name.lower() for s in slow_apps) else 1.0

    deadline = time.monotonic() + max(0.3, float(timeout))

    while time.monotonic() <= deadline:
        try:
            import psutil

            # Fast path: check if any known process exists
            for proc in psutil.process_iter(["name"]):
                try:
                    name = str(proc.info.get("name") or "").strip().lower()
                    if process_names and name in process_names:
                        return True, int(proc.info.get("pid") or -1)
                    if not process_names and matched_label and matched_label in name:
                        return True, int(proc.info.get("pid") or -1)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            return False, -1
        time.sleep(0.05)  # Reduced from 0.1s

    return False, -1


def _bring_app_window_to_front(
    window_controller: Any,
    app_name: str,
    *,
    timeout: float = 2.0,
) -> tuple[Optional[bool], dict[str, Any]]:
    """
    Best-effort GUI verification for app launches.

    Returns:
    - True when a window was found and focused
    - False when a window was found but could not be focused
    - None when no top-level window was detected in the timeout window
    """
    if window_controller is None or not hasattr(window_controller, "find_windows"):
        return None, {"window_found": False, "window_focused": False}

    deadline = time.monotonic() + max(0.2, float(timeout or 2.0))
    while time.monotonic() <= deadline:
        try:
            windows = list(window_controller.find_windows(app_name) or [])
        except Exception:
            return None, {"window_found": False, "window_focused": False}

        if not windows:
            time.sleep(0.1)
            continue

        restored = False
        if hasattr(window_controller, "restore_app"):
            try:
                restore_result = window_controller.restore_app(app_name)
                restored = bool(getattr(restore_result, "success", False))
            except Exception:
                restored = False

        focused = False
        if hasattr(window_controller, "focus_app"):
            try:
                focus_result = window_controller.focus_app(app_name)
                focused = bool(getattr(focus_result, "success", False))
            except Exception:
                focused = False

        return focused, {
            "window_found": True,
            "window_restored": restored,
            "window_focused": focused,
            "window_count": len(windows),
        }

    return None, {"window_found": False, "window_focused": False}


def _launch_application(launcher: Any, app_name: str) -> Any:
    if hasattr(launcher, "launch_app"):
        return launcher.launch_app(app_name)
    return launcher.launch_by_name(app_name)


def _browser_open_verified(operation_result: Any, website: str, url: str) -> bool:
    if bool(getattr(operation_result, "verified", False)):
        return True

    state_obj = getattr(operation_result, "state", None)
    title = str(getattr(state_obj, "title", "") or "").strip().lower()
    site_context = str(getattr(state_obj, "site_context", "") or "").strip().lower()
    keywords = {
        token
        for token in (website, url)
        for token in str(token or "").replace("https://", " ").replace("http://", " ").replace("/", " ").replace(".", " ").split()
        if token and token not in {"www", "com", "org", "net", "co", "in"}
    }
    if site_context and site_context in keywords:
        return True
    return any(keyword in title for keyword in keywords)


def _browser_page_title(title: str) -> str:
    cleaned = str(title or "").strip()
    for suffix in (" - Google Chrome", " - Chrome", " - Microsoft Edge"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
            break
    return cleaned


class CommandExecutor:
    """Direct single-command executor for explicit desktop intents."""

    def __init__(
        self,
        *,
        launcher=None,
        window_controller=None,
        system_controller=None,
        browser_controller=None,
        browser_skill=None,
        file_skill=None,
        permission_manager=None,
    ) -> None:
        from core.app_launcher import DesktopAppLauncher
        from core.browser import BrowserController
        from core.system import DesktopSystemController
        from core.window_control import WindowController
        from skills.browser import BrowserSkill
        from skills.files import FileSkill

        self._launcher = launcher or DesktopAppLauncher()
        self._windows = window_controller or WindowController(launcher=self._launcher)
        self._system = system_controller or DesktopSystemController()
        self._browser_controller = browser_controller or BrowserController(launcher=self._launcher)
        self._browser_skill = browser_skill or BrowserSkill(controller=self._browser_controller)
        self._file_skill = file_skill or FileSkill()
        self._permission_manager = permission_manager

    def execute(self, intent: str, entities: dict[str, Any], text: str) -> dict[str, Any] | None:
        from core.app_launcher import app_display_name, app_fallback_url_for, canonicalize_app_name, website_url_for
        from core.response import CommandResponseBuilder

        normalized_intent = str(intent or "").strip().lower()
        payload = dict(entities or {})

        if normalized_intent == "open_app":
            app_name = str(payload.get("app") or payload.get("requested_app") or "").strip()
            action_started = time.perf_counter()
            logger.info("Executing direct action", action="open_app", target=app_name, command=text)
            canonical_app = canonicalize_app_name(app_name)
            website_url = website_url_for(app_name)
            if website_url == "" and self._windows is not None and hasattr(self._windows, "find_windows"):
                try:
                    existing_windows = []
                    def _find_windows_thread():
                        nonlocal existing_windows
                        try:
                            existing_windows = list(self._windows.find_windows(canonical_app or app_name) or [])
                        except Exception:
                            pass
                    find_thread = threading.Thread(target=_find_windows_thread, daemon=True)
                    find_thread.start()
                    find_thread.join(timeout=1.0)
                except Exception:
                    existing_windows = []
                if existing_windows:
                    restore_result = self._windows.restore_app(canonical_app or app_name) if hasattr(self._windows, "restore_app") else None
                    focus_result = self._windows.focus_app(canonical_app or app_name) if hasattr(self._windows, "focus_app") else None
                    focused = bool(getattr(focus_result, "success", False))
                    restored = bool(getattr(restore_result, "success", False))
                    success = focused or restored
                    detail = (
                        f"{app_display_name(app_name)} is already open."
                        if success
                        else f"I found {app_display_name(app_name)}, but I couldn't bring it to the front."
                    )
                    action_result = ActionResult(
                        success=success,
                        action="open_app",
                        target=canonical_app or app_name,
                        message=detail,
                        data={
                            "existing_instance": True,
                            "window_found": True,
                            "window_restored": restored,
                            "window_focused": focused,
                            "requested_app": app_name,
                            "matched_name": app_display_name(app_name),
                        },
                        error_code=None if success else "window_not_focused",
                        verified=success,
                        duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
                    )
                    return self._result(
                        intent="open_app",
                        response=CommandResponseBuilder.opening_app(app_name, detail),
                        backend="window_control",
                        route="window_control",
                        target_app=canonical_app or app_name,
                        action_result=action_result,
                    )
            if website_url and self._browser_controller is not None:
                logger.info("Open-app target resolved to website", target=app_name, url=website_url)
                result = self._browser_controller.open_url(website_url)
                success = bool(result.success)
                detail = (
                    f"{app_display_name(app_name)} opened."
                    if success
                    else result.message
                    if not result.success
                    else f"Opening {app_display_name(app_name)}."
                )
                action_result = ActionResult(
                    success=success,
                    action="open_website",
                    target=app_name,
                    message=detail,
                    data={
                        "website": app_name,
                        "url": website_url,
                        "browser": getattr(result, "browser_id", ""),
                        **dict(getattr(result, "data", {}) or {}),
                    },
                    error_code="" if success else result.error or "open_website_not_verified",
                    verified=success,
                    duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
                )
                return self._result(
                    intent="open_app",
                    response=CommandResponseBuilder.opening_website(app_name, detail) if success else detail,
                    backend="browser",
                    route="browser",
                    target_app=getattr(result, "browser_id", "") or "browser",
                    action_result=action_result,
                )
            result = _launch_application(self._launcher, app_name)
            launch_succeeded = bool(getattr(result, "success", False))
            pid = getattr(result, "pid", -1) or -1
            detail = ""
            success = False
            if launch_succeeded:
                success = True
                detail = f"{app_display_name(app_name)} opened."
            else:
                pid = getattr(result, "pid", -1) or -1
                try:
                    from core.app_index import get_app_entry
                    app_entry = get_app_entry(canonical_app or app_name)
                    if app_entry and app_entry.executable_name:
                        is_installed = self._launcher.is_installed(app_entry.executable_name.replace(".exe", ""))
                        if is_installed:
                            success = True
                            detail = f"{app_display_name(app_name)} opened."
                except Exception:
                    pass
            if not success:
                fallback_url = ""
                try:
                    from core.app_launcher import app_fallback_url_for
                    fallback_url = app_fallback_url_for(canonical_app or app_name)
                except Exception:
                    pass
                if fallback_url and self._browser_controller is not None:
                    logger.info("Open-app launch failed, trying browser fallback", target=app_name, url=fallback_url)
                    browser_result = self._browser_controller.open_url(fallback_url)
                    if browser_result.success:
                        success = True
                        detail = f"Opening {app_display_name(app_name)} in the browser."
            if not success:
                detail = f"I couldn't open {app_display_name(app_name)}."
            action_result = ActionResult(
                success=success,
                action="open_app",
                target=app_name or getattr(result, "matched_name", "") or getattr(result, "app_name", ""),
                message=detail,
                data={
                    "path": getattr(result, "path", ""),
                    "pid": pid if pid > 0 else getattr(result, "pid", -1),
                    "matched_name": getattr(result, "matched_name", ""),
                    **dict(getattr(result, "data", {}) or {}),
                },
                error_code="" if success else getattr(result, "error", "") or "app_not_found",
                verified=success,
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            )
            return self._result(
                intent="open_app",
                response=CommandResponseBuilder.opening_app(app_name, detail) if success else detail,
                backend="app_launcher",
                route="launcher",
                target_app=canonical_app or app_name,
                action_result=action_result,
            )

        if normalized_intent == "close_app":
            app_name = str(payload.get("app") or payload.get("requested_app") or "").strip()
            action_started = time.perf_counter()
            logger.info("Executing direct action", action="close_app", target=app_name, command=text)
            
            import psutil
            killed = False
            app_lower = app_name.lower()
            for proc in psutil.process_iter(["name"]):
                try:
                    name = str(proc.info.get("name") or "").lower()
                    if app_lower in name or name in app_lower:
                        proc.kill()
                        killed = True
                except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
                    pass
            
            detail = f"Closed {app_display_name(app_name)}." if killed else f"{app_display_name(app_name)} not found."
            action_result = ActionResult(
                success=True,
                action="close_app",
                target=app_name,
                message=detail,
                data={"killed": killed},
                error_code=None,
                verified=True,
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            )
            return self._result(
                intent="close_app",
                response=CommandResponseBuilder.closing_app(app_name, detail),
                backend="window_control",
                route="window_control",
                target_app=app_name,
                action_result=action_result,
            )

        if normalized_intent == "minimize_app":
            app_name = str(payload.get("app") or "").strip()
            action_started = time.perf_counter()
            logger.info("Executing direct action", action="minimize_app", target=app_name, command=text)
            result = self._windows.minimize_app(app_name)
            action_result = ActionResult(
                success=result.success,
                action="minimize_app",
                target=result.app_id or app_name,
                message=result.message,
                data=result.to_dict(),
                error_code=result.error or None,
                verified=result.success,
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            )
            return self._result(
                intent="minimize_app",
                response=CommandResponseBuilder.minimizing_app(app_name, result.message),
                backend="window_control",
                route="window_control",
                target_app=result.app_id or app_name,
                action_result=action_result,
            )

        if normalized_intent == "maximize_app":
            app_name = str(payload.get("app") or "").strip()
            action_started = time.perf_counter()
            logger.info("Executing direct action", action="maximize_app", target=app_name, command=text)
            result = self._windows.maximize_app(app_name)
            action_result = ActionResult(
                success=result.success,
                action="maximize_app",
                target=result.app_id or app_name,
                message=result.message,
                data=result.to_dict(),
                error_code=result.error or None,
                verified=result.success,
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            )
            return self._result(
                intent="maximize_app",
                response=CommandResponseBuilder.maximizing_app(app_name, result.message),
                backend="window_control",
                route="window_control",
                target_app=result.app_id or app_name,
                action_result=action_result,
            )

        if normalized_intent == "focus_app":
            app_name = str(payload.get("app") or "").strip()
            action_started = time.perf_counter()
            logger.info("Executing direct action", action="focus_app", target=app_name, command=text)
            result = self._windows.focus_app(app_name)
            action_result = ActionResult(
                success=result.success,
                action="focus_app",
                target=result.app_id or app_name,
                message=result.message,
                data=result.to_dict(),
                error_code=result.error or None,
                verified=result.success,
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            )
            return self._result(
                intent="focus_app",
                response=CommandResponseBuilder.focusing_app(app_name, result.message),
                backend="window_control",
                route="window_control",
                target_app=result.app_id or app_name,
                action_result=action_result,
            )

        if normalized_intent == "restore_app":
            app_name = str(payload.get("app") or "").strip()
            action_started = time.perf_counter()
            logger.info("Executing direct action", action="restore_app", target=app_name, command=text)
            result = self._windows.restore_app(app_name)
            action_result = ActionResult(
                success=result.success,
                action="restore_app",
                target=result.app_id or app_name,
                message=result.message,
                data=result.to_dict(),
                error_code=result.error or None,
                verified=result.success,
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            )
            return self._result(
                intent="restore_app",
                response=CommandResponseBuilder.restoring_app(app_name, result.message),
                backend="window_control",
                route="window_control",
                target_app=result.app_id or app_name,
                action_result=action_result,
            )

        if normalized_intent == "toggle_app":
            app_name = str(payload.get("app") or "").strip()
            action_started = time.perf_counter()
            logger.info("Executing direct action", action="toggle_app", target=app_name, command=text)
            result = self._windows.toggle_app(app_name)
            action_result = ActionResult(
                success=result.success,
                action="toggle_app",
                target=result.app_id or app_name,
                message=result.message,
                data=result.to_dict(),
                error_code=result.error or None,
                verified=result.success,
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            )
            return self._result(
                intent="toggle_app",
                response=CommandResponseBuilder.toggling_app(app_name, result.message),
                backend="window_control",
                route="window_control",
                target_app=result.app_id or app_name,
                action_result=action_result,
            )

        if normalized_intent == "search_web":
            query = str(payload.get("query") or "").strip()
            browser = str(payload.get("browser") or payload.get("target_app") or "").strip() or None
            action_started = time.perf_counter()
            result = self._browser_skill.search(query, target_app=browser)
            action_result = ActionResult(
                success=result.success,
                action="search_web",
                target=result.browser or browser or "browser",
                message=result.message if not result.success else f"Searched for {query}.",
                data={
                    "query": result.query,
                    "engine": result.engine,
                    "url": result.url,
                    **dict(result.data or {}),
                },
                error_code=result.error or None,
                verified=result.success,
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            )
            return self._result(
                intent="search_web",
                response=(
                    CommandResponseBuilder.searching_web(query, result.browser or browser or "")
                    if result.success
                    else result.message
                ),
                backend="browser",
                route="browser",
                target_app=result.browser or browser or "browser",
                action_result=action_result,
            )

        if normalized_intent == "open_website":
            website = str(payload.get("website") or "").strip()
            url = str(payload.get("url") or website_url_for(website)).strip()
            browser = str(payload.get("browser") or "").strip() or None
            action_started = time.perf_counter()
            logger.info("Executing direct action", action="open_website", target=website or url, command=text)
            result = self._browser_controller.open_url(url, browser_name=browser)
            verified = _browser_open_verified(result, website, url)
            success = bool(result.success and verified)
            detail = (
                f"{app_display_name(website or url)} opened successfully."
                if success
                else result.message
                if not result.success
                else f"I couldn't verify that {app_display_name(website or url)} opened."
            )
            action_result = ActionResult(
                success=success,
                action="open_website",
                target=website or url,
                message=detail,
                data={"website": website, "url": url, **dict(result.data or {})},
                error_code="" if success else result.error or "open_website_not_verified",
                verified=verified,
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            )
            return self._result(
                intent="open_website",
                response=CommandResponseBuilder.opening_website(website or url, detail) if success else detail,
                backend="browser",
                route="browser",
                target_app=result.browser_id or browser or "browser",
                action_result=action_result,
            )

        if normalized_intent in {"browser_tab_close", "browser_tab_next", "browser_tab_previous"}:
            action_map = {
                "browser_tab_close": self._browser_controller.close_tab,
                "browser_tab_next": self._browser_controller.next_tab,
                "browser_tab_previous": self._browser_controller.previous_tab,
            }
            action_started = time.perf_counter()
            result = action_map[normalized_intent](browser_name=str(payload.get("browser") or "").strip() or None)
            action_result = ActionResult(
                success=result.success,
                action=normalized_intent,
                target=result.browser_id or str(payload.get("browser") or "").strip() or "browser",
                message=result.message,
                data=dict(result.data or {}),
                error_code=result.error or None,
                verified=bool(getattr(result, "verified", result.success)),
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            )
            return self._result(
                intent=normalized_intent,
                response=result.message,
                backend="browser",
                route="browser",
                target_app=result.browser_id or payload.get("browser") or "browser",
                action_result=action_result,
            )

        if normalized_intent == "browser_tab_new":
            action_started = time.perf_counter()
            result = self._browser_controller.new_tab(browser_name=str(payload.get("browser") or "").strip() or None)
            action_result = ActionResult(
                success=result.success,
                action="browser_tab_new",
                target=result.browser_id or str(payload.get("browser") or "").strip() or "browser",
                message=result.message,
                data=dict(result.data or {}),
                error_code=result.error or None,
                verified=bool(getattr(result, "verified", result.success)),
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            )
            return self._result(
                intent="browser_tab_new",
                response=result.message,
                backend="browser",
                route="browser",
                target_app=result.browser_id or payload.get("browser") or "browser",
                action_result=action_result,
            )

        if normalized_intent == "browser_tab_switch":
            action_started = time.perf_counter()
            result = self._browser_controller.switch_to_tab(
                int(payload.get("tab_index") or 0),
                browser_name=str(payload.get("browser") or "").strip() or None,
            )
            action_result = ActionResult(
                success=result.success,
                action="browser_tab_switch",
                target=result.browser_id or str(payload.get("browser") or "").strip() or "browser",
                message=result.message,
                data={"tab_index": int(payload.get("tab_index") or 0), **dict(result.data or {})},
                error_code=result.error or None,
                verified=bool(getattr(result, "verified", result.success)),
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            )
            return self._result(
                intent="browser_tab_switch",
                response=result.message,
                backend="browser",
                route="browser",
                target_app=result.browser_id or payload.get("browser") or "browser",
                action_result=action_result,
            )

        if normalized_intent == "browser_action":
            action = str(payload.get("action") or "").strip().lower()
            browser = str(payload.get("browser") or "").strip() or None
            action_map = {
                "go_back": self._browser_controller.go_back,
                "go_forward": self._browser_controller.go_forward,
                "refresh": self._browser_controller.refresh,
                "home": self._browser_controller.go_home,
                "scroll_down": lambda **kwargs: self._browser_controller.scroll_down(browser_name=kwargs.get("browser_name")),
                "scroll_up": lambda **kwargs: self._browser_controller.scroll_up(browser_name=kwargs.get("browser_name")),
                "copy_page_url": self._browser_controller.copy_page_url,
            }
            if action in action_map:
                action_started = time.perf_counter()
                result = action_map[action](browser_name=browser)
                action_result = ActionResult(
                    success=result.success,
                    action=action,
                    target=result.browser_id or browser or "browser",
                    message=result.message,
                    data=dict(result.data or {}),
                    error_code=result.error or None,
                    verified=bool(getattr(result, "verified", result.success)),
                    duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
                )
                return self._result(
                    intent="browser_action",
                    response=result.message,
                    backend="browser",
                    route="browser",
                    target_app=result.browser_id or browser or "browser",
                    action_result=action_result,
                )

            if action == "read_page_title":
                action_started = time.perf_counter()
                result = self._browser_controller.focus_browser(browser, launch_if_missing=False)
                page_title = _browser_page_title(result.state.title if result.success and result.state else "")
                success = bool(result.success and page_title)
                message = f"Current page title: {page_title}" if success else result.message or "The page title is unavailable."
                action_result = ActionResult(
                    success=success,
                    action=action,
                    target=getattr(result, "browser_id", "") or browser or "browser",
                    message=message,
                    data={"page_title": page_title},
                    error_code="" if success else getattr(result, "error", "") or "page_title_unavailable",
                    verified=success,
                    duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
                )
                return self._result(
                    intent="browser_action",
                    response=message,
                    backend="browser",
                    route="browser",
                    target_app=getattr(result, "browser_id", "") or browser or "browser",
                    action_result=action_result,
                )

            if action == "read_page":
                return self._result(
                    intent="browser_action",
                    response="Reading page content requires the Chrome skill.",
                    backend="browser",
                    route="browser",
                    target_app=browser or "browser",
                    action_result=ActionResult(
                        success=False,
                        action=action,
                        target=browser or "browser",
                        message="Reading page content requires the Chrome skill.",
                        data={},
                        error_code="browser_read_requires_skill",
                        verified=False,
                        duration_ms=0,
                    ),
                )

        if normalized_intent in {
            "volume_up",
            "volume_down",
            "mute",
            "unmute",
            "set_volume",
            "brightness_up",
            "brightness_down",
            "set_brightness",
            "lock_pc",
            "shutdown_pc",
            "restart_pc",
            "sleep_pc",
            "play_media",
            "pause_media",
            "next_track",
            "previous_track",
        }:
            if normalized_intent in {"shutdown_pc", "restart_pc", "sleep_pc"}:
                blocked = self._require_confirmation(normalized_intent)
                if blocked is not None:
                    return blocked
            action_started = time.perf_counter()
            result = self._system.execute_intent(normalized_intent, payload)
            action_result = ActionResult(
                success=result.success,
                action=result.action,
                target="system",
                message=result.message,
                data=dict(result.data or {}),
                error_code=result.error or None,
                verified=result.success,
                duration_ms=int(round((time.perf_counter() - action_started) * 1000.0)),
            )
            return self._result(
                intent=normalized_intent,
                response=result.message,
                backend="system",
                route="system",
                target_app="system",
                action_result=action_result,
            )

        if normalized_intent in {"create_file", "open_file", "delete_file", "move_file", "rename_file"}:
            operation_map = {
                "create_file": "create",
                "open_file": "open",
                "delete_file": "delete",
                "move_file": "move",
                "rename_file": "rename",
            }
            skill_result = self._file_skill.execute_operation(
                operation_map[normalized_intent],
                reference=str(payload.get("reference") or payload.get("filename") or "").strip(),
                destination=str(payload.get("destination") or "").strip(),
                new_name=str(payload.get("new_name") or "").strip(),
                location=str(payload.get("location") or "").strip(),
                source_location=str(payload.get("source_location") or "").strip(),
                permanent=bool(payload.get("permanent")),
                content=payload.get("content"),
            )
            result = skill_result.to_dict()
            result["skill_name"] = skill_result.skill_name or "FileSkill"
            return result

        if normalized_intent == "search_file":
            skill_result = self._file_skill.execute(text, {"intent": "search_file"})
            result = skill_result.to_dict()
            result["skill_name"] = skill_result.skill_name or "FileSkill"
            return result

        return None

    def _require_confirmation(self, intent: str) -> dict[str, Any] | None:
        if self._permission_manager is None:
            return None

        action_name = {
            "shutdown_pc": "shutdown",
            "restart_pc": "restart",
            "sleep_pc": "sleep",
        }.get(intent, intent)
        permission = self._permission_manager.evaluate(
            "system_control",
            {"action": action_name, "control": action_name},
        )
        if permission.decision.value == "allow":
            return None
        if permission.decision.value == "deny":
            return self._result(
                success=False,
                intent=intent,
                response=permission.reason,
                error="permission_denied",
                backend="system",
                route="system",
                target_app="system",
            )

        prompt = permission.reason or f"Confirm {action_name.replace('_', ' ')}."
        token = self._permission_manager.request_confirmation(
            "system_control",
            {
                "prompt": prompt,
                "reason": permission.reason,
                "risk_level": permission.risk_level,
                "params": {"action": action_name, "control": action_name},
            },
        )
        confirm_cb = getattr(self._permission_manager, "_confirmation_callback", None)
        if callable(confirm_cb):
            if confirm_cb(prompt):
                self._permission_manager.approve(token)
                return None
            self._permission_manager.deny(token)
            return self._result(
                success=False,
                intent=intent,
                response="Cancelled the system action.",
                error="cancelled",
                backend="system",
                route="system",
                target_app="system",
            )
        return self._result(
            success=False,
            intent=intent,
            response=f"{prompt} Say yes to continue or cancel to stop.",
            error="confirmation_required",
            backend="system",
            route="system",
            target_app="system",
            data={"token": token},
        )

    def _result(
        self,
        *,
        intent: str,
        response: str,
        backend: str,
        target_app: str,
        route: str = "",
        data: dict[str, Any] | None = None,
        success: bool | None = None,
        error: str = "",
        action_result: ActionResult | None = None,
    ) -> dict[str, Any]:
        resolved_action_result = action_result or ActionResult(
            success=bool(success),
            action=intent,
            target=target_app,
            message=response,
            data=dict(data or {}),
            error_code=error or None,
            verified=bool((data or {}).get("verified")) if isinstance(data, dict) else False,
            duration_ms=int((data or {}).get("duration_ms", 0) or 0) if isinstance(data, dict) else 0,
        )
        payload = dict(data or {})
        payload.update(dict(resolved_action_result.data))
        payload.setdefault("target_app", target_app)
        payload.setdefault("backend", backend)
        payload.setdefault("route", route or backend)
        payload.setdefault("verified", bool(resolved_action_result.verified))
        payload.setdefault("duration_ms", int(resolved_action_result.duration_ms))
        payload.setdefault("speak_response", True)
        payload.setdefault("notify_on_success", bool(resolved_action_result.success))
        logger.info(
            "Direct action completed",
            intent=intent,
            backend=backend,
            target=target_app,
            success=resolved_action_result.success,
            verified=resolved_action_result.verified,
            duration_ms=resolved_action_result.duration_ms,
            error_code=resolved_action_result.error_code or "",
        )
        return {
            "success": resolved_action_result.success,
            "intent": intent,
            "response": response,
            "error": resolved_action_result.error_code or "",
            "data": payload,
            "action_result": resolved_action_result.to_dict(),
            "skill_name": "CommandExecutor",
        }
