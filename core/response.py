"""
User-facing response helpers for the direct desktop command pipeline.
"""
from __future__ import annotations

from core.app_launcher import app_display_name


class CommandResponseBuilder:
    @staticmethod
    def opening_app(app_name: str, detail: str = "") -> str:
        label = app_display_name(app_name)
        return _combine(f"Opening {label}.", detail)

    @staticmethod
    def closing_app(app_name: str, detail: str = "") -> str:
        label = app_display_name(app_name)
        return _combine(f"Closing {label}.", detail)

    @staticmethod
    def minimizing_app(app_name: str, detail: str = "") -> str:
        label = app_display_name(app_name)
        return _combine(f"Minimizing {label}.", detail)

    @staticmethod
    def maximizing_app(app_name: str, detail: str = "") -> str:
        label = app_display_name(app_name)
        return _combine(f"Maximizing {label}.", detail)

    @staticmethod
    def focusing_app(app_name: str, detail: str = "") -> str:
        label = app_display_name(app_name)
        return _combine(f"Switching to {label}.", detail)

    @staticmethod
    def restoring_app(app_name: str, detail: str = "") -> str:
        label = app_display_name(app_name)
        return _combine(f"Restoring {label}.", detail)

    @staticmethod
    def toggling_app(app_name: str, detail: str = "") -> str:
        label = app_display_name(app_name)
        return _combine(f"Toggling {label}.", detail)

    @staticmethod
    def searching_web(query: str, browser: str = "") -> str:
        cleaned_query = str(query or "").strip()
        if browser:
            return f"Searching {cleaned_query} in {app_display_name(browser)}."
        return f"Searching {cleaned_query}."

    @staticmethod
    def opening_website(name: str, detail: str = "") -> str:
        label = app_display_name(name)
        return _combine(f"Opening {label}.", detail)


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
