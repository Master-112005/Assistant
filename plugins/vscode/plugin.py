from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping

from skills.base import PluginBase, SkillExecutionResult


class VSCodePlugin(PluginBase):
    def __init__(self) -> None:
        self._code_command = ""

    def plugin_id(self) -> str:
        return "vscode"

    def name(self) -> str:
        return "VS Code Plugin"

    def version(self) -> str:
        return "1.0.0"

    def description(self) -> str:
        return "Launches Visual Studio Code from local Windows installations."

    def initialize(self, context: Mapping[str, Any]) -> None:
        self._code_command = self._resolve_code_command()

    def shutdown(self) -> None:
        return None

    def can_handle(self, command: str, context: Mapping[str, Any]) -> bool:
        normalized = " ".join(str(command or "").lower().split())
        triggers = (
            "open vscode",
            "open vs code",
            "launch vscode",
            "launch vs code",
            "start vscode",
            "start vs code",
            "open code editor",
        )
        return normalized in triggers or normalized.startswith("open current folder in vs code") or normalized.startswith("open current folder in vscode")

    def execute(self, command: str, context: Mapping[str, Any]) -> SkillExecutionResult:
        if not self._code_command:
            return SkillExecutionResult(
                success=False,
                intent="vscode_open",
                response="Visual Studio Code was not found on PATH or in standard Windows install locations.",
                skill_name="",
                error="vscode_not_found",
                data={"target_app": "vscode", "action_taken": "resolve_executable"},
            )

        normalized = " ".join(str(command or "").lower().split())
        args = [self._code_command]
        if "current folder" in normalized:
            cwd = Path(str(context.get("cwd") or Path.cwd())).resolve()
            args.append(str(cwd))

        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
        target = " current folder" if len(args) > 1 else ""
        return SkillExecutionResult(
            success=True,
            intent="vscode_open",
            response=f"Opening VS Code{target}.",
            skill_name="",
            data={"target_app": "vscode", "action_taken": "launch_vscode", "executable": self._code_command},
        )

    def health_check(self) -> dict[str, Any]:
        return {
            "ok": bool(self._code_command),
            "code_command": self._code_command,
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "commands": [
                "open vscode",
                "open vs code",
                "open current folder in vscode",
            ],
            "permissions": ["filesystem", "automation"],
        }

    @staticmethod
    def _resolve_code_command() -> str:
        discovered = shutil.which("code") or shutil.which("code.cmd")
        if discovered:
            return discovered

        candidates = []
        local_app_data = os.environ.get("LOCALAPPDATA")
        program_files = os.environ.get("ProgramFiles")
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        if local_app_data:
            candidates.append(Path(local_app_data) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd")
        if program_files:
            candidates.append(Path(program_files) / "Microsoft VS Code" / "bin" / "code.cmd")
        if program_files_x86:
            candidates.append(Path(program_files_x86) / "Microsoft VS Code" / "bin" / "code.cmd")

        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return ""
