"""
Phase 14 - Context Engine data models.

ContextDecision is the structured output of every context resolution.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ContextDecision:
    """
    Output of a single ContextEngine.resolve() call.

    Attributes:
        resolved_intent: Normalized intent string.
        target_app: App that should handle the action.
        reason: Human-readable explanation.
        confidence: 0.0-1.0 confidence score.
        rewritten_command: Explicit internal command for the planner.
        entities: Extracted named entities.
        requires_confirmation: True when the action should be confirmed.
        original_text: Raw user input text.
        context_used: Which context layer produced the decision.
        clarification_prompt: Optional clarification text for the user.
        matched_layers: Ordered context layers used to resolve the command.
        plan_hints: Structured hints that can be consumed by the planner.
        timestamp: Monotonic creation time.
    """

    resolved_intent: str = "unknown"
    target_app: str = "unknown"
    reason: str = ""
    confidence: float = 0.0
    rewritten_command: str = ""
    entities: Dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False
    original_text: str = ""
    context_used: str = "none"
    clarification_prompt: str = ""
    matched_layers: List[str] = field(default_factory=list)
    plan_hints: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.monotonic)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resolved_intent": self.resolved_intent,
            "target_app": self.target_app,
            "reason": self.reason,
            "confidence": round(self.confidence, 3),
            "rewritten_command": self.rewritten_command,
            "entities": self.entities,
            "requires_confirmation": self.requires_confirmation,
            "original_text": self.original_text,
            "context_used": self.context_used,
            "clarification_prompt": self.clarification_prompt,
            "matched_layers": list(self.matched_layers),
            "plan_hints": self.plan_hints,
        }

    def __str__(self) -> str:
        return (
            f"ContextDecision(intent={self.resolved_intent!r}, "
            f"app={self.target_app!r}, conf={self.confidence:.2f}, "
            f"rewrite={self.rewritten_command!r})"
        )


NULL_DECISION = ContextDecision(
    resolved_intent="passthrough",
    target_app="unknown",
    reason="Context engine disabled or not applicable",
    confidence=0.0,
    context_used="none",
)


@dataclass
class RecentCommand:
    """Single entry in the recent command history."""

    text: str
    intent: str
    target_app: str
    success: bool
    rewritten_command: str = ""
    entities: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.monotonic)
