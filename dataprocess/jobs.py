from __future__ import annotations

import datetime as dt
import multiprocessing as mp
import queue
import signal
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


ProcessWork = Callable[..., dict[str, Any]]


def _process_entry(
    work: ProcessWork,
    args: tuple[Any, ...],
    updates: Any,
) -> None:
    """Run native-heavy work outside the HTTP server process."""

    def progress(value: float, message: str) -> None:
        updates.put(("progress", float(value), str(message)))

    try:
        result = work(progress, *args)
    except BaseException as exc:
        updates.put(("error", f"{exc}\n\n{traceback.format_exc(limit=8)}"))
    else:
        updates.put(("result", result))


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"
    progress: float = 0.0
    message: str = "等待执行"
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": round(self.progress, 4),
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, kind: str, work: Callable[[Callable[[float, str], None]], dict[str, Any]]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        with self._lock:
            self._jobs[job.id] = job

        def progress(value: float, message: str) -> None:
            with self._lock:
                job.progress = max(0.0, min(1.0, float(value)))
                job.message = str(message)
                job.updated_at = dt.datetime.now(dt.timezone.utc).isoformat()

        def runner() -> None:
            try:
                with self._lock:
                    job.status = "running"
                    job.message = "开始执行"
                result = work(progress)
                with self._lock:
                    job.status = "completed"
                    job.progress = 1.0
                    job.message = "完成"
                    job.result = result
                    job.updated_at = dt.datetime.now(dt.timezone.utc).isoformat()
            except Exception as exc:  # job boundary: surface a useful error to the UI
                with self._lock:
                    job.status = "failed"
                    job.message = "执行失败"
                    job.error = f"{exc}\n\n{traceback.format_exc(limit=8)}"
                    job.updated_at = dt.datetime.now(dt.timezone.utc).isoformat()

        threading.Thread(target=runner, name=f"job-{job.id}", daemon=True).start()
        return job

    def create_process(self, kind: str, work: ProcessWork, *args: Any) -> Job:
        """Run a job in a spawned process so a native crash cannot kill the web server."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        with self._lock:
            self._jobs[job.id] = job

        context = mp.get_context("spawn")
        updates = context.Queue()
        process = context.Process(
            target=_process_entry,
            args=(work, args, updates),
            name=f"job-{job.id}",
            daemon=True,
        )

        try:
            process.start()
        except Exception as exc:
            with self._lock:
                job.status = "failed"
                job.message = "无法启动导出子进程"
                job.error = str(exc)
                job.updated_at = dt.datetime.now(dt.timezone.utc).isoformat()
            updates.close()
            return job

        with self._lock:
            job.status = "running"
            job.message = "开始执行"
            job.updated_at = dt.datetime.now(dt.timezone.utc).isoformat()

        def monitor() -> None:
            result: dict[str, Any] | None = None
            error: str | None = None

            def receive(block: bool) -> bool:
                nonlocal result, error
                try:
                    if block:
                        update = updates.get(timeout=0.25)
                    else:
                        update = updates.get_nowait()
                except queue.Empty:
                    return False
                update_kind, *payload = update
                with self._lock:
                    if update_kind == "progress":
                        job.progress = max(0.0, min(1.0, float(payload[0])))
                        job.message = str(payload[1])
                        job.updated_at = dt.datetime.now(dt.timezone.utc).isoformat()
                    elif update_kind == "result":
                        result = payload[0]
                    elif update_kind == "error":
                        error = str(payload[0])
                return True

            while process.is_alive():
                receive(block=True)
            process.join()
            while receive(block=False):
                pass

            with self._lock:
                job.updated_at = dt.datetime.now(dt.timezone.utc).isoformat()
                if result is not None and process.exitcode == 0:
                    job.status = "completed"
                    job.progress = 1.0
                    job.message = "完成"
                    job.result = result
                else:
                    job.status = "failed"
                    job.message = "执行失败"
                    if error:
                        job.error = error
                    elif process.exitcode is not None and process.exitcode < 0:
                        try:
                            signal_name = signal.Signals(-process.exitcode).name
                        except ValueError:
                            signal_name = f"signal {-process.exitcode}"
                        job.error = (
                            f"导出子进程因原生库崩溃退出（{signal_name}）。"
                            "网页服务仍在运行；请检查 PyArrow/FFmpeg 版本后重试。"
                        )
                    else:
                        job.error = f"导出子进程异常退出（exit code {process.exitcode}）"
            updates.close()
            updates.join_thread()

        threading.Thread(target=monitor, name=f"monitor-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.as_dict() for job in reversed(list(self._jobs.values()))]
