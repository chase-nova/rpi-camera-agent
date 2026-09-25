"""Tests for agent.uploader — queue overflow, retry/backoff, no network."""

import io
import json
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.capture import CaptureItem
from agent.config import Settings
from agent.uploader import Uploader

TZ = ZoneInfo("Asia/Seoul")

SETTINGS = Settings(
    stage="test",
    location_id="TEST",
    device_id="dam-test",
    timezone="Asia/Seoul",
    upload_signer_url="https://signer.example",
    device_token="tok",
    queue_max=2,
)


def make_item(second=0):
    ts = datetime(2026, 8, 13, 12, 0, second, tzinfo=TZ)
    return CaptureItem(
        jpeg=b"\xff\xd8jpeg",
        captured_at=ts,
        ulid=f"ULID{second:022d}",
        key=f"images/TEST/2026-08-13/1200{second:02d}000.jpg",
        camera_metadata={},
    )


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeHttp:
    """Callable standing in for urllib.request.urlopen."""

    def __init__(self, fail_signs=0, fail_puts=0, sign_answer=None):
        self.sign_requests = []
        self.put_requests = []
        self.fail_signs = fail_signs
        self.fail_puts = fail_puts
        self.sign_answer = sign_answer  # override /sign response dict or HTTPError

    def __call__(self, request, timeout=None):
        if request.full_url.endswith("/sign"):
            self.sign_requests.append(json.loads(request.data))
            if self.fail_signs > 0:
                self.fail_signs -= 1
                raise urllib.error.URLError("signer unreachable")
            if isinstance(self.sign_answer, Exception):
                raise self.sign_answer
            if self.sign_answer is not None:
                return FakeResponse(json.dumps(self.sign_answer).encode())
            return FakeResponse(
                json.dumps(
                    {
                        "status": "ok",
                        "url": "https://s3.example/put",
                        "key": "signed/key.jpg",
                    }
                ).encode()
            )
        self.put_requests.append(request)
        if self.fail_puts > 0:
            self.fail_puts -= 1
            raise urllib.error.HTTPError(
                request.full_url, 403, "expired", None, io.BytesIO(b"expired")
            )
        return FakeResponse(b"")


def make_uploader(http, sleeps=None):
    return Uploader(
        SETTINGS,
        urlopen=http,
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
    )


def test_submit_overflow_drops_oldest_never_blocks():
    uploader = make_uploader(FakeHttp())
    items = [make_item(s) for s in range(3)]
    for item in items:
        uploader.submit(item)
    assert uploader.queue_depth == 2
    assert uploader.counters()["dropped"] == 1
    # oldest (second=0) was dropped; head is second=1
    assert uploader._queue.get_nowait().ulid == items[1].ulid


def test_upload_success_sends_sign_body_and_put_headers():
    http = FakeHttp()
    uploader = make_uploader(http)
    assert uploader.process(make_item(5)) is True

    sign = http.sign_requests[0]
    assert sign["token"] == "tok"
    assert sign["date"] == "2026-08-13"
    assert sign["filename"] == "120005000.jpg"
    assert sign["content_type"] == "image/jpeg"
    assert sign["metadata"]["ulid"] == "ULID0000000000000000000005"
    assert sign["metadata"]["timezone"] == "Asia/Seoul"

    put = http.put_requests[0]
    assert put.data == b"\xff\xd8jpeg"
    assert put.get_header("Content-type") == "image/jpeg"
    assert put.get_header("X-amz-meta-device-id") == "dam-test"
    assert uploader.counters() == {
        "uploaded": 1,
        "dropped": 0,
        "skipped": 0,
        "failed_attempts": 0,
    }
    assert sign["device_id"] == "dam-test"
    assert "status" not in sign  # no status_fn attached in this test


def test_sign_body_includes_status_when_status_fn_set():
    http = FakeHttp()
    uploader = make_uploader(http)
    uploader.status_fn = lambda: {"uploaded": 42, "thermal_state": "ok"}
    uploader.process(make_item())
    assert http.sign_requests[0]["status"] == {
        "uploaded": 42, "thermal_state": "ok"
    }


def test_paused_answer_is_skip_not_retry():
    http = FakeHttp(sign_answer={"status": "paused"})
    sleeps = []
    uploader = make_uploader(http, sleeps)
    assert uploader.process(make_item()) is True
    assert uploader.counters()["skipped"] == 1
    assert uploader.counters()["uploaded"] == 0
    assert uploader.counters()["failed_attempts"] == 0
    assert sleeps == []  # no backoff
    assert len(http.sign_requests) == 1  # no retry
    assert http.put_requests == []


def test_shutdown_answer_calls_shutdown_fn_and_skips():
    http = FakeHttp(sign_answer={"status": "shutdown"})
    uploader = make_uploader(http)
    calls = []
    uploader.shutdown_fn = lambda: calls.append(1)
    assert uploader.process(make_item()) is True
    assert calls == [1]
    assert uploader.counters()["skipped"] == 1
    assert http.put_requests == []


def test_unassigned_409_is_skip_not_retry():
    error = urllib.error.HTTPError(
        "https://signer.example/sign", 409, "conflict", None,
        io.BytesIO(json.dumps({"error": "unassigned"}).encode()),
    )
    http = FakeHttp(sign_answer=error)
    sleeps = []
    uploader = make_uploader(http, sleeps)
    assert uploader.process(make_item()) is True
    assert uploader.counters()["skipped"] == 1
    assert sleeps == []


