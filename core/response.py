"""
User-facing response helpers for the direct desktop command pipeline.
"""
from __future__ import annotations
import random

from core.app_launcher import app_display_name

_CONVERSATIONAL_OPENERS = [
    "Sure thing! ",
    "On it! ",
    "Got it! ",
    "Okay! ",
    "Alright! ",
    "No problem! ",
    "Absolutely! ",
    "",
]

_CONVERSATIONAL_CLOSERS = [
    " for you",
    "",
    " now",
    "",
]


class CommandResponseBuilder:
    @staticmethod
    def opening_app(app_name: str, detail: str = "") -> str:
        label = app_display_name(app_name)
        openers = ["Got it!", "Sure!", "On it!", "Opening", "Starting"]
        opener = random.choice(openers)
        if detail:
            return f"{opener} {label}. {detail}"
        return f"{opener} {label}."

    @staticmethod
    def closing_app(app_name: str, detail: str = "") -> str:
        label = app_display_name(app_name)
        openers = ["Got it!", "Sure!", "On it!", "Closing", "Stopping"]
        opener = random.choice(openers)
        if detail:
            return f"{opener} {label}. {detail}"
        return f"{opener} {label}."

    @staticmethod
    def minimizing_app(app_name: str, detail: str = "") -> str:
        label = app_display_name(app_name)
        openers = ["Got it!", "Sure!", "On it!", "Minimizing", "Hiding"]
        opener = random.choice(openers)
        if detail:
            return f"{opener} {label}. {detail}"
        return f"{opener} {label}."

    @staticmethod
    def maximizing_app(app_name: str, detail: str = "") -> str:
        label = app_display_name(app_name)
        openers = ["Got it!", "Sure!", "On it!", "Maximizing", "Expanding"]
        opener = random.choice(openers)
        if detail:
            return f"{opener} {label}. {detail}"
        return f"{opener} {label}."

    @staticmethod
    def focusing_app(app_name: str, detail: str = "") -> str:
        label = app_display_name(app_name)
        opener = random.choice(_CONVERSATIONAL_OPENERS)
        return _combine(f"{opener}Switching to {label}.", detail)

    @staticmethod
    def restoring_app(app_name: str, detail: str = "") -> str:
        label = app_display_name(app_name)
        opener = random.choice(_CONVERSATIONAL_OPENERS)
        return _combine(f"{opener}Restoring {label}.", detail)

    @staticmethod
    def toggling_app(app_name: str, detail: str = "") -> str:
        label = app_display_name(app_name)
        opener = random.choice(_CONVERSATIONAL_OPENERS)
        return _combine(f"{opener}Toggling {label}.", detail)

    @staticmethod
    def searching_web(query: str, browser: str = "") -> str:
        cleaned_query = str(query or "").strip()
        opener = random.choice(_CONVERSATIONAL_OPENERS)
        if browser:
            return f"{opener}Searching for {cleaned_query} in {app_display_name(browser)}."
        return f"{opener}Searching for {cleaned_query}."

    @staticmethod
    def opening_website(name: str, detail: str = "") -> str:
        label = app_display_name(name)
        opener = random.choice(_CONVERSATIONAL_OPENERS)
        return _combine(f"{opener}Opening {label}.", detail)


def _combine(prefix: str, detail: str) -> str:
    suffix = str(detail or "").strip()
    if not suffix:
        return prefix
    if suffix == prefix:
        return prefix
    lowered = suffix.lower()
    if (
        "already" in lowered
        or "not running" in lowered
        or "is not open" in lowered
        or "is not installed" in lowered
        or lowered.startswith(("i couldn't", "couldn't", "no "))
    ):
        return suffix
    return f"{prefix} {suffix}"
