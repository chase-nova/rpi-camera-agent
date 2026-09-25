"""Solar event times — design 14 §2/§4 (`docs/design/14-dawn-and-dusk.md`).

NOAA/Meeus solar position arithmetic: pure math, no dependencies, no
network; accurate to well under a minute for any populated latitude.
Shared by the agent (window resolution, golden boost, IMX462 night mode)
and the video builder (nominal window resolution) — one implementation
so both always agree.

Conventions: latitude/longitude in decimal degrees, north/east positive
(WGS84); ``date`` is the LOCAL calendar date at the site; results are
tz-aware datetimes in the site's IANA zone (DST falls out of the zone —
design 14 §2). ``None`` means the sun never crosses that elevation on
that date (polar day/night).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from datetime import date as date_t

from dam_shared.constants import CIVIL_TWILIGHT_DEG, SUNRISE_SUNSET_DEG

_J2000 = datetime(2000, 1, 1, 12, 0, 0, tzinfo=UTC)  # JD 2451545.0


@dataclass(frozen=True)
class SolarEvents:
    """One local day's events; any field None on polar day/night."""

    dawn: datetime | None      # sun rises to CIVIL_TWILIGHT_DEG (−6°)
    sunrise: datetime | None   # −0.833° rising
    sunset: datetime | None    # −0.833° falling
    dusk: datetime | None      # −6° falling


def _julian_century(t_utc: datetime) -> float:
    """Julian centuries since J2000.0 for a UTC instant."""
    return (t_utc - _J2000).total_seconds() / (86400.0 * 36525.0)


def _declination_and_eot(t: float) -> tuple[float, float]:
    """Solar declination (degrees) and equation of time (minutes) at
    Julian century ``t`` — the NOAA spreadsheet formulas (Meeus ch. 25/28).
    """
    # geometric mean longitude / anomaly (degrees)
    l0 = (280.46646 + t * (36000.76983 + 0.0003032 * t)) % 360.0
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    ecc = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    m_rad = math.radians(m)
    # equation of center -> true and apparent longitude
    center = (
        math.sin(m_rad) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m_rad) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m_rad) * 0.000289
    )
    omega = math.radians(125.04 - 1934.136 * t)
    app_long = l0 + center - 0.00569 - 0.00478 * math.sin(omega)
    # obliquity (corrected)
    obliq0 = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    obliq = obliq0 + 0.00256 * math.cos(omega)
    obliq_rad = math.radians(obliq)
    # declination
    decl = math.degrees(math.asin(math.sin(obliq_rad) * math.sin(math.radians(app_long))))
    # equation of time (minutes)
    y = math.tan(obliq_rad / 2.0) ** 2
    l0_rad = math.radians(l0)
    eot = 4.0 * math.degrees(
        y * math.sin(2 * l0_rad)
        - 2.0 * ecc * math.sin(m_rad)
        + 4.0 * ecc * y * math.sin(m_rad) * math.cos(2 * l0_rad)
        - 0.5 * y * y * math.sin(4 * l0_rad)
        - 1.25 * ecc * ecc * math.sin(2 * m_rad)
    )
    return decl, eot


def _hour_angle_deg(lat: float, decl: float, elevation_deg: float) -> float | None:
    """Hour angle (degrees, positive) at which the sun sits at
    ``elevation_deg``; None when it never does (polar day/night)."""
    lat_rad = math.radians(lat)
    decl_rad = math.radians(decl)
    zenith_rad = math.radians(90.0 - elevation_deg)
    cos_ha = (math.cos(zenith_rad) - math.sin(lat_rad) * math.sin(decl_rad)) / (
        math.cos(lat_rad) * math.cos(decl_rad)
    )
    if cos_ha < -1.0 or cos_ha > 1.0:
        return None
    return math.degrees(math.acos(cos_ha))


def crossing(
    date: date_t,
    lat: float,
    lon: float,
    tz: tzinfo,
    elevation_deg: float,
    *,
    rising: bool,
) -> datetime | None:
    """The instant on the site's local ``date`` when the sun crosses
    ``elevation_deg`` going up (``rising=True``) or down. None when the
    sun never reaches that elevation that day."""
    # seed: local noon of the requested calendar date, as UTC
    guess = datetime(date.year, date.month, date.day, 12, 0, tzinfo=tz).astimezone(UTC)
    utc_midnight = guess.replace(hour=0, minute=0, second=0, microsecond=0)
    event = guess
    for _ in range(2):  # second pass evaluates the sun at the event itself
        decl, eot = _declination_and_eot(_julian_century(event))
        ha = _hour_angle_deg(lat, decl, elevation_deg)
        if ha is None:
            return None
        noon_minutes = 720.0 - 4.0 * lon - eot  # solar noon, minutes after 00:00 UTC
        minutes = noon_minutes + (-1.0 if rising else 1.0) * 4.0 * ha
        event = utc_midnight + timedelta(minutes=minutes)
    return event.astimezone(tz)


def solar_events(date: date_t, lat: float, lon: float, tz: tzinfo) -> SolarEvents:
    """Civil dawn/dusk and sunrise/sunset for the site's local date."""
    return SolarEvents(
        dawn=crossing(date, lat, lon, tz, CIVIL_TWILIGHT_DEG, rising=True),
        sunrise=crossing(date, lat, lon, tz, SUNRISE_SUNSET_DEG, rising=True),
        sunset=crossing(date, lat, lon, tz, SUNRISE_SUNSET_DEG, rising=False),
        dusk=crossing(date, lat, lon, tz, CIVIL_TWILIGHT_DEG, rising=False),
    )
