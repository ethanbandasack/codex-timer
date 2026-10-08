import tempfile
import unittest
from pathlib import Path

from codex_timer.scheduler import PromptSchedule, format_schedule_time, parse_schedule_time


class PromptScheduleTests(unittest.TestCase):
    def test_claims_only_due_jobs_and_records_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = PromptSchedule(Path(directory) / "history.sqlite3")
            job_id = jobs.add(100, directory, "model", "low", "Run the task")

            self.assertIsNone(jobs.claim_due(now=99))
            claimed = jobs.claim_due(now=100)
            self.assertEqual(claimed["id"], job_id)
            self.assertEqual(claimed["status"], "running")
            self.assertEqual(claimed["started_at"], 100)

            jobs.finish(job_id, thread_id="thread-1")
            saved = jobs.list()[0]
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(saved["thread_id"], "thread-1")
            self.assertIsNone(jobs.claim_due(now=200))

    def test_cancel_and_recover_interrupted_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = PromptSchedule(Path(directory) / "history.sqlite3")
            cancelled_id = jobs.add(100, directory, "model", "low", "cancel me")
            running_id = jobs.add(100, directory, "model", "low", "retry me")

            self.assertTrue(jobs.cancel(cancelled_id))
            jobs.claim_due(now=100)
            self.assertEqual(jobs.recover_interrupted(), 1)
            self.assertEqual(jobs.list()[0]["status"], "cancelled")
            self.assertEqual(jobs.claim_due(now=100)["id"], running_id)

    def test_schedule_times_format_with_local_offset(self):
        timestamp = parse_schedule_time("2026-10-08T12:00+00:00")
        self.assertEqual(timestamp, 1_791_460_800)
        self.assertTrue(format_schedule_time(timestamp).startswith("2026-10-08T"))


if __name__ == "__main__":
    unittest.main()
