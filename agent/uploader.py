"""Upload queue + uploader thread — presigned two-step flow (design §2, §5).

No AWS credentials and no boto3 on the device (ADR-0003): each frame is
uploaded by asking the upload-signer for a presigned PUT URL (authenticated
with the device token), then PUTting the JPEG over plain HTTPS with stdlib
urllib. The head item is retried in place with exponential backoff and a
fresh presign per attempt (URLs expire) — equivalent to re-queue-at-front
with a single uploader thread.

Offline handling (design 12 §3, when a spill store is wired):

    ONLINE ──(OFFLINE_AFTER_FAILURES consecutive transport failures)──▶ OFFLINE
    OFFLINE ──(one successful /sign: 2xx, 409 unassigned, paused)──▶ ONLINE

- *Transport failure* = the internet is not reachable (DNS, timeout,
  reset, TLS — including a captive portal's certificate). HTTP errors from
  the real signer/S3 are not transport failures and keep today's retry.
- OFFLINE: every queued frame goes to the spill store at once; a
  heartbeat-shaped /sign probe runs every OFFLINE_PROBE_S.
- ONLINE: live frames first; whenever the queue is empty, cached frames
  are replayed oldest-first (≥ REPLAY_MIN_GAP_S apart) and deleted after
  a verified PUT.
- Frames stamped ``provisional`` (clock not yet anchored this boot) are
  always spilled, never uploaded; the first trusted reading (the signer's
  ``Date`` header or NTP) yields a ``Resync`` that restamps them exactly.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from agent.cache import CachedFrame, SpillStore
from agent.capture import CaptureItem, build_key, format_hhmmssfff
from agent.enroll import EnrollError
from agent.wifi import WifiManager
from agent.clock import (
    QUALITY_HOLDOVER,
    QUALITY_PROVISIONAL,
    RESYNC_PROVISIONAL,
    AgentClock,
    Resync,
)
from agent.config import Settings
from agent.constants import (
    BACKOFF_CAP_S,
    BACKOFF_INITIAL_S,
    HEARTBEAT_MIN_INTERVAL_S,
    NET_STATES,
    PUT_TIMEOUT_S,
    REPLAY_DISCARD_CHECK_EVERY,
    REPLAY_INTERLEAVE_EVERY,
    SIDECAR_META_KEYS,
    SIGN_TIMEOUT_S,
)
from dam_shared.constants import CONTENT_TYPE_JPEG, CONTENT_TYPE_JSON, JPG_SUFFIX

log = logging.getLogger(__name__)

NET_ONLINE, NET_OFFLINE = NET_STATES
_QUEUE_POLL_S = 0.5


def build_sidecar(
    item: CaptureItem,
    status: dict[str, Any],
    *,
    late: bool = False,
    drift_s: float = 0.0,
) -> dict[str, Any]:
    """Per-frame hardware/capture log, uploaded as {hhmmssfff}.json next to
    the image (architecture §7): device basics + camera settings actually
    used (exposure/gain/lux) + hardware condition at capture time."""
    camera_meta: dict[str, Any] = {}
    for key in SIDECAR_META_KEYS:
        value = item.camera_metadata.get(key)
        if value is None:
            continue
        camera_meta[key] = list(value) if isinstance(value, tuple | list) else value
    sidecar: dict[str, Any] = {
        "captured_at": item.captured_at.isoformat(),
        "time_quality": item.time_quality,  # design 12 §4
        "ulid": item.ulid,
        "image_bytes": len(item.jpeg),
        "camera_meta": camera_meta,
        "status": status,
    }
    if late:
        # replayed from the spill cache after an outage (design 12 §3.4)
        sidecar["late"] = True
        sidecar["drift_s"] = drift_s
    return sidecar


def is_transport_error(exc: BaseException) -> bool:
    """Does this exception mean "the internet is not reachable"? HTTP
    errors (any status) come from a reachable service and are not
    transport failures; everything network-shaped is."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(
        exc,
        (
            urllib.error.URLError,   # wraps DNS/connect errors
            socket.timeout,
            TimeoutError,
            ConnectionError,         # reset / aborted / RemoteDisconnected
            ssl.SSLError,            # handshake, CERTIFICATE_VERIFY_FAILED (portal)
            json.JSONDecodeError,    # a portal page instead of the signer's JSON
            OSError,
        ),
    )


