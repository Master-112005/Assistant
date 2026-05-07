"""
Structured output schemas for the local LLM layer.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CorrectedCommand(StrictSchema):
    original_text: str = Field(min_length=1, description="The original noisy speech-to-text command.")
    corrected_text: str = Field(min_length=1, description="The corrected command text.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0.")


class IntentSchema(StrictSchema):
    intent: str = Field(min_length=1, description="Detected intent label.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0.")
    entities: dict[str, Any] = Field(default_factory=dict, description="Extracted entities.")
    reason: str = Field(min_length=1, description="Brief classification reason.")

    @field_validator("intent")
    @classmethod
    def normalize_intent(cls, value: str) -> str:
        return value.strip().lower().replace(" ", "_")


class PlanStep(StrictSchema):
    order: int = Field(ge=1, description="Execution order step number.")
    action: str = Field(min_length=1, description="The action to perform.")
    target: str = Field(default="", description="The main target of the action.")
    params: dict[str, Any] = Field(default_factory=dict, description="Additional execution parameters.")

    @field_validator("action")
    @classmethod
    def normalize_action(cls, value: str) -> str:
        return value.strip().lower().replace(" ", "_")


class PlanSchema(StrictSchema):
    steps: list[PlanStep] = Field(default_factory=list, description="Ordered plan steps.")

    @model_validator(mode="after")
    def sort_steps(self) -> "PlanSchema":
        self.steps = sorted(self.steps, key=lambda step: step.order)
        return self
