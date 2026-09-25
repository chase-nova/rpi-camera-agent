"""Per-day solar times for the agent — design 14 §4/§5 (phase 7.4).

``SolarDay`` owns the device's coordinates (learned from the ``/sign``
answer, persisted next to the clock anchor so offline reboots keep them)
and computes, once per local calendar day, today's civil dawn/dusk plus
the night-mode threshold crossings (§6b — consumed in phase 7.6).

- ``resolve(bound, date)`` turns a window bound into ``HH:MM``: plain
  ``HH:MM`` passes through; ``"dawn"``/``"dusk"`` resolve via
  ``shared.solar``; without coordinates or without an event (polar
  day/night) the fixed fallbacks apply, logged once per day.
- ``report(date)`` yields ``dawn_at``/``dusk_at`` (local ISO) for status.

Rollover needs no timer: every call passes today's date from the agent
clock (trustworthy offline — design 12), and a date change triggers the
recompute.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date as date_t
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from datetime import timedelta

from dam_shared.constants import (
    FALLBACK_DAWN,
    FALLBACK_DUSK,
    GOLDEN_BOOST_FACTOR,
    GOLDEN_PAD_AFTER_SUN_MIN,
    GOLDEN_PAD_BEFORE_MIN,
    NIGHT_OFF_DEG,
    NIGHT_ON_DEG,
    WINDOW_NOMINALS,
)
from dam_shared.solar import SolarEvents, crossing, solar_events

log = logging.getLogger(__name__)

_FALLBACKS = {"dawn": FALLBACK_DAWN, "dusk": FALLBACK_DUSK}


def golden_interval(base_s: int) -> int:
    """Capture interval inside an enabled golden window: base ÷ 4 (design
    14 §6 — factor fixed, never fed back into the base interval)."""
    return max(1, base_s // GOLDEN_BOOST_FACTOR)


class SolarDay:
    def __init__(self, coords_path: Path, timezone: str) -> None:
        self._path = Path(coords_path)
        self._tz = ZoneInfo(timezone)
        self.coords: tuple[float, float] | None = self._load()
        self._date: date_t | None = None
        self.events: SolarEvents | None = None
        # night-mode thresholds (design 14 §6b, applied in phase 7.6)
        self.night_on: datetime | None = None   # sun falls to −4°
        self.night_off: datetime | None = None  # sun rises to −5°
        self._fallback_logged: date_t | None = None

    # ── coordinates (from the /sign answer; design 14 §4) ───────────────────

    def _load(self) -> tuple[float, float] | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return (float(data["latitude"]), float(data["longitude"]))
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def update_coords(self, latitude: float, longitude: float) -> None:
        """Adopt (and persist) coordinates; a change forces a recompute."""
        coords = (float(latitude), float(longitude))
        if coords == self.coords:
            return
        self.coords = coords
        self._date = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"latitude": coords[0], "longitude": coords[1]}),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
            log.info("coordinates updated: %.4f, %.4f", *coords)
        except OSError as exc:
            log.warning("cannot persist coordinates: %s", exc)

    # ── per-day computation ─────────────────────────────────────────────────

    def _ensure(self, date: date_t) -> None:
        if date == self._date:
            return
        self._date = date
        self.events = self.night_on = self.night_off = None
        if self.coords is None:
            return
        lat, lon = self.coords
        self.events = solar_events(date, lat, lon, self._tz)
        self.night_on = crossing(date, lat, lon, self._tz, NIGHT_ON_DEG, rising=False)
        self.night_off = crossing(date, lat, lon, self._tz, NIGHT_OFF_DEG, rising=True)
        if self.events.dawn and self.events.dusk:
            log.info(
                "solar day %s: dawn %s dusk %s", date,
                self.events.dawn.strftime("%H:%M"), self.events.dusk.strftime("%H:%M"),
            )

    def resolve(self, bound: str, date: date_t) -> str:
        """Window bound → concrete ``HH:MM`` for the given local date."""
        if bound not in WINDOW_NOMINALS:
            return bound
        self._ensure(date)
        event = getattr(self.events, bound, None) if self.events else None
        if event is None:
            if self._fallback_logged != date:
                self._fallback_logged = date
                log.info(
                    "no solar %s for %s (%s) - fixed fallback %s/%s", bound, date,
                    "no coordinates" if self.coords is None else "no event",
                    FALLBACK_DAWN, FALLBACK_DUSK,
                )
            return _FALLBACKS[bound]
        return event.strftime("%H:%M")

    def in_golden(
        self, now: datetime, date: date_t, boost_dawn: bool, boost_dusk: bool
    ) -> bool:
        """Is ``now`` inside an ENABLED golden window (design 14 §6)?
        Dawn window: dawn−5 min … sunrise+15 min; dusk window:
        sunset−15 min … dusk+5 min. Without coordinates/events: False."""
        if not (boost_dawn or boost_dusk):
            return False
        self._ensure(date)
        events = self.events
        if events is None:
            return False
        before = timedelta(minutes=GOLDEN_PAD_BEFORE_MIN)
        after_sun = timedelta(minutes=GOLDEN_PAD_AFTER_SUN_MIN)
        if boost_dawn and events.dawn and events.sunrise:
            if events.dawn - before <= now < events.sunrise + after_sun:
                return True
        if boost_dusk and events.sunset and events.dusk:
            if events.sunset - after_sun <= now < events.dusk + before:
                return True
        return False

    def night_active(
        self,
        now: datetime,
        date: date_t,
        on_offset_min: int = 0,
        off_offset_min: int = 0,
    ) -> bool:
        """IMX462 night mode by solar time (design 14 §6b): ON when the sun
        fell to −4° this evening, OFF when it rises to −5° tomorrow
        morning (decided with today's crossings — they shift ~1–2 min/day,
        inside the capture cadence). Fallback without coordinates or
        events: fixed 18:00 → 06:00 local."""
        self._ensure(date)
        if self.night_on is None or self.night_off is None:
            hhmm = now.strftime("%H:%M")
            return hhmm >= FALLBACK_DUSK or hhmm < FALLBACK_DAWN
        on = self.night_on + timedelta(minutes=on_offset_min)
        off = self.night_off + timedelta(minutes=off_offset_min)
        return now >= on or now < off

    def report(self, date: date_t) -> dict[str, Any]:
        """Today's dawn/dusk for status (``reported.dawn_at/dusk_at``)."""
        self._ensure(date)
        if self.events is None:
            return {}
        report: dict[str, Any] = {}
        if self.events.dawn:
            report["dawn_at"] = self.events.dawn.isoformat()
        if self.events.dusk:
            report["dusk_at"] = self.events.dusk.isoformat()
        return report
