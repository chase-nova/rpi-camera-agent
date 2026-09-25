"""Agent code constants (CLAUDE.md code style).

Pure code constants for the agent service. Environment-driven settings and
their defaults stay in ``agent.config`` (the env-file config module).
"""

# ── uploader (design 01 §5) ──────────────────────────────────────────────────
BACKOFF_INITIAL_S = 1.0
BACKOFF_CAP_S = 60.0
SIGN_TIMEOUT_S = 30
PUT_TIMEOUT_S = 60
HEARTBEAT_MIN_INTERVAL_S = 30.0  # rest-state heartbeat floor
# Camera metadata worth logging per frame (scalars/short tuples only — the
# full picamera2 metadata also carries matrices and histograms).
SIDECAR_META_KEYS = (
    "ExposureTime", "AnalogueGain", "DigitalGain", "Lux",
    "ColourTemperature", "ColourGains", "ScalerCrop",
    "SensorTemperature", "FocusFoM",
)

# ── capture loop ─────────────────────────────────────────────────────────────
MIN_INTERVAL_S = 2  # capture+upload needs ~1.5 s on a Pi 3
# Stall watchdog (design 01 §8): the loop refreshes ``last_tick`` on every
# iteration. A capture call that never returns — libcamera "Camera frontend
# has timed out" under under-voltage, seen 3× on dam-imx477-45-2 on
# 2026-08-28 — stops the ticks; the agent then exits so systemd
# (Restart=always) brings it back with a freshly initialised camera.
CAPTURE_STALL_FACTOR = 3      # capture intervals without a tick → stalled
CAPTURE_STALL_MIN_S = 180.0   # floor so short intervals don't false-trip
WATCHDOG_POLL_S = 10.0
EXIT_CAPTURE_STALLED = 3      # process exit code of a watchdog exit

# ── camera ───────────────────────────────────────────────────────────────────
# Shortest frame duration we ever ask for (30 fps) when extending the AE
# exposure ceiling via MAX_EXPOSURE_MS.
FRAME_DURATION_MIN_US = 33_333
# The Arducam Pivariety bridge MCU reports its own name instead of the
# sensor behind it; every Pivariety module in this fleet is an IMX462
# (UC-955), so report the real sensor.
CAMERA_MODEL_ALIASES = {"arducam-pivariety": "imx462"}
# Camera-less degraded mode (2026-09-07): with no camera on the CSI bus
# the agent heartbeats `camera_error` and re-probes at this cadence.
CAMERA_RETRY_S = 60.0

# ── viewer (design 01 §6) ────────────────────────────────────────────────────
MJPEG_BOUNDARY = "damframe"
# UDP "connect" target for discovering the outbound interface IP — no
# packet is actually sent; any routable address works.
IP_PROBE_ADDR = ("8.8.8.8", 80)
STREAM_WAIT_S = 1.0  # condition-wait slice so handler threads notice shutdown

# ── local spill cache + clock holdover (design 12) ───────────────────────────
# A trusted reading within this many seconds of the holdover estimate is a
# refresh; a larger jump is logged as a clock correction.
ANCHOR_TOLERANCE_S = 2.0
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
ANCHOR_FILE = "anchor.json"      # under CACHE_DIR
SPILL_SUBDIR = "spill"           # under CACHE_DIR: {ulid}.jpg + {ulid}.json
TIME_QUALITIES = ("synced", "holdover", "provisional")
TIME_SOURCES = ("ntp", "signer", "none")
NET_STATES = ("online", "offline")
# During replay, re-check the age cap every N frames (cheap listing).
REPLAY_DISCARD_CHECK_EVERY = 500
# Interleave one replay frame after every N live frames even while the live
# queue is backed up — on a starved uplink the queue never empties, so the
# empty-queue-only replay rule froze the cache entirely (2026-09-06, 92-1).
REPLAY_INTERLEAVE_EVERY = 5

# ── remote Wi-Fi setup (design 13) ───────────────────────────────────────────
NMCLI_CMD = "nmcli"
NMCLI_SUDO_PREFIX = ["sudo", "-n"]  # sudoers line from provision-pi.sh
NMCLI_PRIVILEGE_ERRORS = ("Insufficient privileges", "not authorized")
WIFI_IFNAME = "wlan0"
WIFI_DNS = "1.1.1.1 8.8.8.8"  # hotspots may hand out no DNS (2026-08-28)
WIFI_SCAN_MAX = 30  # (the old WIFI_PROFILE_PRIORITY pin was removed 2026-09-02)
WIFI_SCAN_TIMEOUT_S = 20.0
WIFI_ACK_FILE = "wifi-ack.json"  # under CACHE_DIR: handled request ids

# ── dawn & dusk (design 14 §4) ───────────────────────────────────────────────
COORDS_FILE = "coords.json"  # under CACHE_DIR: persisted Post coordinates

# ── network self-healing (design 12 §3 extension) ────────────────────────────
REBOOT_CMD = "/usr/sbin/reboot"  # sudoers line installed by provision-pi.sh

# ── main ─────────────────────────────────────────────────────────────────────
UPLOADER_DRAIN_S = 10.0
