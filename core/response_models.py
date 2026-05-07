"""
Production-grade response models for the assistant.

All assistant outputs must conform to these contracts.
"""
from dataclasses import dataclass, field
from typing import Any, Optional
from datetime import datetime
from enum import Enum
import uuid


class ResponseCategory(str, Enum):
    """Response classification for routing and behavior."""
    GREETING = "greeting"
    COMMAND_RESULT = "command_result"
    CONFIRMATION = "confirmation"
    ERROR = "error"
    INFO = "info"
    WARNING = "warning"
    SYSTEM = "system"
    CLARIFICATION = "clarification"
    MEDIA_CONTROL = "media_control"
    NOTIFICATION = "notification"


class ResponseSeverity(str, Enum):
    """Urgency/importance level."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class AssistantResponse:
    """
    Strongly-typed response contract.

    Every assistant-visible output must be converted to this form
    and routed through ResponseService.
    """
    # Identification
    response_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)

    # Core content
    text: str = ""
    category: ResponseCategory = ResponseCategory.INFO

    # Success/failure
    success: bool = True
    severity: ResponseSeverity = ResponseSeverity.INFO
    error_code: Optional[str] = None
    error_details: Optional[str] = None

    # Speech behavior
    speak_enabled: bool = True
    silent_reason: Optional[str] = None  # Why we're not speaking

    # Notifications
    notification_enabled: bool = False
    notification_title: Optional[str] = None
    notification_icon: Optional[str] = None

    # Action tracking
    source_skill: Optional[str] = None
    action_name: Optional[str] = None
    action_result: Optional[Any] = None

    # Context
    entities: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for serialization."""
        return {
            "response_id": self.response_id,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at.isoformat(),
            "text": self.text,
            "category": self.category.value,
            "success": self.success,
            "severity": self.severity.value,
            "error_code": self.error_code,
            "error_details": self.error_details,
            "speak_enabled": self.speak_enabled,
            "silent_reason": self.silent_reason,
            "notification_enabled": self.notification_enabled,
            "notification_title": self.notification_title,
            "notification_icon": self.notification_icon,
            "source_skill": self.source_skill,
            "action_name": self.action_name,
            "entities": self.entities,
            "metadata": self.metadata,
        }


@dataclass
class TTSSpeechEvent:
    """Telemetry for each spoken utterance."""
    tts_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    response_id: Optional[str] = None
    text: str = ""
    text_length: int = 0
    queued_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    engine_restart_count: int = 0
    status: str = "queued"  # queued, speaking, finished, failed, cancelled
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for logging."""
        return {
            "tts_id": self.tts_id,
            "response_id": self.response_id,
            "text": self.text,
            "text_length": self.text_length,
            "queued_at": self.queued_at.isoformat() if self.queued_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "engine_restart_count": self.engine_restart_count,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class ConfirmationToken:
    """Represents a pending action that requires user confirmation."""
    token_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None

    action_type: str = ""  # e.g., "delete_file", "shutdown", "send_message"
    action_payload: dict[str, Any] = field(default_factory=dict)  # Serialized action data

    risk_level: str = "medium"  # low, medium, high
    source_command: str = ""  # Original command text
    prompt_text: str = ""  # Text shown to user

    state: str = "pending"  # pending, confirmed, cancelled, expired, executing, completed
    confirmed_at: Optional[datetime] = None

    def is_expired(self) -> bool:
        """Check if token has expired."""
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for logging."""
        return {
            "token_id": self.token_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "action_type": self.action_type,
            "risk_level": self.risk_level,
            "source_command": self.source_command,
            "state": self.state,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
        }
