from __future__ import annotations

import bisect
import datetime as dt
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


EPISODE_RE = re.compile(r"^episode_\d{6}$")
CAMERAS = ("head", "left_wrist", "right_wrist")
CAMERA_KEYS = {
    "head": "observation.images.ego_view",
    "left_wrist": "observation.images.left_wrist",
    "right_wrist": "observation.images.right_wrist",
}
REQUIRED_FILES = (
    "manifest.json",
    "observation_state_frame.jsonl",
    "applied_action_frame.jsonl",
    "events.jsonl",
)
PREVIEW_TARGET_FPS = 20.0
PREVIEW_MAX_CONCURRENCY = 3
_PREVIEW_GENERATION_SLOTS = threading.BoundedSemaphore(PREVIEW_MAX_CONCURRENCY)


class DataProcessError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataProcessError(f"无法读取 JSON: {path}: {exc}") from exc


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise DataProcessError(
                        f"JSONL 解析失败: {path}:{line_number}: {exc}"
                    ) from exc
                if isinstance(value, dict):
                    yield value
    except OSError as exc:
        raise DataProcessError(f"无法读取 JSONL: {path}: {exc}") from exc


def first_last_jsonl(
    path: Path, expected_count: int | None = None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int]:
    """Read only the first/last JSONL records when the manifest provides a count.

    Camera raw episodes contain hundreds of thousands of large JSON records across
    a dataset. Scanning every line made the initial page load needlessly expensive.
    """
    try:
        with path.open("rb") as stream:
            first_line = b""
            while not first_line.strip():
                first_line = stream.readline()
                if not first_line:
                    return None, None, 0

            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            buffer = b""
            last_line = b""
            while position > 0:
                read_size = min(8192, position)
                position -= read_size
                stream.seek(position)
                buffer = stream.read(read_size) + buffer
                stripped = buffer.rstrip(b"\r\n")
                # A newline before the final record means the whole final line is buffered.
                if position == 0 or b"\n" in stripped:
                    lines = [line for line in stripped.splitlines() if line.strip()]
                    if lines:
                        last_line = lines[-1]
                    break
        first = json.loads(first_line)
        last = json.loads(last_line or first_line)
        if not isinstance(first, dict) or not isinstance(last, dict):
            raise ValueError("JSONL record is not an object")
        if expected_count is not None and int(expected_count) > 0:
            count = int(expected_count)
        else:
            # Used for raw data without a counts manifest and for small unit fixtures.
            with path.open("rb") as stream:
                count = sum(1 for line in stream if line.strip())
        return first, last, count
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise DataProcessError(f"无法读取 JSONL 首尾记录: {path}: {exc}") from exc


def row_timestamp_ms(row: dict[str, Any], kind: str) -> float:
    """Return a comparable wall/device timestamp in milliseconds."""
    if kind == "state":
        if row.get("source_timestamp_ms") is not None:
            return float(row["source_timestamp_ms"])
        return float(row["sample_wall_time_ns"]) / 1e6
    if kind == "action":
        if row.get("source_timestamp_ms") is not None:
            return float(row["source_timestamp_ms"])
        return float(row["applied_wall_time_ns"]) / 1e6
    if row.get("device_timestamp_ms") is not None:
        return float(row["device_timestamp_ms"])
    return float(row["receive_wall_time_ns"]) / 1e6


def vector_from_row(row: dict[str, Any], key: str) -> list[float]:
    value = row.get(key)
    if not isinstance(value, list):
        raise DataProcessError(f"数据行缺少 {key} 数组")
    return [float(item) for item in value]


def preview_sampling_plan(
    frame_count: int,
    source_fps: float,
    target_fps: float = PREVIEW_TARGET_FPS,
) -> tuple[list[int], float]:
    """Select fixed-size raw frames without reading frames the UI cannot display.

    Review stepping and exported training data use 20 Hz. Camera capture is normally
    about 30 Hz, so a 20 Hz preview preserves the review timeline while avoiding
    roughly one third of the large NFS reads. The original raw files remain untouched
    and conversion continues to sample them independently.
    """
    if frame_count < 2 or source_fps <= 0 or target_fps <= 0:
        raise DataProcessError("预览采样参数非法")
    if source_fps <= target_fps:
        return list(range(frame_count)), source_fps
    source_duration = (frame_count - 1) / source_fps
    output_count = min(
        frame_count,
        max(2, int(round(source_duration * target_fps)) + 1),
    )
    indices = [
        int(round(index * (frame_count - 1) / (output_count - 1)))
        for index in range(output_count)
    ]
    actual_fps = (output_count - 1) / source_duration
    return indices, actual_fps


