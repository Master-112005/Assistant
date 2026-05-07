"""
Production-grade response integration layer.

This module provides the glue between all subsystems to ensure:
- Unified response pipeline (processor → response service)
- Proper confirmation state machine integration
- NLU router integration for media commands
- Context-aware skill routing
- Comprehensive logging and telemetry

All assistant responses MUST flow through this layer.
"""
from __future__ import annotations

from typing import Any, Optional, Callable, Dict
from datetime import datetime

from core.logger import get_logger, new_correlation_id
from core.response_service import (
    ResponseService,
    get_response_service,
    initialize_response_service,
)
from core.response_models import (
    AssistantResponse,
    ResponseCategory,
    ResponseSeverity,
    ConfirmationToken,
)
from core.confirmation_manager import (
    ConfirmationManager,
    get_confirmation_manager,
    RISKY_ACTIONS,
    SAFE_ACTIONS,
)
from core.nlu_router import NLURouter, NLUIntent, IntentType, get_nlu_router
from core.metrics import metrics
from core.analytics import analytics

logger = get_logger(__name__)


class IntegratedResponsePipeline:
    """
    Production-grade integration of response, confirmation, and NLU systems.
    
    This is the master orchestrator that ensures:
    1. All responses flow through response service
    2. Confirmations are properly managed
    3. Media commands are routed via NLU
    4. Every request is logged and metered
    5. No duplicate output paths exist
    """
    
    def __init__(
        self,
        response_service: Optional[ResponseService] = None,
        confirmation_manager: Optional[ConfirmationManager] = None,
        nlu_router: Optional[NLURouter] = None,
    ):
        """Initialize integrated pipeline with dependencies."""
        self.response_service = response_service or get_response_service()
        self.confirmation_manager = confirmation_manager or get_confirmation_manager()
        self.nlu_router = nlu_router or get_nlu_router()
    
    # ────────────────────────────────────────────────────────────────
    # PRIMARY RESPONSE API
    # ────────────────────────────────────────────────────────────────
    
    def handle_command_result(
        self,
        text: str,
        *,
        success: bool = True,
        action_name: Optional[str] = None,
        source_skill: Optional[str] = None,
        category: ResponseCategory | str = ResponseCategory.COMMAND_RESULT,
        severity: ResponseSeverity | str = ResponseSeverity.INFO,
        correlation_id: Optional[str] = None,
        entities: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AssistantResponse:
        """
        Handle a command result and route it through the response pipeline.
        
        This is the PRIMARY way to send command responses.
        """
        correlation_id = correlation_id or new_correlation_id()
        
        with metrics.measure("response_dispatch", action=action_name or "unknown"):
            response = self.response_service.respond(
                text=text,
                category=category,
                success=success,
                severity=severity,
                speak_enabled=True,
                source_skill=source_skill,
                action_name=action_name,
                correlation_id=correlation_id,
                entities=entities,
                metadata=metadata,
            )
        
        logger.info(
            "Command result dispatched",
            response_id=response.response_id,
            action=action_name,
            success=success,
            correlation_id=correlation_id,
        )
        
        return response
    
    def handle_error(
        self,
        error_text: str,
        *,
        error_code: Optional[str] = None,
        error_details: Optional[str] = None,
        action_name: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> AssistantResponse:
        """
        Handle an error and route through response pipeline.
        """
        correlation_id = correlation_id or new_correlation_id()
        
        response = self.response_service.respond_error(
            text=error_text,
            error_code=error_code,
            error_details=error_details,
            correlation_id=correlation_id,
        )
        
        logger.error(
            "Error dispatched",
            response_id=response.response_id,
            error_code=error_code,
            action_name=action_name,
            correlation_id=correlation_id,
        )
        
        # Record analytics
        analytics.record_error(
            error_code or "unknown_error",
            error_text,
            module="response_pipeline",
            context=f"action:{action_name}",
            source="integrated_pipeline",
        )
        
        return response
    
    def handle_clarification(
        self,
        prompt_text: str,
        *,
        correlation_id: Optional[str] = None,
    ) -> AssistantResponse:
        """
        Request clarification from user.
        """
        correlation_id = correlation_id or new_correlation_id()
        
        response = self.response_service.respond(
            text=prompt_text,
            category=ResponseCategory.CLARIFICATION,
            success=True,
            severity=ResponseSeverity.INFO,
            speak_enabled=True,
            correlation_id=correlation_id,
        )
        
        logger.info(
            "Clarification requested",
            response_id=response.response_id,
            correlation_id=correlation_id,
        )
        
        return response
    
    # ────────────────────────────────────────────────────────────────
    # CONFIRMATION STATE MACHINE API
    # ────────────────────────────────────────────────────────────────
    
    def request_confirmation(
        self,
        action_type: str,
        prompt_text: str,
        action_payload: Optional[Dict[str, Any]] = None,
        risk_level: str = "medium",
        correlation_id: Optional[str] = None,
    ) -> ConfirmationToken:
        """
        Request confirmation for a high-risk action.
        
        Returns a token that must be consumed via consume_confirmation().
        """
        correlation_id = correlation_id or new_correlation_id()
        
        # Check if action actually requires confirmation
        if not self.confirmation_manager.requires_confirmation(action_type):
            logger.warning(
                "Confirmation requested for non-risky action",
                action_type=action_type,
                correlation_id=correlation_id,
            )
        
        # Create token
        token = self.confirmation_manager.request_confirmation(
            action_type=action_type,
            prompt_text=prompt_text,
            action_payload=action_payload,
            risk_level=risk_level,
            expires_in_seconds=60,  # Production standard
        )
        
        # Show prompt to user via response service
        self.response_service.respond(
            text=prompt_text,
            category=ResponseCategory.CONFIRMATION,
            success=True,
            speak_enabled=True,
            notification_enabled=False,
            correlation_id=correlation_id,
            metadata={
                "token_id": token.token_id,
                "action_type": action_type,
                "risk_level": risk_level,
            },
        )
        
        logger.info(
            "Confirmation requested via pipeline",
            token_id=token.token_id,
            action_type=action_type,
            correlation_id=correlation_id,
        )
        
        return token
    
    def consume_confirmation(
        self,
        token_id: str,
        correlation_id: Optional[str] = None,
    ) -> Optional[ConfirmationToken]:
        """
        Consume a confirmation token when user says "yes".
        
        Returns the token if valid, or None if missing/expired.
        """
        correlation_id = correlation_id or new_correlation_id()
        
        token = self.confirmation_manager.consume_confirmation(token_id)
        
        if token is None:
            logger.warning(
                "Confirmation consumption failed: no valid token",
                token_id=token_id,
                correlation_id=correlation_id,
            )
            return None
        
        logger.info(
            "Confirmation consumed",
            token_id=token_id,
            action_type=token.action_type,
            correlation_id=correlation_id,
        )
        
        return token
    
    def handle_confirmation_reply(
        self,
        user_input: str,
        correlation_id: Optional[str] = None,
    ) -> Optional[ConfirmationToken]:
        """
        Process user input as potential confirmation (yes/no/cancel).
        
        Returns token if confirmed, None otherwise.
        """
        correlation_id = correlation_id or new_correlation_id()
        
        # Normalize input
        normalized = user_input.lower().strip()
        
        # Check if it's a confirmation response
        if normalized in ("yes", "y", "confirm", "ok", "okay"):
            # Check if any confirmation is pending
            if not self.confirmation_manager.has_pending_confirmation():
                logger.debug(
                    "User said yes but no pending confirmation",
                    correlation_id=correlation_id,
                )
                return None
            
            # Get pending token and consume it
            pending = self.confirmation_manager.get_pending_confirmation()
            if pending:
                return self.consume_confirmation(pending.token_id, correlation_id)
        
        elif normalized in ("no", "n", "cancel", "nope", "negative"):
            # Cancel pending confirmation if any
            pending = self.confirmation_manager.get_pending_confirmation()
            if pending:
                self.confirmation_manager.cancel_confirmation(pending.token_id)
                logger.info(
                    "User cancelled confirmation",
                    token_id=pending.token_id,
                    correlation_id=correlation_id,
                )
            return None
        
        return None
    
    # ────────────────────────────────────────────────────────────────
    # NLU ROUTING API
    # ────────────────────────────────────────────────────────────────
    
    def route_media_command(
        self,
        user_input: str,
        context_app: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> Optional[NLUIntent]:
        """
        Route user input through NLU to detect media commands.
        
        Returns NLUIntent if it's a media command, None otherwise.
        """
        correlation_id = correlation_id or new_correlation_id()
        
        intent = self.nlu_router.route(user_input)
        
        if intent.intent == IntentType.UNKNOWN:
            logger.debug(
                "NLU did not recognize command",
                input=user_input,
                correlation_id=correlation_id,
            )
            return None
        
        logger.info(
            "NLU matched intent",
            intent=intent.intent.value,
            confidence=intent.confidence,
            target=intent.target.value if intent.target else None,
            correlation_id=correlation_id,
        )
        
        # Record metrics
        metrics.record_gauge(
            "nlu_confidence",
            value=intent.confidence,
            intent=intent.intent.value,
        )
        
        return intent
    
    # ────────────────────────────────────────────────────────────────
    # QUERY & STATE APIs
    # ────────────────────────────────────────────────────────────────
    
    def has_pending_confirmation(self) -> bool:
        """Check if any confirmation is waiting for user."""
        return self.confirmation_manager.has_pending_confirmation()
    
    def get_pending_confirmation_state(self) -> Dict[str, Any]:
        """Get full pending confirmation state."""
        return self.confirmation_manager.get_pending_state()
    
    def get_response_history(self, limit: int = 50) -> list[AssistantResponse]:
        """Get recent responses."""
        return self.response_service.get_response_history(limit)
    
    def get_tts_telemetry(self, limit: int = 100) -> list:
        """Get TTS telemetry."""
        return self.response_service.get_tts_telemetry(limit)
    
    def get_confirmation_history(self, limit: int = 50) -> list[ConfirmationToken]:
        """Get confirmation history."""
        return self.confirmation_manager.get_history(limit)
    
    # ────────────────────────────────────────────────────────────────
    # LIFECYCLE
    # ────────────────────────────────────────────────────────────────
    
    def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("IntegratedResponsePipeline shutting down")
        self.response_service.shutdown()
        self.confirmation_manager.shutdown()


# Global singleton
_pipeline: Optional[IntegratedResponsePipeline] = None


def get_response_pipeline() -> IntegratedResponsePipeline:
    """Get global response pipeline."""
    global _pipeline
    if _pipeline is None:
        _pipeline = IntegratedResponsePipeline()
    return _pipeline


def set_response_pipeline(pipeline: IntegratedResponsePipeline) -> None:
    """Set global response pipeline."""
    global _pipeline
    _pipeline = pipeline


def initialize_response_pipeline(
    ui_callback: Optional[Callable[[str, str], None]] = None,
    tts_callback: Optional[Callable[[str], bool]] = None,
    notification_callback: Optional[Callable[[str, Optional[str]], None]] = None,
) -> IntegratedResponsePipeline:
    """Initialize all systems and create integrated pipeline."""
    # Initialize response service
    response_service = initialize_response_service(
        ui_callback=ui_callback,
        tts_callback=tts_callback,
        notification_callback=notification_callback,
    )
    
    # Get confirmation manager
    confirmation_manager = get_confirmation_manager()
    
    # Get NLU router
    nlu_router = get_nlu_router()
    
    # Create and set pipeline
    pipeline = IntegratedResponsePipeline(
        response_service=response_service,
        confirmation_manager=confirmation_manager,
        nlu_router=nlu_router,
    )
    set_response_pipeline(pipeline)
    
    logger.info("Integrated response pipeline initialized")
    
    return pipeline