class SkipUpload(Exception):
    """Server said the frame should be skipped (paused/unassigned) — §5."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Uploader:
    def __init__(
        self,
        settings: Settings,
        *,
        urlopen: Callable = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._urlopen = urlopen
        self._sleep = sleep
        self._mono = monotonic
        self._queue: queue.Queue[CaptureItem] = queue.Queue(maxsize=settings.queue_max)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="uploader", daemon=True)
        self._lock = threading.Lock()
        self._uploaded = 0
        self._dropped = 0
        # heartbeat timestamp source; the agent wires its internet-anchored
        # clock here (design 12 §4), tests keep the system clock
        self.clock: Callable[[], datetime] = lambda: datetime.now(
            ZoneInfo(settings.timezone)
        )
        self._skipped = 0
        self._failed_attempts = 0
        # Learned from the signer's key on each upload (the assignment is
        # cloud-authoritative; the device env no longer carries a location).
        self.location_id: str | None = settings.location_id
        # Capture window learned from every /sign answer (operator-set in
        # the control plane); full day until the signer says otherwise.
        self.window: tuple[str, str] = ("00:00", "00:00")
        # Post coordinates from the /sign answer (design 14 §4) — consumed
        # by Agent._resolved_window via SolarDay (persisted there).
        self.coords: tuple[float, float] | None = None
        # Golden-boost flags echoed from control (design 14 §6).
        self.boost_dawn = False
        self.boost_dusk = False
        # Spill cache on/off (design 12, optional since 2026-09-02):
        # echoed from control.cache_enabled; absent = enabled. Disabled =
        # no disk writes and no offline state — offline frames drop from
        # the bounded queue (pre-design-12 behavior); already-cached
        # frames still drain via replay.
        self.cache_enabled = True
        # Network self-healing watchdog (design 12 §3 extension) — wired
        # by Agent; ticked here, fed by every successful signer response.
        self.healer: Any = None
        self._last_heartbeat_mono = 0.0
        # Set by Agent after construction; included in every /sign body so
        # the sign call doubles as the fleet heartbeat (design 02 §5).
        self.status_fn: Callable[[], dict[str, Any]] | None = None
        # Called once when the signer answers "shutdown" (design 11: the
        # device's location was closed) — main wires this to poweroff.
        self.shutdown_fn: Callable[[], None] | None = None
        # ── design 12: spill cache + clock (wired by Agent; None = legacy) ──
        self.store: SpillStore | None = None
        self.agent_clock: AgentClock | None = None
        self.net_state: str = NET_ONLINE
        self.offline_since: datetime | None = None
        self._consecutive_transport_failures = 0
        self._replayed = 0
        self._last_probe_mono = float("-inf")
        self._last_replay_mono = float("-inf")
        self._replays_since_discard_check = 0
        self._live_since_replay = 0  # interleave counter (design 12 §3.2)
        # ── design 13: desired Wi-Fi config from the /sign answer ──
        # Wired by Agent; the latest request is stashed by _sign (any thread)
        # and executed by tick() on the uploader thread, between uploads.
        self.wifi: WifiManager | None = None
        self._wifi_request: dict[str, Any] | None = None
        # ── design 15 §3a: re-enrollment relayed in the /sign answer ──
        # The credential can change at runtime, so it lives here, not in
        # the frozen settings. reenroll_fn (wired by Agent) enrolls with a
        # relayed token and returns the new credential.
        self.device_id: str = settings.device_id
        self.device_token: str = settings.device_token
        self.reenroll_fn: Callable[[str], Any] | None = None
        self._reenroll_request: dict[str, Any] | None = None
        self._reenroll_done_id: str | None = None

    # ── capture side (never blocks) ──────────────────────────────────────────

    def submit(self, item: CaptureItem) -> None:
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    with self._lock:
                        self._dropped += 1
                    log.warning(
                        "queue full - dropped oldest frame (dropped=%d)", self._dropped
                    )
                except queue.Empty:
                    pass  # raced with the uploader; try the put again

    # ── status (viewer /healthz) ─────────────────────────────────────────────

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def counters(self) -> dict[str, Any]:
        with self._lock:
            counters: dict[str, Any] = {
                "uploaded": self._uploaded,
                "dropped": self._dropped,
                "skipped": self._skipped,
                "failed_attempts": self._failed_attempts,
            }
        if self.store is not None:
            stats = self.store.stats()
            counters.update(
                {
                    "net_state": self.net_state,
                    "offline_since": (
                        self.offline_since.isoformat() if self.offline_since else None
                    ),
                    "cache_frames": stats["frames"],
                    "cache_bytes": stats["bytes"],
                    "cache_evicted": stats["evicted"],
                    "cache_error": stats["error"],
                    "replay_pending": stats["frames"],
                    "replayed": self._replayed,
                }
            )
        if self.wifi is not None:
            counters.update(self.wifi.status())
        if self.healer is not None:
            counters["net_kicks"] = self.healer.kicks
            counters["net_reboots"] = self.healer.reboots
        return counters

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread.start()

    def stop(self, drain_seconds: float = 10.0) -> None:
        """Give the queue a bounded chance to drain, then stop the thread."""
        deadline = time.monotonic() + drain_seconds
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.1)
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    # ── uploader thread ──────────────────────────────────────────────────────

    def _run(self) -> None:
        log.info("uploader started signer=%s", self._settings.upload_signer_url)
        if self.store is not None and len(self.store):
            log.info("uploader: %d cached frame(s) to replay", len(self.store))
        while not self._stop.is_set():
            self.tick()
        log.info("uploader stopped %s", self.counters())

    def tick(self, poll_s: float = _QUEUE_POLL_S) -> None:
        """One scheduler step: probe while offline, live frames first, then
        one replay frame when the queue is empty (design 12 §3.2). On a
        starved uplink the live queue never empties, so additionally one
        replay frame is interleaved after every ``REPLAY_INTERLEAVE_EVERY``
        live frames — otherwise the cache freezes entirely (2026-09-06)."""
        if self.net_state == NET_OFFLINE:
            self._maybe_probe()
        if self.healer is not None:
            self.healer.check()
        self._handle_wifi_request()
        self._handle_reenroll()
        try:
            item = self._queue.get(timeout=poll_s)
        except queue.Empty:
            item = None
        if item is not None:
            self.process(item)
            self._live_since_replay += 1
            if self._live_since_replay >= REPLAY_INTERLEAVE_EVERY:
                self._live_since_replay = 0
                if self.net_state == NET_ONLINE and self.store is not None and len(self.store):
                    self._replay_one()
            return
        if self.net_state == NET_ONLINE and self.store is not None and len(self.store):
            self._replay_one()

    def process(self, item: CaptureItem) -> bool:
        """Upload one live item, retrying with backoff until success, stop,
        or — with a spill store — until the network is declared offline."""
        if self.store is not None and self.cache_enabled:
            if item.time_quality == QUALITY_PROVISIONAL:
                # never upload under a provisional stamp (design 12 §4.3);
                # a heartbeat fetches the signer's Date so the clock anchors
                self._spill(item, "provisional clock")
                if self.net_state == NET_ONLINE:
                    self.send_heartbeat()
                return False
            if self.net_state == NET_OFFLINE:
                self._spill(item, "offline")
                return False
        elif item.time_quality == QUALITY_PROVISIONAL:
            # cache disabled: a provisional stamp still must not upload
            # (design 12 §4.3) and has nowhere to wait — drop as skipped
            with self._lock:
                self._skipped += 1
            log.warning("cache disabled - dropped provisional frame %s", item.ulid)
            if self.net_state == NET_ONLINE:
                self.send_heartbeat()
            return False
        backoff = BACKOFF_INITIAL_S
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            try:
                key = self._upload_once(item)
                with self._lock:
                    self._uploaded += 1
                log.info("uploaded key=%s attempt=%d depth=%d",
                         key, attempt, self._queue.qsize())
                return True
            except SkipUpload as skip:
                with self._lock:
                    self._skipped += 1
                log.info("skipped key=%s reason=%s", item.key, skip.reason)
                return True  # deliberate skip — not a failure, no retry
            except Exception as exc:
                with self._lock:
                    self._failed_attempts += 1
                log.warning(
                    "upload failed key=%s attempt=%d error=%s",
                    item.key, attempt, exc,
                )
                if self._note_failure(exc):
                    self._spill(item, "offline")
                    return False
                self._sleep(backoff)
                backoff = min(BACKOFF_CAP_S, backoff * 2)
        return False

    # ── desired Wi-Fi config (design 13) ─────────────────────────────────────

    def _stash_wifi(self, payload: Any) -> None:
        wifi = payload.get("wifi") if isinstance(payload, dict) else None
        if isinstance(wifi, dict) and wifi.get("id"):
            self._wifi_request = wifi
        # the re-enroll relay rides the same answers (design 15 §3a)
        reenroll = payload.get("reenroll") if isinstance(payload, dict) else None
        if isinstance(reenroll, dict) and reenroll.get("token"):
            self._reenroll_request = reenroll

    def _stash_coords(self, payload: Any) -> None:
        """Post coordinates ride every answer when assigned (design 14 §4)."""
        coords = payload.get("coords") if isinstance(payload, dict) else None
        if (
            isinstance(coords, dict)
            and isinstance(coords.get("latitude"), int | float)
            and isinstance(coords.get("longitude"), int | float)
        ):
            self.coords = (float(coords["latitude"]), float(coords["longitude"]))
        if isinstance(payload, dict):
            # golden-boost flags (design 14 §6): adopt when echoed
            if isinstance(payload.get("boost_dawn"), bool):
                self.boost_dawn = payload["boost_dawn"]
            if isinstance(payload.get("boost_dusk"), bool):
                self.boost_dusk = payload["boost_dusk"]
            if isinstance(payload.get("cache_enabled"), bool):
                self.cache_enabled = payload["cache_enabled"]

    def _handle_wifi_request(self) -> None:
        request, self._wifi_request = self._wifi_request, None
        if request is None or self.wifi is None:
            return
        try:
            if self.wifi.handle(request):
                # a switch may have changed our network: report at once
                self.send_heartbeat(force=True)
        except Exception as exc:  # never let a Wi-Fi request kill uploads
            log.exception("wifi request %s failed: %s", request.get("id"), exc)

    def _handle_reenroll(self) -> None:
        """Enroll with a relayed token and switch to the new credential
        (design 15 §3a). A refused token is not retried; a transport
        failure is — the signer keeps relaying until expiry, and the
        pending secret makes the retry idempotent."""
        request, self._reenroll_request = self._reenroll_request, None
        if request is None or self.reenroll_fn is None:
            return
        request_id = str(request.get("id", ""))
        if request_id and request_id == self._reenroll_done_id:
            return
        try:
            credential = self.reenroll_fn(str(request["token"]))
        except EnrollError as exc:
            self._reenroll_done_id = request_id
            log.error("re-enrollment %s refused: %s", request_id, exc)
            return
        except Exception as exc:  # never let re-enrollment kill uploads
            log.warning("re-enrollment %s failed, will retry: %s", request_id, exc)
            return
        self.device_id = credential.device_id
        self.device_token = credential.device_secret
        self._reenroll_done_id = request_id
        log.info("re-enrolled as %s — new credential in use", credential.device_id)

    # ── online / offline ─────────────────────────────────────────────────────

    def _note_failure(self, exc: BaseException) -> bool:
        """Count a transport failure; True when it tipped us OFFLINE (only
        possible with a spill store — otherwise today's retry forever)."""
        if not is_transport_error(exc):
            self._consecutive_transport_failures = 0  # the network works
            return False
        self._consecutive_transport_failures += 1
        if (
            self.store is None
            or not self.cache_enabled  # optional cache off: retry forever
            or self.net_state == NET_OFFLINE
            or self._consecutive_transport_failures < self._settings.offline_after_failures
        ):
            return False
        self._go_offline()
        return True

    def _go_offline(self) -> None:
        self.net_state = NET_OFFLINE
        self.offline_since = self.clock()
        self._last_probe_mono = self._mono()
        if self.agent_clock is not None:
            self.agent_clock.set_online(False)
            self.agent_clock.persist_now()
        log.warning(
            "network OFFLINE after %d transport failures - spilling frames to %s",
            self._consecutive_transport_failures,
            self.store.root if self.store is not None else "?",
        )

    def _mark_online(self) -> None:
        """A /sign round trip succeeded — the internet is reachable."""
        self._consecutive_transport_failures = 0
        if self.net_state == NET_ONLINE:
            return
        self.net_state = NET_ONLINE
        self.offline_since = None
        if self.agent_clock is not None:
            self.agent_clock.set_online(True)
        pending = len(self.store) if self.store is not None else 0
        log.info("network ONLINE again - %d cached frame(s) to replay", pending)
        if self.store is not None and pending:
            self.store.discard_older_than(self.clock().astimezone(UTC))
            self._replays_since_discard_check = 0

    def _maybe_probe(self) -> None:
        """Heartbeat-shaped /sign every OFFLINE_PROBE_S while offline; a
        success flips us ONLINE (explicitly — the deliberate probe is the
        one small request allowed to prove the link; if the uplink is
        actually asymmetric, the next PUT failures tip us straight back
        offline and frames keep spilling)."""
        now = self._mono()
        if now - self._last_probe_mono < self._settings.offline_probe_s:
            return
        self._last_probe_mono = now
        stamp = self.clock()
        try:
            self._sign(
                stamp.strftime("%Y-%m-%d"),
                f"{format_hhmmssfff(stamp)}{JPG_SUFFIX}",
                {"device-id": self.device_id},
            )
        except SkipUpload:
            self._mark_online()  # paused/unassigned: still a good round trip
        except Exception as exc:
            log.info("offline probe failed error=%s", exc)
        else:
            self._mark_online()

    def _spill(self, item: CaptureItem, reason: str) -> None:
        assert self.store is not None
        if self.store.put(item):
            log.info("spilled key=%s reason=%s cached=%d", item.key, reason, len(self.store))
        else:
            with self._lock:
                self._dropped += 1
            log.warning(
                "spill failed (%s) - dropped frame %s (dropped=%d)",
                self.store.error, item.key, self._dropped,
            )

    # ── resync: correct cached frames after a trusted time reading ──────────

    def apply_resync(self, resync: Resync) -> int:
        """Restamp cached frames of this boot per design 12 §4.3/§4.4.
        Returns the number of frames corrected."""
        if self.store is None or self.agent_clock is None:
            return 0
        boot_id = self.agent_clock.boot_id
        corrected = 0
        for frame in list(self.store.iter_oldest()):
            if frame.boot_id != boot_id or frame.captured_mono is None:
                continue  # another boot: offset unknown (§4.3), or torn stamp
            if resync.kind == RESYNC_PROVISIONAL:
                if frame.time_quality != QUALITY_PROVISIONAL:
                    continue
            elif frame.time_quality != QUALITY_HOLDOVER:
                continue
            delta = resync.correction_s(frame.captured_mono)
            if delta == 0.0 and resync.kind != RESYNC_PROVISIONAL:
                continue
            new_utc = frame.captured_utc + timedelta(seconds=delta)
            if self.store.restamp(frame.ulid, new_utc, QUALITY_HOLDOVER, drift_s=delta):
                corrected += 1
        if corrected:
            log.info(
                "resync (%s, %s): restamped %d cached frame(s)",
                resync.kind, resync.source, corrected,
            )
        return corrected

    # ── replay ───────────────────────────────────────────────────────────────

    def _replay_one(self) -> None:
        assert self.store is not None
        now = self._mono()
        if now - self._last_replay_mono < self._settings.replay_min_gap_s:
            return
        if self._replays_since_discard_check >= REPLAY_DISCARD_CHECK_EVERY:
            self.store.discard_older_than(self.clock().astimezone(UTC))
            self._replays_since_discard_check = 0
        frame = next(iter(self.store.iter_oldest()), None)
        if frame is None:
            return
        self._last_replay_mono = now
        self._replays_since_discard_check += 1
        item = self._frame_to_item(frame)
        if item is None:
            return
        try:
            key = self._upload_once(item, late=True, drift_s=frame.drift_s)
        except SkipUpload as skip:
            # paused/unassigned: the operator does not want these frames
            self.store.delete(frame.ulid)
            with self._lock:
                self._skipped += 1
            log.info("replay skipped ulid=%s reason=%s (deleted)", frame.ulid, skip.reason)
            return
        except Exception as exc:
            with self._lock:
                self._failed_attempts += 1
            log.warning("replay failed ulid=%s error=%s", frame.ulid, exc)
            self._note_failure(exc)  # may flip OFFLINE; the frame stays cached
            return
        self.store.delete(frame.ulid)
        with self._lock:
            self._uploaded += 1
            self._replayed += 1
        log.info("replayed key=%s pending=%d", key, len(self.store))

    def _frame_to_item(self, frame: CachedFrame) -> CaptureItem | None:
        """A cached frame as an upload item under its final stamp (§4.4).
        Uncorrectable provisional frames are uploaded flagged or dropped
        per CACHE_KEEP_PROVISIONAL; unreadable files are dropped."""
        assert self.store is not None
        if frame.time_quality == QUALITY_PROVISIONAL:
            # still provisional at replay time = uncorrectable: another
            # boot's frame (offset unknown, §4.3) or a torn stamp (no
            # captured_mono); this boot's frames were restamped at resync
            if not self._settings.cache_keep_provisional:
                self.store.delete(frame.ulid)
                with self._lock:
                    self._skipped += 1
                log.warning("dropped uncorrectable provisional frame %s", frame.ulid)
                return None
        try:
            jpeg = frame.read_jpeg()
        except OSError as exc:
            log.error("cached frame unreadable, dropping %s: %s", frame.ulid, exc)
            self.store.delete(frame.ulid)
            return None
        captured_at = frame.captured_utc.astimezone(ZoneInfo(self._settings.timezone))
        return CaptureItem(
            jpeg=jpeg,
            captured_at=captured_at,
            ulid=frame.ulid,
            key=build_key(
                self._settings.s3_image_prefix,
                self.location_id or "unassigned",
                captured_at,
            ),
            camera_metadata=frame.camera,
            captured_mono=frame.captured_mono or 0.0,
            time_quality=frame.time_quality,
        )

    # ── signer + S3 ──────────────────────────────────────────────────────────

    def _sign(
        self, date: str, filename: str, metadata: dict, sidecar: bool = False
    ) -> dict:
        """POST /sign; raises SkipUpload for paused/unassigned answers. Any
        answer from the signer (incl. 409 unassigned) proves the internet
        is reachable and carries its Date header — a trusted time."""
        body: dict[str, Any] = {
            "token": self.device_token,
            "date": date,
            "filename": filename,
            "content_type": CONTENT_TYPE_JPEG,
            "metadata": metadata,
            "device_id": self.device_id,
        }
        if sidecar:
            body["sidecar"] = True
        if self.status_fn is not None:
            body["status"] = self.status_fn()
        request = urllib.request.Request(
            self._settings.upload_signer_url.rstrip("/") + "/sign",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._urlopen(request, timeout=SIGN_TIMEOUT_S) as response:
                signed = json.loads(response.read())
                self._observe_response(response)
        except urllib.error.HTTPError as exc:
            self._observe_response(exc)
            if exc.code == 409:
                try:
                    payload = json.loads(exc.read())
                except (json.JSONDecodeError, OSError):
                    payload = {}
                self._stash_wifi(payload)  # Wi-Fi is set up before assignment
                self._stash_coords(payload)
                if payload.get("error") == "unassigned":
                    raise SkipUpload("unassigned") from exc
            raise
        self._stash_wifi(signed)
        self._stash_coords(signed)
        window = signed.get("window")
        if isinstance(window, dict) and window.get("start") and window.get("end"):
            self.window = (str(window["start"]), str(window["end"]))
        if signed.get("status") == "paused":
            raise SkipUpload("paused")
        if signed.get("status") == "shutdown":
            # operator closed this device's location (design 11)
            if self.shutdown_fn is not None:
                self.shutdown_fn()
            raise SkipUpload("shutdown")
        return signed

    def _observe_response(self, response: Any) -> None:
        """A signer round trip completed: its Date header is a trusted time
        reading (design 12 §4.1). It does NOT prove the uplink — on an
        asymmetric link small /sign requests pass while bulk PUTs die, and
        marking online here reset the failure counter every cycle so
        OFFLINE_AFTER_FAILURES could never trip (dam-imx462-92-1,
        2026-09-01: attempt=18 on one frame, queue overflow instead of
        spill). Online is proven only by a successful frame PUT
        (_upload_once) or the explicit probe (_maybe_probe)."""
        was_offline = self.net_state == NET_OFFLINE
        if self.healer is not None:
            # any signer response (2xx or HTTP error) proves the network
            # path end-to-end — the self-healing silence clock resets
            self.healer.note_success()
        if self.agent_clock is not None:
            headers = getattr(response, "headers", None)
            date = headers.get("Date") if headers is not None else None
            resync = self.agent_clock.observe_http_date(date, force=was_offline)
            if resync is not None:
                self.apply_resync(resync)

    def _upload_once(
        self, item: CaptureItem, *, late: bool = False, drift_s: float = 0.0
    ) -> str:
        metadata = {
            "ulid": item.ulid,
            "device-id": self.device_id,
            "captured-utc": item.captured_at.astimezone(UTC).isoformat(),
            "timezone": self._settings.timezone,
        }
        signed = self._sign(
            item.captured_at.strftime("%Y-%m-%d"),
            f"{format_hhmmssfff(item.captured_at)}{JPG_SUFFIX}",
            metadata,
            sidecar=True,
        )
        key_parts = str(signed.get("key", "")).split("/")
        if len(key_parts) >= 2 and key_parts[1]:
            self.location_id = key_parts[1]
        headers = {"Content-Type": CONTENT_TYPE_JPEG}
        headers.update({f"x-amz-meta-{k}": v for k, v in metadata.items()})
        put_request = urllib.request.Request(
            signed["url"], data=item.jpeg, method="PUT", headers=headers
        )
        with self._urlopen(put_request, timeout=PUT_TIMEOUT_S):
            pass
        # the bulk PUT is the proof of a working uplink (asymmetric-link
        # rule — see _observe_response); covers live uploads and replays
        self._mark_online()
        self._upload_sidecar(item, signed, late=late, drift_s=drift_s)
        return signed["key"]

    def _upload_sidecar(
        self, item: CaptureItem, signed: dict, *, late: bool = False, drift_s: float = 0.0
    ) -> None:
        """Best-effort hardware/capture log next to the frame (§7 sidecar).
        Never fails the frame — the image is already safely uploaded."""
        sidecar_url = signed.get("sidecar_url")
        if not sidecar_url:
            return
        try:
            status = self.status_fn() if self.status_fn is not None else {}
            payload = json.dumps(
                build_sidecar(item, status, late=late, drift_s=drift_s), default=str
            ).encode("utf-8")
            request = urllib.request.Request(
                sidecar_url, data=payload, method="PUT",
                headers={"Content-Type": CONTENT_TYPE_JSON},
            )
            with self._urlopen(request, timeout=PUT_TIMEOUT_S):
                pass
        except Exception as exc:
            log.warning(
                "sidecar upload failed key=%s error=%s",
                signed.get("sidecar_key"), exc,
            )

    def send_heartbeat(self, *, force: bool = False) -> None:
        """Status-only /sign (URL unused) — keeps the manager informed
        while the camera rests (thermal pause / capture-window idle).
        Rate-limited so rest-state loops can call it freely (``force``
        bypasses the limit, e.g. right after a Wi-Fi switch). Never
        raises."""
        now = time.monotonic()
        if not force and now - self._last_heartbeat_mono < HEARTBEAT_MIN_INTERVAL_S:
            return
        self._last_heartbeat_mono = now
        now = self.clock()
        try:
            self._sign(
                now.strftime("%Y-%m-%d"),
                f"{format_hhmmssfff(now)}{JPG_SUFFIX}",
                {"device-id": self.device_id},
            )
        except SkipUpload:
            pass  # paused/unassigned — status was still recorded server-side
        except Exception as exc:
            log.warning("heartbeat failed error=%s", exc)
            self._note_failure(exc)
