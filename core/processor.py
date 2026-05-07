"""
Main command processing engine with rules-first, context-aware routing.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from core.alerts import notify_error, notify_success, notify_warning
from core.action_results import ensure_action_result
from core import parser, responses, settings, state
from core.response_service import get_response_service
from core.command_results import ensure_command_result, infer_command_category
from core.context import context_manager as _ctx_mgr
from core.context_engine import context_engine as _context_engine
from core.context_models import ContextDecision, NULL_DECISION
from core.conversation_memory import TrackedEntity, EntityType, conversation_memory as _cm
from core.resolver import reference_resolver as _resolver
from core.correction import get_corrector
from core.execution_models import ExecutionResult
from core.executor import CommandExecutor, ExecutionEngine
from core.errors import ActionTimeoutError
from core.metrics import metrics
from core.analytics import analytics
from core.identity import IdentityManager
from core.intent import IntentDetector, IntentResult, IntentType
from core.launcher import AppLauncher
from core.app_launcher import DesktopAppLauncher

from core.logger import correlation_context, get_logger, new_correlation_id
from core.normalizer import normalize_command, normalize_command_result
from core.nlu import resolve_context_entities
from core.plan_models import ExecutionPlan, PlannerContext
from core.planner import ActionPlanner
from core.permissions import Decision, permission_manager as default_permission_manager
from core.recovery import RecoveryManager
from core.response_models import ResponseCategory, ResponseSeverity
from core.runtime_tasks import invoke_with_timeout
from core.skills_manager import SkillsManager
from core.notifications import NotificationManager
from core.schemas import IntentSchema, PlanSchema, PlanStep
from core.wake import detect_wake_word, strip_wake_word
from core.router import route_command
from core.apps_registry import canonicalize_app_name
from core.nlu_router import (
    IntentType as MediaIntentType,
    MediaTarget,
    NLUIntent,
    get_nlu_router,
)
from skills.browser import BrowserSkill
from skills.files import FileSkill

logger = get_logger(__name__)


class CommandProcessor:
    RULE_CONFIDENCE_THRESHOLD = 0.50
    STT_CORRECTION_CONFIDENCE_THRESHOLD = 0.9
    SPEECH_NOISE_TOKENS = {"an", "n", "uh", "umm", "pley", "plae", "serch", "opan"}

    def __init__(
        self,
        identity_mgr: IdentityManager | None = None,
        detector: IntentDetector | None = None,
        launcher: AppLauncher | None = None,
        browser_controller=None,
        skills_manager: SkillsManager | None = None,
        progress_callback: Optional[Callable[[str], None]] = None,
        notification_manager: NotificationManager | None = None,
        permission_manager=None,
    ) -> None:
        self.identity_mgr = identity_mgr or IdentityManager()
        self.detector = detector or IntentDetector()
        self.launcher = launcher or AppLauncher()
        self.desktop_launcher = DesktopAppLauncher(self.launcher)
        self.permission_manager = permission_manager or default_permission_manager
        self.skills_manager = skills_manager or SkillsManager(
            browser_controller=browser_controller,
            launcher=self.desktop_launcher,
            permission_manager=self.permission_manager,
        )
        self.skills_manager.load_builtin_skills()
        self.browser_skill = BrowserSkill(controller=self.skills_manager.browser_controller)
        self.corrector = get_corrector()
        self.planner = ActionPlanner()
        self.engine = ExecutionEngine(
            launcher=self.desktop_launcher,
            progress_callback=progress_callback,
            permission_manager=self.permission_manager,
        )
        self.file_skill = self.skills_manager.file_skill if hasattr(self.skills_manager, "file_skill") else None
        self.clipboard_skill = self.skills_manager.clipboard_skill if hasattr(self.skills_manager, "clipboard_skill") else None
        self.reminder_skill = self.skills_manager.reminder_skill if hasattr(self.skills_manager, "reminder_skill") else None
        if self.file_skill is None:
            self.file_skill = FileSkill()
        if self.file_skill is not None and hasattr(self.file_skill, "ensure_index_ready_async"):
            try:
                if settings.get("smart_file_search_enabled") and settings.get("use_file_index") and settings.get(
                    "index_update_on_startup"
                ):
                    self.file_skill.ensure_index_ready_async()
            except Exception as exc:
                logger.warning("File index startup warmup failed: %s", exc)
        self.notification_manager = notification_manager
        self.context = _ctx_mgr
        self.context_engine = _context_engine
        self.recovery = RecoveryManager(action_executor=self._execute_recovery_action)
        self.command_executor = CommandExecutor(
            launcher=self.desktop_launcher,
            browser_controller=self.skills_manager.browser_controller,
            browser_skill=self.browser_skill,
            file_skill=self.file_skill,
            permission_manager=self.permission_manager,
        )
        self.set_notification_manager(notification_manager)
        
        self.response_service = get_response_service()
        self.nlu_router = get_nlu_router()
        
        metrics.init()
        analytics.init()
        
        logger.info("CommandProcessor initialized")

    def set_progress_callback(self, callback: Callable[[str], None]) -> None:
        self.engine.set_progress_callback(callback)

    def set_confirm_callback(self, callback: Callable[[str], bool]) -> None:
        self.permission_manager.set_confirmation_callback(callback)
        self.engine.set_confirm_callback(callback)
        if hasattr(self.skills_manager, "set_confirm_callback"):
            self.skills_manager.set_confirm_callback(callback)
        if self.file_skill is not None and hasattr(self.file_skill, "set_confirm_callback"):
            self.file_skill.set_confirm_callback(callback)
        if self.clipboard_skill is not None and hasattr(self.clipboard_skill, "set_confirm_callback"):
            self.clipboard_skill.set_confirm_callback(callback)

    def set_tts_engine(self, tts_engine) -> None:
        if self.reminder_skill is not None and hasattr(self.reminder_skill, "set_tts_engine"):
            self.reminder_skill.set_tts_engine(tts_engine)

    def set_notification_manager(self, notification_manager: NotificationManager | None) -> None:
        self.notification_manager = notification_manager
        if self.reminder_skill is not None and hasattr(self.reminder_skill, "set_notification_manager"):
            self.reminder_skill.set_notification_manager(notification_manager)

    def set_reminder_callback(self, callback) -> None:
        if self.reminder_skill is not None and hasattr(self.reminder_skill, "set_event_callback"):
            self.reminder_skill.set_event_callback(callback)

    def start_background_services(self) -> None:
        if self.clipboard_skill is not None and hasattr(self.clipboard_skill, "start"):
            try:
                self.clipboard_skill.start()
            except Exception as exc:
                logger.warning("Clipboard watcher startup failed: %s", exc)
        if self.reminder_skill is not None and hasattr(self.reminder_skill, "start"):
            try:
                self.reminder_skill.start()
            except Exception as exc:
                logger.warning("Reminder scheduler startup failed: %s", exc)

    def shutdown(self) -> None:
        if self.reminder_skill is not None and hasattr(self.reminder_skill, "shutdown"):
            try:
                self.reminder_skill.shutdown()
            except Exception as exc:
                logger.warning("Reminder scheduler shutdown failed: %s", exc)
        if self.clipboard_skill is not None and hasattr(self.clipboard_skill, "shutdown"):
            try:
                self.clipboard_skill.shutdown()
            except Exception as exc:
                logger.warning("Clipboard watcher shutdown failed: %s", exc)
        plugin_manager = getattr(self.skills_manager, "plugin_manager", None)
        if plugin_manager is not None and hasattr(plugin_manager, "shutdown_all"):
            try:
                plugin_manager.shutdown_all()
            except Exception as exc:
                logger.warning("Plugin shutdown failed: %s", exc)
        try:
            metrics.shutdown()
        except Exception as exc:
            logger.warning("Metrics shutdown failed: %s", exc)
        try:
            analytics.shutdown()
        except Exception as exc:
            logger.warning("Analytics shutdown failed: %s", exc)

    def process(self, text: str, source: str = "text") -> dict[str, Any]:
        """Main entry point for evaluating text commands."""
        correlation_id = new_correlation_id()
        raw_input = text
        cleaned = ""
        normalized_input = ""
        detected_intent = ""
        context_decision = NULL_DECISION

        with correlation_context(correlation_id):
            timer = metrics.start_timer("request_latency", source=source)
            # State machine transition: READY -> PROCESSING (Section 16 of spec)
            state.set_state("PROCESSING", reason="command_received")
            state.is_processing = True
            metrics.record_counter("requests_total", source=source)

            try:
                if parser.is_empty(text):
                    state.set_state("READY", reason="empty_command")
                    result = self._build_result(False, "empty", "Empty command.")
                    return self._finalize_processed_result(
                        result,
                        raw_input=raw_input,
                        normalized_input="",
                        detected_intent="empty",
                        source=source,
                        context_decision=NULL_DECISION,
                        timer=timer,
                    )

                cleaned = parser.clean_text(text)
                normalized_input = cleaned
                state.command_count += 1
                logger.info("Command received", raw_input=cleaned, source=source)

                if settings.get("context_detection_enabled"):
                    try:
                        with metrics.measure("context_refresh", source=source):
                            self.context.refresh()
                        logger.debug(
                            "Context at command time",
                            app=state.current_context or state.current_app,
                            title=state.current_window_title[:80],
                        )
                    except Exception as exc:
                        logger.warning("Context refresh failed: %s", exc)

                # WAKE WORD ENGINE
                if detect_wake_word(cleaned):
                    stripped = strip_wake_word(cleaned)
                    if not stripped:
                        if self._is_greeting_wake_phrase(cleaned):
                            result = self.handle_greeting()
                        else:
                            result = self._build_result(True, IntentType.GREETING.value, "Yes? How can I help?")
                        return self._finalize_processed_result(
                            result,
                            raw_input=raw_input,
                            normalized_input=cleaned,
                            detected_intent=IntentType.GREETING.value,
                            source=source,
                            context_decision=context_decision,
                            timer=timer,
                        )
                    working_text = stripped
                else:
                    working_text = cleaned
                    
                normalized_input = working_text

                lower_cmd = working_text.lower()
                if lower_cmd == "show logs":
                    return self._finalize_processed_result(
                        self.handle_show_logs(),
                        raw_input=raw_input,
                        normalized_input=working_text,
                        detected_intent="analytics_logs",
                        source=source,
                        context_decision=context_decision,
                        timer=timer,
                    )
                if lower_cmd == "show errors":
                    return self._finalize_processed_result(
                        self.handle_show_errors(),
                        raw_input=raw_input,
                        normalized_input=working_text,
                        detected_intent="analytics_errors",
                        source=source,
                        context_decision=context_decision,
                        timer=timer,
                    )
                if lower_cmd == "performance stats":
                    return self._finalize_processed_result(
                        self.handle_performance_stats(),
                        raw_input=raw_input,
                        normalized_input=working_text,
                        detected_intent="analytics_stats",
                        source=source,
                        context_decision=context_decision,
                        timer=timer,
                    )
                plugin_command = self._parse_plugin_management_command(working_text)
                if plugin_command is not None:
                    action, plugin_name = plugin_command
                    return self._finalize_processed_result(
                        self.handle_plugin_command(action, plugin_name),
                        raw_input=raw_input,
                        normalized_input=working_text,
                        detected_intent=f"plugin_{action}",
                        source=source,
                        context_decision=context_decision,
                        timer=timer,
                    )

                working_text = self._apply_stt_correction_if_needed(working_text, source)
                normalization = normalize_command_result(working_text)
                normalized_input = normalization.normalized_text or working_text
                working_text = normalized_input or working_text

                if normalization.requires_confirmation and normalization.confirmation_prompt:
                    return self._finalize_processed_result(
                        self._build_result(False, "clarification", normalization.confirmation_prompt),
                        raw_input=raw_input,
                        normalized_input=normalized_input,
                        detected_intent="clarification",
                        source=source,
                        context_decision=context_decision,
                        timer=timer,
                    )

                pending_result = self.permission_manager.handle_confirmation_reply(working_text)
                if pending_result is not None:
                    coerced = self._coerce_result(pending_result)
                    return self._finalize_processed_result(
                        coerced,
                        raw_input=raw_input,
                        normalized_input=working_text,
                        detected_intent=str(coerced.get("intent", "permission_confirmation")),
                        source=source,
                        context_decision=context_decision,
                        timer=timer,
                    )

                recovery_reply = self.recovery.consume_reply(working_text)
                if recovery_reply is not None:
                    coerced = self._coerce_result(recovery_reply)
                    return self._finalize_processed_result(
                        coerced,
                        raw_input=raw_input,
                        normalized_input=working_text,
                        detected_intent=str(coerced.get("intent", "recovery")),
                        source=source,
                        context_decision=context_decision,
                        timer=timer,
                    )

                if settings.get("conversation_memory_enabled"):
                    with metrics.measure("reference_resolution", source=source):
                        res = _resolver.resolve_all(working_text)
                    if res.needs_clarification:
                        if not self._can_resolve_follow_up_locally(working_text):
                            return self._finalize_processed_result(
                                self._build_result(False, "clarification", res.message),
                                raw_input=raw_input,
                                normalized_input=working_text,
                                detected_intent="clarification",
                                source=source,
                                context_decision=context_decision,
                                timer=timer,
                            )
                    if res.resolved:
                        working_text = res.enriched_text
                        normalized_input = normalize_command_result(working_text).normalized_text or working_text

                nlu_media_result = self._try_nlu_media_execution(working_text)
                if nlu_media_result is not None:
                    return self._finalize_processed_result(
                        nlu_media_result,
                        raw_input=raw_input,
                        normalized_input=normalized_input or working_text,
                        detected_intent=str(nlu_media_result.get("detected_intent") or "media_nlu"),
                        source=source,
                        context_decision=NULL_DECISION,
                        timer=timer,
                    )

                music_query_result = self._try_music_query_execution(working_text)
                if music_query_result is not None:
                    return self._finalize_processed_result(
                        music_query_result,
                        raw_input=raw_input,
                        normalized_input=normalized_input or working_text,
                        detected_intent=str(music_query_result.get("detected_intent") or "play_media"),
                        source=source,
                        context_decision=NULL_DECISION,
                        timer=timer,
                    )

                state.last_input_text = working_text
                with metrics.measure("intent_detection", source=source, pipeline="rules"):
                    rule_result = self.detector.detect(cleaned, working_text, context=self._runtime_context_snapshot())
                working_text = rule_result.cleaned_text or working_text
                normalized_input = rule_result.cleaned_text or normalized_input or working_text
                detected_intent = rule_result.intent.value
                logger.info("Command normalized", raw_input=raw_input, normalized_input=normalized_input, source=source)
                rule_result.entities = resolve_context_entities(
                    rule_result.intent.value,
                    rule_result.entities,
                    rule_result.cleaned_text,
                    context=self._runtime_context_snapshot(),
                )
                logger.info(
                    "Command classified",
                    normalized_input=normalized_input,
                    intent=detected_intent,
                    entities=rule_result.entities,
                )
                self._prime_runtime_state(rule_result)

                if rule_result.requires_confirmation and rule_result.intent == IntentType.UNKNOWN:
                    clarification_prompt = str(rule_result.entities.get("clarification_prompt") or rule_result.decision_reason or "")
                    if clarification_prompt:
                        return self._finalize_processed_result(
                            self._build_result(False, "clarification", clarification_prompt),
                            raw_input=raw_input,
                            normalized_input=working_text,
                            detected_intent=detected_intent,
                            source=source,
                            context_decision=context_decision,
                            timer=timer,
                        )

                # FAST-PATH: For simple explicit commands, skip context engine overhead
                fast_path_result = self._try_fast_path_execution(rule_result, working_text)
                if fast_path_result is not None:
                    return self._finalize_processed_result(
                        fast_path_result,
                        raw_input=raw_input,
                        normalized_input=working_text,
                        detected_intent=detected_intent,
                        source=source,
                        context_decision=NULL_DECISION,
                        timer=timer,
                    )

                context_decision = self._resolve_context_decision(working_text)
                skill_result = self._try_skill_execution(working_text, rule_result, context_decision)
                if skill_result is not None:
                    return self._finalize_processed_result(
                        skill_result,
                        raw_input=raw_input,
                        normalized_input=working_text,
                        detected_intent=detected_intent,
                        source=source,
                        context_decision=context_decision,
                        timer=timer,
                    )

                context_result = self._handle_context_decision(context_decision, working_text)
                if context_result is not None:
                    return self._finalize_processed_result(
                        context_result,
                        raw_input=raw_input,
                        normalized_input=working_text,
                        detected_intent=detected_intent,
                        source=source,
                        context_decision=context_decision,
                        timer=timer,
                    )

                if rule_result.intent == IntentType.MULTI_ACTION:
                    plan = self._generate_action_plan(working_text)
                    if plan and plan.steps:
                        return self._finalize_processed_result(
                            self._execute_action_plan(plan),
                            raw_input=raw_input,
                            normalized_input=working_text,
                            detected_intent=detected_intent,
                            source=source,
                            context_decision=context_decision,
                            timer=timer,
                        )
                    return self._finalize_processed_result(
                        self._build_result(False, "multi_action_error", "Could not process multi-action command."),
                        raw_input=raw_input,
                        normalized_input=working_text,
                        detected_intent=detected_intent,
                        source=source,
                        context_decision=context_decision,
                        timer=timer,
                    )

                if self._should_use_planner(rule_result, working_text):
                    plan = self._generate_action_plan(working_text)
                    if plan:
                        return self._finalize_processed_result(
                            self._execute_action_plan(plan),
                            raw_input=raw_input,
                            normalized_input=working_text,
                            detected_intent=detected_intent,
                            source=source,
                            context_decision=context_decision,
                            timer=timer,
                        )

                result = self._route_rule_result(rule_result, working_text)
                return self._finalize_processed_result(
                    result,
                    raw_input=raw_input,
                    normalized_input=working_text,
                    detected_intent=detected_intent,
                    source=source,
                    context_decision=context_decision,
                    timer=timer,
                )

            except ActionTimeoutError as exc:
                logger.warning(
                    "Command timed out",
                    exc=exc,
                    raw_input=raw_input,
                    normalized_input=normalized_input or cleaned,
                    detected_intent=detected_intent,
                    source=source,
                )
                result = self._build_result(
                    False,
                    detected_intent or "error",
                    str(exc),
                    error=exc.code or "action_timeout",
                    data=dict(exc.context or {}),
                )
                return self._finalize_processed_result(
                    result,
                    raw_input=raw_input,
                    normalized_input=normalized_input or cleaned or raw_input,
                    detected_intent=detected_intent or "error",
                    source=source,
                    context_decision=context_decision,
                    timer=timer,
                )
            except Exception as exc:
                logger.exception(
                    "Command processing failed",
                    exc=exc,
                    raw_input=raw_input,
                    normalized_input=normalized_input or cleaned,
                    detected_intent=detected_intent,
                    source=source,
                )
                analytics.record_error(
                    "processor_error",
                    str(exc),
                    exc=exc,
                    module=__name__,
                    context=normalized_input or cleaned or raw_input,
                    command_context=self._serialize_command_context(
                        raw_input=raw_input,
                        normalized_input=normalized_input or cleaned or raw_input,
                        detected_intent=detected_intent,
                        source=source,
                    ),
                    source=source,
                    metadata={"correlation_id": correlation_id},
                )
                result = self._build_result(
                    False,
                    "error",
                    responses.error_response(),
                    error="exception",
                    data={"message": str(exc), "exception_type": type(exc).__name__},
                )
                return self._finalize_processed_result(
                    result,
                    raw_input=raw_input,
                    normalized_input=normalized_input or cleaned or raw_input,
                    detected_intent=detected_intent or "error",
                    source=source,
                    context_decision=context_decision,
                    timer=timer,
                )
            finally:
                state.is_processing = False

    def handle_greeting(self) -> dict[str, Any]:
        user_name = self.identity_mgr.get_user_name()
        return self._build_result(True, IntentType.GREETING.value, responses.greeting_response(user_name))

    def handle_show_logs(self) -> dict[str, Any]:
        commands = analytics.recent_commands(5)
        if not commands:
            return self._build_result(True, "analytics_logs", "No recent logs found.")
        lines = ["Recent commands:"]
        for command in commands:
            status = "ok" if command.get("success") else "failed"
            display = command.get("normalized_input") or command.get("raw_input") or "(empty)"
            skill = command.get("selected_skill") or "rule_router"
            intent = command.get("intent") or "unknown"
            latency = int(round(float(command.get("latency_ms") or 0.0)))
            lines.append(f"- [{status}] {display} -> {intent} via {skill} ({latency} ms)")
        return self._build_result(True, "analytics_logs", "\n".join(lines))

    def handle_show_errors(self) -> dict[str, Any]:
        errors = analytics.recent_errors(5)
        if not errors:
            return self._build_result(True, "analytics_errors", "No recent errors found.")
        lines = ["Recent errors:"]
        for error in errors:
            module = error.get("module") or "unknown_module"
            lines.append(f"- {error['error_type']} in {module}: {error['message']}")
        return self._build_result(True, "analytics_errors", "\n".join(lines))

    def handle_performance_stats(self) -> dict[str, Any]:
        stats = analytics.stats()
        slowest = analytics.slowest_actions(1)
        slowest_text = "None"
        if slowest:
            slowest_text = f"{slowest[0]['name']} ({round(float(slowest[0].get('duration_ms') or 0.0), 1)} ms)"
        text = (
            f"Average response time today: {stats.get('avg_latency_ms', 0)} ms\n"
            f"Most used skill: {stats.get('top_skill') or 'None'}\n"
            f"Total commands today: {stats.get('total_commands', 0)}\n"
            f"Success rate today: {stats.get('success_rate', 0) * 100:.1f}%\n"
            f"Total errors today: {stats.get('total_errors', 0)}\n"
            f"Slowest recent action: {slowest_text}"
        )
        return self._build_result(True, "analytics_stats", text)

    def handle_plugin_command(self, action: str, plugin_name: str = "") -> dict[str, Any]:
        manager = getattr(self.skills_manager, "plugin_manager", None)
        if manager is None or not settings.get("plugins_enabled"):
            return self._build_result(False, f"plugin_{action}", "Plugins are disabled.", error="plugins_disabled")

        if action == "list":
            plugins = manager.list_plugins()
            if not plugins:
                detail = ""
                if getattr(manager, "discovery_errors", None):
                    detail = f" Discovery errors: {len(manager.discovery_errors)}."
                return self._build_result(True, "plugin_list", f"No plugins installed.{detail}".strip())
            lines = ["Installed plugins:"]
            for idx, info in enumerate(plugins, start=1):
                status = "enabled" if info.enabled else "disabled"
                health = "healthy" if info.healthy else "unhealthy"
                loaded = "loaded" if info.loaded else "not loaded"
                permissions = ", ".join(info.permissions_requested) if info.permissions_requested else "none"
                suffix = f" - {info.error}" if info.error else ""
                lines.append(f"{idx}. {info.name} v{info.version} ({status}, {health}, {loaded}; permissions: {permissions}){suffix}")
            return self._build_result(True, "plugin_list", "\n".join(lines), data={"target_app": "plugins"})

        if not plugin_name:
            return self._build_result(False, f"plugin_{action}", "Specify a plugin name.", error="missing_plugin_name")

        if action == "enable":
            info = manager.enable(plugin_name)
            if info is None:
                return self._build_result(False, "plugin_enable", f"Plugin not found: {plugin_name}", error="plugin_not_found")
            status = "loaded" if info.loaded else "enabled"
            if info.error:
                status = f"enabled but not loaded: {info.error}"
            return self._build_result(True, "plugin_enable", f"{info.name} is {status}.", data={"target_app": "plugins", "plugin_id": info.id})

        if action == "disable":
            info = manager.disable(plugin_name)
            if info is None:
                return self._build_result(False, "plugin_disable", f"Plugin not found: {plugin_name}", error="plugin_not_found")
            return self._build_result(True, "plugin_disable", f"{info.name} is disabled.", data={"target_app": "plugins", "plugin_id": info.id})

        if action == "reload":
            info = manager.reload(plugin_name)
            if info is None:
                return self._build_result(False, "plugin_reload", f"Plugin not found: {plugin_name}", error="plugin_not_found")
            if not info.loaded:
                return self._build_result(False, "plugin_reload", f"{info.name} did not reload: {info.error or 'not loaded'}", error="plugin_reload_failed", data={"target_app": "plugins", "plugin_id": info.id})
            return self._build_result(True, "plugin_reload", f"{info.name} reloaded.", data={"target_app": "plugins", "plugin_id": info.id})

        if action == "health":
            checks = manager.health_check_all()
            lines = ["Plugin health:"]
            for check in checks:
                name = check.get("name") or check.get("id")
                health = check.get("health") if isinstance(check.get("health"), dict) else {}
                status = "healthy" if health.get("ok") else str(health.get("status") or health.get("error") or "unhealthy")
                lines.append(f"- {name}: {status}")
            return self._build_result(True, "plugin_health", "\n".join(lines), data={"target_app": "plugins"})

        return self._build_result(False, f"plugin_{action}", f"Unknown plugin command: {action}", error="unknown_plugin_command")

    def _try_nlu_media_execution(self, text: str) -> dict[str, Any] | None:
        nlu_result = self.nlu_router.route(
            text,
            context_app=state.current_context or state.current_app,
            window_title=state.current_window_title,
            last_action=state.last_successful_action,
            recent_commands=state.recent_commands,
        )
        if nlu_result.intent == MediaIntentType.UNKNOWN or nlu_result.confidence < 0.50:
            return None

        logger.info(
            "NLU media route selected",
            normalized_input=nlu_result.normalized_text,
            intent=nlu_result.intent.value,
            confidence=nlu_result.confidence,
            target=nlu_result.target.value if nlu_result.target else "",
            entities=nlu_result.entities,
        )
        state.last_entities = dict(nlu_result.entities)
        result = self._execute_nlu_media_intent(text, nlu_result)
        if result is not None:
            result["detected_intent"] = nlu_result.intent.value
        return result

    def _execute_nlu_media_intent(self, raw_text: str, nlu_result: NLUIntent) -> dict[str, Any] | None:
        target = self._resolve_nlu_media_target(nlu_result)

        if target == MediaTarget.YOUTUBE.value:
            skill = self.skills_manager.get_skill("YouTubeSkill")
            if skill is not None:
                method_name = {
                    MediaIntentType.MEDIA_RESUME: "resume",
                    MediaIntentType.MEDIA_PAUSE: "pause",
                    MediaIntentType.MEDIA_NEXT: "next_video",
                    MediaIntentType.MEDIA_PREVIOUS: "previous_video",
                    MediaIntentType.MEDIA_MUTE: "mute",
                    MediaIntentType.MEDIA_UNMUTE: "unmute",
                    MediaIntentType.MEDIA_STOP: "pause",
                }.get(nlu_result.intent)
                if method_name and hasattr(skill, method_name):
                    skill_result = getattr(skill, method_name)()
                    return self._normalize_nlu_result(
                        skill_result.to_dict(),
                        intent=nlu_result.intent.value,
                        target_app=target,
                        response_text=self._nlu_response_text(nlu_result.intent, target),
                    )
            routed = self._execute_media_skill_route(raw_text, target_route="youtube", intent_name=IntentType.PLAY_MEDIA.value)
            if routed is not None:
                return self._normalize_nlu_result(
                    routed,
                    intent=nlu_result.intent.value,
                    target_app=target,
                    response_text=str(routed.get("response") or self._nlu_response_text(nlu_result.intent, target)),
                )

        if target == MediaTarget.SPOTIFY.value:
            skill = self.skills_manager.get_skill("MusicSkill")
            if skill is not None and hasattr(skill, "execute_operation"):
                operation = {
                    MediaIntentType.MEDIA_RESUME: "play",
                    MediaIntentType.MEDIA_PAUSE: "pause",
                    MediaIntentType.MEDIA_NEXT: "next_track",
                    MediaIntentType.MEDIA_PREVIOUS: "previous_track",
                    MediaIntentType.MEDIA_STOP: "pause",
                }.get(nlu_result.intent)
                if operation:
                    action_result = skill.execute_operation(operation, provider="spotify")
                    return self._build_result(
                        action_result.success,
                        "music_action" if operation in {"play", "pause"} else f"music_{operation}",
                        self._nlu_response_text(nlu_result.intent, target),
                        error=action_result.error,
                        data={
                            "target_app": target,
                            "action": operation,
                            "verified": action_result.success,
                            "route": "spotify",
                            "backend": "MusicSkill",
                            **dict(action_result.data or {}),
                        },
                    )
            routed = self._execute_media_skill_route(raw_text, target_route="spotify", intent_name=IntentType.PLAY_MEDIA.value)
            if routed is not None:
                return self._normalize_nlu_result(
                    routed,
                    intent=nlu_result.intent.value,
                    target_app=target,
                    response_text=str(routed.get("response") or self._nlu_response_text(nlu_result.intent, target)),
                )

        direct_intent = {
            MediaIntentType.MEDIA_RESUME: IntentType.PLAY_MEDIA.value,
            MediaIntentType.MEDIA_PAUSE: IntentType.PAUSE_MEDIA.value,
            MediaIntentType.MEDIA_NEXT: IntentType.NEXT_TRACK.value,
            MediaIntentType.MEDIA_PREVIOUS: IntentType.PREVIOUS_TRACK.value,
            MediaIntentType.MEDIA_STOP: IntentType.PAUSE_MEDIA.value,
        }.get(nlu_result.intent)
        if direct_intent is None:
            return self._build_result(
                False,
                nlu_result.intent.value,
                f"I couldn't execute {nlu_result.intent.value.replace('_', ' ')} for {target or 'playback'}.",
                error="unsupported_media_target",
                data={"target_app": target or "system"},
            )

        direct_result = self._execute_direct_intent(
            direct_intent,
            {"target_app": target or "system", **dict(nlu_result.entities)},
            raw_text,
        )
        if direct_result is None:
            return None
        return self._normalize_nlu_result(
            direct_result,
            intent=nlu_result.intent.value,
            target_app=target or "system",
            response_text=self._nlu_response_text(nlu_result.intent, target or "generic"),
        )

    @staticmethod
    def _resolve_nlu_media_target(nlu_result: NLUIntent) -> str:
        if nlu_result.target is not None:
            return nlu_result.target.value
        explicit_target = str((nlu_result.entities or {}).get("target_app") or "").strip().lower()
        if explicit_target:
            return explicit_target
        window_title = str(getattr(state, "current_window_title", "") or "").lower()
        current_app = str(getattr(state, "current_app", "") or "").lower()
        if "youtube" in window_title or "youtube" in current_app:
            return "youtube"
        return ""

    def _normalize_nlu_result(
        self,
        result: dict[str, Any],
        *,
        intent: str,
        target_app: str,
        response_text: str,
    ) -> dict[str, Any]:
        payload = dict(result or {})
        payload["response"] = response_text
        payload.setdefault("intent", intent)
        payload.setdefault("success", bool(payload.get("success")))
        data = payload.get("data", {})
        if not isinstance(data, dict):
            data = {}
        data.setdefault("target_app", target_app)
        data.setdefault("route", target_app or "system")
        data.setdefault("backend", payload.get("skill_name") or payload.get("skill") or target_app or "system")
        data.setdefault("speak_response", True)
        payload["data"] = data
        return payload

    @staticmethod
    def _nlu_response_text(intent: MediaIntentType, target_app: str) -> str:
        label = {
            MediaTarget.YOUTUBE.value: "YouTube playback",
            MediaTarget.SPOTIFY.value: "Spotify playback",
            MediaTarget.VLC.value: "VLC playback",
            "generic": "playback",
            "system": "playback",
        }.get(str(target_app or "").strip().lower(), "playback")
        mapping = {
            MediaIntentType.MEDIA_RESUME: f"Resuming {label}.",
            MediaIntentType.MEDIA_PAUSE: f"Pausing {label}.",
            MediaIntentType.MEDIA_NEXT: "Skipping to the next track." if "Spotify" not in label else "Skipping to the next Spotify track.",
            MediaIntentType.MEDIA_PREVIOUS: "Going to the previous track." if "Spotify" not in label else "Going to the previous Spotify track.",
            MediaIntentType.MEDIA_STOP: f"Stopping {label}.",
            MediaIntentType.MEDIA_MUTE: f"Muting {label}.",
            MediaIntentType.MEDIA_UNMUTE: f"Unmuting {label}.",
        }
        return mapping[intent]

    def _try_music_query_execution(self, text: str) -> dict[str, Any] | None:
        normalized = normalize_command(text)
        if not normalized.startswith("play "):
            return None
        if normalized in {"play again", "play video", "play youtube", "play playback", "play music"}:
            return None
        if "youtube" in normalized or "video" in normalized:
            return None
        if (state.current_context or state.current_app) == "youtube":
            return None
        routed = self._execute_media_skill_route(text, target_route="spotify", intent_name=IntentType.PLAY_MEDIA.value)
        if routed is None:
            return None
        routed["detected_intent"] = IntentType.PLAY_MEDIA.value
        return routed

    def _execute_media_skill_route(self, text: str, *, target_route: str, intent_name: str) -> dict[str, Any] | None:
        skill_context = {
            "command": text,
            "intent": intent_name,
            "entities": {},
            "current_app": state.current_context or state.current_app,
            "current_context": state.current_context,
            "current_process_name": state.current_process_name,
            "current_window_title": state.current_window_title,
            "preferred_browser": settings.get("preferred_browser"),
            "context_target_app": target_route,
            "context_resolved_intent": intent_name,
            "context_confidence": 1.0,
            "target_route": target_route,
            "last_skill_used": state.last_skill_used,
            "last_successful_action": state.last_successful_action,
            "youtube_active": getattr(state, "youtube_active", False),
            "music_active": getattr(state, "music_active", False),
            "active_music_provider": getattr(state, "active_music_provider", ""),
        }
        skill_result = self.skills_manager.execute_with_skill(
            context=skill_context,
            intent=intent_name,
            command=text,
        )
        return skill_result.to_dict() if skill_result is not None else None

    def _try_fast_path_execution(self, rule_result: IntentResult, text: str) -> dict[str, Any] | None:
        """
        Fast-path for simple explicit commands that don't need context resolution.

        Targets: "open X", "close X", "hi", etc. where the intent and target are unambiguous.
        Returns None if fast-path doesn't apply; otherwise returns execution result.
        """
        intent = rule_result.intent
        entities = rule_result.entities or {}

        # OPEN_APP with explicit target - fastest path
        if intent == IntentType.OPEN_APP:
            app_name = self._extract_primary_app_name(entities)
            if app_name and len(app_name.strip()) > 0:
                logger.info("Fast-path: opening app '%s' without context resolution", app_name)
                return self.handle_open_app(app_name)

        # GREETING without parameters
        if intent == IntentType.GREETING:
            logger.info("Fast-path: greeting")
            return self.handle_greeting()

        # TIME query
        if intent == IntentType.QUESTION and "time" in text.lower():
            logger.info("Fast-path: time query")
            return self.handle_time()

        # HELP request
        if intent == IntentType.HELP:
            logger.info("Fast-path: help")
            return self.handle_help()

        # SYSTEM CONTROL fast-path - volume, brightness, etc.
        if intent in {IntentType.VOLUME_UP, IntentType.VOLUME_DOWN, IntentType.MUTE, IntentType.UNMUTE,
                      IntentType.BRIGHTNESS_UP, IntentType.BRIGHTNESS_DOWN, IntentType.SET_VOLUME,
                      IntentType.SET_BRIGHTNESS, IntentType.LOCK_PC, IntentType.SHUTDOWN_PC,
                      IntentType.RESTART_PC, IntentType.SLEEP_PC}:
            logger.info("Fast-path: system control '%s'", intent.value)
            return self.handle_system_control(intent.value, entities, text)

        # No fast-path applicable
        return None

    def handle_time(self) -> dict[str, Any]:
        return self._build_result(True, "time_query", responses.time_response())

    def handle_help(self) -> dict[str, Any]:
        return self._build_result(True, IntentType.HELP.value, responses.help_response())

    def handle_open_app(self, app_name: str) -> dict[str, Any]:
        permission = self.permission_manager.evaluate(IntentType.OPEN_APP.value, {"app_name": app_name})
        if permission.decision == Decision.DENY:
            return self._build_result(False, IntentType.OPEN_APP.value, permission.reason, error="permission_denied", data={"permission": permission.to_dict()})

        direct_result = self._run_callable_with_timeout(
            lambda: self.command_executor.execute(
                IntentType.OPEN_APP.value,
                {"app": app_name, "app_name": app_name, "requested_app": app_name},
                f"open {app_name}",
            ),
            intent=IntentType.OPEN_APP.value,
            command_text=f"open {app_name}",
            timeout_seconds=self._resolve_timeout_seconds(IntentType.OPEN_APP.value),
            route="launcher",
            backend="CommandExecutor",
            target_app=app_name,
        )
        if direct_result is not None:
            self.permission_manager.record_execution(
                IntentType.OPEN_APP.value,
                {"app_name": app_name},
                success=bool(direct_result.get("success")),
                error=str(direct_result.get("error") or ""),
            )
            return self._maybe_fallback_search_for_unknown_app(app_name, direct_result)

        launch_result = self.desktop_launcher.launch_app(app_name)
        self.permission_manager.record_execution(IntentType.OPEN_APP.value, {"app_name": app_name}, success=launch_result.success, error="" if launch_result.success else launch_result.message)
        return self._build_result(
            launch_result.success,
            IntentType.OPEN_APP.value,
            launch_result.message,
            error=launch_result.error,
            data={
                "target_app": launch_result.matched_name or app_name,
                "requested_app": app_name,
                "matched_name": launch_result.matched_name,
                "path": launch_result.path,
                "pid": launch_result.pid,
                "verified": bool(getattr(launch_result, "verified", False)),
                "route": "launcher",
                "backend": "app_launcher",
                **dict(launch_result.data or {}),
            },
        )

    def handle_system_control(self, intent_name: str, entities: dict[str, Any], text: str) -> dict[str, Any]:
        system_skill = getattr(self.skills_manager, "system_skill", None)
        if system_skill is None and hasattr(self.skills_manager, "get_skill"):
            system_skill = self.skills_manager.get_skill("SystemSkill")
        if system_skill is not None:
            return system_skill.execute(text, {"intent": "system_control", "entities": entities}).to_dict()
        if hasattr(self.skills_manager, "execute_with_skill"):
            skill_result = self.skills_manager.execute_with_skill(
                context={
                    "command": text,
                    "intent": "system_control",
                    "entities": dict(entities or {}),
                    "target_route": "system",
                    "context_target_app": "system",
                    "current_app": state.current_context or state.current_app,
                    "current_context": state.current_context,
                    "current_process_name": state.current_process_name,
                    "current_window_title": state.current_window_title,
                },
                intent="system_control",
                command=text,
            )
            if skill_result is not None:
                return skill_result.to_dict()

        permission = self.permission_manager.evaluate(
            "system_control",
            {
                "action": intent_name,
                "control": intent_name,
                **dict(entities or {}),
            },
        )
        if permission.decision == Decision.DENY:
            return self._build_result(
                False,
                intent_name,
                permission.reason,
                error="permission_denied",
                data={"permission": permission.to_dict(), "target_app": "system"},
            )

        result = self._run_callable_with_timeout(
            lambda: self.command_executor.execute(intent_name, entities, text),
            intent=intent_name,
            command_text=text,
            timeout_seconds=self._resolve_timeout_seconds(intent_name, route="system"),
            route="system",
            backend="CommandExecutor",
            target_app="system",
        )
        if result is not None:
            self.permission_manager.record_execution(
                "system_control",
                {"action": intent_name, "control": intent_name, **dict(entities or {})},
                success=bool(result.get("success")),
                error=str(result.get("error") or ""),
            )
            return result

        return self._build_result(
            False,
            intent_name,
            "I couldn't resolve that system action.",
            error="unsupported_system_action",
            data={"target_app": "system"},
        )

    def _maybe_fallback_search_for_unknown_app(self, app_name: str, result: dict[str, Any]) -> dict[str, Any]:
        payload = dict(result or {})
        if bool(payload.get("success")):
            return payload

        error = str(payload.get("error") or "").strip().lower()
        if error not in {"app_not_found", "path_not_found", "unsupported_app_type"}:
            return payload

        query = str(app_name or "").strip()
        if not query:
            return payload

        KNOWN_SERVICES = {"instagram", "facebook", "twitter", "youtube", "gmail", "netflix", "tiktok", "discord", "whatsapp", "telegram", "linkedin", "reddit", "quora", "spotify", "apple tv", "phone link", "apple music"}
        is_known_web_service = any(service in query.lower() for service in KNOWN_SERVICES)
        
        if is_known_web_service:
            logger.info("[Routing] Unknown app '%s' is a known web service, opening in browser", query)
            search_result = self.handle_search(query)
            search_data = search_result.get("data", {})
            if not isinstance(search_data, dict):
                search_data = {}
            search_data.update(
                {
                    "fallback_from": "open_app",
                    "fallback_reason": error,
                    "requested_app": query,
                }
            )
            search_result["data"] = search_data
            return search_result
        
        if len(query.split()) <= 2 and not any(ext in query.lower() for ext in ["app", "application", "software", "program"]):
            return self._build_result(
                False,
                IntentType.OPEN_APP.value,
                f"I couldn't find any app named '{query}'. It's not installed on this system.",
                error="app_not_found",
                data={"requested_app": query},
            )
        
        logger.info("[Routing] Unknown app target falling back to search", input=f"open {query}", corrected=f"open {query}", intent="open_app", entities={"app": query}, route="browser", skill="BrowserSkill", fallback_triggered=True)
        search_result = self.handle_search(query)
        search_data = search_result.get("data", {})
        if not isinstance(search_data, dict):
            search_data = {}
        search_data.update(
            {
                "fallback_from": "open_app",
                "fallback_reason": error,
                "requested_app": query,
            }
        )
        search_result["data"] = search_data
        return search_result

    def handle_search(self, query: str, target: str | None = None) -> dict[str, Any]:
        normalized_target = str(target or "").strip().lower() or None
        permission = self.permission_manager.evaluate(IntentType.SEARCH.value, {"query": query, "target": normalized_target})
        if permission.decision == Decision.DENY:
            return self._build_result(False, IntentType.SEARCH.value, permission.reason, error="permission_denied", data={"permission": permission.to_dict()})
        result = self._run_callable_with_timeout(
            lambda: self.browser_skill.search(query, target_app=normalized_target),
            intent=IntentType.SEARCH.value,
            command_text=f"search {query}",
            timeout_seconds=self._resolve_timeout_seconds(IntentType.SEARCH.value, route="browser"),
            route="browser",
            backend="BrowserSkill",
            target_app=normalized_target or "browser",
        )
        self.permission_manager.record_execution(IntentType.SEARCH.value, {"query": query, "target": normalized_target}, success=result.success, error=result.error)
        return self._build_result(
            result.success,
            IntentType.SEARCH.value,
            result.message,
            error=result.error,
            data={
                "target_app": result.browser or normalized_target or "browser",
                "requested_browser": normalized_target or "",
                "query": result.query,
                "search_engine": result.engine,
                "url": result.url,
                "route": "browser",
                "backend": "browser",
                **dict(result.data or {}),
            },
        )

    def handle_unknown(self) -> dict[str, Any]:
        return self._build_result(False, IntentType.UNKNOWN.value, responses.unknown_response())

    def _apply_stt_correction_if_needed(self, text: str, source: str) -> str:
        """Apply STT correction for speech input when enabled."""
        if source.lower() != "speech":
            return text

        if not settings.get("stt_correction_enabled"):
            return text

        with metrics.measure("stt_correction", source=source):
            preliminary_result = self.detector.detect(text, text)
            if not self._should_try_stt_correction(text, preliminary_result):
                return text

            correction_result = self.corrector.correct(text)

            state.last_raw_transcript = text
            state.last_corrected_transcript = correction_result.corrected_text
            state.last_correction_confidence = correction_result.confidence
            state.last_correction_method = correction_result.method_used
            state.correction_applied = correction_result.safe_to_apply

            if correction_result.safe_to_apply and correction_result.corrected_text != text:
                logger.info(
                    "STT correction applied",
                    raw_input=text,
                    normalized_input=correction_result.corrected_text,
                    confidence=round(correction_result.confidence, 3),
                    method=correction_result.method_used,
                )
                if settings.get("show_original_transcript"):
                    logger.info("Original transcript captured", transcript=text)
                return parser.clean_text(correction_result.corrected_text)

        return text

    def _resolve_context_decision(self, text: str) -> ContextDecision:
        if not settings.get("context_engine_enabled"):
            return NULL_DECISION

        try:
            with metrics.measure("context_resolution", source="processor"):
                snapshot = self.context.get_context_snapshot()
                return self.context_engine.resolve(text, snapshot, state)
        except Exception as exc:
            logger.warning("Context resolution failed: %s", exc)
            return NULL_DECISION

    def _handle_context_decision(
        self,
        decision: ContextDecision,
        original_text: str,
    ) -> dict[str, Any] | None:
        if decision.resolved_intent == "passthrough":
            return None

        if decision.requires_confirmation and decision.clarification_prompt and decision.confidence < float(
            settings.get("context_confidence_threshold") or 0.70
        ):
            return self._build_result(False, "clarification", decision.clarification_prompt)

        rewritten = self.context_engine.rewrite_command(original_text, decision)
        plan = self._generate_action_plan(rewritten, context_decision=decision)
        if plan:
            return self._execute_action_plan(plan)

        logger.warning(
            "Planner did not build a plan for context decision %s. Falling back.",
            decision.resolved_intent,
        )

        query = decision.entities.get("query") if isinstance(decision.entities, dict) else None
        if isinstance(query, str) and query.strip():
            return self.handle_search(query.strip(), target=decision.target_app)

        return self._build_result(
            False,
            decision.resolved_intent,
            f"I resolved the command for {decision.target_app}, but could not build an execution plan.",
        )

    def _run_callable_with_timeout(
        self,
        callback,
        *,
        intent: str,
        command_text: str,
        timeout_seconds: float,
        route: str = "",
        backend: str = "",
        target_app: str = "",
    ):
        outcome = invoke_with_timeout(callback, timeout_seconds=timeout_seconds)
        if outcome.timed_out:
            subject = command_text.strip() or intent.replace("_", " ")
            message = f"The command '{subject}' timed out after {int(timeout_seconds)} seconds."
            raise ActionTimeoutError(
                message=message,
                code="action_timeout",
                context={
                    "intent": intent,
                    "command": command_text,
                    "timeout_seconds": timeout_seconds,
                    "route": route,
                    "backend": backend,
                    "target_app": target_app,
                },
            )
        if outcome.error is not None:
            raise outcome.error
        return outcome.value

    def _resolve_timeout_seconds(self, intent_name: str, *, route: str = "") -> float:
        normalized_intent = str(intent_name or "").strip().lower()
        normalized_route = str(route or "").strip().lower()
        command_timeout = float(settings.get("command_timeout_seconds") or 35)

        if normalized_intent in {IntentType.OPEN_APP.value}:
            timeout = float(settings.get("app_launch_timeout_seconds") or 5)
        elif normalized_intent == IntentType.CLOSE_APP.value:
            timeout = 3.0
        elif normalized_intent in {
            IntentType.MINIMIZE_APP.value,
            IntentType.MAXIMIZE_APP.value,
            IntentType.FOCUS_APP.value,
            IntentType.RESTORE_APP.value,
            IntentType.TOGGLE_APP.value,
        }:
            timeout = float(settings.get("window_action_timeout_seconds") or 15)
        elif normalized_intent in {IntentType.OPEN_WEBSITE.value, IntentType.SEARCH.value, IntentType.SEARCH_WEB.value}:
            timeout = 5.0
        elif normalized_route in {"browser", "youtube", "chrome"}:
            timeout = 5.0
        elif normalized_route in {"spotify", "music"}:
            timeout = 10.0
        elif normalized_route in {"youtube_search", "youtube_skill"}:
            timeout = 10.0
        elif normalized_route == "whatsapp" or normalized_intent in {"whatsapp_call", "whatsapp_message", "send_message"}:
            timeout = float(settings.get("whatsapp_skill_timeout_seconds") or 60)
        elif normalized_route == "ocr" or normalized_intent.startswith("ocr"):
            timeout = float(settings.get("ocr_timeout_seconds") or 15)
        else:
            timeout = command_timeout

        if "whatsapp" not in normalized_route and "whatsapp" not in normalized_intent:
            timeout = min(timeout, max(int(command_timeout * 0.9), timeout))
        return max(1.0, timeout)

    def _execute_direct_intent(self, intent_name: str, entities: dict[str, Any], text: str) -> dict[str, Any] | None:
        target_app = self._extract_primary_app_name(entities) or self._extract_search_target(entities) or ""
        return self._run_callable_with_timeout(
            lambda: self.command_executor.execute(intent_name, entities, text),
            intent=intent_name,
            command_text=text,
            timeout_seconds=self._resolve_timeout_seconds(intent_name, route="direct"),
            route="direct",
            backend="CommandExecutor",
            target_app=target_app,
        )

    def _route_rule_result(self, detection_result: IntentResult, text: str) -> dict[str, Any]:
        self._update_runtime_state(
            detection_result.intent.value,
            detection_result.confidence,
            detection_result.entities,
        )
        return self._route_intent(detection_result.intent.value, detection_result.entities, text)

    def _route_intent(self, intent_name: str, entities: dict[str, Any], text: str) -> dict[str, Any]:
        intent_name = (intent_name or IntentType.UNKNOWN.value).lower()
        normalized_text = normalize_command(text)

        if intent_name == IntentType.GREETING.value:
            return self.handle_greeting()

        if intent_name == IntentType.REPEAT_LAST_COMMAND.value:
            replay_command = str(getattr(state, "last_replayable_command", "") or "").strip()
            if not replay_command:
                return self._build_result(
                    False,
                    IntentType.REPEAT_LAST_COMMAND.value,
                    "There is no previous command I can repeat yet.",
                    error="no_previous_command",
                    data={"target_app": "history", "speak_response": True},
                )
            return self.process(replay_command, source="repeat")

        if intent_name == IntentType.QUESTION.value:
            if "time" in normalized_text:
                return self.handle_time()
            if self._is_status_question(normalized_text):
                return self._build_result(
                    True,
                    IntentType.QUESTION.value,
                    responses.status_response(),
                    category="chat",
                    data={"target_app": "assistant", "speak_response": True},
                )
            if self._is_identity_question(normalized_text):
                return self._build_result(
                    True,
                    "identity",
                    responses.assistant_ready_response(self.identity_mgr.get_assistant_name()),
                    category="chat",
                    data={"target_app": "assistant", "speak_response": True},
                )
            return self._route_intent(IntentType.SEARCH_WEB.value, {"query": normalized_text}, text)

        if intent_name == IntentType.HELP.value:
            return self.handle_help()

        if self._is_gratitude_phrase(normalized_text):
            return self._build_result(
                True,
                "gratitude",
                responses.thanks_response(),
                category="chat",
                data={"target_app": "assistant", "speak_response": True},
            )

        direct_result = self._execute_direct_intent(intent_name, entities, text)
        if direct_result is not None:
            return direct_result

        if intent_name == IntentType.OPEN_APP.value:
            app_name = self._extract_primary_app_name(entities) or "unknown application"
            return self.handle_open_app(app_name)

        if intent_name in {IntentType.SEARCH.value, IntentType.SEARCH_WEB.value}:
            query = self._resolve_search_query_text(text, entities)
            target = self._extract_search_target(entities)
            return self.handle_search(query, target=target)

        if intent_name == IntentType.SYSTEM_CONTROL.value:
            system_skill = getattr(self.skills_manager, "system_skill", None)
            if system_skill is not None:
                return system_skill.execute(text, {"intent": intent_name, "entities": entities}).to_dict()
            return self._build_result(
                False,
                IntentType.SYSTEM_CONTROL.value,
                "I couldn't resolve that system action.",
                error="unsupported_system_action",
                data={"target_app": "system"},
            )

        if intent_name == IntentType.FILE_ACTION.value:
            return self._handle_file_action_entities(entities)

        if intent_name == IntentType.MULTI_ACTION.value:
            return self._build_result(
                False,
                IntentType.MULTI_ACTION.value,
                "I couldn't process this multi-step command.",
                error="multi_action_failed",
                data={"speak_response": True},
            )

        if intent_name == IntentType.UNKNOWN.value:
            logger.warning("Command could not be classified: %s", text)
            return self._build_result(
                False,
                IntentType.UNKNOWN.value,
                "I'm not sure what you want me to do. Try being more specific.",
                error="unknown_intent",
                data={"speak_response": True},
            )

        return self.handle_unknown()

    def _execute_plan(self, plan: PlanSchema) -> dict[str, Any]:
        messages: list[str] = []
        success = True

        for step in plan.steps:
            step_result = self._execute_plan_step(step)
            messages.append(step_result["response"])
            success = success and bool(step_result["success"])

        return self._build_result(success, IntentType.MULTI_ACTION.value, " ".join(messages))

    def _execute_plan_step(self, step: PlanStep) -> dict[str, Any]:
        if step.action == IntentType.OPEN_APP.value:
            app_name = step.target or str(step.params.get("app_name") or "")
            return self.handle_open_app(app_name or "unknown application")

        if step.action == IntentType.SEARCH.value:
            query = str(step.params.get("query") or step.target or "").strip()
            target = step.target or str(step.params.get("target") or "")
            return self.handle_search(query or "unknown query", target=target or None)

        if step.action == IntentType.SYSTEM_CONTROL.value:
            entities = {"control": step.target, **step.params}
            return self._route_intent(IntentType.SYSTEM_CONTROL.value, entities, step.target)

        if step.action == IntentType.FILE_ACTION.value:
            entities = {"filename": step.target, **step.params}
            return self._route_intent(IntentType.FILE_ACTION.value, entities, step.target)

        if step.action == IntentType.QUESTION.value and "time" in (step.target or "").lower():
            return self.handle_time()

        return self._build_result(
            False,
            step.action,
            f"Planned step '{step.action}' is not executable yet in the local processor.",
        )

    def _should_try_stt_correction(self, text: str, detection_result: IntentResult) -> bool:
        tokens = {token.lower() for token in parser.tokenize(text)}
        noisy = bool(tokens & self.SPEECH_NOISE_TOKENS)
        return noisy or detection_result.intent == IntentType.UNKNOWN or (
            detection_result.confidence < self.STT_CORRECTION_CONFIDENCE_THRESHOLD
        )

    def _should_use_planner(self, rule_result: IntentResult, text: str) -> bool:
        """Determine whether the command should be passed through the action planner."""
        if not settings.get("planner_enabled"):
            return False
        normalized = normalize_command(text)
        if rule_result.intent in {IntentType.GREETING, IntentType.QUESTION, IntentType.HELP}:
            return False
        if self._is_status_question(normalized) or self._is_identity_question(normalized) or self._is_gratitude_phrase(normalized):
            return False

        if rule_result.intent == IntentType.MULTI_ACTION:
            return True

        lowered = text.lower()
        connectors = (" and ", " then ", " after ", " after that ", " followed by ", ",")
        if any(connector in lowered for connector in connectors):
            return True

        verb_markers = (
            "open ",
            "launch ",
            "start ",
            "run ",
            "search ",
            "find ",
            "look up ",
            "create ",
            "delete ",
            "move ",
            "copy ",
            "turn ",
            "switch ",
            "play ",
        )
        verb_count = sum(1 for marker in verb_markers if marker in lowered)
        return verb_count >= 2

    def _build_plan_context(
        self,
        text: str,
        rule_result: IntentResult,
        entities: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "command": text,
            "rule_intent": rule_result.intent.value,
            "rule_confidence": rule_result.confidence,
            "known_entities": entities,
            "supported_actions": [
                IntentType.OPEN_APP.value,
                IntentType.SEARCH.value,
                IntentType.SYSTEM_CONTROL.value,
                IntentType.FILE_ACTION.value,
                IntentType.QUESTION.value,
                IntentType.HELP.value,
            ],
        }

    @staticmethod
    def _extract_primary_app_name(entities: dict[str, Any]) -> str | None:
        app_name = entities.get("app")
        if isinstance(app_name, str) and app_name.strip():
            return app_name.strip()

        app_name = entities.get("app_name")
        if isinstance(app_name, str) and app_name.strip():
            return app_name.strip()

        apps = entities.get("apps")
        if isinstance(apps, list) and apps:
            first_app = apps[0]
            if isinstance(first_app, str) and first_app.strip():
                return first_app.strip()

        target = entities.get("target")
        if isinstance(target, str) and target.strip():
            return target.strip()
        return None

    @staticmethod
    def _extract_search_target(entities: dict[str, Any]) -> str | None:
        for key in ("browser", "target", "service", "app", "app_name"):
            value = entities.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        apps = entities.get("apps")
        if isinstance(apps, list) and apps:
            first_app = apps[0]
            if isinstance(first_app, str) and first_app.strip():
                return first_app.strip()
        return None

    def _update_runtime_state(self, intent: str, confidence: float, entities: dict[str, Any]) -> None:
        state.last_intent = intent
        state.last_confidence = confidence
        state.last_entities = entities
        state.intent_count_by_type[intent] = state.intent_count_by_type.get(intent, 0) + 1
        
        # Track typed entities for conversation memory
        tracked: list[TrackedEntity] = []
        primary_app = entities.get("app") or entities.get("app_name")
        if primary_app:
            tracked.append(TrackedEntity(type=EntityType.APP, name=str(primary_app), confidence=confidence))
        if "filename" in entities:
            tracked.append(TrackedEntity(type=EntityType.FILE, name=str(entities["filename"]), confidence=confidence))
        if "query" in entities:
            tracked.append(TrackedEntity(type=EntityType.QUERY, name=str(entities["query"]), confidence=confidence))
        if "contact" in entities or "person" in entities:
            name = str(entities.get("contact") or entities.get("person") or "")
            if name:
                tracked.append(TrackedEntity(type=EntityType.PERSON, name=name, confidence=confidence))
                
        state._current_turn_entities = tracked
        if primary_app:
            state.last_target_app = str(primary_app)
        if entities.get("contact"):
            state.last_message_target = str(entities.get("contact"))

    def _generate_action_plan(
        self,
        text: str,
        context_decision: ContextDecision | None = None,
    ) -> ExecutionPlan | None:
        """Generate an action plan using the planner and optional context hints."""
        if not settings.get("planner_enabled"):
            return None

        try:
            with metrics.measure("planner_generation", source="processor"):
                context = PlannerContext(
                    current_app=state.current_context or state.current_app,
                    browser_open=(state.current_process_name.lower() in (
                        "chrome.exe",
                        "msedge.exe",
                        "firefox.exe",
                        "opera.exe",
                        "brave.exe",
                        "vivaldi.exe",
                    )),
                    search_results_available=bool(context_decision and "result" in context_decision.resolved_intent),
                    window_title=state.current_window_title,
                    last_action=state.last_successful_action,
                    recent_commands=list(getattr(state, "recent_commands", [])),
                    resolved_target_app=context_decision.target_app if context_decision else "",
                    rewritten_command=context_decision.rewritten_command if context_decision else "",
                )
                plan = self.planner.plan(
                    text,
                    context=context,
                    context_hints=context_decision.plan_hints if context_decision else None,
                )

                if plan and plan.is_valid and plan.confidence >= settings.get("planner_confidence_threshold"):
                    logger.info("Generated plan with %d steps (confidence=%.2f)", plan.step_count, plan.confidence)
                    if settings.get("planner_show_debug"):
                        logger.info("Plan:\n%s", plan)
                    return plan

                logger.debug("Plan generation failed or low confidence (confidence=%.2f)", plan.confidence if plan else 0.0)
                return None

        except Exception as exc:
            logger.error("Action planner failed: %s", exc)
            return None

    def _execute_action_plan(self, plan: ExecutionPlan) -> dict[str, Any]:
        """Execute steps from an action plan through the execution engine."""
        logger.info("Dispatching plan to ExecutionEngine: %d steps", plan.step_count)
        # State machine transition: PROCESSING -> EXECUTING (Section 16 of spec)
        state.set_state("EXECUTING", reason="plan_execution_start")
        state.is_executing = True
        try:
            with metrics.measure("plan_execution", source="processor", steps=plan.step_count):
                exec_result: ExecutionResult = self.engine.execute_plan(plan)
            messages: list[str] = []
            for step_result in exec_result.results:
                if step_result.message:
                    messages.append(step_result.message)

            response = exec_result.summary
            if messages and settings.get("show_step_progress"):
                response = "\n".join(messages) + "\n" + exec_result.summary

            return self._build_result(exec_result.success, "multi_action", response)
        finally:
            state.is_executing = False
            # State will transition to SPEAKING or READY in finalize

    def _finalize_processed_result(
        self,
        result: dict[str, Any],
        *,
        raw_input: str,
        normalized_input: str,
        detected_intent: str,
        source: str,
        context_decision: ContextDecision,
        timer: Any = None,
        trace_id: str = "",
    ) -> dict[str, Any]:
        resolved_trace_id = str(trace_id or new_correlation_id())
        result = self._coerce_result(result)
        result = self._recover_result(
            result,
            raw_input=raw_input,
            normalized_input=normalized_input,
            detected_intent=detected_intent,
            source=source,
            context_decision=context_decision,
        )
        result_data = result.get("data", {})
        if not isinstance(result_data, dict):
            result_data = {}
        result_data.setdefault("trace_id", resolved_trace_id)
        action_result = ensure_action_result(
            result,
            default_action=str(result.get("intent") or ""),
            default_target=str(result_data.get("target_app") or "").strip() or None,
        )
        action_result["trace_id"] = resolved_trace_id
        action_result["recovered"] = bool(result_data.get("recovered", False))
        result["action_result"] = action_result
        result["data"] = result_data
        result = ensure_command_result(result)
        selected_skill = self._extract_selected_skill(result)
        action_taken = self._extract_action_taken(result)
        if timer:
            timer.tags.update(
                {
                    "intent": str(result.get("intent", "")),
                    "detected_intent": detected_intent or str(result.get("intent", "")),
                    "selected_skill": selected_skill,
                    "success": bool(result.get("success", False)),
                    "source": source,
                }
            )
            duration_ms = metrics.end_timer(timer)
        else:
            duration_ms = 0.0

        result = self._attach_command_trace(
            result,
            raw_input=raw_input,
            normalized_input=normalized_input,
            detected_intent=detected_intent,
            selected_skill=selected_skill,
            duration_ms=duration_ms,
        )

        state.last_command_latency_ms = duration_ms
        metrics.record_counter(
            "requests_succeeded_total" if result.get("success") else "requests_failed_total",
            source=source,
        )
        metrics.capture_process_metrics(source=source, intent=str(result.get("intent", "")))
        self._record_request_performance_breakouts(duration_ms, result, selected_skill)

        analytics.record_command(
            raw_input=raw_input,
            normalized_input=normalized_input,
            intent=result.get("intent", ""),
            detected_intent=detected_intent or result.get("intent", ""),
            selected_skill=selected_skill,
            action_taken=action_taken,
            success=bool(result.get("success", False)),
            latency_ms=duration_ms,
            source=source,
            failure_reason=str(result.get("error", "") or ""),
            metadata={
                "route": str((result.get("data") or {}).get("route") or ""),
                "backend": str((result.get("data") or {}).get("backend") or ""),
                "verified": bool((result.get("action_result") or {}).get("verified", False)),
                "context_target_app": context_decision.target_app if context_decision else "",
                "context_resolved_intent": context_decision.resolved_intent if context_decision else "",
                "active_app": state.current_context or state.current_app,
            },
        )
        self._record_feature_usage(result, selected_skill, source)

        self._record_context_history(normalized_input or raw_input, result, context_decision)

        if settings.get("conversation_memory_enabled"):
            entities = getattr(state, "_current_turn_entities", [])
            result_data = result.get("data", {})
            choices = []
            if isinstance(result_data, dict):
                if isinstance(result_data.get("choices"), list):
                    choices = [str(c) for c in result_data["choices"]]
                elif isinstance(result_data.get("results"), list):
                    for r in result_data["results"]:
                        if isinstance(r, dict) and "name" in r:
                            choices.append(str(r["name"]))
                        elif isinstance(r, dict) and "path" in r:
                            choices.append(str(r["path"]))

            _cm.add_turn(
                user_input=normalized_input or raw_input,
                intent=str(result.get("intent", "")),
                entities=entities,
                action_result=result,
                choices=choices,
            )
            state._current_turn_entities = []

        final_result = self._decorate_result_with_context(result, context_decision)
        self._remember_follow_up_state(
            final_result,
            normalized_input=normalized_input,
            raw_input=raw_input,
        )
        self._emit_result_alert(final_result)
        state.last_error_id = "" if final_result.get("success") else resolved_trace_id
        logger.info(
            "Command completed",
            raw_input=raw_input,
            normalized_input=normalized_input,
            intent=str(final_result.get("intent", "")),
            success=bool(final_result.get("success", False)),
            error=str(final_result.get("error", "")),
            duration_ms=round(float(duration_ms or 0.0), 2),
        )
        return final_result

    def _record_context_history(
        self,
        original_text: str,
        result: dict[str, Any],
        context_decision: ContextDecision,
    ) -> None:
        if not settings.get("context_engine_enabled"):
            return

        target_app = context_decision.target_app
        if not target_app or target_app == "unknown":
            result_data = result.get("data", {})
            if isinstance(result_data, dict):
                target_app = str(result_data.get("target_app") or "").strip()
        if not target_app or target_app == "unknown":
            target_app = self._extract_search_target(state.last_entities) or state.current_context or state.current_app or "unknown"

        try:
            self.context_engine.record_command(
                original_text,
                str(result.get("intent", "unknown")),
                target_app,
                bool(result.get("success")),
                rewritten_command=context_decision.rewritten_command if context_decision else "",
                entities=context_decision.entities if context_decision else state.last_entities,
            )
        except Exception as exc:
            logger.warning("Failed to record context history: %s", exc)

    def _decorate_result_with_context(
        self,
        result: dict[str, Any],
        decision: ContextDecision,
    ) -> dict[str, Any]:
        if not settings.get("show_context_debug"):
            return result

        decorated = dict(result)
        decorated["context_debug"] = {
            "detected_context": decision.target_app,
            "resolved_intent": decision.resolved_intent,
            "rewritten_command": decision.rewritten_command,
            "confidence": decision.confidence,
            "reason": decision.reason,
        }
        return decorated

    def _emit_result_alert(self, result: dict[str, Any]) -> None:
        """
        Emit result notification through the unified response pipeline.
        """
        response = str(result.get("message") or result.get("response") or "").strip()
        if not response:
            return
        
        result_data = result.get("data", {})
        if not isinstance(result_data, dict):
            result_data = {}
        if result_data.get("suppress_notification"):
            return
        
        intent = str(result.get("intent") or "").strip().lower()
        success = bool(result.get("success"))
        message = str(result_data.get("notification_message") or response).strip() or response
        
        # Determine severity based on success and intent
        if success:
            severity = ResponseSeverity.INFO
        elif intent in {"clarification"}:
            severity = ResponseSeverity.WARNING
        else:
            severity = ResponseSeverity.ERROR

        get_response_service().respond(
            text=message,
            category=ResponseCategory.COMMAND_RESULT,
            success=success,
            severity=severity,
            speak_enabled=bool(result_data.get("speak_response", True)),
            silent_reason=str(result_data.get("silent_reason") or "") or None,
            notification_enabled=bool(result_data.get("notification_enabled", False)),
            notification_title=str(result_data.get("notification_title") or "") or None,
            source_skill=str(result.get("skill_name") or result.get("skill") or "processor"),
            action_name=intent or "unknown",
            entities=result.get("entities") if isinstance(result.get("entities"), dict) else state.last_entities,
            metadata=result_data,
        )

    def handle_external_error(
        self,
        error: Exception | dict[str, Any] | str,
        *,
        command_context: dict[str, Any] | None = None,
        source: str = "ui",
    ) -> dict[str, Any]:
        context = dict(command_context or {})
        raw_input = str(context.get("raw_input") or context.get("command") or "runtime error")
        normalized_input = str(context.get("normalized_input") or context.get("command") or raw_input)
        detected_intent = str(context.get("detected_intent") or context.get("intent") or "runtime_error")
        detected_intent, context = self._resolve_timeout_context(
            detected_intent,
            raw_input=raw_input,
            normalized_input=normalized_input,
            context=context,
        )
        outcome = self.recovery.handle(
            error,
            {
                **context,
                "raw_input": raw_input,
                "normalized_input": normalized_input,
                "command": normalized_input,
                "source": source,
                "preferred_browser": settings.get("preferred_browser"),
                "entities": {
                    **dict(context.get("entities") or {}),
                    **dict(state.last_entities or {}),
                },
            },
        )
        return self._finalize_processed_result(
            self._coerce_result(outcome.result),
            raw_input=raw_input,
            normalized_input=normalized_input,
            detected_intent=detected_intent,
            source=source,
            context_decision=NULL_DECISION,
            timer=None,
        )

    def _resolve_timeout_context(
        self,
        detected_intent: str,
        *,
        raw_input: str,
        normalized_input: str,
        context: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        normalized_detected = str(detected_intent or "").strip().lower()
        if normalized_detected not in {"action_timeout", "runtime_error", ""}:
            return detected_intent, context
        if not normalized_input.strip():
            return detected_intent, context

        try:
            inferred = self.detector.detect(raw_input, normalized_input, context=self._runtime_context_snapshot())
        except Exception as exc:
            logger.debug("Timeout context inference failed: %s", exc)
            return detected_intent, context

        if inferred.intent == IntentType.UNKNOWN:
            return detected_intent, context

        inferred_entities = resolve_context_entities(
            inferred.intent.value,
            inferred.entities,
            inferred.cleaned_text or normalized_input,
            context=self._runtime_context_snapshot(),
        )
        merged = dict(context)
        merged["entities"] = {**inferred_entities, **dict(context.get("entities") or {})}
        merged["intent"] = inferred.intent.value
        merged["detected_intent"] = inferred.intent.value
        merged.setdefault("requested_app", self._extract_primary_app_name(merged["entities"]))
        merged.setdefault("target", self._extract_search_target(merged["entities"]))
        merged.setdefault("query", self._resolve_search_query_text(normalized_input, merged["entities"]))
        return inferred.intent.value, merged

    def _recover_result(
        self,
        result: dict[str, Any],
        *,
        raw_input: str,
        normalized_input: str,
        detected_intent: str,
        source: str,
        context_decision: ContextDecision,
    ) -> dict[str, Any]:
        if not settings.get("error_recovery_enabled", True):
            return result
        if bool(result.get("success")):
            return result
        if not self._should_attempt_recovery(result):
            return result

        recovery_context = self._build_recovery_context(
            result,
            raw_input=raw_input,
            normalized_input=normalized_input,
            detected_intent=detected_intent,
            source=source,
            context_decision=context_decision,
        )
        outcome = self.recovery.handle(result, recovery_context)
        resolved = self._coerce_result(outcome.result)
        resolved_data = resolved.get("data", {}) if isinstance(resolved, dict) else {}
        if not isinstance(resolved_data, dict):
            resolved_data = {}
        original_data = result.get("data", {}) if isinstance(result, dict) else {}
        if not isinstance(original_data, dict):
            original_data = {}
        resolved["data"] = {
            **resolved_data,
            "recovery_attempted": True,
            "recovered": bool(resolved.get("success")),
            "attempted_route": str(resolved_data.get("attempted_route") or original_data.get("route") or original_data.get("backend") or ""),
            "attempted_backend": str(resolved_data.get("attempted_backend") or original_data.get("backend") or ""),
            "attempted_selected_skill": str(resolved_data.get("attempted_selected_skill") or result.get("skill_name") or ""),
            "attempted_intent": str(resolved_data.get("attempted_intent") or result.get("intent") or detected_intent or ""),
        }
        return resolved

    @staticmethod
    def _should_attempt_recovery(result: dict[str, Any]) -> bool:
        intent = str(result.get("intent") or "").strip().lower()
        error = str(result.get("error") or "").strip().lower()
        data = result.get("data", {})
        if not isinstance(data, dict):
            data = {}

        if intent.startswith("recovery"):
            return False
        if data.get("recovery_prompt") or data.get("recovery_attempted"):
            return False
        if intent in {"clarification", "empty", "analytics_logs", "analytics_errors", "analytics_stats"}:
            return False
        if error in {"confirmation_required", "selection_required", "cancelled", "empty", "unknown_plugin_command"}:
            return False

        recoverable_errors = {
            "app_not_found",
            "browser_not_found",
            "path_not_found",
            "launch_error",
            "unsupported_app_type",
            "permission_denied",
            "timeout",
            "action_timeout",
            "launch_timeout",
            "contact_not_found",
            "empty_contact",
            "whatsapp_disambiguation",
            "ambiguous_match",
            "network_error",
            "search_failed",
            "device_unavailable",
            "microphone_unavailable",
            "backend_unavailable",
            "ocr_unavailable",
            "multi_action_failed",
            "unsupported",
            "exception",
        }
        if error in recoverable_errors:
            return True
        return any(key in data for key in ("requested_app", "matches", "cached_result", "device", "feature"))

    def _build_recovery_context(
        self,
        result: dict[str, Any],
        *,
        raw_input: str,
        normalized_input: str,
        detected_intent: str,
        source: str,
        context_decision: ContextDecision,
    ) -> dict[str, Any]:
        result_data = result.get("data", {})
        if not isinstance(result_data, dict):
            result_data = {}
        entities = dict(state.last_entities or {})
        return {
            "raw_input": raw_input,
            "normalized_input": normalized_input,
            "command": normalized_input or raw_input,
            "detected_intent": detected_intent or str(result.get("intent") or ""),
            "intent": str(result.get("intent") or ""),
            "error": str(result.get("error") or ""),
            "response": str(result.get("response") or ""),
            "source": source,
            "preferred_browser": settings.get("preferred_browser"),
            "current_app": state.current_context or state.current_app,
            "context_target_app": context_decision.target_app if context_decision else "",
            "entities": entities,
            "requested_app": result_data.get("requested_app") or self._extract_primary_app_name(entities),
            "query": result_data.get("query") or self._resolve_search_query_text(normalized_input or raw_input, entities),
            "target": result_data.get("requested_browser") or self._extract_search_target(entities),
            "contact": result_data.get("contact") or entities.get("contact") or state.last_message_target,
            "platform": result_data.get("platform") or entities.get("platform") or "",
            "feature": result_data.get("feature") or result.get("intent") or result.get("error") or "",
            "cached_result": result_data.get("cached_result"),
            **result_data,
        }

    def _execute_recovery_action(
        self,
        action: str,
        params_json: dict[str, Any],
        command_context: dict[str, Any],
    ) -> dict[str, Any]:
        action_name = str(action or "").strip().lower()
        params = dict(params_json or {})

        if action_name == "open_app":
            return self.handle_open_app(str(params.get("app_name") or command_context.get("requested_app") or "").strip())
        if action_name == "search":
            target = str(params.get("target") or params.get("target_app") or command_context.get("target") or "").strip() or None
            query = str(params.get("query") or command_context.get("query") or "").strip()
            return self.handle_search(query, target=target)
        if action_name == "retry_original":
            return self._retry_original_action(command_context)
        if action_name == "retry_command":
            command = str(params.get("command") or "").strip()
            if not command:
                return self._build_result(False, "recovery_retry", "Retry command is unavailable.", error="retry_unavailable")
            return self.process(command, source=str(command_context.get("source") or "text"))
        return self._build_result(
            False,
            "recovery",
            f"Unsupported recovery action: {action_name or 'unknown'}.",
            error="unsupported_recovery_action",
        )

    def _retry_original_action(self, command_context: dict[str, Any]) -> dict[str, Any]:
        intent = str(command_context.get("intent") or command_context.get("detected_intent") or "").strip().lower()
        entities = dict(command_context.get("entities") or {})
        command = str(command_context.get("normalized_input") or command_context.get("command") or command_context.get("raw_input") or "").strip()

        if intent == IntentType.OPEN_APP.value:
            app_name = str(command_context.get("requested_app") or self._extract_primary_app_name(entities) or command).strip()
            return self.handle_open_app(app_name)
        if intent in {IntentType.SEARCH.value, IntentType.SEARCH_WEB.value}:
            query = str(command_context.get("query") or self._resolve_search_query_text(command, entities)).strip()
            target = str(command_context.get("target") or self._extract_search_target(entities) or "").strip() or None
            return self.handle_search(query, target=target)
        return self._build_result(
            False,
            "recovery_retry",
            "Retry is not available for that action.",
            error="retry_unavailable",
            data={"target_app": "recovery"},
        )

    @staticmethod
    def _looks_complex_command(text: str) -> bool:
        lowered = text.lower()
        if len(lowered.split()) >= 10:
            return True
        return any(connector in lowered for connector in (" and ", " then ", " after ", " followed by "))

    @staticmethod
    def _runtime_context_snapshot() -> dict[str, Any]:
        return {
            "current_app": state.current_app,
            "current_context": state.current_context,
            "preferred_browser": settings.get("preferred_browser"),
            "last_successful_action": state.last_successful_action,
            "last_target_app": getattr(state, "last_target_app", ""),
            "last_message_target": getattr(state, "last_message_target", ""),
        }

    @staticmethod
    def _can_resolve_follow_up_locally(text: str) -> bool:
        normalized = normalize_command(text)
        current_app = str(state.current_context or state.current_app or "").strip().lower()
        last_contact = str(getattr(state, "last_message_target", "") or "").strip()
        
        # Check for pending WhatsApp message follow-up
        pending_msg = getattr(state, "pending_whatsapp_message", {})
        if pending_msg and "contact" in pending_msg and not normalized.startswith(("yes", "no", "cancel")):
            # User is giving a message after we asked for one
            return True

        app_follow_up_prefixes = (
            "close it",
            "close this",
            "open it",
            "open this",
            "minimize it",
            "minimize this",
            "maximize it",
            "maximize this",
            "focus it",
            "focus this",
            "toggle it",
            "toggle this",
        )
        if current_app and current_app != "unknown" and any(normalized.startswith(prefix) for prefix in app_follow_up_prefixes):
            return True

        contact_follow_up_prefixes = ("message him ", "message her ", "tell him ", "tell her ", "call him", "call her")
        if last_contact and any(normalized.startswith(prefix) for prefix in contact_follow_up_prefixes):
            return True

        return False

    @staticmethod
    def _is_greeting_wake_phrase(text: str) -> bool:
        normalized = normalize_command(text)
        return normalized in {"hi nova", "hello nova", "hey nova"}

    @staticmethod
    def _is_status_question(text: str) -> bool:
        normalized = normalize_command(text)
        return "how are you" in normalized

    @staticmethod
    def _is_identity_question(text: str) -> bool:
        normalized = normalize_command(text)
        return normalized in {
            "who are you",
            "what is your name",
            "what's your name",
            "tell me your name",
        }

    @staticmethod
    def _is_gratitude_phrase(text: str) -> bool:
        normalized = normalize_command(text)
        return normalized in {"thanks", "thank you", "thanks nova", "thank you nova", "thx"}

    @staticmethod
    def _is_low_information_unknown(text: str) -> bool:
        normalized = normalize_command(text)
        if not normalized:
            return True
        tokens = normalized.split()
        if len(tokens) != 1:
            return False
        token = tokens[0]
        # Only very short ambiguous words - names, vague terms
        return bool(re.fullmatch(r"[a-z]{3,6}", token)) and token not in {"open", "close", "start", "stop", "show", "hide", "play", "pause", "check", "read", "find", "search"}

    def _remember_follow_up_state(
        self,
        result: dict[str, Any],
        *,
        normalized_input: str,
        raw_input: str,
    ) -> None:
        if not bool(result.get("success")):
            return

        result_data = result.get("data", {})
        if not isinstance(result_data, dict):
            result_data = {}

        intent = str(result.get("intent") or "").strip().lower()
        target_app = str(result_data.get("target_app") or "").strip()
        if target_app:
            state.last_target_app = target_app
            state.last_successful_action = f"{intent}:{target_app}"
        elif intent:
            state.last_successful_action = intent
        if result_data.get("contact"):
            state.last_message_target = str(result_data.get("contact"))

        if intent == IntentType.REPEAT_LAST_COMMAND.value:
            return

        replay_command = str(result_data.get("replay_command") or self._derive_replay_command(result) or normalized_input or raw_input).strip()
        if replay_command and replay_command.lower() not in {
            "do the same thing",
            "do that again",
            "same thing again",
            "repeat that",
            "repeat last command",
        }:
            state.last_replayable_command = replay_command

    def _derive_replay_command(self, result: dict[str, Any]) -> str:
        intent = str(result.get("intent") or "").strip().lower()
        data = result.get("data", {})
        if not isinstance(data, dict):
            data = {}

        target_app = str(data.get("target_app") or getattr(state, "last_target_app", "") or "").strip()
        query = str(data.get("query") or "").strip()
        website = str(data.get("website") or "").strip()
        url = str(data.get("url") or "").strip()

        if intent in {
            "open_app",
            "close_app",
            "minimize_app",
            "maximize_app",
            "focus_app",
            "restore_app",
            "toggle_app",
        } and target_app:
            return f"{intent.removesuffix('_app')} {target_app}"
        if intent == "search_web" and query:
            return f"search {query}"
        if intent == "open_website":
            return f"open {website or url}"
        if intent == "browser_tab_close":
            return "close tab"
        if intent == "browser_tab_new":
            return "new tab"
        if intent == "browser_tab_next":
            return "next tab"
        if intent == "browser_tab_previous":
            return "previous tab"
        if intent == "browser_tab_switch" and data.get("tab_index"):
            return f"switch to tab {data['tab_index']}"
        if intent == "browser_action":
            action = str(data.get("action") or data.get("browser_result", {}).get("action") or "").strip().lower()
            replay_map = {
                "go_back": "go back",
                "go_forward": "go forward",
                "refresh": "refresh page",
                "home": "go home",
                "scroll_down": "scroll down",
                "scroll_up": "scroll up",
                "read_page": "read page",
                "read_page_title": "read page title",
                "copy_page_url": "copy page url",
            }
            if action in replay_map:
                return replay_map[action]
        if intent in {"mute", "unmute", "volume_up", "volume_down", "lock_pc"}:
            return intent.replace("_", " ")
        if intent == "set_volume" and data.get("current_state"):
            current_state = data.get("current_state")
            if isinstance(current_state, dict) and current_state.get("percent") is not None:
                return f"set volume to {current_state['percent']}"
        if intent in {"pause_media", "next_track", "previous_track"}:
            return intent.replace("_", " ")
        return ""

    @staticmethod
    def _resolve_search_query_text(text: str, entities: dict[str, Any]) -> str:
        fallback = str(entities.get("query") or text).strip()
        if not text:
            return fallback

        lowered = text.lower()
        for prefix in ("search for ", "search ", "find ", "look up ", "lookup ", "google "):
            if lowered.startswith(prefix):
                return text[len(prefix) :].strip()
        return fallback

    def _prime_runtime_state(self, detection_result: IntentResult) -> None:
        state.last_intent = detection_result.intent.value
        state.last_confidence = detection_result.confidence
        state.last_entities = detection_result.entities

    def _try_skill_execution(
        self,
        text: str,
        rule_result: IntentResult,
        context_decision: ContextDecision,
    ) -> dict[str, Any] | None:
        from core.router import route_command

        direct_intents = {
            IntentType.OPEN_APP.value,
            IntentType.CLOSE_APP.value,
            IntentType.MINIMIZE_APP.value,
            IntentType.MAXIMIZE_APP.value,
            IntentType.FOCUS_APP.value,
            IntentType.RESTORE_APP.value,
            IntentType.TOGGLE_APP.value,
        }
        if rule_result.intent.value in direct_intents:
            return None

        target_route = route_command(rule_result.intent.value, rule_result.entities, text)
        logger.info(
            "[PROCESSOR:ROUTE] Route selected for skill execution",
            intent=rule_result.intent.value,
            route=target_route,
            app=str(rule_result.entities.get("app") or ""),
            app_name=str(rule_result.entities.get("app_name") or ""),
        )
        
        # DEBUG: Log entity extraction
        logger.debug(
            "[PROCESSOR:ROUTE:DETAILS] Full routing context: intent=%s, entities=%s, text='%s'",
            rule_result.intent.value,
            rule_result.entities,
            text,
        )

        # CRITICAL: Add pendingWhatsApp message to context BEFORE skill execution
        pending_msg = getattr(state, "pending_whatsapp_message", {} or {})
        logger.info("[PROCESSOR:PENDING] pending_whatsapp_message: %s", pending_msg)

        # If there's a pending WhatsApp message and route is unknown, route to WhatsApp
        if target_route == "unknown" and pending_msg and pending_msg.get("contact"):
            target_route = "whatsapp"
            logger.info("[PROCESSOR:PENDING] Routing to WhatsApp due to pending message")

        if target_route == "unknown" and rule_result.intent.value in {"click", "click_by_text"}:
            return None

        context = self._build_skill_context(text, rule_result, context_decision)
        context = dict(context)
        context["target_route"] = target_route
        
        # CRITICAL: Add pending WhatsApp message to context
        pending_msg = getattr(state, "pending_whatsapp_message", {})
        if pending_msg:
            context["pending_whatsapp_message"] = pending_msg
            logger.info("[PROCESSOR:PENDING:ADDED] Added pending_whatsapp_message to skill context: %s", pending_msg)
        
        timer = metrics.start_timer("skill_execution", requested_intent=rule_result.intent.value)
        logger.debug(
            "[PROCESSOR:SKILL:FIND] Searching for skill: route='%s', intent='%s'",
            target_route,
            rule_result.intent.value,
        )
        skill_result = self._run_callable_with_timeout(
            lambda: self.skills_manager.execute_with_skill(
                context=context,
                intent=rule_result.intent.value,
                command=text,
            ),
            intent=rule_result.intent.value,
            command_text=text,
            timeout_seconds=self._resolve_timeout_seconds(rule_result.intent.value, route=target_route),
            route=target_route,
            backend="SkillsManager",
            target_app=str(rule_result.entities.get("app") or context_decision.target_app or target_route or ""),
        )
        if skill_result:
            logger.info(
                "[PROCESSOR:SKILL:SELECTED] Skill selected for execution",
                skill_name=skill_result.skill_name,
                intent=rule_result.intent.value,
                route=target_route,
            )
        else:
            logger.info(
                "[PROCESSOR:SKILL:NONE] No skill found for routing",
                intent=rule_result.intent.value,
                route=target_route,
            )
        timer.tags["selected_skill"] = skill_result.skill_name if skill_result is not None else ""
        timer.tags["handled"] = skill_result is not None
        duration_ms = metrics.end_timer(timer)
        if skill_result is None:
            return None
        if skill_result.skill_name:
            metrics.record_duration(
                f"skill_request_latency.{skill_result.skill_name}",
                duration_ms,
                intent=skill_result.intent,
            )
        state.last_response = skill_result.response
        state.last_intent = skill_result.intent
        state.intent_count_by_type[skill_result.intent] = state.intent_count_by_type.get(skill_result.intent, 0) + 1
        if skill_result.success:
            result_data = skill_result.data if isinstance(skill_result.data, dict) else {}
            target_hint = (
                str(result_data.get("target_app") or "").strip()
                or state.current_context
                or state.current_app
                or context_decision.target_app
                or state.last_browser
                or "app"
            )
            state.last_successful_action = ":".join(part for part in (skill_result.intent, target_hint) if part)
        result = skill_result.to_dict()
        result_data = result.get("data", {})
        if not isinstance(result_data, dict):
            result_data = {}
        result_data.setdefault("route", target_route)
        result_data.setdefault("backend", skill_result.skill_name or target_route)
        result["data"] = result_data
        return result

    def _build_skill_context(
        self,
        text: str,
        rule_result: IntentResult,
        context_decision: ContextDecision,
    ) -> dict[str, Any]:
        return {
            "command": text,
            "intent": rule_result.intent.value,
            "entities": dict(rule_result.entities),
            "current_app": state.current_context or state.current_app,
            "current_context": state.current_context,
            "current_process_name": state.current_process_name,
            "current_window_title": state.current_window_title,
            "preferred_browser": settings.get("preferred_browser"),
            "context_target_app": context_decision.target_app if context_decision else "",
            "context_resolved_intent": context_decision.resolved_intent if context_decision else "",
            "context_confidence": context_decision.confidence if context_decision else 0.0,
            "context_decision": context_decision.to_dict() if context_decision else {},
            "last_skill_used": state.last_skill_used,
            "last_successful_action": state.last_successful_action,
            "youtube_active": getattr(state, "youtube_active", False),
            "last_media_action": getattr(state, "last_media_action", ""),
            "last_youtube_query": getattr(state, "last_youtube_query", ""),
            "whatsapp_active": getattr(state, "whatsapp_active", False),
            "last_contact_search": dict(getattr(state, "last_contact_search", {}) or {}),
            "pending_contact_choices": list(getattr(state, "pending_contact_choices", []) or []),
            "pending_whatsapp_message": dict(getattr(state, "pending_whatsapp_message", {}) or {}),
            "last_message_target": getattr(state, "last_message_target", ""),
            "last_chat_name": getattr(state, "last_chat_name", ""),
            "music_active": getattr(state, "music_active", False),
            "active_music_provider": getattr(state, "active_music_provider", ""),
            "last_track_name": getattr(state, "last_track_name", ""),
            "last_artist_name": getattr(state, "last_artist_name", ""),
            "ocr_ready": getattr(state, "ocr_ready", False),
            "last_ocr_text": getattr(state, "last_ocr_text", ""),
            "last_ocr_engine": getattr(state, "last_ocr_engine", ""),
            "last_screenshot_path": getattr(state, "last_screenshot_path", ""),
            "last_text_matches": list(getattr(state, "last_text_matches", []) or []),
            "awareness_ready": getattr(state, "awareness_ready", False),
            "last_awareness_report": dict(getattr(state, "last_awareness_report", {}) or {}),
            "last_desktop_snapshot": dict(getattr(state, "last_desktop_snapshot", {}) or {}),
            "last_visible_apps": list(getattr(state, "last_visible_apps", []) or []),
            "last_text_click_target": dict(getattr(state, "last_text_click_target", {}) or {}),
            "last_text_click_result": dict(getattr(state, "last_text_click_result", {}) or {}),
            "last_clicked_position": dict(getattr(state, "last_clicked_position", {}) or {}),
            "text_click_count": int(getattr(state, "text_click_count", 0) or 0),
            "last_file_action": getattr(state, "last_file_action", ""),
            "last_file_path": getattr(state, "last_file_path", ""),
            "last_destination_path": getattr(state, "last_destination_path", ""),
            "last_system_action": dict(getattr(state, "last_system_action", {}) or {}),
            "last_volume": int(getattr(state, "last_volume", -1) or -1),
            "last_brightness": int(getattr(state, "last_brightness", -1) or -1),
            "wifi_state": getattr(state, "wifi_state", "unknown"),
            "bluetooth_state": getattr(state, "bluetooth_state", "unknown"),
            "last_file_search_query": dict(getattr(state, "last_file_search_query", {}) or {}),
            "last_file_search_results": list(getattr(state, "last_file_search_results", []) or []),
            "pending_file_choices": list(getattr(state, "pending_file_choices", []) or []),
            "file_index_ready": bool(getattr(state, "file_index_ready", False)),
            "pending_confirmation": dict(getattr(state, "pending_confirmation", {}) or {}),
            "recent_files_touched": list(getattr(state, "recent_files_touched", []) or []),
            "clipboard_ready": bool(getattr(state, "clipboard_ready", False)),
            "last_clipboard_item": dict(getattr(state, "last_clipboard_item", {}) or {}),
            "clipboard_count": int(getattr(state, "clipboard_count", 0) or 0),
            "pending_clipboard_choices": list(getattr(state, "pending_clipboard_choices", []) or []),
            "scheduler_running": bool(getattr(state, "scheduler_running", False)),
            "next_reminder_time": getattr(state, "next_reminder_time", ""),
            "last_triggered_reminder": dict(getattr(state, "last_triggered_reminder", {}) or {}),
            "reminder_count": int(getattr(state, "reminder_count", 0) or 0),
        }

    @staticmethod
    def _parse_plugin_management_command(text: str) -> tuple[str, str] | None:
        normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
        if normalized in {"list plugins", "show plugins", "plugin list", "plugins", "installed plugins"}:
            return "list", ""
        if normalized in {"plugin health", "plugin health check", "check plugins", "check plugin health"}:
            return "health", ""
        match = re.match(r"^(enable|disable|reload)\s+(.+?)(?:\s+plugin)?$", normalized)
        if match:
            return match.group(1), match.group(2).strip()
        match = re.match(r"^plugin\s+(enable|disable|reload)\s+(.+)$", normalized)
        if match:
            return match.group(1), match.group(2).strip()
        return None

    def _handle_file_action_entities(self, entities: dict[str, Any]) -> dict[str, Any]:
        reference = (
            str(entities.get("reference") or "").strip()
            or str(entities.get("source_path") or "").strip()
            or str(entities.get("path") or "").strip()
            or str(entities.get("filename") or "").strip()
        )
        if self.file_skill is None:
            return self._build_result(False, "file_action", "File management is unavailable.")

        result = self._run_callable_with_timeout(
            lambda: self.file_skill.execute_operation(
                str(entities.get("action") or "unknown").strip().lower(),
                reference=reference,
                destination=str(entities.get("destination") or entities.get("target_path") or "").strip(),
                new_name=str(entities.get("new_name") or "").strip(),
                location=str(entities.get("location") or "").strip(),
                source_location=str(entities.get("source_location") or "").strip(),
                permanent=bool(entities.get("permanent")),
                content=entities.get("content"),
                overwrite=bool(entities.get("overwrite")),
            ),
            intent=IntentType.FILE_ACTION.value,
            command_text=reference or str(entities.get("action") or "file action"),
            timeout_seconds=self._resolve_timeout_seconds(IntentType.FILE_ACTION.value, route="files"),
            route="files",
            backend="FileSkill",
            target_app="files",
        )
        state.last_response = result.response
        state.last_intent = result.intent
        state.intent_count_by_type[result.intent] = state.intent_count_by_type.get(result.intent, 0) + 1
        if result.success:
            state.last_successful_action = ":".join(part for part in (result.intent, "files") if part)
        return result.to_dict()

    def _build_result(
        self,
        success: bool,
        intent: str,
        response: str,
        *,
        error: str = "",
        data: dict[str, Any] | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        state.last_response = response
        payload = dict(data or {})
        payload.setdefault("category", infer_command_category(intent, explicit=category or ""))
        action_result = ensure_action_result(
            {
                "success": success,
                "action": intent,
                "target": payload.get("target_app"),
                "message": response,
                "error_code": error or None,
                "verified": payload.get("verified", False),
                "duration_ms": payload.get("duration_ms", 0),
                "data": payload,
            },
            default_action=intent,
            default_target=str(payload.get("target_app") or "").strip() or None,
        )
        success = bool(action_result.get("success", success))
        if not success and not error:
            error = str(action_result.get("error_code") or "")

        if success and intent != "multi_action":
            target_hint = self._extract_search_target(state.last_entities) or state.current_context or state.current_app
            state.last_successful_action = ":".join(part for part in (intent, target_hint) if part)

        result = ensure_command_result(
            {
                "success": success,
                "intent": intent,
                "category": payload.get("category"),
                "message": response,
                "error_code": error or None,
                "verified": bool(action_result.get("verified")),
                "duration_ms": int(action_result.get("duration_ms") or payload.get("duration_ms", 0) or 0),
                "data": payload,
                "action_result": action_result,
            }
        )

        logger.info(
            "Intent routed",
            intent=intent,
            category=result.get("category"),
            success=bool(result.get("success")),
            error=str(result.get("error") or ""),
            response=response,
            verified=bool(result.get("verified")),
        )

        return result

    def _attach_command_trace(
        self,
        result: dict[str, Any],
        *,
        raw_input: str,
        normalized_input: str,
        detected_intent: str,
        selected_skill: str,
        duration_ms: float,
    ) -> dict[str, Any]:
        payload = dict(result)
        result_data = payload.get("data", {})
        if not isinstance(result_data, dict):
            result_data = {}
        action_result = payload.get("action_result", {})
        if not isinstance(action_result, dict):
            action_result = {}

        route = str(result_data.get("route") or result_data.get("attempted_route") or result_data.get("backend") or "").strip()
        backend = str(result_data.get("backend") or result_data.get("attempted_backend") or selected_skill or "").strip()
        resolved_skill = str(selected_skill or result_data.get("attempted_selected_skill") or "").strip()
        trace = {
            "trace_id": str(action_result.get("trace_id") or result_data.get("trace_id") or ""),
            "raw_input": raw_input,
            "normalized_input": normalized_input,
            "detected_intent": detected_intent or str(payload.get("intent") or ""),
            "final_intent": str(payload.get("intent") or ""),
            "entities": dict(getattr(state, "last_entities", {}) or {}),
            "route": route,
            "backend": backend,
            "selected_skill": resolved_skill,
            "action": str(action_result.get("action") or ""),
            "target": str(action_result.get("target") or result_data.get("target_app") or ""),
            "verified": bool(action_result.get("verified", False)),
            "latency_ms": round(float(duration_ms or 0.0), 2),
            "success": bool(payload.get("success", False)),
            "error_reason": str(payload.get("error") or action_result.get("error_code") or ""),
            "fallback_triggered": bool(result_data.get("recovery_attempted") or result_data.get("fallback_from")),
        }
        result_data["trace"] = trace
        if route:
            result_data.setdefault("route", route)
        if backend:
            result_data.setdefault("backend", backend)
        payload["data"] = result_data
        logger.info("Command trace", **trace)
        logger.info("[Routing]", input=raw_input, corrected=normalized_input, intent=trace["detected_intent"], entities=trace["entities"], route=trace["route"], skill=trace["selected_skill"], fallback_triggered=trace["fallback_triggered"])
        return payload

    @staticmethod
    def _coerce_result(result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            payload = dict(result)
            payload["action_result"] = ensure_action_result(
                payload,
                default_action=str(payload.get("intent") or ""),
                default_target=str((payload.get("data") or {}).get("target_app") or "").strip() or None
                if isinstance(payload.get("data"), dict)
                else None,
            )
            payload["success"] = bool(payload["action_result"].get("success", payload.get("success", False)))
            if not payload["success"] and not payload.get("error"):
                payload["error"] = str(payload["action_result"].get("error_code") or "")
            return ensure_command_result(payload)
        if hasattr(result, "to_dict"):
            payload = result.to_dict()
            if isinstance(payload, dict):
                payload["action_result"] = ensure_action_result(
                    payload,
                    default_action=str(payload.get("intent") or ""),
                    default_target=str((payload.get("data") or {}).get("target_app") or "").strip() or None
                    if isinstance(payload.get("data"), dict)
                    else None,
                )
                payload["success"] = bool(payload["action_result"].get("success", payload.get("success", False)))
                if not payload["success"] and not payload.get("error"):
                    payload["error"] = str(payload["action_result"].get("error_code") or "")
                return ensure_command_result(payload)
        payload = {
            "success": False,
            "intent": "error",
            "response": str(result),
            "error": "invalid_result",
            "data": {},
        }
        payload["action_result"] = ensure_action_result(payload, default_action="error")
        return ensure_command_result(payload)

    @staticmethod
    def _extract_selected_skill(result: dict[str, Any]) -> str:
        if not isinstance(result, dict):
            return ""
        for key in ("skill_name", "skill", "selected_skill"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        result_data = result.get("data", {})
        if isinstance(result_data, dict):
            for key in ("skill_name", "skill", "selected_skill"):
                value = result_data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    @staticmethod
    def _extract_action_taken(result: dict[str, Any]) -> str:
        if not isinstance(result, dict):
            return ""
        action_result = result.get("action_result", {})
        if isinstance(action_result, dict):
            value = action_result.get("action")
            if isinstance(value, str) and value.strip():
                return value.strip()
        result_data = result.get("data", {})
        if isinstance(result_data, dict):
            for key in ("action_taken", "action", "operation"):
                value = result_data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        intent = result.get("intent")
        if isinstance(intent, str) and intent.strip():
            return intent.strip()
        error = result.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
        return ""

    def _record_request_performance_breakouts(
        self,
        duration_ms: float,
        result: dict[str, Any],
        selected_skill: str,
    ) -> None:
        if duration_ms <= 0:
            return
        intent = str(result.get("intent", "")).strip().lower()
        if selected_skill:
            metrics.record_duration(f"request_latency.{selected_skill}", duration_ms, intent=intent)
        if selected_skill == "OCRSkill" or intent.startswith("ocr"):
            metrics.record_duration("ocr_request_latency", duration_ms, intent=intent)
        if selected_skill == "FileSkill" or intent.startswith("file_") or intent == "file_action":
            metrics.record_duration("file_operation_latency", duration_ms, intent=intent)

    def _record_feature_usage(self, result: dict[str, Any], selected_skill: str, source: str) -> None:
        intent = str(result.get("intent", "")).strip()
        if intent:
            analytics.increment_feature(f"intent:{intent}")
        if selected_skill:
            analytics.increment_feature(f"skill:{selected_skill}")
        if source:
            analytics.increment_feature(f"source:{source}")

    @staticmethod
    def _serialize_command_context(
        *,
        raw_input: str,
        normalized_input: str,
        detected_intent: str,
        source: str,
    ) -> str:
        return json.dumps(
            {
                "raw_input": raw_input,
                "normalized_input": normalized_input,
                "detected_intent": detected_intent,
                "source": source,
            },
            ensure_ascii=False,
        )
