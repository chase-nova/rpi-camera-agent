"""Tests for agent.uploader — offline spill, probe, replay, resync (design 12 §3–§4, phase 5.5)."""

import io
import json
import ssl
import urllib.error
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from agent.cache import SpillStore
from agent.capture import CaptureItem
from agent.clock import RESYNC_HOLDOVER, AgentClock, Resync
from agent.config import Settings
from agent.uploader import Uploader, is_transport_error

TZ = ZoneInfo("Asia/Seoul")
T0 = datetime(2026, 8, 13, 3, 0, 0, tzinfo=UTC)  # 12:00 KST
DATE_HEADER = "Thu, 13 Aug 2026 03:00:37 GMT"  # T0 + 37 s
DEFAULT_SIGN = {"status": "ok", "url": "https://s3.example/put", "key": "signed/key.jpg"}


class Mono:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


class DateResponse(io.BytesIO):
    headers = {"Date": DATE_HEADER}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class NetHttp:
    """urlopen stand-in: scripted /sign failures, Date header, recorded PUTs."""

    def __init__(self, failures=(), sign_answer=None):
        self.failures = list(failures)
        self.sign_answer = sign_answer or DEFAULT_SIGN
        self.sign_requests = []
        self.put_requests = []

    def __call__(self, request, timeout=None):
        if request.full_url.endswith("/sign"):
            self.sign_requests.append(json.loads(request.data))
            if self.failures:
                raise self.failures.pop(0)
            return DateResponse(json.dumps(self.sign_answer).encode())
        self.put_requests.append(request)
        return DateResponse(b"")


def url_error():
    return urllib.error.URLError("dns failed")


def http_500():
    return urllib.error.HTTPError(
        "https://signer.example/sign", 500, "boom", None, io.BytesIO(b"")
    )


def settings(tmp_path, **kw):
    fields = dict(
        stage="test", location_id="TEST", device_id="dam-test", timezone="Asia/Seoul",
        upload_signer_url="https://signer.example", device_token="tok", queue_max=8,
        cache_dir=str(tmp_path), offline_after_failures=3, offline_probe_s=30,
        replay_min_gap_s=1.0, anchor_refresh_s=600,
    )
    fields.update(kw)
    return Settings(**fields)


def store(tmp_path, boot_id="boot-A"):
    return SpillStore(
        tmp_path / "spill", max_frames=100, min_free_mb=0, max_age_days=30,
        boot_id=boot_id, timezone="Asia/Seoul", disk_free=lambda p: 10 ** 12,
    )


def make_uploader(tmp_path, http, mono, *, anchored=True, system_utc=T0, **kw):
    clock = AgentClock(TZ, None, refresh_s=600, monotonic=mono,
                       system_utc=lambda: system_utc, boot_id="boot-A")
    if anchored:
        clock.observe_trusted(T0, mono.value, "ntp")
    up = Uploader(settings(tmp_path, **kw), urlopen=http, sleep=lambda s: None, monotonic=mono)
    up.clock = clock.now_local
    up.agent_clock = clock
    up.store = store(tmp_path)
    return up, clock


def frame(mono, n=0, quality="synced"):
    ts = (T0 + timedelta(seconds=n)).astimezone(TZ)
    return CaptureItem(
        jpeg=b"\xff\xd8" + bytes([n]) + b"\xff\xd9", captured_at=ts,
        ulid=f"01ARZ3NDEKTSV4RRFFQ69G5F{n:02d}", key=f"images/TEST/2026-08-13/x{n}.jpg",
        camera_metadata={}, captured_mono=mono.value, time_quality=quality,
    )


# ── classification + state machine ──────────────────────────────────────────

def test_transport_error_classification():
    assert is_transport_error(url_error())
    assert is_transport_error(TimeoutError())
    assert is_transport_error(ConnectionResetError())
    assert is_transport_error(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))
    assert not is_transport_error(http_500())
    assert not is_transport_error(KeyError("url"))


def test_transport_failures_flip_offline_and_spill(tmp_path):
    http = NetHttp(failures=[url_error(), url_error(), url_error()])
    mono = Mono()
    up, clock = make_uploader(tmp_path, http, mono)
    assert up.process(frame(mono, 0)) is False
    assert up.net_state == "offline" and up.offline_since is not None
    assert len(up.store) == 1 and up.counters()["failed_attempts"] == 3
    assert clock.now()[1] == "holdover"  # the clock was told we are offline
    signs = len(http.sign_requests)
    assert up.process(frame(mono, 1)) is False  # spilled without touching the network
    assert len(up.store) == 2 and len(http.sign_requests) == signs
    c = up.counters()
    assert c["net_state"] == "offline" and c["cache_frames"] == 2 and c["replay_pending"] == 2


