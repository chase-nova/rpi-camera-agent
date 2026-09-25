"""Agent configuration — the single place for settings and magic values.

Loads ``.env.{STAGE}`` (there is no plain ``.env``) and exposes typed
settings. Every other module takes values from here; no literals elsewhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from dam_shared.constants import (
    CAPTURE_DURATION_SECONDS,
    FRAME_PER_MINUTE,
    IMAGE_PREFIX_DEFAULT,
)

# Device endpoint (design 15 §5): the public API Gateway custom domain in
# front of the signer. DAM_ENDPOINT overrides; UPLOAD_SIGNER_URL is the
# pre-15 name and still accepted.
DEFAULT_DAM_ENDPOINT = "https://device.chase-nova.com"
# Device credential written by enrollment (design 15 §6) — survives
# reinstalls of /opt/dam-agent; owner-only file.
DEFAULT_CREDENTIAL_FILE = "/var/lib/dam-agent/credential.json"
# SD-card path: an ENROLLMENT_TOKEN line in this file on the boot (FAT)
# partition, written on the PC right after imaging.
DEFAULT_BOOT_ENV_FILE = "/boot/firmware/dam-agent.env"
DEFAULT_S3_IMAGE_PREFIX = IMAGE_PREFIX_DEFAULT
DEFAULT_VIDEO_MINUTES = 1
DEFAULT_CAPTURE_SIZE = (1280, 720)
DEFAULT_QUEUE_MAX = 64
DEFAULT_VIEWER_PORT = 8080
# Local spill cache + clock holdover (design 12 §7). Frames are written to
# CACHE_DIR only while the uploader is offline and replayed on reconnect.
DEFAULT_CACHE_DIR = "/var/cache/dam-agent"
# 30 days at the 48 s interval (~6.8 GB typical, ~10 GB worst case).
DEFAULT_CACHE_MAX_FRAMES = 54_000
# Stop caching / evict oldest when the card has less than this left.
DEFAULT_CACHE_MIN_FREE_MB = 512
# Cached frames older than this are discarded at replay — the S3 image
# lifecycle is 30 days, so older frames would land in folders that are
# already expiring.
DEFAULT_CACHE_MAX_AGE_DAYS = 30
# Upload frames whose timestamp could never be corrected (captured before
# the clock was re-anchored, in an earlier boot) flagged "provisional",
# instead of discarding them.
DEFAULT_CACHE_KEEP_PROVISIONAL = True
# Consecutive transport failures (DNS, timeout, reset, TLS/portal) before
# the uploader goes offline and starts spilling to disk.
DEFAULT_OFFLINE_AFTER_FAILURES = 3
# /sign probe cadence while offline (no per-frame retry storms).
DEFAULT_OFFLINE_PROBE_S = 30
# Minimum gap between replay PUTs once online (replay runs whenever the
# live queue is empty; ~1 frame/s clears a day's backlog in ~30 min).
DEFAULT_REPLAY_MIN_GAP_S = 1.0
# How often a trusted time reading (NTP / signer Date) refreshes and
# persists the time anchor.
DEFAULT_ANCHOR_REFRESH_S = 600
# Remote Wi-Fi setup (design 13 §3): how long `nmcli connection up` may
# take before the apply counts as failed, and how long to let
# NetworkManager fall back to the previous profile on its own.
DEFAULT_WIFI_APPLY_TIMEOUT_S = 45
DEFAULT_WIFI_FALLBACK_S = 30
# IMX462 night mode by solar time (design 14 §6b): per-site tuning of the
# −4° on / −5° off crossings when the horizon shifts effective light.
DEFAULT_NIGHT_ON_OFFSET_MIN = 0
DEFAULT_NIGHT_OFF_OFFSET_MIN = 0
# Network self-healing (design 12 §3 extension): after this much silence
# (no successful signer round trip) bounce the Wi-Fi radio; after the
# longer window, last-resort reboot. 0 disables the stage.
DEFAULT_NETWORK_KICK_AFTER_S = 600
DEFAULT_NETWORK_REBOOT_AFTER_S = 3600
# Night/long exposure: AE may extend exposure up to this many ms when dark.
# 0 keeps the stock picamera2 ceiling (~66 ms). Only useful on low-light
# sensors (IMX462); keep well under the capture interval (48 s).
DEFAULT_MAX_EXPOSURE_MS = 0
# Optional libcamera tuning file name (e.g. "imx219_noir.json" for
# filterless NoIR modules). Empty = picamera2's automatic choice.
DEFAULT_TUNING_FILE = ""
# Manual night mode (legacy camera_viewer.py AEC pattern): AE cannot exceed
# the tuning file's ~66 ms shutter ceiling (measured 2026-08-14), so when
# the scene lux drops below NIGHT_LUX_ON the agent switches to manual
# ExposureTime/AnalogueGain, and back to AE above NIGHT_LUX_OFF
# (hysteresis). 0 disables. Sweet spots measured: IMX462+F/0.95 ~1000 ms
# gain 4; IMX477 ~5000 ms gain 10.
DEFAULT_NIGHT_EXPOSURE_MS = 0
DEFAULT_NIGHT_GAIN = 8.0
NIGHT_LUX_ON = 10.0
NIGHT_LUX_OFF = 30.0
# Overexposure escape hatch: a saturated sensor caps the lux estimate, so
# a blown night frame can never reach NIGHT_LUX_OFF (measured: dam-imx462-92
# stuck all-white all morning, lux frozen, 2026-08-16). Mean JPEG luminance
# at or above this exits night mode — a blown long-exposure IS daylight.
NIGHT_LUMA_EXIT = 200
# Anti-flicker hardening (A/B nights of 2026-08-16/17: scenes hovering at
# a threshold made the mode oscillate). Transitions need this many
# consecutive agreeing frames, and after a blown-frame exit the mode
# cannot re-enter night for the cooldown period.
NIGHT_CONFIRM_FRAMES = 3
NIGHT_REENTRY_COOLDOWN_S = 900.0
# AE metering probe (2026-08-23): while night mode is on, every capture
# cycle first re-enables AE, lets it settle, and reads the TRUE scene lux
# for the exit decision. A fixed manual night exposure saturates the
# sensor at dawn, capping the lux estimate far below NIGHT_LUX_OFF
# (measured 2026-08-22: "22 lux" under 500 ms at sunrise, 101+ lux the
# moment AE resumed — night mode exited ~25 min late, one blown minute
# in the daily video). The probe costs a few seconds per 48 s cycle,
# night only.
NIGHT_PROBE_SETTLE_S = 2.0  # AE convergence before reading lux
# After re-applying the manual exposure, DRAIN queued frames until the
# metadata proves it is live — a fixed sleep is not enough: the pipeline
# buffers frames, and capture_request() returns the oldest, so AE-metered
# frames (gain 90+) leaked into uploads (measured: a bench IMX462 2026-08-23
# night, 2 of 3 frames were AE frames — visible flicker at 57-59 s of
# the daily video). Bound the drain so a stuck pipeline cannot hang.
NIGHT_SETTLE_MAX_FRAMES = 20  # ≈5 s at a 250 ms night exposure
NIGHT_EXPOSURE_TOLERANCE = 0.1  # ±10% counts as "the exposure is live"
# Optional raw sensor mode "W,H" (empty = libcamera's choice). Needed on
# sensors whose auto-picked video mode crops the FoV: the OV5647's
# 1920x1080 mode uses only 74% of the sensor width — set RAW_SIZE=1296,972
# (binned, full FoV) there. Measured on dam-ov5647ir-75, 2026-08-15.
DEFAULT_RAW_SIZE = ""
# Live-view boost: while an MJPEG viewer client is connected, extra
# preview captures at this interval refresh the viewer between the
# scheduled uploads (which keep their exact cadence; previews are never
# uploaded).
PREVIEW_INTERVAL_S = 1.0

# Capture cadence constants are shared with the video builder
# (shared/constants.py): FPS, FRAME_PER_MINUTE, CAPTURE_DURATION_SECONDS.

# Thermal protection (design 02-agent-manager.md §5.2). Bench reality:
# a Pi 3 in an enclosure idles ~73 C, so warn/pause/resume sit 5 C above
# the first draft; pause equals the firmware's own soft-throttle point.
# Resume raised 75 -> 77.5 (owner, 2026-08-24): a sun-heated outdoor box
# hovers in the 75-80 band for hours, and a 5 C hysteresis kept capture
# paused the whole time; 2.5 C still prevents pause/resume flapping.
DEFAULT_TEMP_WARN_C = 75.0
DEFAULT_TEMP_PAUSE_C = 80.0
DEFAULT_TEMP_RESUME_C = 77.5
DEFAULT_TEMP_SHUTDOWN_C = 85.0
DEFAULT_TEMP_SHUTDOWN_ENABLED = False  # remote devices must not strand themselves
TEMP_SHUTDOWN_CONSECUTIVE = 3

# LOCATION_ID is optional since phase 2: the manager assigns locations
# (02-agent-manager.md §6); the signer builds authoritative keys. Since
# design 15 the device identity is optional here too: it comes from the
# credential file / enrollment (agent.enroll.resolve_identity), with
# DEVICE_ID + DEVICE_TOKEN kept for the legacy fleet.
_REQUIRED_KEYS = ("TIMEZONE",)


class ConfigError(Exception):
    """Raised when the stage env file is missing or incomplete."""


@dataclass(frozen=True)
class Settings:
    stage: str
    device_id: str
    timezone: str
    upload_signer_url: str
    device_token: str
    location_id: str | None = None  # display-only; assignment is authoritative
    enrollment_token: str | None = None
    credential_file: str = DEFAULT_CREDENTIAL_FILE
    boot_env_file: str = DEFAULT_BOOT_ENV_FILE
    # where the settings came from (enrollment scrubs the spent token there)
    env_file: str | None = field(default=None, compare=False)
    s3_image_prefix: str = DEFAULT_S3_IMAGE_PREFIX
    video_minutes: int = DEFAULT_VIDEO_MINUTES
    capture_size: tuple[int, int] = DEFAULT_CAPTURE_SIZE
    queue_max: int = DEFAULT_QUEUE_MAX
    viewer_port: int = DEFAULT_VIEWER_PORT
    cache_dir: str = DEFAULT_CACHE_DIR
    cache_max_frames: int = DEFAULT_CACHE_MAX_FRAMES
    cache_min_free_mb: int = DEFAULT_CACHE_MIN_FREE_MB
    cache_max_age_days: int = DEFAULT_CACHE_MAX_AGE_DAYS
    cache_keep_provisional: bool = DEFAULT_CACHE_KEEP_PROVISIONAL
    offline_after_failures: int = DEFAULT_OFFLINE_AFTER_FAILURES
    offline_probe_s: int = DEFAULT_OFFLINE_PROBE_S
    replay_min_gap_s: float = DEFAULT_REPLAY_MIN_GAP_S
    anchor_refresh_s: int = DEFAULT_ANCHOR_REFRESH_S
    wifi_apply_timeout_s: int = DEFAULT_WIFI_APPLY_TIMEOUT_S
    wifi_fallback_s: int = DEFAULT_WIFI_FALLBACK_S
    night_on_offset_min: int = DEFAULT_NIGHT_ON_OFFSET_MIN
    night_off_offset_min: int = DEFAULT_NIGHT_OFF_OFFSET_MIN
    network_kick_after_s: int = DEFAULT_NETWORK_KICK_AFTER_S
    network_reboot_after_s: int = DEFAULT_NETWORK_REBOOT_AFTER_S
    temp_warn_c: float = DEFAULT_TEMP_WARN_C
    temp_pause_c: float = DEFAULT_TEMP_PAUSE_C
    temp_resume_c: float = DEFAULT_TEMP_RESUME_C
    temp_shutdown_c: float = DEFAULT_TEMP_SHUTDOWN_C
    temp_shutdown_enabled: bool = DEFAULT_TEMP_SHUTDOWN_ENABLED
    max_exposure_ms: int = DEFAULT_MAX_EXPOSURE_MS
    tuning_file: str | None = None
    night_exposure_ms: int = DEFAULT_NIGHT_EXPOSURE_MS
    night_gain: float = DEFAULT_NIGHT_GAIN
    raw_size: tuple[int, int] | None = None

    @property
    def interval_s(self) -> int:
        """Seconds between captures — one day becomes video_minutes of video."""
        return CAPTURE_DURATION_SECONDS // (FRAME_PER_MINUTE * self.video_minutes)


def _find_env_file(stage: str) -> Path:
    name = f".env.{stage}"
    candidates = [Path.cwd() / name, Path(__file__).resolve().parent.parent / name]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(c) for c in candidates)
    raise ConfigError(f"stage env file {name!r} not found (searched: {searched})")


def _parse_capture_size(raw: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in raw.split(","))
    except ValueError as exc:
        raise ConfigError(f"CAPTURE_SIZE must be 'W,H', got {raw!r}") from exc
    return (width, height)


def read_boot_enrollment_token(path: Path) -> str | None:
    """ENROLLMENT_TOKEN from the boot-partition file, if present."""
    if not path.is_file():
        return None
    return dotenv_values(path).get("ENROLLMENT_TOKEN") or None


def _env_bool(raw: object) -> bool:
    """Env-file boolean: 1/true/yes (any case) → True; everything else False."""
    return str(raw).strip().lower() in ("1", "true", "yes")


def load_settings(stage: str | None = None, env_file: Path | None = None) -> Settings:
    """Load settings for ``stage`` (defaults to the STAGE env var)."""
    stage = stage or os.environ.get("STAGE")
    if not stage:
        raise ConfigError("STAGE is not set and no stage was given")

    path = env_file if env_file is not None else _find_env_file(stage)
    if not Path(path).is_file():
        raise ConfigError(f"stage env file not found: {path}")
    values = {k: v for k, v in dotenv_values(path).items() if v is not None}

    missing = [key for key in _REQUIRED_KEYS if not values.get(key)]
    if missing:
        raise ConfigError(f"missing required keys in {path}: {', '.join(missing)}")

    video_minutes = int(values.get("VIDEO_MINUTES", DEFAULT_VIDEO_MINUTES))
    if video_minutes < 1:
        raise ConfigError(f"VIDEO_MINUTES must be >= 1, got {video_minutes}")

    max_exposure_ms = int(values.get("MAX_EXPOSURE_MS", DEFAULT_MAX_EXPOSURE_MS))
    if max_exposure_ms < 0:
        raise ConfigError(f"MAX_EXPOSURE_MS must be >= 0, got {max_exposure_ms}")

    night_exposure_ms = int(
        values.get("NIGHT_EXPOSURE_MS", DEFAULT_NIGHT_EXPOSURE_MS)
    )
    if night_exposure_ms < 0:
        raise ConfigError(
            f"NIGHT_EXPOSURE_MS must be >= 0, got {night_exposure_ms}"
        )

    return Settings(
        stage=stage,
        location_id=values.get("LOCATION_ID") or None,
        # identity may be empty here — resolve_identity fills it
        device_id=values.get("DEVICE_ID", ""),
        timezone=values["TIMEZONE"],
        upload_signer_url=(
            values.get("DAM_ENDPOINT")
            or values.get("UPLOAD_SIGNER_URL")
            or DEFAULT_DAM_ENDPOINT
        ),
        device_token=values.get("DEVICE_TOKEN", ""),
        enrollment_token=values.get("ENROLLMENT_TOKEN") or None,
        credential_file=values.get("CREDENTIAL_FILE") or DEFAULT_CREDENTIAL_FILE,
        boot_env_file=values.get("BOOT_ENV_FILE") or DEFAULT_BOOT_ENV_FILE,
        env_file=str(path),
        s3_image_prefix=values.get("S3_IMAGE_PREFIX", DEFAULT_S3_IMAGE_PREFIX),
        video_minutes=video_minutes,
        capture_size=(
            _parse_capture_size(values["CAPTURE_SIZE"])
            if "CAPTURE_SIZE" in values
            else DEFAULT_CAPTURE_SIZE
        ),
        queue_max=int(values.get("QUEUE_MAX", DEFAULT_QUEUE_MAX)),
        viewer_port=int(values.get("VIEWER_PORT", DEFAULT_VIEWER_PORT)),
        cache_dir=values.get("CACHE_DIR", DEFAULT_CACHE_DIR) or DEFAULT_CACHE_DIR,
        cache_max_frames=int(values.get("CACHE_MAX_FRAMES", DEFAULT_CACHE_MAX_FRAMES)),
        cache_min_free_mb=int(
            values.get("CACHE_MIN_FREE_MB", DEFAULT_CACHE_MIN_FREE_MB)
        ),
        cache_max_age_days=int(
            values.get("CACHE_MAX_AGE_DAYS", DEFAULT_CACHE_MAX_AGE_DAYS)
        ),
        cache_keep_provisional=_env_bool(
            values.get("CACHE_KEEP_PROVISIONAL", DEFAULT_CACHE_KEEP_PROVISIONAL)
        ),
        offline_after_failures=int(
            values.get("OFFLINE_AFTER_FAILURES", DEFAULT_OFFLINE_AFTER_FAILURES)
        ),
        offline_probe_s=int(values.get("OFFLINE_PROBE_S", DEFAULT_OFFLINE_PROBE_S)),
        replay_min_gap_s=float(
            values.get("REPLAY_MIN_GAP_S", DEFAULT_REPLAY_MIN_GAP_S)
        ),
        anchor_refresh_s=int(values.get("ANCHOR_REFRESH_S", DEFAULT_ANCHOR_REFRESH_S)),
        wifi_apply_timeout_s=int(
            values.get("WIFI_APPLY_TIMEOUT_S", DEFAULT_WIFI_APPLY_TIMEOUT_S)
        ),
        wifi_fallback_s=int(values.get("WIFI_FALLBACK_S", DEFAULT_WIFI_FALLBACK_S)),
        night_on_offset_min=int(
            values.get("NIGHT_ON_OFFSET_MIN", DEFAULT_NIGHT_ON_OFFSET_MIN)
        ),
        night_off_offset_min=int(
            values.get("NIGHT_OFF_OFFSET_MIN", DEFAULT_NIGHT_OFF_OFFSET_MIN)
        ),
        network_kick_after_s=int(
            values.get("NETWORK_KICK_AFTER_S", DEFAULT_NETWORK_KICK_AFTER_S)
        ),
        network_reboot_after_s=int(
            values.get("NETWORK_REBOOT_AFTER_S", DEFAULT_NETWORK_REBOOT_AFTER_S)
        ),
        temp_warn_c=float(values.get("TEMP_WARN_C", DEFAULT_TEMP_WARN_C)),
        temp_pause_c=float(values.get("TEMP_PAUSE_C", DEFAULT_TEMP_PAUSE_C)),
        temp_resume_c=float(values.get("TEMP_RESUME_C", DEFAULT_TEMP_RESUME_C)),
        temp_shutdown_c=float(
            values.get("TEMP_SHUTDOWN_C", DEFAULT_TEMP_SHUTDOWN_C)
        ),
        temp_shutdown_enabled=_env_bool(
            values.get("TEMP_SHUTDOWN_ENABLED", DEFAULT_TEMP_SHUTDOWN_ENABLED)
        ),
        max_exposure_ms=max_exposure_ms,
        tuning_file=values.get("TUNING_FILE", DEFAULT_TUNING_FILE) or None,
        night_exposure_ms=night_exposure_ms,
        night_gain=float(values.get("NIGHT_GAIN", DEFAULT_NIGHT_GAIN)),
        raw_size=(
            _parse_capture_size(values["RAW_SIZE"])
            if values.get("RAW_SIZE")
            else None
        ),
    )
