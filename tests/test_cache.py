"""Tests for agent.cache — on-disk spill store (design 12 §3)."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ulid import ULID

from agent.cache import SpillStore
from agent.capture import CaptureItem

TZ = ZoneInfo("America/New_York")
T0 = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
BIG_FREE = 10 * 1024 ** 3


def _item(i, *, jpeg=b"\xff\xd8" + b"x" * 100 + b"\xff\xd9", quality="holdover"):
    captured = T0 + timedelta(seconds=48 * i)
    return CaptureItem(
        jpeg=jpeg,
        captured_at=captured.astimezone(TZ),
        ulid=str(ULID.from_timestamp(captured.timestamp())),
        key=f"images/unassigned/2026-08-28/{i:09d}.jpg",
        camera_metadata={"ExposureTime": 1000 * i, "ColourGains": (1.5, 2.0), "Junk": "no"},
        captured_mono=1000.0 + 48 * i,
        time_quality=quality,
    )


def _store(tmp_path, **kw):
    defaults = dict(max_frames=100, min_free_mb=1, max_age_days=30,
                    boot_id="boot-A", timezone="America/New_York",
                    disk_free=lambda p: BIG_FREE)
    defaults.update(kw)
    return SpillStore(tmp_path / "spill", **defaults)


def test_put_iter_order_and_stamp_round_trip(tmp_path):
    store = _store(tmp_path)
    items = [_item(i) for i in (2, 0, 1)]  # out of order on purpose
    assert all(store.put(item) for item in items)
    frames = list(store.iter_oldest())
    assert [f.ulid for f in frames] == [items[1].ulid, items[2].ulid, items[0].ulid]
    f = frames[0]
    assert f.captured_utc == T0 and f.captured_mono == 1000.0
    assert f.boot_id == "boot-A" and f.time_quality == "holdover"
    assert f.timezone == "America/New_York" and not f.torn
    assert f.camera == {"ExposureTime": 0, "ColourGains": [1.5, 2.0]}  # SIDECAR_META_KEYS only
    assert f.read_jpeg() == items[1].jpeg and f.size == len(items[1].jpeg)
    assert store.stats() == {"frames": 3, "bytes": 3 * 104, "evicted": 0, "discarded": 0, "error": None}
    assert not list(tmp_path.glob("spill/*.tmp"))


def test_delete_removes_both_files_and_is_idempotent(tmp_path):
    store = _store(tmp_path)
    item = _item(0)
    store.put(item)
    store.delete(item.ulid)
    store.delete(item.ulid)  # no-op
    assert list((tmp_path / "spill").iterdir()) == []
    assert len(store) == 0


def test_max_frames_evicts_oldest_first(tmp_path):
    store = _store(tmp_path, max_frames=3)
    items = [_item(i) for i in range(4)]
    for item in items:
        assert store.put(item)
    assert [f.ulid for f in store.iter_oldest()] == [i.ulid for i in items[1:]]
    assert store.stats()["evicted"] == 1


def test_free_space_floor_evicts_then_refuses(tmp_path):
    free = {"value": 3 * 1024 * 1024}  # 3 MB free, floor 1 MB
    store = _store(tmp_path, min_free_mb=1, disk_free=lambda p: free["value"])
    a, b = _item(0), _item(1)
    assert store.put(a) and store.put(b)
    free["value"] = 512 * 1024  # card filled up by something else
    c = _item(2)
    assert store.put(c) is False  # evicts a and b, still below the floor → refuse
    assert store.stats()["evicted"] == 2 and len(store) == 0
    assert "floor" in store.error
    free["value"] = BIG_FREE
    assert store.put(c) is True  # recovers
    assert store.error is None


def test_scan_drops_torn_writes_and_orphan_sidecars(tmp_path):
    root = tmp_path / "spill"
    root.mkdir()
    keep = _item(0)
    _store(tmp_path).put(keep)
    (root / "01ZZZZZZZZZZZZZZZZZZZZZZZZ.jpg.tmp").write_bytes(b"partial")
    (root / "01YYYYYYYYYYYYYYYYYYYYYYYY.json").write_text("{}")  # frame never landed
    store = _store(tmp_path)  # restart: index rebuilt from disk
    assert [f.ulid for f in store.iter_oldest()] == [keep.ulid]
    assert sorted(p.name for p in root.iterdir()) == sorted(
        [f"{keep.ulid}.jpg", f"{keep.ulid}.json"]
    )


def test_torn_sidecar_yields_provisional_frame_from_the_ulid(tmp_path):
    store = _store(tmp_path)
    item = _item(3)
    store.put(item)
    (tmp_path / "spill" / f"{item.ulid}.json").write_text("{not json")
    frame = next(iter(store.iter_oldest()))
    assert frame.torn and frame.time_quality == "provisional"
    assert frame.captured_mono is None and frame.boot_id == ""
    assert abs((frame.captured_utc - item.captured_at.astimezone(UTC)).total_seconds()) < 0.01
    assert frame.read_jpeg() == item.jpeg


def test_restamp_round_trip(tmp_path):
    store = _store(tmp_path)
    item = _item(0, quality="provisional")
    store.put(item)
    corrected = T0 + timedelta(seconds=37)
    assert store.restamp(item.ulid, corrected, "holdover", drift_s=1.5)
    frame = next(iter(store.iter_oldest()))
    assert frame.captured_utc == corrected
    assert frame.time_quality == "holdover" and frame.drift_s == 1.5
    assert frame.captured_mono == 1000.0  # other fields kept
    assert json.loads((tmp_path / "spill" / f"{item.ulid}.json").read_text())["restamped"] is True
    assert store.restamp("01ZZZZZZZZZZZZZZZZZZZZZZZZ", corrected, "holdover") is False


def test_discard_older_than_max_age(tmp_path):
    store = _store(tmp_path, max_age_days=30)
    old = _item(0)
    store.put(old)
    fresh = _item(10)
    store.put(fresh)
    now = T0 + timedelta(days=30, minutes=1)  # old is 30 d + 1 min, fresh 30 d − 7 min
    assert store.discard_older_than(now) == 1
    assert [f.ulid for f in store.iter_oldest()] == [fresh.ulid]
    assert store.stats()["discarded"] == 1


def test_unusable_directory_sets_error_and_never_raises(tmp_path):
    blocker = tmp_path / "spill"
    blocker.write_text("I am a file, not a directory")
    store = _store(tmp_path)
    assert store.error and "unusable" in store.error
    assert store.put(_item(0)) is False
    assert store.stats()["frames"] == 0
    assert list(store.iter_oldest()) == []


def test_index_survives_restart(tmp_path):
    store = _store(tmp_path)
    items = [_item(i) for i in range(3)]
    for item in items:
        store.put(item)
    again = _store(tmp_path)
    assert len(again) == 3 and again.stats()["bytes"] == 3 * 104
    assert [f.ulid for f in again.iter_oldest()] == [i.ulid for i in items]
