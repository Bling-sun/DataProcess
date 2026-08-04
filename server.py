#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dataprocess import __version__
from dataprocess.conversion import convert_dataset
from dataprocess.jobs import JobManager
from dataprocess.raw_dataset import DataProcessError, RawDataset


PROJECT_ROOT = Path(__file__).resolve().parent
WEB_ROOT = PROJECT_ROOT / "web"
RUNTIME_ROOT = PROJECT_ROOT / "runtime"
DEFAULT_RAW_ROOT = Path("/mnt/pangyunyi/figure/raw/20260730")
EPISODE_API_RE = re.compile(r"^/api/episodes/(episode_\d{6})(?:/(.*))?$")
JOB_API_RE = re.compile(r"^/api/jobs/([a-f0-9]{12})$")


class Application:
    def __init__(self, default_raw_root: Path):
        self.default_raw_root = default_raw_root.expanduser().resolve()
        self.runtime_root = RUNTIME_ROOT
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.datasets: dict[str, RawDataset] = {}
        self.jobs = JobManager()

    def dataset(self, raw_root: str | None) -> RawDataset:
        root = Path(raw_root or self.default_raw_root).expanduser().resolve()
        key = str(root)
        if key not in self.datasets:
            self.datasets[key] = RawDataset(root, self.runtime_root)
        return self.datasets[key]


class Handler(BaseHTTPRequestHandler):
    server_version = f"DataProcess/{__version__}"
    protocol_version = "HTTP/1.1"
    application: Application

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {self.address_string()} {format % args}\n")

    def do_GET(self) -> None:
        try:
            self._do_get()
        except DataProcessError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except FileNotFoundError:
            self._json({"error": "资源不存在"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.log_error("Unhandled GET error: %r", exc)
            self._json({"error": f"服务器内部错误: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        try:
            self._do_post()
        except DataProcessError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"error": f"请求参数错误: {exc}"}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.log_error("Unhandled POST error: %r", exc)
            self._json({"error": f"服务器内部错误: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _do_get(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        raw_root = query.get("raw_root", [None])[0]
        if parsed.path == "/api/health":
            self._json(
                {
                    "ok": True,
                    "version": __version__,
                    "default_raw_root": str(self.application.default_raw_root),
                    "ffmpeg": bool(os.environ.get("PATH")) and self._command_exists("ffmpeg"),
                }
            )
            return
        if parsed.path == "/api/episodes":
            refresh = query.get("refresh", ["0"])[0] == "1"
            self._json(self.application.dataset(raw_root).list_episodes(refresh=refresh))
            return
        if parsed.path == "/api/jobs":
            self._json({"jobs": self.application.jobs.list()})
            return
        job_match = JOB_API_RE.fullmatch(parsed.path)
        if job_match:
            job = self.application.jobs.get(job_match.group(1))
            if not job:
                self._json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
            else:
                self._json(job.as_dict())
            return
        episode_match = EPISODE_API_RE.fullmatch(parsed.path)
        if episode_match:
            episode_id, tail = episode_match.groups()
            dataset = self.application.dataset(raw_root)
            if not tail:
                self._json(dataset.detail(episode_id))
                return
            if tail == "series":
                max_points = int(query.get("max_points", ["800"])[0])
                self._json(dataset.series(episode_id, max_points=max_points))
                return
            if tail.startswith("video/"):
                camera = tail.split("/", 1)[1]
                self._send_file(
                    dataset.ensure_preview(episode_id, camera),
                    "video/mp4",
                    allow_range=True,
                    cache_control="private, max-age=31536000, immutable",
                )
                return
            if tail.startswith("poster/"):
                camera = tail.split("/", 1)[1]
                self._send_file(
                    dataset.ensure_poster(episode_id, camera),
                    "image/jpeg",
                    allow_range=False,
                    cache_control="private, max-age=31536000, immutable",
                )
                return
        self._serve_static(parsed.path)

    def _do_post(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        body = self._read_json_body()
        if parsed.path == "/api/scan":
            dataset = self.application.dataset(body.get("raw_root"))
            self._json(dataset.list_episodes(refresh=True))
            return
        if parsed.path == "/api/jobs/convert":
            dataset = self.application.dataset(body.get("raw_root"))
            options = dict(body)
            job = self.application.jobs.create_process(
                "conversion",
                convert_dataset,
                str(dataset.root),
                str(self.application.runtime_root),
                options,
            )
            self._json(job.as_dict(), HTTPStatus.ACCEPTED)
            return
        episode_match = EPISODE_API_RE.fullmatch(parsed.path)
        if episode_match:
            episode_id, tail = episode_match.groups()
            dataset = self.application.dataset(body.get("raw_root"))
            if tail == "review":
                self._json(dataset.save_review(episode_id, body))
                return
            if tail == "auto-trim":
                padding = float(body.get("padding_s", 0.6))
                self._json(dataset.auto_trim(episode_id, padding_s=padding))
                return
        self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1024 * 1024:
            raise DataProcessError("请求体为空或过大")
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise DataProcessError("请求体必须是 application/json")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise DataProcessError("JSON 请求体必须是对象")
        return value

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        candidate = (WEB_ROOT / relative).resolve()
        if WEB_ROOT not in candidate.parents and candidate != WEB_ROOT:
            raise DataProcessError("静态资源路径非法")
        if not candidate.is_file():
            if "." not in Path(relative).name:
                candidate = WEB_ROOT / "index.html"
            else:
                raise FileNotFoundError(candidate)
        mime, _ = mimetypes.guess_type(candidate.name)
        self._send_file(
            candidate,
            mime or "application/octet-stream",
            allow_range=False,
            cache_control="no-cache, no-store, must-revalidate",
        )

    def _send_file(
        self,
        path: Path,
        content_type: str,
        allow_range: bool,
        cache_control: str = "private, max-age=3600",
    ) -> None:
        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range") if allow_range else None
        if range_header and range_header.startswith("bytes="):
            value = range_header[6:].split(",", 1)[0]
            left, _, right = value.partition("-")
            try:
                if left:
                    start = int(left)
                    end = int(right) if right else size - 1
                elif right:
                    start = max(0, size - int(right))
                start = max(0, min(start, size - 1))
                end = max(start, min(end, size - 1))
                status = HTTPStatus.PARTIAL_CONTENT
            except ValueError:
                start, end = 0, size - 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes" if allow_range else "none")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def _command_exists(name: str) -> bool:
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if os.access(Path(directory) / name, os.X_OK):
                return True
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Robot episode review and GR00T conversion web app")
    parser.add_argument("--host", default="0.0.0.0", help="listen address")
    parser.add_argument("--port", default=8088, type=int, help="listen port")
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT, help="default raw dataset")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    application = Application(args.raw_root)
    Handler.application = application
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"DataProcess {__version__}: http://{args.host}:{args.port}")
    print(f"Raw root: {application.default_raw_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
