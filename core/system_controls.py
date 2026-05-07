"""
Industrial-grade Windows system control engine.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core import settings, state
from core.logger import get_logger

logger = get_logger(__name__)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
VOLUME_TOLERANCE = 3
BRIGHTNESS_TOLERANCE = 5

_DELAY_FACTOR = 0.3


def _fast_delay(seconds: float) -> None:
    """Optimized delay with configurable factor for performance."""
    delay = max(0.05, seconds * _DELAY_FACTOR)
    time.sleep(delay)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(text: str) -> str:
    lowered = str(text or "").strip().lower()
    lowered = lowered.replace("wi-fi", "wifi")
    lowered = re.sub(r"\bbrightnedd\b", "brightness", lowered)
    lowered = re.sub(r"\bbrightnes\b", "brightness", lowered)
    lowered = re.sub(r"\bbrighness\b", "brightness", lowered)
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered


def _clamp_percent(value: int) -> int:
    return max(0, min(100, int(value)))


def _parse_int(fragment: str | None) -> int | None:
    if fragment in (None, ""):
        return None
    try:
        return int(str(fragment).strip())
    except (TypeError, ValueError):
        return None


def _format_delay(delay_seconds: int) -> str:
    if delay_seconds <= 0:
        return "now"
    if delay_seconds % 3600 == 0:
        hours = delay_seconds // 3600
        return f"in {hours} hour{'s' if hours != 1 else ''}"
    if delay_seconds % 60 == 0:
        minutes = delay_seconds // 60
        return f"in {minutes} minute{'s' if minutes != 1 else ''}"
    return f"in {delay_seconds} second{'s' if delay_seconds != 1 else ''}"


def _validate_percent(value: int | None) -> tuple[bool, int | None, str]:
    if value is None:
        return False, None, "A value from 0 to 100 is required."
    if value < 0 or value > 100:
        return False, None, "Value must be between 0 and 100."
    return True, int(value), ""


def _parse_delay_seconds(text: str) -> int | None:
    match = re.search(
        r"\b(?:in|after)\s+(\d+)\s*(second|seconds|sec|secs|minute|minutes|min|mins|hour|hours|hr|hrs)\b",
        text,
    )
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)
    if unit.startswith(("hour", "hr")):
        return amount * 3600
    if unit.startswith(("minute", "min")):
        return amount * 60
    return amount


@dataclass(slots=True)
class SystemActionResult:
    success: bool
    action: str
    previous_state: dict[str, Any]
    current_state: dict[str, Any]
    message: str
    error: str = ""
    timestamp: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "action": self.action,
            "previous_state": dict(self.previous_state),
            "current_state": dict(self.current_state),
            "message": self.message,
            "error": self.error,
            "timestamp": self.timestamp,
        }


@dataclass(slots=True)
class ParsedSystemCommand:
    action: str
    control: str
    direction: str
    value: int | None = None
    delay_seconds: int = 0
    raw_text: str = ""

    def to_entities(self) -> dict[str, Any]:
        payload = {
            "action": self.action,
            "control": self.control,
            "direction": self.direction,
        }
        if self.value is not None:
            payload["value"] = self.value
        if self.delay_seconds:
            payload["delay_seconds"] = self.delay_seconds
        return payload


class BackendError(RuntimeError):
    """Raised when a system-control backend is unavailable or fails."""


@dataclass(slots=True)
class CommandExecution:
    success: bool
    stdout: str
    stderr: str
    returncode: int

    @property
    def combined_output(self) -> str:
        return self.stderr.strip() or self.stdout.strip()


class CommandRunner:
    def run(self, args: list[str], *, timeout: float = 10) -> CommandExecution:
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            return CommandExecution(
                success=completed.returncode == 0,
                stdout=(completed.stdout or "").strip(),
                stderr=(completed.stderr or "").strip(),
                returncode=completed.returncode,
            )
        except FileNotFoundError as exc:
            return CommandExecution(False, "", str(exc), 127)
        except subprocess.TimeoutExpired as exc:
            return CommandExecution(
                False,
                (exc.stdout or "").strip() if isinstance(exc.stdout, str) else "",
                (exc.stderr or "").strip() if isinstance(exc.stderr, str) else "Command timed out.",
                -1,
            )

    def run_powershell(self, script: str, *, timeout: float = 15) -> CommandExecution:
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        return self.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            timeout=timeout,
        )


class VolumeBackend:
    """Core Audio endpoint volume backend backed by pycaw."""

    def __init__(self) -> None:
        self._endpoint = None
        self._loaded = False

    def _get_endpoint(self):
        if self._loaded and self._endpoint is not None:
            return self._endpoint
        try:
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        except ImportError as exc:
            raise BackendError("Volume control requires the 'pycaw' package.") from exc

        try:
            device = AudioUtilities.GetSpeakers()
            self._endpoint = device.EndpointVolume
            self._loaded = True
            return self._endpoint
        except Exception as exc:
            raise BackendError(f"Failed to access the default audio endpoint: {exc}") from exc

    def get_state(self) -> dict[str, Any]:
        endpoint = self._get_endpoint()
        try:
            percent = int(round(float(endpoint.GetMasterVolumeLevelScalar()) * 100))
            muted = bool(endpoint.GetMute())
        except Exception as exc:
            raise BackendError(f"Failed to read system volume: {exc}") from exc
        return {
            "available": True,
            "backend": "pycaw",
            "percent": _clamp_percent(percent),
            "muted": muted,
        }

    def set_percent(self, percent: int) -> None:
        endpoint = self._get_endpoint()
        try:
            endpoint.SetMasterVolumeLevelScalar(float(percent) / 100.0, None)
        except Exception as exc:
            raise BackendError(f"Failed to set system volume: {exc}") from exc

    def set_mute(self, muted: bool) -> None:
        endpoint = self._get_endpoint()
        try:
            endpoint.SetMute(bool(muted), None)
        except Exception as exc:
            raise BackendError(f"Failed to change mute state: {exc}") from exc


class BrightnessBackend:
    """Brightness backend with library-first and WMI fallback paths."""

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def get_state(self) -> dict[str, Any]:
        try:
            return self._get_with_library()
        except BackendError as library_error:
            logger.debug("Brightness library backend unavailable: %s", library_error)

        try:
            return self._get_with_wmi()
        except BackendError as wmi_error:
            raise BackendError(
                f"Screen brightness control is unavailable on this device. {wmi_error}"
            ) from wmi_error

    def set_percent(self, percent: int) -> dict[str, Any]:
        try:
            self._set_with_library(percent)
            return self._get_with_library()
        except BackendError as library_error:
            logger.debug("Brightness library set failed: %s", library_error)

        try:
            self._set_with_wmi(percent)
            return self._get_with_wmi()
        except BackendError as wmi_error:
            raise BackendError(
                f"Unable to change screen brightness on this device. {wmi_error}"
            ) from wmi_error

    def _get_with_library(self) -> dict[str, Any]:
        try:
            import screen_brightness_control as sbc
        except ImportError as exc:
            raise BackendError("The 'screen_brightness_control' package is not installed.") from exc

        try:
            level = sbc.get_brightness(display=0)
        except Exception as exc:
            raise BackendError(str(exc)) from exc

        if isinstance(level, list):
            if not level:
                raise BackendError("The brightness backend returned no displays.")
            level = level[0]

        return {
            "supported": True,
            "backend": "screen_brightness_control",
            "percent": _clamp_percent(int(round(float(level)))),
        }

    def _set_with_library(self, percent: int) -> None:
        try:
            import screen_brightness_control as sbc
        except ImportError as exc:
            raise BackendError("The 'screen_brightness_control' package is not installed.") from exc

        try:
            sbc.set_brightness(percent, display=0)
            time.sleep(0.25)
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    def _get_with_wmi(self) -> dict[str, Any]:
        script = """
