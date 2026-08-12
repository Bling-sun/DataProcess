from __future__ import annotations

import bisect
import datetime as dt
import json
import hashlib
import math
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

from .raw_dataset import (
    CAMERAS,
    CAMERA_KEYS,
    DataProcessError,
    RawDataset,
    read_json,
    row_timestamp_ms,
    vector_from_row,
)


Progress = Callable[[float, str], None]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")


def _nearest_index(times: list[float], value: float) -> int:
    position = bisect.bisect_left(times, value)
    candidates = (min(position, len(times) - 1), max(0, position - 1))
    return min(candidates, key=lambda index: abs(times[index] - value))


def _import_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise DataProcessError(
            "转换需要 PyArrow。请先执行 ./setup.sh，然后用 ./run.sh 启动网页。"
        ) from exc
    return pa, pq


def convert_dataset(
    progress: Progress,
    raw_root: str,
    runtime_root: str,
    options: dict[str, Any],
) -> dict[str, Any]:
    """Subprocess entry point for conversion and native dependency loading."""
    dataset = RawDataset(Path(raw_root), Path(runtime_root))
    return Converter(dataset).convert(options, progress)


def _is_trash_path(path: Path) -> bool:
    home_trash = (Path.home() / ".local" / "share" / "Trash").resolve()
    if path == home_trash or home_trash in path.parents:
        return True
    return any(
        parent.name == "files" and parent.parent.name.startswith(".Trash")
        for parent in (path, *path.parents)
        if parent.parent != parent
    )


