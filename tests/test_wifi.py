"""Tests for agent.wifi — remote Wi-Fi setup (design 13 §3), fake nmcli."""

import logging
import subprocess
from datetime import UTC, datetime

from agent.wifi import WifiManager

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

SCAN_LIST = "\n".join([
    "*:HomeNet:70:WPA2:2462 MHz",
    ":SiteWifi:81:WPA2:5180 MHz",
    ":SiteWifi:63:WPA2:2437 MHz",      # same SSID, weaker: de-duplicated
    "::40:WPA2:2412 MHz",              # hidden: dropped
    ":Cafe\\:Guest:55::2437 MHz",      # escaped colon, open network
])


class Result:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeNmcli:
    """Scripted nmcli: `responses` maps a command-prefix tuple to a Result
    (or a list of Results consumed in order); records every invocation."""

    def __init__(self, responses=None, active_ssid="HomeNet"):
        self.responses = dict(responses or {})
        self.calls = []
        self.active = active_ssid

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        self.calls.append(list(cmd))
        args = cmd[3:] if cmd[:2] == ["sudo", "-n"] else cmd[1:]
        if args[:3] == ["-t", "-f", "ACTIVE,SSID"]:
            return Result(0, f"yes:{self.active}\nno:Other\n" if self.active else "no:Other\n")
        if args[:2] == ["-t", "-f"]:
            args = args[3:]  # drop the field selector; match on the verb
        for prefix, response in self.responses.items():
            if tuple(args[: len(prefix)]) == prefix:
                if isinstance(response, list):
                    return response.pop(0) if len(response) > 1 else response[0]
                return response
        return Result(0, "")


def manager(tmp_path, fake, **kw):
    defaults = dict(apply_timeout_s=45, fallback_s=30, runner=fake, sleep=lambda s: None, now=lambda: NOW)
    defaults.update(kw)
    return WifiManager(tmp_path / "wifi-ack.json", **defaults)


# ── scan ─────────────────────────────────────────────────────────────────────

def test_scan_parses_dedupes_and_sorts(tmp_path):
    fake = FakeNmcli({("device", "wifi", "list"): Result(0, SCAN_LIST)})
    result = manager(tmp_path, fake).scan("req-1")
    assert result["id"] == "req-1" and result["at"] == NOW.isoformat()
    assert [n["ssid"] for n in result["networks"]] == ["SiteWifi", "HomeNet", "Cafe:Guest"]
    site = result["networks"][0]
    assert site == {"ssid": "SiteWifi", "signal": 81, "security": "WPA2", "band": "5", "in_use": False}
    assert result["networks"][1]["in_use"] is True
    assert result["networks"][2]["security"] == "open"
    assert "warning" not in result
    assert any(c[1:4] == ["device", "wifi", "rescan"] for c in fake.calls)


def test_scan_reports_rescan_denial_as_warning_and_still_lists(tmp_path):
    fake = FakeNmcli({
        ("device", "wifi", "rescan"): Result(1, "", "Error: not authorized."),
        ("device", "wifi", "list"): Result(0, SCAN_LIST),
    })
    result = manager(tmp_path, fake).scan("req-2")
    assert len(result["networks"]) == 3 and "rescan failed" in result["warning"]


def test_missing_nmcli_is_graceful(tmp_path):
    def broken(cmd, **kw):
        raise FileNotFoundError("nmcli")
    m = manager(tmp_path, broken)
    assert m.current_ssid() is None
    assert m.handle({"id": "req-3", "scan": True}) is True
    result = m.last_scan
    assert result["networks"] == [] and "list failed" in result["warning"]
    assert m.status() == {"wifi_scan": result}  # no wifi_ssid without nmcli


# ── apply ────────────────────────────────────────────────────────────────────

def test_apply_adds_profile_and_switches(tmp_path, caplog):
    fake = FakeNmcli({("connection", "up"): Result(0, "Connection successfully activated")})
    with caplog.at_level(logging.INFO):
        outcome = manager(tmp_path, fake).apply("req-4", {"ssid": "SiteWifi", "psk": "s3cret-pw"})
    assert outcome["ok"] is True and outcome["ssid"] == "SiteWifi" and outcome["error"] is None
    add = next(c for c in fake.calls if c[1:3] == ["connection", "add"])
    assert add[1:] == [
        "connection", "add", "type", "wifi", "con-name", "SiteWifi", "ifname", "wlan0",
        "ssid", "SiteWifi", "connection.autoconnect", "yes",
        "connection.autoconnect-retries", "0", "ipv4.dns", "1.1.1.1 8.8.8.8",
        "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", "s3cret-pw",
    ]
    assert fake.calls[0][1:] == ["-t", "-f", "ACTIVE,SSID", "device", "wifi", "list"]  # previous ssid
    assert any(c[1:4] == ["connection", "delete", "SiteWifi"] for c in fake.calls)  # replace
    assert "s3cret-pw" not in caplog.text


