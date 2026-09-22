import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_timer.charts import ChartDependencyError, chart_series, export_chart


class ChartTests(unittest.TestCase):
    def test_chart_series_separates_daily_tokens_and_quota_windows(self):
        series = chart_series(
            {
                "days": 7,
                "daily": [{"usage_date": "2026-09-20", "tokens": 1200}],
                "quota": [
                    {"window": "5-hour", "captured_at": 1_800_000_000, "used_percent": 20},
                    {"window": "weekly", "captured_at": 1_800_000_000, "used_percent": 45},
                ],
            }
        )

        self.assertEqual(series["days"], 7)
        self.assertEqual(series["daily"], [(dt.date(2026, 9, 20), 1200)])
        self.assertEqual(series["quota"]["5-hour"][0][1], 20.0)
        self.assertEqual(series["quota"]["weekly"][0][1], 45.0)

    def test_export_gives_install_hint_when_optional_plot_library_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "usage.png"
            with (
                mock.patch("builtins.__import__", side_effect=ImportError("not installed")),
                self.assertRaisesRegex(ChartDependencyError, "Install it with"),
            ):
                export_chart({"daily": [], "quota": {}, "days": 30}, output)


if __name__ == "__main__":
    unittest.main()
