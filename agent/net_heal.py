"""Network self-healing watchdog — design 12 §3 extension (2026-09-02).

NetworkManager's autoconnect is the normal reconnect path, but a wedged
radio/supplicant can sit dead forever while the AP is up and waiting
(seen repeatedly on ``dam-imx462-92-1``: hotspot On, clients=0, device
silent until a manual power-cycle). The agent therefore watches for
prolonged silence — no successful signer round trip — and escalates:

1. after ``NETWORK_KICK_AFTER_S``: bounce the radio
   (``nmcli radio wifi off`` / ``on``) — clears wedges and re-fires
   autoconnect cleanly, with none of the manual-disconnect traps;
   repeated every interval while the silence lasts;
2. after ``NETWORK_REBOOT_AFTER_S``: last-resort reboot — every wedge of
   this kind has been cured by a boot, and the spill cache (design 12)
   carries the frames across it.

Either timer set to 0 disables that stage. All actions are logged and
counted in status (``net_kicks``/``net_reboots``). Commands run through
the same nmcli sudoers grant as design 13; the reboot needs one more
sudoers line (provision-pi.sh).
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from typing import Any

from agent.constants import NMCLI_CMD, NMCLI_SUDO_PREFIX, REBOOT_CMD

log = logging.getLogger(__name__)

_CMD_TIMEOUT_S = 30.0


class NetworkHealer:
    def __init__(
        self,
        kick_after_s: float,
        reboot_after_s: float,
        *,
        runner: Callable[..., Any] = subprocess.run,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._kick_after_s = float(kick_after_s)
        self._reboot_after_s = float(reboot_after_s)
        self._run = runner
        self._mono = monotonic
        self._sleep = sleep
        self._last_success = self._mono()
        self._last_kick = float("-inf")
        self.kicks = 0
        self.reboots = 0

    def note_success(self) -> None:
        """Any successful signer round trip proves the network path."""
        self._last_success = self._mono()

    def check(self) -> None:
        """Called from the uploader loop; never raises."""
        now = self._mono()
        silence = now - self._last_success
        if self._reboot_after_s > 0 and silence >= self._reboot_after_s:
            self._reboot(silence)
            return
        if (
            self._kick_after_s > 0
            and silence >= self._kick_after_s
            and now - self._last_kick >= self._kick_after_s
        ):
            self._last_kick = now
            self._kick(silence)

    def _cmd(self, args: list[str]) -> None:
        try:
            self._run(
                NMCLI_SUDO_PREFIX + args,
                capture_output=True, text=True, timeout=_CMD_TIMEOUT_S,
            )
        except Exception as exc:  # missing binary / timeout: log, never crash
            log.warning("net-heal command %s failed: %s", args[:3], exc)

    def _kick(self, silence: float) -> None:
        self.kicks += 1
        log.warning(
            "network silent for %.0fs - bouncing the radio (kick #%d)",
            silence, self.kicks,
        )
        self._cmd([NMCLI_CMD, "radio", "wifi", "off"])
        self._sleep(2)
        self._cmd([NMCLI_CMD, "radio", "wifi", "on"])

    def _reboot(self, silence: float) -> None:
        self.reboots += 1
        log.error(
            "network silent for %.0fs despite kicks - rebooting (design 12: "
            "the spill cache carries the frames across the boot)", silence,
        )
        self._cmd([REBOOT_CMD])
        # if the reboot command failed (no sudoers yet), do not spin: only
        # try again after another full reboot window
        self._last_success = self._mono()
