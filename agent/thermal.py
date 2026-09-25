"""Thermal protection — sensor reads + state machine (design 02 §5.2).

Read at every capture interval. Graduated response:
warn (>= temp_warn_c) -> pause capturing (>= temp_pause_c, resume at
<= temp_resume_c) -> optional OS shutdown (>= temp_shutdown_c for
TEMP_SHUTDOWN_CONSECUTIVE consecutive readings, off by default — a
remotely shut-down NAT device cannot be restarted).

On non-Pi systems the sensor reads return None and the state stays "ok".
"""

from __future__ import annotations

import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent.config import TEMP_SHUTDOWN_CONSECUTIVE, Settings
from agent.constants import IP_PROBE_ADDR

THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")


def read_temp_c() -> float | None:
    """SoC temperature in Celsius; None where unavailable (e.g. Windows)."""
    try:
        return int(THERMAL_ZONE.read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def read_volt_core() -> float | None:
    """Core rail voltage via vcgencmd, e.g. 1.3125; None if unavailable.
    (Pi 3/Zero cannot measure the 5 V input rail — the core voltage plus
    the get_throttled under-voltage bits are the available power signals.)"""
    try:
        out = subprocess.run(
            ["vcgencmd", "measure_volts", "core"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        return float(out.strip().split("=", 1)[-1].rstrip("V"))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def read_throttled() -> str | None:
    """Firmware throttle flags via vcgencmd, e.g. '0x0'; None if unavailable."""
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        return out.strip().split("=", 1)[-1]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


@dataclass(frozen=True)
class ThermalStatus:
    state: str  # "ok" | "warn" | "paused"
    temp_c: float | None
    throttled: str | None
    should_shutdown: bool


class ThermalMonitor:
    """State machine with pause/resume hysteresis and shutdown counting."""

    def __init__(
        self,
        settings: Settings,
        *,
        read_temp: Callable[[], float | None] = read_temp_c,
        read_throttle: Callable[[], str | None] = read_throttled,
    ) -> None:
        self._settings = settings
        self._read_temp = read_temp
        self._read_throttle = read_throttle
        self._paused = False
        self._shutdown_streak = 0

    def check(self) -> ThermalStatus:
        s = self._settings
        temp = self._read_temp()
        throttled = self._read_throttle()

        if temp is None:
            self._shutdown_streak = 0
            return ThermalStatus("ok", None, throttled, False)

        if temp >= s.temp_shutdown_c:
            self._shutdown_streak += 1
        else:
            self._shutdown_streak = 0
        should_shutdown = (
            s.temp_shutdown_enabled
            and self._shutdown_streak >= TEMP_SHUTDOWN_CONSECUTIVE
        )

        if self._paused:
            if temp <= s.temp_resume_c:
                self._paused = False
        elif temp >= s.temp_pause_c:
            self._paused = True

        if self._paused:
            state = "paused"
        elif temp >= s.temp_warn_c:
            state = "warn"
        else:
            state = "ok"
        return ThermalStatus(state, temp, throttled, should_shutdown)


def read_local_ip() -> str | None:
    """This device's outbound-interface IP (e.g. a private LAN address,
    172.20.10.x on an iPhone hotspot); None when offline. The UDP connect
    sends no packet — it only resolves the routing decision."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(IP_PROBE_ADDR)
            return sock.getsockname()[0]
    except OSError:
        return None


def read_pi_model() -> str | None:
    """Pi model string, e.g. 'Raspberry Pi 3 Model B Rev 1.2'; None off-Pi."""
    try:
        raw = Path("/proc/device-tree/model").read_bytes()
        return raw.rstrip(b"\x00").decode("utf-8", "replace")
    except OSError:
        return None
