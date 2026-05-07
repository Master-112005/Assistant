"""
Compatibility wrapper around the canonical app index.
"""
from __future__ import annotations

from core.app_index import (
    STATIC_APP_INDEX,
    canonicalize_app_name as _canonicalize_app_name,
    get_all_aliases,
)
from core.app_launcher import app_display_name, app_process_names, preferred_browser_id, website_url_for

APP_PROFILES = STATIC_APP_INDEX


def canonicalize_app_name(name: str, *, resolve_browser_alias: bool = True) -> str:
    normalized = str(name or "").strip().lower()
    if resolve_browser_alias and normalized in {"browser", "default browser", "web browser"}:
        return preferred_browser_id()
    return _canonicalize_app_name(name, resolve_browser_alias=False)

__all__ = [
    "APP_PROFILES",
    "app_display_name",
    "app_process_names",
    "canonicalize_app_name",
    "get_all_aliases",
    "preferred_browser_id",
    "website_url_for",
]
