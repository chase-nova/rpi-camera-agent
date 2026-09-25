"""Camera sources — capture behind a small interface (design 01-agent.md §2).

``Picamera2Camera`` runs on the Pi (picamera2, ADR-0001); ``FakeCamera``
runs anywhere and draws the timestamp into a generated image. Both return
in-memory JPEG bytes — nothing is written to disk (no-local-save design).
"""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Callable
from datetime import datetime, tzinfo
from typing import Any, Protocol

from agent.config import (
    NIGHT_CONFIRM_FRAMES,
    NIGHT_EXPOSURE_TOLERANCE,
    NIGHT_LUMA_EXIT,
    NIGHT_LUX_OFF,
    NIGHT_LUX_ON,
    NIGHT_PROBE_SETTLE_S,
    NIGHT_REENTRY_COOLDOWN_S,
    NIGHT_SETTLE_MAX_FRAMES,
)
from agent.constants import CAMERA_MODEL_ALIASES, FRAME_DURATION_MIN_US

log = logging.getLogger(__name__)


class CameraError(Exception):
    """Raised when the camera is unavailable or used before start()."""


class CameraSource(Protocol):
    def start(self) -> None: ...

    def capture_jpeg(self) -> tuple[bytes, datetime, dict[str, Any]]: ...

    def stop(self) -> None: ...


