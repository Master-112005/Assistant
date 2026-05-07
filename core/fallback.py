"""
Context-aware fallback resolvers used by the recovery system.
"""
from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

try:
    from rapidfuzz import fuzz
except Exception:  # pragma: no cover - optional dependency
    fuzz = None

from core import settings, state
from core.app_index import AppIndexer
from core.logger import get_logger
from core.memory import memory_manager

logger = get_logger(__name__)

_BROWSER_CANDIDATES = (
    ("chrome", "Google Chrome"),
    ("edge", "Microsoft Edge"),
    ("firefox", "Mozilla Firefox"),
    ("brave", "Brave"),
)

_APP_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "browser": ("chrome", "edge", "firefox", "brave", "opera", "vivaldi"),
    "music": ("spotify", "itunes", "vlc", "musicbee", "foobar2000"),
    "chat": ("whatsapp", "telegram", "slack", "discord", "teams"),
    "editor": ("notepad", "word", "vscode", "notepad++"),
}


def find_alternative_browser(
    missing_browser: str = "",
    *,
    preferred_browser: str | None = None,
    limit: int = 3,
) -> list[dict[str, str]]:
    """Return installed browser alternatives ordered by preference."""
    installed = _installed_app_names()
    normalized_missing = str(missing_browser or "").strip().lower()
    preferred = str(preferred_browser or settings.get("preferred_browser") or "").strip().lower()

    matches: list[dict[str, str]] = []
    for browser_id, display_name in _BROWSER_CANDIDATES:
        if browser_id == normalized_missing:
            continue
        if browser_id in installed or display_name.lower() in installed:
            matches.append({"app_name": browser_id, "display_name": display_name, "category": "browser"})

    if preferred:
        matches.sort(key=lambda item: (item["app_name"] != preferred, item["display_name"].lower()))
    return matches[: max(1, int(limit))]


def find_alternative_app(category: str, *, exclude: str = "", limit: int = 3) -> list[dict[str, str]]:
    """Return installed alternatives for a coarse application category."""
    installed = _installed_app_names()
    exclude_name = str(exclude or "").strip().lower()
    aliases = _APP_CATEGORY_ALIASES.get(str(category or "").strip().lower(), ())
    results: list[dict[str, str]] = []
    for alias in aliases:
        if alias == exclude_name:
            continue
        for installed_name in installed:
            if alias == installed_name or alias in installed_name:
                results.append({"app_name": installed_name, "display_name": installed_name.title(), "category": category})
                break
    return results[: max(1, int(limit))]


def find_similar_contact(name: str, *, platform: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
    """Return similar contact suggestions from local memory."""
    needle = str(name or "").strip()
    if not needle:
        return []

    try:
        memory_manager.init()
        rows = memory_manager._store.query("SELECT name, platform, aliases FROM contacts")
    except Exception as exc:
        logger.debug("Contact fallback lookup unavailable: %s", exc)
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    normalized_needle = needle.casefold()
    for row in rows:
        candidate_name = str(row.get("name") or "").strip()
        candidate_platform = str(row.get("platform") or "").strip().lower()
        if platform and candidate_platform != str(platform).strip().lower():
            continue
        if not candidate_name:
            continue
        aliases = row.get("aliases") or []
        if isinstance(aliases, str):
            try:
                aliases = memory_manager._parse_json_list(aliases)
            except Exception:
                aliases = []
        best_score = _similarity_score(normalized_needle, candidate_name.casefold())
        for alias in aliases:
            alias_text = str(alias or "").strip()
            if alias_text:
                best_score = max(best_score, _similarity_score(normalized_needle, alias_text.casefold()))
        if best_score >= 0.55:
            scored.append(
                (
                    best_score,
                    {
                        "name": candidate_name,
                        "platform": candidate_platform,
                        "aliases": list(aliases),
                        "score": round(best_score, 3),
                    },
                )
            )

    scored.sort(key=lambda item: (-item[0], item[1]["name"].casefold()))
    return [payload for _score, payload in scored[: max(1, int(limit))]]


def fallback_input_mode() -> dict[str, Any]:
    """Switch the assistant into text-first recovery mode."""
    state.ui_mode = getattr(state, "ui_mode", "desktop") or "desktop"
    return {
        "mode": "text",
        "label": "Text input",
        "message": "Voice input is unavailable right now. You can continue with typed commands.",
    }


def reduced_capability_mode(feature: str = "") -> dict[str, Any]:
    """Describe a graceful-degradation mode for an unavailable subsystem."""
    normalized = str(feature or "").strip().lower()
    if normalized == "llm":
        return {
            "feature": "llm",
            "mode": "rules_only",
            "message": "Advanced language features are unavailable. Rule-based commands still work.",
        }
    if normalized in {"ocr", "screen"}:
        return {
            "feature": normalized or "ocr",
            "mode": "text_only",
            "message": "Screen reading is unavailable. Typed and app-control commands still work.",
        }
    if normalized in {"microphone", "stt", "voice"}:
        return {
            "feature": normalized or "voice",
            "mode": "text_only",
            "message": fallback_input_mode()["message"],
        }
    return {
        "feature": normalized or "general",
        "mode": "reduced",
        "message": "Some advanced features are unavailable. Basic commands remain available.",
    }


def _installed_app_names() -> set[str]:
    indexer = AppIndexer()
    if not indexer.load_cache():
        try:
            indexer.refresh()
        except Exception as exc:
            logger.debug("App index refresh failed while resolving fallback apps: %s", exc)
    names = {record.normalized_name for record in indexer.get_all_records()}
    return names


def _similarity_score(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if fuzz is not None:
        return float(fuzz.token_sort_ratio(left, right)) / 100.0
    return SequenceMatcher(None, left, right).ratio()

