import unittest

from codex_timer.usage import remaining_text, reset_map, window_rows


class UsageFormattingTests(unittest.TestCase):
    def test_names_primary_and_weekly_windows(self):
        limits = {
            "primary": {"windowDurationMins": 300, "resetsAt": 200},
            "secondary": {"windowDurationMins": 10080, "resetsAt": 300},
        }

        self.assertEqual([label for label, _ in window_rows(limits)], ["5-hour", "weekly"])
        self.assertEqual(reset_map(limits), {"5-hour": 200, "weekly": 300})

    def test_countdown_is_clamped_and_formatted(self):
        self.assertEqual(remaining_text(7200, now=3600), "01:00:00")
        self.assertEqual(remaining_text(100, now=200), "00:00:00")


if __name__ == "__main__":
    unittest.main()
