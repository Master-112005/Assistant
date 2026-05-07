"""
Universal assistant response orchestration.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import threading
from typing import Any, Callable, Optional

from core.analytics import analytics
from core.logger import get_logger
from core.metrics import metrics
from core.response_models import (
    AssistantResponse,
    ConfirmationToken,
    ResponseCategory,
    ResponseSeverity,
    TTSSpeechEvent,
)

logger = get_logger(__name__)


def _coerce_response_category(category: ResponseCategory | str) -> ResponseCategory:
    if isinstance(category, ResponseCategory):
        return category
    normalized = str(category or "").strip().lower()
    if not normalized:
        return ResponseCategory.INFO
    aliases = {
        "action": ResponseCategory.COMMAND_RESULT,
        "chat": ResponseCategory.COMMAND_RESULT,
        "command": ResponseCategory.COMMAND_RESULT,
        "media": ResponseCategory.MEDIA_CONTROL,
        "system": ResponseCategory.SYSTEM,
        "warn": ResponseCategory.WARNING,
    }
    normalized = aliases.get(normalized, normalized)
    return ResponseCategory._value2member_map_.get(normalized, ResponseCategory.INFO)


def _coerce_response_severity(severity: ResponseSeverity | str) -> ResponseSeverity:
    if isinstance(severity, ResponseSeverity):
        return severity
    normalized = str(severity or "").strip().lower()
    if normalized == "warn":
        normalized = ResponseSeverity.WARNING.value
    return ResponseSeverity._value2member_map_.get(normalized, ResponseSeverity.INFO)


class ResponseService:
    """
    The single assistant-visible output path.

    Every assistant response is normalized to `AssistantResponse` and then
    dispatched in a fixed order: persist, UI, notification, TTS, metrics.
    """

    def __init__(
        self,
        ui_callback: Optional[Callable[[str, str], None]] = None,
        tts_callback: Optional[Callable[..., Any]] = None,
        notification_callback: Optional[Callable[[str, Optional[str]], None]] = None,
    ) -> None:
        self.ui_callback = ui_callback
        self.tts_callback = tts_callback
        self.notification_callback = notification_callback
        self._lock = threading.RLock()
        self._response_history: list[AssistantResponse] = []
        self._tts_telemetry: list[TTSSpeechEvent] = []
        self._pending_confirmations: dict[str, ConfirmationToken] = {}

    def set_callbacks(
        self,
        *,
        ui_callback: Optional[Callable[[str, str], None]] = None,
        tts_callback: Optional[Callable[..., Any]] = None,
        notification_callback: Optional[Callable[[str, Optional[str]], None]] = None,
    ) -> None:
        if ui_callback is not None:
            self.ui_callback = ui_callback
        if tts_callback is not None:
            self.tts_callback = tts_callback
        if notification_callback is not None:
            self.notification_callback = notification_callback

    def respond(
        self,
        text: str,
        *,
        category: ResponseCategory | str = ResponseCategory.INFO,
        success: bool = True,
        severity: ResponseSeverity | str = ResponseSeverity.INFO,
        speak_enabled: bool = True,
        silent_reason: Optional[str] = None,
        notification_enabled: bool = False,
        notification_title: Optional[str] = None,
        source_skill: Optional[str] = None,
        action_name: Optional[str] = None,
        correlation_id: Optional[str] = None,
        entities: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> AssistantResponse:
        response = AssistantResponse(
            text=str(text or ""),
            category=_coerce_response_category(category),
            success=bool(success),
            severity=_coerce_response_severity(severity),
            speak_enabled=bool(speak_enabled),
            silent_reason=silent_reason,
            notification_enabled=bool(notification_enabled),
            notification_title=notification_title,
            source_skill=source_skill,
            action_name=action_name,
            correlation_id=correlation_id,
            entities=dict(entities or {}),
            metadata=dict(metadata or {}),
        )
        return self._dispatch(response)

    def respond_error(
        self,
        text: str,
        *,
        error_code: Optional[str] = None,
        error_details: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> AssistantResponse:
        response = AssistantResponse(
            text=str(text or ""),
            category=ResponseCategory.ERROR,
            success=False,
            severity=ResponseSeverity.ERROR,
            error_code=error_code,
            error_details=error_details,
            correlation_id=correlation_id,
            source_skill="system",
        )
        return self._dispatch(response)

    def respond_confirmation(
        self,
        prompt_text: str,
        *,
        action_type: str,
        action_payload: dict[str, Any],
        risk_level: str = "medium",
        expires_in_seconds: int = 60,
        correlation_id: Optional[str] = None,
    ) -> ConfirmationToken:
        token = ConfirmationToken(
            action_type=str(action_type or ""),
            action_payload=dict(action_payload or {}),
            risk_level=str(risk_level or "medium"),
            source_command=str(prompt_text or ""),
            prompt_text=str(prompt_text or ""),
            expires_at=datetime.utcnow() + timedelta(seconds=max(1, int(expires_in_seconds))),
        )
        with self._lock:
            self._pending_confirmations[token.token_id] = token
        self.respond(
            prompt_text,
            category=ResponseCategory.CONFIRMATION,
            success=True,
            speak_enabled=True,
            notification_enabled=False,
            correlation_id=correlation_id,
            metadata={"token_id": token.token_id, "action_type": action_type, "risk_level": risk_level},
        )
        return token

    def consume_confirmation(self, token_id: str) -> Optional[ConfirmationToken]:
        with self._lock:
            token = self._pending_confirmations.get(token_id)
            if token is None:
                return None
            if token.is_expired():
                token.state = "expired"
                self._pending_confirmations.pop(token_id, None)
                return None
            token.state = "confirmed"
            token.confirmed_at = datetime.utcnow()
            self._pending_confirmations.pop(token_id, None)
            return token

    def has_pending_confirmation(self) -> bool:
        with self._lock:
            return bool(self._pending_confirmations)

    def get_response_history(self, limit: int = 50) -> list[AssistantResponse]:
        with self._lock:
            return list(self._response_history[-max(0, int(limit)) :])

    def get_tts_telemetry(self, limit: int = 100) -> list[TTSSpeechEvent]:
        with self._lock:
            return list(self._tts_telemetry[-max(0, int(limit)) :])

    def get_pending_confirmation_state(self) -> dict[str, Any]:
        with self._lock:
            pending = [
                {
                    "token_id": token.token_id,
                    "action_type": token.action_type,
                    "risk_level": token.risk_level,
                    "prompt_text": token.prompt_text,
                    "expires_at": token.expires_at.isoformat() if token.expires_at else None,
                    "is_expired": token.is_expired(),
                }
                for token in self._pending_confirmations.values()
            ]
        return {"has_pending": bool(pending), "pending_confirmations": pending}

    def shutdown(self) -> None:
        logger.info(
            "ResponseService shutdown",
            total_responses=len(self._response_history),
            total_tts_events=len(self._tts_telemetry),
        )

    def _dispatch(self, response: AssistantResponse) -> AssistantResponse:
        with self._lock:
            self._response_history.append(response)

        self._persist_response(response)
        self._render_ui(response)
        self._notify(response)
        tts_event = self._queue_speech(response)
        self._record_metrics(response, tts_event)
        return response

    def _persist_response(self, response: AssistantResponse) -> None:
        logger.info(
            "Assistant response",
            response_id=response.response_id,
            category=response.category.value,
            severity=response.severity.value,
            success=response.success,
            source_skill=response.source_skill or "",
            action_name=response.action_name or "",
        )

    def _render_ui(self, response: AssistantResponse) -> None:
        callback = self.ui_callback
        if callback is None or not response.text:
            return
        try:
            callback("Assistant", response.text)
        except Exception:
            logger.exception("Response UI render failed", response_id=response.response_id)

    def _notify(self, response: AssistantResponse) -> None:
        callback = self.notification_callback
        if callback is None or not response.notification_enabled or not response.text:
            return
        try:
            callback(response.notification_title or "Assistant", response.text)
        except Exception:
            logger.exception("Response notification failed", response_id=response.response_id)

    def _queue_speech(self, response: AssistantResponse) -> TTSSpeechEvent | None:
        if not response.text:
            return None
        if not response.speak_enabled:
            if not response.silent_reason:
                response.silent_reason = "speech_disabled_by_response"
            return None
        callback = self.tts_callback
        if callback is None:
            response.silent_reason = response.silent_reason or "tts_unavailable"
            return None

        event = TTSSpeechEvent(
            response_id=response.response_id,
            text=response.text,
            text_length=len(response.text),
            queued_at=datetime.utcnow(),
            status="queued",
        )
        try:
            queued = self._invoke_tts_callback(callback, response.text, response.response_id, response.metadata)
        except Exception as exc:
            event.status = "failed"
            event.error = str(exc)
            logger.exception("Response TTS queue failed", response_id=response.response_id)
        else:
            if queued:
                event.started_at = datetime.utcnow()
                event.status = "speaking"
            else:
                event.status = "failed"
                event.error = "tts_rejected_request"
                response.silent_reason = response.silent_reason or "tts_rejected_request"

        with self._lock:
            self._tts_telemetry.append(event)
            if len(self._tts_telemetry) > 500:
                self._tts_telemetry = self._tts_telemetry[-500:]
        return event

    @staticmethod
    def _invoke_tts_callback(
        callback: Callable[..., Any],
        text: str,
        response_id: str,
        metadata: dict[str, Any],
    ) -> bool:
        try:
            result = callback(text, response_id=response_id, metadata=metadata)
        except TypeError:
            result = callback(text)
        if isinstance(result, bool):
            return result
        return True

    def _record_metrics(self, response: AssistantResponse, tts_event: TTSSpeechEvent | None) -> None:
        metrics.record_counter(
            "responses_total",
            category=response.category.value,
            severity=response.severity.value,
            success=response.success,
        )
        if tts_event is not None:
            metrics.record_counter("tts_requests_total", status=tts_event.status)
        try:
            analytics.increment_feature(f"response:{response.category.value}")
            if response.source_skill:
                analytics.increment_feature(f"response_source:{response.source_skill}")
        except Exception:
            logger.debug("Response analytics skipped", exc_info=True)


_response_service: Optional[ResponseService] = None


def get_response_service() -> ResponseService:
    global _response_service
    if _response_service is None:
        _response_service = ResponseService()
    return _response_service


def set_response_service(service: ResponseService) -> None:
    global _response_service
    _response_service = service


def initialize_response_service(
    ui_callback: Optional[Callable[[str, str], None]] = None,
    tts_callback: Optional[Callable[..., Any]] = None,
    notification_callback: Optional[Callable[[str, Optional[str]], None]] = None,
) -> ResponseService:
    service = get_response_service()
    service.set_callbacks(
        ui_callback=ui_callback,
        tts_callback=tts_callback,
        notification_callback=notification_callback,
    )
    return service
