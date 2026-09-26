import unittest
from unittest.mock import ANY, Mock, patch

from codex_timer.usage import notify_desktop, remaining_text, reset_map, window_rows


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

    @patch("codex_timer.usage.subprocess.run")
    @patch("codex_timer.usage.shutil.which", return_value="/usr/bin/notify-send")
    def test_desktop_notification_uses_ubuntu_notify_send(self, _which, run):
        run.return_value = Mock(returncode=0)

        self.assertTrue(notify_desktop("Codex Timer", "Window reset"))

        run.assert_called_once_with(
            ["/usr/bin/notify-send", "Codex Timer", "Window reset"],
            stdout=ANY,
            stderr=ANY,
            check=False,
        )

    @patch("codex_timer.usage.shutil.which", return_value=None)
    def test_desktop_notification_is_optional(self, _which):
        self.assertFalse(notify_desktop("Codex Timer", "Window reset"))


if __name__ == "__main__":
    unittest.main()
