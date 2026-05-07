"""
Explorer skill.

Provides structured planner hints plus active-window keyboard actions for
Windows File Explorer when the selected item is the target.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ExplorerActionResult:
    success: bool
    operation: str
    message: str
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class ExplorerSkill:
    """Explorer-specific helpers for the context engine and executor."""

    def get_window_context(self, hwnd: int | None = None, *, fallback_title: str = "") -> dict[str, Any]:
        folder_name = str(fallback_title or "").strip()
        folder_path = ""

        if sys.platform == "win32":
            try:
                import win32com.client  # type: ignore[import-not-found]

                shell = win32com.client.Dispatch("Shell.Application")
                for window in shell.Windows():
                    try:
                        if hwnd and int(window.HWND) != int(hwnd):
                            continue
                        folder_path = str(window.Document.Folder.Self.Path or "").strip()
                        folder_name = str(window.LocationName or "").strip() or Path(folder_path).name or folder_name
                        if folder_name or folder_path:
                            break
                    except Exception:
                        continue
            except Exception as exc:
                logger.debug("Explorer context lookup failed: %s", exc)

        return {
            "folder_name": folder_name,
            "folder_path": folder_path,
        }

    def describe_window(self, *, hwnd: int | None = None, title: str = "") -> dict[str, Any]:
        context = self.get_window_context(hwnd, fallback_title=title)
        folder_name = str(context.get("folder_name") or "").strip()
        folder_path = str(context.get("folder_path") or "").strip()

        if folder_name:
            return {
                "summary": f"File Explorer is open in {folder_name}.",
                "confidence": 0.90 if folder_path else 0.82,
                "details": context,
            }
        if title:
            return {
                "summary": f"File Explorer is open in {title}.",
                "confidence": 0.76,
                "details": context,
            }
        return {"summary": "File Explorer is open.", "confidence": 0.60, "details": context}

    def build_file_action_step(
        self,
        action: str,
        *,
        target: str = "selected item",
        destination: str = "",
        selected: bool = False,
        new_name: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"action": action, "context_app": "explorer"}
        if destination:
            params["destination"] = destination
        if selected:
            params["selected"] = True
        if new_name:
            params["new_name"] = new_name
        estimated_risk = "high" if action in {"delete", "remove"} else "low"
        return {
            "action": "file_action",
            "target": target,
            "params": params,
            "requires_confirmation": estimated_risk == "high",
            "estimated_risk": estimated_risk,
        }

    def execute(self, action: str, **params: Any) -> ExplorerActionResult:
        normalized = (action or "").strip().lower()
        target = str(params.get("target", "selected item")).strip() or "selected item"
        new_name = str(params.get("new_name", "")).strip()

        if normalized in {"delete", "remove"}:
            return self._send_keys("{DEL}", "delete", f"Sent delete to {target}.")
        if normalized == "open":
            return self._send_keys("~", "open", f"Opened {target}.")
        if normalized == "copy":
            return self._send_keys("^c", "copy", f"Copied {target}.")
        if normalized == "cut":
            return self._send_keys("^x", "cut", f"Cut {target}.")
        if normalized == "paste":
            return self._send_keys("^v", "paste", "Pasted item in Explorer.")
        if normalized == "rename":
            if not new_name:
                return ExplorerActionResult(
                    success=False,
                    operation="rename",
                    error="missing_new_name",
                    message="Rename requires the new file or folder name.",
                )
            return self._rename_selected(new_name, target)

        return ExplorerActionResult(
            success=False,
            operation=normalized or "unknown",
            error="unsupported_explorer_action",
            message=f"Unsupported Explorer action: {action or 'unknown'}.",
        )

    def _rename_selected(self, new_name: str, target: str) -> ExplorerActionResult:
        escaped_name = self._escape_sendkeys_text(new_name)
        if not escaped_name:
            return ExplorerActionResult(
                success=False,
                operation="rename",
                error="invalid_new_name",
                message="Rename could not encode the new file or folder name for SendKeys.",
            )

        script = (
            "$wshell = New-Object -ComObject WScript.Shell; "
            "Start-Sleep -Milliseconds 50; "
            "$wshell.SendKeys('{F2}'); "
            "Start-Sleep -Milliseconds 50; "
            "$wshell.SendKeys('^a'); "
            f"$wshell.SendKeys('{escaped_name}'); "
            "$wshell.SendKeys('~')"
        )
        return self._run_script(script, "rename", f"Renamed {target} to {new_name}.")

    def _send_keys(self, key_sequence: str, operation: str, success_message: str) -> ExplorerActionResult:
        script = (
            "$wshell = New-Object -ComObject WScript.Shell; "
            "Start-Sleep -Milliseconds 50; "
            f"$wshell.SendKeys('{key_sequence}')"
        )
        return self._run_script(script, operation, success_message)

    def _run_script(self, script: str, operation: str, success_message: str) -> ExplorerActionResult:
        if sys.platform != "win32":
            return ExplorerActionResult(
                success=False,
                operation=operation,
                error="platform_not_supported",
                message="Explorer active-window actions are currently implemented only on Windows.",
            )

        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception as exc:
            return ExplorerActionResult(
                success=False,
                operation=operation,
                error=str(exc),
                message=f"Explorer action failed: {exc}",
            )

        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip() or f"Exit code {result.returncode}"
            return ExplorerActionResult(
                success=False,
                operation=operation,
                error=error,
                message=f"Explorer action failed: {error}",
            )

        return ExplorerActionResult(success=True, operation=operation, message=success_message)

    @staticmethod
    def _escape_sendkeys_text(text: str) -> str:
        escaped = text.replace("{", "{{}").replace("}", "{}}")
        for symbol in ("+", "^", "%", "~", "(", ")"):
            escaped = escaped.replace(symbol, f"{{{symbol}}}")
        return escaped
