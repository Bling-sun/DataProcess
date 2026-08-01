from __future__ import annotations

import datetime as dt
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


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

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.as_dict() for job in reversed(list(self._jobs.values()))]