$monitor = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness | Select-Object -First 1
if (-not $monitor) {
    throw "No WMI brightness monitor was found."
}
[pscustomobject]@{
    supported = $true
    backend = "wmi"
    percent = [int]$monitor.CurrentBrightness
} | ConvertTo-Json -Compress
"""
        payload = _load_json(self._runner.run_powershell(script), action="get brightness")
        return {
            "supported": bool(payload.get("supported", True)),
            "backend": str(payload.get("backend") or "wmi"),
            "percent": _clamp_percent(int(payload.get("percent", 0))),
        }

    def _set_with_wmi(self, percent: int) -> None:
        script = f"""
$methods = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | Select-Object -First 1
if (-not $methods) {{
    throw "No WMI brightness method was found."
}}
Invoke-CimMethod -InputObject $methods -MethodName WmiSetBrightness -Arguments @{{ Timeout = 1; Brightness = {percent} }} | Out-Null
Start-Sleep -Milliseconds 250
"""
        execution = self._runner.run_powershell(script)
        if not execution.success:
            raise BackendError(_classify_command_error(execution, "Unable to change brightness"))


class WifiBackend:
    """Wi-Fi adapter backend built on Get-NetAdapter with netsh metadata."""

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def get_state(self) -> dict[str, Any]:
        adapters = self._list_adapters()
        if not adapters:
            raise BackendError("No Wi-Fi adapter was found on this PC.")

        connection = self._connection_details()
        any_enabled = any(adapter["admin_enabled"] for adapter in adapters)
        any_connected = any(adapter["status"].lower() == "up" for adapter in adapters)
        status = "on" if any_enabled else "off"
        return {
            "supported": True,
            "state": status,
            "connected": any_connected,
            "ssid": connection.get("ssid", ""),
            "connection_state": connection.get("state", ""),
            "adapters": adapters,
        }

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        # First try direct toggle
        try:
            return self._direct_toggle(enabled)
        except BackendError:
            pass
        
        # Try elevated toggle
        return self._try_elevated_toggle(enabled)
    
    def _direct_toggle(self, enabled: bool) -> dict[str, Any]:
        adapters = self._list_adapters()
        if not adapters:
            raise BackendError("No Wi-Fi adapter was found on this PC.")

        action = "Enable-NetAdapter" if enabled else "Disable-NetAdapter"
        names = ", ".join(_ps_single_quote(adapter["name"]) for adapter in adapters)
        script = f"""
$adapters = @({names})
foreach ($name in $adapters) {{
    {action} -Name $name -Confirm:$false -ErrorAction Stop | Out-Null
}}
"""
        execution = self._runner.run_powershell(script, timeout=20)
        if not execution.success:
            raise BackendError(_classify_command_error(execution, "Unable to change Wi-Fi state"))
        _fast_delay(1.0)
        return self.get_state()
    
    def _try_elevated_toggle(self, enabled: bool) -> dict[str, Any]:
        import ctypes
        import subprocess
        import webbrowser
        
        target_state = "on" if enabled else "off"
        
        # Check admin
        is_admin = False
        try:
            is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        except:
            pass
        
        # Try direct if admin
        if is_admin:
            try:
                return self._direct_toggle(enabled)
            except:
                pass
        
        # Try netsh
        try:
            action = "enable" if enabled else "disable"
            result = subprocess.run(
                ["netsh", "wlan", "connect" if enabled else "disconnect", "name=Secure Network"],
                capture_output=True, timeout=10
            )
            # For disconnect, trying alternative
            if not enabled:
                result2 = subprocess.run(
                    ["netsh", "wlan", "disconnect"],
                    capture_output=True, timeout=10
                )
        except:
            pass
        
        # Try ShellExecute RunAs
        try:
            action = "Enable-NetAdapter" if enabled else "Disable-NetAdapter"
            script = f"""$adapter = Get-NetAdapter -Physical | Where-Object {{$_.Name -match 'Wi-Fi'}} | Select-Object -First 1; if ($adapter) {{ {action} -Name $adapter.Name -Confirm:$false }}"""
            
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.ps1', delete=False) as f:
                f.write(script)
                sp = f.name
            
            result = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", f"-ExecutionPolicy Bypass -File \"{sp}\"", None, 1)
            _fast_delay(2.0)
            
            state = self.get_state()
            if (enabled and state["state"] == "on") or (not enabled and state["state"] == "off"):
                return state
        except:
            pass
        
        # Open Wi-Fi Settings as fallback
        try:
            webbrowser.open("ms-settings:network-wifi")
        except:
            subprocess.Popen(["start", "ms-settings:network-wifi"], shell=True)
        
        time.sleep(1)
        state = self.get_state()
        
        if (enabled and state["state"] == "on") or (not enabled and state["state"] == "off"):
            return state
        
        return {
            "supported": True,
            "state": state["state"],
            "adapters": state.get("adapters", []),
            "connected": state.get("connected", False),
            "ssid": state.get("ssid", ""),
            "needs_manual": True,
            "message": f"Wi-Fi is {state['state']}. Opened Settings. Please toggle manually."
        }

    def _list_adapters(self) -> list[dict[str, Any]]:
        script = """
$adapters = Get-NetAdapter -Physical | Where-Object {
    $_.NdisPhysicalMedium -eq 9 -or
    $_.InterfaceDescription -match 'Wireless|Wi-Fi|802\\.11|WLAN' -or
    $_.Name -match 'Wi-?Fi|WLAN'
} | Sort-Object Name