class FakeCamera:
    """Test/dev camera: a generated image with the capture time drawn in."""

    model = "fake"

    def __init__(
        self,
        tz: tzinfo,
        size: tuple[int, int] = (1280, 720),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._tz = tz
        self._size = size
        # capture-time source: the agent's internet-anchored clock (design
        # 12 §4); the system clock only when nothing is injected (tests)
        self._clock = clock if clock is not None else (lambda: datetime.now(tz))
        self._started = False

    def start(self) -> None:
        self._started = True

    def capture_jpeg(self) -> tuple[bytes, datetime, dict[str, Any]]:
        if not self._started:
            raise CameraError("FakeCamera used before start()")
        from PIL import Image, ImageDraw  # dev-only dependency

        captured_at = self._clock()
        image = Image.new("RGB", self._size, color=(24, 48, 96))
        draw = ImageDraw.Draw(image)
        draw.text((20, 20), captured_at.isoformat(), fill=(255, 255, 255))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG")
        return buffer.getvalue(), captured_at, {"source": "fake"}

    def stop(self) -> None:
        self._started = False




def resolve_camera_model(model: str | None) -> str | None:
    return CAMERA_MODEL_ALIASES.get(model, model) if model else model


class NightController:
    """Anti-flicker night-mode decision (A/B measured 2026-08-16/17).

    Naive per-frame thresholds oscillate when a scene sits AT a
    threshold (a room lamp put one camera at exactly ~10 lux, another's
    long exposure at luma ~202). Hardening:

    - transitions need ``confirm_frames`` consecutive agreeing frames
      (a lone blip in either direction is ignored);
    - a blown-frame exit (saturated sensor: lux is untrustworthy, but a
      blown long exposure IS bright light) starts a re-entry cooldown so
      a night-time light source causes at most one flip per cooldown;
    - a high-lux exit (real daylight, trustworthy) needs no cooldown.
    """

    def __init__(
        self,
        lux_on: float = NIGHT_LUX_ON,
        lux_off: float = NIGHT_LUX_OFF,
        luma_exit: float = NIGHT_LUMA_EXIT,
        confirm_frames: int = NIGHT_CONFIRM_FRAMES,
        cooldown_s: float = NIGHT_REENTRY_COOLDOWN_S,
        clock: Any = time.monotonic,
    ) -> None:
        self._lux_on = lux_on
        self._lux_off = lux_off
        self._luma_exit = luma_exit
        self._confirm = confirm_frames
        self._cooldown_s = cooldown_s
        self._clock = clock
        self.is_night = False
        self._dark_streak = 0
        self._exit_streak = 0
        self._cooldown_until = 0.0

    def update(self, lux: float | None, luma: float | None) -> bool:
        """Feed one frame's measurements; returns the desired mode."""
        if self.is_night:
            blown = luma is not None and luma >= self._luma_exit
            bright = lux is not None and lux >= self._lux_off
            if blown or bright:
                self._exit_streak += 1
                if self._exit_streak >= self._confirm:
                    self.is_night = False
                    self._exit_streak = 0
                    if blown:
                        # untrusted brightness source — back off before
                        # trusting the lux entry threshold again
                        self._cooldown_until = (
                            self._clock() + self._cooldown_s
                        )
            else:
                self._exit_streak = 0
        else:
            self._dark_streak = 0 if (
                lux is None or lux >= self._lux_on
            ) else self._dark_streak + 1
            if (
                self._dark_streak >= self._confirm
                and self._clock() >= self._cooldown_until
            ):
                self.is_night = True
                self._dark_streak = 0
        return self.is_night


def mean_luma(jpeg: bytes) -> float | None:
    """Cheap mean luminance (0-255) of a JPEG; None if PIL is missing."""
    try:
        from PIL import Image  # picamera2 depends on PIL, present on Pis
    except ImportError:
        return None
    import io as _io

    image = Image.open(_io.BytesIO(jpeg)).convert("L").resize((32, 32))
    data = list(image.getdata())
    return sum(data) / len(data)


class Picamera2Camera:
    """Real camera via picamera2 — preview configuration, BGR888 (legacy)."""

    model: str | None = None

    def __init__(
        self,
        tz: tzinfo,
        size: tuple[int, int] = (1280, 720),
        max_exposure_ms: int = 0,
        tuning_file: str | None = None,
        night_exposure_ms: int = 0,
        night_gain: float = 8.0,
        raw_size: tuple[int, int] | None = None,
        clock: Callable[[], datetime] | None = None,
        night_fn: Callable[[], bool] | None = None,
    ) -> None:
        self._tz = tz
        self._size = size
        self._clock = clock if clock is not None else (lambda: datetime.now(tz))
        self._max_exposure_ms = max_exposure_ms
        self._tuning_file = tuning_file
        self._night_exposure_ms = night_exposure_ms
        self._night_gain = night_gain
        self._raw_size = raw_size
        self._night = NightController()
        self._last_luma: float | None = None  # last night frame's mean luma
        # design 14 §6b: solar-time night decision. When set, it REPLACES
        # the lux state machine below (kept but bypassed until the
        # real-dawn verification on a 92-x device — phase 7.6/7.8).
        self._night_fn = night_fn
        self._time_night = False
        self._cam: Any = None

    @property
    def is_night(self) -> bool:
        if self._night_fn is not None:
            return self._time_night
        return self._night.is_night

    def start(self) -> None:
        try:
            from picamera2 import Picamera2  # only importable on the Pi
        except ImportError as exc:
            raise CameraError("picamera2 is not available on this system") from exc
        tuning = None
        if self._tuning_file:
            # e.g. "imx219_noir.json" — corrects AWB for filterless NoIR
            # modules (whites render pink under the default tuning).
            tuning = Picamera2.load_tuning_file(self._tuning_file)
        cam = Picamera2(tuning=tuning)
        controls: dict[str, Any] = {}
        if self._max_exposure_ms > 0:
            # Let AE extend exposure up to the configured ceiling at night
            # (stock preview config caps frame duration at ~66 ms, which
            # blinds low-light sensors like the IMX462 — see
            # docs/reference/rpi-camera-list.md).
            controls["FrameDurationLimits"] = (
                FRAME_DURATION_MIN_US,
                self._max_exposure_ms * 1000,
            )
        kwargs: dict[str, Any] = {}
        if self._raw_size is not None:
            # Pin the sensor mode — some sensors' auto-picked video modes
            # crop the FoV (OV5647 1080p uses 74% of the sensor width).
            kwargs["raw"] = {"size": self._raw_size}
        config = cam.create_preview_configuration(
            main={"format": "BGR888", "size": self._size},
            controls=controls,
            **kwargs,
        )
        cam.configure(config)
        cam.start()
        time.sleep(1)  # let AE/AWB settle after start, as the legacy code did
        self._cam = cam
        self.model = resolve_camera_model(cam.camera_properties.get("Model"))

    def capture_jpeg(self) -> tuple[bytes, datetime, dict[str, Any]]:
        if self._cam is None:
            raise CameraError("Picamera2Camera used before start()")
        if self._night_exposure_ms > 0 and self._night_fn is not None:
            # solar-time night mode (design 14 §6b): a pure clock decision
            # per capture — no AE probe, no lux hysteresis, no flapping.
            self._apply_time_night(bool(self._night_fn()))
        elif self._night_exposure_ms > 0 and self._night.is_night:
            # Night exit is decided from the probe's trusted lux, once per
            # cycle — the manual frame below feeds no measurement.
            self._probe_ae()
        captured_at = self._clock()
        buffer = io.BytesIO()
        request = self._cam.capture_request()
        try:
            request.save("main", buffer, format="jpeg")
            metadata = request.get_metadata()
        finally:
            request.release()
        jpeg = buffer.getvalue()
        if self._night_exposure_ms > 0 and self._night_fn is None:
            if self._night.is_night:
                # Remember this manual frame's brightness for the next
                # probe: the blown-frame (luma) escape needs it — a scene
                # can blow out at a metered lux still below NIGHT_LUX_OFF
                # (a bench IMX462 dawn 2026-08-25: frames white from ~05:50,
                # metered lux reached 30 only at 06:24).
                self._last_luma = mean_luma(jpeg)
            else:
                # Day side: AE metadata lux is trustworthy as-is.
                self._update_night_mode(metadata.get("Lux"), None)
        return jpeg, captured_at, metadata

    def _apply_time_night(self, want: bool) -> None:
        """Switch the exposure regime when the solar decision changed
        (design 14 §6b); drains the pipeline so no frame of the previous
        regime reaches the upload (same guarantee as the lux path)."""
        if want == self._time_night:
            return
        self._time_night = want
        if want:
            self._cam.set_controls({
                "AeEnable": False,
                "ExposureTime": self._night_exposure_ms * 1000,
                "AnalogueGain": self._night_gain,
            })
            log.info(
                "night mode ON (solar) exposure_ms=%d gain=%.1f",
                self._night_exposure_ms, self._night_gain,
            )
        else:
            self._cam.set_controls({"AeEnable": True})
            log.info("night mode OFF (solar)")
        self._drain_until(want_manual=want)

    def _is_manual_exposure(self, metadata: dict[str, Any]) -> bool:
        target_us = self._night_exposure_ms * 1000
        exposure = metadata.get("ExposureTime")
        return (
            exposure is not None
            and abs(exposure - target_us) <= target_us * NIGHT_EXPOSURE_TOLERANCE
        )

    def _drain_until(self, want_manual: bool) -> dict[str, Any]:
        """Consume queued frames until one carries the wanted exposure
        regime (manual night exposure or not-manual = AE). The pipeline
        buffers frames, so right after set_controls the queue still holds
        frames from the PREVIOUS regime — a fixed sleep let AE-metered
        frames (gain 90+) leak into night uploads as flicker (measured
        a bench IMX462 2026-08-23). Bounded: returns the last metadata even if
        the target regime never shows, and logs the miss."""
        metadata: dict[str, Any] = {}
        for _ in range(NIGHT_SETTLE_MAX_FRAMES):
            metadata = self._cam.capture_metadata()
            if self._is_manual_exposure(metadata) == want_manual:
                return metadata
        log.warning(
            "night settle: %s exposure not reached after %d frames",
            "manual" if want_manual else "AE", NIGHT_SETTLE_MAX_FRAMES,
        )
        return metadata

    def _probe_ae(self) -> None:
        """AE metering probe before each night capture: re-enable AE, let
        it settle, and feed the TRUE scene lux to the controller. Under a
        fixed manual night exposure a brightening scene saturates the
        sensor and caps the lux estimate below NIGHT_LUX_OFF, so night
        mode exited dawn ~25 min late (measured 2026-08-22, a bench IMX462).
        The AE frames are metering-only; the uploaded frame is captured
        only after the drain proves the manual exposure is live again."""
        self._cam.set_controls({"AeEnable": True})
        time.sleep(NIGHT_PROBE_SETTLE_S)
        # discard queued manual-exposure frames; read lux from a real AE frame
        lux = self._drain_until(want_manual=False).get("Lux")
        # feed the previous manual frame's luma too: the blown-frame escape
        # exits scenes that overexpose below the lux threshold
        if self._night.update(lux, self._last_luma):
            # staying in night mode: restore the manual night exposure and
            # drain until it is live so no AE frame reaches the upload
            self._cam.set_controls({
                "AeEnable": False,
                "ExposureTime": self._night_exposure_ms * 1000,
                "AnalogueGain": self._night_gain,
            })
            self._drain_until(want_manual=True)
        else:
            log.info(
                "night mode OFF (AE probe) lux=%.1f luma=%.0f",
                -1.0 if lux is None else lux,
                -1.0 if self._last_luma is None else self._last_luma,
            )
            self._last_luma = None

    def _update_night_mode(self, lux: float | None, luma: float | None) -> None:
        """Legacy camera_viewer.py AEC pattern: below the lux threshold,
        disable AE and set manual ExposureTime/AnalogueGain (the tuning
        file caps AE shutter at ~66 ms, far too short for night); above
        it, hand control back to AE. Applies to the NEXT capture."""
        was_night = self._night.is_night
        want_night = self._night.update(lux, luma)
        if want_night == was_night:
            return
        if want_night:
            self._last_luma = None  # never trust a previous night's frames
            self._cam.set_controls({
                "AeEnable": False,
                "ExposureTime": self._night_exposure_ms * 1000,
                "AnalogueGain": self._night_gain,
            })
            log.info(
                "night mode ON lux=%.1f exposure_ms=%d gain=%.1f",
                -1.0 if lux is None else lux,
                self._night_exposure_ms, self._night_gain,
            )
        else:
            self._cam.set_controls({"AeEnable": True})
            log.info(
                "night mode OFF lux=%.1f luma=%.0f",
                -1.0 if lux is None else lux,
                -1.0 if luma is None else luma,
            )


    def stop(self) -> None:
        if self._cam is not None:
            self._cam.stop()
            self._cam.close()
            self._cam = None
