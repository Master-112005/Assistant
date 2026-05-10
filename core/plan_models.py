"""
Action planner data models and structures.

Defines the core data types for representing actions, plan steps, and execution plans.
"""
from __future__ import annotations

from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Optional, List
import time


class ActionType(Enum):
    """Enumeration of all supported action types."""
    OPEN_APP = "open_app"
    CLOSE_APP = "close_app"
    SEARCH = "search"
    PLAY = "play"
    APP_ACTION = "app_action"
    CLICK = "click"
    TYPE = "type"
    SYSTEM_CONTROL = "system_control"
    FILE_ACTION = "file_action"
    ASK_USER = "ask_user"
    WAIT = "wait"
    SEND_MESSAGE = "send_message"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


@dataclass
class PlanStep:
    """
    Represents a single executable step in an action plan.
    
    Attributes:
        id: Unique identifier for this step.
        order: Execution order (1-based index).
        action: The ActionType to execute.
        target: The primary target of the action (app name, URL, etc.).
        params: Additional execution parameters as key-value pairs.
        depends_on: List of step IDs this step depends on.
        requires_confirmation: Whether user approval is needed before execution.
        estimated_risk: Risk level (low, medium, high).
        source_text: Original text that generated this step.
        created_at: Timestamp when this step was created.
    """
    id: str
    order: int
    action: ActionType
    target: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    requires_confirmation: bool = False
    estimated_risk: str = "low"
    source_text: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Convert step to dictionary representation."""
        return {
            "id": self.id,
            "order": self.order,
            "action": self.action.value,
            "target": self.target,
            "params": self.params,
            "depends_on": self.depends_on,
            "requires_confirmation": self.requires_confirmation,
            "estimated_risk": self.estimated_risk,
            "source_text": self.source_text,
        }

    def __str__(self) -> str:
        """Human-readable representation."""
        if self.params:
            param_str = " | " + ", ".join(f"{k}={v}" for k, v in self.params.items())
        else:
            param_str = ""
        
        deps = f" [depends: {','.join(self.depends_on)}]" if self.depends_on else ""
        return f"Step {self.order}: {self.action.value} {self.target}{param_str}{deps}"


@dataclass
class ExecutionPlan:
    """
    Represents a complete action plan derived from user input.
    
    Attributes:
        original_text: The original user input text.
        normalized_text: The normalized/cleaned input text.
        steps: List of PlanStep objects in execution order.
        confidence: Overall confidence score (0.0-1.0).
        planner_used: Which planner generated this ("rules", "context").
        has_dependencies: Whether any steps have dependencies.
        estimated_risk: Overall plan risk level.
        created_at: Timestamp when this plan was created.
    """
    original_text: str
    normalized_text: str
    steps: List[PlanStep] = field(default_factory=list)
    confidence: float = 0.0
    planner_used: str = "unknown"
    has_dependencies: bool = False
    estimated_risk: str = "low"
    created_at: float = field(default_factory=time.time)

    @property
    def step_count(self) -> int:
        """Number of steps in the plan."""
        return len(self.steps)

    @property
    def is_valid(self) -> bool:
        """Check if the plan is valid (non-empty with valid steps)."""
        return self.step_count > 0 and all(s.order > 0 for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        """Convert plan to dictionary representation."""
        return {
            "original_text": self.original_text,
            "normalized_text": self.normalized_text,
            "steps": [s.to_dict() for s in self.steps],
            "step_count": self.step_count,
            "confidence": self.confidence,
            "planner_used": self.planner_used,
            "has_dependencies": self.has_dependencies,
            "estimated_risk": self.estimated_risk,
        }

    def __str__(self) -> str:
        """Human-readable representation."""
        lines = [f"Plan (confidence={self.confidence:.2f}, planner={self.planner_used}):"]
        for step in self.steps:
            lines.append(f"  {step}")
        return "\n".join(lines)


@dataclass
class PlannerContext:
    """
    Optional context provided to the planner for better decision-making.
    
    Attributes:
        current_app: Currently active application.
        browser_open: Whether a browser is currently open.
        search_results_available: Whether recent search results are cached.
        previous_steps: Previous steps in a multi-turn conversation.
    """
    current_app: str = ""
    browser_open: bool = False
    search_results_available: bool = False
    previous_steps: List[PlanStep] = field(default_factory=list)
    window_title: str = ""
    last_action: str = ""
    recent_commands: List[dict[str, Any]] = field(default_factory=list)
    resolved_target_app: str = ""
    rewritten_command: str = ""

    def has_context(self) -> bool:
        """Check if any context is available."""
        return any(
            (
                bool(self.current_app),
                self.browser_open,
                self.search_results_available,
                bool(self.window_title),
                bool(self.last_action),
                bool(self.recent_commands),
                bool(self.resolved_target_app),
                bool(self.rewritten_command),
            )
        )