def test_other_409_still_retries():
    def fresh_error():
        return urllib.error.HTTPError(
            "https://signer.example/sign", 409, "conflict", None,
            io.BytesIO(json.dumps({"error": "something-else"}).encode()),
        )

    http = FakeHttp(sign_answer=fresh_error())
    uploader = Uploader(
        SETTINGS, urlopen=http, sleep=lambda s: uploader._stop.set()
    )
    assert uploader.process(make_item()) is False  # stopped mid-retry
    assert uploader.counters()["failed_attempts"] == 1


def test_heartbeat_signs_and_swallows_errors():
    http = FakeHttp(sign_answer={"status": "paused"})
    uploader = make_uploader(http)
    uploader.status_fn = lambda: {"thermal_state": "paused", "temp_c": 76.0}
    uploader.send_heartbeat()  # paused answer must not raise
    assert http.sign_requests[0]["status"]["thermal_state"] == "paused"

    broken = FakeHttp(fail_signs=1)
    uploader2 = make_uploader(broken)
    uploader2.send_heartbeat()  # network error must not raise
    assert broken.put_requests == []


def test_sign_failure_retries_with_backoff():
    http = FakeHttp(fail_signs=2)
    sleeps = []
    uploader = make_uploader(http, sleeps)
    assert uploader.process(make_item()) is True
    assert sleeps == [1.0, 2.0]  # exponential backoff
    assert len(http.sign_requests) == 3
    assert uploader.counters()["failed_attempts"] == 2
    assert uploader.counters()["uploaded"] == 1


def test_expired_put_gets_fresh_presign_on_retry():
    http = FakeHttp(fail_puts=1)
    sleeps = []
    uploader = make_uploader(http, sleeps)
    assert uploader.process(make_item()) is True
    assert len(http.sign_requests) == 2  # fresh presign after the 403 PUT
    assert len(http.put_requests) == 2
    assert sleeps == [1.0]


def test_backoff_caps_at_60s():
    http = FakeHttp(fail_signs=8)
    sleeps = []
    uploader = make_uploader(http, sleeps)
    assert uploader.process(make_item()) is True
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_stop_aborts_retry_loop():
    http = FakeHttp(fail_signs=1000)
    uploader = Uploader(SETTINGS, urlopen=http, sleep=lambda s: uploader._stop.set())
    assert uploader.process(make_item()) is False
    assert uploader.counters()["uploaded"] == 0


def test_build_sidecar_contents():
    from agent.uploader import build_sidecar
    item = make_item(7)
    item = CaptureItem(
        jpeg=item.jpeg, captured_at=item.captured_at, ulid=item.ulid,
        key=item.key,
        camera_metadata={
            "ExposureTime": 45999, "AnalogueGain": 4.0, "Lux": 0.8,
            "ScalerCrop": (0, 0, 1920, 1080),
            "ColourCorrectionMatrix": [1] * 9,  # not whitelisted
        },
    )
    sidecar = build_sidecar(item, {"device_id": "dam-test", "temp_c": 55.1})
    assert sidecar["ulid"] == item.ulid
    assert sidecar["image_bytes"] == len(item.jpeg)
    assert sidecar["camera_meta"]["ExposureTime"] == 45999
    assert sidecar["camera_meta"]["ScalerCrop"] == [0, 0, 1920, 1080]
    assert "ColourCorrectionMatrix" not in sidecar["camera_meta"]
    assert sidecar["status"]["temp_c"] == 55.1


def test_sidecar_uploaded_after_frame():
    http = FakeHttp(sign_answer={
        "status": "ok", "url": "https://s3.example/put",
        "key": "images/TEST/2026-08-13/120007000.jpg",
        "sidecar_url": "https://s3.example/put-json",
        "sidecar_key": "images/TEST/2026-08-13/120007000.json",
    })
    uploader = make_uploader(http)
    uploader.status_fn = lambda: {"device_id": "dam-test"}
    assert uploader.process(make_item(7))
    assert len(http.put_requests) == 2  # frame, then sidecar
    assert http.sign_requests[0]["sidecar"] is True
    sidecar_req = http.put_requests[1]
    assert sidecar_req.full_url == "https://s3.example/put-json"
    body = json.loads(sidecar_req.data)
    assert body["status"]["device_id"] == "dam-test"


def test_sidecar_failure_does_not_fail_frame():
    class SidecarFailingHttp(FakeHttp):
        def __call__(self, request, timeout=None):
            if request.full_url.endswith("put-json"):
                raise urllib.error.URLError("sidecar boom")
            return super().__call__(request, timeout)

    http = SidecarFailingHttp(sign_answer={
        "status": "ok", "url": "https://s3.example/put",
        "key": "images/TEST/2026-08-13/120008000.jpg",
        "sidecar_url": "https://s3.example/put-json",
    })
    uploader = make_uploader(http)
    assert uploader.process(make_item(8))  # frame still succeeds


def test_window_learned_from_sign_response():
    http = FakeHttp(sign_answer={
        "status": "ok", "url": "https://s3.example/put",
        "key": "images/TEST/2026-08-13/120009000.jpg",
        "window": {"start": "06:00", "end": "18:00"},
    })
    uploader = make_uploader(http)
    assert uploader.window == ("00:00", "00:00")  # default until taught
    uploader.process(make_item(9))
    assert uploader.window == ("06:00", "18:00")


def test_heartbeat_rate_limited():
    http = FakeHttp()
    uploader = make_uploader(http)
    uploader.send_heartbeat()
    uploader.send_heartbeat()  # immediately after - suppressed
    assert len(http.sign_requests) == 1
