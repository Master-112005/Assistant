from __future__ import annotations

import pytest

from core import settings, state
from core.system_controls import BackendError, ParsedSystemCommand, SystemController, parse_system_command


class FakeVolumeBackend:
    def __init__(self, *, percent: int = 50, muted: bool = False, supported: bool = True):
        self.percent = percent
        self.muted = muted
        self.supported = supported

    def get_state(self):
        if not self.supported:
            raise BackendError("Volume backend unavailable.")
        return {
            "available": True,
            "backend": "fake_volume",
            "percent": self.percent,
            "muted": self.muted,
        }

    def set_percent(self, percent: int):
        if not self.supported:
            raise BackendError("Volume backend unavailable.")
        self.percent = percent

    def set_mute(self, muted: bool):
        if not self.supported:
            raise BackendError("Volume backend unavailable.")
        self.muted = muted


class FakeBrightnessBackend:
    def __init__(self, *, percent: int = 40, supported: bool = True):
        self.percent = percent
        self.supported = supported

    def get_state(self):
        if not self.supported:
            raise BackendError("Brightness unsupported.")
        return {
            "supported": True,
            "backend": "fake_brightness",
            "percent": self.percent,
        }

    def set_percent(self, percent: int):
        if not self.supported:
            raise BackendError("Brightness unsupported.")
        self.percent = percent
        return self.get_state()


class FakeRadioBackend:
    def __init__(self, *, state_value: str = "on", supported: bool = True, label: str = "wifi"):
        self.state_value = state_value
        self.supported = supported
        self.label = label

    def get_state(self):
        if not self.supported:
            raise BackendError(f"{self.label} unsupported.")
        payload = {
            "supported": True,
            "state": self.state_value,
            "adapters": [{"name": f"{self.label}-0", "status": "Up" if self.state_value == "on" else "Disabled"}],
        }
        if self.label == "wifi":
            payload["connected"] = self.state_value == "on"
            payload["ssid"] = "TestWiFi" if self.state_value == "on" else ""
        return payload

    def set_enabled(self, enabled: bool):
        if not self.supported:
            raise BackendError(f"{self.label} unsupported.")
        self.state_value = "on" if enabled else "off"
        return self.get_state()


class FakePowerBackend:
    def __init__(self):
        self.lock_calls = 0
        self.shutdown_calls: list[int] = []
        self.restart_calls: list[int] = []
        self.cancel_calls = 0

    def lock(self):
        self.lock_calls += 1

    def shutdown(self, *, delay_seconds: int):
        self.shutdown_calls.append(delay_seconds)

    def restart(self, *, delay_seconds: int):
        self.restart_calls.append(delay_seconds)

    def cancel_shutdown(self):
        self.cancel_calls += 1


@pytest.fixture(autouse=True)
def reset_state():
    settings.reset_defaults()
    state.pending_confirmation = {}
    state.last_system_action = {}
    state.last_volume = -1
    state.last_brightness = -1
    state.wifi_state = "unknown"
    state.bluetooth_state = "unknown"
    yield
    settings.reset_defaults()


def make_controller(
    *,
    volume: FakeVolumeBackend | None = None,
    brightness: FakeBrightnessBackend | None = None,
    wifi: FakeRadioBackend | None = None,
    bluetooth: FakeRadioBackend | None = None,
    power: FakePowerBackend | None = None,
) -> SystemController:
    return SystemController(
        volume_backend=volume or FakeVolumeBackend(),
        brightness_backend=brightness or FakeBrightnessBackend(),
        wifi_backend=wifi or FakeRadioBackend(label="wifi"),
        bluetooth_backend=bluetooth or FakeRadioBackend(label="bluetooth"),
        power_backend=power or FakePowerBackend(),
    )


def test_parse_system_command_covers_core_phase_commands():
    assert parse_system_command("set volume to 50") == ParsedSystemCommand(
        action="set_volume",
        control="volume",
        direction="set",
        value=50,
        delay_seconds=0,
        raw_text="set volume to 50",
    )
    assert parse_system_command("wifi status").action == "wifi_status"
    assert parse_system_command("shutdown pc in 1 minute").delay_seconds == 60


