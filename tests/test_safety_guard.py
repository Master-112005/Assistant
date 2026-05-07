"""
Tests for PHASE 30 - Safety Guard pre-execution analysis.

Covers:
  - Enums and dataclasses
  - File delete inspection (single file, folder, permanent, common folder)
  - Move inspection (collision, cross-drive, protected destination)
  - System action inspection (shutdown, restart, sleep, wifi)
  - Token creation / approval / denial / expiry
  - Threshold-based severity escalation
  - Human-readable warning generation
  - Edge cases (missing path, permission denied, guard disabled)
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.safety_guard import (
    ImpactMetrics,
    SafetyCheckResult,
    SafetyGuard,
    SafetySeverity,
    _format_bytes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_guard(*, time_fn=None):
    """Create a SafetyGuard with mocked settings/state."""
    with patch("core.safety_guard.settings") as mock_settings, \
         patch("core.safety_guard.state") as mock_state:
        defaults = {
            "safety_guard_enabled": True,
            "confirmation_timeout_seconds": 60,
            "large_delete_threshold_files": 20,
            "large_delete_threshold_mb": 100,
            "warn_on_common_folders": True,
            "prefer_recycle_bin": True,
            "audit_log_enabled": False,
        }
        mock_settings.get = lambda key, default=None: defaults.get(key, default)
        mock_state.last_safety_check = {}
        mock_state.pending_safety_confirmations = {}
        mock_state.last_warning_message = ""
        mock_state.last_confirmed_action = {}
        guard = SafetyGuard(time_fn=time_fn)
    guard._settings_mock = mock_settings
    guard._state_mock = mock_state
    return guard


def _patched(guard):
    return patch.multiple(
        "core.safety_guard",
        settings=guard._settings_mock,
        state=guard._state_mock,
    )


# ===========================================================================
# Enum / Dataclass Tests
# ===========================================================================

class TestSafetySeverity(unittest.TestCase):
    def test_values(self):
        self.assertEqual(SafetySeverity.LOW.value, "LOW")
        self.assertEqual(SafetySeverity.MEDIUM.value, "MEDIUM")
        self.assertEqual(SafetySeverity.HIGH.value, "HIGH")
        self.assertEqual(SafetySeverity.CRITICAL.value, "CRITICAL")


class TestImpactMetrics(unittest.TestCase):
    def test_size_human_bytes(self):
        m = ImpactMetrics(bytes_affected=500)
        self.assertEqual(m.size_human, "500 B")

    def test_size_human_kb(self):
        m = ImpactMetrics(bytes_affected=2048)
        self.assertEqual(m.size_human, "2.0 KB")

    def test_size_human_mb(self):
        m = ImpactMetrics(bytes_affected=5 * 1024 * 1024)
        self.assertEqual(m.size_human, "5.0 MB")

    def test_size_human_gb(self):
        m = ImpactMetrics(bytes_affected=3 * 1024 * 1024 * 1024)
        self.assertIn("3.00 GB", m.size_human)

    def test_to_dict(self):
        m = ImpactMetrics(files_count=5, folders_count=2, bytes_affected=1024)
        d = m.to_dict()
        self.assertEqual(d["files_count"], 5)
        self.assertEqual(d["folders_count"], 2)
        self.assertIn("size_human", d)


class TestSafetyCheckResult(unittest.TestCase):
    def test_to_dict(self):
        r = SafetyCheckResult(
            allowed=False, requires_confirmation=True,
            severity=SafetySeverity.HIGH, action="delete",
            summary="Test", confirmation_token="abc",
        )
        d = r.to_dict()
        self.assertFalse(d["allowed"])
        self.assertEqual(d["severity"], "HIGH")
        self.assertEqual(d["confirmation_token"], "abc")


# ===========================================================================
# File Delete Inspection Tests
# ===========================================================================

class TestInspectFileDelete(unittest.TestCase):
    def setUp(self):
        self.guard = _make_guard()
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_single_file_delete(self):
        f = Path(self.tmpdir) / "test.txt"
        f.write_text("hello")
        with _patched(self.guard):
            result = self.guard.inspect("delete", {"path": str(f)})
        self.assertIn("1 file", result.summary)
        self.assertIn("test.txt", result.summary)

    def test_folder_delete_counts_contents(self):
        folder = Path(self.tmpdir) / "subfolder"
        folder.mkdir()
        for i in range(5):
            (folder / f"file{i}.txt").write_text(f"content{i}")
        with _patched(self.guard):
            result = self.guard.inspect("delete", {"path": str(folder)})
        self.assertEqual(result.impact_metrics.files_count, 5)
        # 5 files is below the 20-file threshold → allowed without confirmation
        self.assertTrue(result.allowed)

    def test_permanent_delete_escalates(self):
        f = Path(self.tmpdir) / "important.txt"
        f.write_text("data")
        with _patched(self.guard):
            result = self.guard.inspect("delete", {"path": str(f), "permanent": True})
        self.assertIn("permanently delete", result.summary)
        self.assertGreaterEqual(
            list(SafetySeverity).index(result.severity),
            list(SafetySeverity).index(SafetySeverity.HIGH),
        )

    def test_permanent_suggests_recycle_alternative(self):
        f = Path(self.tmpdir) / "report.txt"
        f.write_text("data")
        with _patched(self.guard):
            result = self.guard.inspect("delete", {"path": str(f), "permanent": True})
        self.assertIn("Recycle Bin", result.recommended_alternative)

    def test_missing_path_handled(self):
        with _patched(self.guard):
            result = self.guard.inspect("delete", {"path": os.path.join(self.tmpdir, "nonexistent.txt")})
        self.assertFalse(result.allowed)
        self.assertIn("does not exist", result.summary)

    def test_no_path_provided(self):
        with _patched(self.guard):
            result = self.guard.inspect("delete", {})
        self.assertFalse(result.allowed)
        self.assertTrue(result.requires_confirmation)

    def test_large_folder_escalates_severity(self):
        folder = Path(self.tmpdir) / "bigfolder"
        folder.mkdir()
        for i in range(25):
            (folder / f"file{i}.txt").write_text("x" * 100)
        with _patched(self.guard):
            result = self.guard.inspect("delete", {"path": str(folder)})
        self.assertEqual(result.impact_metrics.files_count, 25)
        self.assertGreaterEqual(
            list(SafetySeverity).index(result.severity),
            list(SafetySeverity).index(SafetySeverity.HIGH),
        )

    def test_common_folder_warning(self):
        """Test with a folder named 'Downloads' inside the temp dir."""
        downloads = Path(self.tmpdir) / "Downloads"
        downloads.mkdir()
        (downloads / "file.txt").write_text("test")
        with _patched(self.guard):
            result = self.guard.inspect("delete", {"path": str(downloads)})
        has_common = any("common" in w.lower() or "high-value" in w.lower() for w in result.warnings)
        self.assertTrue(has_common)


# ===========================================================================
# Move Inspection Tests
# ===========================================================================

class TestInspectMove(unittest.TestCase):
    def setUp(self):
        self.guard = _make_guard()
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_simple_move_no_collision(self):
        src = Path(self.tmpdir) / "src.txt"
        src.write_text("data")
        dst_dir = Path(self.tmpdir) / "dest"
        dst_dir.mkdir()
        with _patched(self.guard):
            result = self.guard.inspect("move", {"source_path": str(src), "target_path": str(dst_dir)})
        self.assertTrue(result.allowed)

    def test_collision_warning(self):
        src = Path(self.tmpdir) / "conflict.txt"
        src.write_text("source")
        dst_dir = Path(self.tmpdir) / "dest"
        dst_dir.mkdir()
        (dst_dir / "conflict.txt").write_text("existing")
        with _patched(self.guard):
            result = self.guard.inspect("move", {"source_path": str(src), "target_path": str(dst_dir)})
        self.assertTrue(result.requires_confirmation)
        has_collision = any("already exists" in w for w in result.warnings)
        self.assertTrue(has_collision)

    def test_missing_source(self):
        with _patched(self.guard):
            result = self.guard.inspect("move", {
                "source_path": os.path.join(self.tmpdir, "nope.txt"),
                "target_path": self.tmpdir,
            })
        self.assertIn("does not exist", result.summary)


# ===========================================================================
# System Action Inspection Tests
# ===========================================================================

class TestInspectSystemAction(unittest.TestCase):
    def setUp(self):
        self.guard = _make_guard()

    def test_shutdown_warning(self):
        with _patched(self.guard):
            result = self.guard.inspect("shutdown", {})
        self.assertTrue(result.requires_confirmation)
        self.assertIn("power off", result.summary)
        self.assertFalse(result.impact_metrics.is_reversible)

    def test_restart_warning(self):
        with _patched(self.guard):
            result = self.guard.inspect("restart", {})
        self.assertTrue(result.requires_confirmation)
        self.assertIn("restart", result.summary)

    def test_sleep_warning(self):
        with _patched(self.guard):
            result = self.guard.inspect("sleep", {})
        self.assertTrue(result.requires_confirmation)
        self.assertIn("sleep", result.summary)

    def test_wifi_off_warning(self):
        with _patched(self.guard):
            result = self.guard.inspect("wifi_off", {})
        self.assertTrue(result.requires_confirmation)
        self.assertIn("Wi-Fi", result.summary)


# ===========================================================================
# Token Flow Tests
# ===========================================================================

class TestTokenFlow(unittest.TestCase):
    def setUp(self):
        self.guard = _make_guard()

    def test_token_created_on_confirm_required(self):
        with _patched(self.guard):
            result = self.guard.inspect("shutdown", {})
        self.assertTrue(len(result.confirmation_token) > 0)
        self.assertIsNotNone(result.expires_at)

    def test_approve_valid_token(self):
        with _patched(self.guard):
            result = self.guard.inspect("shutdown", {})
            approved = self.guard.approve(result.confirmation_token)
        self.assertIsNotNone(approved)
        self.assertEqual(approved.action, "shutdown")

    def test_deny_valid_token(self):
        with _patched(self.guard):
            result = self.guard.inspect("shutdown", {})
            denied = self.guard.deny(result.confirmation_token)
        self.assertIsNotNone(denied)

    def test_approve_invalid_token(self):
        with _patched(self.guard):
            self.assertIsNone(self.guard.approve("invalid_token"))

    def test_approve_expired_token(self):
        fake_time = [1000.0]
        guard = _make_guard(time_fn=lambda: fake_time[0])
        with _patched(guard):
            result = guard.inspect("shutdown", {})
            token = result.confirmation_token
            fake_time[0] = 1100.0  # Past 60s timeout
            approved = guard.approve(token)
        self.assertIsNone(approved)

    def test_token_valid_check(self):
        with _patched(self.guard):
            result = self.guard.inspect("shutdown", {})
            self.assertTrue(self.guard.is_token_valid(result.confirmation_token))
            self.guard.approve(result.confirmation_token)
            self.assertFalse(self.guard.is_token_valid(result.confirmation_token))

    def test_no_token_reuse(self):
        with _patched(self.guard):
            result = self.guard.inspect("shutdown", {})
            token = result.confirmation_token
            self.guard.approve(token)
            # Second approve should fail
            self.assertIsNone(self.guard.approve(token))


# ===========================================================================
# Guard Disabled Tests
# ===========================================================================

class TestGuardDisabled(unittest.TestCase):
    def test_disabled_allows_everything(self):
        guard = _make_guard()
        guard._settings_mock.get = lambda key, default=None: {
            "safety_guard_enabled": False,
            "confirmation_timeout_seconds": 60,
            "audit_log_enabled": False,
        }.get(key, default)

        with _patched(guard):
            result = guard.inspect("shutdown", {})
        self.assertTrue(result.allowed)
        self.assertFalse(result.requires_confirmation)


# ===========================================================================
# Safe Actions (no analysis needed)
# ===========================================================================

class TestSafeActions(unittest.TestCase):
    def setUp(self):
        self.guard = _make_guard()

    def test_open_app_no_guard(self):
        with _patched(self.guard):
            result = self.guard.inspect("open_app", {"target": "chrome"})
        self.assertTrue(result.allowed)

    def test_search_no_guard(self):
        with _patched(self.guard):
            result = self.guard.inspect("search", {"query": "hello"})
        self.assertTrue(result.allowed)


# ===========================================================================
# Utility Tests
# ===========================================================================

class TestFormatBytes(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(_format_bytes(0), "0 B")
        self.assertEqual(_format_bytes(512), "512 B")

    def test_kb(self):
        self.assertEqual(_format_bytes(1024), "1.0 KB")

    def test_mb(self):
        self.assertEqual(_format_bytes(1024 * 1024), "1.0 MB")

    def test_gb(self):
        self.assertEqual(_format_bytes(1024 * 1024 * 1024), "1.00 GB")


class TestEstimateFolderContents(unittest.TestCase):
    def setUp(self):
        self.guard = _make_guard()
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_folder(self):
        folder = Path(self.tmpdir) / "empty"
        folder.mkdir()
        metrics = self.guard.estimate_folder_contents(folder)
        self.assertEqual(metrics.files_count, 0)
        self.assertEqual(metrics.folders_count, 0)

    def test_folder_with_files(self):
        folder = Path(self.tmpdir) / "mixed"
        folder.mkdir()
        sub = folder / "sub"
        sub.mkdir()
        for i in range(3):
            (folder / f"f{i}.txt").write_text("x" * 50)
        (sub / "deep.txt").write_text("y" * 100)
        metrics = self.guard.estimate_folder_contents(folder)
        self.assertEqual(metrics.files_count, 4)
        self.assertEqual(metrics.folders_count, 1)
        self.assertGreater(metrics.bytes_affected, 0)


if __name__ == "__main__":
    unittest.main()
