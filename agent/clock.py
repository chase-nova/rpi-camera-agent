"""Internet-anchored agent clock — design 12 §4.

The Pi has no RTC, so the only trustworthy time is the internet's: NTP
(``systemd-timesyncd``) or the ``Date`` header of a signer response. The
clock keeps one **anchor** ``(utc, monotonic, boot_id, source)`` taken from
such a reading and derives "now" as ``anchor.utc + (monotonic − anchor.mono)``
— a crystal-accurate holdover that never consults the system wall clock,
so NTP steps or a stale ``fake-hwclock`` restore cannot move frame names.

Qualities of a reading (``TIME_QUALITIES``):

- ``synced``      — anchor of this boot, fresh, and the uploader is online
- ``holdover``    — anchor of this boot, but stale/offline
- ``provisional`` — no anchor for this boot (booted offline): seeded from
  ``max(system clock, last persisted anchor)``; frames stamped this way are
  corrected exactly when the first trusted reading arrives (``Resync``).

The anchor is persisted (``CACHE_DIR/anchor.json``) so a restart of the
agent inside the same boot keeps its anchor (monotonic time continues),
and a reboot during an outage still knows the last internet time seen.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from agent.constants import (
    ANCHOR_TOLERANCE_S,
    BOOT_ID_PATH,
    TIME_QUALITIES,
    TIME_SOURCES,
)

log = logging.getLogger(__name__)

QUALITY_SYNCED, QUALITY_HOLDOVER, QUALITY_PROVISIONAL = TIME_QUALITIES
SOURCE_NTP, SOURCE_SIGNER, SOURCE_NONE = TIME_SOURCES
RESYNC_PROVISIONAL = "provisional"
RESYNC_HOLDOVER = "holdover"
_NTP_CMD = ["timedatectl", "show", "-p", "NTPSynchronized", "--value"]
_NTP_TIMEOUT_S = 5


def read_boot_id(path: str = BOOT_ID_PATH) -> str:
    """Kernel boot id; a per-process random id where it is unavailable
    (Windows/tests) — which correctly makes any stored anchor 'another
    boot'."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return f"proc-{uuid.uuid4()}"


