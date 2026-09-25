"""Tests for agent.config — stage env loading and validation."""

import pytest

from agent.config import ConfigError, Settings, load_settings

MINIMAL_ENV = (
    "LOCATION_ID=TEST\n"
    "DEVICE_ID=dam-test\n"
    "TIMEZONE=Asia/Seoul\n"
    "UPLOAD_SIGNER_URL=https://signer.example\n"
    "DEVICE_TOKEN=test-token\n"
)


def _write_env(tmp_path, content):
    env_file = tmp_path / ".env.test"
    env_file.write_text(content, encoding="utf-8")
    return env_file


def test_minimal_env_uses_defaults(tmp_path):
    settings = load_settings(stage="test", env_file=_write_env(tmp_path, MINIMAL_ENV))
    assert settings == Settings(
        stage="test",
        location_id="TEST",
        device_id="dam-test",
        timezone="Asia/Seoul",
        upload_signer_url="https://signer.example",
        device_token="test-token",
    )
    assert settings.video_minutes == 1
    assert settings.capture_size == (1280, 720)


@pytest.mark.parametrize(
    ("video_minutes", "expected_interval"),
    [(1, 48), (2, 24), (3, 16)],
)
def test_interval_table(tmp_path, video_minutes, expected_interval):
    env_file = _write_env(tmp_path, MINIMAL_ENV + f"VIDEO_MINUTES={video_minutes}\n")
    settings = load_settings(stage="test", env_file=env_file)
    assert settings.interval_s == expected_interval


def test_video_minutes_must_be_positive(tmp_path):
    env_file = _write_env(tmp_path, MINIMAL_ENV + "VIDEO_MINUTES=0\n")
    with pytest.raises(ConfigError, match="VIDEO_MINUTES"):
        load_settings(stage="test", env_file=env_file)


def test_cache_max_age_defaults_to_lifecycle(tmp_path):
    settings = load_settings(stage="test", env_file=_write_env(tmp_path, MINIMAL_ENV))
    assert settings.cache_max_age_days == 30  # = the S3 image lifecycle (design 12)


def test_cache_max_age_override(tmp_path):
    env_file = _write_env(tmp_path, MINIMAL_ENV + "CACHE_MAX_AGE_DAYS=7\n")
    assert load_settings(stage="test", env_file=env_file).cache_max_age_days == 7


def test_spill_cache_defaults(tmp_path):
    # design 12 §7 defaults (phase 5.2)
    s = load_settings(stage="test", env_file=_write_env(tmp_path, MINIMAL_ENV))
    assert s.cache_dir == "/var/cache/dam-agent"
    assert s.cache_max_frames == 54_000
    assert s.cache_min_free_mb == 512
    assert s.cache_keep_provisional is True
    assert s.offline_after_failures == 3
    assert s.offline_probe_s == 30
    assert s.replay_min_gap_s == 1.0
    assert s.anchor_refresh_s == 600


def test_spill_cache_overrides(tmp_path):
    env_file = _write_env(
        tmp_path,
        MINIMAL_ENV
        + "CACHE_DIR=/tmp/dam-cache\nCACHE_MAX_FRAMES=1000\nCACHE_MIN_FREE_MB=64\n"
        + "CACHE_KEEP_PROVISIONAL=no\nOFFLINE_AFTER_FAILURES=5\nOFFLINE_PROBE_S=10\n"
        + "REPLAY_MIN_GAP_S=0.25\nANCHOR_REFRESH_S=120\n",
    )
    s = load_settings(stage="test", env_file=env_file)
    assert s.cache_dir == "/tmp/dam-cache"
    assert s.cache_max_frames == 1000
    assert s.cache_min_free_mb == 64
    assert s.cache_keep_provisional is False
    assert s.offline_after_failures == 5
    assert s.offline_probe_s == 10
    assert s.replay_min_gap_s == 0.25
    assert s.anchor_refresh_s == 120


def test_empty_cache_dir_falls_back_to_default(tmp_path):
    env_file = _write_env(tmp_path, MINIMAL_ENV + "CACHE_DIR=\n")
    assert load_settings(stage="test", env_file=env_file).cache_dir == "/var/cache/dam-agent"


def test_max_exposure_defaults_to_disabled(tmp_path):
    settings = load_settings(stage="test", env_file=_write_env(tmp_path, MINIMAL_ENV))
    assert settings.max_exposure_ms == 0