class Converter:
    def __init__(self, dataset: RawDataset):
        self.dataset = dataset

    def convert(self, options: dict[str, Any], progress: Progress) -> dict[str, Any]:
        pa, pq = _import_pyarrow()
        output = Path(str(options.get("output_root", ""))).expanduser().resolve()
        if not output.is_absolute() or str(output) == "/":
            raise DataProcessError("输出目录必须是安全的绝对路径")
        if _is_trash_path(output):
            raise DataProcessError("输出目录不能位于系统回收站，请选择正常的数据目录")
        if output == self.dataset.root:
            raise DataProcessError("输出目录不能覆盖 raw 数据目录")
        output.parent.mkdir(parents=True, exist_ok=True)

        task = str(options.get("task", "")).strip()
        if not task:
            raise DataProcessError("请输入任务描述")
        fps = float(options.get("fps", 20))
        if not math.isfinite(fps) or fps < 1 or fps > 120:
            raise DataProcessError("FPS 必须在 1 到 120 之间")
        layout = str(options.get("layout", "chunked"))
        if layout not in {"chunked", "flat"}:
            raise DataProcessError("未知输出布局")
        cameras = options.get("cameras", list(CAMERAS))
        if not isinstance(cameras, list) or not cameras:
            raise DataProcessError("至少选择一个相机")
        cameras = [str(camera) for camera in cameras]
        if any(camera not in CAMERAS for camera in cameras):
            raise DataProcessError("相机配置非法")

        direct_raw = options.get("direct_raw", False)
        if not isinstance(direct_raw, bool):
            raise DataProcessError("原始数据直转选项非法")
        conversion_mode = "raw" if direct_raw else "reviewed"

        reviews = self.dataset.review_store.load(self.dataset.root)
        requested = options.get("episode_ids")
        episode_ids = requested if isinstance(requested, list) else self.dataset.episode_ids()
        selected: list[tuple[str, Any, dict[str, Any]]] = []
        for episode_id in episode_ids:
            inspection = self.dataset.inspect(str(episode_id))
            review = reviews.get(str(episode_id), {})
            processed = bool(review.get("processed", bool(review)))
            if (
                inspection.ready
                and (direct_raw or (processed and not review.get("excluded", False)))
            ):
                # Raw mode deliberately ignores review exclusion and trimming.
                selected.append((str(episode_id), inspection, {} if direct_raw else review))
        if not selected:
            if direct_raw:
                raise DataProcessError("没有结构校验通过、可直接转换的 episode")
            raise DataProcessError("没有已处理且未标记失败的 episode")

        existing_conversion: dict[str, Any] | None = None
        existing_episode_meta: list[dict[str, Any]] = []
        managed_existing = False
        if output.exists():
            try:
                existing_conversion = read_json(output / "meta" / "conversion.json")
                with (output / "meta" / "episodes.jsonl").open("r", encoding="utf-8") as stream:
                    existing_episode_meta = [json.loads(line) for line in stream if line.strip()]
                managed_existing = (
                    existing_conversion.get("source") == str(self.dataset.root)
                    and existing_conversion.get("layout") == layout
                    and existing_conversion.get("conversion_mode", "reviewed")
                    == conversion_mode
                )
            except (DataProcessError, OSError, json.JSONDecodeError):
                managed_existing = False
            if managed_existing:
                old_order = {
                    str(item.get("raw_episode_id")): int(item.get("episode_index", index))
                    for index, item in enumerate(existing_episode_meta)
                }
                selected.sort(
                    key=lambda item: (
                        0 if item[0] in old_order else 1,
                        old_order.get(item[0], 0),
                        item[0],
                    )
                )

        existing_empty = output.is_dir() and not any(output.iterdir())
        if (
            output.exists()
            and not managed_existing
            and not existing_empty
            and not bool(options.get("overwrite", False))
        ):
            raise DataProcessError(f"输出目录已存在且不是当前数据集的历史输出: {output}")
        dataset_signature = self._dataset_signature(
            selected, task, fps, layout, cameras, conversion_mode
        )
        if (
            managed_existing
            and existing_conversion is not None
            and existing_conversion.get("dataset_signature") == dataset_signature
        ):
            progress(1.0, "输出已是最新版本，无需重复转换")
            return {
                "output_root": str(output),
                "backup_root": None,
                "episodes": len(existing_episode_meta),
                "frames": int(existing_conversion.get("frame_count", 0)),
                "videos": len(existing_episode_meta) * len(cameras),
                "layout": layout,
                "conversion_mode": conversion_mode,
                "skipped_unchanged": True,
            }

        building = output.parent / f".{output.name}.building-{uuid.uuid4().hex[:8]}"
        building.mkdir(parents=False, exist_ok=False)
        global_index = 0
        episode_meta: list[dict[str, Any]] = []
        tables = []
        try:
            progress(0.01, f"准备转换 {len(selected)} 个 episode")
            first_manifest = read_json(selected[0][1].path / "manifest.json")
            names = first_manifest.get("dataset_layout", {}).get("names", [])
            dimension = selected[0][1].dimensions
            if len(names) != dimension:
                names = [f"joint_{index}" for index in range(dimension)]
            schema = pa.schema(
                [
                    pa.field("observation.state", pa.list_(pa.float32(), dimension)),
                    pa.field("action", pa.list_(pa.float32(), dimension)),
                    pa.field("timestamp", pa.float32()),
                    pa.field("frame_index", pa.int64()),
                    pa.field("episode_index", pa.int64()),
                    pa.field("index", pa.int64()),
                    pa.field("task_index", pa.int64()),
                    pa.field("annotation.human.task_description", pa.int64()),
                    pa.field("next.reward", pa.float32()),
                    pa.field("next.done", pa.bool_()),
                ]
            )
            for episode_index, (source_id, inspection, review) in enumerate(selected):
                if inspection.dimensions != dimension:
                    raise DataProcessError(
                        f"{source_id} 维度为 {inspection.dimensions}，预期 {dimension}"
                    )
                start_s = max(0.0, float(review.get("trim_start_s", 0.0)))
                end_s = min(
                    inspection.duration_s,
                    float(review.get("trim_end_s", inspection.duration_s)),
                )
                if end_s - start_s < 0.25:
                    raise DataProcessError(f"{source_id} 的裁剪区间过短")
                states = self.dataset.load_state_rows(source_id)
                actions = self.dataset.load_action_rows(source_id)
                state_times = [row_timestamp_ms(row, "state") for row in states]
                action_times = [row_timestamp_ms(row, "action") for row in actions]
                absolute_start_ms = float(inspection.state_start_ms) + start_s * 1000
                frame_count = max(1, int(math.floor((end_s - start_s) * fps)))
                rows: list[dict[str, Any]] = []
                for frame_index in range(frame_count):
                    target_ms = absolute_start_ms + frame_index * 1000 / fps
                    state = vector_from_row(states[_nearest_index(state_times, target_ms)], "observation_state_rad")
                    action = vector_from_row(actions[_nearest_index(action_times, target_ms)], "action_rad")
                    rows.append(
                        {
                            "observation.state": state,
                            "action": action,
                            "timestamp": frame_index / fps,
                            "frame_index": frame_index,
                            "episode_index": episode_index,
                            "index": global_index + frame_index,
                            "task_index": 0,
                            "annotation.human.task_description": 0,
                            "next.reward": 0.0,
                            "next.done": frame_index == frame_count - 1,
                        }
                    )
                table = pa.Table.from_pylist(rows, schema=schema)
                if layout == "chunked":
                    parquet_path = building / "data" / "chunk-000" / f"episode_{episode_index:06d}.parquet"
                    parquet_path.parent.mkdir(parents=True, exist_ok=True)
                    pq.write_table(table, parquet_path, compression="zstd")
                else:
                    tables.append(table)

                for camera in cameras:
                    if layout == "chunked":
                        video_path = (
                            building
                            / "videos"
                            / "chunk-000"
                            / CAMERA_KEYS[camera]
                            / f"episode_{episode_index:06d}.mp4"
                        )
                    else:
                        video_path = (
                            building
                            / "videos"
                            / CAMERA_KEYS[camera]
                            / f"episode_{episode_index:06d}.mp4"
                        )
                    self._encode_video(
                        inspection=inspection,
                        camera=camera,
                        absolute_start_ms=absolute_start_ms,
                        frame_count=frame_count,
                        fps=fps,
                        output=video_path,
                    )
                episode_meta.append(
                    {
                        "episode_index": episode_index,
                        "tasks": [task],
                        "length": frame_count,
                        "raw_episode_id": source_id,
                        "trim_start_s": round(start_s, 4),
                        "trim_end_s": round(end_s, 4),
                    }
                )
                global_index += frame_count
                completed = episode_index + 1
                progress(
                    0.04 + 0.88 * completed / len(selected),
                    f"已转换 {completed}/{len(selected)}: {source_id}",
                )

            if layout == "flat":
                data_dir = building / "data"
                data_dir.mkdir(parents=True, exist_ok=True)
                pq.write_table(pa.concat_tables(tables), data_dir / "train-00000.parquet", compression="zstd")

            info = self._make_info(
                episode_meta=episode_meta,
                cameras=cameras,
                fps=fps,
                dimension=dimension,
                names=names,
                layout=layout,
            )
            _write_json(building / "meta" / "info.json", info)
            _write_json(building / "meta" / "modality.json", self._make_modality(dimension, cameras))
            _write_jsonl(building / "meta" / "episodes.jsonl", episode_meta)
            _write_jsonl(building / "meta" / "tasks.jsonl", [{"task_index": 0, "task": task}])
            _write_json(
                building / "meta" / "conversion.json",
                {
                    "source": str(self.dataset.root),
                    "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "fps": fps,
                    "layout": layout,
                    "conversion_mode": conversion_mode,
                    "episode_count": len(selected),
                    "frame_count": global_index,
                    "camera_names": cameras,
                    "dataset_signature": dataset_signature,
                    "source_episode_ids": [source_id for source_id, _, _ in selected],
                },
            )
            progress(0.95, "校验输出文件")
            self._validate(building, episode_meta, cameras, layout, pq)

            backup: Path | None = None
            if output.exists():
                if output.is_dir() and not any(output.iterdir()):
                    output.rmdir()
                else:
                    if not managed_existing and not bool(options.get("overwrite", False)):
                        raise DataProcessError(
                            f"输出目录已存在且不是当前数据集的历史输出: {output}"
                        )
                    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                    backup = output.parent / f"{output.name}.backup-{stamp}"
                    if backup.exists():
                        backup = output.parent / f"{output.name}.backup-{stamp}-{uuid.uuid4().hex[:4]}"
                    output.rename(backup)
            building.rename(output)
            if backup is not None and managed_existing and not bool(options.get("overwrite", False)):
                shutil.rmtree(backup)
                backup = None
            if not direct_raw:
                self.dataset.mark_exported(
                    [(source_id, index) for index, (source_id, _, _) in enumerate(selected)],
                    output,
                )
            progress(1.0, "转换完成")
            return {
                "output_root": str(output),
                "backup_root": str(backup) if backup else None,
                "episodes": len(episode_meta),
                "frames": global_index,
                "videos": len(episode_meta) * len(cameras),
                "layout": layout,
                "conversion_mode": conversion_mode,
            }
        except Exception:
            if building.exists():
                shutil.rmtree(building)
            raise

    def _dataset_signature(
        self,
        selected: list[tuple[str, Any, dict[str, Any]]],
        task: str,
        fps: float,
        layout: str,
        cameras: list[str],
        conversion_mode: str,
    ) -> str:
        episodes: list[dict[str, Any]] = []
        for source_id, inspection, review in selected:
            files = [
                inspection.path / "manifest.json",
                inspection.path / "observation_state_frame.jsonl",
                inspection.path / "applied_action_frame.jsonl",
            ]
            for camera in cameras:
                files.extend([
                    inspection.path / camera / "rgb.raw",
                    inspection.path / camera / "frames.jsonl",
                ])
            source_signature = []
            for path in files:
                stat = path.stat()
                source_signature.append([
                    str(path.relative_to(inspection.path)), stat.st_size, stat.st_mtime_ns
                ])
            episodes.append({
                "source_id": source_id,
                "trim_start_s": round(float(review.get("trim_start_s", 0.0)), 4),
                "trim_end_s": round(float(review.get("trim_end_s", inspection.duration_s)), 4),
                "source_signature": source_signature,
            })
        payload = {
            "source": str(self.dataset.root),
            "task": task,
            "fps": fps,
            "layout": layout,
            "conversion_mode": conversion_mode,
            "cameras": cameras,
            "episodes": episodes,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


    def _encode_video(
        self,
        inspection: Any,
        camera: str,
        absolute_start_ms: float,
        frame_count: int,
        fps: float,
        output: Path,
    ) -> None:
        info = inspection.cameras[camera]
        seek_s = max(0.0, (absolute_start_ms - info.first_timestamp_ms) / 1000)
        pad_start_s = max(0.0, (info.first_timestamp_ms - absolute_start_ms) / 1000)
        filters: list[str] = []
        if pad_start_s > 0.0005:
            filters.append(f"tpad=start_mode=clone:start_duration={pad_start_s:.6f}")
        filters.extend(
            [
                f"fps={fps:.8f}",
                "tpad=stop_mode=clone:stop_duration=2",
            ]
        )
        output.parent.mkdir(parents=True, exist_ok=True)
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
            f"{info.width}x{info.height}",
            "-framerate",
            f"{info.fps:.8f}",
            "-i",
            str(inspection.path / camera / "rgb.raw"),
            "-vf",
            ",".join(filters),
            "-frames:v",
            str(frame_count),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(max(1, round(fps * 2))),
            "-movflags",
            "+faststart",
            str(output),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = exc.stderr[-2000:] if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            raise DataProcessError(f"{inspection.episode_id}/{camera} 视频转换失败: {detail}") from exc

    @staticmethod
    def _make_modality(dimension: int, cameras: list[str]) -> dict[str, Any]:
        if dimension == 38:
            groups = [
                ("left_arm", 0, 7),
                ("right_arm", 7, 14),
                ("left_hand", 14, 26),
                ("right_hand", 26, 38),
            ]
        else:
            groups = [("joints", 0, dimension)]
        return {
            "state": {name: {"start": start, "end": end} for name, start, end in groups},
            "action": {name: {"start": start, "end": end} for name, start, end in groups},
            "video": {
                camera: {"original_key": CAMERA_KEYS[camera]} for camera in cameras
            },
            "annotation": {
                "human.task_description": {"original_key": "task_index"}
            },
        }

    @staticmethod
    def _make_info(
        episode_meta: list[dict[str, Any]],
        cameras: list[str],
        fps: float,
        dimension: int,
        names: list[str],
        layout: str,
    ) -> dict[str, Any]:
        feature_info: dict[str, Any] = {
            "action": {"dtype": "float32", "shape": [dimension], "names": names},
            "observation.state": {"dtype": "float32", "shape": [dimension], "names": names},
        }
        for camera in cameras:
            feature_info[CAMERA_KEYS[camera]] = {
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
        scalar_features = {
            "timestamp": "float32",
            "frame_index": "int64",
            "episode_index": "int64",
            "index": "int64",
            "task_index": "int64",
            "annotation.human.task_description": "int64",
            "next.reward": "float32",
            "next.done": "bool",
        }
        for key, dtype in scalar_features.items():
            feature_info[key] = {"dtype": dtype, "shape": [1], "names": None}
        total_episodes = len(episode_meta)
        return {
            "codebase_version": "v2.1",
            "robot_type": "G1_robot01",
            "total_episodes": total_episodes,
            "total_frames": sum(item["length"] for item in episode_meta),
            "total_tasks": 1,
            "total_videos": total_episodes * len(cameras),
            "total_chunks": max(1, math.ceil(total_episodes / 1000)),
            "chunks_size": 1000,
            "fps": fps,
            "splits": {"train": f"0:{total_episodes}"},
            "data_path": (
                "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
                if layout == "chunked"
                else "data/train-{episode_chunk:05d}.parquet"
            ),
            "video_path": (
                "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4"
                if layout == "chunked"
                else "videos/{video_key}/episode_{episode_index:06d}.mp4"
            ),
            "features": feature_info,
        }

    @staticmethod
    def _validate(
        root: Path,
        episodes: list[dict[str, Any]],
        cameras: list[str],
        layout: str,
        pq: Any,
    ) -> None:
        info = read_json(root / "meta" / "info.json")
        if info.get("codebase_version") != "v2.1":
            raise DataProcessError("info.json 版本校验失败")
        if layout == "flat":
            table = pq.read_table(root / "data" / "train-00000.parquet")
            expected = sum(item["length"] for item in episodes)
            if table.num_rows != expected:
                raise DataProcessError("Parquet 总帧数校验失败")
        else:
            for episode in episodes:
                index = episode["episode_index"]
                table = pq.read_table(root / "data" / "chunk-000" / f"episode_{index:06d}.parquet")
                if table.num_rows != episode["length"]:
                    raise DataProcessError(f"episode_{index:06d} Parquet 帧数校验失败")
        for episode in episodes:
            index = episode["episode_index"]
            for camera in cameras:
                prefix = root / "videos"
                if layout == "chunked":
                    prefix = prefix / "chunk-000"
                video = prefix / CAMERA_KEYS[camera] / f"episode_{index:06d}.mp4"
                if not video.is_file() or video.stat().st_size < 1024:
                    raise DataProcessError(f"视频输出缺失: {video}")
