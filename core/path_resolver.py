"""
Windows-friendly path resolution helpers for assistant file commands.
"""
from __future__ import annotations

import ctypes
import os
from collections import deque
from pathlib import Path, PureWindowsPath
from typing import Iterable, Mapping
from uuid import UUID

from core import settings
from core.logger import get_logger

logger = get_logger(__name__)

_SPECIAL_NAME_ALIASES = {
    "desktop": "desktop",
    "desktop folder": "desktop",
    "documents": "documents",
    "document": "documents",
    "docs": "documents",
    "downloads": "downloads",
    "download": "downloads",
    "pictures": "pictures",
    "picture": "pictures",
    "photos": "pictures",
    "photo": "pictures",
    "music": "music",
    "songs": "music",
    "videos": "videos",
    "video": "videos",
}

_DEFAULT_KNOWN_FOLDER_NAMES = {
    "desktop": "Desktop",
    "documents": "Documents",
    "downloads": "Downloads",
    "pictures": "Pictures",
    "music": "Music",
    "videos": "Videos",
}

_KNOWN_FOLDER_GUIDS = {
    "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
}

_DISPLAY_NAMES = {
    "desktop": "Desktop",
    "documents": "Documents",
    "downloads": "Downloads",
    "pictures": "Pictures",
    "music": "Music",
    "videos": "Videos",
}


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, value: str) -> "_GUID":
        parsed = UUID(value)
        data4 = (ctypes.c_ubyte * 8).from_buffer_copy(parsed.bytes[8:])
        return cls(parsed.time_low, parsed.time_mid, parsed.time_hi_version, data4)


