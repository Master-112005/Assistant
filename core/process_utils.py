"""
Low-level Windows process utilities for Phase 13 context detection.

Wraps psutil and optional pywin32 calls behind a clean, exception-safe API.
All functions return None / empty-string rather than raising on access-denied
or process-not-found errors, so callers never need to handle OS errors.

Usage::

    from core.process_utils import get_process_info, pid_to_exe_path

Dependencies:
    psutil          (always required)
    pywin32         (optional â€” enhanced if available)
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

import psutil

from core.logger import get_logger

logger = get_logger(__name__)

# Try to import pywin32 â€” enhanced metadata if available
try:
    import win32process
    import win32api
    _WIN32_AVAILABLE = True
except ImportError:
    _WIN32_AVAILABLE = False
    logger.debug("pywin32 not installed - using psutil-only process introspection")


@dataclass
class ProcessInfo:
    """
    Snapshot of a Windows process's key attributes.

    All string fields default to "" rather than None so callers can safely
    call .lower() without None-checks.
    """
    pid:        int
    name:       str   = ""   # e.g.  "chrome.exe"
    exe_path:   str   = ""   # e.g.  "C:\\Program Files\\Google\\Chrome\\...\\chrome.exe"
    cmdline:    str   = ""   # space-joined command line (may be empty for protected processes)
    status:     str   = ""   # "running", "sleeping", etc.

    @property
    def exe_name(self) -> str:
        """Basename of exe_path, lower-cased. Falls back to name."""
        if self.exe_path:
            return os.path.basename(self.exe_path).lower()
        return self.name.lower()


def get_process_info(pid: int) -> Optional[ProcessInfo]:
    """
    Return a ProcessInfo for *pid*, or None if the process is inaccessible.

    Gracefully handles:
    - psutil.NoSuchProcess     (process already exited)
    - psutil.AccessDenied      (system/protected process)
    - psutil.ZombieProcess     (zombie on Linux â€” shouldn't happen on Windows)
    - Any other unexpected exception
    """
    if pid <= 0:
        return None

    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            name    = _safe(proc.name)
            exe     = _safe(proc.exe)
            status  = _safe(proc.status)
            try:
                cmdline = " ".join(proc.cmdline())
            except (psutil.AccessDenied, OSError):
                cmdline = ""

        return ProcessInfo(
            pid=pid,
            name=name or "",
            exe_path=exe or "",
            cmdline=cmdline,
            status=status or "",
        )

    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        logger.debug("Process %d no longer exists", pid)
        return None
    except psutil.AccessDenied:
        # Process exists but we can't read it (system process, UAC)
        # Return minimal info so callers still know the PID
        logger.debug("Access denied for process %d", pid)
        return ProcessInfo(pid=pid)
    except Exception as exc:
        logger.warning("Unexpected error reading process %d: %s", pid, exc)
        return None


def pid_to_exe_path(pid: int) -> str:
    """
    Return the full executable path for *pid*, empty string on failure.

    Tries pywin32 first (more reliable for protected processes), then psutil.
    """
    if _WIN32_AVAILABLE:
        try:
            handle = win32api.OpenProcess(
                0x1000,  # PROCESS_QUERY_LIMITED_INFORMATION
                False,
                pid,
            )
            if handle:
                path = win32process.GetModuleFileNameEx(handle, 0)
                win32api.CloseHandle(handle)
                return path or ""
        except Exception:
            pass  # Fall through to psutil

    try:
        return psutil.Process(pid).exe() or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe(method):
    """Call a psutil method, return None on failure."""
    try:
        return method()
    except Exception:
        return None

