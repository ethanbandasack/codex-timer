import unittest
from unittest.mock import Mock

from codex_timer.app_server import DEFAULT_EFFORT, DEFAULT_MODEL, CodexServer


class CodexServerTests(unittest.TestCase):
    def test_ping_prefers_requested_model_when_installed_cli_reports_it(self):
        server = object.__new__(CodexServer)
        server.request = Mock(
            side_effect=[
                {"data": [{"id": DEFAULT_MODEL, "isDefault": True}]},
                {"thread": {"id": "thread-1"}},
                {"turn": {"id": "turn-1"}},
            ]
        )
        server._next_message = Mock(
            return_value={
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": "completed"}},
            }
        )

        selected_model = server.ping()

        model_method, model_params = server.request.call_args_list[0].args
        self.assertEqual(model_method, "model/list")
        self.assertEqual(model_params, {})

        thread_method, thread_params = server.request.call_args_list[1].args
        self.assertEqual(thread_method, "thread/start")
        self.assertEqual(thread_params["sandbox"], "read-only")
        self.assertTrue(thread_params["ephemeral"])

        turn_method, turn_params = server.request.call_args_list[2].args
        self.assertEqual(turn_method, "turn/start")
        self.assertEqual(turn_params["threadId"], "thread-1")
        self.assertEqual(turn_params["model"], DEFAULT_MODEL)
        self.assertEqual(turn_params["effort"], DEFAULT_EFFORT)
        self.assertEqual(selected_model, DEFAULT_MODEL)

    def test_ping_falls_back_to_cli_default_when_preferred_model_is_too_new(self):
        server = object.__new__(CodexServer)
        server.request = Mock(
            side_effect=[
                {"data": [{"id": "gpt-5.5", "isDefault": True}]},
                {"thread": {"id": "thread-1"}},
                {"turn": {"id": "turn-1"}},
            ]
        )
        server._next_message = Mock(
            return_value={
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": "completed"}},
            }
        )

        selected_model = server.ping()

        thread_params = server.request.call_args_list[1].args[1]
        turn_params = server.request.call_args_list[2].args[1]
        self.assertEqual(thread_params["model"], "gpt-5.5")
        self.assertEqual(turn_params["model"], "gpt-5.5")
        self.assertEqual(turn_params["effort"], "low")
        self.assertEqual(selected_model, "gpt-5.5")


if __name__ == "__main__":
    unittest.main()
