"""Tests for shared.solar — design 14 §2/§4 (phase 7.1).

Reference instants fetched 2026-08-31 from api.sunrise-sunset.org
(formatted=0, UTC; NOAA-based) for each site/date; tolerance ±2 min per
the phase 7 plan. The API's civil twilight is −6° and sunrise/sunset
−0.833° — the same conventions as shared/constants.py.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from dam_shared.constants import NIGHT_OFF_DEG, NIGHT_ON_DEG
from dam_shared.solar import crossing, solar_events

NYC = (40.7128, -74.0060, ZoneInfo("America/New_York"))
SEOUL = (37.5665, 126.9780, ZoneInfo("Asia/Seoul"))
SYDNEY = (-33.8688, 151.2093, ZoneInfo("Australia/Sydney"))
MUMBAI = (19.0760, 72.8777, ZoneInfo("Asia/Kolkata"))       # UTC+5:30
SVALBARD = (78.2232, 15.6267, ZoneInfo("Arctic/Longyearbyen"))

TOL = timedelta(minutes=2)

# (site, local date, dawn, sunrise, sunset, dusk) — reference UTC instants
REFERENCE = [
    (NYC, date(2026, 6, 21),  # summer solstice
     "2026-06-21T08:51:40", "2026-06-21T09:23:27",
     "2026-06-22T00:32:20", "2026-06-22T01:04:07"),
    (NYC, date(2026, 12, 21),  # winter solstice
     "2026-12-21T11:45:39", "2026-12-21T12:14:59",
     "2026-12-21T21:33:23", "2026-12-21T22:02:43"),
    (NYC, date(2026, 3, 20),  # equinox
     "2026-03-20T10:31:34", "2026-03-20T10:57:28",
     "2026-03-20T23:09:21", "2026-03-20T23:35:15"),
    (NYC, date(2026, 3, 8),  # US spring-forward day (EST -> EDT)
     "2026-03-08T10:51:11", "2026-03-08T11:17:04",
     "2026-03-08T22:56:22", "2026-03-08T23:22:16"),
    (SEOUL, date(2026, 6, 21),
     "2026-06-20T19:39:50", "2026-06-20T20:09:30",
     "2026-06-21T10:58:10", "2026-06-21T11:27:50"),
    (SYDNEY, date(2026, 6, 21),  # southern-hemisphere winter
     "2026-06-20T20:32:16", "2026-06-20T20:58:34",
     "2026-06-21T06:55:13", "2026-06-21T07:21:31"),
    (MUMBAI, date(2026, 1, 15),  # non-integer UTC offset (+5:30)
     "2026-01-15T01:21:14", "2026-01-15T01:43:27",
     "2026-01-15T12:52:11", "2026-01-15T13:14:24"),
]


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def test_events_match_published_references_within_two_minutes():
    for (lat, lon, tz), d, dawn, sunrise, sunset, dusk in REFERENCE:
        events = solar_events(d, lat, lon, tz)
        for name, got, want in (
            ("dawn", events.dawn, dawn), ("sunrise", events.sunrise, sunrise),
            ("sunset", events.sunset, sunset), ("dusk", events.dusk, dusk),
        ):
            assert got is not None, f"{tz} {d} {name} missing"
            delta = abs(got - utc(want))
            assert delta <= TOL, f"{tz} {d} {name}: {got} vs {want} ({delta})"


def test_results_carry_the_local_timezone_and_date():
    lat, lon, tz = NYC
    events = solar_events(date(2026, 6, 21), lat, lon, tz)
    assert events.sunrise.tzinfo is tz and events.sunrise.date() == date(2026, 6, 21)
    assert events.sunrise.strftime("%z") == "-0400"  # EDT in June
    winter = solar_events(date(2026, 12, 21), lat, lon, tz)
    assert winter.sunrise.strftime("%z") == "-0500"  # EST in December


def test_polar_day_and_night_return_none():
    lat, lon, tz = SVALBARD
    midsun = solar_events(date(2026, 6, 21), lat, lon, tz)
    assert midsun.dawn is None and midsun.sunrise is None
    assert midsun.sunset is None and midsun.dusk is None
    polar = solar_events(date(2026, 12, 21), lat, lon, tz)
    assert polar.sunrise is None and polar.dusk is None


def test_crossing_is_monotonic_in_elevation():
    """Rising: the sun reaches higher elevations later; falling: earlier.
    Also orders the night-mode thresholds around dawn/dusk (design §6b)."""
    lat, lon, tz = NYC
    d = date(2026, 8, 31)
    rising = [crossing(d, lat, lon, tz, deg, rising=True) for deg in (-6, NIGHT_OFF_DEG, NIGHT_ON_DEG, -0.833)]
    assert rising == sorted(rising)  # −6° first, then −5°, −4°, sunrise
    falling = [crossing(d, lat, lon, tz, deg, rising=False) for deg in (-0.833, NIGHT_ON_DEG, NIGHT_OFF_DEG, -6)]
    assert falling == sorted(falling)  # sunset first, then −4°, −5°, dusk
    # night-mode ON (−4° falling) sits between sunset and dusk, ~10 min
    gap = falling[3] - falling[1]
    assert timedelta(minutes=3) < gap < timedelta(minutes=20)