def test_apply_open_and_hidden_network_shape(tmp_path):
    fake = FakeNmcli({("connection", "up"): Result(0)})
    manager(tmp_path, fake).apply("req-5", {"ssid": "Open", "hidden": True})
    add = next(c for c in fake.calls if c[1:3] == ["connection", "add"])
    assert "wifi-sec.key-mgmt" not in add and add[-2:] == ["802-11-wireless.hidden", "yes"]


def test_apply_failure_falls_back_and_deletes_profile(tmp_path, caplog):
    fake = FakeNmcli({
        ("connection", "up", "SiteWifi"): Result(4, "", "Error: Connection activation failed: (7) Secrets were required, but not provided."),
    })
    m = manager(tmp_path, fake)
    fake.active = None  # after the failed switch nothing is active …
    with caplog.at_level(logging.INFO):
        outcome = m.apply("req-6", {"ssid": "SiteWifi", "psk": "wrong-pw"})
    assert outcome["ok"] is False and "Secrets were required" in outcome["error"]
    assert "wrong-pw" not in caplog.text and "wrong-pw" not in str(outcome)
    ups = [c for c in fake.calls if c[1:3] == ["connection", "up"]]
    assert ups[0][3] == "SiteWifi"
    deletes = [c for c in fake.calls if c[1:3] == ["connection", "delete"]]
    assert deletes[-1][3] == "SiteWifi"  # failed profile removed


def test_apply_timeout_counts_as_failure(tmp_path):
    def slow(cmd, **kw):
        if cmd[1:3] == ["connection", "up"]:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return Result(0, "yes:HomeNet\n")
    outcome = manager(tmp_path, slow).apply("req-7", {"ssid": "SiteWifi", "psk": "x"})
    assert outcome["ok"] is False and "timeout" in outcome["error"]


def test_sudo_fallback_on_insufficient_privileges(tmp_path):
    fake = FakeNmcli({
        ("connection", "add"): [Result(1, "", "Error: Failed to add 'SiteWifi' connection: Insufficient privileges"), Result(0)],
        ("connection", "up"): Result(0),
    })
    outcome = manager(tmp_path, fake).apply("req-8", {"ssid": "SiteWifi", "psk": "x"})
    assert outcome["ok"] is True
    adds = [c for c in fake.calls if "add" in c]
    assert adds[0][0] == "nmcli" and adds[1][:3] == ["sudo", "-n", "nmcli"]


# ── handle / idempotence ─────────────────────────────────────────────────────

def test_handle_runs_each_request_once_and_persists_acks(tmp_path):
    fake = FakeNmcli({("device", "wifi", "list"): Result(0, SCAN_LIST), ("connection", "up"): Result(0)})
    m = manager(tmp_path, fake)
    assert m.handle({"id": "s1", "scan": True}) is True
    assert m.handle({"id": "s1", "scan": True}) is False  # same id: no-op
    assert m.handle({"id": "s2", "scan": True}) is True   # new id: runs
    assert m.handle({"id": "a1", "apply": {"ssid": "SiteWifi", "psk": "x"}}) is True
    assert m.handle({"id": "a1", "apply": {"ssid": "SiteWifi", "psk": "x"}}) is False
    assert m.handle({"id": "x", "apply": {}}) is False and m.handle("junk") is False
    again = manager(tmp_path, FakeNmcli())  # restart: acks persisted
    assert again.handle({"id": "a1", "apply": {"ssid": "SiteWifi", "psk": "x"}}) is False
    assert again.handle({"id": "s2", "scan": True}) is False


def test_status_reports_ssid_and_last_results(tmp_path):
    fake = FakeNmcli({("device", "wifi", "list"): Result(0, SCAN_LIST), ("connection", "up"): Result(0)})
    m = manager(tmp_path, fake)
    assert m.status() == {"wifi_ssid": "HomeNet"}
    m.handle({"id": "s1", "scan": True})
    m.handle({"id": "a1", "apply": {"ssid": "SiteWifi", "psk": "x"}})
    fake.active = "SiteWifi"
    status = m.status()
    assert status["wifi_ssid"] == "SiteWifi"
    assert status["wifi_scan"]["id"] == "s1" and status["wifi_applied"]["id"] == "a1"