def test_parse_system_command_accepts_absolute_natural_volume_and_brightness_phrases():
    assert parse_system_command("reduce the volume to 50").action == "set_volume"
    assert parse_system_command("reduce the volume to 50").value == 50
    assert parse_system_command("increase the brightnedd to 50").action == "set_brightness"
    assert parse_system_command("increase the brightnedd to 50").value == 50


def test_volume_increase_updates_verified_state():
    controller = make_controller(volume=FakeVolumeBackend(percent=50))

    result = controller.volume_up(step=10)

    assert result.success is True
    assert result.current_state["percent"] == 60
    assert result.message == "Volume increased to 60%."
    assert state.last_volume == 60


def test_set_volume_and_mute_unmute_work():
    controller = make_controller(volume=FakeVolumeBackend(percent=30))

    set_result = controller.set_volume(80)
    mute_result = controller.mute()
    unmute_result = controller.unmute()

    assert set_result.success is True
    assert set_result.current_state["percent"] == 80
    assert mute_result.success is True
    assert mute_result.current_state["muted"] is True
    assert unmute_result.success is True
    assert unmute_result.current_state["muted"] is False


def test_invalid_percent_is_blocked():
    controller = make_controller()

    volume_result = controller.set_volume(120)
    brightness_result = controller.set_brightness(-1)

    assert volume_result.success is False
    assert volume_result.error == "invalid_value"
    assert brightness_result.success is False
    assert brightness_result.error == "invalid_value"


def test_brightness_increase_and_state_tracking_work():
    controller = make_controller(brightness=FakeBrightnessBackend(percent=25))

    result = controller.brightness_up(step=15)

    assert result.success is True
    assert result.current_state["percent"] == 40
    assert state.last_brightness == 40


def test_brightness_unsupported_device_is_honest():
    controller = make_controller(brightness=FakeBrightnessBackend(supported=False))

    result = controller.set_brightness(70)

    assert result.success is False
    assert result.error == "unsupported"
    assert "unavailable" in result.message.lower()


def test_wifi_status_and_toggle_work():
    wifi = FakeRadioBackend(state_value="on", label="wifi")
    controller = make_controller(wifi=wifi)

    status = controller.wifi_status()
    off = controller.wifi_off()
    already_off = controller.wifi_off()
    on = controller.wifi_on()

    assert status.success is True
    assert "connected" in status.message.lower()
    assert off.success is True
    assert off.current_state["state"] == "off"
    assert already_off.success is True
    assert already_off.message == "Wi-Fi is already off."
    assert on.success is True
    assert state.wifi_state == "on"


def test_bluetooth_status_and_toggle_work():
    bluetooth = FakeRadioBackend(state_value="on", label="bluetooth")
    controller = make_controller(bluetooth=bluetooth)

    status = controller.bluetooth_status()
    off = controller.bluetooth_off()
    on = controller.bluetooth_on()

    assert status.success is True
    assert status.current_state["state"] == "on"
    assert off.success is True
    assert off.current_state["state"] == "off"
    assert on.success is True
    assert state.bluetooth_state == "on"


def test_lock_shutdown_restart_and_cancel_are_real_controller_actions():
    power = FakePowerBackend()
    controller = make_controller(power=power)

    lock_result = controller.lock_pc()
    shutdown_result = controller.shutdown(delay=60)
    restart_result = controller.restart(delay=0)
    cancel_result = controller.cancel_shutdown()

    assert lock_result.success is True
    assert power.lock_calls == 1
    assert shutdown_result.success is True
    assert power.shutdown_calls == [60]
    assert "scheduled" in shutdown_result.message.lower()
    assert restart_result.success is True
    assert power.restart_calls == [0]
    assert cancel_result.success is True
    assert power.cancel_calls == 1