$payload = @()
foreach ($adapter in $adapters) {
    $payload += [pscustomobject]@{
        name = [string]$adapter.Name
        status = [string]$adapter.Status
        admin_enabled = [bool]($adapter.InterfaceAdminStatus -eq 1 -or [string]$adapter.InterfaceAdminStatus -eq 'Up')
        interface_description = [string]$adapter.InterfaceDescription
    }
}
$payload | ConvertTo-Json -Compress
"""
        payload = _load_json(self._runner.run_powershell(script), action="query Wi-Fi adapters", allow_empty=True)
        if not payload:
            return []
        if isinstance(payload, dict):
            payload = [payload]
        return [
            {
                "name": str(item.get("name") or ""),
                "status": str(item.get("status") or "Unknown"),
                "admin_enabled": bool(item.get("admin_enabled")),
                "interface_description": str(item.get("interface_description") or ""),
            }
            for item in payload
        ]

    def _connection_details(self) -> dict[str, str]:
        execution = self._runner.run(["netsh", "wlan", "show", "interfaces"], timeout=10)
        if not execution.success or not execution.stdout:
            return {}

        state_match = re.search(r"^\s*State\s*:\s*(.+)$", execution.stdout, flags=re.MULTILINE)
        ssid_match = re.search(r"^\s*SSID\s*:\s*(.+)$", execution.stdout, flags=re.MULTILINE)
        return {
            "state": state_match.group(1).strip() if state_match else "",
            "ssid": ssid_match.group(1).strip() if ssid_match else "",
        }


class BluetoothBackend:
    """Bluetooth backend backed by PnP adapter enable/disable operations."""

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def get_state(self) -> dict[str, Any]:
        adapters = self._list_adapters()
        if not adapters:
            raise BackendError("No Bluetooth radio adapter was found on this PC.")

        any_ok = any(adapter["status"].lower() == "ok" for adapter in adapters)
        return {
            "supported": True,
            "state": "on" if any_ok else "off",
"adapters": adapters,
        }

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        is_admin = False
        try:
            import ctypes
            is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            pass
        
        if is_admin:
            try:
                return self._set_with_radio_api(enabled)
            except BackendError as e:
                try:
                    return self._set_with_pnP(enabled)
                except BackendError:
                    raise BackendError(f"Bluetooth: {e}") from e
        else:
            # Not admin - try to get elevation automatically
            return self._try_elevated_toggle(enabled)

    def _set_with_radio_api(self, enabled: bool) -> dict[str, Any]:
        target_state = "on" if enabled else "off"
        
        is_admin = False
        try:
            import ctypes
            is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            pass
        
        # Try with elevated PowerShell if not admin
        if not is_admin:
            import subprocess
            import os
            script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bt_toggle.ps1")
            if os.path.exists(script_path):
                try:
                    args = ["powershell", "-ExecutionPolicy", "Bypass", "-File", script_path, "-Enable" if enabled else "-Enable", str(enabled).lower()]
                    result = subprocess.run(
                        args,
                        capture_output=True,
                        timeout=30,
                        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
                    )
                    # Read result from temp file
                    import tempfile
                    result_file = os.path.join(tempfile.gettempdir(), "nova_bt_result.txt")
                    if os.path.exists(result_file):
                        with open(result_file, "r") as f:
                            content = f.read()
                            try:
                                result_data = json.loads(content)
                                os.remove(result_file)
                                if result_data.get("success"):
                                    return {"supported": True, "state": result_data.get("state", target_state), "adapters": [{"name": result_data.get("adapter", "Bluetooth"), "status": result_data.get("state", target_state)}]}
                            except:
                                pass
                except Exception as e:
                    logger.debug("Elevated toggle failed: %s", e)
        
        # Fallback: try with current privileges
        error_action = "Stop" if is_admin else "SilentlyContinue"
        
        script = f"""
$device = Get-PnpDevice -Class Bluetooth | Where-Object {{
    $_.FriendlyName -match 'adapter|radio' -and $_.FriendlyName -notmatch 'enumerator|profile|service|device information'
}} | Select-Object -First 1

if (-not $device) {{ throw "No Bluetooth adapter found" }}

$currentOk = $device.Status -eq 'OK'
$wantedOn = '{target_state}' -eq 'on'

if ($currentOk -eq $wantedOn) {{
    @{{ state = '{target_state}'; adapter = $device.FriendlyName; success = $true }} | ConvertTo-Json -Compress
    exit
}}

$cmd = if ($wantedOn) {{ 'Enable-PnpDevice' }} else {{ 'Disable-PnpDevice' }}
& $cmd -InstanceId $device.InstanceId -Confirm:$false -ErrorAction {error_action}

Start-Sleep -Milliseconds 800

try {{
    $newDevice = Get-PnpDevice | Where-Object {{ $_.InstanceId -eq $device.InstanceId }}
    $okAfter = if ($newDevice) {{ $newDevice.Status -eq 'OK' }} else {{ $false }}
    $success = $wantedOn -eq $okAfter
}} catch {{
    $okAfter = $false
    $success = $false
}}

@{{ state = if ($okAfter) {{ 'on' }} else {{ 'off' }}; adapter = $device.FriendlyName; success = $success }} | ConvertTo-Json -Compress
"""
        
        runner = self._runner
        execution = runner.run_powershell(script, timeout=30)
        
        if execution.success and execution.stdout.strip():
            try:
                result = json.loads(execution.stdout)
                if result.get("success"):
                    return {"supported": True, "state": result.get("state", target_state), "adapters": [{"name": result.get("adapter", "Bluetooth"), "status": result.get("state", target_state)}]}
            except:
                pass
        
        # Try netsh fallback
        action = "enable" if enabled else "disable"
        netsh_exec = runner.run(["netsh", "bluetooth", "set", "admin", action], timeout=10)
        if netsh_exec.success:
            _fast_delay(1.0)
            return self.get_state()
        
        raise BackendError("Bluetooth: Please right-click and Run as Administrator to toggle automatically.")

    def _set_with_pnP(self, enabled: bool) -> dict[str, Any]:
        adapters = self._list_adapters()
        if not adapters:
            raise BackendError("No Bluetooth adapter was found on this PC.")

        instance_ids = [adapter["instance_id"] for adapter in adapters if adapter["instance_id"]]
        if not instance_ids:
            raise BackendError("Bluetooth adapters were found, but none exposed a usable instance ID.")

        command = "Enable-PnpDevice" if enabled else "Disable-PnpDevice"
        ids = ", ".join(_ps_single_quote(instance_id) for instance_id in instance_ids)
        script = f"""
$ids = @({ids})
foreach ($id in $ids) {{
    {command} -InstanceId $id -Confirm:$false -ErrorAction Stop | Out-Null
}}
"""
        execution = self._runner.run_powershell(script, timeout=20)
        if not execution.success:
            raise BackendError(_classify_command_error(execution, "Unable to change Bluetooth state"))
        _fast_delay(1.0)
        return self._query_specific(instance_ids)

    def _try_elevated_toggle(self, enabled: bool) -> dict[str, Any]:
        """Try multiple methods - open Settings as last resort"""
        import os
        import subprocess
        import tempfile
        import ctypes
        import webbrowser
        
        target_state = "on" if enabled else "off"
        
        # Check admin
        is_admin = False
        try:
            is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        except:
            pass
        
        # Method 1: Direct toggle (if admin)  
        if is_admin:
            try:
                return self._direct_toggle(enabled)
            except:
                pass
        
        # Method 2: Try netsh
        try:
            action = "enable" if enabled else "disable"
            result = subprocess.run(
                ["netsh", "bluetooth", "set", "admin", action],
                capture_output=True, timeout=15
            )
            if result.returncode == 0:
                time.sleep(1.5)
                state = self.get_state()
                if (enabled and state["state"] == "on") or (not enabled and state["state"] == "off"):
                    return state
        except:
            pass
        
        # Method 3: ShellExecute RunAs
        try:
            ps_cmd = "Enable-PnpDevice" if enabled else "Disable-PnpDevice"
            script = f"""
$device = Get-PnpDevice -Class Bluetooth | Where-Object {{
    $_.FriendlyName -match 'adapter|radio' -and $_.FriendlyName -notmatch 'enumerator|profile|service'
}} | Select -First 1
if ($device) {{ {ps_cmd} -InstanceId $device.InstanceId -Confirm:$false -ErrorAction SilentlyContinue }}
"""
            with tempfile.NamedTemporaryFile(mode='w', suffix='.ps1', delete=False) as f:
                f.write(script)
                sp = f.name
            
            result = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", f"-ExecutionPolicy Bypass -File \"{sp}\"", None, 1)
            _fast_delay(2.0)
            
            state = self.get_state()
            if (enabled and state["state"] == "on") or (not enabled and state["state"] == "off"):
                return state
        except:
            pass
        
        # Method 4: Open Windows Bluetooth Settings (fallback)
        try:
            webbrowser.open("ms-settings:bluetooth")
        except:
            subprocess.Popen(["start", "ms-settings:bluetooth"], shell=True)
        
        # Check if toggle worked despite everything
        time.sleep(1)
        state = self.get_state()
        
        if (enabled and state["state"] == "on") or (not enabled and state["state"] == "off"):
            return state
        
        # Return current state with info about manual toggle needed
        return {
            "supported": True,
            "state": state["state"],
            "adapters": state.get("adapters", []),
            "needs_manual": True,
            "message": f"Bluetooth is {state['state']}. Opened Settings. Please toggle manually."
        }

    def _direct_toggle(self, enabled: bool) -> dict[str, Any]:
        import subprocess
        import tempfile
        import os
        
        target = "on" if enabled else "off"
        cmd = "Enable-PnpDevice" if enabled else "Disable-PnpDevice"
        
        script = f"""
