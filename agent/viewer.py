"""HTTP mini-viewer — live MJPEG stream, latest frame, health (design §6).

No framework: stdlib ``ThreadingHTTPServer`` in a daemon thread. The capture
loop publishes each frame into a shared ``FrameStore``; stream handlers wait
on its condition and push new JPEG parts the moment they arrive, so the
browser image updates with zero JavaScript and zero reloads.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from agent.constants import MJPEG_BOUNDARY as BOUNDARY
from agent.constants import STREAM_WAIT_S

log = logging.getLogger(__name__)

PAGE_HTML = """<!doctype html>
<html><head><title>dam-agent viewer</title></head>
<body style="margin:0;background:#111;color:#ddd;font-family:sans-serif;
             text-align:center">
<p id="ts" style="margin:8px">waiting for capture…</p>
<img src="/stream.mjpg" style="max-width:100%" alt="live capture">
<script>
async function refreshCaption() {
  const s = await (await fetch('/healthz')).json();
  const location = s.location_id || 'unassigned';
  let text = location + '  |  ' +
    (s.last_capture || 'no frame yet') + '  (frame #' + s.seq + ')';
  if (s.thermal_state === 'paused')
    text += '  [thermally paused at ' + s.temp_c + '°C - showing last frame]';
  else if (s.thermal_state === 'warn') text += '  [warm: ' + s.temp_c + '°C]';
  document.getElementById('ts').textContent = text;
  document.title = location + ' - ' + (s.device_id || 'dam-agent');
}
refreshCaption().catch(() => {});
setInterval(() => refreshCaption().catch(() => {}), 5000);
</script>
</body></html>
"""


@dataclass(frozen=True)
class LatestFrame:
    jpeg: bytes
    captured_at: datetime
    seq: int


class FrameStore:
    """Shared latest-frame reference + change notification (design §6)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._frame: LatestFrame | None = None

    def publish(self, jpeg: bytes, captured_at: datetime) -> None:
        with self._cond:
            seq = self._frame.seq + 1 if self._frame else 1
            self._frame = LatestFrame(jpeg=jpeg, captured_at=captured_at, seq=seq)
            self._cond.notify_all()

    @property
    def frame(self) -> LatestFrame | None:
        return self._frame  # atomic reference read

    def wait_newer_than(self, seq: int, timeout: float) -> LatestFrame | None:
        """Return a frame newer than ``seq``, or None on timeout."""
        with self._cond:
            self._cond.wait_for(
                lambda: self._frame is not None and self._frame.seq > seq,
                timeout=timeout,
            )
            frame = self._frame
            return frame if frame is not None and frame.seq > seq else None


class _Handler(BaseHTTPRequestHandler):
    server: ViewerServer  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("viewer %s", fmt % args)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        try:
            if self.path == "/":
                self._page()
            elif self.path == "/latest.jpg":
                self._latest()
            elif self.path == "/stream.mjpg":
                self._stream()
            elif self.path == "/healthz":
                self._healthz()
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass  # client went away — never let this kill the server

    def _page(self) -> None:
        body = PAGE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _latest(self) -> None:
        frame = self.server.frames.frame
        if frame is None:
            self.send_error(503, "no frame captured yet")
            return
        etag = f'"{frame.seq}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame.jpeg)))
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(frame.jpeg)

    def _stream(self) -> None:
        self.server.stream_client_started()
        try:
            self._stream_body()
        finally:
            self.server.stream_client_ended()

    def _stream_body(self) -> None:
        self.send_response(200)
        self.send_header(
            "Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}"
        )
        self.end_headers()
        # Browsers render a multipart frame only once the NEXT boundary
        # arrives, so each part ends with the following boundary line —
        # otherwise the first image would sit buffered until the next
        # capture (48 s, or forever while thermally paused).
        self.wfile.write(f"--{BOUNDARY}\r\n".encode("ascii"))
        seen_seq = 0
        while self.server.running:
            frame = self.server.frames.wait_newer_than(seen_seq, STREAM_WAIT_S)
            if frame is None:
                continue  # timeout slice — recheck running flag
            part = (
                "Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(frame.jpeg)}\r\n\r\n"
            ).encode("ascii")
            self.wfile.write(
                part + frame.jpeg + f"\r\n--{BOUNDARY}\r\n".encode("ascii")
            )
            self.wfile.flush()
            seen_seq = frame.seq

    def _healthz(self) -> None:
        frame = self.server.frames.frame
        status = {
            "last_capture": frame.captured_at.isoformat() if frame else None,
            "seq": frame.seq if frame else 0,
        }
        status.update(self.server.status_fn())
        body = json.dumps(status).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, port: int, frames: FrameStore, status_fn: Callable[[], dict[str, Any]]
    ) -> None:
        super().__init__(("0.0.0.0", port), _Handler)
        self.frames = frames
        self.status_fn = status_fn
        self.running = True
        self._clients_lock = threading.Lock()
        self._stream_clients = 0

    def stream_client_started(self) -> None:
        with self._clients_lock:
            self._stream_clients += 1

    def stream_client_ended(self) -> None:
        with self._clients_lock:
            self._stream_clients -= 1

    @property
    def stream_clients(self) -> int:
        return self._stream_clients


class Viewer:
    """Owns the server thread. ``port=0`` picks an ephemeral port (tests)."""

    def __init__(
        self,
        port: int,
        frames: FrameStore,
        status_fn: Callable[[], dict[str, Any]],
    ) -> None:
        self._server = ViewerServer(port, frames, status_fn)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="viewer", daemon=True
        )

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def active_clients(self) -> int:
        """Connected MJPEG stream clients (drives the live-view boost)."""
        return self._server.stream_clients

    def start(self) -> None:
        self._thread.start()
        log.info("viewer listening on port %d", self.port)

    def stop(self) -> None:
        self._server.running = False
        self._server.shutdown()
        self._server.server_close()