def test_http_errors_keep_retrying_and_never_go_offline(tmp_path):
    http = NetHttp(failures=[http_500(), http_500(), http_500(), http_500()])
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    assert up.process(frame(mono, 0)) is True
    assert up.net_state == "online" and len(up.store) == 0
    assert up.counters()["failed_attempts"] == 4


def test_captive_portal_certificate_error_counts_as_transport(tmp_path):
    err = ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED: Hostname mismatch")
    http = NetHttp(failures=[err, err, err])
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    up.process(frame(mono, 0))
    assert up.net_state == "offline"


# ── probe + replay ───────────────────────────────────────────────────────────

def test_probe_cadence_reconnect_and_replay_with_late_sidecar(tmp_path):
    http = NetHttp(
        failures=[url_error(), url_error(), url_error()],
        sign_answer={**DEFAULT_SIGN, "key": "images/TEST/2026-08-13/120000000.jpg",
                     "sidecar_url": "https://s3.example/side", "sidecar_key": "s.json"},
    )
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    up.process(frame(mono, 0))
    assert up.net_state == "offline"
    n = len(http.sign_requests)
    mono.value += 10
    up.tick(poll_s=0)  # too early for a probe
    assert len(http.sign_requests) == n and up.net_state == "offline"
    mono.value += 25  # ≥ OFFLINE_PROBE_S since going offline
    up.tick(poll_s=0)  # probe succeeds → online, and the empty queue lets replay start at once
    assert up.net_state == "online" and up.offline_since is None
    assert len(up.store) == 0
    c = up.counters()
    assert c["replayed"] == 1 and c["uploaded"] == 1 and c["replay_pending"] == 0
    frame_put, sidecar_put = http.put_requests[-2:]
    assert frame_put.data == b"\xff\xd8\x00\xff\xd9"
    sidecar = json.loads(sidecar_put.data)
    assert sidecar["late"] is True and sidecar["time_quality"] == "synced"


def test_live_frames_preempt_replay_and_min_gap_is_respected(tmp_path):
    http = NetHttp()
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    up.store.put(frame(mono, 1, quality="holdover"))
    up.store.put(frame(mono, 2, quality="holdover"))
    up.submit(frame(mono, 9))
    up.tick(poll_s=0)  # live item first
    assert up.counters()["uploaded"] == 1 and len(up.store) == 2
    up.tick(poll_s=0)  # queue empty → one replay
    assert len(up.store) == 1
    up.tick(poll_s=0)  # same instant: min gap not elapsed
    assert len(up.store) == 1
    mono.value += 1.0
    up.tick(poll_s=0)
    assert len(up.store) == 0 and up.counters()["replayed"] == 2


def test_replay_interleaves_after_every_nth_live_frame(tmp_path):
    """A permanently backed-up live queue must not freeze the cache: one
    replay frame is interleaved after every REPLAY_INTERLEAVE_EVERY live
    frames (field evidence 2026-09-06 — 565 frames frozen on 92-1)."""
    from agent.constants import REPLAY_INTERLEAVE_EVERY

    http = NetHttp()
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    up.store.put(frame(mono, 1, quality="holdover"))
    up.store.put(frame(mono, 2, quality="holdover"))
    for n in range(REPLAY_INTERLEAVE_EVERY - 1):  # live pressure, not yet Nth
        up.submit(frame(mono, 10 + n))
        up.tick(poll_s=0)
        mono.value += 2.0
    assert len(up.store) == 2  # live-first still holds below the threshold
    up.submit(frame(mono, 20))
    up.tick(poll_s=0)  # Nth live frame → one replay rides along
    assert len(up.store) == 1
    assert up.counters()["replayed"] == 1
    assert up.counters()["uploaded"] == REPLAY_INTERLEAVE_EVERY + 1


def test_mid_replay_transport_failure_goes_offline_and_keeps_the_frame(tmp_path):
    http = NetHttp(failures=[url_error(), url_error(), url_error()])
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    up.store.put(frame(mono, 1, quality="holdover"))
    for _ in range(3):
        mono.value += 1.0
        up.tick(poll_s=0)
    assert up.net_state == "offline" and len(up.store) == 1


def test_replay_paused_answer_deletes_the_cached_frame(tmp_path):
    http = NetHttp(sign_answer={"status": "paused"})
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    up.store.put(frame(mono, 1, quality="holdover"))
    up.tick(poll_s=0)
    assert len(up.store) == 0 and up.counters()["skipped"] == 1 and up.net_state == "online"


# ── clock corrections ────────────────────────────────────────────────────────

