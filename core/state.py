"""
Track runtime state.
"""

# Global Application State Variables

# True if the live microphone listener is currently active
is_listening: bool = False
voice_state: str = "idle"

# True if assistant is currently processing data (e.g. STT)
is_processing: bool = False

# Explicit state machine states (Section 16 of spec)
# Valid states: READY, LISTENING, PROCESSING, EXECUTING, SPEAKING, ERROR
assistant_state: str = "READY"
_state_history: list = []  # Bounded history for debugging

# Path to the last recorded audio file
last_audio_path: str = ""

# The most recent error message, if any
last_error: str = ""
last_recovery_plan: dict = {}
pending_recovery_choices: list = []
recovery_stats: dict = {}

# STT runtime state
last_transcript: str = ""
last_stt_time: float = 0.0
stt_loaded: bool = False

# Command Processor runtime state
last_input_text: str = ""
last_intent: str = ""
last_confidence: float = 0.0
last_entities: dict = {}
last_response: str = ""
command_count: int = 0
intent_count_by_type: dict = {}

# Identity runtime state
assistant_name: str = ""
user_name: str = ""
last_addressed: bool = False
identity_loaded: bool = False

# TTS runtime state
tts_ready: bool = False
is_speaking: bool = False
last_spoken_text: str = ""

# Launcher runtime state
last_launched_app: str = ""
last_launch_success: bool = False
app_index_count: int = 0
last_launch_pid: int = -1

# STT correction runtime state
last_raw_transcript: str = ""
last_corrected_transcript: str = ""
last_correction_confidence: float = 0.0
last_correction_method: str = ""
correction_applied: bool = False

# Planner runtime state
last_plan = None
last_plan_steps: int = 0
last_plan_confidence: float = 0.0
last_planner_used: str = ""
plan_count: int = 0
last_plan_time: float = 0.0

# Phase 12: Execution engine runtime state
is_executing: bool = False
current_step: int = 0
current_plan_id: str = ""
last_execution_result = None
cancel_requested: bool = False

# Phase 13: Active Window Context state

# Normalized app identifier for the current runtime context.
# Kept in sync with current_app for backward compatibility.
current_context: str = "unknown"

# Normalized app identifier for the currently focused window
# Examples: "chrome", "youtube", "whatsapp", "explorer", "unknown"
current_app: str = "unknown"

# Full window title text of the active window
current_window_title: str = ""

# Executable filename of the active process (e.g. "chrome.exe")
current_process_name: str = ""

# Monotonic timestamp of the last context change (0 = never detected)
last_context_change: float = 0.0

# Bounded history of recent context changes
# Each entry: {"app_id": str, "title": str, "process_name": str, "timestamp": float}
context_history: list = []

# Hook for future selected-file or selected-element context providers.
selected_item_context: dict = {}

# Phase 14: Context Engine runtime state

# The most recent ContextDecision dict produced by the context engine
last_context_decision: dict = {}

# Rolling list of recent commands (bounded, mirrors ContextEngine history)
# Each entry: {"text": str, "intent": str, "target_app": str, "success": bool}
recent_commands: list = []

# Description of the last action that completed successfully
last_successful_action: str = ""
last_target_app: str = ""
last_replayable_command: str = ""
last_window_action: dict = {}

# Total number of context resolution calls made this session
context_resolution_count: int = 0

# Phase 15: Browser automation runtime state
last_browser: str = ""
last_search_query: str = ""
last_browser_action: str = ""
browser_ready: bool = False
last_navigation_time: float = 0.0

# Phase 16: Skill/plugin runtime state
active_skill: str = ""
last_skill_used: str = ""
last_chrome_action: str = ""
chrome_tabs_opened_count: int = 0
last_page_title: str = ""

# Phase 17: YouTube skill runtime state
last_youtube_query: str = ""
last_video_title: str = ""
youtube_active: bool = False
last_media_action: str = ""

# Phase 18: WhatsApp skill runtime state
whatsapp_active: bool = False
last_contact_search: dict = {}
pending_contact_choices: list = []
pending_whatsapp_message: dict = {}
last_message_target: str = ""
last_chat_name: str = ""

