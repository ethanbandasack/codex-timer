import datetime as dt
import tempfile
import unittest
from pathlib import Path

from codex_timer.history import HistoryStore, capture_history


class FakeUsageSource:
    def rate_limits(self):
        return {
            "planType": "plus",
            "primary": {"usedPercent": 12, "resetsAt": 2_000_000_000, "windowDurationMins": 300},
            "secondary": {
                "usedPercent": 34,
                "resetsAt": 2_000_100_000,
                "windowDurationMins": 10080,
            },
        }

    def token_usage(self):
        return {
            "summary": {"lifetimeTokens": 12345, "peakDailyTokens": 2345},
            "dailyUsageBuckets": [
                {"startDate": dt.datetime.now().astimezone().date().isoformat(), "tokens": 678}
            ],
        }


class HistoryStoreTests(unittest.TestCase):
    def test_capture_persists_quota_and_daily_token_activity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(Path(temp_dir) / "history.sqlite3")
            captured = capture_history(FakeUsageSource(), store)
            history = store.history(days=1)

        self.assertEqual(captured["planType"], "plus")
        self.assertEqual([row["window"] for row in history["quota"]], ["5-hour", "weekly"])
        self.assertEqual(history["daily"][0]["tokens"], 678)
        self.assertEqual(history["summary"]["lifetime_tokens"], 12345)

    def test_daily_token_bucket_updates_instead_of_duplicating(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(Path(temp_dir) / "history.sqlite3")
            today = dt.datetime.now().astimezone().date().isoformat()
            store.record_token_activity({"dailyUsageBuckets": [{"startDate": today, "tokens": 20}]})
            store.record_token_activity({"dailyUsageBuckets": [{"startDate": today, "tokens": 30}]})
            history = store.history(days=1)

        self.assertEqual(len(history["daily"]), 1)
        self.assertEqual(history["daily"][0]["tokens"], 30)


if __name__ == "__main__":
    unittest.main()