def test_max_exposure_override(tmp_path):
    env_file = _write_env(tmp_path, MINIMAL_ENV + "MAX_EXPOSURE_MS=5000\n")
    settings = load_settings(stage="test", env_file=env_file)
    assert settings.max_exposure_ms == 5000


def test_tuning_file_defaults_to_none(tmp_path):
    settings = load_settings(stage="test", env_file=_write_env(tmp_path, MINIMAL_ENV))
    assert settings.tuning_file is None


def test_tuning_file_override(tmp_path):
    env_file = _write_env(tmp_path, MINIMAL_ENV + "TUNING_FILE=imx219_noir.json\n")
    settings = load_settings(stage="test", env_file=env_file)
    assert settings.tuning_file == "imx219_noir.json"


def test_night_mode_defaults_disabled(tmp_path):
    settings = load_settings(stage="test", env_file=_write_env(tmp_path, MINIMAL_ENV))
    assert settings.night_exposure_ms == 0
    assert settings.night_gain == 8.0


def test_night_mode_override(tmp_path):
    env_file = _write_env(
        tmp_path, MINIMAL_ENV + "NIGHT_EXPOSURE_MS=1000\nNIGHT_GAIN=4\n"
    )
    settings = load_settings(stage="test", env_file=env_file)
    assert settings.night_exposure_ms == 1000
    assert settings.night_gain == 4.0


def test_max_exposure_must_not_be_negative(tmp_path):
    env_file = _write_env(tmp_path, MINIMAL_ENV + "MAX_EXPOSURE_MS=-1\n")
    with pytest.raises(ConfigError, match="MAX_EXPOSURE_MS"):
        load_settings(stage="test", env_file=env_file)


def test_overrides_are_read(tmp_path):
    env_file = _write_env(
        tmp_path,
        MINIMAL_ENV + "VIDEO_MINUTES=2\nCAPTURE_SIZE=1920,1080\nVIEWER_PORT=0\n",
    )
    settings = load_settings(stage="test", env_file=env_file)
    assert settings.video_minutes == 2
    assert settings.capture_size == (1920, 1080)
    assert settings.viewer_port == 0


def test_missing_required_key_fails_loudly(tmp_path):
    env_file = _write_env(tmp_path, "LOCATION_ID=TEST\n")
    with pytest.raises(ConfigError, match="TIMEZONE"):
        load_settings(stage="test", env_file=env_file)


def test_missing_stage_file_fails_loudly(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(stage="nope", env_file=tmp_path / ".env.nope")


def test_no_stage_fails_loudly(monkeypatch):
    monkeypatch.delenv("STAGE", raising=False)
    with pytest.raises(ConfigError, match="STAGE"):
        load_settings()


def test_bad_capture_size_fails_loudly(tmp_path):
    env_file = _write_env(tmp_path, MINIMAL_ENV + "CAPTURE_SIZE=wide\n")
    with pytest.raises(ConfigError, match="CAPTURE_SIZE"):
        load_settings(stage="test", env_file=env_file)


def test_raw_size_defaults_to_none(tmp_path):
    settings = load_settings(stage="test", env_file=_write_env(tmp_path, MINIMAL_ENV))
    assert settings.raw_size is None


def test_raw_size_override(tmp_path):
    env_file = _write_env(tmp_path, MINIMAL_ENV + "RAW_SIZE=1296,972\n")
    settings = load_settings(stage="test", env_file=env_file)
    assert settings.raw_size == (1296, 972)


def test_endpoint_defaults_to_the_public_device_domain(tmp_path):
    env_file = _write_env(tmp_path, "TIMEZONE=Asia/Seoul\n")
    settings = load_settings(stage="test", env_file=env_file)
    assert settings.upload_signer_url == "https://device.chase-nova.com"
    assert settings.device_id == "" and settings.device_token == ""


def test_dam_endpoint_wins_over_the_legacy_name(tmp_path):
    env_file = _write_env(
        tmp_path,
        MINIMAL_ENV + "DAM_ENDPOINT=https://dam.example\nENROLLMENT_TOKEN=dame_x\n",
    )
    settings = load_settings(stage="test", env_file=env_file)
    assert settings.upload_signer_url == "https://dam.example"
    assert settings.enrollment_token == "dame_x"
    assert settings.env_file == str(env_file)