$device = Get-PnpDevice -Class Bluetooth | Where-Object {{
    $_.FriendlyName -match 'adapter|radio' -and $_.FriendlyName -notmatch 'enumerator'
}} | Select-Object -First 1
if ($device) {{ {cmd} -InstanceId $device.InstanceId -Confirm:$false }}
"""
        path = tempfile.mktemp(suffix='.ps1')
        with open(path, 'w') as f:
            f.write(script)
        
        try:
            subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", path], 
                       capture_output=True, timeout=20)
            time.sleep(1)
            return self.get_state()
        finally:
            try: os.unlink(path)
            except: pass
    
    def _another_missing_method(self):
        pass
        
        # Method 3: Try ShellExecute RunAs
        try:
            ps_cmd = "Enable-PnpDevice" if enabled else "Disable-PnpDevice"
            
            script = f"""
$device = Get-PnpDevice -Class Bluetooth | Where-Object {{
    $_.FriendlyName -match 'adapter|radio' -and $_.FriendlyName -notmatch 'enumerator|profile|service|device information'
}} | Select-Object -First 1
if ($device) {{
    {ps_cmd} -InstanceId $device.InstanceId -Confirm:$false -ErrorAction Stop
}}
"""
            with tempfile.NamedTemporaryFile(mode='w', suffix='.ps1', delete=False, encoding='utf-8') as f:
                f.write(script)
                script_path = f.name
            
            # Use ShellExecute to run elevated
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "powershell.exe", 
                f"-ExecutionPolicy Bypass -File \"{script_path}\"", 
                None, 1
            )
            
            _fast_delay(2.0)
            
            # Check result
            state = self.get_state()
            current = state.get("state", "unknown")
            success = (enabled and current == "on") or (not enabled and current == "off")
            
            try:
                os.unlink(script_path)
            except:
                pass
            
            if success:
                return state
            
            # ShellExecute failed/denied - try UAC prompt
            return self._request_elevation(enabled)
            
        except Exception as e:
            logger.debug("ShellExecute failed: %s", e)
        
        # Ultimate fallback: ask user to run as admin
        return self._request_elevation(enabled)

    def _direct_toggle(self, enabled: bool) -> dict[str, Any]:
        """Direct toggle with admin privileges"""
        import subprocess
        import tempfile
        import os
        
        target_state = "on" if enabled else "off"
        ps_cmd = "Enable-PnpDevice" if enabled else "Disable-PnpDevice"
        
        script = f"""
$device = Get-PnpDevice -Class Bluetooth | Where-Object {{
    $_.FriendlyName -match 'adapter|radio' -and $_.FriendlyName -notmatch 'enumerator|profile|service|device information'
}} | Select-Object -First 1
if ($device) {{
    {ps_cmd} -InstanceId $device.InstanceId -Confirm:$false -ErrorAction Stop
    Start-Sleep -Milliseconds 800
}}
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.ps1', delete=False, encoding='utf-8') as f:
            f.write(script)
            script_path = f.name
        
        try:
            result = subprocess.run(
                ["powershell", "-ExecutionPolicy", "Bypass", "-File", script_path],
                capture_output=True,
                timeout=20
            )
            time.sleep(1)
            return self.get_state()
        finally:
            try:
                os.unlink(script_path)
            except:
                pass
    
    def _request_elevation(self, enabled: bool) -> dict[str, Any]:
        """Request elevation via UAC prompt"""
        import ctypes
        import os
        import subprocess
        import tempfile
        import sys
        
        target_state = "on" if enabled else "off"
        
        # Create a temp script that will run elevated
        script_template = """
$device = Get-PnpDevice -Class Bluetooth | Where-Object {{
    $_.FriendlyName -match 'adapter|radio' -and $_.FriendlyName -notmatch 'enumerator|profile|service|device information'
}} | Select-Object -First 1
if ($device) {{
    {cmd} -InstanceId $device.InstanceId -Confirm:$false -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 1000
}}
"""
        ps_cmd = "Enable-PnpDevice" if enabled else "Disable-PnpDevice"
        
        # Write temp script in same dir as app
        script_content = script_template.format(cmd=ps_cmd)
        
        script_dir = os.path.dirname(os.path.abspath(__file__))
        script_path = os.path.join(script_dir, "bt_toggle.ps1")
        
        try:
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script_content)
        except:
            # Fallback to temp dir
            with tempfile.NamedTemporaryFile(mode='w', suffix='.ps1', delete=False, encoding='utf-8') as f:
                f.write(script_content)
                script_path = f.name
        
        # Try to spawn elevated PowerShell
        try:
            # Method 1: ShellExecute with runas
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "powershell.exe",
                f"-ExecutionPolicy Bypass -File \"{script_path}\"",
                None, 1
            )
            
            # result > 32 means success
            if result > 32:
                _fast_delay(2.0)
                # Clean up script
                try:
                    os.unlink(script_path)
                except:
                    pass
                return self.get_state()
        except Exception as e:
            logger.debug("ShellExecute error: %s", e)
        
        # Final fallback - provide clear instructions
        raise BackendError(
            f"Bluetooth control requires Administrator privileges. "
            "Run launcher as Administrator or click 'Yes' on UAC prompt."
        )

    def _list_adapters(self) -> list[dict[str, Any]]:
        script = """
$devices = Get-PnpDevice -Class Bluetooth | Where-Object {
    $friendly = [string]$_.FriendlyName
    $instance = [string]$_.InstanceId
    $friendly -match 'adapter|radio' -and $friendly -notmatch 'enumerator|profile|service|transport|device information|generic attribute|generic access'
} | Sort-Object FriendlyName

$payload = @()
foreach ($device in $devices) {
    $payload += [pscustomobject]@{
        friendly_name = [string]$device.FriendlyName
        instance_id = [string]$device.InstanceId
        status = [string]$device.Status
    }
}
$payload | ConvertTo-Json -Compress
"""
        payload = _load_json(self._runner.run_powershell(script), action="query Bluetooth adapters", allow_empty=True)
        if not payload:
            return []
        if isinstance(payload, dict):
            payload = [payload]
        return [
            {
                "friendly_name": str(item.get("friendly_name") or ""),
                "instance_id": str(item.get("instance_id") or ""),
                "status": str(item.get("status") or "Unknown"),
            }
            for item in payload
        ]

    def _query_specific(self, instance_ids: list[str]) -> dict[str, Any]:
        ids = ", ".join(_ps_single_quote(instance_id) for instance_id in instance_ids)
        script = f"""
$devices = Get-PnpDevice -InstanceId @({ids}) -ErrorAction SilentlyContinue | Sort-Object FriendlyName
$payload = @()
foreach ($device in $devices) {{
    $payload += [pscustomobject]@{{
        friendly_name = [string]$device.FriendlyName
        instance_id = [string]$device.InstanceId
        status = [string]$device.Status
    }}
}}
$payload | ConvertTo-Json -Compress
"""
        payload = _load_json(self._runner.run_powershell(script), action="verify Bluetooth adapters", allow_empty=True)
        if not payload:
            raise BackendError("Bluetooth adapters disappeared after the state change.")
        if isinstance(payload, dict):
            payload = [payload]
        adapters = [
            {
                "friendly_name": str(item.get("friendly_name") or ""),
                "instance_id": str(item.get("instance_id") or ""),
                "status": str(item.get("status") or "Unknown"),
            }
            for item in payload
        ]
        return {
            "supported": True,
            "state": "on" if any(adapter["status"].lower() == "ok" for adapter in adapters) else "off",
            "adapters": adapters,
        }


