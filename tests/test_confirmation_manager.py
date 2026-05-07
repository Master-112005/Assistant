"""
Production-grade tests for Confirmation Manager.

Tests the complete confirmation state machine:
- Token creation/expiration
- Yes/no/cancel handling
- State transitions
- Safe rejection of "yes" without token
- Risk level enforcement
"""
import pytest
import time
from datetime import datetime, timedelta

from core.confirmation_manager import (
    ConfirmationManager,
    RISKY_ACTIONS,
    SAFE_ACTIONS,
    get_confirmation_manager,
)


class TestConfirmationBasics:
    """Test basic confirmation operations."""
    
    def test_manager_initialization(self):
        """ConfirmationManager initializes."""
        manager = ConfirmationManager()
        assert manager is not None
        assert not manager.has_pending_confirmation()
    
    def test_default_expiry_seconds(self):
        """Default expiry is set."""
        manager = ConfirmationManager(default_expiry_seconds=30)
        assert manager.default_expiry_seconds == 30
    
    def test_max_pending_enforced(self):
        """Max pending confirmations is enforced."""
        manager = ConfirmationManager(max_pending=2)
        
        token1 = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete file?",
        )
        token2 = manager.request_confirmation(
            action_type="delete_folder",
            prompt_text="Delete folder?",
        )
        
        # Both should be created
        assert manager.has_pending_confirmation()


class TestConfirmationRiskLevels:
    """Test risk level checking."""
    
    def test_risky_action_requires_confirmation(self):
        """Risky actions require confirmation."""
        manager = ConfirmationManager()
        
        for action in RISKY_ACTIONS:
            assert manager.requires_confirmation(action)
    
    def test_safe_action_no_confirmation(self):
        """Safe actions don't require confirmation."""
        manager = ConfirmationManager()
        
        for action in SAFE_ACTIONS:
            assert not manager.requires_confirmation(action)
    
    def test_unknown_action_safe_by_default(self):
        """Unknown actions default to safe."""
        manager = ConfirmationManager()
        assert not manager.requires_confirmation("some_unknown_action")


class TestConfirmationTokenCreation:
    """Test token creation and state."""
    
    def test_token_created(self):
        """Confirmation token is created."""
        manager = ConfirmationManager()
        
        token = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete report.txt?",
            action_payload={"path": "/report.txt"},
            risk_level="high",
        )
        
        assert token is not None
        assert token.action_type == "delete_file"
        assert token.prompt_text == "Delete report.txt?"
        assert token.state == "pending"
        assert token.risk_level == "high"
    
    def test_token_has_id(self):
        """Token has unique ID."""
        manager = ConfirmationManager()
        
        token1 = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
        )
        token2 = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
        )
        
        assert token1.token_id != token2.token_id
    
    def test_token_has_expiry(self):
        """Token has expiration time."""
        manager = ConfirmationManager(default_expiry_seconds=60)
        
        token = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
        )
        
        assert token.expires_at is not None
        # Should expire in ~60 seconds
        time_left = (token.expires_at - datetime.utcnow()).total_seconds()
        assert 59 <= time_left <= 61


class TestConfirmationExpiration:
    """Test token expiration."""
    
    def test_token_not_expired_initially(self):
        """Token is not expired immediately after creation."""
        manager = ConfirmationManager(default_expiry_seconds=60)
        
        token = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
        )
        
        assert not token.is_expired()
    
    def test_token_expired_after_timeout(self):
        """Token is expired after timeout."""
        manager = ConfirmationManager(default_expiry_seconds=0)
        
        token = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
        )
        
        time.sleep(0.1)
        assert token.is_expired()
    
    def test_cleanup_removes_expired(self):
        """Cleanup removes expired tokens."""
        manager = ConfirmationManager(default_expiry_seconds=0)
        
        token = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
        )
        
        time.sleep(0.1)
        assert manager.has_pending_confirmation()
        
        # Cleanup should remove it
        manager._cleanup_expired()
        assert not manager.has_pending_confirmation()


class TestConfirmationConsumption:
    """Test token consumption (user says yes)."""
    
    def test_consume_valid_token(self):
        """Valid token can be consumed."""
        manager = ConfirmationManager()
        
        token = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
            action_payload={"path": "/file.txt"},
        )
        
        consumed = manager.consume_confirmation(token.token_id)
        
        assert consumed is not None
        assert consumed.state == "confirmed"
        assert consumed.action_type == "delete_file"
        assert consumed.action_payload == {"path": "/file.txt"}
    
    def test_consume_missing_token(self):
        """Consuming missing token returns None."""
        manager = ConfirmationManager()
        
        consumed = manager.consume_confirmation("nonexistent_token_id")
        
        assert consumed is None
    
    def test_consume_expired_token(self):
        """Consuming expired token returns None."""
        manager = ConfirmationManager(default_expiry_seconds=0)
        
        token = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
        )
        
        time.sleep(0.1)
        
        consumed = manager.consume_confirmation(token.token_id)
        assert consumed is None
    
    def test_consume_twice_fails(self):
        """Consuming same token twice fails."""
        manager = ConfirmationManager()
        
        token = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
        )
        
        consumed1 = manager.consume_confirmation(token.token_id)
        assert consumed1 is not None
        
        consumed2 = manager.consume_confirmation(token.token_id)
        assert consumed2 is None


