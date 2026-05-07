"""
Confirmation state machine for destructive/sensitive actions.

Guarantees:
- No action executes without explicit confirmation for high-risk operations
- Confirmation tokens have strict expiration
- No "yes" accepted without an active token
- State transitions are auditable
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Callable, Any

from core.logger import get_logger
from core.response_models import ConfirmationToken

logger = get_logger(__name__)


# Actions that MUST require confirmation
RISKY_ACTIONS = {
    "delete_file",
    "delete_folder",
    "delete_directory",
    "format_drive",
    "shutdown_system",
    "restart_system",
    "send_message",
    "send_email",
    "bulk_delete",
    "uninstall_app",
}

# Actions that NEVER require confirmation
SAFE_ACTIONS = {
    "media_resume",
    "media_pause",
    "media_next",
    "media_previous",
    "media_stop",
    "open_app",
    "open_file",
    "open_chrome",
    "open_explorer",
    "search_files",
    "copy_text",
    "close_window",
    "minimize_window",
    "maximize_window",
}


class ConfirmationManager:
    """
    Manages confirmation state machine for high-risk operations.

    State transitions:
    NONE -> PENDING (action requested)
    PENDING -> CONFIRMED (user says yes)
    PENDING -> CANCELLED (user says no/cancel)
    PENDING -> EXPIRED (timeout)
    CONFIRMED -> EXECUTING -> COMPLETED
    """

    def __init__(
        self,
        default_expiry_seconds: int = 60,
        max_pending: int = 1,
    ):
        """
        Initialize confirmation manager.

        Args:
            default_expiry_seconds: How long tokens remain valid
            max_pending: Maximum concurrent pending confirmations
        """
        self.default_expiry_seconds = default_expiry_seconds
        self.max_pending = max_pending
        self._pending_tokens: dict[str, ConfirmationToken] = {}
        self._completed_tokens: list[ConfirmationToken] = []
        self._max_history = 100

    # ────────────────────── MAIN API ───────────────────────────────────

    def requires_confirmation(self, action_type: str) -> bool:
        """Check if action requires confirmation."""
        if action_type in SAFE_ACTIONS:
            return False
        if action_type in RISKY_ACTIONS:
            return True
        # Default to safe for unknown actions
        return False

    def request_confirmation(
        self,
        action_type: str,
        prompt_text: str,
        action_payload: Optional[dict[str, Any]] = None,
        risk_level: str = "medium",
        expires_in_seconds: Optional[int] = None,
    ) -> ConfirmationToken:
        """
        Create a confirmation token for an action.

        Args:
            action_type: Type of action (e.g., "delete_file")
            prompt_text: Text to show user
            action_payload: Data needed to execute action
            risk_level: "low", "medium", "high"
            expires_in_seconds: When token expires (default from __init__)

        Returns:
            ConfirmationToken ready for user response
        """
        # Enforce max pending
        expired = self._cleanup_expired()
        if len(self._pending_tokens) >= self.max_pending:
            logger.warning(
                "Max pending confirmations reached, clearing oldest",
                count=len(self._pending_tokens),
            )
            # Remove oldest
            oldest_id = next(iter(self._pending_tokens))
            del self._pending_tokens[oldest_id]

        # Create token
        token = ConfirmationToken(
            action_type=action_type,
            action_payload=action_payload or {},
            prompt_text=prompt_text,
            source_command=prompt_text,
            risk_level=risk_level,
            state="pending",
            expires_at=datetime.utcnow() + timedelta(
                seconds=expires_in_seconds or self.default_expiry_seconds
            ),
        )

        self._pending_tokens[token.token_id] = token

        logger.info(
            "Confirmation requested",
            token_id=token.token_id,
            action_type=action_type,
            risk_level=risk_level,
            expires_at=token.expires_at,
        )

        return token

    def consume_confirmation(self, token_id: str) -> Optional[ConfirmationToken]:
        """
        Consume a confirmation token when user confirms.

        Args:
            token_id: The token ID to confirm

        Returns:
            The confirmed token, or None if invalid/expired/missing
        """
        token = self._pending_tokens.get(token_id)

        if token is None:
            logger.warning(
                "Confirmation consumed but token missing",
                token_id=token_id,
            )
            return None

        if token.is_expired():
            token.state = "expired"
            del self._pending_tokens[token_id]
            logger.warning(
                "Confirmation expired",
                token_id=token_id,
                action_type=token.action_type,
            )
            return None

        # Mark as confirmed
        token.state = "confirmed"
        token.confirmed_at = datetime.utcnow()
        del self._pending_tokens[token_id]
        self._add_to_history(token)

        logger.info(
            "Confirmation consumed",
            token_id=token_id,
            action_type=token.action_type,
        )

        return token

    def cancel_confirmation(self, token_id: str) -> Optional[ConfirmationToken]:
        """
        Cancel a pending confirmation.

        Args:
            token_id: The token to cancel

        Returns:
            The cancelled token, or None if missing
        """
        token = self._pending_tokens.get(token_id)

        if token is None:
            return None

        token.state = "cancelled"
        del self._pending_tokens[token_id]
        self._add_to_history(token)

        logger.info(
            "Confirmation cancelled",
            token_id=token_id,
            action_type=token.action_type,
        )

        return token

    # ────────────────────── STATE QUERIES ──────────────────────────────

    def has_pending_confirmation(self) -> bool:
        """Check if any confirmation is pending."""
        return len(self._pending_tokens) > 0

    def get_pending_confirmation(self) -> Optional[ConfirmationToken]:
        """Get the active pending confirmation (if any)."""
        self._cleanup_expired()
        if not self._pending_tokens:
            return None
        # Return most recent
        return list(self._pending_tokens.values())[0]

    def get_pending_state(self) -> dict[str, Any]:
        """Get full pending confirmation state."""
        self._cleanup_expired()
        pending = [
            {
                "token_id": t.token_id,
                "action_type": t.action_type,
                "prompt_text": t.prompt_text,
                "risk_level": t.risk_level,
                "expires_at": t.expires_at.isoformat() if t.expires_at else None,
                "time_left_seconds": max(
                    0,
                    int((t.expires_at - datetime.utcnow()).total_seconds())
                    if t.expires_at
                    else 0,
                ),
            }
            for t in self._pending_tokens.values()
        ]
        return {
            "has_pending": len(pending) > 0,
            "count": len(pending),
            "confirmations": pending,
        }

    # ────────────────────── UTILITIES ──────────────────────────────────

    def _cleanup_expired(self) -> int:
        """Remove expired tokens. Returns count removed."""
        expired_ids = [
            tid for tid, token in self._pending_tokens.items()
            if token.is_expired()
        ]
        for tid in expired_ids:
            token = self._pending_tokens.pop(tid)
            token.state = "expired"
            self._add_to_history(token)

        if expired_ids:
            logger.debug("Cleaned up expired confirmations", count=len(expired_ids))

        return len(expired_ids)

    def _add_to_history(self, token: ConfirmationToken) -> None:
        """Add token to completed history."""
        self._completed_tokens.append(token)
        # Keep history bounded
        if len(self._completed_tokens) > self._max_history:
            self._completed_tokens = self._completed_tokens[-self._max_history :]

    def get_history(self, limit: int = 50) -> list[ConfirmationToken]:
        """Get recent confirmation history."""
        return self._completed_tokens[-limit:]

    def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info(
            "ConfirmationManager shutting down",
            pending_count=len(self._pending_tokens),
            history_count=len(self._completed_tokens),
        )
        self._pending_tokens.clear()


# Global singleton
_confirmation_manager: Optional[ConfirmationManager] = None


def get_confirmation_manager() -> ConfirmationManager:
    """Get global confirmation manager."""
    global _confirmation_manager
    if _confirmation_manager is None:
        _confirmation_manager = ConfirmationManager()
    return _confirmation_manager


def set_confirmation_manager(manager: ConfirmationManager) -> None:
    """Set global confirmation manager."""
    global _confirmation_manager
    _confirmation_manager = manager
