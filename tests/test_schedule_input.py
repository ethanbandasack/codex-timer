import datetime as dt
import unittest

from codex_timer.tui import _format_interval, _parse_interval, _parse_local_datetime


class ScheduleInputTests(unittest.TestCase):
    def test_custom_interval_parsing_and_formatting(self):
        self.assertEqual(_parse_interval("8:01"), 8 * 3600 + 60)
        self.assertEqual(_format_interval(8 * 3600 + 60), "08:01")
        with self.assertRaises(ValueError):
            _parse_interval("00:00")
        with self.assertRaises(ValueError):
            _parse_interval("5:60")

    def test_exact_time_input_uses_local_timezone(self):
        value = _parse_local_datetime("2026-09-23 14:35")
        parsed = dt.datetime.fromtimestamp(value).astimezone()
        self.assertEqual(parsed.strftime("%Y-%m-%d %H:%M"), "2026-09-23 14:35")


if __name__ == "__main__":
    unittest.main()
