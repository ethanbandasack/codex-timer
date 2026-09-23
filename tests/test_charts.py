import datetime as dt
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from codex_timer.charts import chart_series, export_chart


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

    def test_export_writes_a_valid_png_without_optional_packages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "usage.png"
            result = export_chart(
                {
                    "days": 7,
                    "daily": [
                        {"usage_date": "2026-09-20", "tokens": 1200},
                        {"usage_date": "2026-09-21", "tokens": 2400},
                    ],
                    "quota": [
                        {"window": "5-hour", "captured_at": 1_800_000_000, "used_percent": 20},
                        {"window": "weekly", "captured_at": 1_800_000_000, "used_percent": 45},
                    ],
                },
                output,
            )
            data = result.read_bytes()

        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(data[12:16], b"IHDR")
        self.assertEqual(struct.unpack(">II", data[16:24]), (1200, 760))
        self.assertIn(b"IDAT", data)
        self.assertTrue(zlib.decompress(data[data.index(b"IDAT") + 4 : -16]))


if __name__ == "__main__":
    unittest.main()
