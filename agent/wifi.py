"""Remote Wi-Fi setup — design 13 §3.

Requests arrive as desired config in the signer's ``/sign`` answer
(``{"wifi": {"id", "scan": true}}`` or ``{"wifi": {"id", "apply": {ssid,
psk, hidden}}}``); the uploader hands them to ``WifiManager.handle`` on its
own thread and reports the outcome in the next status (``wifi_scan`` /
``wifi_applied``), alongside the current ``wifi_ssid`` on every status.

- Scan: ``nmcli device wifi rescan`` (best effort) + ``device wifi list``,
  de-duplicated by SSID (strongest signal wins), hidden SSIDs dropped.
- Apply & switch: add/replace the profile (DNS pinned — hotspots without
  a DNS option, 2026-08-28), ``connection up`` with a timeout; on failure
  fall back to the previously active profile and delete the failed one so
  a wrong password is never retried forever.
- Privileges: plain ``nmcli`` first; on NetworkManager's "Insufficient
  privileges"/"not authorized" retry via ``sudo -n nmcli`` (sudoers line
  from ``provision-pi.sh``; Bookworm needs it — see the design note).
- Idempotent: handled request ids are persisted (``CACHE_DIR/wifi-ack.json``)
  so a repeated answer or a restart mid-apply never re-applies.
- The psk is never logged: command lines are masked before logging.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.constants import (
    NMCLI_CMD,
    NMCLI_PRIVILEGE_ERRORS,
    NMCLI_SUDO_PREFIX,
    WIFI_DNS,
    WIFI_IFNAME,
    WIFI_SCAN_MAX,
    WIFI_SCAN_TIMEOUT_S,
)

log = logging.getLogger(__name__)

_UNESCAPED_COLON = re.compile(r"(?<!\\):")
_MASK = "<psk>"


def _split_terse(line: str) -> list[str]:
    """Split an ``nmcli -t`` line on unescaped colons and unescape ``\\:``."""
    return [part.replace("\\:", ":") for part in _UNESCAPED_COLON.split(line)]


def _band(freq_field: str) -> str:
    try:
        mhz = int(freq_field.split()[0])
    except (ValueError, IndexError):
        return "?"
    return "2.4" if mhz < 3000 else "5"


class WifiManager:
    def __init__(
        self,
        ack_path: Path,
        *,
        apply_timeout_s: float,
        fallback_s: float,
        runner: Callable[..., Any] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ack_path = Path(ack_path)
        self._apply_timeout_s = float(apply_timeout_s)
        self._fallback_s = float(fallback_s)
        self._run = runner
        self._sleep = sleep
        self._now = now
        self._handled: dict[str, str] = self._load_acks()
        self.last_scan: dict[str, Any] | None = None
        self.last_applied: dict[str, Any] | None = None

    # ── persistence of handled ids ──────────────────────────────────────────

    def _load_acks(self) -> dict[str, str]:
        try:
            data = json.loads(self._ack_path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_acks(self) -> None:
        try:
            self._ack_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._ack_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._handled), encoding="utf-8")
            os.replace(tmp, self._ack_path)
        except OSError as exc:
            log.warning("cannot persist wifi acks: %s", exc)

    # ── nmcli ────────────────────────────────────────────────────────────────

    def _nmcli(
        self, args: list[str], *, secret: str | None = None, timeout: float
    ) -> tuple[int, str, str]:
        """Run nmcli; retry through sudo when NetworkManager refuses the
        service user. Returns (rc, stdout, stderr); never raises."""
        for prefix in ([NMCLI_CMD], NMCLI_SUDO_PREFIX + [NMCLI_CMD]):
            try:
                result = self._run(
                    prefix + args, capture_output=True, text=True, timeout=timeout
                )
            except subprocess.TimeoutExpired:
                return 124, "", f"timeout after {timeout:.0f}s"
            except (OSError, subprocess.SubprocessError) as exc:
                return 127, "", str(exc)
            rc = int(getattr(result, "returncode", 1))
            out = str(getattr(result, "stdout", "") or "")
            err = str(getattr(result, "stderr", "") or "")
            if rc == 0 or not any(m in err for m in NMCLI_PRIVILEGE_ERRORS):
                return rc, out, self._mask(err, secret)
            log.info("nmcli refused unprivileged (%s) - retrying via sudo", args[0:2])
        return rc, out, self._mask(err, secret)

    @staticmethod
    def _mask(text: str, secret: str | None) -> str:
        return text.replace(secret, _MASK) if secret else text

    # ── status ───────────────────────────────────────────────────────────────

    def current_ssid(self) -> str | None:
        rc, out, _ = self._nmcli(
            ["-t", "-f", "ACTIVE,SSID", "device", "wifi", "list"], timeout=WIFI_SCAN_TIMEOUT_S
        )
        if rc != 0:
            return None
        for line in out.splitlines():
            parts = _split_terse(line.strip())
            if len(parts) >= 2 and parts[0] == "yes" and parts[1]:
                return parts[1]
        return None

    def status(self) -> dict[str, Any]:
        status: dict[str, Any] = {}
        ssid = self.current_ssid()
        if ssid is not None:
            status["wifi_ssid"] = ssid
        if self.last_scan is not None:
            status["wifi_scan"] = self.last_scan
        if self.last_applied is not None:
            status["wifi_applied"] = self.last_applied
        return status

    # ── requests ─────────────────────────────────────────────────────────────

    def handle(self, request: Any) -> bool:
        """Run a desired-config request once; True when work was done."""
        if not isinstance(request, dict) or not request.get("id"):
            return False
        request_id = str(request["id"])
        if request.get("scan"):
            if self._handled.get("scan") == request_id:
                return False
            self.last_scan = self.scan(request_id)
            self._handled["scan"] = request_id
            self._save_acks()
            return True
        apply = request.get("apply")
        if isinstance(apply, dict) and apply.get("ssid"):
            if self._handled.get("apply") == request_id:
                return False
            # ack BEFORE switching: a restart mid-apply must not re-apply
            self._handled["apply"] = request_id
            self._save_acks()
            self.last_applied = self.apply(request_id, apply)
            return True
        return False

    def scan(self, request_id: str) -> dict[str, Any]:
        warning = None
        rc, _, err = self._nmcli(["device", "wifi", "rescan"], timeout=WIFI_SCAN_TIMEOUT_S)
        if rc != 0:
            warning = f"rescan failed ({err.strip().splitlines()[-1] if err.strip() else rc}); cached list"
        rc, out, err = self._nmcli(
            ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,FREQ", "device", "wifi", "list"],
            timeout=WIFI_SCAN_TIMEOUT_S,
        )
        best: dict[str, dict[str, Any]] = {}
        if rc == 0:
            for line in out.splitlines():
                parts = _split_terse(line.strip())
                if len(parts) < 5 or not parts[1]:
                    continue  # hidden SSID or malformed line
                try:
                    signal = int(parts[2])
                except ValueError:
                    signal = 0
                entry = {
                    "ssid": parts[1],
                    "signal": signal,
                    "security": parts[3] or "open",
                    "band": _band(parts[4]),
                    "in_use": parts[0] == "*",
                }
                if parts[1] not in best or signal > best[parts[1]]["signal"]:
                    best[parts[1]] = entry
        else:
            warning = f"list failed: {err.strip() or rc}"
        networks = sorted(best.values(), key=lambda e: -e["signal"])[:WIFI_SCAN_MAX]
        result: dict[str, Any] = {
            "id": request_id,
            "at": self._now().isoformat(),
            "networks": networks,
        }
        if warning:
            result["warning"] = warning
        log.info("wifi scan id=%s networks=%d%s", request_id, len(networks),
                 f" warning={warning}" if warning else "")
        return result

    def apply(self, request_id: str, apply: dict[str, Any]) -> dict[str, Any]:
        ssid = str(apply["ssid"])
        psk = apply.get("psk")
        psk = str(psk) if psk else None
        hidden = bool(apply.get("hidden", False))
        previous = self.current_ssid()
        outcome: dict[str, Any] = {"id": request_id, "ssid": ssid, "ok": False, "error": None}
        log.info("wifi apply id=%s ssid=%s (previous=%s)", request_id, ssid, previous)

        # replace any profile of the same name (ignore "unknown connection")
        self._nmcli(["connection", "delete", ssid], timeout=WIFI_SCAN_TIMEOUT_S)
        add = [
            "connection", "add", "type", "wifi", "con-name", ssid,
            "ifname", WIFI_IFNAME, "ssid", ssid,
            "connection.autoconnect", "yes",
            # retry forever: NM's default 4 retries BLOCKS the profile after
            # a brief bad patch and the device never rejoins (bit 92-1 twice,
            # 2026-09-02). No priority pin: a pinned priority made NM prefer
            # this network even where it is the weaker one at the device's
            # position — neutral priority lets signal decide.
            "connection.autoconnect-retries", "0",
            "ipv4.dns", WIFI_DNS,
        ]
        if psk:
            add += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
        if hidden:
            add += ["802-11-wireless.hidden", "yes"]
        rc, _, err = self._nmcli(add, secret=psk, timeout=WIFI_SCAN_TIMEOUT_S)
        if rc != 0:
            outcome["error"] = f"add failed: {err.strip() or rc}"
            outcome["at"] = self._now().isoformat()
            log.warning("wifi apply id=%s %s", request_id, outcome["error"])
            return outcome

        rc, _, err = self._nmcli(["connection", "up", ssid], secret=psk,
                                 timeout=self._apply_timeout_s)
        if rc == 0:
            outcome["ok"] = True
            outcome["at"] = self._now().isoformat()
            log.info("wifi apply id=%s switched to %s", request_id, ssid)
            return outcome

        # first stderr line carries the reason; later lines are nmcli hints
        reason = err.strip().splitlines()[0] if err.strip() else rc
        outcome["error"] = f"connect failed: {reason}"
        log.warning("wifi apply id=%s %s - falling back", request_id, outcome["error"])
        # NetworkManager autoconnect normally restores the previous profile;
        # give it a moment, then push it explicitly if nothing is active.
        self._sleep(self._fallback_s)
        if self.current_ssid() is None and previous:
            self._nmcli(["connection", "up", previous], timeout=self._apply_timeout_s)
        self._nmcli(["connection", "delete", ssid], timeout=WIFI_SCAN_TIMEOUT_S)
        outcome["fallback"] = self.current_ssid()
        outcome["at"] = self._now().isoformat()
        return outcome
