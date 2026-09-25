"""Tests for agent.viewer — real server on an ephemeral port, no sleeps."""

import http.client
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.viewer import BOUNDARY, FrameStore, Viewer

TZ = ZoneInfo("Asia/Seoul")
TS1 = datetime(2026, 8, 13, 12, 0, 0, tzinfo=TZ)
TS2 = datetime(2026, 8, 13, 12, 0, 48, tzinfo=TZ)
JPEG1 = b"\xff\xd8frame-one\xff\xd9"
JPEG2 = b"\xff\xd8frame-two!\xff\xd9"


@pytest.fixture()
def viewer():
    frames = FrameStore()
    v = Viewer(
        port=0,
        frames=frames,
        status_fn=lambda: {"queue_depth": 3, "uploaded": 7, "dropped": 0},
    )
    v.start()
    yield v, frames
    v.stop()


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


def test_latest_before_any_frame_is_503(viewer):
    v, _ = viewer
    resp, _ = _get(v.port, "/latest.jpg")
    assert resp.status == 503


def test_latest_serves_frame_with_etag_and_304(viewer):
    v, frames = viewer
    frames.publish(JPEG1, TS1)
    resp, body = _get(v.port, "/latest.jpg")
    assert resp.status == 200
    assert body == JPEG1
    assert resp.getheader("ETag") == '"1"'
    assert resp.getheader("Content-Type") == "image/jpeg"

    resp, body = _get(v.port, "/latest.jpg", headers={"If-None-Match": '"1"'})
    assert resp.status == 304
    assert body == b""

    frames.publish(JPEG2, TS2)
    resp, body = _get(v.port, "/latest.jpg", headers={"If-None-Match": '"1"'})
    assert resp.status == 200
    assert body == JPEG2
    assert resp.getheader("ETag") == '"2"'


def test_healthz_shape(viewer):
    v, frames = viewer
    frames.publish(JPEG1, TS1)
    resp, body = _get(v.port, "/healthz")
    assert resp.status == 200
    status = json.loads(body)
    assert status["seq"] == 1
    assert status["last_capture"] == TS1.isoformat()
    assert status["queue_depth"] == 3
    assert status["uploaded"] == 7


def test_index_page_embeds_stream(viewer):
    v, _ = viewer
    resp, body = _get(v.port, "/")
    assert resp.status == 200
    assert b"/stream.mjpg" in body


def test_unknown_path_404(viewer):
    v, _ = viewer
    resp, _ = _get(v.port, "/nope")
    assert resp.status == 404


def _read_stream_part(fp):
    """Read one MJPEG part: skip boundary/blank lines, parse headers, read
    exactly Content-Length bytes. (Each part is FOLLOWED by the next
    boundary so browsers render immediately — the skip handles both.)"""
    length = None
    while True:
        line = fp.readline(200).strip()
        if line == f"--{BOUNDARY}".encode() or line == b"":
            if length is not None:
                break  # blank line after headers -> body follows
            continue  # leading boundary / inter-part blank
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            length = int(value)
    assert length is not None
    return fp.read(length)


def test_stream_pushes_current_then_new_frames_and_survives_disconnect(viewer):
    v, frames = viewer
    frames.publish(JPEG1, TS1)

    conn = http.client.HTTPConnection("127.0.0.1", v.port, timeout=5)
    conn.request("GET", "/stream.mjpg")
    resp = conn.getresponse()
    assert resp.status == 200
    assert f"boundary={BOUNDARY}" in resp.getheader("Content-Type")

    # current frame arrives immediately on connect
    assert _read_stream_part(resp.fp) == JPEG1
    # a newly published frame is pushed without any client action
    frames.publish(JPEG2, TS2)
    assert _read_stream_part(resp.fp) == JPEG2

    # drop the connection mid-stream — the server must keep serving others
    conn.close()
    resp2, body = _get(v.port, "/healthz")
    assert resp2.status == 200
    assert json.loads(body)["seq"] == 2
