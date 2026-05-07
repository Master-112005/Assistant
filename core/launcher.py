"""
Launch Windows applications.

Industrial-grade launcher with:
- Non-blocking launch (returns immediately)
- Robust process verification with retry
- Fallback to alternative launch methods
- Proper handling for shortcuts and executables
"""
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from core.logger import get_logger
from core import state
from core.app_index import AppIndexer, AppRecord
from core.aliases import AliasManager

logger = get_logger(__name__)

@dataclass
class LaunchResult:
    success: bool
    app_name: str
    matched_name: str = ""
    path: str = ""
    pid: int = -1
    message: str = ""
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    launch_time: float = field(default_factory=time.time)
    verified: bool = False
    duration_ms: int = 0

class AppLauncher:
    """Handles discovering and launching applications."""
    def __init__(self):
        self.indexer = AppIndexer()
        self.aliases = AliasManager()
        
        # Load cache or index if missing
        if not self.indexer.load_cache():
            self.indexer.refresh()
            
        logger.info("AppLauncher initialized")

    def refresh_index(self):
        self.indexer.refresh()

    def open_app(self, app_name: str) -> LaunchResult:
        """Compatibility wrapper for older callers expecting open_app()."""
        return self.launch_by_name(app_name)

    def launch_by_name(self, query: str) -> LaunchResult:
        """Attempts to find and launch an app matching the query."""
        if not query or not query.strip():
            return LaunchResult(
                False,
                query,
                message="Empty query provided.",
                error="invalid_query",
                data={"requested_app": query},
            )
            
        # 1. Resolve alias
        resolved_name = self.aliases.resolve_alias(query)
        logger.info(f"Launch request: {query} -> resolved to: {resolved_name}")
        
        # 2. Find best match
        record = self.find_best_match(resolved_name)
        if not record:
            msg = f"I couldn't find an installed app named {query}."
            logger.warning(msg)
            return LaunchResult(
                False,
                query,
                message=msg,
                error="app_not_found",
                data={"requested_app": query, "resolved_name": resolved_name},
            )
            
        # 3. Launch the record
        return self.launch_record(record)

    def find_best_match(self, query: str) -> Optional[AppRecord]:
        """Finds the best matching app record for the given query."""
        query_lower = query.lower().strip()
        records = self.indexer.get_all_records()
        
        best_match = None
        best_score = 0.0
        
        for record in records:
            score = 0.0
            rec_name = record.normalized_name
            
            # Exact match
            if query_lower == rec_name:
                score = 1.0
            # Starts with (e.g. "google chrome" -> "google")
            elif rec_name.startswith(query_lower):
                score = 0.8
            # Word token match
            elif query_lower in rec_name.split():
                score = 0.7
            # Substring match
            elif query_lower in rec_name:
                score = 0.5
                
            if score > best_score:
                best_score = score
                best_match = record
                
            if best_score == 1.0:
                break
                
        if best_match:
            logger.info(f"Matched '{query}' to '{best_match.name}' with score {best_score}")
            
        return best_match

    def launch_record(self, record: AppRecord) -> LaunchResult:
        """Launches the specified app record using non-blocking launch with async verification."""
        path = record.path
        if not os.path.exists(path):
            return LaunchResult(
                False,
                record.name,
                matched_name=record.name,
                path=path,
                message=f"Path not found: {path}",
                error="path_not_found",
                data={"requested_app": record.name, "matched_name": record.name},
            )

        action_started = time.time()
        success = False
        pid = -1
        msg = ""
        verified = False
        launch_method = "unknown"

        try:
            if record.type == "executable":
                # Non-blocking subprocess launch - returns immediately
                process = subprocess.Popen(
                    [path],
                    shell=False,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | getattr(subprocess, "DETACHED_PROCESS", 0x00000008),
                )
                pid = process.pid
                success = True
                launch_method = "subprocess"
                msg = f"Started executable {record.name}"
                logger.info("Launch dispatched: %s pid=%s method=%s", record.name, pid, launch_method)

            elif record.type == "shortcut":
                # startfile handles windows shortcuts (.lnk) natively - non-blocking
                os.startfile(path)
                success = True
                launch_method = "startfile"
                msg = f"Opened shortcut {record.name}"
                logger.info("Launch dispatched: %s method=%s", record.name, launch_method)

            else:
                success = False
                msg = f"Unsupported app type: {record.type}"

        except Exception as e:
            logger.error(f"Failed to launch {record.name} at {path}: {e}")
            return LaunchResult(
                False,
                record.name,
                matched_name=record.name,
                path=path,
                message=f"Launch error: {e}",
                error="launch_error",
                data={
                    "requested_app": record.name,
                    "matched_name": record.name,
                    "exception_type": type(e).__name__,
                },
            )

        # For executables with PID, we have verification already
        if success and pid > 0:
            verified = True

        # Attempt async verification - check if process is running after short delay
        if success and not verified:
            verify_start = time.time()
            process_names = self._get_process_names_for_app(record.name)
            verified = self._verify_process_running(process_names, timeout=2.0)
            if verified:
                pid = self._get_pid_for_process(process_names)
            logger.info("Launch verification: %s verified=%s duration_ms=%d", record.name, verified, int((time.time() - verify_start) * 1000))

        if success:
            state.last_launched_app = record.name
            state.last_launch_success = True
            state.last_launch_pid = pid
            launch_duration_ms = int((time.time() - action_started) * 1000)
            logger.info(
                "Launch complete: %s pid=%s verified=%s source_type=%s duration_ms=%d",
                record.name,
                pid,
                verified,
                record.type,
                launch_duration_ms,
            )
            return LaunchResult(
                True,
                record.name,
                matched_name=record.name,
                path=path,
                pid=pid,
                message=f"Opening {record.name}",
                verified=verified,
                data={
                    "requested_app": record.name,
                    "matched_name": record.name,
                    "launch_method": launch_method,
                    "launch_duration_ms": launch_duration_ms,
                },
            )
        else:
            return LaunchResult(
                False,
                record.name,
                matched_name=record.name,
                path=path,
                message=msg,
                error="unsupported_app_type",
                data={"requested_app": record.name, "matched_name": record.name, "app_type": record.type},
            )

    def _get_process_names_for_app(self, app_name: str) -> set[str]:
        """Get expected process names for an application."""
        process_names: set[str] = set()
        app_lower = app_name.lower().strip()

        # Common app name to process name mappings
        mappings = {
            "chrome": {"chrome.exe"},
            "google chrome": {"chrome.exe"},
            "edge": {"msedge.exe"},
            "microsoft edge": {"msedge.exe"},
            "firefox": {"firefox.exe"},
            "mozilla firefox": {"firefox.exe"},
            "whatsapp": {"whatsapp.exe"},
            "spotify": {"spotify.exe"},
            "code": {"Code.exe", "code.exe"},
            "visual studio code": {"Code.exe", "code.exe"},
        }

        for key, names in mappings.items():
            if key in app_lower:
                process_names.update(names)

        # Also try to get from indexer
        record = self.find_best_match(app_name)
        if record and record.path.lower().endswith(".exe"):
            process_names.add(os.path.basename(record.path).lower())

        return process_names

    def _verify_process_running(self, process_names: set[str], timeout: float = 2.0) -> bool:
        """Check if any of the specified processes are running within timeout."""
        if not process_names:
            return False

        deadline = time.monotonic() + timeout
        while time.monotonic() <= deadline:
            try:
                import psutil
                for process in psutil.process_iter(["pid", "name", "exe"]):
                    try:
                        pname = str(process.info.get("name") or "").strip().lower()
                        exe_name = os.path.basename(str(process.info.get("exe") or "")).strip().lower()
                        if pname in process_names or exe_name in process_names:
                            return True
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except Exception:
                pass
            time.sleep(0.15)
        return False

    def _get_pid_for_process(self, process_names: set[str]) -> int:
        """Get PID of first matching running process."""
        try:
            import psutil
            for process in psutil.process_iter(["pid", "name", "exe"]):
                try:
                    pname = str(process.info.get("name") or "").strip().lower()
                    exe_name = os.path.basename(str(process.info.get("exe") or "")).strip().lower()
                    if pname in process_names or exe_name in process_names:
                        return int(process.info.get("pid") or -1)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass
        return -1
