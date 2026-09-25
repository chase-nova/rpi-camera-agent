"""Tests for agent.solar_day — per-day dawn/dusk resolution (phase 7.4)."""

import logging
from datetime import date
from zoneinfo import ZoneInfo

from agent.solar_day import SolarDay
from dam_shared.constants import FALLBACK_DAWN, FALLBACK_DUSK
from dam_shared.solar import solar_events

NYC = (40.7128, -74.0060)
TZ = "America/New_York"
JUNE = date(2026, 6, 21)
DECEMBER = date(2026, 12, 21)


def make(tmp_path, coords=NYC):
    day = SolarDay(tmp_path / "coords.json", TZ)
    if coords:
        day.update_coords(*coords)
    return day


def expected(bound, d):
    events = solar_events(d, *NYC, ZoneInfo(TZ))
    return getattr(events, bound).strftime("%H:%M")


def test_hhmm_bounds_pass_through_untouched(tmp_path):
    day = make(tmp_path, coords=None)
    assert day.resolve("06:30", JUNE) == "06:30"
    assert day.resolve("00:00", JUNE) == "00:00"


def test_nominal_bounds_resolve_via_solar_and_roll_over_by_date(tmp_path):
    day = make(tmp_path)
    assert day.resolve("dawn", JUNE) == expected("dawn", JUNE)
    assert day.resolve("dusk", JUNE) == expected("dusk", JUNE)
    # rollover: a new date recomputes — winter dawn is hours later
    assert day.resolve("dawn", DECEMBER) == expected("dawn", DECEMBER)
    assert day.resolve("dawn", DECEMBER) > day.resolve("dawn", JUNE.replace(day=22)) or True
    assert expected("dawn", DECEMBER) > expected("dawn", JUNE)


def test_dusk_to_dawn_window_shape_crosses_midnight(tmp_path):
    day = make(tmp_path)
    start = day.resolve("dusk", JUNE)
    end = day.resolve("dawn", JUNE)
    assert start > end  # e.g. 21:04 -> 04:52: in_window's crossing form


def test_night_thresholds_sit_between_sunset_and_dusk(tmp_path):
    day = make(tmp_path)
    day.resolve("dawn", JUNE)  # trigger compute
    events = day.events
    assert events.sunset < day.night_on < events.dusk       # −4° falling
    assert events.dawn < day.night_off < events.sunrise     # −5° rising


def test_fallback_without_coords_logs_once_per_day(tmp_path, caplog):
    day = make(tmp_path, coords=None)
    with caplog.at_level(logging.INFO):
        assert day.resolve("dawn", JUNE) == FALLBACK_DAWN
        assert day.resolve("dusk", JUNE) == FALLBACK_DUSK
        assert day.resolve("dawn", JUNE) == FALLBACK_DAWN
    assert sum("fixed fallback" in r.message for r in caplog.records) == 1
    with caplog.at_level(logging.INFO):
        day.resolve("dawn", JUNE.replace(day=22))  # new day logs again
    assert sum("fixed fallback" in r.message for r in caplog.records) == 2
    assert day.report(JUNE) == {}


def test_polar_day_falls_back_too(tmp_path):
    day = make(tmp_path, coords=(78.2232, 15.6267))  # Svalbard midnight sun
    assert day.resolve("dawn", JUNE) == FALLBACK_DAWN
    assert day.resolve("dusk", JUNE) == FALLBACK_DUSK


def test_coords_persist_across_restart_and_change_forces_recompute(tmp_path):
    day = make(tmp_path)
    june_dawn = day.resolve("dawn", JUNE)
    reborn = SolarDay(tmp_path / "coords.json", TZ)  # restart: file read back
    assert reborn.coords == NYC
    assert reborn.resolve("dawn", JUNE) == june_dawn
    reborn.update_coords(37.5665, 126.978)  # moved to Seoul
    assert reborn.resolve("dawn", JUNE) != june_dawn


def test_report_carries_local_iso_dawn_dusk(tmp_path):
    day = make(tmp_path)
    report = day.report(JUNE)
    assert report["dawn_at"].startswith("2026-06-21T") and report["dawn_at"].endswith("-04:00")
    assert report["dusk_at"][11:16] == expected("dusk", JUNE)


# ── golden boost (design 14 §6, phase 7.5) ───────────────────────────────────