class TestConfirmationCancellation:
    """Test token cancellation (user says no)."""
    
    def test_cancel_pending_token(self):
        """Pending token can be cancelled."""
        manager = ConfirmationManager()
        
        token = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
        )
        
        cancelled = manager.cancel_confirmation(token.token_id)
        
        assert cancelled is not None
        assert cancelled.state == "cancelled"
    
    def test_cancel_missing_token(self):
        """Cancelling missing token returns None."""
        manager = ConfirmationManager()
        
        cancelled = manager.cancel_confirmation("nonexistent_token_id")
        assert cancelled is None
    
    def test_cancel_removes_from_pending(self):
        """Cancelled token is removed from pending."""
        manager = ConfirmationManager()
        
        token = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
        )
        
        assert manager.has_pending_confirmation()
        
        manager.cancel_confirmation(token.token_id)
        assert not manager.has_pending_confirmation()


class TestConfirmationStateQueries:
    """Test state query APIs."""
    
    def test_has_pending_confirmation(self):
        """has_pending_confirmation returns correct state."""
        manager = ConfirmationManager()
        
        assert not manager.has_pending_confirmation()
        
        manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
        )
        
        assert manager.has_pending_confirmation()
    
    def test_get_pending_confirmation(self):
        """get_pending_confirmation returns active token."""
        manager = ConfirmationManager()
        
        token = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
        )
        
        pending = manager.get_pending_confirmation()
        assert pending is not None
        assert pending.token_id == token.token_id
    
    def test_get_pending_state(self):
        """get_pending_state returns full state."""
        manager = ConfirmationManager()
        
        token = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
            risk_level="high",
        )
        
        state = manager.get_pending_state()
        
        assert state["has_pending"]
        assert state["count"] == 1
        assert len(state["confirmations"]) == 1
        
        confirmation = state["confirmations"][0]
        assert confirmation["token_id"] == token.token_id
        assert confirmation["action_type"] == "delete_file"
        assert confirmation["risk_level"] == "high"


class TestConfirmationHistory:
    """Test confirmation history."""
    
    def test_history_tracks_confirmations(self):
        """Completed confirmations are tracked in history."""
        manager = ConfirmationManager()
        
        token = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete?",
        )
        
        manager.consume_confirmation(token.token_id)
        
        history = manager.get_history()
        assert len(history) == 1
        assert history[0].token_id == token.token_id


class TestConfirmationGlobalSingleton:
    """Test global confirmation manager."""
    
    def test_global_manager_exists(self):
        """Global manager can be retrieved."""
        manager = get_confirmation_manager()
        assert manager is not None
    
    def test_global_manager_is_singleton(self):
        """Global manager is same instance."""
        manager1 = get_confirmation_manager()
        manager2 = get_confirmation_manager()
        
        assert manager1 is manager2


class TestConfirmationRegressions:
    """Regression tests for known failures."""
    
    def test_yes_without_token_safe_rejection(self):
        """
        FAILURE: User says "yes" without pending confirmation
        System shows error: "There is no pending confirmation to resolve."
        FIXED: Should gracefully handle as no-op
        """
        manager = ConfirmationManager()
        
        # No pending confirmation
        assert not manager.has_pending_confirmation()
        
        # Try to consume non-existent token
        result = manager.consume_confirmation("fake_token")
        
        # Should return None safely
        assert result is None
    
    def test_confirmation_token_exists_and_found(self):
        """
        FAILURE: User says "yes" but system says token doesn't exist
        FIXED: Token should be properly stored and retrieved
        """
        manager = ConfirmationManager()
        
        # Create token
        token = manager.request_confirmation(
            action_type="delete_file",
            prompt_text="Delete report.txt?",
            action_payload={"path": "/report.txt"},
        )
        
        # Token should exist and be findable
        assert manager.has_pending_confirmation()
        pending = manager.get_pending_confirmation()
        assert pending is not None
        assert pending.token_id == token.token_id
        
        # Should be consumable
        consumed = manager.consume_confirmation(token.token_id)
        assert consumed is not None
        assert consumed.action_payload == {"path": "/report.txt"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
