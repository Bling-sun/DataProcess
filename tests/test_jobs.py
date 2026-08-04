from __future__ import annotations

import os
import time
import unittest

from dataprocess.jobs import JobManager


def successful_process(progress, value: int) -> dict[str, int]:
    progress(0.5, "halfway")
    return {"value": value}


def abruptly_exiting_process(_progress) -> dict:
    os._exit(23)


class ProcessJobTest(unittest.TestCase):
    @staticmethod
    def wait_for_job(manager: JobManager, job_id: str, timeout: float = 8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = manager.get(job_id)
            if job is not None and job.status in {"completed", "failed"}:
                return job
            time.sleep(0.05)
        raise AssertionError("job did not finish before timeout")

    def test_process_job_reports_result(self) -> None:
        manager = JobManager()
        created = manager.create_process("test", successful_process, 42)
        job = self.wait_for_job(manager, created.id)
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.result, {"value": 42})
        self.assertEqual(job.progress, 1.0)

    def test_abrupt_process_exit_is_contained(self) -> None:
        manager = JobManager()
        created = manager.create_process("test", abruptly_exiting_process)
        job = self.wait_for_job(manager, created.id)
        self.assertEqual(job.status, "failed")
        self.assertIn("exit code 23", job.error or "")
        self.assertIsNotNone(manager.get(created.id))


if __name__ == "__main__":
    unittest.main()
