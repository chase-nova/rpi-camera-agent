"""Tests for agent.clock — internet-anchored holdover clock (design 12 §4)."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from agent.clock import (
    QUALITY_HOLDOVER,
    QUALITY_PROVISIONAL,
    QUALITY_SYNCED,
    RESYNC_HOLDOVER,
    RESYNC_PROVISIONAL,
    SOURCE_NONE,
    SOURCE_NTP,
    SOURCE_SIGNER,
    AgentClock,
    Anchor,
    AnchorStore,
)

TZ = ZoneInfo("America/New_York")
T0 = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
REFRESH = 600.0


class FakeMono:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


def _clock(tmp_path, mono, system_utc=T0, boot_id="boot-A", store=True):
    return AgentClock(
        TZ,
        AnchorStore(tmp_path / "anchor.json") if store else None,
        refresh_s=REFRESH,
        monotonic=mono,
        system_utc=lambda: system_utc,
        boot_id=boot_id,
    )


# ── provisional → anchored ──────────────────────────────────────────────────

def test_starts_provisional_from_the_system_clock(tmp_path):
    mono = FakeMono(1000.0)
    clock = _clock(tmp_path, mono)
    local, quality = clock.now()
    assert quality == QUALITY_PROVISIONAL
    assert clock.time_source == SOURCE_NONE
    assert local == T0.astimezone(TZ)
    mono.value = 1048.0  # provisional time still advances with monotonic
    assert clock.now()[0] == (T0 + timedelta(seconds=48)).astimezone(TZ)


def test_first_trusted_reading_anchors_and_reports_the_provisional_offset(tmp_path):
    mono = FakeMono(1000.0)
    clock = _clock(tmp_path, mono)  # provisional estimate == T0 at mono 1000
    mono.value = 1100.0
    real = T0 + timedelta(seconds=100 + 37)  # system clock was 37 s behind
    resync = clock.observe_trusted(real, 1100.0, SOURCE_NTP)
    assert resync is not None and resync.kind == RESYNC_PROVISIONAL
    assert resync.offset_s == 37.0
    assert resync.correction_s(1050.0) == 37.0  # every provisional frame of this boot
    assert clock.now() == (real.astimezone(TZ), QUALITY_SYNCED)
    assert clock.time_source == SOURCE_NTP
    mono.value = 1148.0
    assert clock.now()[0] == (real + timedelta(seconds=48)).astimezone(TZ)


def test_quality_follows_online_state_and_anchor_age(tmp_path):
    mono = FakeMono(1000.0)
    clock = _clock(tmp_path, mono)
    clock.observe_trusted(T0, 1000.0, SOURCE_NTP)
    assert clock.now()[1] == QUALITY_SYNCED
    clock.set_online(False)
    assert clock.now()[1] == QUALITY_HOLDOVER
    clock.set_online(True)
    mono.value = 1000.0 + 2 * REFRESH + 1  # anchor went stale even though online
    assert clock.now()[1] == QUALITY_HOLDOVER


# ── holdover drift ──────────────────────────────────────────────────────────

def test_refresh_after_holdover_measures_drift_and_distributes_it_linearly(tmp_path):
    mono = FakeMono(1000.0)
    clock = _clock(tmp_path, mono)
    clock.observe_trusted(T0, 1000.0, SOURCE_NTP)
    # a day offline: the crystal ran 4 s slow over 86 400 s
    end = 1000.0 + 86_400.0
    real_end = T0 + timedelta(seconds=86_400 + 4)
    resync = clock.observe_trusted(real_end, end, SOURCE_SIGNER, force=True)
    assert resync is not None and resync.kind == RESYNC_HOLDOVER
    assert resync.drift_s == 4.0
    assert (resync.window_start_mono, resync.window_end_mono) == (1000.0, end)
    assert resync.correction_s(1000.0) == 0.0  # at the anchor: no drift yet
    assert resync.correction_s(1000.0 + 43_200.0) == 2.0  # halfway: half the drift
    assert resync.correction_s(end) == 4.0
    assert resync.correction_s(end + 5000) == 4.0  # clamped outside the window
    # the new anchor is the trusted reading; holdover continues from it
    mono.value = end + 48
    assert clock.now()[0] == (real_end + timedelta(seconds=48)).astimezone(TZ)


def test_zero_length_window_yields_no_correction():
    r = __import__("agent.clock", fromlist=["Resync"]).Resync(
        RESYNC_HOLDOVER, SOURCE_NTP, 0.0, 3.0, 500.0, 500.0
    )
    assert r.correction_s(500.0) == 0.0


def test_refresh_is_rate_limited_unless_forced(tmp_path):
    mono = FakeMono(1000.0)
    clock = _clock(tmp_path, mono)
    clock.observe_trusted(T0, 1000.0, SOURCE_NTP)
    assert clock.observe_trusted(T0 + timedelta(seconds=100), 1100.0, SOURCE_NTP) is None
    assert clock.anchor.mono == 1000.0  # unchanged
    assert clock.observe_trusted(T0 + timedelta(seconds=100), 1100.0, SOURCE_NTP, force=True)
    assert clock.anchor.mono == 1100.0
    assert clock.observe_trusted(T0 + timedelta(seconds=800), 1800.0, SOURCE_NTP)  # ≥ refresh


# ── persistence ─────────────────────────────────────────────────────────────

def test_anchor_persists_and_survives_an_agent_restart_in_the_same_boot(tmp_path):
    mono = FakeMono(1000.0)
    clock = _clock(tmp_path, mono)
    clock.observe_trusted(T0, 1000.0, SOURCE_NTP)
    saved = json.loads((tmp_path / "anchor.json").read_text())
    assert saved["boot_id"] == "boot-A" and saved["source"] == SOURCE_NTP

    mono.value = 5000.0  # same boot: monotonic continued while the agent restarted
    again = _clock(tmp_path, mono, system_utc=T0 - timedelta(days=2))  # garbage system clock
    assert again.anchor is not None
    # time is continuous (anchor + elapsed monotonic), never the system clock;
    # the anchor is 4 000 s old, so it counts as holdover until refreshed
    assert again.now() == ((T0 + timedelta(seconds=4000)).astimezone(TZ), QUALITY_HOLDOVER)
    mono.value = 1100.0
    assert _clock(tmp_path, mono).now()[1] == QUALITY_SYNCED  # fresh anchor → synced


def test_anchor_from_another_boot_seeds_a_provisional_clock(tmp_path):
    mono = FakeMono(1000.0)
    _clock(tmp_path, mono).observe_trusted(T0, 1000.0, SOURCE_NTP)
    # reboot while offline: monotonic restarts, fake-hwclock restored an old time
    mono.value = 30.0
    stale_system = T0 - timedelta(hours=3)
    rebooted = _clock(tmp_path, mono, system_utc=stale_system, boot_id="boot-B")
    assert rebooted.anchor is None
    local, quality = rebooted.now()
    assert quality == QUALITY_PROVISIONAL
    assert local == T0.astimezone(TZ)  # seeded from max(system clock, stored anchor)


def test_provisional_seed_prefers_a_later_system_clock(tmp_path):
    mono = FakeMono(1000.0)
    _clock(tmp_path, mono).observe_trusted(T0, 1000.0, SOURCE_NTP)
    later = T0 + timedelta(hours=5)
    rebooted = _clock(tmp_path, FakeMono(30.0), system_utc=later, boot_id="boot-B")
    assert rebooted.now()[0] == later.astimezone(TZ)


def test_corrupt_or_unwritable_store_never_raises(tmp_path):
    (tmp_path / "anchor.json").write_text("{not json")
    clock = _clock(tmp_path, FakeMono(1.0))
    assert clock.now()[1] == QUALITY_PROVISIONAL
    blocked = AnchorStore(tmp_path / "anchor.json" / "impossible" / "anchor.json")
    assert blocked.save(Anchor(T0, 0.0, "b", SOURCE_NTP)) is False
    clock2 = AgentClock(TZ, blocked, refresh_s=REFRESH, monotonic=FakeMono(1.0),
                        system_utc=lambda: T0, boot_id="b")
    assert clock2.observe_trusted(T0, 1.0, SOURCE_NTP) is not None  # persisted best-effort


# ── sources ─────────────────────────────────────────────────────────────────

class FakeRun:
    def __init__(self, stdout=None, exc=None):
        self.stdout = stdout
        self.exc = exc
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.exc:
            raise self.exc
        return type("R", (), {"stdout": self.stdout})()


def test_ntp_poll_anchors_when_synchronized_and_is_rate_limited(tmp_path):
    mono = FakeMono(1000.0)
    clock = _clock(tmp_path, mono)
    run = FakeRun(stdout="yes\n")
    assert clock.maybe_poll_ntp(runner=run) is not None
    assert clock.time_source == SOURCE_NTP and clock.now()[1] == QUALITY_SYNCED
    assert clock.maybe_poll_ntp(runner=run) is None  # within the refresh window
    assert run.calls == 1


def test_ntp_poll_ignores_unsynchronized_or_missing_timedatectl(tmp_path):
    clock = _clock(tmp_path, FakeMono(1000.0))
    assert clock.maybe_poll_ntp(runner=FakeRun(stdout="no\n")) is None
    clock2 = _clock(tmp_path, FakeMono(1000.0), store=False)
    assert clock2.maybe_poll_ntp(runner=FakeRun(exc=FileNotFoundError())) is None
    assert clock2.now()[1] == QUALITY_PROVISIONAL


def test_signer_date_header_is_a_trusted_source_unless_ntp_is_fresh(tmp_path):
    mono = FakeMono(1000.0)
    clock = _clock(tmp_path, mono)
    header = "Fri, 28 Aug 2026 12:05:00 GMT"
    resync = clock.observe_http_date(header, 1000.0)
    assert resync is not None and clock.time_source == SOURCE_SIGNER
    assert clock.now()[0] == datetime(2026, 8, 28, 12, 5, tzinfo=UTC).astimezone(TZ)
    assert clock.observe_http_date("garbage", 1001.0) is None
    assert clock.observe_http_date(None, 1001.0) is None
    # a fresh NTP anchor outranks the signer
    clock.observe_trusted(T0, 2000.0, SOURCE_NTP, force=True)
    assert clock.observe_http_date(header, 2100.0, force=True) is None
    assert clock.time_source == SOURCE_NTP
