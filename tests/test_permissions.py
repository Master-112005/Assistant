"""
Tests for PHASE 29 - Permission Levels framework.

Covers:
  - Enums & coercion
  - PermissionResult / PendingConfirmation / TemporaryGrant dataclasses
  - PermissionManager with isolated instances (not singleton)
  - Risk classification (safe / medium / dangerous)
  - Policy evaluation at each permission level
  - Confirmation flow (request / approve / deny / expire)
  - Voice/text confirmation handling
  - Temporary grants (create / match / expire / revoke)
  - Audit logging
  - Edge cases & error handling
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from core.permissions import (
    Decision,
    PendingConfirmation,
    PermissionLevel,
    PermissionManager,
    PermissionResult,
    RiskLevel,
    TemporaryGrant,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager(*, level: str = "BASIC", time_fn=None):
    """Create an isolated PermissionManager for testing."""
    with patch("core.permissions.settings") as mock_settings, \
         patch("core.permissions.state") as mock_state:
        defaults = {
            "permission_level": level,
            "allow_temporary_approvals": True,
            "confirmation_timeout_seconds": 60,
            "audit_log_enabled": False,
            "confirm_shutdown": True,
            "confirm_restart": True,
            "confirm_lock": False,
            "confirm_delete": True,
            "confirm_overwrite": True,
            "confirm_before_sending_message": False,
            "file_bulk_delete_confirmation_threshold": 25,
        }
        mock_settings.get = lambda key, default=None: defaults.get(key, default)
        mock_settings.set = MagicMock()
        for attr in (
            "permission_level", "pending_confirmations", "pending_confirmation",
            "temporary_grants", "last_permission_decision", "last_denied_action",
        ):
            setattr(mock_state, attr, {} if "dict" in attr or attr.endswith("action") or attr.endswith("decision") or attr.endswith("confirmation") else [])
        mock_state.permission_level = level
        mock_state.pending_confirmations = {}
        mock_state.pending_confirmation = {}
        mock_state.temporary_grants = []
        mock_state.last_permission_decision = {}
        mock_state.last_denied_action = {}

        mgr = PermissionManager(time_fn=time_fn)
    # Patch settings/state for runtime calls too
    mgr._settings_mock = mock_settings
    mgr._state_mock = mock_state
    return mgr


def _patched(mgr):
    """Context manager that patches settings and state for the manager."""
    return patch.multiple(
        "core.permissions",
        settings=mgr._settings_mock,
        state=mgr._state_mock,
    )


# ===========================================================================
# Enum Tests
# ===========================================================================

class TestPermissionLevel(unittest.TestCase):
    def test_values(self):
        self.assertEqual(PermissionLevel.BASIC.value, "BASIC")
        self.assertEqual(PermissionLevel.TRUSTED.value, "TRUSTED")
        self.assertEqual(PermissionLevel.ADMIN_CONFIRM.value, "ADMIN_CONFIRM")

    def test_coerce_case_insensitive(self):
        self.assertEqual(PermissionLevel.coerce("basic"), PermissionLevel.BASIC)
        self.assertEqual(PermissionLevel.coerce("TRUSTED"), PermissionLevel.TRUSTED)
        self.assertEqual(PermissionLevel.coerce("admin_confirm"), PermissionLevel.ADMIN_CONFIRM)

    def test_coerce_alias_admin(self):
        self.assertEqual(PermissionLevel.coerce("ADMIN"), PermissionLevel.ADMIN_CONFIRM)
        self.assertEqual(PermissionLevel.coerce("admin"), PermissionLevel.ADMIN_CONFIRM)

    def test_coerce_invalid_raises(self):
        with self.assertRaises(ValueError):
            PermissionLevel.coerce("invalid")
        with self.assertRaises(ValueError):
            PermissionLevel.coerce("")


class TestRiskLevel(unittest.TestCase):
    def test_values(self):
        self.assertEqual(RiskLevel.SAFE.value, "SAFE")
        self.assertEqual(RiskLevel.MEDIUM.value, "MEDIUM")
        self.assertEqual(RiskLevel.DANGEROUS.value, "DANGEROUS")


class TestDecision(unittest.TestCase):
    def test_values(self):
        self.assertEqual(Decision.ALLOW.value, "ALLOW")
        self.assertEqual(Decision.REQUIRE_CONFIRMATION.value, "REQUIRE_CONFIRMATION")
        self.assertEqual(Decision.DENY.value, "DENY")


# ===========================================================================
# Dataclass Tests
# ===========================================================================

class TestPermissionResult(unittest.TestCase):
    def test_creation_and_to_dict(self):
        r = PermissionResult(
            allowed=True, decision=Decision.ALLOW,
            permission_level=PermissionLevel.BASIC, risk_level=RiskLevel.SAFE,
            reason="safe", requires_confirmation=False,
        )
        self.assertTrue(r.allowed)
        d = r.to_dict()
        self.assertEqual(d["decision"], "ALLOW")
        self.assertEqual(d["risk_level"], "SAFE")
        self.assertIsNone(d["expires_at"])


class TestPendingConfirmation(unittest.TestCase):
    def test_creation(self):
        p = PendingConfirmation(
            token="abc", action="shutdown", normalized_action="shutdown",
            permission_level=PermissionLevel.ADMIN_CONFIRM,
            risk_level=RiskLevel.DANGEROUS,
            reason="test", prompt="Confirm?",
        )
        self.assertEqual(p.status, "pending")

    def test_to_public_dict(self):
        p = PendingConfirmation(
            token="abc", action="shutdown", normalized_action="shutdown",
            permission_level=PermissionLevel.ADMIN_CONFIRM,
            risk_level=RiskLevel.DANGEROUS,
            reason="test", prompt="Confirm?",
        )
        d = p.to_public_dict()
        self.assertIn("token", d)
        self.assertIn("allow_temporary_approval", d)
        self.assertFalse(d["allow_temporary_approval"])  # DANGEROUS blocks temp


class TestTemporaryGrant(unittest.TestCase):
    def test_active_no_expiry(self):
        g = TemporaryGrant(grant_id="g1", action="a", normalized_action="a")
        self.assertTrue(g.active(time.time()))

    def test_active_future_expiry(self):
        g = TemporaryGrant(grant_id="g1", action="a", normalized_action="a",
                           expires_at=time.time() + 60)
        self.assertTrue(g.active(time.time()))

    def test_inactive_past_expiry(self):
        g = TemporaryGrant(grant_id="g1", action="a", normalized_action="a",
                           expires_at=time.time() - 10)
        self.assertFalse(g.active(time.time()))


# ===========================================================================
# Classification Tests
# ===========================================================================

class TestClassifyAction(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def test_safe_actions(self):
        safe = ["open_app", "search", "read_screen", "new_tab", "pause_music",
                "play_music", "clipboard_history", "help", "time_query", "ocr"]
        with _patched(self.mgr):
            for action in safe:
                risk = self.mgr.classify_action(action, {})
                self.assertEqual(risk, RiskLevel.SAFE, f"{action} should be SAFE")

    def test_medium_actions(self):
        with _patched(self.mgr):
            self.assertEqual(self.mgr.classify_action("close_app", {}), RiskLevel.MEDIUM)
            self.assertEqual(self.mgr.classify_action("send_message", {}), RiskLevel.MEDIUM)

    def test_unknown_action_dangerous(self):
        with _patched(self.mgr):
            risk = self.mgr.classify_action("totally_unknown", {})
            self.assertEqual(risk, RiskLevel.DANGEROUS)

    def test_none_action_dangerous(self):
        with _patched(self.mgr):
            risk = self.mgr.classify_action(None, {})
            self.assertEqual(risk, RiskLevel.DANGEROUS)


class TestFileClassification(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def test_open_file_safe(self):
        with _patched(self.mgr):
            r = self.mgr.classify_action("file_action", {"action": "open"})
            self.assertEqual(r, RiskLevel.SAFE)

    def test_create_file_safe(self):
        with _patched(self.mgr):
            r = self.mgr.classify_action("file_action", {"action": "create"})
            self.assertEqual(r, RiskLevel.SAFE)

    def test_move_file_medium(self):
        with _patched(self.mgr):
            r = self.mgr.classify_action("file_action", {"action": "move"})
            self.assertEqual(r, RiskLevel.MEDIUM)

    def test_rename_file_medium(self):
        with _patched(self.mgr):
            r = self.mgr.classify_action("file_action", {"action": "rename"})
            self.assertEqual(r, RiskLevel.MEDIUM)

    def test_delete_recycle_medium(self):
        with _patched(self.mgr):
            r = self.mgr.classify_action("file_action", {"action": "delete"})
            self.assertEqual(r, RiskLevel.MEDIUM)

    def test_delete_permanent_dangerous(self):
        with _patched(self.mgr):
            r = self.mgr.classify_action("file_action", {"action": "delete", "permanent": True})
            self.assertEqual(r, RiskLevel.DANGEROUS)

    def test_overwrite_medium(self):
        with _patched(self.mgr):
            r = self.mgr.classify_action("file_action", {"action": "create", "overwrite": True})
            self.assertEqual(r, RiskLevel.MEDIUM)


class TestSystemClassification(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def test_volume_safe(self):
        with _patched(self.mgr):
            self.assertEqual(
                self.mgr.classify_action("system_control", {"control": "volume_up"}),
                RiskLevel.SAFE,
            )

    def test_brightness_safe(self):
        with _patched(self.mgr):
            self.assertEqual(
                self.mgr.classify_action("system_control", {"control": "set_brightness"}),
                RiskLevel.SAFE,
            )

    def test_wifi_off_medium(self):
        with _patched(self.mgr):
            self.assertEqual(
                self.mgr.classify_action("system_control", {"control": "wifi_off"}),
                RiskLevel.MEDIUM,
            )

    def test_shutdown_dangerous(self):
        with _patched(self.mgr):
            self.assertEqual(
                self.mgr.classify_action("system_control", {"control": "shutdown"}),
                RiskLevel.DANGEROUS,
            )

    def test_restart_dangerous(self):
        with _patched(self.mgr):
            self.assertEqual(
                self.mgr.classify_action("system_control", {"control": "restart"}),
                RiskLevel.DANGEROUS,
            )

    def test_sleep_dangerous(self):
        with _patched(self.mgr):
            self.assertEqual(
                self.mgr.classify_action("system_control", {"control": "sleep"}),
                RiskLevel.DANGEROUS,
            )


class TestSkillClassification(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def test_browser_skill_safe(self):
        with _patched(self.mgr):
            self.assertEqual(
                self.mgr.classify_action("BrowserSkill", {}),
                RiskLevel.SAFE,
            )

    def test_youtube_skill_safe(self):
        with _patched(self.mgr):
            self.assertEqual(
                self.mgr.classify_action("YouTubeSkill", {}),
                RiskLevel.SAFE,
            )

    def test_whatsapp_send_medium(self):
        with _patched(self.mgr):
            self.assertEqual(
                self.mgr.classify_action("WhatsAppSkill", {"command": "send message", "intent": "send_message"}),
                RiskLevel.MEDIUM,
            )

    def test_click_text_medium(self):
        with _patched(self.mgr):
            self.assertEqual(
                self.mgr.classify_action("ClickTextSkill", {}),
                RiskLevel.MEDIUM,
            )


# ===========================================================================
# Policy Evaluation Tests
# ===========================================================================

class TestEvaluateBasicLevel(unittest.TestCase):
    """At BASIC level: SAFE=allow, MEDIUM=confirm, DANGEROUS=confirm."""

    def setUp(self):
        self.mgr = _make_manager(level="BASIC")

    def test_safe_allowed(self):
        with _patched(self.mgr):
            r = self.mgr.evaluate("open_app", {})
            self.assertTrue(r.allowed)
            self.assertEqual(r.decision, Decision.ALLOW)

    def test_medium_requires_confirmation(self):
        with _patched(self.mgr):
            r = self.mgr.evaluate("close_app", {})
            self.assertFalse(r.allowed)
            self.assertEqual(r.decision, Decision.REQUIRE_CONFIRMATION)

    def test_dangerous_requires_confirmation(self):
        with _patched(self.mgr):
            r = self.mgr.evaluate("system_control", {"control": "shutdown"})
            self.assertFalse(r.allowed)
            self.assertEqual(r.decision, Decision.REQUIRE_CONFIRMATION)
            self.assertTrue(r.requires_confirmation)

    def test_unknown_denied(self):
        with _patched(self.mgr):
            r = self.mgr.evaluate("unknown_action_xyz", {})
            self.assertFalse(r.allowed)
            self.assertEqual(r.decision, Decision.DENY)


class TestEvaluateTrustedLevel(unittest.TestCase):
    """At TRUSTED level: SAFE=allow, MEDIUM=allow, DANGEROUS=confirm."""

    def setUp(self):
        self.mgr = _make_manager(level="TRUSTED")

    def test_safe_allowed(self):
        with _patched(self.mgr):
            r = self.mgr.evaluate("open_app", {})
            self.assertTrue(r.allowed)

    def test_medium_allowed(self):
        with _patched(self.mgr):
            r = self.mgr.evaluate("close_app", {})
            self.assertTrue(r.allowed)
            self.assertEqual(r.decision, Decision.ALLOW)

    def test_medium_file_allowed(self):
        with _patched(self.mgr):
            r = self.mgr.evaluate("file_action", {"action": "move"})
            self.assertTrue(r.allowed)

    def test_dangerous_requires_confirmation(self):
        with _patched(self.mgr):
            r = self.mgr.evaluate("system_control", {"control": "shutdown"})
            self.assertFalse(r.allowed)
            self.assertEqual(r.decision, Decision.REQUIRE_CONFIRMATION)


class TestEvaluateAdminLevel(unittest.TestCase):
    """At ADMIN_CONFIRM: SAFE=allow, MEDIUM=allow, DANGEROUS=confirm."""

    def setUp(self):
        self.mgr = _make_manager(level="ADMIN_CONFIRM")

    def test_safe_allowed(self):
        with _patched(self.mgr):
            r = self.mgr.evaluate("search", {})
            self.assertTrue(r.allowed)

    def test_medium_allowed(self):
        with _patched(self.mgr):
            r = self.mgr.evaluate("system_control", {"control": "wifi_off"})
            self.assertTrue(r.allowed)

    def test_dangerous_requires_confirmation(self):
        with _patched(self.mgr):
            r = self.mgr.evaluate("system_control", {"control": "restart"})
            self.assertFalse(r.allowed)
            self.assertEqual(r.decision, Decision.REQUIRE_CONFIRMATION)


# ===========================================================================
# Confirmation Flow Tests
# ===========================================================================

class TestConfirmationFlow(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def test_request_creates_token(self):
        with _patched(self.mgr):
            token = self.mgr.request_confirmation("system_control", {"control": "shutdown"})
            self.assertIsInstance(token, str)
            self.assertTrue(len(token) > 0)

    def test_approve_returns_pending(self):
        with _patched(self.mgr):
            token = self.mgr.request_confirmation("system_control", {"control": "shutdown"})
            pending = self.mgr.approve(token)
            self.assertIsNotNone(pending)
            self.assertEqual(pending.status, "approved")

    def test_deny_returns_pending(self):
        with _patched(self.mgr):
            token = self.mgr.request_confirmation("system_control", {"control": "shutdown"})
            pending = self.mgr.deny(token)
            self.assertIsNotNone(pending)
            self.assertEqual(pending.status, "denied")

    def test_approve_invalid_token_returns_none(self):
        with _patched(self.mgr):
            self.assertIsNone(self.mgr.approve("nonexistent"))

    def test_deny_invalid_token_returns_none(self):
        with _patched(self.mgr):
            self.assertIsNone(self.mgr.deny("nonexistent"))

    def test_double_approve_returns_none(self):
        with _patched(self.mgr):
            token = self.mgr.request_confirmation("system_control", {"control": "shutdown"})
            self.mgr.approve(token)
            self.assertIsNone(self.mgr.approve(token))

    def test_has_pending(self):
        with _patched(self.mgr):
            self.assertFalse(self.mgr.has_pending_confirmation())
            token = self.mgr.request_confirmation("system_control", {"control": "shutdown"})
            self.assertTrue(self.mgr.has_pending_confirmation())
            self.mgr.approve(token)
            self.assertFalse(self.mgr.has_pending_confirmation())


class TestConfirmationExpiry(unittest.TestCase):
    def test_expired_token_cannot_be_approved(self):
        fake_time = [1000.0]
        mgr = _make_manager(time_fn=lambda: fake_time[0])
        with _patched(mgr):
            token = mgr.request_confirmation("system_control", {"control": "shutdown"})
            # Advance time past timeout (60s default)
            fake_time[0] = 1100.0
            pending = mgr.approve(token)
            self.assertIsNone(pending)

    def test_expired_token_cannot_be_denied(self):
        fake_time = [1000.0]
        mgr = _make_manager(time_fn=lambda: fake_time[0])
        with _patched(mgr):
            token = mgr.request_confirmation("system_control", {"control": "shutdown"})
            fake_time[0] = 1100.0
            pending = mgr.deny(token)
            self.assertIsNone(pending)


# ===========================================================================
# Voice / Text Confirmation Tests
# ===========================================================================

class TestVoiceConfirmation(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def test_yes_approves(self):
        with _patched(self.mgr):
            self.mgr.request_confirmation("system_control", {"control": "shutdown"})
            result = self.mgr.handle_confirmation_reply("yes")
            self.assertIsNotNone(result)
            self.assertTrue(result.get("success"))

    def test_confirm_approves(self):
        with _patched(self.mgr):
            self.mgr.request_confirmation("system_control", {"control": "shutdown"})
            result = self.mgr.handle_confirmation_reply("confirm")
            self.assertIsNotNone(result)
            self.assertTrue(result.get("success"))

    def test_no_denies(self):
        with _patched(self.mgr):
            self.mgr.request_confirmation("system_control", {"control": "shutdown"})
            result = self.mgr.handle_confirmation_reply("no")
            self.assertIsNotNone(result)
            self.assertFalse(result.get("success"))

    def test_cancel_denies(self):
        with _patched(self.mgr):
            self.mgr.request_confirmation("system_control", {"control": "shutdown"})
            result = self.mgr.handle_confirmation_reply("cancel")
            self.assertIsNotNone(result)
            self.assertFalse(result.get("success"))

    def test_no_pending_returns_error(self):
        with _patched(self.mgr):
            result = self.mgr.handle_confirmation_reply("yes")
            self.assertIsNotNone(result)
            self.assertTrue(result.get("success"))
            self.assertEqual(result.get("error"), "no_pending_confirmation")
            self.assertIn("don't have any pending action", result.get("response", "").lower())

    def test_random_text_returns_none(self):
        with _patched(self.mgr):
            self.mgr.request_confirmation("system_control", {"control": "shutdown"})
            result = self.mgr.handle_confirmation_reply("maybe later")
            self.assertIsNone(result)

    def test_callback_executed_on_approve(self):
        callback = MagicMock(return_value={"success": True, "response": "done"})
        with _patched(self.mgr):
            self.mgr.request_confirmation(
                "system_control",
                {"control": "shutdown"},
                callback=callback,
            )
            result = self.mgr.handle_confirmation_reply("yes")
            callback.assert_called_once()
            self.assertTrue(result.get("success"))


# ===========================================================================
# Temporary Grant Tests
# ===========================================================================

class TestTemporaryGrants(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager(level="BASIC")

    def test_grant_medium_action(self):
        with _patched(self.mgr):
            grant = self.mgr.grant_temporary("close_app", 600)
            self.assertIsNotNone(grant)
            self.assertEqual(grant.normalized_action, "close_app")

    def test_grant_dangerous_rejected(self):
        with _patched(self.mgr):
            grant = self.mgr.grant_temporary("system_control", 600)
            # system_control alone maps to unknown → dangerous
            self.assertIsNone(grant)

    def test_grant_bypasses_confirmation(self):
        with _patched(self.mgr):
            # Without grant, close_app at BASIC requires confirmation
            r1 = self.mgr.evaluate("close_app", {})
            self.assertEqual(r1.decision, Decision.REQUIRE_CONFIRMATION)

            # Grant temporary approval
            self.mgr.grant_temporary("close_app", 600)

            # Now it should be allowed
            r2 = self.mgr.evaluate("close_app", {})
            self.assertTrue(r2.allowed)
            self.assertEqual(r2.decision, Decision.ALLOW)

    def test_grant_expires(self):
        fake_time = [1000.0]
        mgr = _make_manager(level="BASIC", time_fn=lambda: fake_time[0])
        with _patched(mgr):
            mgr.grant_temporary("close_app", 60)

            r1 = mgr.evaluate("close_app", {})
            self.assertTrue(r1.allowed)

            fake_time[0] = 1100.0  # Past expiry
            r2 = mgr.evaluate("close_app", {})
            self.assertFalse(r2.allowed)

    def test_revoke_by_action(self):
        with _patched(self.mgr):
            self.mgr.grant_temporary("close_app", 600)
            removed = self.mgr.revoke_temporary(action="close_app")
            self.assertEqual(removed, 1)

            r = self.mgr.evaluate("close_app", {})
            self.assertFalse(r.allowed)

    def test_revoke_nonexistent(self):
        with _patched(self.mgr):
            removed = self.mgr.revoke_temporary(action="nonexistent")
            self.assertEqual(removed, 0)

    def test_session_grant_no_expiry(self):
        with _patched(self.mgr):
            grant = self.mgr.grant_temporary("close_app", None)
            self.assertIsNotNone(grant)
            self.assertIsNone(grant.expires_at)
            # Should remain active indefinitely
            self.assertTrue(grant.active(time.time() + 999999))


# ===========================================================================
# Audit Logging Tests
# ===========================================================================

class TestAuditLogging(unittest.TestCase):
    def test_audit_called_on_evaluate(self):
        mgr = _make_manager()
        mgr._settings_mock.get = lambda key, default=None: {
            "permission_level": "BASIC",
            "audit_log_enabled": True,
            "allow_temporary_approvals": True,
            "confirmation_timeout_seconds": 60,
            "confirm_shutdown": True,
            "confirm_restart": True,
            "confirm_lock": False,
            "confirm_delete": True,
            "confirm_overwrite": True,
            "confirm_before_sending_message": False,
            "file_bulk_delete_confirmation_threshold": 25,
        }.get(key, default)

        with _patched(mgr), \
             patch("core.permissions.audit_logger") as mock_audit:
            mgr.evaluate("open_app", {})
            mock_audit.info.assert_called()

    def test_audit_skipped_when_disabled(self):
        mgr = _make_manager()
        # audit_log_enabled is False by default in _make_manager
        with _patched(mgr), \
             patch("core.permissions.audit_logger") as mock_audit:
            mgr.evaluate("open_app", {})
            mock_audit.info.assert_not_called()


# ===========================================================================
# Level Switching Tests
# ===========================================================================

class TestLevelSwitching(unittest.TestCase):
    def test_set_level(self):
        mgr = _make_manager()
        with _patched(mgr):
            result = mgr.set_level("TRUSTED")
            self.assertEqual(result, PermissionLevel.TRUSTED)

    def test_set_level_alias(self):
        mgr = _make_manager()
        with _patched(mgr):
            result = mgr.set_level("admin")
            self.assertEqual(result, PermissionLevel.ADMIN_CONFIRM)

    def test_set_level_invalid_raises(self):
        mgr = _make_manager()
        with _patched(mgr):
            with self.assertRaises(ValueError):
                mgr.set_level("invalid_level")


# ===========================================================================
# Record Execution Tests
# ===========================================================================

class TestRecordExecution(unittest.TestCase):
    def test_record_does_not_crash(self):
        mgr = _make_manager()
        with _patched(mgr):
            mgr.record_execution("open_app", {}, success=True)
            mgr.record_execution("unknown", {}, success=False, error="test error")


if __name__ == "__main__":
    unittest.main()
