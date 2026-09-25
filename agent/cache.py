"""On-disk spill cache for frames captured while offline — design 12 §3.

Layout (``CACHE_DIR/spill/``): ``{ulid}.jpg`` (frame bytes exactly as
captured) + ``{ulid}.json`` (stamp: when it was captured, by which clock
quality, plus the camera metadata for the hardware sidecar). ULID names
sort in capture order and are unique across reboots.

Guarantees:

- **Atomic**: each file is written to ``.tmp`` and renamed into place; the
  frame is fsynced. The sidecar is written first, so a frame that exists
  always has a complete sidecar unless the card lost the write (then the
  frame falls back to a provisional stamp taken from its ULID).
- **Bounded**: ``max_frames`` and a free-space floor; the oldest frame is
  evicted first and counted (``evicted``), mirroring the in-memory queue's
  drop-oldest rule.
- **Never raises** into the uploader: disk trouble sets ``error`` and
  ``put()`` returns False (the frame is then dropped as today).
- Only the uploader thread touches the store; the capture loop never does.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ulid import ULID

from agent.capture import CaptureItem
from agent.clock import QUALITY_PROVISIONAL
from agent.constants import SIDECAR_META_KEYS
from dam_shared.constants import JPG_SUFFIX

log = logging.getLogger(__name__)

STAMP_SUFFIX = ".json"
TMP_SUFFIX = ".tmp"
STAMP_VERSION = 1


def _default_disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


@dataclass(frozen=True)
class CachedFrame:
    """One frame in the store, as seen by the replay scheduler (5.5)."""

    ulid: str
    path: Path  # the .jpg
    size: int
    captured_utc: datetime  # aware, UTC — best estimate at capture (or restamped)
    captured_mono: float | None  # None when the stamp was lost
    boot_id: str
    time_quality: str
    timezone: str
    camera: dict[str, Any]
    drift_s: float = 0.0
    torn: bool = False  # sidecar missing/unreadable → provisional from the ULID

    def read_jpeg(self) -> bytes:
        return self.path.read_bytes()


class SpillStore:
    def __init__(
        self,
        root: Path,
        *,
        max_frames: int,
        min_free_mb: int,
        max_age_days: int,
        boot_id: str,
        timezone: str,
        disk_free: Callable[[Path], int] = _default_disk_free,
    ) -> None:
        self.root = Path(root)
        self._max_frames = int(max_frames)
        self._min_free_bytes = int(min_free_mb) * 1024 * 1024
        self._max_age = timedelta(days=int(max_age_days))
        self._boot_id = boot_id
        self._timezone = timezone
        self._disk_free = disk_free
        self._sizes: dict[str, int] = {}  # ulid -> frame bytes (index)
        self.evicted = 0
        self.discarded = 0
        self.error: str | None = None
        self._scan()

    # ── index ───────────────────────────────────────────────────────────────

    def _scan(self) -> None:
        """Rebuild the index from disk (agent start); drop leftovers of
        interrupted writes and sidecars whose frame never made it."""
        self._sizes.clear()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            entries = list(self.root.iterdir())
        except OSError as exc:
            self._fail(f"cache dir unusable: {exc}")
            return
        frames = {p.stem for p in entries if p.suffix == JPG_SUFFIX}
        for path in entries:
            if path.name.endswith(TMP_SUFFIX):
                self._unlink(path)  # torn write
            elif path.suffix == STAMP_SUFFIX and path.stem not in frames:
                self._unlink(path)  # orphan sidecar
            elif path.suffix == JPG_SUFFIX:
                try:
                    self._sizes[path.stem] = path.stat().st_size
                except OSError:
                    continue
        if self._sizes:
            log.info("spill cache: %d frame(s) waiting in %s", len(self._sizes), self.root)

    def _fail(self, message: str) -> None:
        if self.error != message:
            log.error("spill cache: %s", message)
        self.error = message

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def _frame_path(self, ulid: str) -> Path:
        return self.root / f"{ulid}{JPG_SUFFIX}"

    def _stamp_path(self, ulid: str) -> Path:
        return self.root / f"{ulid}{STAMP_SUFFIX}"

    def _oldest_first(self) -> list[str]:
        return sorted(self._sizes)

    # ── public API ──────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        return {
            "frames": len(self._sizes),
            "bytes": sum(self._sizes.values()),
            "evicted": self.evicted,
            "discarded": self.discarded,
            "error": self.error,
        }

    def __len__(self) -> int:
        return len(self._sizes)

    def put(self, item: CaptureItem) -> bool:
        """Spill one frame. False (and ``error`` set) when the disk cannot
        take it — the caller drops the frame as it would from the queue."""
        if not self.evict_to_bounds(incoming=len(item.jpeg)):
            return False
        stamp = {
            "version": STAMP_VERSION,
            "ulid": item.ulid,
            "captured_utc": item.captured_at.astimezone(UTC).isoformat(),
            "captured_mono": item.captured_mono,
            "boot_id": self._boot_id,
            "time_quality": item.time_quality,
            "timezone": self._timezone,
            "camera": _camera_subset(item.camera_metadata),
        }
        try:
            self._write_atomic(self._stamp_path(item.ulid), json.dumps(stamp).encode(), fsync=False)
            self._write_atomic(self._frame_path(item.ulid), item.jpeg, fsync=True)
        except OSError as exc:
            self._unlink(self._stamp_path(item.ulid))
            self._fail(f"write failed: {exc}")
            return False
        self._sizes[item.ulid] = len(item.jpeg)
        if self.error is not None:
            log.info("spill cache: writable again")
            self.error = None
        return True

    def _write_atomic(self, path: Path, data: bytes, *, fsync: bool) -> None:
        tmp = path.with_name(path.name + TMP_SUFFIX)
        with open(tmp, "wb") as handle:
            handle.write(data)
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(tmp, path)

    def evict_to_bounds(self, incoming: int = 0) -> bool:
        """Make room for ``incoming`` bytes: evict oldest while over
        ``max_frames`` or under the free-space floor. False when the floor
        cannot be met even with the store empty (card full for other
        reasons)."""
        while self._sizes and len(self._sizes) >= self._max_frames:
            self._evict(self._oldest_first()[0])
        while self._free() - incoming < self._min_free_bytes:
            if not self._sizes:
                self._fail("free space below floor and nothing left to evict")
                return False
            self._evict(self._oldest_first()[0])
        return True

    def _free(self) -> int:
        try:
            return int(self._disk_free(self.root))
        except OSError:
            return 0

    def _evict(self, ulid: str) -> None:
        self.delete(ulid)
        self.evicted += 1
        log.warning("spill cache full - evicted oldest frame %s (evicted=%d)", ulid, self.evicted)

    def delete(self, ulid: str) -> None:
        """Remove a frame and its stamp (after a verified upload, or when
        the operator paused/unassigned the device). Unknown ulid: no-op."""
        self._unlink(self._frame_path(ulid))
        self._unlink(self._stamp_path(ulid))
        self._sizes.pop(ulid, None)

    def iter_oldest(self) -> Iterator[CachedFrame]:
        """Frames in capture (ULID) order. A frame whose stamp is missing or
        unreadable is yielded ``torn`` with a provisional time taken from
        its ULID (design 12 §3.1)."""
        for ulid in self._oldest_first():
            path = self._frame_path(ulid)
            size = self._sizes.get(ulid, 0)
            try:
                stamp = json.loads(self._stamp_path(ulid).read_text(encoding="utf-8"))
                yield CachedFrame(
                    ulid=ulid,
                    path=path,
                    size=size,
                    captured_utc=datetime.fromisoformat(stamp["captured_utc"]).astimezone(UTC),
                    captured_mono=(
                        float(stamp["captured_mono"])
                        if stamp.get("captured_mono") is not None else None
                    ),
                    boot_id=str(stamp.get("boot_id") or ""),
                    time_quality=str(stamp.get("time_quality") or QUALITY_PROVISIONAL),
                    timezone=str(stamp.get("timezone") or self._timezone),
                    camera=dict(stamp.get("camera") or {}),
                    drift_s=float(stamp.get("drift_s") or 0.0),
                )
            except (OSError, ValueError, KeyError, TypeError):
                yield CachedFrame(
                    ulid=ulid,
                    path=path,
                    size=size,
                    captured_utc=_ulid_time(ulid),
                    captured_mono=None,
                    boot_id="",
                    time_quality=QUALITY_PROVISIONAL,
                    timezone=self._timezone,
                    camera={},
                    torn=True,
                )

    def restamp(
        self, ulid: str, captured_utc: datetime, time_quality: str, drift_s: float = 0.0
    ) -> bool:
        """Rewrite a frame's stamp after a resync (design 12 §4.3/§4.4).
        Keeps the other fields; a torn stamp is recreated minimally."""
        if ulid not in self._sizes:
            return False
        stamp_path = self._stamp_path(ulid)
        try:
            stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stamp = {"version": STAMP_VERSION, "ulid": ulid, "camera": {},
                     "boot_id": "", "captured_mono": None, "timezone": self._timezone}
        stamp["captured_utc"] = captured_utc.astimezone(UTC).isoformat()
        stamp["time_quality"] = time_quality
        stamp["drift_s"] = float(drift_s)
        stamp["restamped"] = True
        try:
            self._write_atomic(stamp_path, json.dumps(stamp).encode(), fsync=False)
        except OSError as exc:
            self._fail(f"restamp failed: {exc}")
            return False
        return True

    def discard_older_than(self, now_utc: datetime) -> int:
        """Delete frames older than ``max_age_days`` (their S3 day folders
        are expiring anyway — design 12 §6). Returns the count."""
        cutoff = now_utc.astimezone(UTC) - self._max_age
        count = 0
        for frame in list(self.iter_oldest()):
            if frame.captured_utc >= cutoff:
                break  # ULID order ≈ time order; the rest are newer
            self.delete(frame.ulid)
            count += 1
        if count:
            self.discarded += count
            log.warning("spill cache: discarded %d frame(s) older than %s", count, cutoff.date())
        return count


def _camera_subset(metadata: dict[str, Any]) -> dict[str, Any]:
    subset: dict[str, Any] = {}
    for key in SIDECAR_META_KEYS:
        value = metadata.get(key)
        if value is None:
            continue
        subset[key] = list(value) if isinstance(value, tuple | list) else value
    return subset


def _ulid_time(ulid: str) -> datetime:
    """The ULID's embedded millisecond timestamp (the generating clock at
    capture) — the only time left when a stamp is lost."""
    try:
        return ULID.from_str(ulid).datetime.astimezone(UTC)
    except (ValueError, TypeError):
        return datetime.now(UTC)
