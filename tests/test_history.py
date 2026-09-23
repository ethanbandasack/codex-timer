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

    def test_bands_start_at_reset_plus_five_hours_and_shift_with_reset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(Path(temp_dir) / "history.sqlite3")
            reset_at = 3 * 3600
            store.ensure_plan_anchor(reset_at)
            store.add_planned_slot(anchor_at=reset_at)
            store.add_planned_slot(anchor_at=reset_at)
            slots = store.planned_slots()

            shifted_reset = store.shift_plan_anchor(3600)
            shifted_slots = store.planned_slots()

        self.assertEqual(slots[0]["scheduled_at"], 8 * 3600 + 60)
        self.assertEqual(slots[1]["scheduled_at"], 13 * 3600 + 120)
        self.assertEqual(shifted_reset, 4 * 3600)
        self.assertEqual(shifted_slots[0]["scheduled_at"], 9 * 3600 + 60)
        self.assertEqual(shifted_slots[1]["scheduled_at"], 14 * 3600 + 120)

    def test_initializing_reset_anchor_migrates_existing_bands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(Path(temp_dir) / "history.sqlite3")
            store.add_planned_slot(now=1_000)
            store.add_planned_slot(now=1_000)

            reset_at = 50_000
            store.ensure_plan_anchor(reset_at)
            slots = store.planned_slots()

        self.assertEqual(slots[0]["scheduled_at"], reset_at + DEFAULT_PLAN_INTERVAL_SECONDS)
        self.assertEqual(slots[1]["scheduled_at"], reset_at + 2 * DEFAULT_PLAN_INTERVAL_SECONDS)

    def test_new_server_reset_preserves_local_anchor_adjustment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(Path(temp_dir) / "history.sqlite3")
            first_reset = 3 * 3600
            store.ensure_plan_anchor(first_reset)
            store.add_planned_slot(anchor_at=first_reset)
            store.shift_plan_anchor(3600)

            next_reset = 8 * 3600
            anchor = store.ensure_plan_anchor(next_reset)
            slots = store.planned_slots()

        self.assertEqual(anchor, 9 * 3600)
        self.assertEqual(slots[0]["scheduled_at"], 14 * 3600 + 60)

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
