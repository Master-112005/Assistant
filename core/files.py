"""
Shared file-management backend used by the processor, skills, and executor.
"""
from __future__ import annotations

import errno
import mimetypes
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Callable

from core import settings
from core.logger import get_logger
from core.path_resolver import PathResolver

logger = get_logger(__name__)

try:
    from send2trash import send2trash as _send2trash
except Exception:  # pragma: no cover - import availability depends on environment
    _send2trash = None

_WINDOWS_INVALID_NAME_CHARS = set('<>:"/\\|?*')


@dataclass(slots=True)
class FileActionResult:
    success: bool
    action: str
    source_path: str = ""
    target_path: str = ""
    message: str = ""
    error: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "success": self.success,
            "action": self.action,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "message": self.message,
            "error": self.error,
            "timestamp": self.timestamp,
        }


class FileManager:
    """Industrial-grade file operations with safe defaults and honest failures."""

    def __init__(
        self,
        *,
        path_resolver: PathResolver | None = None,
        opener: Callable[[str], None] | None = None,
        trash_func: Callable[[str], None] | None = None,
    ) -> None:
        self._resolver = path_resolver or PathResolver()
        self._opener = opener or self._default_opener
        self._trash = trash_func if trash_func is not None else _send2trash

    @property
    def resolver(self) -> PathResolver:
        return self._resolver

    def create_file(
        self,
        path: str | Path,
        content: str | None = None,
        *,
        overwrite: bool = False,
        create_parents: bool | None = None,
    ) -> FileActionResult:
        action = "create"
        try:
            target = self.resolve_target_path(path)
            create_parents = bool(
                settings.get("file_create_missing_parents") if create_parents is None else create_parents
            )
            validation_error = self._validate_name(target.name)
            if validation_error:
                return self._failure(action, target_path=target, message=validation_error, error="invalid_filename")

            if target.parent.exists() is False:
                if not create_parents:
                    return self._failure(
                        action,
                        target_path=target,
                        message=f"Parent folder does not exist: {target.parent}",
                        error="missing_parent_directory",
                    )
                target.parent.mkdir(parents=True, exist_ok=True)

            if target.exists() and not overwrite:
                return self._failure(
                    action,
                    target_path=target,
                    message=f"{target.name} already exists.",
                    error="destination_exists",
                )

            write_mode = "w" if overwrite else "x"
            with open(target, write_mode, encoding="utf-8", newline="") as handle:
                if content is not None:
                    handle.write(content)
            if content is None and overwrite and target.exists():
                target.touch(exist_ok=True)

            logger.info("Create file: %s", target)
            return FileActionResult(
                success=True,
                action=action,
                target_path=str(target),
                message=f"Created {target.name} {self._location_phrase(target.parent)}.",
            )
        except FileExistsError:
            target = self.resolve_target_path(path)
            return self._failure(action, target_path=target, message=f"{target.name} already exists.", error="destination_exists")
        except Exception as exc:
            return self._exception_result(action, exc, target_path=path)

    def open_file(self, path: str | Path) -> FileActionResult:
        action = "open"
        try:
            target = self.resolve_existing_path(path)
            self._opener(str(target))
            logger.info("Open file: %s", target)
            return FileActionResult(
                success=True,
                action=action,
                source_path=str(target),
                message=f"Opening {self._resolver.describe_path(target)}.",
            )
        except Exception as exc:
            return self._exception_result(action, exc, source_path=path)

    def rename_file(
        self,
        path: str | Path,
        new_name: str,
        *,
        overwrite: bool = False,
    ) -> FileActionResult:
        action = "rename"
        try:
            source = self.resolve_existing_path(path)
            target_name = self._clean_text(new_name)
            validation_error = self._validate_name(target_name)
            if validation_error:
                return self._failure(action, source_path=source, message=validation_error, error="invalid_filename")

            target = source.with_name(target_name)
            if self._same_path(source, target):
                return self._failure(
                    action,
                    source_path=source,
                    target_path=target,
                    message="Source and target name are the same.",
                    error="same_target",
                )

            if target.exists():
                if not overwrite:
                    return self._failure(
                        action,
                        source_path=source,
                        target_path=target,
                        message=f"{target.name} already exists.",
                        error="destination_exists",
                    )
                if target.is_dir() != source.is_dir():
                    return self._failure(
                        action,
                        source_path=source,
                        target_path=target,
                        message="Cannot overwrite a file with a folder or a folder with a file.",
                        error="type_mismatch",
                    )
                self._remove_for_overwrite(target)

            source.replace(target)
            logger.info("Rename file: %s -> %s", source, target)
            return FileActionResult(
                success=True,
                action=action,
                source_path=str(source),
                target_path=str(target),
                message=f"Renamed {source.name} to {target.name}.",
            )
        except Exception as exc:
            return self._exception_result(action, exc, source_path=path)

    def delete_file(self, path: str | Path, permanent: bool = False) -> FileActionResult:
        action = "delete"
        try:
            source = self.resolve_existing_path(path)
            logger.info("Delete file: %s", source)

            if permanent:
                self._delete_permanently(source)
                return FileActionResult(
                    success=True,
                    action=action,
                    source_path=str(source),
                    message=f"Permanently deleted {source.name}.",
                )

            if self._trash is None:
                return self._failure(
                    action,
                    source_path=source,
                    message="Safe delete is unavailable because send2trash is not installed.",
                    error="safe_delete_unavailable",
                )

            self._trash(str(source))
            return FileActionResult(
                success=True,
                action=action,
                source_path=str(source),
                message=f"Moved {source.name} to Recycle Bin.",
            )
        except Exception as exc:
            return self._exception_result(action, exc, source_path=path)

    def move_file(
        self,
        src: str | Path,
        dst: str | Path,
        *,
        overwrite: bool = False,
    ) -> FileActionResult:
        action = "move"
        try:
            source = self.resolve_existing_path(src)
            target = self.preview_move_target(source, dst)

            if self._same_path(source, target):
                return self._failure(
                    action,
                    source_path=source,
                    target_path=target,
                    message="Source and destination are the same.",
                    error="same_target",
                )

            parent = target.parent
            if parent.exists() is False:
                return self._failure(
                    action,
                    source_path=source,
                    target_path=target,
                    message=f"Destination folder does not exist: {parent}",
                    error="missing_destination_directory",
                )

            if target.exists():
                if not overwrite:
                    return self._failure(
                        action,
                        source_path=source,
                        target_path=target,
                        message=f"{target.name} already exists.",
                        error="destination_exists",
                    )
                if source.is_dir() != target.is_dir():
                    return self._failure(
                        action,
                        source_path=source,
                        target_path=target,
                        message="Cannot overwrite a file with a folder or a folder with a file.",
                        error="type_mismatch",
                    )
                self._remove_for_overwrite(target)

            moved_to = Path(shutil.move(str(source), str(target))).resolve(strict=False)
            logger.info("Move file: %s -> %s", source, moved_to)
            return FileActionResult(
                success=True,
                action=action,
                source_path=str(source),
                target_path=str(moved_to),
                message=f"Moved {source.name} to {self._resolver.location_label(moved_to.parent)}.",
            )
        except Exception as exc:
            return self._exception_result(action, exc, source_path=src, target_path=dst)

    def exists(self, path: str | Path) -> bool:
        try:
            return self.resolve_target_path(path).exists()
        except Exception:
            return False

    def list_if_needed(self, path: str | Path, *, limit: int = 20) -> list[str]:
        target = self.resolve_existing_path(path)
        if not target.is_dir():
            return []
        try:
            children = sorted(child.name for child in target.iterdir())
        except OSError:
            return []
        return children[:limit]

    def detect_type(self, path: str | Path) -> dict[str, str | bool]:
        target = self.resolve_target_path(path)
        if target.exists() and target.is_dir():
            return {"kind": "directory", "mime_type": "inode/directory", "exists": True}
        mime_type, _encoding = mimetypes.guess_type(str(target))
        return {
            "kind": "directory" if str(target).endswith(("\\", "/")) else "file",
            "mime_type": mime_type or "application/octet-stream",
            "exists": target.exists(),
        }

    def find_matches(
        self,
        reference: str,
        *,
        location_hint: str | Path | None = None,
        max_results: int | None = None,
    ) -> list[Path]:
        cleaned = self._clean_text(reference)
        if not cleaned:
            return []

        candidates: list[Path] = []
        try:
            resolved = self.resolve_target_path(cleaned, location_hint=location_hint)
            if resolved.exists():
                candidates.append(resolved)
        except Exception:
            pass

        if candidates:
            return self._dedupe_paths(candidates)

        return self._resolver.search_common_locations(
            cleaned,
            preferred_location=location_hint,
            max_results=max_results,
        )

    def resolve_target_path(
        self,
        reference: str | Path,
        *,
        location_hint: str | Path | None = None,
    ) -> Path:
        cleaned = self._clean_text(reference)
        if not cleaned:
            raise ValueError("Path is empty.")

        if location_hint and not Path(cleaned).is_absolute():
            base = self._resolver.resolve(location_hint)
            return self._resolver.ensure_safe((base / cleaned).resolve(strict=False))
        return self._resolver.resolve(cleaned)

    def resolve_existing_path(
        self,
        reference: str | Path,
        *,
        location_hint: str | Path | None = None,
    ) -> Path:
        target = self.resolve_target_path(reference, location_hint=location_hint)
        if not target.exists():
            raise FileNotFoundError(f"Path not found: {target}")
        return target

    def preview_move_target(self, src: str | Path, dst: str | Path) -> Path:
        source = src if isinstance(src, Path) else self.resolve_existing_path(src)
        destination_input = self._clean_text(dst)
        if not destination_input:
            raise ValueError("Destination path is empty.")

        destination = self.resolve_target_path(destination_input)
        if self._looks_like_directory_reference(destination_input, destination):
            return (destination / source.name).resolve(strict=False)
        return destination

    def count_items(self, path: str | Path, *, limit: int = 500) -> int:
        target = self.resolve_existing_path(path)
        if not target.is_dir():
            return 1
        count = 0
        queue = [target]
        while queue and count < limit:
            current = queue.pop()
            try:
                for child in current.iterdir():
                    count += 1
                    if count >= limit:
                        return count
                    if child.is_dir():
                        queue.append(child)
            except OSError:
                return count
        return count

    def _default_opener(self, path: str) -> None:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
            return
        command = ["open", path] if os.name == "posix" and "darwin" in os.sys.platform else ["xdg-open", path]
        subprocess.Popen(command)

    def _delete_permanently(self, target: Path) -> None:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, onerror=self._handle_remove_readonly)
            return
        target.unlink()

    def _remove_for_overwrite(self, target: Path) -> None:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, onerror=self._handle_remove_readonly)
            return
        target.unlink()

    def _handle_remove_readonly(self, func, path, exc_info) -> None:
        exc = exc_info[1]
        if not isinstance(exc, PermissionError):
            raise exc
        os.chmod(path, 0o666)
        func(path)

    def _validate_name(self, name: str) -> str:
        candidate = self._clean_text(name)
        if not candidate:
            return "File name cannot be empty."
        if any(char in _WINDOWS_INVALID_NAME_CHARS for char in candidate):
            return f"'{candidate}' contains characters that are not valid on Windows."
        if any(ord(char) < 32 for char in candidate):
            return "File name contains an invalid control character."
        if hasattr(os.path, "isreserved"):
            reserved = os.path.isreserved(candidate)
        else:  # pragma: no cover - fallback for older Python versions
            reserved = PureWindowsPath(candidate).is_reserved()
        if reserved:
            return f"'{candidate}' is a reserved Windows name."
        return ""

    def _exception_result(
        self,
        action: str,
        exc: Exception,
        *,
        source_path: str | Path | None = None,
        target_path: str | Path | None = None,
    ) -> FileActionResult:
        message, error = self._translate_exception(exc)
        logger.error("File action failed: %s | source=%s target=%s error=%s", action, source_path, target_path, exc)
        return self._failure(action, source_path=source_path, target_path=target_path, message=message, error=error)

    def _translate_exception(self, exc: Exception) -> tuple[str, str]:
        if isinstance(exc, FileNotFoundError):
            return ("File or folder not found.", "not_found")
        if isinstance(exc, PermissionError):
            winerror = getattr(exc, "winerror", None)
            if winerror == 32:
                return ("The file is currently in use.", "file_in_use")
            if winerror == 5:
                return ("Access denied.", "access_denied")
            return ("Operation blocked by file permissions.", "access_denied")
        if isinstance(exc, shutil.SameFileError):
            return ("Source and destination are the same.", "same_target")
        if isinstance(exc, OSError):
            if getattr(exc, "winerror", None) == 206:
                return ("The path is too long for Windows.", "path_too_long")
            if exc.errno in {errno.ENAMETOOLONG}:
                return ("The path is too long.", "path_too_long")
            if exc.errno in {errno.EINVAL}:
                return ("The file or folder name is invalid.", "invalid_filename")
        return (str(exc) or "File operation failed.", exc.__class__.__name__.lower())

    def _failure(
        self,
        action: str,
        *,
        source_path: str | Path | None = None,
        target_path: str | Path | None = None,
        message: str,
        error: str,
    ) -> FileActionResult:
        return FileActionResult(
            success=False,
            action=action,
            source_path=str(source_path or ""),
            target_path=str(target_path or ""),
            message=message,
            error=error,
        )

    def _location_phrase(self, folder: Path) -> str:
        label = self._resolver.location_label(folder)
        top_level = label.split("\\", 1)[0]
        preposition = "on" if top_level == "Desktop" else "in"
        return f"{preposition} {label}"

    def _looks_like_directory_reference(self, raw_destination: str, resolved_destination: Path) -> bool:
        special = self._resolver.resolve_special_name(raw_destination)
        if special is not None:
            return True
        if raw_destination.endswith(("\\", "/")):
            return True
        if resolved_destination.exists():
            return resolved_destination.is_dir()
        last_part = Path(raw_destination).name
        return "." not in last_part

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        return str(left.resolve(strict=False)).lower() == str(right.resolve(strict=False)).lower()

    @staticmethod
    def _clean_text(value: str | Path) -> str:
        text = str(value).strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            return text[1:-1].strip()
        return text

    @staticmethod
    def _dedupe_paths(paths: list[Path]) -> list[Path]:
        seen: set[str] = set()
        unique: list[Path] = []
        for path in paths:
            key = str(path.resolve(strict=False)).lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(path.resolve(strict=False))
        return unique
