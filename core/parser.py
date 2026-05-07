"""
Text command parser and sanitizer.
"""
import re

def clean_text(text: str) -> str:
    """Trims and removes extra spaces from the input text."""
    if not text:
        return ""
    # Replace multiple spaces with a single space and strip
    cleaned = re.sub(r'\s+', ' ', text)
    return cleaned.strip()

def tokenize(text: str) -> list[str]:
    """Converts cleaned text into a list of lowercase tokens."""
    cleaned = clean_text(text)
    if not cleaned:
        return []
    return cleaned.lower().split(' ')

def is_empty(text: str) -> bool:
    """Checks if the text is empty or just whitespace."""
    return not clean_text(text)
