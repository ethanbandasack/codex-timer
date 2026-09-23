import unittest

from codex_timer.histogram import render_histogram, render_hourly_histogram


class HistogramTests(unittest.TestCase):
    def test_daily_histogram_scales_bars_and_shows_reported_tokens(self):
        lines = render_histogram(
            {
                "days": 14,
                "daily": [
                    {"usage_date": "2026-09-20", "tokens": 1000},
                    {"usage_date": "2026-09-21", "tokens": 2000},
                ],
            },
            width=60,
        )

        self.assertIn("Daily token activity · last 14 days", lines[0])
        self.assertIn("2026-09-20", lines[2])
        self.assertIn("2026-09-21", lines[3])
        self.assertGreater(lines[3].count("#"), lines[2].count("#"))

    def test_quota_snapshots_are_used_until_token_activity_arrives(self):
        lines = render_histogram(
            {
                "daily": [],
                "quota": [
                    {"window": "5-hour", "captured_at": 1_800_000_000, "used_percent": 50},
                ],
            }
        )

        self.assertIn("token activity not available yet", lines[0])
        self.assertIn("50.0%", lines[2])

    def test_hourly_histogram_sums_local_events_into_recent_hours(self):
        now = 1_800_000_000
        current_hour = int(now // 3600) * 3600
        now = current_hour + 600
        lines = render_hourly_histogram(
            {
                "local_tokens": [
                    {"captured_at": current_hour + 120, "total_tokens": 100},
                    {"captured_at": current_hour + 180, "total_tokens": 200},
                    {"captured_at": current_hour - 3600 + 150, "total_tokens": 150},
                ]
            },
            width=60,
            hours=2,
            now=now,
        )

        self.assertIn("last 2 hours", lines[0])
        self.assertIn("150", lines[2])
        self.assertIn("300", lines[3])
        self.assertIn("local Codex session logs", lines[-2])


if __name__ == "__main__":
    unittest.main()
