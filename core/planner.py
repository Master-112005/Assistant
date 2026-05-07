"""
Action planning engine - converts user requests into ordered executable steps.

Hybrid approach:
1. Rule-based planner for common patterns
2. LLM-based planner for complex natural language
3. Fallback handling for edge cases
"""
from __future__ import annotations

import re
import time
from typing import Optional, List, Dict, Any, Set, Tuple
from enum import Enum

from core.plan_models import (
    ActionType,
    PlanStep,
    ExecutionPlan,
    PlannerContext,
)
from core.logger import get_logger
from core.llm import LLMClient
from core import entities, parser, state, prompts
from core.app_launcher import canonicalize_app_name
from core.schemas import PlanSchema

logger = get_logger(__name__)


# Command connectors that separate multiple actions
COMMAND_CONNECTORS = {
    "and",
    "then",
    "after that",
    "next",
    "also",
    "followed by",
    "&",
    ",",
}

# Regex patterns for command segmentation
CONNECTOR_PATTERN = re.compile(
    r'\s+(and|then|after\s+that|next|also|followed\s+by)\s+|,\s*',
    re.IGNORECASE
)


class ActionPlanner:
    """
    Hybrid action planner that converts natural language commands into structured plans.
    
    Strategy:
    1. Normalize input
    2. Segment into commands
    3. Extract actions (rules first, fallback to LLM)
    4. Resolve dependencies
    5. Validate plan
    6. Return structured execution plan
    """

    def __init__(self, llm_client: Optional[LLMClient] = None):
        """Initialize the planner."""
        self.llm = llm_client or LLMClient()
        self.step_id_counter = 0
        
        # Rule patterns for common commands
        self.app_open_patterns = [
            (r'\bopen\s+(\w+)', 'open_app'),
            (r'\blaunch\s+(\w+)', 'open_app'),
            (r'\bstart\s+(\w+)', 'open_app'),
            (r'\brun\s+(\w+)', 'open_app'),
        ]
        
        self.search_patterns = [
            (r'\bsearch\s+(?:for\s+)?(.+?)(?:\s+on\s+(\w+))?$', 'search'),
            (r'\blook\s+(?:for\s+)?(?:up\s+)?(.+?)(?:\s+on\s+(\w+))?$', 'search'),
            (r'\bfind\s+(?:(?:information\s+)?(?:on|about)\s+)?(.+?)(?:\s+on\s+(\w+))?$', 'search'),
            (r'\bgoogle\s+(.+)$', 'search'),
        ]
        
        self.play_patterns = [
            (r'\bplay\s+(?:the\s+)?(?:first\s+)?(?:song|music|video|track)\b', 'play'),
            (r'\bplay\s+(.+?)\s+(?:song|music|video|track)\b', 'play'),
            (r'\bplay\s+(.+)$', 'play'),
        ]
        
        self.click_patterns = [
            (r'\bclick\s+(?:on\s+)?(.+)', 'click'),
        ]
        
        self.type_patterns = [
            (r'\btype\s+(.+)', 'type'),
        ]
        
        self.system_control_patterns = [
            (r'\b(?:turn|set)\s+(?:up|down|increase|decrease|raise|lower|louder|quieter|mute|unmute)\s+(.+)', 'system_control'),
            (r'\b(?:volume|brightness|wifi|bluetooth|shutdown|sleep|lock)\s+(?:up|down|on|off|increase|decrease)', 'system_control'),
        ]
        
        logger.info("ActionPlanner initialized")

    def plan(
        self,
        text: str,
        context: Optional[PlannerContext] = None,
        use_llm: bool = True,
        context_hints: Optional[Dict[str, Any]] = None,
    ) -> ExecutionPlan:
        """
        Main entry point: convert text to execution plan.
        
        Args:
            text: User input text
            context: Optional context about current state
            use_llm: Whether to use LLM for complex cases
            
        Returns:
            ExecutionPlan with ordered steps
        """
        if not text or not text.strip():
            logger.warning("Empty input text for planner")
            return self._empty_plan(text)

        # Step 1: Normalize input
        normalized = self.normalize(text)
        logger.info("Planning: %s", normalized)

        steps: List[PlanStep] = []
        planner_used = "rules"

        if context_hints and context_hints.get("steps"):
            steps = self._extract_steps_from_context_hints(context_hints["steps"], normalized)
            planner_used = "context"
            logger.info("Using %d context-enriched planner steps", len(steps))
        else:
            # Step 2: Segment into commands
            segments = self.split_commands(normalized)
            logger.info("Segments found: %d", len(segments))

            # Step 3: Try rule-based extraction
            steps = self._extract_steps_rules(segments)
            planner_used = "rules"

        # Step 4: If rules yielded nothing or only UNKNOWN placeholders, try LLM
        if use_llm and (not steps or all(step.action == ActionType.UNKNOWN for step in steps)):
            steps = self._extract_steps_llm(normalized, context)
            planner_used = "llm"
        elif steps and use_llm and planner_used != "context":
            # For complex multi-step, enhance with LLM
            if len(steps) > 1:
                enhanced = self._enhance_plan_llm(steps, normalized, context)
                if enhanced:
                    steps = enhanced
                    planner_used = "hybrid"

        # Step 5: Resolve dependencies
        steps = self.resolve_dependencies(steps, context)

        # Step 6: Create plan
        plan = ExecutionPlan(
            original_text=text,
            normalized_text=normalized,
            steps=steps,
            confidence=self._calculate_confidence(steps, planner_used),
            planner_used=planner_used,
            has_dependencies=any(s.depends_on for s in steps),
        )

        # Step 7: Validate
        if not self.validate_plan(plan):
            logger.warning("Plan validation failed, returning single unknown step")
            plan = self._unknown_plan(text)

        logger.info("Plan generated: %d steps, confidence=%.2f, planner=%s",
                    plan.step_count, plan.confidence, planner_used)

        # Update state
        state.last_plan = plan
        state.last_plan_steps = plan.step_count
        state.last_plan_confidence = plan.confidence
        state.last_planner_used = planner_used

        return plan

    def normalize(self, text: str) -> str:
        """
        Normalize input text for processing.
        
        Args:
            text: Raw user input
            
        Returns:
            Cleaned, normalized text
        """
        # Use existing parser cleanup
        cleaned = parser.clean_text(text)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return cleaned

    def split_commands(self, text: str) -> List[str]:
        """
        Split text into command segments based on connectors.
        
        Args:
            text: Normalized input text
            
        Returns:
            List of command segments
        """
        # Split on connector words/punctuation
        parts = CONNECTOR_PATTERN.split(text)
        
        # parts = [segment, connector, segment, connector, ...]
        # Extract only the segments (even indices)
        segments = [parts[i].strip() for i in range(0, len(parts), 2)]
        
        # Filter empty segments
        segments = [s for s in segments if s]
        
        logger.debug("Split into segments: %s", segments)
        return segments

    def _extract_steps_rules(self, segments: List[str]) -> List[PlanStep]:
        """
        Extract steps using rule-based patterns.
        
        Args:
            segments: Command segments
            
        Returns:
            List of PlanStep objects
        """
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
        """Build plan steps directly from ContextDecision planner hints."""
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
        """
        Extract a single step from a segment using pattern matching.
        
        Args:
            segment: Single command segment
            order: Execution order
            
        Returns:
            PlanStep or None if no pattern matched
        """
        segment_lower = segment.lower()

        # Try app open patterns
        for pattern, action_name in self.app_open_patterns:
            match = re.search(pattern, segment_lower)
            if match:
                app_name = match.group(1).strip()
                return self._create_step(
                    order=order,
                    action=ActionType.OPEN_APP,
                    target=app_name,
                    source_text=segment,
                )

        # Try search patterns
        for pattern, action_name in self.search_patterns:
            match = re.search(pattern, segment_lower)
            if match:
                query = match.group(1).strip()
                target = match.group(2).strip() if match.lastindex >= 2 else ""
                return self._create_step(
                    order=order,
                    action=ActionType.SEARCH,
                    target=target or "default",
                    params={"query": query},
                    source_text=segment,
                )

        # Try play patterns
        for pattern, action_name in self.play_patterns:
            match = re.search(pattern, segment_lower)
            if match:
                # Check if a specific item was mentioned
                target = match.group(1).strip() if match.lastindex and match.lastindex >= 1 else "first_result"
                selection = 1 if "first" in segment_lower else -1
                return self._create_step(
                    order=order,
                    action=ActionType.PLAY,
                    target=target,
                    params={"selection": selection},
                    source_text=segment,
                )

        # Try click patterns
        for pattern, action_name in self.click_patterns:
            match = re.search(pattern, segment_lower)
            if match:
                target = match.group(1).strip()
                return self._create_step(
                    order=order,
                    action=ActionType.CLICK,
                    target=target,
                    source_text=segment,
                )

        # Try type patterns
        for pattern, action_name in self.type_patterns:
            match = re.search(pattern, segment_lower)
            if match:
                text_to_type = match.group(1).strip()
                return self._create_step(
                    order=order,
                    action=ActionType.TYPE,
                    params={"text": text_to_type},
                    source_text=segment,
                )

        # Try system control patterns
        for pattern, action_name in self.system_control_patterns:
            match = re.search(pattern, segment_lower)
            if match:
                parsed = entities.extract_system_control(segment)
                return self._create_step(
                    order=order,
                    action=ActionType.SYSTEM_CONTROL,
                    target=str(parsed.get("control") or (match.group(1).strip() if match.lastindex and match.lastindex >= 1 else "unknown")),
                    params={key: value for key, value in parsed.items() if key != "control"},
                    source_text=segment,
                )

        parsed_system = entities.extract_system_control(segment)
        if parsed_system.get("action"):
            return self._create_step(
                order=order,
                action=ActionType.SYSTEM_CONTROL,
                target=str(parsed_system.get("control") or "unknown"),
                params={key: value for key, value in parsed_system.items() if key != "control"},
                source_text=segment,
            )

        # No pattern matched - return unknown
        return self._create_step(
            order=order,
            action=ActionType.UNKNOWN,
            target=segment,
            source_text=segment,
        )

    def _extract_steps_llm(
        self,
        text: str,
        context: Optional[PlannerContext] = None,
    ) -> List[PlanStep]:
        """
        Extract steps using LLM for complex natural language.
        
        Args:
            text: Normalized input text
            context: Optional context
            
        Returns:
            List of PlanStep objects
        """
        logger.info("Using LLM planner for: %s", text)
        
        try:
            response = None
            context_payload = self._planner_context_payload(context)
            if hasattr(self.llm, "plan_actions"):
                response = self.llm.plan_actions(text, context=context_payload)
            elif hasattr(self.llm, "json_generate"):
                response = self.llm.json_generate(
                    self._build_planner_prompt(text, context),
                    schema=PlanSchema,
                    task="planning",
                )

            steps = self._steps_from_llm_response(response, text)
            logger.info("LLM generated %d steps", len(steps))
            return steps
        except Exception as e:
            logger.error("LLM planner failed: %s", e)
            return []

    def _enhance_plan_llm(
        self,
        steps: List[PlanStep],
        text: str,
        context: Optional[PlannerContext] = None,
    ) -> Optional[List[PlanStep]]:
        """
        Enhance rule-based plan with LLM for better dependencies/params.
        
        Args:
            steps: Initial rule-based steps
            text: Original text
            context: Optional context
            
        Returns:
            Enhanced steps or None to keep original
        """
        logger.info("Enhancing plan with LLM")
        
        try:
            prompt = f"""Given the original request and these preliminary steps, improve them by:
- Adding any missing steps
- Setting correct dependencies between steps
- Ensuring proper order

Original request: {text}

Preliminary steps:
{chr(10).join(str(s) for s in steps)}

Provide improved steps in JSON format with: order, action, target, params, depends_on"""

            response = None
            if hasattr(self.llm, "json_generate"):
                response = self.llm.json_generate(
                    prompt,
                    schema=PlanSchema,
                    task="planning_enhancement",
                )
            elif hasattr(self.llm, "plan_actions"):
                response = self.llm.plan_actions(text, context=self._planner_context_payload(context))

            enhanced = self._steps_from_llm_response(response, text)
            logger.info("LLM enhanced plan: %d steps", len(enhanced))
            return enhanced if enhanced else None
        except Exception as e:
            logger.error("LLM enhancement failed: %s", e)
        
        return None

    @staticmethod
    def _planner_context_payload(context: Optional[PlannerContext]) -> Dict[str, Any] | None:
        if context is None:
            return None
        return {
            "current_app": context.current_app,
            "browser_open": context.browser_open,
            "search_results_available": context.search_results_available,
            "window_title": context.window_title,
            "last_action": context.last_action,
            "recent_commands": list(context.recent_commands or []),
            "resolved_target_app": context.resolved_target_app,
            "rewritten_command": context.rewritten_command,
        }

    def _steps_from_llm_response(self, response: Any, source_text: str) -> List[PlanStep]:
        if response is None:
            return []
        if isinstance(response, PlanSchema):
            return self._parse_llm_steps(
                [step.model_dump() if hasattr(step, "model_dump") else step.to_dict() for step in response.steps],
                source_text,
            )
        if isinstance(response, dict) and isinstance(response.get("steps"), list):
            return self._parse_llm_steps(response["steps"], source_text)
        if hasattr(response, "steps"):
            raw_steps = getattr(response, "steps")
            if isinstance(raw_steps, list):
                normalized_steps: List[Dict[str, Any]] = []
                for item in raw_steps:
                    if hasattr(item, "model_dump"):
                        normalized_steps.append(item.model_dump())
                    elif hasattr(item, "to_dict"):
                        normalized_steps.append(item.to_dict())
                    elif isinstance(item, dict):
                        normalized_steps.append(item)
                return self._parse_llm_steps(normalized_steps, source_text)
        logger.warning("Unexpected LLM response format: %s", type(response))
        return []

    def resolve_dependencies(
        self,
        steps: List[PlanStep],
        context: Optional[PlannerContext] = None,
    ) -> List[PlanStep]:
        """
        Analyze steps and add dependencies based on logical flow.
        
        Args:
            steps: Initial steps
            context: Optional context
            
        Returns:
            Steps with resolved dependencies
        """
        if not steps:
            return steps

        # Mark which apps/resources are available
        available_apps: Set[str] = {context.current_app} if context and context.current_app else set()
        
        for i, step in enumerate(steps):
            # SEARCH depends on having a search target/browser open
            if step.action == ActionType.SEARCH:
                # Search often needs a browser or the target app open
                if step.target and step.target != "default":
                    # Depends on that app being open
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

            # PLAY usually depends on having search results or the app open
            elif step.action == ActionType.PLAY:
                preceding_app = self._nearest_preceding_open_app(steps, i)
                if preceding_app in {"youtube", "spotify", "music"}:
                    self._bind_play_step_to_app(step, preceding_app)
                    for j in range(i - 1, -1, -1):
                        if steps[j].action == ActionType.OPEN_APP and self._app_matches(steps[j].target, preceding_app):
                            step.depends_on = [steps[j].id]
                            break
                    continue

                # Play depends on having search results (previous search)
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

            # CLICK depends on having navigated to the page
            elif step.action == ActionType.CLICK:
                # Click on something requires the relevant app to be open
                for j in range(i - 1, -1, -1):
                    if steps[j].action in (ActionType.OPEN_APP, ActionType.SEARCH):
                        step.depends_on = [steps[j].id]
                        break

        return steps

    def validate_plan(self, plan: ExecutionPlan) -> bool:
        """
        Validate that a plan is logically sound.
        
        Args:
            plan: ExecutionPlan to validate
            
        Returns:
            True if valid, False otherwise
        """
        if not plan.is_valid:
            return False

        # Check for circular dependencies
        if self._has_circular_dependency(plan.steps):
            logger.warning("Plan has circular dependencies")
            return False

        # Check that all dependencies reference existing steps
        step_ids = {s.id for s in plan.steps}
        for step in plan.steps:
            for dep_id in step.depends_on:
                if dep_id not in step_ids:
                    logger.warning("Step %s references non-existent dependency %s", step.id, dep_id)
                    return False

        # Check that steps are in valid order
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
        """Helper to create a PlanStep with unique ID."""
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

    def _calculate_confidence(self, steps: List[PlanStep], planner_used: str) -> float:
        """Calculate overall plan confidence."""
        if not steps:
            return 0.0

        # Unknown actions reduce confidence
        unknown_count = sum(1 for s in steps if s.action == ActionType.UNKNOWN)
        unknown_penalty = unknown_count * 0.2

        # LLM gets slightly higher confidence than rules
        if planner_used == "context":
            base_confidence = 0.94
        elif planner_used == "llm":
            base_confidence = 0.85
        elif planner_used == "hybrid":
            base_confidence = 0.88
        else:
            base_confidence = 0.8

        confidence = base_confidence - unknown_penalty
        return max(0.1, min(1.0, confidence))

    def _app_matches(self, app1: str, app2: str) -> bool:
        """Check if two app names likely refer to the same app."""
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
        """Check if steps have circular dependencies."""
        if not steps:
            return False

        # Build adjacency list
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
        """Check if steps are in valid topological order."""
        if not steps:
            return True

        step_index = {s.id: s.order for s in steps}

        for step in steps:
            for dep_id in step.depends_on:
                if dep_id in step_index and step_index[dep_id] >= step.order:
                    return False

        return True

    def _topological_sort(self, steps: List[PlanStep]) -> List[PlanStep]:
        """Sort steps in valid topological order."""
        # This is a simplified version; proper implementation would use
        # Kahn's or DFS-based topological sort
        sorted_steps = sorted(
            steps,
            key=lambda s: (len(s.depends_on), s.order),
        )
        
        # Reassign orders
        for i, step in enumerate(sorted_steps, 1):
            step.order = i

        return sorted_steps

    def _parse_llm_steps(self, llm_steps: List[Dict[str, Any]], source_text: str) -> List[PlanStep]:
        """Parse LLM response into PlanStep objects."""
        steps: List[PlanStep] = []

        for i, step_data in enumerate(llm_steps, 1):
            try:
                action_str = step_data.get("action", "unknown").upper().replace(" ", "_")
                action = ActionType[action_str] if action_str in ActionType.__members__ else ActionType.UNKNOWN

                step = self._create_step(
                    order=step_data.get("order", i),
                    action=action,
                    target=step_data.get("target", ""),
                    params=step_data.get("params", {}),
                    depends_on=step_data.get("depends_on", []),
                    source_text=source_text,
                )
                steps.append(step)
            except (KeyError, ValueError) as e:
                logger.warning("Failed to parse LLM step: %s", e)
                continue

        return steps

    def _build_planner_prompt(self, text: str, context: Optional[PlannerContext] = None) -> str:
        """Build prompt for LLM planner."""
        # Try to load from prompts module
        try:
            prompt_template = prompts.get_prompt("planning")
        except Exception:
            prompt_template = self._default_planner_prompt()

        # Fill in context
        context_info = ""
        if context and context.has_context():
            context_info = f"\nCurrent context:\n"
            if context.current_app:
                context_info += f"- Current app: {context.current_app}\n"
            if context.browser_open:
                context_info += f"- Browser is open\n"
            if context.search_results_available:
                context_info += f"- Recent search results available\n"

        full_prompt = f"{prompt_template}\n\nUser request: {text}{context_info}"
        return full_prompt

    def _default_planner_prompt(self) -> str:
        """Default planning prompt if file not found."""
        return """Convert the user request into structured action steps.

Supported actions:
- open_app: Open an application
- search: Search for something
- play: Play media
- app_action: Execute an app-specific action against the active app
- click: Click an element
- type: Type text
- system_control: Control system settings
- file_action: File operations
- ask_user: Ask for clarification
- wait: Wait for something

For each step provide:
{
  "order": <step number>,
  "action": "<action_type>",
  "target": "<what/where>",
  "params": {<key>: <value>},
  "depends_on": [<list of step IDs this depends on>]
}

Return as JSON array of steps."""

    def _empty_plan(self, text: str) -> ExecutionPlan:
        """Return empty plan for empty input."""
        return ExecutionPlan(
            original_text=text,
            normalized_text="",
            steps=[],
            confidence=0.0,
            planner_used="none",
        )

    def _unknown_plan(self, text: str) -> ExecutionPlan:
        """Return single unknown step plan."""
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
