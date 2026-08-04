from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from dataprocess.conversion import Converter
from dataprocess.raw_dataset import (
    CAMERAS,
    DataProcessError,
    RawDataset,
    preview_sampling_plan,
)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class RawDatasetTest(unittest.TestCase):
    def setUp(self) -> None:
        project_tmp = Path(__file__).resolve().parents[1] / "runtime" / "test-tmp"
        project_tmp.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=project_tmp)
        root = Path(self.temp.name)
        self.raw = root / "raw"
        self.runtime = root / "runtime"
        self.raw.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_episode(self, episode_id: str = "episode_000000", complete: bool = True) -> Path:
        episode = self.raw / episode_id
        episode.mkdir()
        if not complete:
            for camera in CAMERAS:
                (episode / camera).mkdir()
            return episode
        manifest = {
            "episode_id": episode_id,
            "status": "finished",
            "reason": "operator_finished",
            "dataset_layout": {"names": ["j0", "j1"]},
            "counts": {
                "observation_state_frame": 20,
                "applied_action_frame": 20,
                "camera:head": 20,
                "camera:left_wrist": 20,
                "camera:right_wrist": 20,
            },
        }
        (episode / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        state_rows = []
        action_rows = []
        for index in range(20):
            value = 0.0 if index < 5 or index > 15 else (index - 5) * 0.02
            state_rows.append(
                {
                    "source_timestamp_ms": 1000 + index * 50,
                    "observation_state_rad": [value, value],
                }
            )
            action_rows.append(
                {
                    "source_timestamp_ms": 1000 + index * 50,
                    "action_rad": [value + 0.01, value + 0.01],
                }
            )
        write_jsonl(episode / "observation_state_frame.jsonl", state_rows)
        write_jsonl(episode / "applied_action_frame.jsonl", action_rows)
        write_jsonl(episode / "events.jsonl", [{"event": "segment_started"}, {"event": "segment_paused"}])
        for camera in CAMERAS:
            camera_dir = episode / camera
            camera_dir.mkdir()
            frames = [
                {
                    "device_timestamp_ms": 1000 + index * 50,
                    "width": 2,
                    "height": 2,
                    "offset": index * 12,
                    "size_bytes": 12,
                }
                for index in range(20)
            ]
            write_jsonl(camera_dir / "frames.jsonl", frames)
            (camera_dir / "rgb.raw").write_bytes(bytes(20 * 12))
        return episode

    def test_scan_distinguishes_complete_and_incomplete(self) -> None:
        self.make_episode("episode_000000", complete=True)
        self.make_episode("episode_000001", complete=False)
        dataset = RawDataset(self.raw, self.runtime)
        payload = dataset.list_episodes()
        self.assertEqual(payload["counts"]["total"], 2)
        self.assertEqual(payload["counts"]["ready"], 1)
        self.assertEqual(payload["counts"]["failed"], 1)
        self.assertEqual(payload["episodes"][0]["dimensions"], 2)
        self.assertRegex(
            payload["episodes"][0]["cameras"]["head"]["cache_version"],
            r"^[0-9a-f]+-[0-9a-f]+$",
        )

    def test_review_excludes_without_touching_raw(self) -> None:
        episode = self.make_episode()
        dataset = RawDataset(self.raw, self.runtime)
        result = dataset.save_review(
            "episode_000000",
            {"excluded": True, "trim_start_s": 0.1, "trim_end_s": 0.8, "note": "bad grasp"},
        )
        self.assertTrue(result["excluded"])
        self.assertTrue((episode / "manifest.json").exists())
        self.assertEqual(dataset.list_episodes()["counts"]["exportable"], 0)

    def test_workflow_requires_review_and_prevents_duplicate_export(self) -> None:
        self.make_episode()
        dataset = RawDataset(self.raw, self.runtime)
        initial = dataset.list_episodes()
        self.assertEqual(initial["counts"]["unprocessed"], 1)
        self.assertEqual(initial["counts"]["pending_export"], 0)
        self.assertTrue(initial["episodes"][0]["default_success"])

        reviewed = dataset.save_review(
            "episode_000000",
            {"excluded": False, "trim_start_s": 0.0, "trim_end_s": 0.8},
        )
        self.assertEqual(reviewed["workflow_status"], "processed")
        self.assertTrue(reviewed["export_eligible"])

        dataset.mark_exported([("episode_000000", 0)], self.raw.parent / "output")
        exported = dataset.list_episodes()
        self.assertEqual(exported["counts"]["exported"], 1)
        self.assertEqual(exported["counts"]["pending_export"], 0)
        self.assertFalse(exported["episodes"][0]["export_eligible"])

    def test_auto_trim_produces_valid_window(self) -> None:
        self.make_episode()
        dataset = RawDataset(self.raw, self.runtime)
        result = dataset.auto_trim("episode_000000", padding_s=0.05)
        self.assertGreaterEqual(result["trim_start_s"], 0)
        self.assertLessEqual(result["trim_end_s"], result["duration_s"])
        self.assertGreater(result["trim_end_s"] - result["trim_start_s"], 0.25)

    def test_rejects_path_traversal(self) -> None:
        self.make_episode()
        dataset = RawDataset(self.raw, self.runtime)
        with self.assertRaises(DataProcessError):
            dataset.detail("../episode_000000")

    def test_conversion_rejects_trash_output(self) -> None:
        self.make_episode()
        dataset = RawDataset(self.raw, self.runtime)
        trash_output = Path.home() / ".local" / "share" / "Trash" / "files" / "dataset"
        with self.assertRaisesRegex(DataProcessError, "回收站"):
            Converter(dataset).convert(
                {"output_root": str(trash_output)},
                lambda _value, _message: None,
            )

    def test_preview_sampling_matches_20_hz_timeline(self) -> None:
        indices, fps = preview_sampling_plan(854, 29.9917)
        self.assertEqual(indices[0], 0)
        self.assertEqual(indices[-1], 853)
        self.assertTrue(all(left < right for left, right in zip(indices, indices[1:])))
        self.assertLess(len(indices), 854)
        self.assertAlmostEqual(fps, 20.0, delta=0.05)

    def test_preview_sampling_preserves_slower_source(self) -> None:
        indices, fps = preview_sampling_plan(20, 15.0)
        self.assertEqual(indices, list(range(20)))
        self.assertEqual(fps, 15.0)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_sampled_preview_pipe_writes_mp4(self) -> None:
        width, height, frame_count = 16, 16, 30
        frame_size = width * height * 3
        source = Path(self.temp.name) / "preview.raw"
        output = Path(self.temp.name) / "preview.mp4"
        source.write_bytes(
            b"".join(bytes([index * 7 % 256]) * frame_size for index in range(frame_count))
        )
        indices, fps = preview_sampling_plan(frame_count, 30.0)
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pixel_format", "bgr24",
            "-video_size", f"{width}x{height}", "-framerate", f"{fps:.6f}",
            "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
        ]
        RawDataset._encode_sampled_preview(command, source, frame_size, indices)
        self.assertTrue(output.is_file())
        self.assertGreater(output.stat().st_size, 1024)


    def test_conversion_sync_skips_unchanged_appends_and_replaces(self) -> None:
        self.make_episode("episode_000000")
        dataset = RawDataset(self.raw, self.runtime)
        dataset.save_review(
            "episode_000000",
            {"excluded": False, "trim_start_s": 0.0, "trim_end_s": 0.9},
        )
        output = Path(self.temp.name) / "output"
        output.mkdir()
        options = {
            "output_root": str(output),
            "task": "sort parcel",
            "fps": 20,
            "layout": "chunked",
            "cameras": ["head"],
            "episode_ids": ["episode_000000"],
        }
        first = Converter(dataset).convert(options, lambda _value, _message: None)
        self.assertFalse(first.get("skipped_unchanged", False))
        second = Converter(dataset).convert(options, lambda _value, _message: None)
        self.assertTrue(second["skipped_unchanged"])

        self.make_episode("episode_000001")
        dataset.save_review(
            "episode_000001",
            {"excluded": False, "trim_start_s": 0.0, "trim_end_s": 0.9},
        )
        options["episode_ids"] = ["episode_000000", "episode_000001"]
        appended = Converter(dataset).convert(options, lambda _value, _message: None)
        self.assertEqual(appended["episodes"], 2)
        with (output / "meta" / "episodes.jsonl").open(encoding="utf-8") as stream:
            metadata = [json.loads(line) for line in stream]
        self.assertEqual(
            [item["raw_episode_id"] for item in metadata],
            ["episode_000000", "episode_000001"],
        )

        dataset.save_review(
            "episode_000000",
            {"excluded": False, "trim_start_s": 0.1, "trim_end_s": 0.8},
        )
        replaced = Converter(dataset).convert(options, lambda _value, _message: None)
        self.assertFalse(replaced.get("skipped_unchanged", False))
        with (output / "meta" / "episodes.jsonl").open(encoding="utf-8") as stream:
            metadata = [json.loads(line) for line in stream]
        self.assertEqual(metadata[0]["episode_index"], 0)
        self.assertEqual(metadata[0]["trim_start_s"], 0.1)
        self.assertEqual(metadata[1]["episode_index"], 1)


if __name__ == "__main__":
    unittest.main()