def test_provisional_frame_is_spilled_then_corrected_by_the_signer_date(tmp_path):
    http = NetHttp()
    mono = Mono()
    # system clock 37 s behind the internet → provisional stamps are 37 s early
    up, clock = make_uploader(tmp_path, http, mono, anchored=False, system_utc=T0)
    local, quality = clock.now()
    assert quality == "provisional"
    item = CaptureItem(
        jpeg=b"\xff\xd8p\xff\xd9", captured_at=local, ulid="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        key="images/TEST/x.jpg", camera_metadata={}, captured_mono=mono.value,
        time_quality="provisional",
    )
    assert up.process(item) is False  # spilled, never uploaded
    assert len(up.store) == 1 and http.put_requests == []
    # the heartbeat's Date header anchored the clock and corrected the frame
    assert clock.now()[1] == "synced" and clock.time_source == "signer"
    cached = next(iter(up.store.iter_oldest()))
    assert cached.time_quality == "holdover"
    assert cached.captured_utc == T0 + timedelta(seconds=37)
    up.tick(poll_s=0)  # replay under the corrected stamp
    assert len(up.store) == 0
    assert http.sign_requests[-1]["filename"] == "120037000.jpg"  # 12:00:37 KST
    assert http.sign_requests[-1]["date"] == "2026-08-13"


