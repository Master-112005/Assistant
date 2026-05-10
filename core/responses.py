"""
Reusable assistant text responses.
"""
from datetime import datetime
import random

_GREETINGS = [
    "Hey there! ",
    "Hi! ",
    "Hello! ",
    "Hey! ",
    "Good to see you! ",
]

_THANKS = [
    "You're welcome! ",
    "No problem at all! ",
    "Happy to help! ",
    "Anytime! ",
    "Glad I could help! ",
]

_UNKNOWN_OPTIONS = [
    "I'm not sure I got that. Can you try saying it differently?",
    "Hmm, I didn't catch that. What were you trying to do?",
    "That's new to me! Could you rephrase that?",
    "I didn't quite understand. Try saying it another way?",
    "What was that? I missed it - can you say it again?",
]

_ERROR_OPTIONS = [
    "Oops! Something went wrong. Let's try that again.",
    "That didn't work as expected. Want to give it another shot?",
    "Ah, something hiccuped there. Let's try once more!",
    "Well, that didn't go as planned. Ready to try again?",
    "Something went sideways there. Let's give it another go, shall we?",
]


def greeting_response(user_name: str = "User") -> str:
    """Returns a dynamic greeting."""
    openers = [
        "Hey there! ",
        "Hi! ",
        "Hello! ",
        "Hey! ",
    ]
    responses = [
        "How's it going?",
        "How are you doing?",
        "What's up?",
        "Good to see you!",
    ]
    return f"{random.choice(openers)}{random.choice(responses)}"

def assistant_ready_response(assistant_name: str) -> str:
    return f"My name is {assistant_name}. I'm here to help!"

def status_response() -> str:
    return "I'm doing great and ready to help you out!"

def thanks_response() -> str:
    opener = random.choice(_THANKS)
    return f"{opener}Let me know if you need anything else!"

def name_changed_response(new_name: str) -> str:
    return f"Got it! I'll go by {new_name} from now on."

def unknown_response() -> str:
    """Returns a natural unknown command response."""
    return random.choice(_UNKNOWN_OPTIONS)

def error_response() -> str:
    """Returns a more natural error response."""
    return random.choice(_ERROR_OPTIONS)

def time_response() -> str:
    """Returns the current local time in a friendly way."""
    now_str = datetime.now().strftime("%I:%M %p")
    return f"It's {now_str}."

def ready_response() -> str:
    """Returns a friendly ready response."""
    responses = [
        "I'm all set and ready to go!",
        "Ready when you are!",
        "Here and waiting!",
        "What's up?",
    ]
    return random.choice(responses)

def help_response() -> str:
    """Returns a helpful response about capabilities."""
    return "I can help you open apps, play music, send messages, search the web, manage windows, answer questions, and lots more. Just ask!"

def question_unavailable_response() -> str:
    return "I can answer time and date questions right now. For anything else, just ask me to search the web!"

def open_app_response(app_name: str) -> str:
    """Returns a friendly open app response."""
    openers = ["Sure!", "Got it!", "On it!", "Okay!", "Opening"]
    return f"{random.choice(openers)} {app_name}..."

def search_response(query: str, target: str | None = None) -> str:
    """Returns a friendly search acknowledgment."""
    if target:
        return f"Looking up '{query}' on {target}..."
    return f"Searching for '{query}'..."


def message_sent_response(contact: str, message: str) -> str:
    """Returns a natural message sent confirmation."""
    templates = [
        f"Done! Just sent '{message}' to {contact}.",
        f"Perfect! I messaged {contact} for you.",
        f"Sent! '{message}' is now on its way to {contact}.",
        f"Okay! Told {contact}: {message}",
        f"All set! Just pinged {contact} with your message.",
    ]
    return random.choice(templates)

def app_open_response(app: str) -> str:
    """Returns a casual app open confirmation."""
    templates = [
        f"Launching {app} now...",
        f"Opening {app}...",
        f"Got it! Starting {app}...",
        f"{app} is on its way!",
    ]
    return random.choice(templates)

def multi_action_response(actions: list[str]) -> str:
    """Returns confirmation for multi-step actions."""
    if not actions:
        return "Okay, doing what you asked..."
    
    action_count = len(actions)
    if action_count == 1:
        return f"I'll {actions[0]}..."
    
    if action_count == 2:
        return f"First {actions[0]}, then {actions[1]}..."
    
    return f"Working through {action_count} steps..."