class PowerBackend:
    """Power backend for lock, shutdown, restart, and shutdown cancellation."""

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def lock(self) -> None:
        try:
            import ctypes

            result = ctypes.windll.user32.LockWorkStation()
            if result:
                return
            error = ctypes.WinError()
            raise BackendError(str(error))
        except Exception as exc:
            logger.debug("LockWorkStation failed, falling back to rundll32: %s", exc)

        execution = self._runner.run(["rundll32.exe", "user32.dll,LockWorkStation"], timeout=10)
        if not execution.success:
            raise BackendError(_classify_command_error(execution, "Unable to lock this PC"))

    def shutdown(self, *, delay_seconds: int) -> None:
        self._schedule_power_action("shutdown", delay_seconds=delay_seconds)

    def restart(self, *, delay_seconds: int) -> None:
        self._schedule_power_action("restart", delay_seconds=delay_seconds)

    def cancel_shutdown(self) -> None:
        execution = self._runner.run(["shutdown", "/a"], timeout=10)
        if not execution.success:
            raise BackendError(_classify_command_error(execution, "Unable to cancel shutdown or restart"))

    def _schedule_power_action(self, action: str, *, delay_seconds: int) -> None:
        if delay_seconds < 0:
            raise BackendError("Delay must be zero or a positive number of seconds.")
        flag = "/s" if action == "shutdown" else "/r"
        execution = self._runner.run(
            [
                "shutdown",
                flag,
                "/t",
                str(int(delay_seconds)),
                "/c",
                f"Nova Assistant requested {action}.",
            ],
            timeout=10,
        )
        if not execution.success:
            raise BackendError(_classify_command_error(execution, f"Unable to {action} the PC"))


def _ps_single_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _load_json(
    execution: CommandExecution,
    *,
    action: str,
    allow_empty: bool = False,
) -> Any:
    if not execution.success:
        raise BackendError(_classify_command_error(execution, f"Unable to {action}"))

    if not execution.stdout.strip():
        if allow_empty:
            return []
        raise BackendError(f"No data was returned while trying to {action}.")

    try:
        return json.loads(execution.stdout)
    except json.JSONDecodeError as exc:
        raise BackendError(f"Unexpected output while trying to {action}: {execution.stdout}") from exc


def _classify_command_error(execution: CommandExecution, fallback: str) -> str:
    text = execution.combined_output or fallback
    lowered = text.lower()
    if "access is denied" in lowered or "requires elevation" in lowered or "administrator privilege" in lowered:
        return f"{fallback}. Administrator privileges are required."
    if "not found" in lowered or "is not recognized" in lowered:
        return f"{fallback}. The required Windows command is not available."
    if "timed out" in lowered:
        return f"{fallback}. The command timed out."
    return text


def _append_muted_suffix(message: str, state_payload: dict[str, Any]) -> str:
    if state_payload.get("muted"):
        return f"{message} The system is currently muted."
    return message


def parse_system_command(text: str) -> ParsedSystemCommand | None:
    normalized = _normalize_text(text)
    if not normalized:
        return None

    volume_set = re.fullmatch(
        r"(?:set|change|adjust|increase|decrease|reduce|lower|raise)\s+(?:the\s+)?(?:volume|sound|audio)(?:\s+level)?(?:\s+to\s*|\s+)?(?:(\d{1,3})\s*%?|(max(?:imum)?|full))",
        normalized,
    )
    if volume_set:
        value = volume_set.group(1) or volume_set.group(2)
        if value in (None, "max", "maximum", "full"):
            value = 100
        return ParsedSystemCommand(
            action="set_volume",
            control="volume",
            direction="set",
            value=int(value),
            raw_text=text,
        )

    brightness_set = re.fullmatch(
        r"(?:set|change|adjust|increase|decrease|reduce|lower|raise)\s+(?:the\s+)?(?:(?:screen\s+)?brightness|brightness)(?:\s+level)?(?:\s+to\s*|\s+)?(?:(\d{1,3})\s*%?|(max(?:imum)?|full))",
        normalized,
    )
    if brightness_set:
        value = brightness_set.group(1) or brightness_set.group(2)
        if value in (None, "max", "maximum", "full"):
            value = 100
        return ParsedSystemCommand(
            action="set_brightness",
            control="brightness",
            direction="set",
            value=int(value),
            raw_text=text,
        )

    if normalized in {"mute", "mute volume", "mute sound", "mute audio"}:
        return ParsedSystemCommand("mute", "volume", "mute", raw_text=text)
    if normalized in {"unmute", "unmute volume", "unmute sound", "unmute audio"}:
        return ParsedSystemCommand("unmute", "volume", "unmute", raw_text=text)
    if normalized in {"toggle mute", "toggle volume mute"}:
        return ParsedSystemCommand("toggle_mute", "volume", "toggle", raw_text=text)

    if re.fullmatch(r"(?:volume|sound|audio)\s+status", normalized) or re.fullmatch(
        r"what(?:'s| is)\s+(?:the\s+)?(?:volume|sound|audio)", normalized
    ):
        return ParsedSystemCommand("get_volume", "volume", "status", raw_text=text)

    if normalized in {
        "volume up",
        "increase volume",
        "increase the volume",
        "raise volume",
        "raise the volume",
        "turn volume up",
        "louder",
    }:
        return ParsedSystemCommand("volume_up", "volume", "up", raw_text=text)
    if normalized in {
        "volume down",
        "decrease volume",
        "decrease the volume",
        "reduce volume",
        "reduce the volume",
        "lower volume",
        "lower the volume",
        "turn volume down",
        "quieter",
    }:
        return ParsedSystemCommand("volume_down", "volume", "down", raw_text=text)

    if normalized in {
        "brightness up",
        "increase brightness",
        "increase the brightness",
        "raise brightness",
        "raise the brightness",
    }:
        return ParsedSystemCommand("brightness_up", "brightness", "up", raw_text=text)
    if normalized in {
        "brightness down",
        "decrease brightness",
        "decrease the brightness",
        "reduce brightness",
        "reduce the brightness",
        "lower brightness",
        "lower the brightness",
    }:
        return ParsedSystemCommand("brightness_down", "brightness", "down", raw_text=text)

    if re.fullmatch(r"(?:brightness|screen brightness)\s+status", normalized) or re.fullmatch(
        r"what(?:'s| is)\s+(?:the\s+)?(?:screen\s+)?brightness", normalized
    ):
        return ParsedSystemCommand("get_brightness", "brightness", "status", raw_text=text)

    if normalized in {"wifi status", "wireless status", "what is wifi", "what is the wifi"}:
        return ParsedSystemCommand("wifi_status", "wifi", "status", raw_text=text)
    if normalized in {"bluetooth status", "what is bluetooth", "what is the bluetooth"}:
        return ParsedSystemCommand("bluetooth_status", "bluetooth", "status", raw_text=text)

    if re.fullmatch(r"(?:turn|switch|set|disable)\s+(?:the\s+)?(?:wifi|wireless)\s+on", normalized) or re.fullmatch(
        r"(?:turn|switch)\s+on\s+(?:the\s+)?(?:wifi|wireless)", normalized
    ) or normalized in {"turnonwifi", "turnon wireless", "enablewifi", "enable wireless"}:
        return ParsedSystemCommand("wifi_on", "wifi", "on", raw_text=text)

    if re.fullmatch(r"(?:turn|switch|set|disable)\s+(?:the\s+)?(?:wifi|wireless)\s+off", normalized) or re.fullmatch(
        r"(?:turn|switch)\s+off\s+(?:the\s+)?(?:wifi|wireless)", normalized
    ) or normalized in {"turnoffwifi", "turnoff wireless", "disablewifi", "disable wireless"}:
        return ParsedSystemCommand("wifi_off", "wifi", "off", raw_text=text)

    if re.fullmatch(r"(?:turn|switch|set)\s+(?:the\s+)?bluetooth\s+on", normalized) or re.fullmatch(
        r"(?:turn|switch)\s+on\s+(?:the\s+)?bluetooth", normalized
    ) or normalized in {"turnonbluetooth", "enablebluetooth", "enable bluetooth"}:
        return ParsedSystemCommand("bluetooth_on", "bluetooth", "on", raw_text=text)

    if re.fullmatch(r"(?:turn|switch|set|disable)\s+(?:the\s+)?bluetooth\s+off", normalized) or re.fullmatch(
        r"(?:turn|switch)\s+off\s+(?:the\s+)?bluetooth", normalized
    ) or normalized in {"turnoffbluetooth", "disablebluetooth", "disable bluetooth"}:
        return ParsedSystemCommand("bluetooth_off", "bluetooth", "off", raw_text=text)

    if re.fullmatch(r"lock(?:\s+(?:pc|computer|screen|workstation))?", normalized):
        return ParsedSystemCommand("lock_pc", "lock", "lock", raw_text=text)

    if re.fullmatch(r"(?:cancel|abort|stop)\s+(?:the\s+)?shutdown", normalized):
        return ParsedSystemCommand("cancel_shutdown", "shutdown", "cancel", raw_text=text)

    power_match = re.fullmatch(
        r"(shutdown|shut down|restart|reboot)(?:\s+(?:pc|computer|system))?(?:\s+(?:in|after)\s+\d+\s*(?:second|seconds|sec|secs|minute|minutes|min|mins|hour|hours|hr|hrs))?",
        normalized,
    )
    if power_match:
        action_word = power_match.group(1)
        action = "shutdown" if action_word in {"shutdown", "shut down"} else "restart"
        delay_seconds = _parse_delay_seconds(normalized) or 0
        return ParsedSystemCommand(
            action=action,
            control=action,
            direction=action,
            delay_seconds=delay_seconds,
            raw_text=text,
        )

    return None


