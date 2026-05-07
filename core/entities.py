"""
Entity extraction helpers.
"""
from __future__ import annotations

from pathlib import Path
import re

from core.system_controls import entities_from_system_command

_FILE_LOCATION_PREPOSITIONS = ("from", "in", "on", "under")
_FILE_WORDS = ("file", "folder", "directory", "document")
_SPECIAL_LOCATIONS = {"desktop", "documents", "downloads", "pictures", "music", "videos"}
_APP_LIKE_FILE_PHRASES = ("file explorer", "windows explorer", "file manager")


def extract_app_name(text: str, match_text: str) -> str:
    """Extract an application name from text based on the matched prefix."""
    return text.replace(match_text, "", 1).strip()


def extract_search_query(text: str, match_text: str) -> str:
    """Extract a search query from text."""
    query = text.replace(match_text, "", 1).strip()
    if query.startswith("for "):
        query = query[4:].strip()
    elif query.startswith("about "):
        query = query[6:].strip()
    return query


def extract_system_control(text: str) -> dict:
    """Extracts control type and direction."""
    return entities_from_system_command(text)


def extract_file_action(text: str) -> dict:
    """Extract file-action details from natural language commands."""
    cleaned = text.strip()
    lowered = cleaned.lower()
    permanent = bool(re.search(r"\b(permanent|permanently|forever|shift delete)\b", lowered))

    create_match = re.match(
        r"^(?:create|make)(?:\s+(?:a|an|new))?\s+(?:(text|blank)\s+)?(?:file|document)\b(?:\s+(?!on\b|in\b|under\b)(.+?))?(?:\s+(?:on|in|under)\s+(.+?))?(?:\s+(?:with content|saying)\s+(.+))?$",
        cleaned,
        flags=re.IGNORECASE,
    )
    if create_match:
        text_file_hint = bool(create_match.group(1))
        filename = _strip_quotes(create_match.group(2) or "")
        location = _strip_quotes(create_match.group(3) or "")
        content = create_match.group(4).strip() if create_match.group(4) else ""
        if not filename:
            filename = "New Text Document.txt" if text_file_hint else "New Document.txt"
        elif text_file_hint and "." not in Path(filename).name:
            filename = f"{filename}.txt"
        return {
            "action": "create",
            "filename": filename,
            "destination": "",
            "new_name": "",
            "location": location,
            "source_location": "",
            "permanent": False,
            "content": content,
        }

    open_match = re.match(
        r"^(?:open|launch|start)\s+(?:the\s+)?(?:(?:file|folder|directory)\s+)?(.+)$",
        cleaned,
        flags=re.IGNORECASE,
    )
    if open_match and looks_like_file_reference(cleaned):
        filename, location = split_reference_and_location(open_match.group(1))
        return {
            "action": "open",
            "filename": filename,
            "destination": "",
            "new_name": "",
            "location": location,
            "source_location": location,
            "permanent": False,
            "content": "",
        }

    rename_match = re.match(r"^rename\s+(.+?)\s+to\s+(.+)$", cleaned, flags=re.IGNORECASE)
    if rename_match:
        filename, location = split_reference_and_location(rename_match.group(1))
        return {
            "action": "rename",
            "filename": filename,
            "destination": "",
            "new_name": _strip_quotes(rename_match.group(2)),
            "location": location,
            "source_location": location,
            "permanent": False,
            "content": "",
        }

    move_match = re.match(r"^(?:move|relocate)\s+(.+?)\s+to\s+(.+)$", cleaned, flags=re.IGNORECASE)
    if move_match:
        filename, source_location = split_reference_and_location(move_match.group(1))
        return {
            "action": "move",
            "filename": filename,
            "destination": _strip_quotes(move_match.group(2)),
            "new_name": "",
            "location": "",
            "source_location": source_location,
            "permanent": False,
            "content": "",
        }

    delete_match = re.match(
        r"^(?:(?:permanently\s+)?delete|remove|trash)\s+(?:the\s+)?(?:(?:file|folder|directory)\s+)?(.+)$",
        cleaned,
        flags=re.IGNORECASE,
    )
    if delete_match:
        filename, location = split_reference_and_location(delete_match.group(1))
        return {
            "action": "delete",
            "filename": filename,
            "destination": "",
            "new_name": "",
            "location": location,
            "source_location": location,
            "permanent": permanent,
            "content": "",
        }

    return {
        "action": "unknown",
        "filename": "",
        "destination": "",
        "new_name": "",
        "location": "",
        "source_location": "",
        "permanent": permanent,
        "content": "",
    }


def looks_like_file_reference(text: str) -> bool:
    lowered = " ".join(text.lower().strip().split())
    if any(phrase in lowered for phrase in _APP_LIKE_FILE_PHRASES):
        return False
    if any(word in lowered for word in _FILE_WORDS):
        return True
    if any(location in lowered for location in _SPECIAL_LOCATIONS):
        return True
    if any(sep in lowered for sep in ("\\", "/")):
        return True
    if re.search(r"\.[a-z0-9]{1,6}\b", lowered):
        return True
    return False


def split_reference_and_location(fragment: str) -> tuple[str, str]:
    text = _strip_quotes(fragment.strip())
    pattern = r"\s+(?:%s)\s+(.+)$" % "|".join(_FILE_LOCATION_PREPOSITIONS)
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return _normalize_special_location_reference(text), ""
    location = _strip_quotes(match.group(1))
    reference = text[: match.start()].strip()
    return _normalize_special_location_reference(reference), location


def _strip_quotes(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1].strip()
    return text


def _normalize_special_location_reference(value: str) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(
        r"(desktop|documents|downloads|pictures|music|videos)\s+(file|folder|directory|document)",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else text