@dataclass(frozen=True)
class CameraInfo:
    name: str
    key: str
    width: int
    height: int
    fps: float
    frames: int
    first_timestamp_ms: float
    last_timestamp_ms: float
    raw_size: int
    source_mtime_ns: int

    def as_dict(self, state_start_ms: float) -> dict[str, Any]:
        return {
            "name": self.name,
            "key": self.key,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "frames": self.frames,
            "duration_s": max(0.0, (self.last_timestamp_ms - self.first_timestamp_ms) / 1000),
            "offset_s": (self.first_timestamp_ms - state_start_ms) / 1000,
            "raw_size": self.raw_size,
            "cache_version": f"{self.raw_size:x}-{self.source_mtime_ns:x}",
        }


@dataclass(frozen=True)
class EpisodeInspection:
    episode_id: str
    path: Path
    ready: bool
    status: str
    reason: str
    warnings: tuple[str, ...]
    manifest_id: str | None
    state_frames: int
    action_frames: int
    duration_s: float
    state_start_ms: float | None
    state_end_ms: float | None
    cameras: dict[str, CameraInfo]
    dimensions: int

    def as_dict(self, review: dict[str, Any] | None = None) -> dict[str, Any]:
        review = review or {}
        trim_end = review.get("trim_end_s", self.duration_s)
        excluded = bool(review.get("excluded", False))
        # Review files created before workflow tracking contain a record only
        # after the user saved/auto-trimmed an episode, so record existence is
        # a safe backwards-compatible processed marker.
        processed = bool(review.get("processed", bool(review)))
        exports = review.get("exports", [])
        if not isinstance(exports, list):
            exports = []
        exported = bool(exports)
        if not self.ready or excluded:
            workflow_status = "failed"
        elif exported:
            workflow_status = "exported"
        elif processed:
            workflow_status = "processed"
        else:
            workflow_status = "unprocessed"
        effective_status = "excluded" if excluded and self.ready else self.status
        return {
            "id": self.episode_id,
            "manifest_id": self.manifest_id,
            "status": effective_status,
            "ready": self.ready,
            "excluded": excluded or not self.ready,
            "processed": processed,
            "exported": exported,
            "workflow_status": workflow_status,
            "export_eligible": self.ready and processed and not excluded and not exported,
            "default_success": self.ready and not processed and not excluded,
            "last_export": exports[-1] if exports else None,
            "reason": review.get("reason") or self.reason,
            "warnings": list(self.warnings),
            "state_frames": self.state_frames,
            "action_frames": self.action_frames,
            "duration_s": round(self.duration_s, 3),
            "dimensions": self.dimensions,
            "trim_start_s": float(review.get("trim_start_s", 0.0)),
            "trim_end_s": float(trim_end),
            "note": str(review.get("note", "")),
            "cameras": {
                name: camera.as_dict(self.state_start_ms or camera.first_timestamp_ms)
                for name, camera in self.cameras.items()
            },
        }


