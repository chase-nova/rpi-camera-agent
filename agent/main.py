"""Agent entrypoint — wiring, signals, logging (design §8).

Threads: main (capture loop), uploader, viewer, watchdog. SIGTERM/SIGINT
set a stop event that both ends the loop and interrupts its sleep (the
loop's sleep is ``Event.wait``), then the camera stops, the viewer closes,
and the uploader gets a bounded drain. Anything still queued after the
drain is lost by design (01-agent.md §1).

Watchdog: the capture loop can hang inside the camera stack (libcamera
"Camera frontend has timed out" — no exception ever surfaces). The watchdog
thread notices the loop stopped ticking and exits the process; systemd
(``Restart=always``) restarts it with a fresh camera. Recovery by process
exit is deliberate: re-creating the camera in-process while a thread is
stuck in libcamera is not safe.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from agent import __version__
from agent.cache import SpillStore
from agent.camera import CameraSource, FakeCamera, Picamera2Camera
from agent.clock import AgentClock, AnchorStore
from agent.capture import (
    CaptureItem,
    CaptureLoop,
    capture_interval_s,
    in_window,
    window_seconds,
)
from agent.config import Settings, load_settings
from agent.enroll import enroll, resolve_identity
from agent.constants import (
    ANCHOR_FILE,
    CAMERA_RETRY_S,
    CAPTURE_STALL_FACTOR,
    CAPTURE_STALL_MIN_S,
    EXIT_CAPTURE_STALLED,
    COORDS_FILE,
    SPILL_SUBDIR,
    UPLOADER_DRAIN_S,
    WATCHDOG_POLL_S,
    WIFI_ACK_FILE,
)
from agent.thermal import (
    ThermalMonitor,
    ThermalStatus,
    read_local_ip,
    read_pi_model,
    read_volt_core,
)
from agent.net_heal import NetworkHealer
from agent.solar_day import SolarDay, golden_interval
from agent.uploader import Uploader
from agent.viewer import FrameStore, Viewer
from agent.wifi import WifiManager

log = logging.getLogger(__name__)

# A relayed re-enrollment (design 15 §3a) runs on the uploader thread; keep
# it short — the signer relays the token again on the next answer anyway.
REENROLL_ATTEMPTS = 3


def build_camera(
    settings: Settings,
    clock: Callable[[], datetime] | None = None,
    night_fn: Callable[[], bool] | None = None,
) -> CameraSource:
    tz = ZoneInfo(settings.timezone)
    if settings.stage == "test":
        return FakeCamera(tz=tz, size=settings.capture_size, clock=clock)
    return Picamera2Camera(
        tz=tz,
        size=settings.capture_size,
        clock=clock,
        # manual night exposure must fit inside the frame duration limit
        max_exposure_ms=max(settings.max_exposure_ms, settings.night_exposure_ms),
        tuning_file=settings.tuning_file,
        night_exposure_ms=settings.night_exposure_ms,
        night_gain=settings.night_gain,
        raw_size=settings.raw_size,
        night_fn=night_fn,
    )


class Agent:
    def __init__(
        self,
        settings: Settings,
        *,
        camera: CameraSource | None = None,
        urlopen: Callable | None = None,
        thermal: ThermalMonitor | None = None,
        poweroff: Callable[[], None] | None = None,
        exit_fn: Callable[[int], None] | None = None,
    ) -> None:
        self.settings = settings
        # Watchdog exit: os._exit on purpose — a thread stuck in libcamera
        # would never let a normal interpreter shutdown finish.
        self._exit = exit_fn if exit_fn is not None else os._exit
        # Internet-anchored clock (design 12 §4): every capture timestamp,
        # key and heartbeat time comes from here, never from datetime.now.
        self.clock = AgentClock(
            ZoneInfo(settings.timezone),
            AnchorStore(Path(settings.cache_dir) / ANCHOR_FILE),
            refresh_s=settings.anchor_refresh_s,
        )
        # Camera-less degraded mode (2026-09-07, `45-3` CSI fault): a
        # missing camera must not crash-loop the agent — it heartbeats
        # with `camera_error` so the fleet page can say "camera not
        # detected" instead of "disconnected" (run() handles the rest).
        self.camera_error: str | None = None
        self.camera: CameraSource | None
        try:
            self.camera = (
                camera if camera is not None
                # night_fn is evaluated per capture, after init completes —
                # solar-time night mode for IMX462 devices (design 14 §6b)
                else build_camera(
                    settings, clock=self.clock.now_local, night_fn=self._night_now
                )
            )
        except Exception as exc:
            log.error("camera init failed (%s: %s) - no camera detected",
                      type(exc).__name__, exc)
            self.camera = None
            self.camera_error = "not detected"
        self.frames = FrameStore()
        self._stop_event = threading.Event()
        uploader_kwargs = {"urlopen": urlopen} if urlopen is not None else {}
        self.uploader = Uploader(settings, **uploader_kwargs)
        self.uploader.clock = self.clock.now_local
        # design 12 §3: frames captured while offline spill here and are
        # replayed on reconnect; a disk problem degrades to today's drop.
        self.uploader.store = SpillStore(
            Path(settings.cache_dir) / SPILL_SUBDIR,
            max_frames=settings.cache_max_frames,
            min_free_mb=settings.cache_min_free_mb,
            max_age_days=settings.cache_max_age_days,
            boot_id=self.clock.boot_id,
            timezone=settings.timezone,
        )
        self.uploader.agent_clock = self.clock
        # design 13: Wi-Fi requests from the device page ride the /sign answer
        self.uploader.wifi = WifiManager(
            Path(settings.cache_dir) / WIFI_ACK_FILE,
            apply_timeout_s=settings.wifi_apply_timeout_s,
            fallback_s=settings.wifi_fallback_s,
        )
        # design 14: dawn/dusk from Post coordinates riding the /sign answer
        self.solar = SolarDay(Path(settings.cache_dir) / COORDS_FILE, settings.timezone)
        # design 12 §3 extension: self-heal a wedged Wi-Fi (kick, then reboot)
        self.uploader.healer = NetworkHealer(
            settings.network_kick_after_s, settings.network_reboot_after_s
        )
        self.uploader.status_fn = self.status
        self.uploader.shutdown_fn = self._remote_shutdown
        # design 15 §3a: the signer may relay a re-enrollment token
        self.uploader.reenroll_fn = lambda token: enroll(
            settings.upload_signer_url,
            token,
            Path(settings.credential_file),
            max_attempts=REENROLL_ATTEMPTS,
        )
        self.thermal = thermal if thermal is not None else ThermalMonitor(settings)
        self._thermal_status = ThermalStatus("ok", None, None, False)
        self._poweroff = poweroff if poweroff is not None else _sudo_poweroff
        self._pi_model = read_pi_model()
        self._window_idle = False
        self.loop = None if self.camera is None else CaptureLoop(
            self.camera,
            settings,
            self._sink,
            sleep=self._stop_event.wait,
            gate=self._capture_gate,
            interval_fn=self._interval_s,
            quality_fn=lambda: self.clock.now()[1],
            # late-bound: self.viewer is created a few lines below
            preview_active=lambda: bool(
                self.viewer and self.viewer.active_clients > 0
            ),
            preview_publish=self.frames.publish,
        )
        self.viewer = (
            Viewer(settings.viewer_port, self.frames, self.status)
            if settings.viewer_port
            else None
        )
        self._started_monotonic = time.monotonic()

    def _thermal_gate(self) -> bool:
        """Per-interval thermal check (design 02 §5.2). False = skip capture."""
        status = self.thermal.check()
        previous = self._thermal_status
        self._thermal_status = status
        if status.state != previous.state:
            log.warning(
                "thermal state %s -> %s temp=%s", previous.state, status.state,
                status.temp_c,
            )
        if status.should_shutdown:
            log.error("thermal shutdown temp=%s - powering off", status.temp_c)
            self.uploader.send_heartbeat()  # final report: event below
            self.request_stop()
            self._poweroff()
            return False
        if status.state == "paused":
            # keep the manager informed while the camera rests
            self.uploader.send_heartbeat()
            return False
        return True

    def _remote_shutdown(self) -> None:
        """Operator shutdown via the signer (design 11: this device's
        location was closed). Runs on the uploader thread; powers off
        through the same sudoers rule as the thermal shutdown."""
        log.error("operator shutdown (location closed) - powering off")
        self.request_stop()
        self._poweroff()

    def _resolved_window(self) -> tuple[str, str]:
        """Window bounds with nominal dawn/dusk resolved for today (design
        14 §5). Both bounds resolve against today's events — for a
        dusk→dawn window the end differs from the builder's D+1 dawn by
        only ~1–2 min, inside the gate's minute granularity (the builder
        remains the frame picker of record)."""
        if self.uploader.coords is not None:
            self.solar.update_coords(*self.uploader.coords)
        start, end = self.uploader.window
        today = self.clock.now_local().date()
        return self.solar.resolve(start, today), self.solar.resolve(end, today)

    def _night_now(self) -> bool:
        """Solar-time night mode for the camera (design 14 §6b): pure
        function of the design-12 clock — deterministic, no lux flapping,
        works offline. The dawn side releases a notch early (−5° vs the
        evening's −4°) — a briefly dark AE frame beats a blown one."""
        if self.uploader.coords is not None:
            self.solar.update_coords(*self.uploader.coords)
        now = self.clock.now_local()
        return self.solar.night_active(
            now,
            now.date(),
            self.settings.night_on_offset_min,
            self.settings.night_off_offset_min,
        )

    def _interval_s(self) -> int:
        """Cadence adapted to the operator-set capture window (§5.3): the
        window's frames still fill VIDEO_MINUTES of 30 fps video. Inside
        an enabled golden window (design 14 §6) the interval divides by
        the fixed ×4 — never fed back into the base."""
        start, end = self._resolved_window()
        base = capture_interval_s(
            window_seconds(start, end), self.settings.video_minutes
        )
        now = self.clock.now_local()
        if self.solar.in_golden(
            now, now.date(), self.uploader.boost_dawn, self.uploader.boost_dusk
        ):
            return golden_interval(base)
        return base

    def _capture_gate(self) -> bool:
        """Thermal check + capture-window check. Outside the window the
        camera rests and heartbeats keep the manager informed (and keep
        the window itself up to date for the next day)."""
        if not self._thermal_gate():
            return False
        start, end = self._resolved_window()
        now = self.clock.now_local()
        if in_window(now, start, end):
            if self._window_idle:
                log.info("capture window entered (%s-%s)", start, end)
            self._window_idle = False
            return True
        if not self._window_idle:
            log.info("capture window idle until %s (window %s-%s)",
                     start, start, end)
        self._window_idle = True
        self.uploader.send_heartbeat()
        return False

    def _sink(self, item: CaptureItem) -> None:
        self.frames.publish(item.jpeg, item.captured_at)
        self.uploader.submit(item)

    def status(self) -> dict[str, Any]:
        """Config + counters for /healthz and the /sign heartbeat (§5) —
        no secrets."""
        thermal = self._thermal_status
        status: dict[str, Any] = {
            "stage": self.settings.stage,
            "device_id": self.settings.device_id,
            "location_id": self.uploader.location_id,
            "timezone": self.settings.timezone,
            "interval_s": self._interval_s(),
            # today's solar times, when coordinates are known (design 14 §4)
            **self.solar.report(self.clock.now_local().date()),
            "capture_size": f"{self.settings.capture_size[0]},"
                            f"{self.settings.capture_size[1]}",
            "agent_version": __version__,
            "queue_depth": self.uploader.queue_depth,
            "uptime_s": int(time.monotonic() - self._started_monotonic),
            "thermal_state": thermal.state,
            **self.uploader.counters(),
        }
        if thermal.temp_c is not None:
            status["temp_c"] = round(thermal.temp_c, 1)
        if thermal.throttled is not None:
            status["throttled"] = thermal.throttled
        volt_core = read_volt_core()
        if volt_core is not None:
            status["volt_core"] = volt_core
        local_ip = read_local_ip()
        if local_ip is not None:
            status["local_ip"] = local_ip
        if self._pi_model:
            status["pi_model"] = self._pi_model
        model = getattr(self.camera, "model", None)
        if model:
            status["camera"] = model
        if self.camera_error is not None:
            status["camera_error"] = self.camera_error
        if getattr(self.camera, "is_night", False):
            status["night_mode"] = True
        if thermal.should_shutdown:
            status["event"] = "thermal-shutdown"
        # design 12 §5: clock state; the NTP poll is rate-limited internally
        # (status() runs once per interval via the heartbeat and on /healthz)
        resync = self.clock.maybe_poll_ntp()
        if resync is not None:
            self.uploader.apply_resync(resync)
        status["time_quality"] = self.clock.now()[1]
        status["time_source"] = self.clock.time_source
        return status

    def request_stop(self, *_args: Any) -> None:
        log.info("stop requested")
        if self.loop is not None:
            self.loop.stop()
        self._stop_event.set()

    # ── stall watchdog ───────────────────────────────────────────────────────

    def stall_limit_s(self) -> float:
        """Seconds without a loop tick before the loop counts as stalled."""
        return max(CAPTURE_STALL_MIN_S, CAPTURE_STALL_FACTOR * self._interval_s())

    def capture_stalled(self, now: float | None = None) -> bool:
        """True when the capture loop has not ticked within the stall limit.
        Before the first tick (camera start-up) the agent start time counts
        as the last tick, so a hang in camera.start() is caught too."""
        now = time.monotonic() if now is None else now
        if self.camera_error is not None:
            return False  # camera-less mode: no capture loop to stall
        last = self.loop.last_tick
        if last is None:
            last = self._started_monotonic
        return now - last > self.stall_limit_s()

    def _watchdog(self, poll_s: float = WATCHDOG_POLL_S) -> None:
        """Daemon thread: poll liveness; on a stall exit the process so
        systemd restarts the service with a fresh camera (§8)."""
        while not self._stop_event.wait(poll_s):
            if self.capture_stalled():
                last = self.loop.last_tick
                age = time.monotonic() - (
                    self._started_monotonic if last is None else last
                )
                log.critical(
                    "capture loop stalled for %.0fs (camera frontend hang?) - "
                    "exiting %d for systemd restart", age, EXIT_CAPTURE_STALLED,
                )
                self._exit(EXIT_CAPTURE_STALLED)
                return

    def _run_cameraless(self) -> None:
        """Degraded mode: no camera on the CSI bus. Keep heartbeating so
        the fleet page reads "camera not detected" instead of
        "disconnected", and probe for a camera every ``CAMERA_RETRY_S``
        — when one appears (ribbon reseated, module swapped), exit so
        systemd restarts straight into normal capture."""
        log.error("running WITHOUT a camera - heartbeat-only mode")
        self.uploader.start()
        if self.viewer is not None:
            self.viewer.start()
        try:
            self.uploader.send_heartbeat(force=True)  # announce at once
            while not self._stop_event.wait(CAMERA_RETRY_S):
                self.uploader.send_heartbeat()
                try:
                    probe = build_camera(
                        self.settings, clock=self.clock.now_local
                    )
                    probe.start()  # the hardware is only touched on start
                except Exception:
                    continue  # still nothing on the bus
                try:
                    probe.stop()
                except Exception:
                    pass
                log.info("camera detected - exiting for a restart into capture mode")
                self._exit(0)
        finally:
            if self.viewer is not None:
                self.viewer.stop()
            self.uploader.stop(drain_seconds=UPLOADER_DRAIN_S)
            log.info("dam-agent stopped %s", self.uploader.counters())

    def run(self) -> None:
        log.info("dam-agent starting %s", self.status())
        if self.loop is None:
            self._run_cameraless()
            return
        # armed before camera.start(): a hang there must be caught as well
        threading.Thread(target=self._watchdog, name="watchdog", daemon=True).start()
        log.info("watchdog armed stall_limit_s=%.0f", self.stall_limit_s())
        try:
            # Picamera2 touches the hardware lazily HERE, not in
            # build_camera() — a missing camera surfaces as an exception
            # on start (2026-09-07, `45-3`): degrade instead of dying.
            self.camera.start()
        except Exception as exc:
            log.error("camera start failed (%s: %s) - no camera detected",
                      type(exc).__name__, exc)
            self.camera_error = "not detected"  # watchdog stands down too
            self._run_cameraless()
            return
        self.uploader.start()
        if self.viewer is not None:
            self.viewer.start()
        try:
            self.loop.run()
        finally:
            self.camera.stop()
            if self.viewer is not None:
                self.viewer.stop()
            self.uploader.stop(drain_seconds=UPLOADER_DRAIN_S)
            log.info("dam-agent stopped %s", self.uploader.counters())


def _sudo_poweroff() -> None:
    """OS shutdown for sustained over-temperature (sudoers rule from
    provision-pi.sh). Never raises — failing to power off must not crash
    the agent."""
    try:
        subprocess.run(["sudo", "-n", "/sbin/poweroff"], timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("poweroff failed: %s", exc)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # design 15 §6: credential file → enrollment → legacy env identity
    agent = Agent(resolve_identity(load_settings()))
    signal.signal(signal.SIGTERM, agent.request_stop)
    signal.signal(signal.SIGINT, agent.request_stop)
    agent.run()


if __name__ == "__main__":
    main()
