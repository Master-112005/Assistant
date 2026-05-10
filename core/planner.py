"""
Action planning engine - converts user requests into ordered executable steps.

Pure rule-based approach using pattern matching and semantic analysis.
"""
from __future__ import annotations

import re
from typing import Optional, List, Dict, Any, Set

from core.plan_models import (
    ActionType,
    PlanStep,
    ExecutionPlan,
    PlannerContext,
)
from core.logger import get_logger
from core import entities, parser, state
from core.app_commands import parse_app_command
from core.app_launcher import canonicalize_app_name

logger = get_logger(__name__)


CONNECTOR_PATTERN = re.compile(
    r'\s+(and|then|after\s+that|next|also|followed\s+by)\s+|,\s*',
    re.IGNORECASE
)
IMPLICIT_ACTION_PATTERN = re.compile(
    r"\b(?:open|close|minimize|maximize|restore|focus|switch\s+to|activate|search|look\s+for|look\s+up|find|google|play|click|type|say|tell|send\s+(?:a\s+)?message|message|download|save|copy|paste)\b",
    re.IGNORECASE,
)


class ActionPlanner:
    """
    Rule-based action planner that converts natural language commands into structured plans.
    
    Strategy:
    1. Normalize input
    2. Segment into commands
    3. Extract actions using pattern matching
    4. Resolve dependencies
    5. Validate plan
    6. Return structured execution plan
    """

    def __init__(self):
        self.step_id_counter = 0
        
        self.app_open_patterns = [
            (r'\bopen\s+(\w+)', 'open_app'),
            (r'\blaunch\s+(\w+)', 'open_app'),
            (r'\bstart\s+(\w+)', 'open_app'),
            (r'\brun\s+(\w+)', 'open_app'),
            (r'\bgo to\s+(\w+)', 'open_app'),
            (r'\bnavigate to\s+(\w+)', 'open_app'),
        ]

        self.app_close_patterns = [
            (r'\bclose\s+(\w+)', 'close_app'),
            (r'\bquit\s+(\w+)', 'close_app'),
            (r'\bexit\s+(\w+)', 'close_app'),
            (r'\bkill\s+(\w+)', 'close_app'),
            (r'\bterminate\s+(\w+)', 'close_app'),
        ]
        
        self.search_patterns = [
            (r'\bsearch\s+(?:for\s+)?(.+?)(?:\s+on\s+(\w+))?$', 'search'),
            (r'\blook\s+(?:for\s+)?(?:up\s+)?(.+?)(?:\s+on\s+(\w+))?$', 'search'),
            (r'\bfind\s+(?:(?:information\s+)?(?:on|about)\s+)?(.+?)(?:\s+on\s+(\w+))?$', 'search'),
            (r'\bgoogle\s+(.+)$', 'search'),
            (r'\bsearch on\s+(\w+)\s+(.+)$', 'search'),
        ]
        
        self.play_patterns = [
            (r'\bplay\s+(?:the\s+)?(?:first\s+)?(?:song|music|video|track)\b', 'play'),
            (r'\bplay\s+(.+?)\s+(?:song|music|video|track)\b', 'play'),
            (r'\bplay\s+(.+)$', 'play'),
            (r'\bplay some\s+(.+)$', 'play'),
            (r'\bput on\s+(.+)$', 'play'),
            (r'\bturn on\s+(.+)$', 'play'),
        ]
        
        self.click_patterns = [
            (r'\bclick\s+(?:on\s+)?(.+)', 'click'),
        ]
        
        self.message_patterns = [
            (r'\bsay\s+(hi|hello|hey)\s+(?:to\s+)?(.+)', 2, 1),
            (r'\bsay\s+(hi|hello|hey)\s+(\w+)', 2, 1),
            (r'\bsay\s+(.+?)\s+to\s+(.+)', 2, 1),
            (r'\bsay\s+(.+)\s+(\w+)', 2, 1),
            (r'\bhi\s+to\s+(.+)', 2, 1),
            (r'\bhello\s+to\s+(.+)', 2, 1),
            (r'\bhey\s+to\s+(.+)', 2, 1),
            (r'\bmsg\s+(.+?)\s+to\s+(.+)', 2, 1),
            (r'\bmsg\s+(\w+)\s+(.+)', 1, 2),
            (r'\btell\s+(.+?)\s+(?:that\s+)?(.+)', 1, 2),
            (r'\btext\s+(.+?)\s*:\s*(.+)', 1, 2),
            (r'\btext\s+(\w+)\s+(.+)', 1, 2),
            (r'\bping\s+(\w+)\s+(.+)', 1, 2),
            (r'\bsend\s+(?:a\s+)?message\s+to\s+(.+?)\s*:\s*(.+)', 1, 2),
            (r'\bsend\s+(?:a\s+)?message\s+(?:to\s+)?(\w+)\s+(.+)', 1, 2),
            (r'\bmessage\s+(.+?)\s*:\s*(.+)', 1, 2),
            (r'\bmessage\s+(\w+)\s+(.+)', 1, 2),
            (r'\bnotify\s+(\w+)\s+(.+)', 1, 2),
            (r'\binform\s+(\w+)\s+(.+)', 1, 2),
        ]
        
        self.type_patterns = [
            (r'\btype\s+(.+)', 'type'),
        ]
        
        self.system_control_patterns = [
            (r'\b(?:turn|set)\s+(?:up|down|increase|decrease|raise|lower|louder|quieter|mute|unmute)\s+(.+)', 'system_control'),
            (r'\b(?:volume|brightness|wifi|bluetooth|shutdown|sleep|lock)\s+(?:up|down|on|off|increase|decrease)', 'system_control'),
        ]
        
        self.navigate_patterns = [
            (r'\bgo\s+to\s+(.+)', 'navigate'),
            (r'\bnavigate\s+to\s+(.+)', 'navigate'),
            (r'\bvisit\s+(.+)', 'navigate'),
        ]
        
        self.download_patterns = [
            (r'\bdownload\s+(.+)', 'download'),
            (r'\bsave\s+(.+)', 'download'),
        ]
        
        self.window_patterns = [
            (r'\bminimize\s+(\w+)', 'minimize_app'),
            (r'\bhide\s+(\w+)', 'minimize_app'),
            (r'\bmaximize\s+(\w+)', 'maximize_app'),
            (r'\bexpand\s+(\w+)', 'maximize_app'),
            (r'\benlarge\s+(\w+)', 'maximize_app'),
            (r'\brestore\s+(\w+)', 'restore_app'),
            (r'\bunminimize\s+(\w+)', 'restore_app'),
            (r'\bfocus\s+(\w+)', 'focus_app'),
            (r'\bswitch to\s+(\w+)', 'focus_app'),
            (r'\bactivate\s+(\w+)', 'focus_app'),
        ]
        
        self.copy_patterns = [
            (r'\bcopy\s+(.+)', 'copy'),
            (r'\bcopy\s+the\s+(.+)', 'copy'),
        ]
        
        self.paste_patterns = [
            (r'\bpaste\s+(.+)', 'paste'),
            (r'\bpaste\s+that\s+(.+)', 'paste'),
        ]
        
        logger.info("ActionPlanner initialized (rules-only)")

    def plan(
        self,
        text: str,
        context: Optional[PlannerContext] = None,
        context_hints: Optional[Dict[str, Any]] = None,
    ) -> ExecutionPlan:
        if not text or not text.strip():
            logger.warning("Empty input text for planner")
            return self._empty_plan(text)

        normalized = self.normalize(text)
        logger.info("Planning: %s", normalized)

        steps: List[PlanStep] = []
        planner_used = "rules"

        if context_hints and context_hints.get("steps"):
            steps = self._extract_steps_from_context_hints(context_hints["steps"], normalized)
            planner_used = "context"
            logger.info("Using %d context-enriched planner steps", len(steps))
        else:
            segments = self.split_commands(normalized)
            logger.info("Segments found: %d", len(segments))
            steps = self._extract_steps_rules(segments)
            planner_used = "rules"

        steps = self.resolve_dependencies(steps, context)

        plan = ExecutionPlan(
            original_text=text,
            normalized_text=normalized,
            steps=steps,
            confidence=self._calculate_confidence(steps),
            planner_used=planner_used,
            has_dependencies=any(s.depends_on for s in steps),
        )

        if not self.validate_plan(plan):
            logger.warning("Plan validation failed, returning single unknown step")
            plan = self._unknown_plan(text)

        logger.info("Plan generated: %d steps, confidence=%.2f, planner=%s",
                    plan.step_count, plan.confidence, planner_used)

        state.last_plan = plan
        state.last_plan_steps = plan.step_count
        state.last_plan_confidence = plan.confidence
        state.last_planner_used = planner_used

        return plan

    def normalize(self, text: str) -> str:
        cleaned = parser.clean_text(text)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return cleaned

    def split_commands(self, text: str) -> List[str]:
        parts = CONNECTOR_PATTERN.split(text)
        explicit_segments = [parts[i].strip() for i in range(0, len(parts), 2)]
        segments: List[str] = []
        for segment in explicit_segments:
            segments.extend(self._split_implicit_commands(segment))
        segments = [s for s in segments if s]
        logger.debug("Split into segments: %s", segments)
        return segments

    def _split_implicit_commands(self, segment: str) -> List[str]:
        cleaned = segment.strip(" ,")
        if not cleaned:
            return []

        whole_step = self._extract_step_from_segment(cleaned, 1)
        if whole_step is None or whole_step.action not in {
            ActionType.OPEN_APP,
            ActionType.CLOSE_APP,
            ActionType.APP_ACTION,
        }:
            return [cleaned]

        for match in IMPLICIT_ACTION_PATTERN.finditer(cleaned):
            if match.start() == 0:
                continue

            prefix = cleaned[:match.start()].strip(" ,")
            suffix = cleaned[match.start():].strip(" ,")
            if not prefix or not suffix:
                continue

            prefix_step = self._extract_step_from_segment(prefix, 1)
            suffix_step = self._extract_step_from_segment(suffix, 2)
            if prefix_step is None or suffix_step is None:
                continue
            if prefix_step.action == ActionType.UNKNOWN or suffix_step.action == ActionType.UNKNOWN:
                continue
            if prefix_step.action not in {
                ActionType.OPEN_APP,
                ActionType.CLOSE_APP,
                ActionType.APP_ACTION,
            }:
                continue

            return [
                *self._split_implicit_commands(prefix),
                *self._split_implicit_commands(suffix),
            ]

        return [cleaned]

    def _extract_steps_rules(self, segments: List[str]) -> List[PlanStep]:
        steps: List[PlanStep] = []
        for i, segment in enumerate(segments):
            step = self._extract_step_from_segment(segment, i + 1)
            if step:
                steps.append(step)
        return steps

    def _extract_steps_from_context_hints(
        self,
        hint_steps: List[Dict[str, Any]],
        source_text: str,
    ) -> List[PlanStep]:
        steps: List[PlanStep] = []
        for order, step_data in enumerate(hint_steps, 1):
            if not isinstance(step_data, dict):
                continue
            action_name = str(step_data.get("action", "unknown")).upper().replace(" ", "_")
            action = ActionType[action_name] if action_name in ActionType.__members__ else ActionType.UNKNOWN
            step = self._create_step(
                order=step_data.get("order", order),
                action=action,
                target=str(step_data.get("target", "")),
                params=step_data.get("params", {}) if isinstance(step_data.get("params"), dict) else {},
                depends_on=step_data.get("depends_on", []) if isinstance(step_data.get("depends_on"), list) else [],
                source_text=source_text,
            )
            step.requires_confirmation = bool(step_data.get("requires_confirmation", False))
            step.estimated_risk = str(step_data.get("estimated_risk", step.estimated_risk) or step.estimated_risk)
            steps.append(step)
        return steps

    def _extract_step_from_segment(self, segment: str, order: int) -> Optional[PlanStep]:
        segment_text = segment.strip()
        segment_lower = segment_text.lower()

        parsed_app = parse_app_command(segment_text)
        if parsed_app is not None:
            intent_to_action = {
                "open_app": ActionType.OPEN_APP,
                "close_app": ActionType.CLOSE_APP,
                "minimize_app": ActionType.APP_ACTION,
                "maximize_app": ActionType.APP_ACTION,
                "focus_app": ActionType.APP_ACTION,
                "restore_app": ActionType.APP_ACTION,
                "toggle_app": ActionType.APP_ACTION,
            }
            action = intent_to_action.get(parsed_app.intent)
            if action == ActionType.OPEN_APP:
                return self._create_step(
                    order=order,
                    action=ActionType.OPEN_APP,
                    target=parsed_app.app_name,
                    source_text=segment_text,
                )
            if action == ActionType.CLOSE_APP:
                return self._create_step(
                    order=order,
                    action=ActionType.CLOSE_APP,
                    target=parsed_app.app_name,
                    source_text=segment_text,
                )
            if action == ActionType.APP_ACTION:
                return self._create_step(
                    order=order,
                    action=ActionType.APP_ACTION,
                    target=parsed_app.app_name,
                    params={"operation": parsed_app.intent.replace("_app", "")},
                    source_text=segment_text,
                )

        for pattern, action_name in self.app_open_patterns:
            match = re.search(pattern, segment_text, re.IGNORECASE)
            if match:
                app_name = match.group(1).strip()
                return self._create_step(
                    order=order,
                    action=ActionType.OPEN_APP,
                    target=app_name,
                    source_text=segment_text,
                )

        for pattern, action_name in self.app_close_patterns:
            match = re.search(pattern, segment_text, re.IGNORECASE)
            if match:
                app_name = match.group(1).strip()
                return self._create_step(
                    order=order,
                    action=ActionType.CLOSE_APP,
                    target=app_name,
                    source_text=segment_text,
                )

        # Window management (minimize, maximize, restore, focus)
        for pattern, action_name in self.window_patterns:
            match = re.search(pattern, segment_text, re.IGNORECASE)
            if match:
                app_name = match.group(1).strip()
                action_map = {
                    'minimize_app': ActionType.APP_ACTION,
                    'maximize_app': ActionType.APP_ACTION,
                    'restore_app': ActionType.APP_ACTION,
                    'focus_app': ActionType.APP_ACTION,
                }
                return self._create_step(
                    order=order,
                    action=action_map.get(action_name, ActionType.APP_ACTION),
                    target=app_name,
                    params={'operation': action_name.replace('_app', '')},
                    source_text=segment_text,
                )

        for pattern, action_name in self.search_patterns:
            match = re.search(pattern, segment_text, re.IGNORECASE)
            if match:
                query = match.group(1).strip()
                target = match.group(2).strip() if match.lastindex >= 2 else ""
                return self._create_step(
                    order=order,
                    action=ActionType.SEARCH,
                    target=target or "default",
                    params={"query": query},
                    source_text=segment_text,
                )

        for pattern, action_name in self.play_patterns:
            match = re.search(pattern, segment_text, re.IGNORECASE)
            if match:
                target = match.group(1).strip() if match.lastindex and match.lastindex >= 1 else "first_result"
                selection = 1 if "first" in segment_lower else -1
                return self._create_step(
                    order=order,
                    action=ActionType.PLAY,
                    target=target,
                    params={"selection": selection},
                    source_text=segment_text,
                )

        for pattern, action_name in self.click_patterns:
            match = re.search(pattern, segment_text, re.IGNORECASE)
            if match:
                target = match.group(1).strip()
                return self._create_step(
                    order=order,
                    action=ActionType.CLICK,
                    target=target,
                    source_text=segment_text,
                )

        # Message/send patterns
        for pattern, recipient_group, message_group in self.message_patterns:
            match = re.search(pattern, segment_text, re.IGNORECASE)
            if match:
                recipient = match.group(recipient_group).strip()
                message = match.group(message_group).strip()
                return self._create_step(
                    order=order,
                    action=ActionType.SEND_MESSAGE,
                    target=recipient,
                    params={"message": message, "contact": recipient},
                    source_text=segment_text,
                )

        for pattern, action_name in self.type_patterns:
            match = re.search(pattern, segment_text, re.IGNORECASE)
            if match:
                text_to_type = match.group(1).strip()
                return self._create_step(
                    order=order,
                    action=ActionType.TYPE,
                    params={"text": text_to_type},
                    source_text=segment_text,
                )

        for pattern, action_name in self.system_control_patterns:
            match = re.search(pattern, segment_text, re.IGNORECASE)
            if match:
                parsed = entities.extract_system_control(segment_text)
                return self._create_step(
                    order=order,
                    action=ActionType.SYSTEM_CONTROL,
                    target=str(parsed.get("control") or (match.group(1).strip() if match.lastindex and match.lastindex >= 1 else "unknown")),
                    params={key: value for key, value in parsed.items() if key != "control"},
                    source_text=segment_text,
                )

        parsed_system = entities.extract_system_control(segment_text)
        if parsed_system.get("action"):
            return self._create_step(
                order=order,
                action=ActionType.SYSTEM_CONTROL,
                target=str(parsed_system.get("control") or "unknown"),
                params={key: value for key, value in parsed_system.items() if key != "control"},
                source_text=segment_text,
            )

        for pattern, action_name in self.navigate_patterns:
            match = re.search(pattern, segment_text, re.IGNORECASE)
            if match:
                destination = match.group(1).strip()
                return self._create_step(
                    order=order,
                    action=ActionType.SEARCH,
                    target="default",
                    params={"query": destination},
                    source_text=segment_text,
                )

        for pattern, action_name in self.download_patterns:
            match = re.search(pattern, segment_text, re.IGNORECASE)
            if match:
                target = match.group(1).strip()
                return self._create_step(
                    order=order,
                    action=ActionType.FILE_ACTION,
                    target=target,
                    params={"operation": "download"},
                    source_text=segment_text,
                )

        return self._create_step(
            order=order,
            action=ActionType.UNKNOWN,
            target=segment_text,
            source_text=segment_text,
        )

    def resolve_dependencies(
        self,
        steps: List[PlanStep],
        context: Optional[PlannerContext] = None,
    ) -> List[PlanStep]:
        if not steps:
            return steps

        for i, step in enumerate(steps):
            if step.action == ActionType.SEARCH:
                if step.target and step.target != "default":
                    for j in range(i):
                        if (steps[j].action == ActionType.OPEN_APP and 
                            self._app_matches(steps[j].target, step.target)):
                            step.depends_on = [steps[j].id]
                            break
                else:
                    for j in range(i - 1, -1, -1):
                        if steps[j].action == ActionType.OPEN_APP:
                            step.depends_on = [steps[j].id]
                            break

            elif step.action == ActionType.PLAY:
                preceding_app = self._nearest_preceding_open_app(steps, i)
                if preceding_app in {"youtube", "spotify", "music"}:
                    self._bind_play_step_to_app(step, preceding_app)
                    for j in range(i - 1, -1, -1):
                        if steps[j].action == ActionType.OPEN_APP and self._app_matches(steps[j].target, preceding_app):
                            step.depends_on = [steps[j].id]
                            break
                    continue

                for j in range(i - 1, -1, -1):
                    if steps[j].action == ActionType.SEARCH:
                        step.depends_on = [steps[j].id]
                        break

            elif step.action == ActionType.APP_ACTION:
                operation = str(step.params.get("operation", "")).lower()
                if operation in {"open_result", "select_result"}:
                    for j in range(i - 1, -1, -1):
                        if steps[j].action == ActionType.SEARCH:
                            step.depends_on = [steps[j].id]
                            break

            elif step.action == ActionType.CLICK:
                for j in range(i - 1, -1, -1):
                    if steps[j].action in (ActionType.OPEN_APP, ActionType.SEARCH):
                        step.depends_on = [steps[j].id]
                        break

            elif step.action == ActionType.SEND_MESSAGE:
                preceding_app = self._nearest_preceding_open_app(steps, i)
                if preceding_app in {"whatsapp", "telegram", "discord", "slack", "teams"}:
                    step.params = {
                        **step.params,
                        "app": preceding_app,
                        "target_app": preceding_app,
                        "contact": step.target or step.params.get("contact", ""),
                    }
                    for j in range(i - 1, -1, -1):
                        if steps[j].action == ActionType.OPEN_APP and self._app_matches(steps[j].target, preceding_app):
                            step.depends_on = [steps[j].id]
                            break
                else:
                    step.params = {
                        **step.params,
                        "app": str(step.params.get("app") or "whatsapp"),
                        "target_app": str(step.params.get("target_app") or "whatsapp"),
                        "contact": step.target or step.params.get("contact", ""),
                    }

        return steps

    def validate_plan(self, plan: ExecutionPlan) -> bool:
        if not plan.is_valid:
            return False

        if self._has_circular_dependency(plan.steps):
            logger.warning("Plan has circular dependencies")
            return False

        step_ids = {s.id for s in plan.steps}
        for step in plan.steps:
            for dep_id in step.depends_on:
                if dep_id not in step_ids:
                    logger.warning("Step %s references non-existent dependency %s", step.id, dep_id)
                    return False

        if not self._is_topologically_sorted(plan.steps):
            logger.debug("Reordering steps for topological sort")
            plan.steps = self._topological_sort(plan.steps)

        return True

    def _create_step(
        self,
        order: int,
        action: ActionType,
        target: str = "",
        params: Optional[Dict[str, Any]] = None,
        depends_on: Optional[List[str]] = None,
        source_text: str = "",
    ) -> PlanStep:
        self.step_id_counter += 1
        return PlanStep(
            id=f"step_{self.step_id_counter}",
            order=order,
            action=action,
            target=target,
            params=params or {},
            depends_on=depends_on or [],
            source_text=source_text,
        )

    def _calculate_confidence(self, steps: List[PlanStep]) -> float:
        if not steps:
            return 0.0

        unknown_count = sum(1 for s in steps if s.action == ActionType.UNKNOWN)
        unknown_penalty = unknown_count * 0.2

        base_confidence = 0.85

        confidence = base_confidence - unknown_penalty
        return max(0.1, min(1.0, confidence))

    def _app_matches(self, app1: str, app2: str) -> bool:
        a1 = canonicalize_app_name(app1).lower().replace(" ", "")
        a2 = canonicalize_app_name(app2).lower().replace(" ", "")
        return a1 == a2 or a1.startswith(a2) or a2.startswith(a1)

    @staticmethod
    def _nearest_preceding_open_app(steps: list[PlanStep], current_index: int) -> str:
        for prior in reversed(steps[:current_index]):
            if prior.action != ActionType.OPEN_APP:
                continue
            app = canonicalize_app_name(prior.target)
            if app:
                return app
        return ""

    @staticmethod
    def _bind_play_step_to_app(step: PlanStep, app_name: str) -> None:
        query = str(step.target or step.params.get("query") or "").strip()
        selection = max(1, int(step.params.get("selection", 1) or 1))
        app = canonicalize_app_name(app_name)

        if app == "youtube":
            step.params = {
                **step.params,
                "target_app": "youtube",
                "app": "youtube",
                "query": "" if query == "first_result" else query,
                "selection": selection,
                "result_index": selection,
                "autoplay": True,
            }
            return

        if app in {"spotify", "music"}:
            step.params = {
                **step.params,
                "target_app": "spotify",
                "app": "spotify",
                "query": "" if query == "first_result" else query,
                "selection": selection,
                "result_index": selection,
            }

    def _has_circular_dependency(self, steps: List[PlanStep]) -> bool:
        if not steps:
            return False

        graph: Dict[str, List[str]] = {s.id: s.depends_on for s in steps}

        def has_cycle(node: str, visited: Set[str], rec_stack: Set[str]) -> bool:
            visited.add(node)
            rec_stack.add(node)

            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    if has_cycle(neighbor, visited, rec_stack):
                        return True
                elif neighbor in rec_stack:
                    return True

            rec_stack.remove(node)
            return False

        visited: Set[str] = set()
        for step in steps:
            if step.id not in visited:
                if has_cycle(step.id, visited, set()):
                    return True

        return False

    def _is_topologically_sorted(self, steps: List[PlanStep]) -> bool:
        if not steps:
            return True

        step_index = {s.id: s.order for s in steps}

        for step in steps:
            for dep_id in step.depends_on:
                if dep_id in step_index and step_index[dep_id] >= step.order:
                    return False

        return True

    def _topological_sort(self, steps: List[PlanStep]) -> List[PlanStep]:
        sorted_steps = sorted(
            steps,
            key=lambda s: (len(s.depends_on), s.order),
        )
        
        for i, step in enumerate(sorted_steps, 1):
            step.order = i

        return sorted_steps

    def _empty_plan(self, text: str) -> ExecutionPlan:
        return ExecutionPlan(
            original_text=text,
            normalized_text="",
            steps=[],
            confidence=0.0,
            planner_used="none",
        )

    def _unknown_plan(self, text: str) -> ExecutionPlan:
        step = self._create_step(
            order=1,
            action=ActionType.UNKNOWN,
            target=text,
            source_text=text,
        )
        return ExecutionPlan(
            original_text=text,
            normalized_text=text,
            steps=[step],
            confidence=0.3,
            planner_used="fallback",
        )
