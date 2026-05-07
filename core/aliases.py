"""
Manage app aliases.
"""
import json
import os
from typing import Dict
from core.paths import DATA_DIR
from core.logger import get_logger

logger = get_logger(__name__)

ALIASES_FILE = DATA_DIR / "aliases.json"

class AliasManager:
    def __init__(self):
        self.aliases: Dict[str, str] = {}
        self.load_aliases()

    def load_aliases(self):
        """Loads aliases from JSON file."""
        if not ALIASES_FILE.exists():
            # Create default if missing
            self.aliases = {
                "chrome": "Google Chrome",
                "cmd": "Command Prompt",
                "calc": "Calculator",
                "paint": "Paint",
                "word": "Word",
                "excel": "Excel",
                "powerpoint": "PowerPoint",
                "vscode": "Visual Studio Code",
                "edge": "Microsoft Edge",
                "spotify": "Spotify",
                "whatsapp": "WhatsApp"
            }
            try:
                with open(ALIASES_FILE, "w", encoding="utf-8") as f:
                    json.dump(self.aliases, f, indent=4)
            except Exception as e:
                logger.error(f"Failed to create aliases.json: {e}")
        else:
            try:
                with open(ALIASES_FILE, "r", encoding="utf-8") as f:
                    self.aliases = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load aliases.json: {e}")
                self.aliases = {}

    def resolve_alias(self, name: str) -> str:
        """Resolves an alias to the real app name, if it exists."""
        lower_name = name.lower().strip()
        # Direct alias check
        if lower_name in self.aliases:
            return self.aliases[lower_name]
            
        # Reverse check (e.g. if someone queries "Google Chrome", we return it)
        # Usually not needed but good for normalization
        for alias, real_name in self.aliases.items():
            if lower_name == real_name.lower():
                return real_name
                
        return name

    def add_alias(self, alias: str, app_name: str):
        """Adds a new custom alias."""
        self.aliases[alias.lower().strip()] = app_name.strip()
        try:
            with open(ALIASES_FILE, "w", encoding="utf-8") as f:
                json.dump(self.aliases, f, indent=4)
            logger.info(f"Added alias: {alias} -> {app_name}")
        except Exception as e:
            logger.error(f"Failed to save new alias: {e}")
