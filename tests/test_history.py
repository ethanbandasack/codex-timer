import datetime as dt
import json
import sqlite3
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
    def test_local_session_import_stores_only_token_metadata_and_is_incremental(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "sessions"
            root.mkdir()
            log = root / "rollout-session-123.jsonl"
            timestamp = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
            rows = [
                {"type": "response_item", "payload": {"text": "private prompt content"}},
                {
                    "type": "event_msg",
                    "timestamp": timestamp,
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 90,
                                "cached_input_tokens": 20,
                                "output_tokens": 30,
                                "reasoning_output_tokens": 10,
                                "total_tokens": 120,
                            }
                        },
                    },
                },
            ]
            log.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            store = HistoryStore(Path(temp_dir) / "history.sqlite3")

            first_import = store.record_local_session_usage(root)
            second_import = store.record_local_session_usage(root)
            local_tokens = store.history(days=1)["local_tokens"]
            with sqlite3.connect(store.path) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(local_session_usage)")}
                saved_values = " ".join(
                    str(value)
                    for row in db.execute("SELECT * FROM local_session_usage")
                    for value in row
                )

        self.assertEqual(first_import, 1)
        self.assertEqual(second_import, 0)
        self.assertEqual(len(local_tokens), 1)
        self.assertEqual(local_tokens[0]["input_tokens"], 90)
        self.assertEqual(local_tokens[0]["cached_input_tokens"], 20)
        self.assertEqual(local_tokens[0]["output_tokens"], 30)
        self.assertEqual(local_tokens[0]["reasoning_output_tokens"], 10)
        self.assertEqual(local_tokens[0]["total_tokens"], 120)
        self.assertFalse({"prompt", "text", "message", "content"} & columns)
        self.assertNotIn("private prompt content", saved_values)

    def test_local_session_import_retries_a_partial_final_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "sessions"
            root.mkdir()
            log = root / "rollout-session-456.jsonl"
            timestamp = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
            event = {
                "type": "event_msg",
                "timestamp": timestamp,
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"total_tokens": 9}},
                },
            }
            encoded = json.dumps(event).encode("utf-8")
            log.write_bytes(encoded)
            store = HistoryStore(Path(temp_dir) / "history.sqlite3")

            before_newline = store.record_local_session_usage(root)
            with log.open("ab") as stream:
                stream.write(b"\n")
            after_newline = store.record_local_session_usage(root)
            imported = store.history(days=1)["local_tokens"]

        self.assertEqual(before_newline, 0)
        self.assertEqual(after_newline, 1)
        self.assertEqual(imported[0]["total_tokens"], 9)

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

    def test_reset_anchor_cannot_move_before_codex_next_reset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(Path(temp_dir) / "history.sqlite3")
            reset_at = 3 * 3600
            store.ensure_plan_anchor(reset_at)
            store.add_planned_slot(anchor_at=reset_at)

            anchor = store.shift_plan_anchor(-3600)
            exact_anchor = store.set_plan_anchor(2 * 3600)
            slots = store.planned_slots()

        self.assertEqual(anchor, reset_at)
        self.assertEqual(exact_anchor, reset_at)
        self.assertEqual(slots[0]["scheduled_at"], reset_at + DEFAULT_PLAN_INTERVAL_SECONDS)

    def test_reset_anchor_does_not_move_earlier_without_a_known_server_floor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(Path(temp_dir) / "history.sqlite3")
            first_band = store.add_planned_slot(now=100_000)
            anchor = store.ensure_plan_anchor(None)

            shifted = store.shift_plan_anchor(-300)
            exact = store.set_plan_anchor(anchor - 3600)
            slots = store.planned_slots()

        self.assertEqual(shifted, anchor)
        self.assertEqual(exact, anchor)
        self.assertEqual(slots[0]["scheduled_at"], first_band["scheduled_at"])

    def test_band_cannot_move_before_previous_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(Path(temp_dir) / "history.sqlite3")
            reset_at = 3 * 3600
            store.ensure_plan_anchor(reset_at)
            first = store.add_planned_slot(anchor_at=reset_at)
            second = store.add_planned_slot(anchor_at=reset_at)

            store.shift_planned_slots(second["id"], -100_000)
            slots = store.planned_slots()

        self.assertEqual(slots[0]["scheduled_at"], first["scheduled_at"])
        self.assertEqual(slots[1]["scheduled_at"], first["scheduled_at"] + 60)

    def test_exact_band_time_respects_order_and_moves_later_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(Path(temp_dir) / "history.sqlite3")
            reset_at = 3 * 3600
            store.ensure_plan_anchor(reset_at)
            first = store.add_planned_slot(anchor_at=reset_at)
            second = store.add_planned_slot(anchor_at=reset_at)
            third = store.add_planned_slot(anchor_at=reset_at)

            actual = store.set_planned_slot_time(second["id"], reset_at)
            slots = store.planned_slots()

        self.assertEqual(actual, first["scheduled_at"] + 60)
        self.assertEqual(slots[1]["scheduled_at"], actual)
        self.assertEqual(
            slots[2]["scheduled_at"], third["scheduled_at"] + actual - second["scheduled_at"]
        )

    def test_existing_plan_settings_database_gains_custom_interval_column(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute(
                    "CREATE TABLE plan_settings (id INTEGER PRIMARY KEY, reset_anchor_at REAL "
                    "NOT NULL, source_reset_at REAL)"
                )
                db.execute("INSERT INTO plan_settings VALUES (1, 1000, 1000)")

            store = HistoryStore(path)

            self.assertEqual(store.plan_interval(), DEFAULT_PLAN_INTERVAL_SECONDS)

    def test_custom_interval_respaces_existing_bands_and_supports_zero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(Path(temp_dir) / "history.sqlite3")
            reset_at = 3 * 3600
            store.ensure_plan_anchor(reset_at)
            store.add_planned_slot(anchor_at=reset_at)
            store.add_planned_slot(anchor_at=reset_at)

            interval = 8 * 3600 + 60
            store.set_plan_interval(interval)
            spaced = store.planned_slots()
            removed = store.clear_planned_slots()
            saved_interval = store.plan_interval()
            remaining = store.planned_slots()

        self.assertEqual(saved_interval, interval)
        self.assertEqual(spaced[0]["scheduled_at"], reset_at + interval)
        self.assertEqual(spaced[1]["scheduled_at"], reset_at + 2 * interval)
        self.assertEqual(removed, 2)
        self.assertEqual(remaining, [])

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
