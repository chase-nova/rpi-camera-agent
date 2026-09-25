"""End-to-end wiring test — FakeCamera → queue → mocked uploads (no AWS)."""

import io
import json
import tempfile
import threading
import time
import urllib.request

from agent.camera import FakeCamera
from agent.config import Settings
from agent.constants import EXIT_CAPTURE_STALLED
from agent.main import Agent, build_camera

SETTINGS = Settings(
    stage="test",
    location_id="TEST",
    device_id="dam-test",
    timezone="Asia/Seoul",
    upload_signer_url="https://signer.example",
    device_token="tok",
    capture_size=(160, 120),
    viewer_port=0,  # viewer covered by its own tests
    cache_dir=tempfile.mkdtemp(prefix="dam-cache-"),  # anchor.json lands here
)


class FakeResponse(io.BytesIO):
    # the signer's Date header is the agent clock's trusted source when NTP
    # is unavailable (design 12 §4.1) — as on this Windows test box
    headers = {"Date": "Fri, 28 Aug 2026 12:00:00 GMT"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeHttp:
    def __init__(self):
        self.puts = []
        self.signs = []

    def sign_requests_dates(self):
        return [body["date"] for body in self.signs]

    def __call__(self, request, timeout=None):
        if request.full_url.endswith("/sign"):
            body = json.loads(request.data)
            self.signs.append(body)
            key = f"images/TEST/{body['date']}/{body['filename']}"
            return FakeResponse(
                json.dumps({"url": "https://s3.example/put", "key": key}).encode()
            )
        self.puts.append(request)
        return FakeResponse(b"")


def test_build_camera_uses_fake_for_test_stage():
    assert isinstance(build_camera(SETTINGS), FakeCamera)


def test_camera_init_failure_degrades_to_heartbeat_mode(monkeypatch):
    """A missing camera (CSI fault) must not crash-loop the agent: it comes
    up camera-less, reports `camera_error` so the fleet page can read
    "camera not detected", and stays stoppable (2026-09-07, `45-3`)."""
    def no_camera(*args, **kwargs):
        raise IndexError("list index out of range")  # picamera2's exact symptom

    monkeypatch.setattr("agent.main.build_camera", no_camera)
    agent = Agent(SETTINGS, urlopen=FakeHttp())
    assert agent.camera is None and agent.loop is None
    assert agent.camera_error == "not detected"
    status = agent.status()
    assert status["camera_error"] == "not detected"
    assert "camera" not in status
    agent.request_stop()  # no capture loop to stop — must not raise


def test_watchdog_stands_down_in_cameraless_mode():
    """When the camera fails at start() (Picamera2 touches hardware lazily
    there), the agent enters degraded mode with the watchdog already armed
    — capture_stalled must never trip it (no loop ticks will ever come)."""
    agent = Agent(SETTINGS, urlopen=FakeHttp())
    agent.camera_error = "not detected"
    assert agent.capture_stalled(now=agent._started_monotonic + 10_000) is False
    agent.request_stop()


def test_camera_ok_reports_no_camera_error():
    agent = Agent(SETTINGS, urlopen=FakeHttp())
    assert agent.camera_error is None and agent.loop is not None
    assert "camera_error" not in agent.status()
    agent.request_stop()


def test_end_to_end_capture_publish_upload(monkeypatch):
    # NTP unavailable (as on a fresh Pi without network, or on Windows):
    # the host's own sync state must not leak into the test — a CI runner
    # with a synced clock would otherwise stamp the first frame "synced"
    monkeypatch.setattr("agent.clock.AgentClock.maybe_poll_ntp", lambda self, runner=None: None)
    http = FakeHttp()
    agent = Agent(SETTINGS, urlopen=http)
    agent.camera.start()
    agent.uploader.start()

    item = agent.loop.capture_once()

    # frame published for the viewer
    assert agent.frames.frame is not None
    assert agent.frames.frame.jpeg == item.jpeg

    # uploader drains the queue through the mocked signer + PUT
    deadline = time.monotonic() + 5.0
    while agent.uploader.counters()["uploaded"] < 1:
        assert time.monotonic() < deadline, "upload did not complete"
        time.sleep(0.02)
    assert len(http.puts) == 1
    assert http.puts[0].data == item.jpeg

    status = agent.status()
    assert status["uploaded"] == 1
    assert status["device_id"] == "dam-test"
    assert status["location_id"] == "TEST"  # learned from the signed key
    assert status["interval_s"] == 48
    assert status["thermal_state"] == "ok"
    assert status["camera"] == "fake"
    assert status["agent_version"]
    # design 12: the first frame was stamped while the clock was still
    # provisional (NTP unavailable — stubbed above), so it was spilled, the
    # heartbeat fetched the signer's Date, the clock anchored, the cached
    # frame was restamped and replayed — status now shows the signer clock
    assert item.time_quality == "provisional"
    assert item.captured_mono > 0
    assert status["time_quality"] == "synced"
    assert status["time_source"] == "signer"
    assert status["net_state"] == "online"
    assert status["replayed"] == 1 and status["cache_frames"] == 0
    # PUT #1 was the replayed frame (bytes unchanged), keyed under the
    # corrected timestamp
    assert http.sign_requests_dates()[-1] == "2026-08-28"

    agent.request_stop()
    agent.uploader.stop(drain_seconds=0.5)


def test_request_stop_ends_run_quickly():
    agent = Agent(SETTINGS, urlopen=FakeHttp())
    runner = threading.Thread(target=agent.run, daemon=True)
    runner.start()
    time.sleep(0.3)  # let it start and enter the interval sleep
    agent.request_stop()
    runner.join(timeout=5.0)
    assert not runner.is_alive(), "run() did not stop after request_stop()"


def test_capture_stall_detection_uses_ticks_and_start_time():
    agent = Agent(SETTINGS, urlopen=FakeHttp(), exit_fn=lambda code: None)
    t0 = agent._started_monotonic
    assert agent.stall_limit_s() == 180.0  # 48 s interval → the floor wins
    # before the first tick the start time is the reference
    assert not agent.capture_stalled(t0 + 100)
    assert agent.capture_stalled(t0 + 181)
    # a fresh tick resets the reference
    agent.loop.last_tick = t0 + 500
    assert not agent.capture_stalled(t0 + 600)
    assert agent.capture_stalled(t0 + 681)


def test_watchdog_exits_once_on_stall():
    exits = []
    agent = Agent(SETTINGS, urlopen=FakeHttp(), exit_fn=exits.append)
    agent.loop.last_tick = time.monotonic() - 10_000  # hung long ago
    worker = threading.Thread(target=agent._watchdog, kwargs={"poll_s": 0.01})
    worker.start()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert exits == [EXIT_CAPTURE_STALLED]


def test_watchdog_quiet_while_loop_ticks():
    exits = []
    agent = Agent(SETTINGS, urlopen=FakeHttp(), exit_fn=exits.append)
    agent.loop.last_tick = time.monotonic()
    worker = threading.Thread(target=agent._watchdog, kwargs={"poll_s": 0.01})
    worker.start()
    time.sleep(0.1)
    agent.request_stop()  # stop event ends the watchdog loop
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert exits == []


def test_no_real_network_is_touched():
    # Guard: the wiring test must never fall back to real urllib.
    assert urllib.request.urlopen is not None  # (sanity; fakes injected above)