def entities_from_system_command(text: str) -> dict[str, Any]:
    parsed = parse_system_command(text)
    if parsed is None:
        return {"control": "unknown", "direction": "unknown"}
    return parsed.to_entities()


def command_from_entities(control: str, params: dict[str, Any]) -> ParsedSystemCommand | None:
    normalized_control = _normalize_text(control)
    direction = _normalize_text(str(params.get("direction") or ""))
    value = _parse_int(params.get("value") or params.get("percent"))
    delay_seconds = _parse_int(params.get("delay_seconds")) or 0
    explicit_action = _normalize_text(str(params.get("action") or ""))

    if explicit_action == "cancel_shutdown":
        return ParsedSystemCommand("cancel_shutdown", "shutdown", "cancel")
    if explicit_action == "lock_pc":
        return ParsedSystemCommand("lock_pc", "lock", "lock")
    if explicit_action in {"shutdown", "restart"}:
        return ParsedSystemCommand(
            action=explicit_action,
            control=explicit_action,
            direction=explicit_action,
            delay_seconds=delay_seconds,
        )
    if explicit_action == "set_volume" and value is not None:
        return ParsedSystemCommand("set_volume", "volume", "set", value=value)
    if explicit_action == "set_brightness" and value is not None:
        return ParsedSystemCommand("set_brightness", "brightness", "set", value=value)

    if normalized_control in {"shutdown", "restart"}:
        return ParsedSystemCommand(
            action=normalized_control,
            control=normalized_control,
            direction=normalized_control,
            delay_seconds=delay_seconds,
        )
    if normalized_control == "lock":
        return ParsedSystemCommand("lock_pc", "lock", "lock")
    if normalized_control == "volume":
        if direction in {"status", "get"}:
            return ParsedSystemCommand("get_volume", "volume", "status")
        if direction in {"set"} and value is not None:
            return ParsedSystemCommand("set_volume", "volume", "set", value=value)
        if direction in {"up", "increase", "raise"}:
            return ParsedSystemCommand("volume_up", "volume", "up", value=value)
        if direction in {"down", "decrease", "lower"}:
            return ParsedSystemCommand("volume_down", "volume", "down", value=value)
        if direction in {"mute", "off"}:
            return ParsedSystemCommand("mute", "volume", "mute")
        if direction in {"unmute", "on"}:
            return ParsedSystemCommand("unmute", "volume", "unmute")
    if normalized_control == "brightness":
        if direction in {"status", "get"}:
            return ParsedSystemCommand("get_brightness", "brightness", "status")
        if direction in {"set"} and value is not None:
            return ParsedSystemCommand("set_brightness", "brightness", "set", value=value)
        if direction in {"up", "increase", "raise"}:
            return ParsedSystemCommand("brightness_up", "brightness", "up", value=value)
        if direction in {"down", "decrease", "lower"}:
            return ParsedSystemCommand("brightness_down", "brightness", "down", value=value)
    if normalized_control == "wifi":
        if direction in {"status", "get"}:
            return ParsedSystemCommand("wifi_status", "wifi", "status")
        if direction in {"on", "enable"}:
            return ParsedSystemCommand("wifi_on", "wifi", "on")
        if direction in {"off", "disable"}:
            return ParsedSystemCommand("wifi_off", "wifi", "off")
    if normalized_control == "bluetooth":
        if direction in {"status", "get"}:
            return ParsedSystemCommand("bluetooth_status", "bluetooth", "status")
        if direction in {"on", "enable"}:
            return ParsedSystemCommand("bluetooth_on", "bluetooth", "on")
        if direction in {"off", "disable"}:
            return ParsedSystemCommand("bluetooth_off", "bluetooth", "off")
    if normalized_control == "cancel shutdown":
        return ParsedSystemCommand("cancel_shutdown", "shutdown", "cancel")
    return None