# Phase 19: Music skill runtime state
music_active: bool = False
active_music_provider: str = ""
last_track_name: str = ""
last_artist_name: str = ""

# Phase 21: Screen awareness runtime state
last_awareness_report: dict = {}
last_desktop_snapshot: dict = {}
awareness_ready: bool = False
last_visible_apps: list = []

# Phase 22: Click-by-text runtime state
last_text_click_target: dict = {}
last_text_click_result: dict = {}
last_clicked_position: dict = {}
text_click_count: int = 0

# Phase 23: File-management runtime state
last_file_action: str = ""
last_file_path: str = ""
last_destination_path: str = ""
pending_confirmation: dict = {}
recent_files_touched: list = []

# Phase 28: System controls runtime state
last_system_action: dict = {}
last_volume: int = -1
last_brightness: int = -1
wifi_state: str = "unknown"
bluetooth_state: str = "unknown"

# Phase 29: Permission framework runtime state
permission_level: str = "BASIC"
pending_confirmations: dict = {}
temporary_grants: list = []
last_permission_decision: dict = {}
last_denied_action: dict = {}

# Phase 30: Safety guard runtime state
last_safety_check: dict = {}
pending_safety_confirmations: dict = {}
last_warning_message: str = ""
last_confirmed_action: dict = {}

# Phase 24: Smart file-search runtime state
last_file_search_query: dict = {}
last_file_search_results: list = []
pending_file_choices: list = []
file_index_ready: bool = False

# Phase 25: Clipboard runtime state
clipboard_ready: bool = False
last_clipboard_item: dict = {}
clipboard_count: int = 0
pending_clipboard_choices: list = []

# Phase 26: Reminder runtime state
scheduler_running: bool = False
next_reminder_time: str = ""
last_triggered_reminder: dict = {}
reminder_count: int = 0

# Phase 27: Notification runtime state
notifications_ready: bool = False
notification_queue_size: int = 0
last_notification: dict = {}

# Phase 31: Memory runtime state
memory_ready: bool = False
last_memory_hit: dict = {}
last_saved_preference: dict = {}
recent_workflow_id: int | None = None

# Phase 32: Workflow memory runtime state
last_workflow_id: int | None = None
last_replayed_workflow: dict = {}
pending_workflow_choices: list = []
workflow_memory_ready: bool = False

# Phase 33: Personalization runtime state
personalization_ready: bool = False
last_preference_used: dict = {}
last_signal_recorded: dict = {}
active_profile_summary: list = []

# Phase 34: Conversation memory runtime state
conversation_ready: bool = False
last_resolved_reference: dict = {}
recent_entities: list = []
session_turn_count: int = 0

# Phase 35: Logging & analytics runtime state
current_session_id: str = ""
last_command_latency_ms: float = 0.0
last_error_id: str = ""
metrics_ready: bool = False
analytics_ready: bool = False

# Phase 36: Plugin manager runtime state
plugins_ready: bool = False
loaded_plugins: list = []
plugin_errors: dict = {}
last_plugin_used: str = ""

recent_notifications: list = []

# State machine transition function
def set_state(new_state: str, reason: str = "") -> None:
    """Transition to a new state with validation and history tracking."""
    global assistant_state, _state_history

    valid_states = {"READY", "LISTENING", "PROCESSING", "EXECUTING", "SPEAKING", "ERROR"}
    if new_state not in valid_states:
        from core.logger import get_logger
        get_logger(__name__).warning("Invalid state transition: %s", new_state)
        return

    old_state = assistant_state
    assistant_state = new_state

    # Track history (bounded to last 50 transitions)
    _state_history.append({
        "from": old_state,
        "to": new_state,
        "reason": reason,
        "timestamp": __import__("time").time()
    })
    if len(_state_history) > 50:
        _state_history = _state_history[-50:]

def get_state() -> str:
    """Get current assistant state."""
    return assistant_state

def get_state_history() -> list:
    """Get recent state transition history."""
    return list(_state_history)

# Phase 38: Themes and UI polish runtime state
theme_loaded: str = ""
orb_visible: bool = False
sidebar_page: str = "chat"
animations_ready: bool = False
ui_mode: str = "desktop"