class PathResolver:
    """Resolve natural path phrases into normalized filesystem paths."""

    def __init__(
        self,
        *,
        base_dir: str | Path | None = None,
        user_home: str | Path | None = None,
        known_locations: Mapping[str, str | Path] | None = None,
    ) -> None:
        self._base_dir = Path(base_dir or Path.cwd())
        self._user_home = Path(user_home or Path.home())
        self._known_locations = self._load_known_locations(known_locations or {})

    @property
    def known_locations(self) -> dict[str, Path]:
        return dict(self._known_locations)

    def resolve(self, text_path: str | os.PathLike[str]) -> Path:
        raw = self._clean_input(text_path)
        if not raw:
            raise ValueError("Path is empty.")

        expanded = self.expand_env(raw)
        special = self.resolve_special_name(expanded)
        if special is not None:
            return special

        prefixed = self._resolve_prefixed_special_path(expanded)
        if prefixed is not None:
            return prefixed

        return self.ensure_safe(self.normalize(expanded))

    def resolve_special_name(self, name: str | os.PathLike[str]) -> Path | None:
        cleaned = self._normalize_special_key(self._clean_input(name))
        if not cleaned:
            return None

        canonical = _SPECIAL_NAME_ALIASES.get(cleaned)
        if canonical is None:
            return None
        return self._known_locations.get(canonical)

    def expand_env(self, path: str | os.PathLike[str]) -> str:
        return os.path.expanduser(os.path.expandvars(str(path).strip()))

    def normalize(self, path: str | os.PathLike[str]) -> Path:
        text = self._clean_input(path)
        if not text:
            raise ValueError("Path is empty.")

        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = self._base_dir / candidate
        return candidate.resolve(strict=False)

    def ensure_safe(self, path: str | os.PathLike[str] | Path) -> Path:
        normalized = path if isinstance(path, Path) else self.normalize(path)
        text = str(normalized)
        if "\x00" in text:
            raise ValueError("Path contains an invalid null character.")
        return normalized

    def search_common_locations(
        self,
        name: str,
        *,
        preferred_location: str | Path | None = None,
        max_results: int | None = None,
        max_depth: int | None = None,
    ) -> list[Path]:
        reference = self._clean_input(name)
        if not reference:
            return []

        max_results = int(max_results or settings.get("file_search_max_results") or 10)
        max_depth = int(max_depth or settings.get("file_search_max_depth") or 4)
        roots = self._search_roots(preferred_location)
        results: list[Path] = []
        seen: set[str] = set()

        direct_candidates = self._direct_candidates(reference, roots)
        for candidate in direct_candidates:
            if candidate.exists():
                self._append_unique(results, seen, candidate)
                if len(results) >= max_results:
                    return results

        needle = reference.lower()
        needle_stem = Path(reference).stem.lower()
        search_by_stem = "." not in Path(reference).name

        for root in roots:
            if len(results) >= max_results or not root.exists() or not root.is_dir():
                continue

            queue = deque([(root, 0)])
            while queue and len(results) < max_results:
                current, depth = queue.popleft()
                try:
                    with os.scandir(current) as entries:
                        for entry in entries:
                            entry_path = Path(entry.path)
                            entry_name = entry.name.lower()
                            if entry_name == needle or (search_by_stem and entry_path.stem.lower() == needle_stem):
                                self._append_unique(results, seen, entry_path)
                                if len(results) >= max_results:
                                    break
                            if entry.is_dir(follow_symlinks=False) and depth < max_depth:
                                queue.append((entry_path, depth + 1))
                except OSError as exc:
                    logger.debug("Skipping %s during file search: %s", current, exc)
                    continue

        return results

    def describe_path(self, path: str | os.PathLike[str] | Path) -> str:
        normalized = path if isinstance(path, Path) else self.normalize(path)
        for key, root in self._known_locations.items():
            try:
                relative = normalized.relative_to(root)
            except ValueError:
                continue
            display_root = _DISPLAY_NAMES.get(key, root.name)
            if not relative.parts:
                return display_root
            return str(PureWindowsPath(display_root, *relative.parts))
        return str(normalized)

    def location_label(self, path: str | os.PathLike[str] | Path) -> str:
        normalized = path if isinstance(path, Path) else self.normalize(path)
        return self.describe_path(normalized)

    def is_in_user_scope(self, path: str | os.PathLike[str] | Path) -> bool:
        normalized = path if isinstance(path, Path) else self.normalize(path)
        roots = [self._user_home.resolve(strict=False), self._base_dir.resolve(strict=False)]
        return any(self._is_relative_to(normalized, root) for root in roots)

    def is_system_directory(self, path: str | os.PathLike[str] | Path) -> bool:
        normalized = path if isinstance(path, Path) else self.normalize(path)
        windows_root = Path(os.environ.get("WINDIR") or "C:\\Windows").resolve(strict=False)
        program_files = [
            Path(os.environ.get("ProgramFiles") or "C:\\Program Files").resolve(strict=False),
            Path(os.environ.get("ProgramFiles(x86)") or "C:\\Program Files (x86)").resolve(strict=False),
            Path(os.environ.get("ProgramData") or "C:\\ProgramData").resolve(strict=False),
        ]
        return any(self._is_relative_to(normalized, root) for root in [windows_root, *program_files])

    def _load_known_locations(self, overrides: Mapping[str, str | Path]) -> dict[str, Path]:
        known: dict[str, Path] = {}
        for key, folder_name in _DEFAULT_KNOWN_FOLDER_NAMES.items():
            override = overrides.get(key)
            if override is not None:
                known[key] = Path(override).resolve(strict=False)
                continue
            resolved = self._known_folder_from_windows_api(key)
            if resolved is None:
                resolved = (self._user_home / folder_name).resolve(strict=False)
            known[key] = resolved
        return known

    def _known_folder_from_windows_api(self, key: str) -> Path | None:
        if os.name != "nt":
            return None
        guid_value = _KNOWN_FOLDER_GUIDS.get(key)
        if not guid_value:
            return None
        try:
            folder_id = _GUID.from_string(guid_value)
            result_path = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            ole32 = ctypes.windll.ole32
            shell32.SHGetKnownFolderPath.argtypes = [
                ctypes.POINTER(_GUID),
                ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_wchar_p),
            ]
            shell32.SHGetKnownFolderPath.restype = ctypes.c_long
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            outcome = shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(result_path))
            if outcome != 0 or not result_path.value:
                return None
            resolved = Path(result_path.value).resolve(strict=False)
            ole32.CoTaskMemFree(ctypes.cast(result_path, ctypes.c_void_p))
            return resolved
        except Exception as exc:
            logger.debug("Known-folder lookup failed for %s: %s", key, exc)
            return None

    def _resolve_prefixed_special_path(self, text: str) -> Path | None:
        separators = text.replace("/", "\\").split("\\")
        if not separators:
            return None
        key = self._normalize_special_key(separators[0])
        canonical = _SPECIAL_NAME_ALIASES.get(key)
        if canonical is None:
            return None
        root = self._known_locations.get(canonical)
        if root is None:
            return None
        remainder = [part for part in separators[1:] if part]
        return self.ensure_safe((root / Path(*remainder)).resolve(strict=False))

    def _search_roots(self, preferred_location: str | Path | None) -> list[Path]:
        roots: list[Path] = []
        if preferred_location:
            try:
                preferred = self.resolve(preferred_location)
            except Exception:
                preferred = None
            if preferred is not None:
                roots.append(preferred)

        if settings.get("search_common_folders"):
            roots.extend(self._known_locations.values())

        roots.append(self._base_dir.resolve(strict=False))
        return self._dedupe_paths(roots)

    def _direct_candidates(self, reference: str, roots: Iterable[Path]) -> list[Path]:
        candidates: list[Path] = []
        for root in roots:
            candidates.append((root / reference).resolve(strict=False))
            if "." not in Path(reference).name:
                for child in root.glob(f"{reference}.*"):
                    candidates.append(child.resolve(strict=False))
        return self._dedupe_paths(candidates)

    @staticmethod
    def _append_unique(results: list[Path], seen: set[str], candidate: Path) -> None:
        key = str(candidate.resolve(strict=False)).lower()
        if key in seen:
            return
        seen.add(key)
        results.append(candidate.resolve(strict=False))

    @staticmethod
    def _clean_input(value: str | os.PathLike[str]) -> str:
        text = str(value).strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            return text[1:-1].strip()
        return text

    @staticmethod
    def _normalize_special_key(value: str) -> str:
        return " ".join(value.strip().lower().split())

    @staticmethod
    def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
        seen: set[str] = set()
        deduped: list[Path] = []
        for path in paths:
            key = str(path.resolve(strict=False)).lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path.resolve(strict=False))
        return deduped

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
