"""
Reusable prompt templates for the assistant.
"""
from __future__ import annotations

import json
from pathlib import Path
from string import Template
from textwrap import dedent
from typing import Any

from core.paths import PROMPTS_DIR


PROMPT_FILES: dict[str, Path] = {
    "stt_correction": PROMPTS_DIR / "stt_correction.md",
    "intent_extraction": PROMPTS_DIR / "intent_extraction.md",
    "planning": PROMPTS_DIR / "planning.md",
    "clarification": PROMPTS_DIR / "clarification.md",
    "safe_refusal": PROMPTS_DIR / "safe_refusal.md",
}

DEFAULT_TEMPLATES: dict[str, str] = {
    "stt_correction": dedent(
        """
        You correct noisy Windows voice-assistant commands transcribed by speech-to-text.
        Keep the command short and natural.
        Rules:
        - Preserve the original intent.
        - Fix phonetic mistakes, spacing, spelling, and capitalization.
        - Do not add new actions.
        - Prefer app names and common Windows actions when the wording strongly implies them.

        Transcript:
        $text
        """
    ).strip(),
    "intent_extraction": dedent(
        """
        You classify offline assistant commands and extract entities for execution.
        Allowed intents:
        - open_app
        - search
        - question
        - system_control
        - file_action
        - greeting
        - help
        - multi_action
        - unknown
        - communication (for messaging/calling contacts via whatsapp, email, etc.)

        Use `multi_action` only when the command clearly contains two or more actions.
        Keep entity keys practical for automation, such as `app_name`, `apps`, `query`, `control`, `direction`, `filename`, or `destination`.

        For communication commands (message, call, send to contact):
        - Extract full message content including all words after the contact name
        - Example: "message hemanth hai how are you" → {intent: "communication", entities: {contact: "hemanth", message: "hai how are you"}}
        - Example: "send hi to rahul" → {intent: "communication", entities: {contact: "rahul", message: "hi"}}

        For greetings and casual speech:
        - "hi", "hello", "hey", "hi ra", "hello there" → {intent: "greeting", entities: {}}
        - Don't overthink - simple casual phrases are greetings

        Command:
        $text
        """
    ).strip(),
    "planning": dedent(
        """
        You create deterministic action plans for an offline Windows assistant.
        Plan only the actions implied by the user command.
        Prefer these action names when possible:
        - open_app
        - search
        - system_control
        - file_action
        - question
        - help

        Runtime context:
        $context

        Command:
        $text
        """
    ).strip(),
    "clarification": dedent(
        """
        Ask one short clarification question for an ambiguous assistant command.

        Context:
        $context

        Command:
        $text
        """
    ).strip(),
    "safe_refusal": dedent(
        """
        Refuse the request briefly and explain the offline safety limitation.

        Reason:
        $reason

        Command:
        $text
        """
    ).strip(),
}


def _load_template(name: str) -> str:
    path = PROMPT_FILES[name]
    try:
        template = path.read_text(encoding="utf-8").strip()
        if template:
            return template
    except OSError:
        pass
    return DEFAULT_TEMPLATES[name]


def _normalize_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, ensure_ascii=True)
    return str(value)


def _render_template(name: str, **values: Any) -> str:
    normalized_values = {key: _normalize_value(value) for key, value in values.items()}
    return Template(_load_template(name)).safe_substitute(**normalized_values).strip()


def build_stt_correction_prompt(text: str) -> str:
    return _render_template("stt_correction", text=text)


def build_intent_prompt(text: str) -> str:
    return _render_template("intent_extraction", text=text)


def build_plan_prompt(text: str, context: dict[str, Any] | None = None) -> str:
    return _render_template("planning", text=text, context=context or {})


def build_clarification_prompt(text: str, context: dict[str, Any] | None = None) -> str:
    return _render_template("clarification", text=text, context=context or {})


def build_safe_refusal_prompt(text: str, reason: str) -> str:
    return _render_template("safe_refusal", text=text, reason=reason)


def get_prompt(name: str) -> str:
    """Get a prompt template by name."""
    return _load_template(name)
