import unittest

from codex_timer.tui import _due_planned_slots


class PlannedPingSchedulerTests(unittest.TestCase):
    def test_returns_newly_due_slots_and_skips_old_slots(self):
        slots = [
            {"id": 1, "scheduled_at": 99},
            {"id": 2, "scheduled_at": 100},
            {"id": 3, "scheduled_at": 151},
            {"id": 4, "scheduled_at": 160},
        ]

        due = _due_planned_slots(slots, previous_check=90, checked_at=160)

        self.assertEqual([slot["id"] for slot in due], [3, 4])

    def test_does_not_repeat_a_slot_after_its_schedule_time(self):
        slots = [{"id": 1, "scheduled_at": 100}]

        due = _due_planned_slots(slots, previous_check=100, checked_at=101)

        self.assertEqual(due, [])


if __name__ == "__main__":
    unittest.main()
