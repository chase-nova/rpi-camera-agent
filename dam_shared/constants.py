"""Cross-service constants (CLAUDE.md code style).

Values shared by the device agent and the cloud services (upload-signer,
upload-monitor, video-builder). This package is PUBLIC (design 15 §7):
it ships with the open-source agent, so it holds no infrastructure names —
bucket and table defaults live in the private ``dam_cloud`` package.
Service-specific constants live in each service's own ``constants.py``.
"""

# ── S3 layout (architecture §7) ──────────────────────────────────────────────
IMAGE_PREFIX_DEFAULT = "images/"
JPG_SUFFIX = ".jpg"
JSON_SUFFIX = ".json"

# ── JPEG magic bytes (damage detection: monitor + builder) ───────────────────
JPEG_SOI = b"\xff\xd8"  # start of image
JPEG_EOI = b"\xff\xd9"  # end of image

# ── content types ────────────────────────────────────────────────────────────
CONTENT_TYPE_JPEG = "image/jpeg"
CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_MP4 = "video/mp4"
CONTENT_TYPE_TEXT = "text/plain"

# ── video math (design 01 §3 / legacy capture-24h.py) ────────────────────────
FPS = 30  # matches the builder's -framerate/-r
FRAME_PER_MINUTE = 60 * FPS
CAPTURE_DURATION_SECONDS = 24 * 60 * 60

# ── dawn & dusk (design 14 §2/§6/§6b; consumed by agent and builder) ─────────
# Solar elevations (degrees; negative = below the horizon).
CIVIL_TWILIGHT_DEG = -6.0     # "dawn"/"dusk" = civil twilight bounds
SUNRISE_SUNSET_DEG = -0.833   # standard refraction + solar semidiameter
NIGHT_ON_DEG = -4.0           # IMX462 night mode ON (evening, ~10 lux)
NIGHT_OFF_DEG = -5.0          # night mode OFF (morning — deliberately early)
# Golden boost (§6): fixed factor, never operator-tunable; window pads.
GOLDEN_BOOST_FACTOR = 4
GOLDEN_PAD_BEFORE_MIN = 5     # window starts at dawn−5 min / sunset−15 min
GOLDEN_PAD_AFTER_SUN_MIN = 15  # ... and ends at sunrise+15 min / dusk+5 min
# Fallbacks when a Post has no coordinates or the sun never crosses the
# elevation (polar day/night): fixed local wall-clock times (§5).
FALLBACK_DAWN = "06:00"
FALLBACK_DUSK = "18:00"
# Nominal window bounds resolved to solar times per day (§5).
WINDOW_NOMINALS = ("dawn", "dusk")