class SystemController:
    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        volume_backend: VolumeBackend | None = None,
        brightness_backend: BrightnessBackend | None = None,
        wifi_backend: WifiBackend | None = None,
        bluetooth_backend: BluetoothBackend | None = None,
        power_backend: PowerBackend | None = None,
    ) -> None:
        self._runner = runner or CommandRunner()
        self._volume = volume_backend or VolumeBackend()
        self._brightness = brightness_backend or BrightnessBackend(self._runner)
        self._wifi = wifi_backend or WifiBackend(self._runner)
        self._bluetooth = bluetooth_backend or BluetoothBackend(self._runner)
        self._power = power_backend or PowerBackend(self._runner)

    def capabilities(self) -> dict[str, Any]:
        return {
            "volume": self._probe(self.get_volume),
            "brightness": self._probe(self.get_brightness),
            "wifi": self._probe(self.wifi_status),
            "bluetooth": self._probe(self.bluetooth_status),
        }

    def get_volume(self) -> SystemActionResult:
        return self._status_action(
            action="get_volume",
            getter=self._volume.get_state,
            message_builder=lambda current: _append_muted_suffix(
                f"Volume is {current['percent']}%.",
                current,
            ),
        )

    def set_volume(self, percent: int) -> SystemActionResult:
        valid, target, error = _validate_percent(_parse_int(percent))
        if not valid:
            return self._failure("set_volume", error, "invalid_value")
        return self._set_volume_like_action("set_volume", target, message_prefix="Volume set to")

    def volume_up(self, step: int = 10) -> SystemActionResult:
        return self._adjust_volume(int(step or settings.get("default_volume_step") or 10))

    def volume_down(self, step: int = 10) -> SystemActionResult:
        return self._adjust_volume(-abs(int(step or settings.get("default_volume_step") or 10)))

    def mute(self) -> SystemActionResult:
        return self._set_mute_state(True)

    def unmute(self) -> SystemActionResult:
        return self._set_mute_state(False)

    def toggle_mute(self) -> SystemActionResult:
        previous = self._safe_get_state("toggle_mute", self._volume.get_state)
        if previous is None:
            return self._failure("toggle_mute", "Volume control is unavailable.", "unsupported")
        return self._set_mute_state(not bool(previous.get("muted")), previous_state=previous)

    def get_brightness(self) -> SystemActionResult:
        return self._status_action(
            action="get_brightness",
            getter=self._brightness.get_state,
            message_builder=lambda current: f"Brightness is {current['percent']}%.",
        )

    def set_brightness(self, percent: int) -> SystemActionResult:
        valid, target, error = _validate_percent(_parse_int(percent))
        if not valid:
            return self._failure("set_brightness", error, "invalid_value")

        previous = self._safe_get_state("set_brightness", self._brightness.get_state)
        if previous is None:
            return self._failure("set_brightness", "Brightness control is unavailable on this device.", "unsupported")

        try:
            current = self._brightness.set_percent(target)
        except BackendError as exc:
            return self._failure("set_brightness", str(exc), "backend_unavailable", previous_state=previous)

        verified = abs(int(current.get("percent", -1)) - target) <= BRIGHTNESS_TOLERANCE
        message = f"Brightness set to {current['percent']}%." if verified else (
            f"Requested brightness {target}%, but the display reported {current.get('percent', 'unknown')}%."
        )
        result = self._result(
            success=verified,
            action="set_brightness",
            previous_state=previous,
            current_state=current,
            message=message,
            error="" if verified else "verification_failed",
        )
        logger.info("Verification result: action=set_brightness success=%s current=%s", verified, current)
        return result

    def brightness_up(self, step: int = 10) -> SystemActionResult:
        return self._adjust_brightness(abs(int(step or settings.get("default_brightness_step") or 10)))

    def brightness_down(self, step: int = 10) -> SystemActionResult:
        return self._adjust_brightness(-abs(int(step or settings.get("default_brightness_step") or 10)))

    def wifi_status(self) -> SystemActionResult:
        return self._status_action(
            action="wifi_status",
            getter=self._wifi.get_state,
            message_builder=self._wifi_status_message,
        )

    def wifi_on(self) -> SystemActionResult:
        return self._set_radio_state(
            action="wifi_on",
            getter=self._wifi.get_state,
            setter=lambda: self._wifi.set_enabled(True),
            desired_state="on",
            success_message="Wi-Fi turned on.",
            already_message="Wi-Fi is already on.",
        )

    def wifi_off(self) -> SystemActionResult:
        return self._set_radio_state(
            action="wifi_off",
            getter=self._wifi.get_state,
            setter=lambda: self._wifi.set_enabled(False),
            desired_state="off",
            success_message="Wi-Fi turned off.",
            already_message="Wi-Fi is already off.",
        )

    def bluetooth_status(self) -> SystemActionResult:
        return self._status_action(
            action="bluetooth_status",
            getter=self._bluetooth.get_state,
            message_builder=lambda current: f"Bluetooth is {current['state']}.",
        )

    def bluetooth_on(self) -> SystemActionResult:
        return self._set_radio_state(
            action="bluetooth_on",
            getter=self._bluetooth.get_state,
            setter=lambda: self._bluetooth.set_enabled(True),
            desired_state="on",
            success_message="Bluetooth turned on.",
            already_message="Bluetooth is already on.",
        )

    def bluetooth_off(self) -> SystemActionResult:
        return self._set_radio_state(
            action="bluetooth_off",
            getter=self._bluetooth.get_state,
            setter=lambda: self._bluetooth.set_enabled(False),
            desired_state="off",
            success_message="Bluetooth turned off.",
            already_message="Bluetooth is already off.",
        )

    def lock_pc(self) -> SystemActionResult:
        try:
            self._power.lock()
        except BackendError as exc:
            return self._failure("lock_pc", str(exc), "lock_failed")
        result = self._result(
            success=True,
            action="lock_pc",
            previous_state={},
            current_state={"lock_requested": True},
            message="PC locked.",
        )
        logger.info("Action executed: lock_pc")
        return result

    def shutdown(self, delay: int = 0) -> SystemActionResult:
        return self._schedule_power_action("shutdown", int(delay or 0))

    def restart(self, delay: int = 0) -> SystemActionResult:
        return self._schedule_power_action("restart", int(delay or 0))

    def cancel_shutdown(self) -> SystemActionResult:
        try:
            self._power.cancel_shutdown()
        except BackendError as exc:
            return self._failure("cancel_shutdown", str(exc), "cancel_failed")
        result = self._result(
            success=True,
            action="cancel_shutdown",
            previous_state={"scheduled": True},
            current_state={"scheduled": False},
            message="Pending shutdown or restart cancelled.",
        )
        logger.info("Action executed: cancel_shutdown")
        return result

    def execute(self, command: ParsedSystemCommand) -> SystemActionResult:
        if command.action == "get_volume":
            return self.get_volume()
        if command.action == "set_volume":
            return self.set_volume(command.value if command.value is not None else -1)
        if command.action == "volume_up":
            return self.volume_up(command.value or int(settings.get("default_volume_step") or 10))
        if command.action == "volume_down":
            return self.volume_down(command.value or int(settings.get("default_volume_step") or 10))
        if command.action == "mute":
            return self.mute()
        if command.action == "unmute":
            return self.unmute()
        if command.action == "toggle_mute":
            return self.toggle_mute()
        if command.action == "get_brightness":
            return self.get_brightness()
        if command.action == "set_brightness":
            return self.set_brightness(command.value if command.value is not None else -1)
        if command.action == "brightness_up":
            return self.brightness_up(command.value or int(settings.get("default_brightness_step") or 10))
        if command.action == "brightness_down":
            return self.brightness_down(command.value or int(settings.get("default_brightness_step") or 10))
        if command.action == "wifi_status":
            return self.wifi_status()
        if command.action == "wifi_on":
            return self.wifi_on()
        if command.action == "wifi_off":
            return self.wifi_off()
        if command.action == "bluetooth_status":
            return self.bluetooth_status()
        if command.action == "bluetooth_on":
            return self.bluetooth_on()
        if command.action == "bluetooth_off":
            return self.bluetooth_off()
        if command.action == "lock_pc":
            return self.lock_pc()
        if command.action == "shutdown":
            return self.shutdown(delay=command.delay_seconds)
        if command.action == "restart":
            return self.restart(delay=command.delay_seconds)
        if command.action == "cancel_shutdown":
            return self.cancel_shutdown()
        return self._failure("system", f"Unsupported system action: {command.action}.", "unsupported_action")

    def _status_action(
        self,
        *,
        action: str,
        getter,
        message_builder,
    ) -> SystemActionResult:
        try:
            current = getter()
        except BackendError as exc:
            return self._failure(action, str(exc), "unsupported")
        result = self._result(
            success=True,
            action=action,
            previous_state={},
            current_state=current,
            message=message_builder(current),
        )
        logger.info("Verification result: action=%s current=%s", action, current)
        return result

    def _adjust_volume(self, delta: int) -> SystemActionResult:
        previous = self._safe_get_state("volume_adjust", self._volume.get_state)
        if previous is None:
            return self._failure("volume_adjust", "Volume control is unavailable.", "unsupported")

        current_percent = int(previous.get("percent", 0))
        target = _clamp_percent(current_percent + delta)
        return self._set_volume_like_action(
            "volume_up" if delta >= 0 else "volume_down",
            target,
            message_prefix="Volume increased to" if delta >= 0 else "Volume decreased to",
            previous_state=previous,
        )

    def _set_volume_like_action(
        self,
        action: str,
        target: int,
        *,
        message_prefix: str,
        previous_state: dict[str, Any] | None = None,
    ) -> SystemActionResult:
        previous = previous_state or self._safe_get_state(action, self._volume.get_state)
        if previous is None:
            return self._failure(action, "Volume control is unavailable.", "unsupported")

        try:
            self._volume.set_percent(target)
            if previous.get("muted") and target > 0:
                self._volume.set_mute(False)
            current = self._volume.get_state()
        except BackendError as exc:
            return self._failure(action, str(exc), "backend_unavailable", previous_state=previous)

        verified = abs(int(current.get("percent", -1)) - target) <= VOLUME_TOLERANCE
        message = _append_muted_suffix(
            f"{message_prefix} {current['percent']}%.",
            current,
        )
        if not verified:
            message = (
                f"Requested volume {target}%, but the system reported {current.get('percent', 'unknown')}%."
            )
        result = self._result(
            success=verified,
            action=action,
            previous_state=previous,
            current_state=current,
            message=message,
            error="" if verified else "verification_failed",
        )
        logger.info("Verification result: action=%s success=%s current=%s", action, verified, current)
        return result

    def _set_mute_state(
        self,
        muted: bool,
        *,
        previous_state: dict[str, Any] | None = None,
    ) -> SystemActionResult:
        action = "mute" if muted else "unmute"
        previous = previous_state or self._safe_get_state(action, self._volume.get_state)
        if previous is None:
            return self._failure(action, "Volume control is unavailable.", "unsupported")

        try:
            self._volume.set_mute(muted)
            current = self._volume.get_state()
        except BackendError as exc:
            return self._failure(action, str(exc), "backend_unavailable", previous_state=previous)

        verified = bool(current.get("muted")) is muted
        result = self._result(
            success=verified,
            action=action,
            previous_state=previous,
            current_state=current,
            message="Volume muted." if muted else "Volume unmuted.",
            error="" if verified else "verification_failed",
        )
        logger.info("Verification result: action=%s success=%s current=%s", action, verified, current)
        return result

    def _adjust_brightness(self, delta: int) -> SystemActionResult:
        previous = self._safe_get_state("brightness_adjust", self._brightness.get_state)
        if previous is None:
            return self._failure(
                "brightness_adjust",
                "Brightness control is unavailable on this device.",
                "unsupported",
            )

        current_percent = int(previous.get("percent", 0))
        target = _clamp_percent(current_percent + delta)
        return self.set_brightness(target)

    def _set_radio_state(
        self,
        *,
        action: str,
        getter,
        setter,
        desired_state: str,
        success_message: str,
        already_message: str,
    ) -> SystemActionResult:
        previous = self._safe_get_state(action, getter)
        if previous is None:
            return self._failure(action, "That radio control is unavailable on this PC.", "unsupported")

        if previous.get("state") == desired_state:
            return self._result(
                success=True,
                action=action,
                previous_state=previous,
                current_state=previous,
                message=already_message,
            )

        try:
            current = setter()
        except BackendError as exc:
            return self._failure(action, str(exc), "backend_unavailable", previous_state=previous)

        verified = current.get("state") == desired_state
        result = self._result(
            success=verified,
            action=action,
            previous_state=previous,
            current_state=current,
            message=success_message if verified else f"Requested {desired_state}, but the device reported {current.get('state', 'unknown')}.",
            error="" if verified else "verification_failed",
        )
        logger.info("Verification result: action=%s success=%s current=%s", action, verified, current)
        return result

    def _schedule_power_action(self, action: str, delay_seconds: int) -> SystemActionResult:
        if delay_seconds < 0:
            return self._failure(action, "Delay must be zero or a positive number of seconds.", "invalid_delay")

        try:
            if action == "shutdown":
                self._power.shutdown(delay_seconds=delay_seconds)
            else:
                self._power.restart(delay_seconds=delay_seconds)
        except BackendError as exc:
            return self._failure(action, str(exc), f"{action}_failed")

        current_state = {
            "scheduled": True,
            "delay_seconds": delay_seconds,
            "type": action,
        }
        when = _format_delay(delay_seconds)
        message = (
            f"{action.title()} scheduled {when}. Use 'cancel shutdown' to abort."
            if delay_seconds > 0
            else f"{action.title()} initiated."
        )
        result = self._result(
            success=True,
            action=action,
            previous_state={"scheduled": False},
            current_state=current_state,
            message=message,
        )
        logger.info("Action executed: %s delay=%s", action, delay_seconds)
        return result

    def _safe_get_state(self, action: str, getter) -> dict[str, Any] | None:
        try:
            return getter()
        except BackendError as exc:
            logger.warning("Capability detection failed for %s: %s", action, exc)
            return None

    def _probe(self, getter) -> dict[str, Any]:
        result = getter()
        return {
            "supported": bool(result.success),
            "error": result.error,
            "state": result.current_state,
        }

    def _wifi_status_message(self, current: dict[str, Any]) -> str:
        if current.get("state") != "on":
            return "Wi-Fi is off."
        ssid = str(current.get("ssid") or "").strip()
        if current.get("connected") and ssid:
            return f"Wi-Fi is on and connected to {ssid}."
        return "Wi-Fi is on."

    def _failure(
        self,
        action: str,
        message: str,
        error: str,
        *,
        previous_state: dict[str, Any] | None = None,
        current_state: dict[str, Any] | None = None,
    ) -> SystemActionResult:
        logger.error("System control failed: action=%s error=%s message=%s", action, error, message)
        return self._result(
            success=False,
            action=action,
            previous_state=previous_state or {},
            current_state=current_state or {},
            message=message,
            error=error,
        )

    def _result(
        self,
        *,
        success: bool,
        action: str,
        previous_state: dict[str, Any],
        current_state: dict[str, Any],
        message: str,
        error: str = "",
    ) -> SystemActionResult:
        result = SystemActionResult(
            success=success,
            action=action,
            previous_state=previous_state,
            current_state=current_state,
            message=message,
            error=error,
        )
        self._remember_state(result)
        logger.info("Action executed: %s success=%s", action, success)
        return result

    def _remember_state(self, result: SystemActionResult) -> None:
        state.last_system_action = result.to_dict()
        current = result.current_state or {}

        if result.action.startswith("volume") or result.action in {"get_volume", "mute", "unmute", "toggle_mute", "set_volume"}:
            percent = current.get("percent")
            if isinstance(percent, int):
                state.last_volume = percent

        if result.action.startswith("brightness") or result.action in {"get_brightness", "set_brightness"}:
            percent = current.get("percent")
            if isinstance(percent, int):
                state.last_brightness = percent

        if result.action.startswith("wifi") and current.get("state"):
            state.wifi_state = str(current.get("state"))

        if result.action.startswith("bluetooth") and current.get("state"):
            state.bluetooth_state = str(current.get("state"))
