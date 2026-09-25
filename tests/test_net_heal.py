"""Tests for agent.net_heal — network self-healing watchdog (design 12 §3)."""

from agent.net_heal import NetworkHealer


class FakeMono:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        self.calls.append(list(cmd))

        class R:
            returncode = 0
        return R()


def make(kick=600, reboot=3600):
    mono = FakeMono()
    runner = FakeRunner()
    healer = NetworkHealer(kick, reboot, runner=runner, monotonic=mono, sleep=lambda s: None)
    return healer, mono, runner


def test_quiet_before_kick_window_does_nothing():
    healer, mono, runner = make()
    mono.value += 599
    healer.check()
    assert runner.calls == [] and healer.kicks == 0


def test_kick_bounces_the_radio_and_respects_spacing():
    healer, mono, runner = make()
    mono.value += 600
    healer.check()
    assert healer.kicks == 1
    assert runner.calls[0] == ["sudo", "-n", "nmcli", "radio", "wifi", "off"]
    assert runner.calls[1] == ["sudo", "-n", "nmcli", "radio", "wifi", "on"]
    healer.check()  # same instant: no re-kick
    assert healer.kicks == 1
    mono.value += 600  # another full window of silence -> kick again
    healer.check()
    assert healer.kicks == 2


def test_success_resets_the_silence_clock():
    healer, mono, runner = make()
    mono.value += 599
    healer.note_success()
    mono.value += 599  # 1198 s total, but only 599 since last success
    healer.check()
    assert healer.kicks == 0


def test_reboot_after_the_long_window_then_backs_off():
    healer, mono, runner = make()
    mono.value += 3600
    healer.check()
    assert healer.reboots == 1
    assert runner.calls[-1] == ["sudo", "-n", "/usr/sbin/reboot"]
    healer.check()  # a failed reboot must not spin: full window again
    assert healer.reboots == 1
    mono.value += 3600
    healer.check()
    assert healer.reboots == 2


def test_zero_disables_each_stage():
    healer, mono, runner = make(kick=0, reboot=0)
    mono.value += 10 ** 6
    healer.check()
    assert runner.calls == [] and healer.kicks == 0 and healer.reboots == 0
    kick_only, mono2, runner2 = make(kick=300, reboot=0)
    mono2.value += 10 ** 6
    kick_only.check()
    assert kick_only.kicks == 1 and kick_only.reboots == 0


def test_command_failure_never_raises():
    def broken(cmd, **kw):
        raise FileNotFoundError("nmcli")
    mono = FakeMono()
    healer = NetworkHealer(600, 0, runner=broken, monotonic=mono, sleep=lambda s: None)
    mono.value += 600
    healer.check()  # logs, no exception
    assert healer.kicks == 1