class ReviewStore:
    def __init__(self, runtime_root: Path):
        self.root = runtime_root / "reviews"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, raw_root: Path) -> Path:
        digest = hashlib.sha256(str(raw_root.resolve()).encode()).hexdigest()[:16]
        return self.root / f"{digest}.json"

    def load(self, raw_root: Path) -> dict[str, dict[str, Any]]:
        path = self._path(raw_root)
        if not path.exists():
            return {}
        try:
            value = read_json(path)
            episodes = value.get("episodes", {})
            return episodes if isinstance(episodes, dict) else {}
        except DataProcessError:
            return {}

    def save_episode(self, raw_root: Path, episode_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        if not EPISODE_RE.fullmatch(episode_id):
            raise DataProcessError("episode id 非法")
        with self._lock:
            path = self._path(raw_root)
            current: dict[str, Any] = {
                "raw_root": str(raw_root.resolve()),
                "episodes": self.load(raw_root),
            }
            record = current["episodes"].get(episode_id, {})
            record.update(patch)
            current["episodes"][episode_id] = record
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=path.name, dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(current, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.replace(temp_name, path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            return record

    def update_episodes(
        self, raw_root: Path, patches: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Apply several episode updates in one atomic review-file write."""
        for episode_id in patches:
            if not EPISODE_RE.fullmatch(episode_id):
                raise DataProcessError(f"episode id 非法: {episode_id}")
        with self._lock:
            path = self._path(raw_root)
            episodes = self.load(raw_root)
            for episode_id, patch in patches.items():
                record = episodes.get(episode_id, {})
                record.update(patch)
                episodes[episode_id] = record
            current: dict[str, Any] = {
                "raw_root": str(raw_root.resolve()),
                "episodes": episodes,
            }
            fd, temp_name = tempfile.mkstemp(prefix=path.name, dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(current, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.replace(temp_name, path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            return {episode_id: episodes[episode_id] for episode_id in patches}


class RawDataset:
    def __init__(self, raw_root: Path, runtime_root: Path):
        self.root = raw_root.expanduser().resolve()
        if not self.root.is_dir():
            raise DataProcessError(f"原始数据目录不存在: {self.root}")
        self.runtime_root = runtime_root.resolve()
        self.review_store = ReviewStore(self.runtime_root)
        self._cache: dict[str, EpisodeInspection] = {}
        self._cache_lock = threading.Lock()
        self._media_locks: dict[str, threading.Lock] = {}
        self._media_locks_guard = threading.Lock()

    def _media_lock(self, path: Path) -> threading.Lock:
        key = str(path)
        with self._media_locks_guard:
            if key not in self._media_locks:
                self._media_locks[key] = threading.Lock()
            return self._media_locks[key]

    def episode_path(self, episode_id: str) -> Path:
        if not EPISODE_RE.fullmatch(episode_id):
            raise DataProcessError("episode id 非法")
        path = (self.root / episode_id).resolve()
        if path.parent != self.root or not path.is_dir():
            raise DataProcessError(f"episode 不存在: {episode_id}")
        return path

    def episode_ids(self) -> list[str]:
        return sorted(
            path.name
            for path in self.root.iterdir()
            if path.is_dir() and EPISODE_RE.fullmatch(path.name)
        )

    def inspect(self, episode_id: str, refresh: bool = False) -> EpisodeInspection:
        with self._cache_lock:
            if not refresh and episode_id in self._cache:
                return self._cache[episode_id]

        path = self.episode_path(episode_id)
        missing: list[str] = []
        warnings: list[str] = []
        for relative in REQUIRED_FILES:
            candidate = path / relative
            if not candidate.is_file() or candidate.stat().st_size == 0:
                missing.append(relative)
        for camera in CAMERAS:
            for relative in (f"{camera}/rgb.raw", f"{camera}/frames.jsonl"):
                candidate = path / relative
                if not candidate.is_file() or candidate.stat().st_size == 0:
                    missing.append(relative)

        if missing:
            result = EpisodeInspection(
                episode_id=episode_id,
                path=path,
                ready=False,
                status="incomplete",
                reason="缺少采集文件",
                warnings=tuple(missing),
                manifest_id=None,
                state_frames=0,
                action_frames=0,
                duration_s=0.0,
                state_start_ms=None,
                state_end_ms=None,
                cameras={},
                dimensions=0,
            )
            with self._cache_lock:
                self._cache[episode_id] = result
            return result

        try:
            manifest = read_json(path / "manifest.json")
            counts = manifest.get("counts", {})
            manifest_id = str(manifest.get("episode_id") or "") or None
            if manifest_id != episode_id:
                warnings.append(f"manifest episode_id 为 {manifest_id}")
            manifest_status = str(manifest.get("status", "unknown"))
            if manifest_status != "finished":
                warnings.append(f"manifest status={manifest_status}")

            state_first, state_last, state_count = first_last_jsonl(
                path / "observation_state_frame.jsonl",
                counts.get("observation_state_frame"),
            )
            _, _, action_count = first_last_jsonl(
                path / "applied_action_frame.jsonl",
                counts.get("applied_action_frame"),
            )
            if not state_first or not state_last or state_count < 2 or action_count < 2:
                raise DataProcessError("状态或动作帧数量不足")
            state_start = row_timestamp_ms(state_first, "state")
            state_end = row_timestamp_ms(state_last, "state")
            dimensions = len(vector_from_row(state_first, "observation_state_rad"))
            cameras: dict[str, CameraInfo] = {}
            for name in CAMERAS:
                frames_path = path / name / "frames.jsonl"
                first, last, count = first_last_jsonl(
                    frames_path, counts.get(f"camera:{name}")
                )
                if not first or not last or count < 2:
                    raise DataProcessError(f"{name} 相机帧数量不足")
                width = int(first["width"])
                height = int(first["height"])
                first_ts = row_timestamp_ms(first, "camera")
                last_ts = row_timestamp_ms(last, "camera")
                duration = max((last_ts - first_ts) / 1000, 1e-6)
                fps = (count - 1) / duration
                raw_stat = (path / name / "rgb.raw").stat()
                frames_stat = frames_path.stat()
                raw_size = raw_stat.st_size
                source_mtime_ns = max(raw_stat.st_mtime_ns, frames_stat.st_mtime_ns)
                expected_min = count * width * height * 3
                if raw_size < expected_min:
                    raise DataProcessError(
                        f"{name}/rgb.raw 不完整: {raw_size} < {expected_min} bytes"
                    )
                cameras[name] = CameraInfo(
                    name=name,
                    key=CAMERA_KEYS[name],
                    width=width,
                    height=height,
                    fps=round(fps, 4),
                    frames=count,
                    first_timestamp_ms=first_ts,
                    last_timestamp_ms=last_ts,
                    raw_size=raw_size,
                    source_mtime_ns=source_mtime_ns,
                )
            common_end = min([state_end, *[cam.last_timestamp_ms for cam in cameras.values()]])
            duration_s = max(0.0, (common_end - state_start) / 1000)
            if duration_s <= 0.25:
                raise DataProcessError("可同步回放区间不足 0.25 秒")
            ready = manifest_status == "finished"
            result = EpisodeInspection(
                episode_id=episode_id,
                path=path,
                ready=ready,
                status="ready" if ready else "error",
                reason=str(manifest.get("reason") or ("采集完成" if ready else "采集未完成")),
                warnings=tuple(warnings),
                manifest_id=manifest_id,
                state_frames=state_count,
                action_frames=action_count,
                duration_s=duration_s,
                state_start_ms=state_start,
                state_end_ms=state_end,
                cameras=cameras,
                dimensions=dimensions,
            )
        except (DataProcessError, KeyError, TypeError, ValueError, OSError) as exc:
            result = EpisodeInspection(
                episode_id=episode_id,
                path=path,
                ready=False,
                status="error",
                reason="episode 校验失败",
                warnings=(str(exc),),
                manifest_id=None,
                state_frames=0,
                action_frames=0,
                duration_s=0.0,
                state_start_ms=None,
                state_end_ms=None,
                cameras={},
                dimensions=0,
            )
        with self._cache_lock:
            self._cache[episode_id] = result
        return result

    def list_episodes(self, refresh: bool = False) -> dict[str, Any]:
        reviews = self.review_store.load(self.root)
        episodes = [
            self.inspect(episode_id, refresh=refresh).as_dict(reviews.get(episode_id))
            for episode_id in self.episode_ids()
        ]
        counts = {
            "total": len(episodes),
            "ready": sum(item["status"] == "ready" for item in episodes),
            "unprocessed": sum(item["workflow_status"] == "unprocessed" for item in episodes),
            "processed": sum(item["workflow_status"] == "processed" for item in episodes),
            "exported": sum(item["workflow_status"] == "exported" for item in episodes),
            "failed": sum(item["workflow_status"] == "failed" for item in episodes),
        }
        counts["pending_export"] = sum(item["export_eligible"] for item in episodes)
        counts["exportable"] = counts["pending_export"]
        return {"raw_root": str(self.root), "counts": counts, "episodes": episodes}

    def detail(self, episode_id: str) -> dict[str, Any]:
        inspection = self.inspect(episode_id)
        review = self.review_store.load(self.root).get(episode_id, {})
        result = inspection.as_dict(review)
        if inspection.ready:
            manifest = read_json(inspection.path / "manifest.json")
            result["manifest"] = {
                "status": manifest.get("status"),
                "reason": manifest.get("reason"),
                "pause_count": manifest.get("pause_count", 0),
                "last_error": manifest.get("last_error", ""),
                "channel_names": manifest.get("dataset_layout", {}).get("names", []),
            }
        return result

    def save_review(self, episode_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        inspection = self.inspect(episode_id)
        allowed: dict[str, Any] = {}
        if "excluded" in payload:
            allowed["excluded"] = bool(payload["excluded"])
        if "reason" in payload:
            allowed["reason"] = str(payload["reason"])[:200]
        if "note" in payload:
            allowed["note"] = str(payload["note"])[:1000]
        start = float(payload.get("trim_start_s", 0.0))
        end = float(payload.get("trim_end_s", inspection.duration_s))
        if not math.isfinite(start) or not math.isfinite(end):
            raise DataProcessError("裁剪值必须是有限数字")
        start = max(0.0, min(start, inspection.duration_s))
        end = max(0.0, min(end, inspection.duration_s))
        if inspection.ready and end - start < 0.25:
            raise DataProcessError("保留区间不能短于 0.25 秒")
        allowed["trim_start_s"] = round(start, 4)
        allowed["trim_end_s"] = round(end, 4)
        allowed["processed"] = True
        allowed["processed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        self.review_store.save_episode(self.root, episode_id, allowed)
        return self.detail(episode_id)

    def load_state_rows(self, episode_id: str) -> list[dict[str, Any]]:
        return list(iter_jsonl(self.episode_path(episode_id) / "observation_state_frame.jsonl"))

    def load_action_rows(self, episode_id: str) -> list[dict[str, Any]]:
        return list(iter_jsonl(self.episode_path(episode_id) / "applied_action_frame.jsonl"))

    def auto_trim(self, episode_id: str, padding_s: float = 0.6) -> dict[str, Any]:
        inspection = self.inspect(episode_id)
        if not inspection.ready:
            raise DataProcessError("不完整 episode 无法自动裁剪")
        rows = self.load_state_rows(episode_id)
        times = [(row_timestamp_ms(row, "state") - row_timestamp_ms(rows[0], "state")) / 1000 for row in rows]
        vectors = [vector_from_row(row, "observation_state_rad") for row in rows]
        energy = [0.0]
        for previous, current in zip(vectors, vectors[1:]):
            if len(previous) != len(current):
                raise DataProcessError("状态向量维度不一致")
            energy.append(sum(abs(a - b) for a, b in zip(previous, current)) / len(current))
        nonzero = sorted(value for value in energy if value > 0)
        p90 = nonzero[min(len(nonzero) - 1, int(len(nonzero) * 0.9))] if nonzero else 0.0
        median = statistics.median(nonzero) if nonzero else 0.0
        threshold = max(0.0008, median * 3.0, p90 * 0.08)
        moving = []
        for index in range(len(energy)):
            lo = max(0, index - 2)
            hi = min(len(energy), index + 3)
            moving.append(sum(value > threshold for value in energy[lo:hi]) >= 2)
        indices = [index for index, value in enumerate(moving) if value]
        if not indices:
            start, end = 0.0, inspection.duration_s
        else:
            start = max(0.0, times[indices[0]] - padding_s)
            end = min(inspection.duration_s, times[indices[-1]] + padding_s)
            if end - start < 1.0:
                start, end = 0.0, inspection.duration_s
        payload = {
            "trim_start_s": start,
            "trim_end_s": end,
            "processed": True,
            "processed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        self.review_store.save_episode(self.root, episode_id, payload)
        result = self.detail(episode_id)
        result["auto_trim"] = {"threshold": threshold, "padding_s": padding_s}
        return result

    def series(self, episode_id: str, max_points: int = 800) -> dict[str, Any]:
        inspection = self.inspect(episode_id)
        if not inspection.ready or inspection.state_start_ms is None:
            raise DataProcessError("该 episode 不可回放")
        states = self.load_state_rows(episode_id)
        actions = self.load_action_rows(episode_id)
        action_times = [row_timestamp_ms(row, "action") for row in actions]
        stride = max(1, math.ceil(len(states) / max(20, min(max_points, 2000))))
        sampled: list[dict[str, Any]] = []
        for row in states[::stride]:
            timestamp = row_timestamp_ms(row, "state")
            action_pos = bisect.bisect_left(action_times, timestamp)
            candidates = [min(action_pos, len(actions) - 1), max(0, action_pos - 1)]
            nearest = min(candidates, key=lambda idx: abs(action_times[idx] - timestamp))
            sampled.append(
                {
                    "t": round((timestamp - inspection.state_start_ms) / 1000, 4),
                    "state": vector_from_row(row, "observation_state_rad"),
                    "action": vector_from_row(actions[nearest], "action_rad"),
                }
            )
        manifest = read_json(inspection.path / "manifest.json")
        names = manifest.get("dataset_layout", {}).get("names") or [
            f"joint_{index}" for index in range(inspection.dimensions)
        ]
        return {"names": names, "points": sampled, "duration_s": inspection.duration_s}

    def mark_exported(
        self,
        episode_mapping: list[tuple[str, int]],
        output_root: Path,
    ) -> None:
        """Persist the latest output mapping for each successfully synchronized episode."""
        exported_at = dt.datetime.now(dt.timezone.utc).isoformat()
        reviews = self.review_store.load(self.root)
        patches: dict[str, dict[str, Any]] = {}
        for source_id, output_episode_index in episode_mapping:
            record = reviews.get(source_id, {})
            exports = record.get("exports", [])
            if not isinstance(exports, list):
                exports = []
            output_text = str(output_root.resolve())
            exports = [
                item
                for item in exports
                if not isinstance(item, dict) or item.get("output_root") != output_text
            ]
            exports.append(
                {
                    "output_root": output_text,
                    "output_episode_index": output_episode_index,
                    "exported_at": exported_at,
                }
            )
            patches[source_id] = {
                "processed": True,
                "exports": exports,
                "last_exported_at": exported_at,
                "last_export_root": output_text,
            }
        self.review_store.update_episodes(self.root, patches)

    def preview_path(self, episode_id: str, camera: str) -> Path:
        if camera not in CAMERAS:
            raise DataProcessError("未知相机")
        inspection = self.inspect(episode_id)
        if not inspection.ready or camera not in inspection.cameras:
            raise DataProcessError("相机数据不可用")
        source = inspection.path / camera / "rgb.raw"
        signature = f"preview-v2:{source}:{source.stat().st_size}:{source.stat().st_mtime_ns}"
        digest = hashlib.sha256(signature.encode()).hexdigest()[:16]
        return self.runtime_root / "previews" / digest / episode_id / f"{camera}.mp4"

    def poster_path(self, episode_id: str, camera: str) -> Path:
        if camera not in CAMERAS:
            raise DataProcessError("未知相机")
        inspection = self.inspect(episode_id)
        if not inspection.ready or camera not in inspection.cameras:
            raise DataProcessError("相机数据不可用")
        source = inspection.path / camera / "rgb.raw"
        source_stat = source.stat()
        signature = f"poster-v1:{source}:{source_stat.st_size}:{source_stat.st_mtime_ns}"
        digest = hashlib.sha256(signature.encode()).hexdigest()[:16]
        return self.runtime_root / "posters" / digest / episode_id / f"{camera}.jpg"

    def ensure_poster(self, episode_id: str, camera: str) -> Path:
        output = self.poster_path(episode_id, camera)
        with self._media_lock(output):
            return self._ensure_poster_locked(episode_id, camera, output)

    def _ensure_poster_locked(self, episode_id: str, camera: str, output: Path) -> Path:
        if output.is_file() and output.stat().st_size > 512:
            return output
        inspection = self.inspect(episode_id)
        info = inspection.cameras[camera]
        output.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_suffix(".tmp.jpg")
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pixel_format", "bgr24",
            "-video_size", f"{info.width}x{info.height}",
            "-i", str(inspection.path / camera / "rgb.raw"),
            "-frames:v", "1", "-q:v", "4", str(temp),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            os.replace(temp, output)
        except (OSError, subprocess.CalledProcessError) as exc:
            if temp.exists():
                temp.unlink()
            detail = exc.stderr[-1000:] if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            raise DataProcessError(f"生成首帧失败: {detail}") from exc
        return output

    def ensure_preview(self, episode_id: str, camera: str) -> Path:
        output = self.preview_path(episode_id, camera)
        with self._media_lock(output):
            if output.is_file() and output.stat().st_size > 1024:
                return output
            # One browser needs at most three camera previews at once. A global
            # ceiling keeps additional users from multiplying NFS traffic while
            # preserving the existing single-user parallelism.
            with _PREVIEW_GENERATION_SLOTS:
                return self._ensure_preview_locked(episode_id, camera, output)

    @staticmethod
    def _encode_sampled_preview(
        command: list[str],
        source: Path,
        frame_size: int,
        frame_indices: list[int],
    ) -> None:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        stderr = b""
        try:
            if process.stdin is None or process.stderr is None:
                raise OSError("无法创建 ffmpeg 管道")
            with source.open("rb", buffering=0) as stream:
                if hasattr(os, "posix_fadvise") and hasattr(os, "POSIX_FADV_RANDOM"):
                    try:
                        os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_RANDOM)
                    except OSError:
                        pass
                for frame_index in frame_indices:
                    stream.seek(frame_index * frame_size)
                    frame = stream.read(frame_size)
                    if len(frame) != frame_size:
                        raise DataProcessError(
                            f"原始视频帧不完整: frame={frame_index}, "
                            f"bytes={len(frame)}/{frame_size}"
                        )
                    remaining = memoryview(frame)
                    while remaining:
                        written = process.stdin.write(remaining)
                        if not written:
                            raise BrokenPipeError("ffmpeg 输入管道已关闭")
                        remaining = remaining[written:]
            process.stdin.close()
            stderr = process.stderr.read()
            return_code = process.wait()
            if return_code:
                raise subprocess.CalledProcessError(
                    return_code,
                    command,
                    stderr=stderr.decode("utf-8", errors="replace"),
                )
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.stderr is not None and not process.stderr.closed:
                process.stderr.close()

    def _ensure_preview_locked(self, episode_id: str, camera: str, output: Path) -> Path:
        if output.is_file() and output.stat().st_size > 1024:
            return output
        inspection = self.inspect(episode_id)
        info = inspection.cameras[camera]
        frame_indices, preview_fps = preview_sampling_plan(info.frames, info.fps)
        frame_size = info.width * info.height * 3
        source = inspection.path / camera / "rgb.raw"
        output.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_suffix(".tmp.mp4")
        common = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pixel_format",
            "bgr24",
            "-video_size",
            f"{info.width}x{info.height}",
            "-framerate",
            f"{preview_fps:.6f}",
            "-i",
            "pipe:0",
            "-vf",
            "scale=480:360",
            "-an",
        ]
        nvenc = [
            *common,
            "-c:v", "h264_nvenc",
            "-preset", "p1",
            "-tune", "ll",
            "-cq", "29",
            "-b:v", "0",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(temp),
        ]
        cpu = [
            *common,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temp),
        ]
        try:
            try:
                self._encode_sampled_preview(
                    nvenc, source, frame_size, frame_indices
                )
            except (subprocess.CalledProcessError, OSError):
                self._encode_sampled_preview(
                    cpu, source, frame_size, frame_indices
                )
            os.replace(temp, output)
        except (DataProcessError, OSError, subprocess.CalledProcessError) as exc:
            if temp.exists():
                temp.unlink()
            detail = exc.stderr[-2000:] if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            raise DataProcessError(f"生成预览失败: {detail}") from exc
        return output