@dataclass(frozen=True)
class Anchor:
    utc: datetime  # aware, UTC
    mono: float
    boot_id: str
    source: str

    def to_json(self) -> dict[str, Any]:
        return {
            "utc": self.utc.isoformat(),
            "mono": self.mono,
            "boot_id": self.boot_id,
            "source": self.source,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Anchor:
        utc = datetime.fromisoformat(str(data["utc"]))
        if utc.tzinfo is None:
            raise ValueError("anchor utc must be timezone-aware")
        return cls(
            utc=utc.astimezone(UTC),
            mono=float(data["mono"]),
            boot_id=str(data["boot_id"]),
            source=str(data.get("source") or SOURCE_NONE),
        )


@dataclass(frozen=True)
class Resync:
    """What a trusted reading told us about frames stamped before it.

    - ``provisional``: the first trusted reading of this boot — every
      provisional frame of this boot is off by exactly ``offset_s``.
    - ``holdover``: a refresh after a holdover window — the crystal drifted
      ``drift_s`` over ``[window_start_mono, window_end_mono]``; a frame
      captured inside the window gets a linear share of it.
    """

    kind: str
    source: str
    offset_s: float
    drift_s: float
    window_start_mono: float
    window_end_mono: float

    def correction_s(self, captured_mono: float) -> float:
        """Seconds to add to a cached frame's stamp."""
        if self.kind == RESYNC_PROVISIONAL:
            return self.offset_s
        span = self.window_end_mono - self.window_start_mono
        if span <= 0:
            return 0.0
        share = (captured_mono - self.window_start_mono) / span
        return self.drift_s * min(1.0, max(0.0, share))


class AnchorStore:
    """Atomic JSON persistence of the anchor; never raises."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._warned = False

    def load(self) -> Anchor | None:
        try:
            with open(self.path, encoding="utf-8") as handle:
                return Anchor.from_json(json.load(handle))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("anchor file unreadable, ignoring: %s", exc)
            return None

    def save(self, anchor: Anchor) -> bool:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(anchor.to_json(), handle)
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            if not self._warned:
                log.warning("cannot persist time anchor to %s: %s", self.path, exc)
                self._warned = True
            return False


class AgentClock:
    def __init__(
        self,
        tz: tzinfo,
        store: AnchorStore | None = None,
        *,
        refresh_s: float,
        monotonic: Callable[[], float] = time.monotonic,
        system_utc: Callable[[], datetime] = lambda: datetime.now(UTC),
        boot_id: str | None = None,
    ) -> None:
        self._tz = tz
        self._store = store
        self._refresh_s = float(refresh_s)
        self._mono = monotonic
        self._system_utc = system_utc
        self.boot_id = boot_id or read_boot_id()
        self._online = True
        self._anchor: Anchor | None = None  # trusted, this boot
        self._last_ntp_poll_mono = float("-inf")
        stored = store.load() if store is not None else None
        if stored is not None and stored.boot_id == self.boot_id:
            # agent restart inside the same boot: monotonic time continued
            self._anchor = stored
            self._prov: Anchor | None = None
            log.info("clock restored anchor source=%s", stored.source)
        else:
            seed = self._system_utc().astimezone(UTC)
            if stored is not None and stored.utc > seed:
                seed = stored.utc  # the system clock can only be behind it
            self._prov = Anchor(seed, self._mono(), self.boot_id, SOURCE_NONE)
            log.info(
                "clock provisional until a trusted reading (seed=%s, stored_anchor=%s)",
                seed.isoformat(timespec="seconds"),
                "none" if stored is None else stored.utc.isoformat(timespec="seconds"),
            )

    # ── reading ─────────────────────────────────────────────────────────────

    @property
    def anchor(self) -> Anchor | None:
        return self._anchor

    @property
    def time_source(self) -> str:
        return self._anchor.source if self._anchor is not None else SOURCE_NONE

    def set_online(self, online: bool) -> None:
        self._online = online

    def utc_at(self, mono: float) -> datetime:
        base = self._anchor if self._anchor is not None else self._prov
        assert base is not None
        return base.utc + timedelta(seconds=mono - base.mono)

    def quality_at(self, mono: float) -> str:
        if self._anchor is None:
            return QUALITY_PROVISIONAL
        fresh = (mono - self._anchor.mono) < 2 * self._refresh_s
        return QUALITY_SYNCED if (self._online and fresh) else QUALITY_HOLDOVER

    def now(self) -> tuple[datetime, str]:
        mono = self._mono()
        return self.utc_at(mono).astimezone(self._tz), self.quality_at(mono)

    def now_local(self) -> datetime:
        """Camera/heartbeat clock callable: device-local aware datetime."""
        return self.now()[0]

    # ── trusted readings ────────────────────────────────────────────────────

    def observe_trusted(
        self, utc: datetime, mono: float, source: str, *, force: bool = False
    ) -> Resync | None:
        """Record an internet time reading taken at monotonic ``mono``.
        Returns the correction cached frames need, or None when the
        reading was only a within-rate-limit confirmation."""
        utc = utc.astimezone(UTC)
        if self._anchor is None:
            offset = (utc - self.utc_at(mono)).total_seconds()
            self._anchor = Anchor(utc, mono, self.boot_id, source)
            self._prov = None
            self.persist_now()
            log.info(
                "clock anchored source=%s provisional_offset=%.2fs", source, offset
            )
            return Resync(RESYNC_PROVISIONAL, source, offset, 0.0, mono, mono)
        age = mono - self._anchor.mono
        if not force and age < self._refresh_s:
            return None
        error = (utc - self.utc_at(mono)).total_seconds()
        if abs(error) > ANCHOR_TOLERANCE_S:
            log.warning(
                "clock correction %.2fs over %.0fs holdover source=%s", error, age, source
            )
        window_start = self._anchor.mono
        self._anchor = Anchor(utc, mono, self.boot_id, source)
        self.persist_now()
        return Resync(RESYNC_HOLDOVER, source, 0.0, error, window_start, mono)

    def persist_now(self) -> None:
        """Write the current anchor (called on refresh and by the uploader
        on the ONLINE → OFFLINE transition)."""
        if self._anchor is not None and self._store is not None:
            self._store.save(self._anchor)

    def observe_http_date(
        self, header: str | None, mono: float | None = None, *, force: bool = False
    ) -> Resync | None:
        """Signer ``Date`` header (±1 s). Ignored while a fresh NTP anchor
        exists — NTP outranks it; used whenever NTP is unavailable (captive
        portals block UDP 123 but not the HTTPS round trip)."""
        if not header:
            return None
        try:
            parsed = parsedate_to_datetime(header)
        except (TypeError, ValueError, IndexError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        mono = self._mono() if mono is None else mono
        if (
            self._anchor is not None
            and self._anchor.source == SOURCE_NTP
            and (mono - self._anchor.mono) < self._refresh_s
        ):
            return None
        return self.observe_trusted(parsed, mono, SOURCE_SIGNER, force=force)

    def maybe_poll_ntp(self, runner: Callable[..., Any] = subprocess.run) -> Resync | None:
        """Ask systemd-timesyncd whether the system clock is NTP-synchronized
        (rate-limited to once per refresh interval); if so, the system
        clock is a trusted reading. Never raises; absent tool → nothing."""
        mono = self._mono()
        if mono - self._last_ntp_poll_mono < self._refresh_s:
            return None
        self._last_ntp_poll_mono = mono
        try:
            result = runner(
                _NTP_CMD, capture_output=True, text=True, timeout=_NTP_TIMEOUT_S
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if (getattr(result, "stdout", "") or "").strip().lower() != "yes":
            return None
        return self.observe_trusted(self._system_utc(), self._mono(), SOURCE_NTP)
