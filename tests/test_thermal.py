"""Tests for agent.thermal — state machine with a fake sensor."""

from agent.config import Settings
from agent.thermal import ThermalMonitor

BASE = dict(
    stage="test",
    device_id="dam-test",
    timezone="Asia/Seoul",
    upload_signer_url="https://signer.example",
    device_token="tok",
)


def monitor(temps, *, shutdown_enabled=False):
    settings = Settings(**BASE, temp_shutdown_enabled=shutdown_enabled)
    seq = iter(temps)
    return ThermalMonitor(
        settings, read_temp=lambda: next(seq), read_throttle=lambda: "0x0"
    )


def states(temps, **kwargs):
    m = monitor(temps, **kwargs)
    return [m.check().state for _ in temps]


def test_ok_warn_pause_progression():
    assert states([60.0, 75.0, 79.9, 80.0]) == ["ok", "warn", "warn", "paused"]


def test_pause_holds_until_resume_threshold():
    # resume at <= 77.5: 78 stays paused; 77.5 resumes (still warm enough
    # for the warn badge, since warn >= 75); fully ok below warn
    assert states([80.0, 79.0, 78.0, 77.5, 60.0]) == [
        "paused", "paused", "paused", "warn", "ok"
    ]


def test_no_sensor_means_ok():
    assert states([None, None]) == ["ok", "ok"]


def test_shutdown_needs_three_consecutive_and_enabled():
    m = monitor([86.0, 86.0, 86.0], shutdown_enabled=True)
    assert [m.check().should_shutdown for _ in range(3)] == [False, False, True]


def test_shutdown_streak_resets_on_dip():
    m = monitor([86.0, 86.0, 80.0, 86.0], shutdown_enabled=True)
    assert [m.check().should_shutdown for _ in range(4)] == [
        False, False, False, False
    ]


def test_shutdown_disabled_by_default():
    m = monitor([90.0, 90.0, 90.0, 90.0])
    assert all(not m.check().should_shutdown for _ in range(4))


def test_status_carries_temp_and_throttled():
    m = monitor([61.2])
    status = m.check()
    assert status.temp_c == 61.2
    assert status.throttled == "0x0"


def test_read_volt_core_parses_vcgencmd(monkeypatch):
    import subprocess

    from agent import thermal

    class FakeResult:
        stdout = "volt=1.3125V\n"

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: FakeResult()
    )
    assert thermal.read_volt_core() == 1.3125


def test_read_volt_core_none_when_unavailable(monkeypatch):
    import subprocess

    from agent import thermal

    def boom(*a, **k):
        raise OSError("no vcgencmd")

    monkeypatch.setattr(subprocess, "run", boom)
    assert thermal.read_volt_core() is None
