"""
Reusable assistant text responses.
"""
from datetime import datetime

def greeting_response(user_name: str = "User") -> str:
    """Returns a dynamic greeting."""
    return f"Hi {user_name}, how are you?"

def assistant_ready_response(assistant_name: str) -> str:
    return f"My name is {assistant_name}."


def status_response() -> str:
    return "I'm doing well and ready to help."


def thanks_response() -> str:
    return "You're welcome."

def name_changed_response(new_name: str) -> str:
    return f"Assistant name updated to {new_name}."

def unknown_response() -> str:
    """Returns a standard unknown command response."""
    return "I'm not sure how to help with that yet."

def error_response() -> str:
    """Returns a standard error response."""
    return "Something went wrong while processing your command."


def time_response() -> str:
    """Returns the current local time."""
    now_str = datetime.now().strftime("%I:%M %p")
    return f"The current time is {now_str}."

def ready_response() -> str:
    """Returns a standard ready response."""
    return "I am ready."

def help_response() -> str:
    """Returns a standard help response."""
    return "I can open apps, answer basic questions, and process commands."


def question_unavailable_response() -> str:
    return "I can answer time questions right now. For current web questions, ask me to search the web."

def open_app_response(app_name: str) -> str:
    """Returns an open app mock response."""
    return f"Opening {app_name}..."

def search_response(query: str, target: str | None = None) -> str:
    """Returns an honest acknowledgement of a search request."""
    if target:
        return f"Captured a search request for {target}: {query}."
    return f"Captured a search request: {query}."
