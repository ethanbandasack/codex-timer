import datetime as dt
import tempfile
import unittest
from pathlib import Path

from codex_timer.history import DEFAULT_PLAN_INTERVAL_SECONDS, HistoryStore, capture_history


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

    def test_planned_slots_default_to_five_hours_one_minute_and_shift_later_slots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(Path(temp_dir) / "history.sqlite3")
            first = store.add_planned_slot(now=1_000)
            second = store.add_planned_slot(now=1_000)
            third = store.add_planned_slot(now=1_000)

            store.shift_planned_slots(second["id"], 300)
            slots = store.planned_slots()

        self.assertEqual(first["scheduled_at"], 1_000 + DEFAULT_PLAN_INTERVAL_SECONDS)
        self.assertEqual(
            second["scheduled_at"], first["scheduled_at"] + DEFAULT_PLAN_INTERVAL_SECONDS
        )
        self.assertEqual(slots[0]["scheduled_at"], first["scheduled_at"])
        self.assertEqual(slots[1]["scheduled_at"], second["scheduled_at"] + 300)
        self.assertEqual(slots[2]["scheduled_at"], third["scheduled_at"] + 300)

    def test_deleting_planned_slot_reindexes_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(Path(temp_dir) / "history.sqlite3")
            first = store.add_planned_slot(now=1_000)
            second = store.add_planned_slot(now=1_000)
            third = store.add_planned_slot(now=1_000)

            self.assertTrue(store.delete_planned_slot(second["id"]))
            slots = store.planned_slots()

        self.assertEqual([slot["position"] for slot in slots], [0, 1])
        self.assertEqual([slot["id"] for slot in slots], [first["id"], third["id"]])


if __name__ == "__main__":
    unittest.main()
