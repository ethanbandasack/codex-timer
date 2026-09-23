"""Full-screen, dependency-free terminal interface."""

from __future__ import annotations

import curses
import datetime as dt
import queue
import sqlite3
import threading
import time
from typing import Any

from .app_server import DEFAULT_EFFORT, DEFAULT_MODEL, CodexServer
from .history import HistoryStore, capture_history
from .usage import local_time, notify_desktop, remaining_text, reset_map


class UsageWorker(threading.Thread):
    """Keep Codex I/O off the UI thread and publish updates through a queue."""

    def __init__(self, executable: str) -> None:
        super().__init__(name="codex-timer-usage", daemon=True)
        self.executable = executable
        self.actions: queue.Queue[str] = queue.Queue()
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stopping = threading.Event()

    def emit(self, kind: str, **payload: Any) -> None:
        self.events.put({"kind": kind, **payload})

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                history = HistoryStore()
                with CodexServer(self.executable) as server:
                    self.emit("connection", text="Connected to Codex")
                    self._refresh(server, history)
                    next_poll = time.monotonic() + 60
                    while not self.stopping.is_set():
                        try:
                            action = self.actions.get(timeout=max(0.1, next_poll - time.monotonic()))
                        except queue.Empty:
                            action = "refresh"
                        if action == "stop":
                            return
                        if action == "ping":
                            self.emit("ping", state="running", text="Sending one-word hello…")
                            try:
                                server.ping(DEFAULT_MODEL, DEFAULT_EFFORT, timeout=30)
                                self.emit("ping", state="done", text="Hello sent; temporary chat closed.")
                            except (RuntimeError, TimeoutError, OSError) as exc:
                                self.emit("ping", state="error", text=f"Ping failed: {exc}")
                        if action in ("refresh", "ping"):
                            self._refresh(server, history)
                            next_poll = time.monotonic() + 60
            except (RuntimeError, TimeoutError, OSError, sqlite3.Error) as exc:
                self.emit("connection", text=f"Codex unavailable: {exc}")
                if self.stopping.wait(10):
                    return

    def _refresh(self, server: CodexServer, history: HistoryStore) -> None:
        try:
            limits = capture_history(server, history)
            self.emit("limits", limits=limits, updated=time.time())
        except (RuntimeError, TimeoutError, OSError, sqlite3.Error) as exc:
            self.emit("connection", text=f"Could not refresh: {exc}")

    def stop(self) -> None:
        self.stopping.set()
        self.actions.put("stop")


