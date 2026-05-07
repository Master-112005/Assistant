"""
Pre-execution safety analysis and confirmation system.

Inspects the real impact of risky actions *before* execution, generates
human-readable warnings with concrete metrics, and manages confirmation
tokens that are bound to the exact inspected action.

Architecture
------------
    input
      → permissions.evaluate()      (policy check — Phase 29)
      → safety_guard.inspect()      (impact analysis — Phase 30)
      → if safe → execute
      → if confirm required → present warning → await user response
      → on approval → execute exact inspected action
      → audit trail

Classes
-------
    SafetySeverity      LOW | MEDIUM | HIGH | CRITICAL
    ImpactMetrics       files/folders/bytes/reversibility
    SafetyCheckResult   full inspection result
    SafetyGuard         main inspection + token engine
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from core import settings, state
from core.logger import get_audit_logger, get_logger
from core.path_resolver import PathResolver

logger = get_logger(__name__)
audit_logger = get_audit_logger("audit.safety")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SafetySeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ImpactMetrics:
    """Concrete, measurable impact of an action on the filesystem or system."""
    files_count: int = 0
    folders_count: int = 0
    bytes_affected: int = 0
    targets: list[str] = field(default_factory=list)
    system_scope: str = ""
    is_reversible: bool = True

    @property
    def size_human(self) -> str:
        return _format_bytes(self.bytes_affected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_count": self.files_count,
            "folders_count": self.folders_count,
            "bytes_affected": self.bytes_affected,
            "size_human": self.size_human,
            "targets": list(self.targets),
            "system_scope": self.system_scope,
            "is_reversible": self.is_reversible,
        }


@dataclass(slots=True)
class SafetyCheckResult:
    """Full pre-execution safety inspection result."""
    allowed: bool
    requires_confirmation: bool
    severity: SafetySeverity
    action: str
    summary: str
    warnings: list[str] = field(default_factory=list)
    impact_metrics: ImpactMetrics = field(default_factory=ImpactMetrics)
    confirmation_token: str = ""
    expires_at: float | None = None
    recommended_alternative: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    _callback: Callable[[], Any] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "requires_confirmation": self.requires_confirmation,
            "severity": self.severity.value,
            "action": self.action,
            "summary": self.summary,
            "warnings": list(self.warnings),
            "impact_metrics": self.impact_metrics.to_dict(),
            "confirmation_token": self.confirmation_token,
            "expires_at": self.expires_at,
            "recommended_alternative": self.recommended_alternative,
        }


# ---------------------------------------------------------------------------
# Common-folder detection
# ---------------------------------------------------------------------------

_COMMON_FOLDER_NAMES = frozenset({
    "desktop", "documents", "downloads", "pictures", "music", "videos",
    "onedrive", "appdata", "programdata",
})

_HIGH_VALUE_FOLDER_NAMES = frozenset({
    "documents", "pictures", "desktop", "onedrive",
})


# ---------------------------------------------------------------------------
# SafetyGuard
# ---------------------------------------------------------------------------

class SafetyGuard:
    """
    Pre-execution safety analysis engine.

    Thread-safe.  Designed to sit between the permission check (Phase 29)
    and the execution engine.  The guard never *blocks* by itself — it
    returns a SafetyCheckResult that the caller uses to decide whether to
    prompt the user.
    """

    # Limits for folder scanning to avoid hanging on huge trees
    _SCAN_MAX_ITEMS = 5_000
    _SCAN_MAX_DEPTH = 10

    def __init__(
        self,
        *,
        path_resolver: PathResolver | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._resolver = path_resolver or PathResolver()
        self._time_fn = time_fn or time.time
        self._lock = threading.RLock()
        self._pending: dict[str, SafetyCheckResult] = {}

    # =================================================================== #
    # Public API                                                           #
    # =================================================================== #

    def inspect(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        callback: Callable[[], Any] | None = None,
    ) -> SafetyCheckResult:
        """
        Inspect an action and return a safety check result.

        If the action is safe, ``allowed=True`` and no token is created.
        If confirmation is required, a token is created and stored.
        """
        if not settings.get("safety_guard_enabled"):
            return self._allow(action, "Safety guard is disabled.")

        params = dict(params or {})
        normalized = self._normalize(action)

        try:
            if normalized in {"delete", "delete_file", "permanent_delete"}:
                result = self._inspect_file_delete(params)
            elif normalized in {"move", "move_file"}:
                result = self._inspect_move(params)
            elif normalized in {"rename", "rename_file"}:
                result = self._inspect_rename(params)
            elif normalized in {"overwrite", "overwrite_file", "create"}:
                result = self._inspect_overwrite(params)
            elif normalized in {
                "shutdown", "restart", "reboot", "sleep",
                "hibernate", "logoff", "sign_out",
            }:
                result = self._inspect_system_action(normalized, params)
            elif normalized in {"wifi_off", "bluetooth_off"}:
                result = self._inspect_connectivity(normalized, params)
            else:
                return self._allow(action, "No safety concerns for this action.")
        except Exception as exc:
            logger.error("Safety inspection failed for %s: %s", action, exc)
            # When uncertain, escalate and require confirmation
            result = SafetyCheckResult(
                allowed=False,
                requires_confirmation=True,
                severity=SafetySeverity.HIGH,
                action=action,
                summary=f"Could not fully inspect this action: {exc}",
                warnings=["Safety analysis encountered an error. Confirmation required as a precaution."],
                params=params,
            )

        result._callback = callback

        if result.requires_confirmation:
            result = self._create_confirmation_token(result)

        self._audit("inspect", action=action, severity=result.severity.value,
                     summary=result.summary, requires_confirmation=result.requires_confirmation)
        state.last_safety_check = result.to_dict()
        return result

    def inspect_file_delete(self, path: str | Path) -> SafetyCheckResult:
        """Convenience for inspecting a file delete by path."""
        return self.inspect("delete", {"path": str(path)})

    def inspect_move(self, src: str | Path, dst: str | Path) -> SafetyCheckResult:
        """Convenience for inspecting a file move."""
        return self.inspect("move", {"source_path": str(src), "target_path": str(dst)})

    def inspect_system_action(self, action: str) -> SafetyCheckResult:
        """Convenience for inspecting a system control action."""
        return self.inspect(action, {})

    def approve(self, token: str) -> SafetyCheckResult | None:
        """Approve a pending safety confirmation. Returns the result or None."""
        with self._lock:
            self._prune_expired()
            result = self._pending.pop(token, None)
        if result is None:
            self._audit("approve_missing", token=token)
            return None
        self._audit("approved", token=token, action=result.action)
        state.last_confirmed_action = result.to_dict()
        return result

    def deny(self, token: str) -> SafetyCheckResult | None:
        """Deny a pending safety confirmation. Returns the result or None."""
        with self._lock:
            self._prune_expired()
            result = self._pending.pop(token, None)
        if result is None:
            self._audit("deny_missing", token=token)
            return None
        self._audit("denied", token=token, action=result.action)
        return result

    def is_token_valid(self, token: str) -> bool:
        """Check if a token is still valid (exists and not expired)."""
        with self._lock:
            self._prune_expired()
            return token in self._pending

    def get_pending(self, token: str) -> SafetyCheckResult | None:
        """Retrieve a pending result without consuming it."""
        with self._lock:
            self._prune_expired()
            return self._pending.get(token)

    def generate_warning(self, result: SafetyCheckResult) -> str:
        """Generate a human-readable warning string from a check result."""
        return result.summary

    def estimate_folder_contents(self, path: str | Path) -> ImpactMetrics:
        """Scan a folder and return impact metrics."""
        try:
            resolved = self._resolve_path(path)
        except Exception:
            return ImpactMetrics()
        if not resolved.exists():
            return ImpactMetrics(targets=[str(path)])
        if not resolved.is_dir():
            return self._single_file_metrics(resolved)
        return self._scan_folder(resolved)

    # =================================================================== #
    # File-action inspections                                              #
    # =================================================================== #

    def _inspect_file_delete(self, params: dict[str, Any]) -> SafetyCheckResult:
        path_ref = (
            params.get("path") or params.get("source_path")
            or params.get("filename") or params.get("reference") or ""
        )
        permanent = bool(params.get("permanent"))
        prefer_recycle = bool(settings.get("prefer_recycle_bin"))

        if not path_ref:
            return SafetyCheckResult(
                allowed=False, requires_confirmation=True,
                severity=SafetySeverity.HIGH,
                action="delete", summary="No target path specified for delete.",
                warnings=["Cannot determine what to delete."], params=params,
            )

        try:
            resolved = self._resolve_path(path_ref)
        except Exception as exc:
            return SafetyCheckResult(
                allowed=False, requires_confirmation=True,
                severity=SafetySeverity.HIGH, action="delete",
                summary=f"Could not resolve path: {path_ref}",
                warnings=[str(exc)], params=params,
            )

        if not resolved.exists():
            return SafetyCheckResult(
                allowed=False, requires_confirmation=False,
                severity=SafetySeverity.LOW, action="delete",
                summary=f"Target does not exist: {resolved.name}",
                warnings=["Nothing to delete."], params=params,
            )

        # Compute metrics
        if resolved.is_dir():
            metrics = self._scan_folder(resolved)
        else:
            metrics = self._single_file_metrics(resolved)

        metrics.is_reversible = not permanent and prefer_recycle
        warnings: list[str] = []
        severity = SafetySeverity.LOW
        alternative = ""

        # Check if it's a common/high-value folder
        folder_name_lower = resolved.name.lower()
        is_common = folder_name_lower in _COMMON_FOLDER_NAMES
        is_high_value = folder_name_lower in _HIGH_VALUE_FOLDER_NAMES

        if is_high_value:
            warnings.append(f"'{resolved.name}' is a high-value user folder.")
            severity = SafetySeverity.CRITICAL

        if is_common and not is_high_value:
            warnings.append(f"'{resolved.name}' is a common system folder.")
            severity = max(severity, SafetySeverity.HIGH, key=lambda s: list(SafetySeverity).index(s))

        # Check protected path
        if self._resolver.is_system_directory(resolved):
            warnings.append("This path is inside a protected system directory.")
            severity = SafetySeverity.CRITICAL

        # Check thresholds
        file_threshold = int(settings.get("large_delete_threshold_files") or 20)
        mb_threshold = int(settings.get("large_delete_threshold_mb") or 100)
        mb_affected = metrics.bytes_affected / (1024 * 1024)

        if metrics.files_count >= file_threshold:
            warnings.append(f"This affects {metrics.files_count} files.")
            severity = max(severity, SafetySeverity.HIGH, key=lambda s: list(SafetySeverity).index(s))

        if mb_affected >= mb_threshold:
            warnings.append(f"This affects {metrics.size_human} of data.")
            severity = max(severity, SafetySeverity.HIGH, key=lambda s: list(SafetySeverity).index(s))

        if permanent:
            warnings.append("This is a permanent delete — data cannot be recovered.")
            severity = max(severity, SafetySeverity.HIGH, key=lambda s: list(SafetySeverity).index(s))
            if prefer_recycle:
                alternative = "Move to Recycle Bin instead of permanent delete?"

        # Build summary
        summary = self._build_delete_summary(resolved, metrics, permanent)

        needs_confirm = (
            severity.value != SafetySeverity.LOW.value
            or permanent
            or metrics.files_count >= file_threshold
            or mb_affected >= mb_threshold
            or is_common
            or bool(settings.get("warn_on_common_folders") and is_common)
        )

        return SafetyCheckResult(
            allowed=not needs_confirm,
            requires_confirmation=needs_confirm,
            severity=severity,
            action="delete",
            summary=summary,
            warnings=warnings,
            impact_metrics=metrics,
            recommended_alternative=alternative,
            params=params,
        )

    def _inspect_move(self, params: dict[str, Any]) -> SafetyCheckResult:
        src_ref = params.get("source_path") or params.get("path") or params.get("filename") or ""
        dst_ref = params.get("target_path") or params.get("destination") or ""

        if not src_ref:
            return self._require("move", "No source path specified for move.", params=params)
        if not dst_ref:
            return self._require("move", "No destination specified for move.", params=params)

        try:
            src = self._resolve_path(src_ref)
        except Exception:
            return self._require("move", f"Could not resolve source: {src_ref}", params=params)

        if not src.exists():
            return SafetyCheckResult(
                allowed=False, requires_confirmation=False,
                severity=SafetySeverity.LOW, action="move",
                summary=f"Source does not exist: {src.name}",
                warnings=["Nothing to move."], params=params,
            )

        try:
            dst = self._resolve_path(dst_ref)
        except Exception:
            return self._require("move", f"Could not resolve destination: {dst_ref}", params=params)

        warnings: list[str] = []
        severity = SafetySeverity.LOW

        # Collision check
        final_target = dst / src.name if dst.is_dir() else dst
        if final_target.exists():
            warnings.append(f"'{final_target.name}' already exists at the destination.")
            severity = SafetySeverity.MEDIUM

        # Protected destination
        if self._resolver.is_system_directory(dst):
            warnings.append("Destination is inside a protected system directory.")
            severity = SafetySeverity.HIGH

        # Cross-drive
        try:
            if src.drive.upper() != dst.drive.upper():
                warnings.append("This is a cross-drive move (copy + delete).")
        except Exception:
            pass

        # Metrics
        if src.is_dir():
            metrics = self._scan_folder(src)
        else:
            metrics = self._single_file_metrics(src)
        metrics.is_reversible = True

        summary = self._build_move_summary(src, dst, metrics)
        needs_confirm = bool(warnings) or metrics.files_count >= int(
            settings.get("large_delete_threshold_files") or 20
        )

        return SafetyCheckResult(
            allowed=not needs_confirm,
            requires_confirmation=needs_confirm,
            severity=severity, action="move",
            summary=summary, warnings=warnings,
            impact_metrics=metrics, params=params,
        )

    def _inspect_rename(self, params: dict[str, Any]) -> SafetyCheckResult:
        src_ref = params.get("source_path") or params.get("path") or ""
        new_name = params.get("new_name") or ""

        if not src_ref or not new_name:
            return self._allow("rename", "Rename parameters incomplete.")

        try:
            src = self._resolve_path(src_ref)
        except Exception:
            return self._allow("rename", "Could not resolve source for rename.")

        target = src.with_name(str(new_name).strip())
        warnings: list[str] = []
        if target.exists():
            warnings.append(f"'{target.name}' already exists — it will be overwritten.")

        if warnings:
            return SafetyCheckResult(
                allowed=False, requires_confirmation=True,
                severity=SafetySeverity.MEDIUM, action="rename",
                summary=f"Rename '{src.name}' to '{target.name}'? {'; '.join(warnings)}",
                warnings=warnings, params=params,
            )
        return self._allow("rename", f"Rename '{src.name}' to '{target.name}'.")

    def _inspect_overwrite(self, params: dict[str, Any]) -> SafetyCheckResult:
        if not params.get("overwrite"):
            return self._allow("create", "No overwrite requested.")

        target_ref = params.get("target_path") or params.get("path") or ""
        if not target_ref:
            return self._allow("create", "No target path for overwrite check.")

        try:
            target = self._resolve_path(target_ref)
        except Exception:
            return self._allow("create", "Could not resolve target.")

        if target.exists():
            metrics = self._single_file_metrics(target) if target.is_file() else self._scan_folder(target)
            return SafetyCheckResult(
                allowed=False, requires_confirmation=True,
                severity=SafetySeverity.MEDIUM, action="overwrite",
                summary=f"This will overwrite '{target.name}' ({metrics.size_human}).",
                warnings=["Existing content will be replaced."],
                impact_metrics=metrics, params=params,
            )

        return self._allow("create", "Target does not exist. No overwrite risk.")

    # =================================================================== #
    # System-action inspections                                            #
    # =================================================================== #

    def _inspect_system_action(self, action: str, params: dict[str, Any]) -> SafetyCheckResult:
        messages = {
            "shutdown": ("This will close all open applications and power off the PC.", SafetySeverity.HIGH),
            "restart": ("This will close all open applications and restart the PC.", SafetySeverity.HIGH),
            "reboot": ("This will close all open applications and restart the PC.", SafetySeverity.HIGH),
            "sleep": ("This will put the PC to sleep immediately.", SafetySeverity.MEDIUM),
            "hibernate": ("This will hibernate the PC immediately.", SafetySeverity.MEDIUM),
            "logoff": ("This will sign out of Windows, closing all applications.", SafetySeverity.HIGH),
            "sign_out": ("This will sign out of Windows, closing all applications.", SafetySeverity.HIGH),
        }
        summary, severity = messages.get(action, (f"System action: {action}", SafetySeverity.MEDIUM))

        warnings = ["Unsaved work in open applications may be lost."]
        if action in {"shutdown", "restart", "reboot"}:
            warnings.append("All running programs will be closed.")

        delay = params.get("delay_seconds")
        if delay and int(delay) > 0:
            summary += f" (in {delay} seconds)"

        return SafetyCheckResult(
            allowed=False, requires_confirmation=True,
            severity=severity, action=action, summary=summary,
            warnings=warnings,
            impact_metrics=ImpactMetrics(system_scope=action, is_reversible=False),
            params=params,
        )

    def _inspect_connectivity(self, action: str, params: dict[str, Any]) -> SafetyCheckResult:
        label = "Wi-Fi" if "wifi" in action else "Bluetooth"
        return SafetyCheckResult(
            allowed=False, requires_confirmation=True,
            severity=SafetySeverity.MEDIUM, action=action,
            summary=f"Turning {label} off will disconnect active connections.",
            warnings=[f"{label} connections will be interrupted."],
            impact_metrics=ImpactMetrics(system_scope=action, is_reversible=True),
            params=params,
        )

    # =================================================================== #
    # Token management                                                     #
    # =================================================================== #

    def _create_confirmation_token(self, result: SafetyCheckResult) -> SafetyCheckResult:
        timeout = max(10, int(settings.get("confirmation_timeout_seconds") or 60))
        token = uuid.uuid4().hex[:12]
        result.confirmation_token = token
        result.expires_at = self._time_fn() + timeout

        with self._lock:
            self._pending[token] = result
            self._sync_state()

        self._audit("token_created", token=token, action=result.action,
                     severity=result.severity.value, expires_in=timeout)
        return result

    def _prune_expired(self) -> None:
        now = self._time_fn()
        expired = [
            token for token, result in self._pending.items()
            if result.expires_at is not None and result.expires_at <= now
        ]
        for token in expired:
            removed = self._pending.pop(token, None)
            if removed:
                self._audit("token_expired", token=token, action=removed.action)
        if expired:
            self._sync_state()

    def _sync_state(self) -> None:
        state.pending_safety_confirmations = {
            token: result.to_dict() for token, result in self._pending.items()
        }

    # =================================================================== #
    # Filesystem scanning                                                  #
    # =================================================================== #

    def _scan_folder(self, path: Path) -> ImpactMetrics:
        """Walk a directory tree with bounded depth and item limits."""
        files = 0
        folders = 0
        total_bytes = 0
        targets: list[str] = [str(path)]

        stack: list[tuple[Path, int]] = [(path, 0)]
        while stack:
            current, depth = stack.pop()
            if depth > self._SCAN_MAX_DEPTH:
                continue
            try:
                for entry in os.scandir(current):
                    if files + folders >= self._SCAN_MAX_ITEMS:
                        break
                    try:
                        if entry.is_file(follow_symlinks=False):
                            files += 1
                            try:
                                total_bytes += entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                pass
                        elif entry.is_dir(follow_symlinks=False):
                            folders += 1
                            stack.append((Path(entry.path), depth + 1))
                    except OSError:
                        continue
            except OSError:
                continue

        return ImpactMetrics(
            files_count=files, folders_count=folders,
            bytes_affected=total_bytes, targets=targets,
        )

    def _single_file_metrics(self, path: Path) -> ImpactMetrics:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return ImpactMetrics(
            files_count=1, folders_count=0,
            bytes_affected=size, targets=[str(path)],
        )

    # =================================================================== #
    # Summary builders                                                     #
    # =================================================================== #

    def _build_delete_summary(
        self, path: Path, metrics: ImpactMetrics, permanent: bool,
    ) -> str:
        verb = "permanently delete" if permanent else "move to the Recycle Bin"

        if path.is_file():
            return f"This will {verb} 1 file: {path.name} ({metrics.size_human})."

        parts: list[str] = []
        if metrics.files_count > 0:
            parts.append(f"{metrics.files_count} file{'s' if metrics.files_count != 1 else ''}")
        if metrics.folders_count > 0:
            parts.append(f"{metrics.folders_count} folder{'s' if metrics.folders_count != 1 else ''}")

        content_desc = " and ".join(parts) if parts else "the folder"
        size_desc = f" ({metrics.size_human})" if metrics.bytes_affected > 0 else ""
        return f"This will {verb} {content_desc}{size_desc} from {path.name}."

    def _build_move_summary(
        self, src: Path, dst: Path, metrics: ImpactMetrics,
    ) -> str:
        if src.is_file():
            return f"Move '{src.name}' ({metrics.size_human}) to {self._resolver.location_label(dst)}."

        parts: list[str] = []
        if metrics.files_count > 0:
            parts.append(f"{metrics.files_count} file{'s' if metrics.files_count != 1 else ''}")
        if metrics.folders_count > 0:
            parts.append(f"{metrics.folders_count} folder{'s' if metrics.folders_count != 1 else ''}")

        content_desc = " and ".join(parts) if parts else src.name
        size_desc = f" ({metrics.size_human})" if metrics.bytes_affected > 0 else ""
        return f"Move {content_desc}{size_desc} to {self._resolver.location_label(dst)}."

    # =================================================================== #
    # Helpers                                                              #
    # =================================================================== #

    def _resolve_path(self, ref: str | Path) -> Path:
        return self._resolver.resolve(str(ref))

    def _allow(self, action: str, summary: str) -> SafetyCheckResult:
        return SafetyCheckResult(
            allowed=True, requires_confirmation=False,
            severity=SafetySeverity.LOW, action=action,
            summary=summary,
        )

    def _require(self, action: str, summary: str, *, params: dict | None = None) -> SafetyCheckResult:
        return SafetyCheckResult(
            allowed=False, requires_confirmation=True,
            severity=SafetySeverity.HIGH, action=action,
            summary=summary, warnings=[summary], params=params or {},
        )

    @staticmethod
    def _normalize(value: Any) -> str:
        return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")

    def _audit(self, event: str, **payload: Any) -> None:
        if not settings.get("audit_log_enabled"):
            return
        msg = {"event": f"safety.{event}", **payload}
        try:
            audit_logger.info(json.dumps(msg, sort_keys=True, default=str))
        except Exception:
            logger.debug("Failed to write safety audit log", exc_info=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

safety_guard = SafetyGuard()
