"""
Comprehensive vocabulary for STT correction: apps, system terms, verbs.
Supports custom term loading for personalization.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.logger import get_logger
from core.paths import DATA_DIR

logger = get_logger(__name__)

CUSTOM_TERMS_PATH = DATA_DIR / "custom_terms.json"


class Vocabulary:
    """Known app names, system terms, and action verbs for correction boosting."""

    # Windows system apps
    WINDOWS_APPS = {
        "chrome": {"aliases": ["crome", "chromee", "chrom"]},
        "edge": {"aliases": ["edgy", "edge browser"]},
        "firefox": {"aliases": ["fire fox", "firefox"]},
        "notepad": {"aliases": ["note pad", "notepad++"]},
        "calculator": {"aliases": ["calc", "calculator app"]},
        "paint": {"aliases": ["paint app", "ms paint"]},
        "wordpad": {"aliases": ["word pad"]},
        "explorer": {"aliases": ["file explorer", "explorer"]},
        "settings": {"aliases": ["setting", "windows settings"]},
        "task manager": {"aliases": ["task mgr", "taskmgr"]},
        "command prompt": {"aliases": ["cmd", "command line", "terminal"]},
        "powershell": {"aliases": ["power shell", "ps"]},
        "notepad++": {"aliases": ["notepad plus", "npp"]},
        "visual studio code": {"aliases": ["vscode", "code", "vs code"]},
        "visual studio": {"aliases": ["vs", "studio"]},
        "office": {"aliases": ["microsoft office", "ms office"]},
        "word": {"aliases": ["ms word", "microsoft word"]},
        "excel": {"aliases": ["spreadsheet", "ms excel"]},
        "powerpoint": {"aliases": ["ppt", "presentation", "power point"]},
        "outlook": {"aliases": ["email", "mail"]},
    }

    # Media apps
    MEDIA_APPS = {
        "spotify": {"aliases": ["spotfy", "spotfiy", "spotify app"]},
        "apple music": {"aliases": ["app music", "apl music", "apple musc"]},
        "youtube": {"aliases": ["yutube", "youtube app", "u tube"]},
        "netflix": {"aliases": ["netflicks", "netflix app"]},
        "vlc": {"aliases": ["vlc player", "vlc media"]},
        "media player": {"aliases": ["windows media player", "media"]},
        "itunes": {"aliases": ["i tunes"]},
    }

    # Communication apps
    COMMUNICATION_APPS = {
        "whatsapp": {"aliases": ["watsapp", "whatsup", "whats app"]},
        "telegram": {"aliases": ["telegrama", "telegram app", "telagram"]},
        "discord": {"aliases": ["discored", "discord app"]},
        "skype": {"aliases": ["skyp"]},
        "teams": {"aliases": ["microsoft teams", "team", "teams app"]},
        "zoom": {"aliases": ["zom", "zoom app"]},
        "slack": {"aliases": ["slack app"]},
    }

    # System control terms
    SYSTEM_TERMS = {
        "volume": {"aliases": ["sound", "audio", "vol"]},
        "brightness": {"aliases": ["screen brightness", "bright", "backlight"]},
        "shutdown": {"aliases": ["shut down", "power off", "turn off"]},
        "restart": {"aliases": ["reboot", "re-start", "restart pc"]},
        "sleep": {"aliases": ["go to sleep", "hibernate"]},
        "lock": {"aliases": ["lock screen", "lock pc"]},
        "unlock": {"aliases": ["unlock screen"]},
        "mute": {"aliases": ["silent", "quiet"]},
        "unmute": {"aliases": ["unmute", "sound on"]},
        "increase": {"aliases": ["up", "raise", "higher", "louder", "brighter"]},
        "decrease": {"aliases": ["down", "lower", "quieter", "dimmer"]},
    }

    # Common action verbs
    ACTION_VERBS = {
        "open": {"aliases": ["opun", "open up", "launch", "start", "run"]},
        "search": {"aliases": ["serch", "sarch", "find", "look up", "look for"]},
        "play": {"aliases": ["pley", "plae", "ply", "start playing"]},
        "stop": {"aliases": ["stap", "pause", "quit"]},
        "close": {"aliases": ["clos", "close app", "exit"]},
        "delete": {"aliases": ["delet", "remove", "erase"]},
        "move": {"aliases": ["mov", "shift"]},
        "copy": {"aliases": ["copi", "duplicate"]},
        "paste": {"aliases": ["pase"]},
        "save": {"aliases": ["sav", "save file"]},
        "create": {"aliases": ["creat", "make", "new"]},
        "rename": {"aliases": ["rename", "call it"]},
        "minimize": {"aliases": ["minimize", "shrink"]},
        "maximize": {"aliases": ["maximize", "expand"]},
        "fullscreen": {"aliases": ["full screen", "f11"]},
    }

    # Common entities
    FILE_TYPES = {
        "document": {"aliases": ["doc", "docx", "txt", "file"]},
        "image": {"aliases": ["img", "png", "jpg", "jpeg", "picture", "photo"]},
        "video": {"aliases": ["mp4", "avi", "mov", "mkv"]},
        "audio": {"aliases": ["mp3", "wav", "flac", "m4a"]},
        "folder": {"aliases": ["directory", "dir"]},
        "zip": {"aliases": ["archive", "compressed"]},
    }

    def __init__(self) -> None:
        self._custom_terms: dict[str, dict[str, Any]] = {}
        self._load_custom_terms()

    def load_custom_terms(self) -> dict[str, dict[str, Any]]:
        """Load custom correction terms from file."""
        self._load_custom_terms()
        return self._custom_terms

    def _load_custom_terms(self) -> None:
        """Internal: Load custom terms from JSON file."""
        if not CUSTOM_TERMS_PATH.exists():
            logger.info("No custom terms file found at %s", CUSTOM_TERMS_PATH)
            self._custom_terms = {}
            return

        try:
            with open(CUSTOM_TERMS_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._custom_terms = loaded
                    logger.info("Loaded %d custom terms", len(self._custom_terms))
                else:
                    logger.warning("Custom terms file is not a JSON object")
                    self._custom_terms = {}
        except Exception as e:
            logger.error("Failed to load custom terms: %s", e)
            self._custom_terms = {}

    def add_term(self, term: str, aliases: list[str] | None = None) -> None:
        """Add a custom term with optional aliases."""
        if not term or not isinstance(term, str):
            return

        term_lower = term.lower().strip()
        if not term_lower:
            return

        if term_lower not in self._custom_terms:
            self._custom_terms[term_lower] = {"aliases": []}

        if aliases:
            existing = set(self._custom_terms[term_lower].get("aliases", []))
            existing.update(alias.lower().strip() for alias in aliases if alias)
            self._custom_terms[term_lower]["aliases"] = list(existing)

        self._save_custom_terms()

    def _save_custom_terms(self) -> None:
        """Internal: Persist custom terms to JSON file."""
        try:
            CUSTOM_TERMS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(CUSTOM_TERMS_PATH, "w", encoding="utf-8") as f:
                json.dump(self._custom_terms, f, indent=2, ensure_ascii=True)
        except Exception as e:
            logger.error("Failed to save custom terms: %s", e)

    def get_all_apps(self) -> dict[str, dict[str, Any]]:
        """Get all known app names with aliases."""
        all_apps = {}
        all_apps.update(self.WINDOWS_APPS)
        all_apps.update(self.MEDIA_APPS)
        all_apps.update(self.COMMUNICATION_APPS)
        all_apps.update(self._custom_terms)
        return all_apps

    def get_all_verbs(self) -> dict[str, dict[str, Any]]:
        """Get all known action verbs."""
        return self.ACTION_VERBS.copy()

    def get_all_system_terms(self) -> dict[str, dict[str, Any]]:
        """Get all known system control terms."""
        return self.SYSTEM_TERMS.copy()

    def is_known_app(self, text: str) -> bool:
        """Check if text is a known app name or alias."""
        text_lower = text.lower().strip()
        apps = self.get_all_apps()

        for app_name, data in apps.items():
            if text_lower == app_name:
                return True
            aliases = data.get("aliases", [])
            if any(text_lower == alias.lower() for alias in aliases):
                return True
        return False

    def is_known_verb(self, text: str) -> bool:
        """Check if text is a known action verb."""
        text_lower = text.lower().strip()

        for verb_name, data in self.ACTION_VERBS.items():
            if text_lower == verb_name:
                return True
            aliases = data.get("aliases", [])
            if any(text_lower == alias.lower() for alias in aliases):
                return True
        return False

    def is_known_system_term(self, text: str) -> bool:
        """Check if text is a known system term."""
        text_lower = text.lower().strip()

        for term_name, data in self.SYSTEM_TERMS.items():
            if text_lower == term_name:
                return True
            aliases = data.get("aliases", [])
            if any(text_lower == alias.lower() for alias in aliases):
                return True
        return False

    def normalize_term(self, text: str) -> str | None:
        """Return canonical form of term if it's known, else None."""
        text_lower = text.lower().strip()

        # Check apps
        for app_name, data in self.get_all_apps().items():
            if text_lower == app_name:
                return app_name.title()
            aliases = data.get("aliases", [])
            if any(text_lower == alias.lower() for alias in aliases):
                return app_name.title()

        # Check verbs
        for verb_name, data in self.ACTION_VERBS.items():
            if text_lower == verb_name:
                return verb_name
            aliases = data.get("aliases", [])
            if any(text_lower == alias.lower() for alias in aliases):
                return verb_name

        # Check system terms
        for term_name, data in self.SYSTEM_TERMS.items():
            if text_lower == term_name:
                return term_name
            aliases = data.get("aliases", [])
            if any(text_lower == alias.lower() for alias in aliases):
                return term_name

        return None


# Global vocabulary instance
_vocab_instance: Vocabulary | None = None


def get_vocabulary() -> Vocabulary:
    """Get or create the global vocabulary instance."""
    global _vocab_instance
    if _vocab_instance is None:
        _vocab_instance = Vocabulary()
    return _vocab_instance