def _safe_addstr(screen: Any, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = screen.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width:
        return
    try:
        screen.addnstr(y, x, text, max(0, width - x - 1), attr)
    except curses.error:
        pass


def _draw_window_card(
    screen: Any,
    x: int,
    y: int,
    width: int,
    title: str,
    window: dict[str, Any] | None,
) -> None:
    card_width = max(16, min(width, screen.getmaxyx()[1] - x - 1))
    rule = "+" + "-" * (card_width - 2) + "+"
    _safe_addstr(screen, y, x, rule, curses.A_DIM)
    _safe_addstr(screen, y + 1, x, "| " + title, curses.A_BOLD)
    if not window:
        lines = ("|  Usage unavailable", "|  Reset time unavailable", "|")
    else:
        used = window.get("usedPercent")
        used_text = f"{used:g}% used" if isinstance(used, (int, float)) else "Usage unavailable"
        reset = window.get("resetsAt")
        lines = (f"|  {used_text}", f"|  In {remaining_text(reset)}", f"|  {local_time(reset)}")
    for offset, line in enumerate(lines, start=2):
        _safe_addstr(screen, y + offset, x, line)
    _safe_addstr(screen, y + 5, x, rule, curses.A_DIM)


def _terminal_app(screen: Any, executable: str) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    screen.timeout(250)
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            for number, color in (
                (1, curses.COLOR_CYAN),
                (2, curses.COLOR_GREEN),
                (3, curses.COLOR_RED),
                (4, curses.COLOR_YELLOW),
            ):
                curses.init_pair(number, color, -1)
        except curses.error:
            pass

    worker = UsageWorker(executable)
    worker.start()
    limits: dict[str, Any] = {}
    old_resets: dict[str, int | float] = {}
    connection = "Starting Codex…"
    message = "Fetching account reset windows"
    message_attr = curses.A_DIM
    ping_running = False
    last_updated: float | None = None
    keep_running = True

    while keep_running:
        while True:
            try:
                event = worker.events.get_nowait()
            except queue.Empty:
                break
            if event["kind"] == "connection":
                connection = event["text"]
            elif event["kind"] == "limits":
                new_limits = event.get("limits") or {}
                new_resets = reset_map(new_limits)
                updated = event.get("updated", time.time())
                for label, previous in old_resets.items():
                    current = new_resets.get(label)
                    if current and current > previous and previous <= updated + 60:
                        message = f"{label} reset detected"
                        message_attr = curses.color_pair(2) | curses.A_BOLD
                        notify_desktop("Codex Timer", f"Codex {label} usage window reset")
                        curses.beep()
                limits = new_limits
                old_resets = new_resets
                last_updated = updated
                connection = "Connected to Codex"
            elif event["kind"] == "ping":
                message = event["text"]
                ping_running = event["state"] == "running"
                if event["state"] == "error":
                    message_attr = curses.color_pair(3) | curses.A_BOLD
                elif ping_running:
                    message_attr = curses.color_pair(4) | curses.A_BOLD
                else:
                    message_attr = curses.color_pair(2) | curses.A_BOLD

        screen.erase()
        height, width = screen.getmaxyx()
        cyan = curses.color_pair(1) | curses.A_BOLD
        safe_addstr(screen, 1, 2, "CODEX TIMER", cyan)
        now_text = dt.datetime.now().astimezone().strftime("%a %d %b  %H:%M:%S %Z")
        safe_addstr(screen, 1, max(2, width - len(now_text) - 3), now_text, curses.A_DIM)
        safe_addstr(screen, 2, 2, "=" * max(1, width - 4), curses.A_DIM)
        online = connection == "Connected to Codex"
        safe_addstr(screen, 3, 2, connection, curses.color_pair(2 if online else 4))

        if width >= 70:
            card_width = (width - 7) // 2
            _draw_window_card(screen, 2, 5, card_width, "5-HOUR WINDOW", limits.get("primary"))
            _draw_window_card(screen, card_width + 4, 5, card_width, "WEEKLY WINDOW", limits.get("secondary"))
            details_y = 13
        else:
            card_width = width - 4
            _draw_window_card(screen, 2, 5, card_width, "5-HOUR WINDOW", limits.get("primary"))
            _draw_window_card(screen, 2, 12, card_width, "WEEKLY WINDOW", limits.get("secondary"))
            details_y = 20

        if limits.get("planType"):
            _safe_addstr(screen, details_y, 2, f"Plan: {limits['planType']}")
        _safe_addstr(screen, details_y + 1, 2, f"Ping model: {DEFAULT_MODEL} · {DEFAULT_EFFORT} effort")
        if last_updated is not None:
            updated_text = dt.datetime.fromtimestamp(last_updated).astimezone().strftime("%H:%M:%S %Z")
            _safe_addstr(screen, details_y + 2, 2, f"Usage refreshed: {updated_text}", curses.A_DIM)
        _safe_addstr(screen, max(0, height - 5), 2, message, message_attr)
        _safe_addstr(screen, max(0, height - 3), 2, "[P] Ping hello   [R] Refresh usage   [Q] Quit", curses.A_BOLD)
        _safe_addstr(screen, max(0, height - 2), 2, "Server reset times · automatic refresh every 60s", curses.A_DIM)
        screen.refresh()

        key = screen.getch()
        if key in (ord("q"), ord("Q"), 27):
            keep_running = False
        elif key in (ord("r"), ord("R")):
            worker.actions.put("refresh")
            message = "Refreshing usage…"
            message_attr = curses.A_DIM
        elif key in (ord("p"), ord("P")) and not ping_running:
            worker.actions.put("ping")
            ping_running = True
            message = "Starting hello ping…"
            message_attr = curses.color_pair(4) | curses.A_BOLD

    worker.stop()
    worker.join(timeout=3)


def run_terminal_app(executable: str) -> int:
    try:
        curses.wrapper(_terminal_app, executable)
        return 0
    except curses.error as exc:
        print(f"Could not start the terminal UI: {exc}")
        print("Try opening a normal Terminal window with a larger size.")
        return 1