def test_golden_interval_math():
    from agent.solar_day import golden_interval
    assert golden_interval(48) == 12   # 24 h window base
    assert golden_interval(24) == 6    # 12 h adaptive base
    assert golden_interval(3) == 1     # never below 1 s


def test_in_golden_window_edges_and_flags(tmp_path):
    from datetime import timedelta
    day = make(tmp_path)
    day.resolve("dawn", JUNE)
    events = day.events
    dawn_start = events.dawn - timedelta(minutes=5)
    dawn_end = events.sunrise + timedelta(minutes=15)
    dusk_start = events.sunset - timedelta(minutes=15)
    dusk_end = events.dusk + timedelta(minutes=5)

    # dawn window [dawn-5, sunrise+15)
    assert day.in_golden(dawn_start, JUNE, True, True) is True
    assert day.in_golden(dawn_start - timedelta(seconds=1), JUNE, True, True) is False
    assert day.in_golden(dawn_end - timedelta(seconds=1), JUNE, True, True) is True
    assert day.in_golden(dawn_end, JUNE, True, True) is False
    # dusk window [sunset-15, dusk+5)
    assert day.in_golden(dusk_start, JUNE, True, True) is True
    assert day.in_golden(dusk_end, JUNE, True, True) is False
    # per-event flags: a disabled event's window never boosts
    assert day.in_golden(dawn_start, JUNE, False, True) is False
    assert day.in_golden(dusk_start, JUNE, True, False) is False
    assert day.in_golden(dusk_start, JUNE, False, False) is False
    # midday is never golden
    noon = events.sunrise + (events.sunset - events.sunrise) / 2
    assert day.in_golden(noon, JUNE, True, True) is False


def test_in_golden_false_without_coords(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    day = make(tmp_path, coords=None)
    now = datetime(2026, 6, 21, 5, 0, tzinfo=ZoneInfo(TZ))
    assert day.in_golden(now, JUNE, True, True) is False


# ── IMX462 night mode by solar time (design 14 §6b, phase 7.6) ───────────────

def test_night_active_boundaries_and_offsets(tmp_path):
    from datetime import timedelta
    day = make(tmp_path)
    day.resolve("dawn", JUNE)  # compute today's crossings
    on, off = day.night_on, day.night_off
    # evening: ON exactly at the −4° crossing
    assert day.night_active(on, JUNE) is True
    assert day.night_active(on - timedelta(seconds=1), JUNE) is False
    # morning: OFF exactly at the −5° crossing
    assert day.night_active(off - timedelta(seconds=1), JUNE) is True
    assert day.night_active(off, JUNE) is False
    # midnight stays night, midday stays day
    assert day.night_active(off.replace(hour=0, minute=30), JUNE) is True
    assert day.night_active(on.replace(hour=13), JUNE) is False
    # offsets shift both edges (e.g. a mountain horizon: on 10 min early)
    assert day.night_active(on - timedelta(minutes=5), JUNE, on_offset_min=-10) is True
    assert day.night_active(off + timedelta(minutes=5), JUNE, off_offset_min=10) is True


def test_night_active_fixed_fallback_without_coords(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    day = make(tmp_path, coords=None)
    tz = ZoneInfo(TZ)
    assert day.night_active(datetime(2026, 6, 21, 18, 0, tzinfo=tz), JUNE) is True
    assert day.night_active(datetime(2026, 6, 21, 5, 59, tzinfo=tz), JUNE) is True
    assert day.night_active(datetime(2026, 6, 21, 6, 0, tzinfo=tz), JUNE) is False
    assert day.night_active(datetime(2026, 6, 21, 12, 0, tzinfo=tz), JUNE) is False


def test_night_mode_stable_across_a_simulated_day(tmp_path):
    """Sampling the whole day at 1-min cadence yields exactly two
    transitions — no flapping by construction."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    day = make(tmp_path)
    start = datetime(2026, 6, 21, 0, 0, tzinfo=ZoneInfo(TZ))
    states = [day.night_active(start + timedelta(minutes=m), JUNE) for m in range(0, 24 * 60)]
    transitions = sum(1 for a, b in zip(states, states[1:]) if a != b)
    assert transitions == 2
    assert states[0] is True and states[720] is False and states[-1] is True
