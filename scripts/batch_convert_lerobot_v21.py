#!/usr/bin/env python3
"""Convert Figure raw episodes to GR00T-compatible LeRobot v2.1.

The converter is deliberately resumable.  It writes to a sibling ``.inprogress``
directory, validates every parquet/video, and only renames that directory to the
requested output after the complete dataset passes validation.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dataprocess.raw_dataset import (  # noqa: E402
    CAMERAS,
    CAMERA_KEYS,
    RawDataset,
    read_json,
    row_timestamp_ms,
    vector_from_row,
)


LOGGER = logging.getLogger("lerobot_v21")
EPISODE_RE = re.compile(r"^episode_\d{6}$")
DIMENSION = 38
ANNOTATION_COLUMN = "annotation.human.task_description"
DEFAULT_TASK = "pick up the packaged item with both hands"
STATE_GROUPS = (
    ("left_arm", 0, 7),
    ("right_arm", 7, 14),
    ("left_hand", 14, 26),
    ("right_hand", 26, 38),
)
_VIDEO_WORKER_LOCAL = threading.local()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, default=PROJECT_ROOT / "runtime")
    parser.add_argument("--fps", type=float, default=20.0)
    parser.add_argument("--task", default=DEFAULT_TASK)
    parser.add_argument("--robot-type", default="tianji_dual_arm_xhand")
    parser.add_argument("--gpus", default="0,1,2,3,4,5,6,7")
    parser.add_argument(
        "--video-encoder",
        choices=("h264_nvenc", "libx264"),
        default="h264_nvenc",
    )
    parser.add_argument(
        "--video-workers",
        type=int,
        default=0,
        help="Video worker count for libx264 (default: up to 8 CPU workers).",
    )
    parser.add_argument("--video-quality", type=int, default=20)
    parser.add_argument("--parquet-workers", type=int, default=8)
    parser.add_argument(
        "--skip-invalid",
        action="store_true",
        help="Skip structurally invalid source episodes and record them in metadata.",
    )
    parser.add_argument("--log-file", type=Path)
    return parser.parse_args()


def configure_logging(log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
    os.replace(temporary, path)


def run_checked(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


def available_gpus(requested: str) -> list[int]:
    values = [int(item.strip()) for item in requested.split(",") if item.strip()]
    if not values:
        raise RuntimeError("no GPUs requested")
    result = run_checked(
        [
            "nvidia-smi",
            "--query-gpu=index",
            "--format=csv,noheader,nounits",
        ]
    )
    present = {int(line.strip()) for line in result.stdout.splitlines() if line.strip()}
    missing = sorted(set(values) - present)
    if missing:
        raise RuntimeError(f"requested GPUs are unavailable: {missing}")
    return values


def inspect_source(
    dataset: RawDataset, fps: float, skip_invalid: bool = False
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]]]:
    source_ids = dataset.episode_ids()
    if not source_ids:
        raise RuntimeError(f"no episodes found under {dataset.root}")
    specifications: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    state_names: list[str] | None = None
    global_index = 0
    for source_id in source_ids:
        try:
            inspection = dataset.inspect(source_id, refresh=True)
            if not inspection.ready:
                raise RuntimeError(
                    f"not exportable: {inspection.status}: {inspection.warnings}"
                )
            if inspection.dimensions != DIMENSION:
                raise RuntimeError(
                    f"has {inspection.dimensions} dimensions, expected {DIMENSION}"
                )
            if inspection.state_start_ms is None or inspection.state_end_ms is None:
                raise RuntimeError("has no valid state timeline")
            manifest = read_json(inspection.path / "manifest.json")
            names = list(manifest.get("dataset_layout", {}).get("names") or [])
            if len(names) != DIMENSION:
                raise RuntimeError(f"has {len(names)} joint names, expected {DIMENSION}")
            if state_names is not None and names != state_names:
                raise RuntimeError("joint layout differs from the first valid episode")
        except Exception as error:
            if not skip_invalid:
                raise RuntimeError(f"{source_id} is not exportable: {error}") from error
            record = {
                "source_episode_id": source_id,
                "error_type": type(error).__name__,
                "reason": str(error),
            }
            skipped.append(record)
            LOGGER.warning("skipping invalid source episode %s: %s", source_id, error)
            continue

        if state_names is None:
            state_names = names
        output_index = len(specifications)
        duration_s = max(
            0.0, (float(inspection.state_end_ms) - float(inspection.state_start_ms)) / 1000
        )
        frame_count = max(1, int(math.floor(duration_s * fps)) + 1)
        specifications.append(
            {
                "source_id": source_id,
                "episode_index": output_index,
                "inspection": inspection,
                "duration_s": duration_s,
                "frame_count": frame_count,
                "global_start": global_index,
            }
        )
        global_index += frame_count
    if not specifications or state_names is None:
        raise RuntimeError(f"no valid episodes found under {dataset.root}")
    return specifications, state_names, skipped


def parquet_path(root: Path, episode_index: int) -> Path:
    return root / "data" / f"chunk-{episode_index // 1000:03d}" / f"episode_{episode_index:06d}.parquet"


def video_path(root: Path, episode_index: int, camera: str) -> Path:
    return (
        root
        / "videos"
        / f"chunk-{episode_index // 1000:03d}"
        / CAMERA_KEYS[camera]
        / f"episode_{episode_index:06d}.mp4"
    )


def parquet_is_valid(path: Path, expected_rows: int) -> bool:
    try:
        metadata = pq.read_metadata(path)
        if metadata.num_rows != expected_rows:
            return False
        # Parquet's low-level metadata schema exposes fixed-size-list leaves as
        # repeated ``element`` names.  The Arrow schema preserves the logical
        # top-level feature names required by LeRobot.
        names = set(pq.read_schema(path).names)
        return {
            "observation.state",
            "action",
            "timestamp",
            "frame_index",
            "episode_index",
            "index",
            "task_index",
            ANNOTATION_COLUMN,
            "next.reward",
            "next.done",
        }.issubset(names)
    except Exception:
        return False


def build_parquet(dataset: RawDataset, spec: dict[str, Any], root: Path, fps: float) -> dict[str, Any]:
    episode_index = int(spec["episode_index"])
    frame_count = int(spec["frame_count"])
    destination = parquet_path(root, episode_index)
    if destination.is_file() and parquet_is_valid(destination, frame_count):
        return {"episode_index": episode_index, "status": "resumed"}

    source_id = str(spec["source_id"])
    inspection = spec["inspection"]
    states = dataset.load_state_rows(source_id)
    actions = dataset.load_action_rows(source_id)
    state_times = np.asarray([row_timestamp_ms(row, "state") for row in states], dtype=np.float64)
    action_times = np.asarray([row_timestamp_ms(row, "action") for row in actions], dtype=np.float64)
    state_values = np.asarray(
        [vector_from_row(row, "observation_state_rad") for row in states], dtype=np.float32
    )
    action_values = np.asarray(
        [vector_from_row(row, "action_rad") for row in actions], dtype=np.float32
    )
    if state_values.shape[1:] != (DIMENSION,) or action_values.shape[1:] != (DIMENSION,):
        raise RuntimeError(f"{source_id}: invalid state/action vector shape")
    if not np.isfinite(state_values).all() or not np.isfinite(action_values).all():
        raise RuntimeError(f"{source_id}: state/action contains non-finite values")

    targets = float(inspection.state_start_ms) + np.arange(frame_count, dtype=np.float64) * (1000 / fps)
    state_right = np.searchsorted(state_times, targets, side="right")
    state_left = np.clip(state_right - 1, 0, len(state_times) - 1)
    state_right = np.clip(state_right, 0, len(state_times) - 1)
    left_times = state_times[state_left]
    right_times = state_times[state_right]
    denominator = right_times - left_times
    alpha = np.divide(
        targets - left_times,
        denominator,
        out=np.zeros_like(targets),
        where=denominator > 0,
    ).clip(0.0, 1.0)
    sampled_states = (
        state_values[state_left] * (1.0 - alpha[:, None])
        + state_values[state_right] * alpha[:, None]
    ).astype(np.float32)

    # Causal zero-order hold: never use an action timestamped after the observation.
    action_indices = np.searchsorted(action_times, targets, side="right") - 1
    action_indices = np.clip(action_indices, 0, len(action_times) - 1)
    sampled_actions = action_values[action_indices].astype(np.float32, copy=False)

    timestamps = np.arange(frame_count, dtype=np.float32) / np.float32(fps)
    schema = pa.schema(
        [
            pa.field("observation.state", pa.list_(pa.float32(), DIMENSION)),
            pa.field("action", pa.list_(pa.float32(), DIMENSION)),
            pa.field("timestamp", pa.float32()),
            pa.field("frame_index", pa.int64()),
            pa.field("episode_index", pa.int64()),
            pa.field("index", pa.int64()),
            pa.field("task_index", pa.int64()),
            pa.field(ANNOTATION_COLUMN, pa.int64()),
            pa.field("next.reward", pa.float32()),
            pa.field("next.done", pa.bool_()),
        ]
    )
    rows = [
        {
            "observation.state": sampled_states[index],
            "action": sampled_actions[index],
            "timestamp": timestamps[index],
            "frame_index": index,
            "episode_index": episode_index,
            "index": int(spec["global_start"]) + index,
            "task_index": 0,
            ANNOTATION_COLUMN: 0,
            "next.reward": np.float32(0.0),
            "next.done": index == frame_count - 1,
        }
        for index in range(frame_count)
    ]
    table = pa.Table.from_pylist(rows, schema=schema)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".parquet.tmp")
    pq.write_table(table, temporary, compression="zstd")
    os.replace(temporary, destination)
    if not parquet_is_valid(destination, frame_count):
        raise RuntimeError(f"{source_id}: parquet validation failed: {destination}")
    return {"episode_index": episode_index, "status": "written"}


def ffprobe(path: Path, count_frames: bool = False) -> dict[str, Any]:
    command = ["ffprobe", "-v", "error", "-select_streams", "v:0"]
    if count_frames:
        command.append("-count_frames")
    command.extend(
        [
            "-show_entries",
            "stream=codec_name,pix_fmt,width,height,avg_frame_rate,nb_frames,nb_read_frames",
            "-of",
            "json",
            str(path),
        ]
    )
    result = run_checked(command)
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError(f"ffprobe found no video stream: {path}")
    return streams[0]


def video_is_valid(path: Path, expected_frames: int, count_frames: bool = False) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 1024:
            return False
        stream = ffprobe(path, count_frames=count_frames)
        if stream.get("codec_name") != "h264" or stream.get("pix_fmt") != "yuv420p":
            return False
        observed = stream.get("nb_read_frames") if count_frames else stream.get("nb_frames")
        if observed not in (None, "N/A") and abs(int(observed) - expected_frames) > 1:
            return False
        return True
    except Exception:
        return False


def initialize_video_worker(worker_queue: queue.Queue[int]) -> None:
    _VIDEO_WORKER_LOCAL.index = worker_queue.get_nowait()


def build_video(
    spec: dict[str, Any],
    camera: str,
    root: Path,
    fps: float,
    quality: int,
    encoder: str,
) -> dict[str, Any]:
    episode_index = int(spec["episode_index"])
    frame_count = int(spec["frame_count"])
    source_id = str(spec["source_id"])
    destination = video_path(root, episode_index, camera)
    if destination.is_file() and video_is_valid(destination, frame_count):
        return {"episode_index": episode_index, "camera": camera, "status": "resumed"}
    inspection = spec["inspection"]
    camera_info = inspection.cameras[camera]
    state_start_ms = float(inspection.state_start_ms)
    seek_s = max(0.0, (state_start_ms - camera_info.first_timestamp_ms) / 1000)
    pad_start_s = max(0.0, (camera_info.first_timestamp_ms - state_start_ms) / 1000)
    filters: list[str] = []
    if pad_start_s > 0.0005:
        filters.append(f"tpad=start_mode=clone:start_duration={pad_start_s:.6f}")
    # Some camera streams end more than five seconds before the state timeline.
    # Extend the last decoded frame indefinitely and let -frames:v enforce the
    # exact episode length. A fixed padding window can silently yield a short MP4.
    filters.extend([f"fps={fps:.8f}", "tpad=stop_mode=clone:stop=-1"])
    worker = int(_VIDEO_WORKER_LOCAL.index)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".mp4.tmp")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{seek_s:.6f}",
        "-f",
        "rawvideo",
        "-pixel_format",
        "bgr24",
        "-video_size",
        f"{camera_info.width}x{camera_info.height}",
        "-framerate",
        f"{camera_info.fps:.8f}",
        "-i",
        str(inspection.path / camera / "rgb.raw"),
        "-vf",
        ",".join(filters),
        "-frames:v",
        str(frame_count),
        "-an",
        "-c:v",
        encoder,
    ]
    if encoder == "h264_nvenc":
        command.extend(
            [
                "-gpu", str(worker), "-preset", "p4", "-tune", "hq",
                "-rc", "vbr", "-cq", str(quality), "-b:v", "0",
            ]
        )
    else:
        command.extend(["-preset", "veryfast", "-crf", str(quality)])
    command.extend(
        [
            "-pix_fmt", "yuv420p", "-g", str(max(1, round(fps * 2))),
            "-movflags", "+faststart", "-f", "mp4", str(temporary),
        ]
    )
    started = time.monotonic()
    try:
        run_checked(command)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "")[-4000:]
        raise RuntimeError(
            f"{source_id}/{camera} failed with {encoder} worker {worker}: {detail}"
        ) from error
    os.replace(temporary, destination)
    if not video_is_valid(destination, frame_count):
        raise RuntimeError(f"{source_id}/{camera}: video validation failed")
    return {
        "episode_index": episode_index,
        "camera": camera,
        "worker": worker,
        "encoder": encoder,
        "seconds": round(time.monotonic() - started, 3),
        "status": "written",
    }


def make_features(state_names: list[str], fps: float) -> dict[str, Any]:
    features: dict[str, Any] = {
        "observation.state": {"dtype": "float32", "shape": [DIMENSION], "names": state_names},
        "action": {"dtype": "float32", "shape": [DIMENSION], "names": state_names},
    }
    for camera in CAMERAS:
        features[CAMERA_KEYS[camera]] = {
            "dtype": "video",
            "shape": [480, 640, 3],
            "names": ["height", "width", "channels"],
            "info": {
                "video.height": 480,
                "video.width": 640,
                "video.codec": "h264",
                "video.pix_fmt": "yuv420p",
                "video.is_depth_map": False,
                "video.fps": fps,
                "video.channels": 3,
                "has_audio": False,
            },
        }
    for key, dtype in (
        ("timestamp", "float32"),
        ("frame_index", "int64"),
        ("episode_index", "int64"),
        ("index", "int64"),
        ("task_index", "int64"),
        (ANNOTATION_COLUMN, "int64"),
        ("next.reward", "float32"),
        ("next.done", "bool"),
    ):
        features[key] = {"dtype": dtype, "shape": [1], "names": None}
    return features


def generate_metadata(
    root: Path,
    specs: list[dict[str, Any]],
    state_names: list[str],
    source: Path,
    fps: float,
    task: str,
    robot_type: str,
    gpus: list[int],
    video_encoder: str,
    video_workers: int,
    skipped: list[dict[str, str]],
) -> None:
    total_frames = sum(int(spec["frame_count"]) for spec in specs)
    total_episodes = len(specs)
    info = {
        "codebase_version": "v2.1",
        "robot_type": robot_type,
        "total_episodes": total_episodes,
        "total_frames": total_frames,
        "total_tasks": 1,
        "total_videos": total_episodes * len(CAMERAS),
        "source_episodes_total": total_episodes + len(skipped),
        "source_episodes_skipped": len(skipped),
        "total_chunks": max(1, math.ceil(total_episodes / 1000)),
        "chunks_size": 1000,
        "fps": fps,
        "splits": {"train": f"0:{total_episodes}"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": make_features(state_names, fps),
    }
    modality = {
        "state": {name: {"start": start, "end": end} for name, start, end in STATE_GROUPS},
        "action": {name: {"start": start, "end": end} for name, start, end in STATE_GROUPS},
        "video": {
            "ego_view": {"original_key": CAMERA_KEYS["head"]},
            "left_wrist": {"original_key": CAMERA_KEYS["left_wrist"]},
            "right_wrist": {"original_key": CAMERA_KEYS["right_wrist"]},
        },
        "annotation": {"human.task_description": {"original_key": "task_index"}},
    }
    episodes = [
        {
            "episode_index": int(spec["episode_index"]),
            "tasks": [task],
            "length": int(spec["frame_count"]),
            "raw_episode_id": str(spec["source_id"]),
        }
        for spec in specs
    ]
    write_json(root / "meta" / "info.json", info)
    write_json(root / "meta" / "modality.json", modality)
    write_json(root / "meta" / "skipped_episodes.json", skipped)
    write_jsonl(root / "meta" / "episodes.jsonl", episodes)
    write_jsonl(root / "meta" / "tasks.jsonl", [{"task_index": 0, "task": task}])
    write_json(
        root / "meta" / "conversion.json",
        {
            "schema": "figure_raw_to_gr00t_lerobot_v21",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source": str(source),
            "source_preserved": True,
            "episode_count": total_episodes,
            "source_episode_count": total_episodes + len(skipped),
            "skipped_episode_count": len(skipped),
            "frame_count": total_frames,
            "video_count": total_episodes * len(CAMERAS),
            "fps": fps,
            "task": task,
            "state_sampling": "linear interpolation",
            "action_sampling": "causal zero-order hold",
            "image_sampling": "constant-fps resample aligned to state start",
            "video_encoder": video_encoder,
            "video_workers": video_workers,
            "gpus": gpus,
            "source_episode_ids": [str(spec["source_id"]) for spec in specs],
            "skipped_episodes": skipped,
        },
    )


def calculate_stats(root: Path, specs: list[dict[str, Any]]) -> dict[str, Any]:
    states: list[np.ndarray] = []
    actions: list[np.ndarray] = []
    timestamps: list[np.ndarray] = []
    for spec in specs:
        table = pq.read_table(
            parquet_path(root, int(spec["episode_index"])),
            columns=["observation.state", "action", "timestamp"],
        )
        states.append(np.asarray(table["observation.state"].to_pylist(), dtype=np.float32))
        actions.append(np.asarray(table["action"].to_pylist(), dtype=np.float32))
        timestamps.append(np.asarray(table["timestamp"].to_pylist(), dtype=np.float32)[:, None])

    def describe(chunks: list[np.ndarray]) -> dict[str, list[float]]:
        values = np.concatenate(chunks, axis=0)
        return {
            "mean": values.mean(axis=0, dtype=np.float64).astype(np.float32).tolist(),
            "std": values.std(axis=0, dtype=np.float64).astype(np.float32).tolist(),
            "min": values.min(axis=0).tolist(),
            "max": values.max(axis=0).tolist(),
            "q01": np.quantile(values, 0.01, axis=0).tolist(),
            "q99": np.quantile(values, 0.99, axis=0).tolist(),
        }

    result = {
        "action": describe(actions),
        "observation.state": describe(states),
        "timestamp": describe(timestamps),
    }
    write_json(root / "meta" / "stats.json", result)
    return result


def write_training_files(root: Path) -> None:
    config = '''"""GR00T N1.7 modality config for Tianji dual arms with xHand hands."""

from gr00t.configs.data.embodiment_configs import register_modality_config
from gr00t.data.embodiment_tags import EmbodimentTag
from gr00t.data.types import ActionConfig, ActionFormat, ActionRepresentation, ActionType, ModalityConfig

figure_config = {
    "video": ModalityConfig(delta_indices=[0], modality_keys=["ego_view", "left_wrist", "right_wrist"]),
    "state": ModalityConfig(delta_indices=[0], modality_keys=["left_arm", "right_arm", "left_hand", "right_hand"]),
    "action": ModalityConfig(
        delta_indices=list(range(16)),
        modality_keys=["left_arm", "right_arm", "left_hand", "right_hand"],
        action_configs=[
            ActionConfig(rep=ActionRepresentation.RELATIVE, type=ActionType.NON_EEF, format=ActionFormat.DEFAULT),
            ActionConfig(rep=ActionRepresentation.RELATIVE, type=ActionType.NON_EEF, format=ActionFormat.DEFAULT),
            ActionConfig(rep=ActionRepresentation.ABSOLUTE, type=ActionType.NON_EEF, format=ActionFormat.DEFAULT),
            ActionConfig(rep=ActionRepresentation.ABSOLUTE, type=ActionType.NON_EEF, format=ActionFormat.DEFAULT),
        ],
    ),
    "language": ModalityConfig(delta_indices=[0], modality_keys=["annotation.human.task_description"]),
}

register_modality_config(figure_config, embodiment_tag=EmbodimentTag.NEW_EMBODIMENT)
'''
    training_dir = root / "training"
    training_dir.mkdir(parents=True, exist_ok=True)
    (training_dir / "figure_38d_config.py").write_text(config, encoding="utf-8")
    readme = """# GR00T training

