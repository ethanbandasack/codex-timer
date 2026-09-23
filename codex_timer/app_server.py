"""Small synchronous client for the local Codex App Server stdio protocol."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from typing import Any

DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_EFFORT = "low"
RPC_TIMEOUT_SECONDS = 45


class CodexServer:
    """Own one local App Server process and exchange newline-delimited JSON-RPC messages."""

    def __init__(self, executable: str) -> None:
        self.process = subprocess.Popen(
            [executable, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            bufsize=1,
        )
        self._incoming: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._events: deque[dict[str, Any]] = deque()
        self._next_id = 1
        self._write_lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex_timer",
                        "title": "Codex Timer",
                        "version": "1.0.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.notify("initialized", {})
        except Exception:
            self.close()
            raise

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._incoming.put(json.loads(line))
                except json.JSONDecodeError:
                    self._incoming.put({"_invalid_line": line})
        finally:
            self._incoming.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("Codex App Server stdin is closed")
        with self._write_lock:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def _next_message(self, timeout: float) -> dict[str, Any]:
        if self._events:
            return self._events.popleft()
        try:
            message = self._incoming.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("Timed out waiting for Codex App Server") from exc
        if message is None:
            raise RuntimeError("Codex App Server exited unexpectedly")
        if "_invalid_line" in message:
            raise RuntimeError("Codex App Server returned a non-JSON response")
        return message

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = RPC_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

        deadline = time.monotonic() + timeout
        deferred: list[dict[str, Any]] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._events.extendleft(reversed(deferred))
                raise TimeoutError(f"Timed out waiting for {method}")
            response = self._next_message(remaining)
            if response.get("id") == request_id:
                self._events.extendleft(reversed(deferred))
                if "error" in response:
                    error = response["error"]
                    raise RuntimeError(error.get("message", str(error)))
                return response.get("result", {})
            deferred.append(response)

    def rate_limits(self) -> dict[str, Any]:
        result = self.request("account/rateLimits/read")
        buckets = result.get("rateLimitsByLimitId") or {}
        return buckets.get("codex") or result.get("rateLimits") or {}

    def token_usage(self) -> dict[str, Any]:
        return self.request("account/usage/read")

    def _resolve_ping_model(self, preferred: str, effort: str) -> str:
        catalog = self.request("model/list", {})
        models = catalog.get("data") or []
        available = {}
        for model_info in models:
            if not isinstance(model_info, dict) or model_info.get("hidden"):
                continue
            model_id = model_info.get("id") or model_info.get("model")
            if isinstance(model_id, str):
                available[model_id] = model_info

        if preferred in available:
            selected = preferred
        elif preferred == DEFAULT_MODEL:
            selected = next(
                (
                    model_id
                    for model_id, model_info in available.items()
                    if model_info.get("isDefault")
                ),
                None,
            )
            if selected is None and len(available) == 1:
                selected = next(iter(available))
        else:
            selected = None

        if selected:
            supported_efforts = {
                option.get("reasoningEffort")
                for option in available[selected].get("supportedReasoningEfforts", [])
                if isinstance(option, dict)
            }
            if supported_efforts and effort not in supported_efforts:
                choices = ", ".join(sorted(supported_efforts))
                raise RuntimeError(
                    f"Model {selected!r} does not support {effort!r} effort. "
                    f"Supported efforts: {choices}."
                )
            return selected

        choices = ", ".join(sorted(available)) or "none reported"
        raise RuntimeError(
            f"Model {preferred!r} is not available in this Codex CLI. "
            f"Available models: {choices}. Update Codex or choose an available model."
        )

    def ping(
        self,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        timeout: float = 30,
    ) -> str:
        model = self._resolve_ping_model(model, effort)
        thread_result = self.request(
            "thread/start",
            {
                "model": model,
                "cwd": os.getcwd(),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "serviceName": "codex_timer",
            },
        )
        thread_id = (thread_result.get("thread") or {}).get("id")
        if not thread_id:
            raise RuntimeError("Codex did not return a thread id")

        turn_result = self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "Reply with exactly one word: hello."}],
                "model": model,
                "effort": effort,
            },
            timeout=timeout,
        )
        turn_id = (turn_result.get("turn") or {}).get("id")
        if not turn_id:
            raise RuntimeError("Codex did not return a turn id")

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    self.request(
                        "turn/interrupt",
                        {"threadId": thread_id, "turnId": turn_id},
                        timeout=5,
                    )
                except (RuntimeError, TimeoutError):
                    pass
                raise TimeoutError("The hello turn exceeded its time limit and was interrupted")

            event = self._next_message(remaining)
            if event.get("method") != "turn/completed":
                continue
            completed_turn = (event.get("params") or {}).get("turn") or {}
            if completed_turn.get("id") != turn_id:
                continue
            status = completed_turn.get("status")
            if status != "completed":
                error = completed_turn.get("error") or {}
                detail = error.get("message") or f"turn status: {status}"
                raise RuntimeError(f"The hello turn did not complete ({detail})")
            return model

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