def test_holdover_resync_distributes_drift_linearly(tmp_path):
    mono = Mono()
    up, _ = make_uploader(tmp_path, NetHttp(), mono)
    up.net_state = "offline"
    stamps = []
    for m in (1000.0, 1500.0, 2000.0):
        mono.value = m
        item = frame(mono, int(m // 100), quality="holdover")  # distinct ULIDs: 10, 15, 20
        up.store.put(item)
        stamps.append(item.captured_at.astimezone(UTC))
    corrected = up.apply_resync(Resync(RESYNC_HOLDOVER, "signer", 0.0, 4.0, 1000.0, 2000.0))
    assert corrected == 2  # the frame at the anchor itself needs no correction
    got = [f.captured_utc for f in up.store.iter_oldest()]
    assert got == [stamps[0], stamps[1] + timedelta(seconds=2), stamps[2] + timedelta(seconds=4)]


def test_older_boot_provisional_frames_follow_keep_provisional(tmp_path):
    mono = Mono()
    store(tmp_path, boot_id="boot-OLD").put(frame(mono, 5, quality="provisional"))
    http = NetHttp(sign_answer={**DEFAULT_SIGN, "sidecar_url": "https://s3.example/side"})
    up, _ = make_uploader(tmp_path, http, mono)  # re-read under boot-A
    assert len(up.store) == 1
    up.tick(poll_s=0)
    assert len(up.store) == 0 and up.counters()["replayed"] == 1
    assert json.loads(http.put_requests[-1].data)["time_quality"] == "provisional"  # flagged

    store(tmp_path, boot_id="boot-OLD").put(frame(mono, 6, quality="provisional"))
    up2, _ = make_uploader(tmp_path, NetHttp(), mono, cache_keep_provisional=False)
    up2.tick(poll_s=0)
    assert len(up2.store) == 0
    assert up2.counters()["skipped"] == 1 and up2.counters()["uploaded"] == 0


# ── design 13: Wi-Fi requests ride the /sign answer ─────────────────────────

class FakeWifi:
    def __init__(self):
        self.handled = []

    def handle(self, request):
        self.handled.append(request)
        return True

    def status(self):
        return {"wifi_ssid": "HomeNet"}


def test_wifi_request_in_sign_answer_is_handled_on_the_uploader_thread(tmp_path):
    http = NetHttp(sign_answer={**DEFAULT_SIGN, "wifi": {"id": "req-1", "scan": True}})
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    up.wifi = FakeWifi()
    assert up.process(frame(mono, 0)) is True  # the answer stashed the request
    assert up.wifi.handled == []               # not run inside the upload
    signs = len(http.sign_requests)
    up.tick(poll_s=0)                          # executed by the scheduler …
    assert up.wifi.handled == [{"id": "req-1", "scan": True}]
    assert len(http.sign_requests) == signs + 1  # … followed by a forced heartbeat
    assert up.counters()["wifi_ssid"] == "HomeNet"


def test_wifi_request_on_409_unassigned_is_still_handled(tmp_path):
    body = json.dumps({"error": "unassigned", "wifi": {"id": "req-2", "apply": {"ssid": "S", "psk": "p"}}}).encode()
    err = urllib.error.HTTPError("https://signer.example/sign", 409, "conflict", None, io.BytesIO(body))
    http = NetHttp(failures=[err])
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    up.wifi = FakeWifi()
    assert up.process(frame(mono, 0)) is True  # skipped (unassigned), not a failure
    up.tick(poll_s=0)
    assert up.wifi.handled[0]["id"] == "req-2"


def test_coords_in_sign_answer_are_stashed(tmp_path):
    http = NetHttp(sign_answer={**DEFAULT_SIGN,
                                "coords": {"latitude": 40.7127, "longitude": -74.0134}})
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    assert up.coords is None
    assert up.process(frame(mono, 0)) is True
    assert up.coords == (40.7127, -74.0134)
    # malformed coords never overwrite good ones
    http.sign_answer = {**DEFAULT_SIGN, "coords": {"latitude": "x"}}
    up.process(frame(mono, 1))
    assert up.coords == (40.7127, -74.0134)


# ── asymmetric-link offline detection (fix of 2026-09-02, found on 92-1) ────

class AsymmetricHttp(NetHttp):
    """Signer answers fine; every bulk PUT dies (write timeout) — the
    pathological link of dam-imx462-92-1 (2026-09-01)."""

    MAX_PUTS = 12  # convert an infinite retry regression into a test failure

    def __call__(self, request, timeout=None):
        if request.full_url.endswith("/sign"):
            return super().__call__(request, timeout)
        self.put_requests.append(request)
        assert len(self.put_requests) <= self.MAX_PUTS, "never went offline"
        raise TimeoutError("The write operation timed out")


def test_asymmetric_link_still_goes_offline_and_spills(tmp_path):
    """Sign successes must NOT reset the failure counter: three PUT
    timeouts tip us offline even though every /sign works."""
    http = AsymmetricHttp()
    mono = Mono()
    up, clock = make_uploader(tmp_path, http, mono)
    assert up.process(frame(mono, 0)) is False
    assert up.net_state == "offline"
    assert len(up.store) == 1  # spilled, not stuck retrying
    assert len(http.put_requests) == 3  # OFFLINE_AFTER_FAILURES
    # later frames spill straight to disk without touching the network
    signs = len(http.sign_requests)
    assert up.process(frame(mono, 1)) is False
    assert len(up.store) == 2 and len(http.sign_requests) == signs


def test_probe_flips_online_but_asymmetric_puts_tip_back_offline(tmp_path):
    http = AsymmetricHttp()
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    up.process(frame(mono, 0))  # -> offline, 1 spilled
    mono.value += 35  # past OFFLINE_PROBE_S
    up.tick(poll_s=0)  # probe succeeds -> online; first replay PUT fails
    assert up.net_state == "online"  # one failure is not yet proof of loss
    for _ in range(3):  # further replay attempts accumulate PUT failures
        mono.value += 2
        up.tick(poll_s=0)
    assert up.net_state == "offline"  # tipped back by PUT failures alone
    assert len(up.store) == 1  # the replayed frame went back to the store


def test_heartbeat_success_does_not_prove_the_link(tmp_path):
    http = AsymmetricHttp()
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    up.process(frame(mono, 0))  # -> offline
    up.send_heartbeat(force=True)  # sign-only, succeeds
    assert up.net_state == "offline"  # still offline: no PUT proof


def test_full_put_success_marks_online_and_resets_counter(tmp_path):
    http = NetHttp(failures=[url_error(), url_error()])  # 2 sign failures
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    assert up.process(frame(mono, 0)) is True  # 3rd attempt: sign+PUT succeed
    assert up.net_state == "online"
    assert up._consecutive_transport_failures == 0


# ── optional spill cache (control.cache_enabled, 2026-09-02) ────────────────

def test_cache_disabled_never_goes_offline_and_never_spills(tmp_path):
    http = NetHttp(failures=[url_error()] * 5)
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    up.cache_enabled = False
    up._sleep = lambda s: up._stop.set()  # break the retry-forever loop
    assert up.process(frame(mono, 0)) is False
    assert up.net_state == "online"  # offline never trips without the cache
    assert len(up.store) == 0        # nothing written to disk


def test_cache_disabled_drops_provisional_as_skipped(tmp_path):
    http = NetHttp()
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    up.cache_enabled = False
    assert up.process(frame(mono, 0, quality="provisional")) is False
    assert len(up.store) == 0
    assert up.counters()["skipped"] == 1


def test_cache_disabled_still_drains_existing_frames(tmp_path):
    http = NetHttp()
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    up.store.put(frame(mono, 1, quality="holdover"))
    up.cache_enabled = False
    up.tick(poll_s=0)  # queue empty -> replay still runs
    assert len(up.store) == 0 and up.counters()["replayed"] == 1


def test_cache_enabled_adopted_from_the_answer(tmp_path):
    http = NetHttp(sign_answer={**DEFAULT_SIGN, "cache_enabled": False})
    mono = Mono()
    up, _ = make_uploader(tmp_path, http, mono)
    assert up.cache_enabled is True
    up.process(frame(mono, 0))
    assert up.cache_enabled is False
    http.sign_answer = {**DEFAULT_SIGN, "cache_enabled": True}
    up.process(frame(mono, 1))
    assert up.cache_enabled is True
