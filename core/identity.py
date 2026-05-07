"""
Manage names and identity logic.
"""
import re
from core import settings
from core import state
from core.logger import get_logger

logger = get_logger(__name__)

class IdentityManager:
    def __init__(self):
        self.load_identity()

    def load_identity(self):
        """Loads identity values from settings."""
        state.assistant_name = settings.get("assistant_name") or "Nova"
        state.user_name = settings.get("user_name") or "User"
        state.identity_loaded = True
        logger.info(f"Identity loaded - Assistant: {state.assistant_name}, User: {state.user_name}")

    def get_assistant_name(self) -> str:
        return state.assistant_name

    def set_assistant_name(self, name: str):
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Assistant name cannot be empty.")
        if len(cleaned) > 30:
            raise ValueError("Assistant name too long (max 30 chars).")
            
        settings.set("assistant_name", cleaned)
        state.assistant_name = cleaned
        logger.info(f"Assistant renamed to {cleaned}")

    def get_user_name(self) -> str:
        return state.user_name

    def set_user_name(self, name: str):
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("User name cannot be empty.")
        if len(cleaned) > 30:
            raise ValueError("User name too long (max 30 chars).")
            
        settings.set("user_name", cleaned)
        state.user_name = cleaned
        logger.info(f"User name updated to {cleaned}")

    def format_title(self) -> str:
        """Returns the formatted window title."""
        return f"{self.get_assistant_name()} Assistant"

    def is_addressed(self, text: str) -> bool:
        """Returns True if the assistant's name is explicitly mentioned in the text."""
        name_lower = self.get_assistant_name().lower()
        text_lower = text.lower()
        
        # Simple word boundary check to avoid partial matches
        pattern = r'\b' + re.escape(name_lower) + r'\b'
        match = bool(re.search(pattern, text_lower))
        state.last_addressed = match
        
        if match:
            logger.info("Addressed command detected")
            
        return match

    def strip_assistant_name(self, text: str) -> str:
        """Removes the assistant's name from the beginning of the text."""
        name_lower = self.get_assistant_name().lower()
        text_lower = text.lower()
        
        # We strip the name if it's at the beginning (with or without greetings/commas)
        # e.g. "Nova open chrome" -> "open chrome"
        # e.g. "Nova, open chrome" -> "open chrome"
        # e.g. "Hi Nova open chrome" -> "open chrome" (optional, but let's just strip exact start prefixes for now)
        
        prefixes_to_strip = [
            f"{name_lower}, ",
            f"{name_lower} ",
            f"hey {name_lower}, ",
            f"hey {name_lower} ",
            f"hi {name_lower}, ",
            f"hi {name_lower} ",
            f"hello {name_lower}, ",
            f"hello {name_lower} "
        ]
        
        stripped = text
        for prefix in prefixes_to_strip:
            if text_lower.startswith(prefix):
                stripped = text[len(prefix):].strip()
                break
                
        # Handle exact match (e.g. just "Nova")
        if text_lower == name_lower or text_lower in [f"hi {name_lower}", f"hey {name_lower}", f"hello {name_lower}"]:
            return ""
            
        return stripped