This is a GR00T-flavored LeRobot v2.1 dataset for the Tianji dual-arm + xHand robot.

Use `NEW_EMBODIMENT`, not `UNITREE_G1_SONIC`:

```bash
uv run python gr00t/experiment/launch_finetune.py \\
  --base-model-path nvidia/GR00T-N1.7-3B \\
  --dataset-path DATASET_ROOT \\
  --embodiment-tag NEW_EMBODIMENT \\
  --modality-config-path DATASET_ROOT/training/figure_38d_config.py \\
  --num-gpus 8 \\
  --output-dir OUTPUT_DIR \\
  --global-batch-size 32 \\
  --dataloader-num-workers 8
```
"""
    (training_dir / "README.md").write_text(readme, encoding="utf-8")


def validate_dataset(
    root: Path,
    specs: list[dict[str, Any]],
    fps: float,
    skipped: list[dict[str, str]],
) -> dict[str, Any]:
    info = read_json(root / "meta" / "info.json")
    if info.get("codebase_version") != "v2.1":
        raise RuntimeError("meta/info.json does not identify LeRobot v2.1")
    expected_frames = sum(int(spec["frame_count"]) for spec in specs)
    if int(info.get("total_frames", -1)) != expected_frames:
        raise RuntimeError("metadata total frame count mismatch")
    parquet_rows = 0
    video_bytes = 0
    for completed, spec in enumerate(specs, 1):
        episode_index = int(spec["episode_index"])
        frame_count = int(spec["frame_count"])
        path = parquet_path(root, episode_index)
        if not parquet_is_valid(path, frame_count):
            raise RuntimeError(f"invalid parquet during final validation: {path}")
        parquet_rows += pq.read_metadata(path).num_rows
        for camera in CAMERAS:
            path = video_path(root, episode_index, camera)
            if not video_is_valid(path, frame_count, count_frames=True):
                raise RuntimeError(f"invalid video during final validation: {path}")
            video_bytes += path.stat().st_size
        if completed % 10 == 0 or completed == len(specs):
            LOGGER.info("final validation %d/%d episodes", completed, len(specs))
    if parquet_rows != expected_frames:
        raise RuntimeError(f"parquet rows {parquet_rows} != expected {expected_frames}")
    return {
        "status": "pass",
        "validated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "episodes": len(specs),
        "source_episodes": len(specs) + len(skipped),
        "skipped_episodes": len(skipped),
        "frames": expected_frames,
        "videos": len(specs) * len(CAMERAS),
        "video_bytes": video_bytes,
        "fps": fps,
        "checks": [
            "all included source episodes structurally valid",
            "invalid source episodes documented in meta/skipped_episodes.json",
            "parquet schemas and row counts",
            "H.264 yuv420p video streams and decoded frame counts",
            "metadata totals and LeRobot v2.1 marker",
            "finite 38-dimensional state and action vectors",
        ],
    }


def main() -> None:
    args = parse_args()
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    log_file = args.log_file or output.parent / f"{output.name}_conversion.log"
    configure_logging(log_file)
    if not source.is_dir():
        raise RuntimeError(f"source does not exist: {source}")
    if output == source or source in output.parents:
        raise RuntimeError("output must be outside the source dataset")
    if output.exists():
        raise RuntimeError(f"final output already exists: {output}")
    if not math.isfinite(args.fps) or args.fps < 1 or args.fps > 120:
        raise RuntimeError("fps must be between 1 and 120")
    task = str(args.task).strip()
    if not task:
        raise RuntimeError("task must not be empty")
    if args.video_encoder == "h264_nvenc":
        gpus = available_gpus(args.gpus)
        video_worker_ids = gpus
    else:
        gpus = []
        requested_workers = int(args.video_workers)
        if requested_workers < 0:
            raise RuntimeError("video workers must not be negative")
        video_worker_count = requested_workers or min(8, os.cpu_count() or 1)
        video_worker_ids = list(range(max(1, video_worker_count)))
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.inprogress"
    staging.mkdir(parents=False, exist_ok=True)
    LOGGER.info("source=%s", source)
    LOGGER.info("staging=%s", staging)
    LOGGER.info("final=%s", output)
    LOGGER.info(
        "video_encoder=%s video_workers=%d gpus=%s fps=%s task=%r",
        args.video_encoder,
        len(video_worker_ids),
        gpus,
        args.fps,
        task,
    )

    dataset = RawDataset(source, args.runtime)
    specs, state_names, skipped = inspect_source(dataset, args.fps, args.skip_invalid)
    LOGGER.info(
        "validated source: %d usable/%d total episodes, %d skipped, %.1f seconds, %d output frames",
        len(specs),
        len(specs) + len(skipped),
        len(skipped),
        sum(float(spec["duration_s"]) for spec in specs),
        sum(int(spec["frame_count"]) for spec in specs),
    )

    parquet_workers = max(1, min(int(args.parquet_workers), len(specs)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=parquet_workers) as executor:
        futures = [executor.submit(build_parquet, dataset, spec, staging, args.fps) for spec in specs]
        for completed, future in enumerate(concurrent.futures.as_completed(futures), 1):
            result = future.result()
            if completed % 10 == 0 or completed == len(futures):
                LOGGER.info(
                    "parquet %d/%d (latest episode=%06d, %s)",
                    completed,
                    len(futures),
                    result["episode_index"],
                    result["status"],
                )

    worker_queue: queue.Queue[int] = queue.Queue()
    for worker in video_worker_ids:
        worker_queue.put(worker)
    video_jobs = [(spec, camera) for spec in specs for camera in CAMERAS]
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(video_worker_ids),
        initializer=initialize_video_worker,
        initargs=(worker_queue,),
    ) as executor:
        futures = [
            executor.submit(
                build_video,
                spec,
                camera,
                staging,
                args.fps,
                args.video_quality,
                args.video_encoder,
            )
            for spec, camera in video_jobs
        ]
        for completed, future in enumerate(concurrent.futures.as_completed(futures), 1):
            result = future.result()
            if completed % 10 == 0 or completed == len(futures):
                LOGGER.info(
                    "video %d/%d (episode=%06d camera=%s worker=%s %s)",
                    completed,
                    len(futures),
                    result["episode_index"],
                    result["camera"],
                    result.get("worker", "resume"),
                    result["status"],
                )

    generate_metadata(
        staging,
        specs,
        state_names,
        source,
        args.fps,
        task,
        str(args.robot_type),
        gpus,
        args.video_encoder,
        len(video_worker_ids),
        skipped,
    )
    LOGGER.info("calculating dataset statistics")
    calculate_stats(staging, specs)
    write_training_files(staging)
    LOGGER.info("running full decoded-frame validation")
    validation = validate_dataset(staging, specs, args.fps, skipped)
    write_json(staging / "meta" / "validation.json", validation)
    (staging / "_SUCCESS").write_text(validation["validated_at"] + "\n", encoding="utf-8")
    staging.rename(output)
    LOGGER.info("conversion complete: %s", output)
    print(json.dumps({**validation, "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
