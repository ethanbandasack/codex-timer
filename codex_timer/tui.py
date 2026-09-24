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
from .charts import ChartDependencyError, default_export_path, export_chart
from .histogram import render_histogram, render_hourly_histogram
from .history import DEFAULT_PLAN_INTERVAL_SECONDS, HistoryStore, capture_history
from .usage import local_time, notify_desktop, remaining_text, reset_map

AUTO_PING_GRACE_SECONDS = 60


def _due_planned_slots(
    slots: list[dict[str, Any]], previous_check: float, checked_at: float
) -> list[dict[str, Any]]:
    """Return bands crossed since the prior check, skipping stale missed times."""
    return [
        slot
        for slot in slots
        if previous_check < slot["scheduled_at"] <= checked_at
        and checked_at - slot["scheduled_at"] < AUTO_PING_GRACE_SECONDS
    ]


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
                    next_auto_check = time.monotonic() + 1
                    last_auto_check = time.time()
                    while not self.stopping.is_set():
                        try:
                            timeout = max(
                                0.1,
                                min(next_poll, next_auto_check) - time.monotonic(),
                            )
                            action = self.actions.get(timeout=timeout)
                        except queue.Empty:
                            action = ""
                        if action == "stop":
                            return
                        if action == "ping":
                            self._ping(server, history, automatic=False)
                            next_poll = time.monotonic() + 60
                        elif action == "refresh" or time.monotonic() >= next_poll:
                            self._refresh(server, history)
                            next_poll = time.monotonic() + 60
                        if time.monotonic() >= next_auto_check:
                            checked_at = time.time()
                            for slot in _due_planned_slots(
                                history.planned_slots(), last_auto_check, checked_at
                            ):
                                scheduled_at = slot["scheduled_at"]
                                if history.claim_planned_ping(slot["id"], scheduled_at):
                                    self._ping(server, history, automatic=True)
                            last_auto_check = checked_at
                            next_auto_check = time.monotonic() + 1
            except (RuntimeError, TimeoutError, OSError, sqlite3.Error) as exc:
                self.emit("connection", text=f"Codex unavailable: {exc}")
                if self.stopping.wait(10):
                    return

    def _refresh(self, server: CodexServer, history: HistoryStore) -> None:
        try:
            limits = capture_history(server, history)
            primary = limits.get("primary") or {}
            reset_at = primary.get("resetsAt")
            if isinstance(reset_at, (int, float)):
                history.ensure_plan_anchor(reset_at)
            self.emit("limits", limits=limits, updated=time.time())
        except (RuntimeError, TimeoutError, OSError, sqlite3.Error) as exc:
            self.emit("connection", text=f"Could not refresh: {exc}")

    def _ping(self, server: CodexServer, history: HistoryStore, automatic: bool) -> None:
        prefix = "Scheduled " if automatic else ""
        self.emit(
            "ping",
            state="running",
            automatic=automatic,
            text=f"Sending {prefix}one-word hello…",
        )
        try:
            used_model = server.ping(DEFAULT_MODEL, DEFAULT_EFFORT, timeout=30)
            self.emit(
                "ping",
                state="done",
                automatic=automatic,
                text=f"{prefix}hello sent with {used_model}; temporary chat closed.",
            )
        except (RuntimeError, TimeoutError, OSError) as exc:
            self.emit(
                "ping",
                state="error",
                automatic=automatic,
                text=f"{prefix}ping failed: {exc}",
            )
        self._refresh(server, history)

    def stop(self) -> None:
        self.stopping.set()
        self.actions.put("stop")


def _export_history(events: queue.Queue[dict[str, Any]]) -> None:
    try:
        store = HistoryStore()
        store.record_local_session_usage()
        history = store.history(30)
        output = export_chart(history, default_export_path())
    except (ChartDependencyError, OSError, sqlite3.Error) as exc:
        events.put({"kind": "export", "state": "error", "text": f"PNG export failed: {exc}"})
        return
    events.put({"kind": "export", "state": "done", "text": f"PNG saved to {output}"})


def _load_history_lines(hourly: bool, width: int, hours: int = 12) -> list[str]:
    store = HistoryStore()
    store.record_local_session_usage()
    history = store.history(2 if hourly else 14)
    if hourly:
        return render_hourly_histogram(history, width, hours=hours)
    return render_histogram(history, width)


def _planned_rows(store: HistoryStore, limits: dict[str, Any]) -> list[dict[str, Any]]:
    primary = limits.get("primary") or {}
    reset_at = primary.get("resetsAt")
    reset_at = reset_at if isinstance(reset_at, (int, float)) else None
    anchor = store.ensure_plan_anchor(reset_at)
    if anchor is None:
        return []
    return [
        {"id": None, "position": -1, "scheduled_at": anchor},
        *store.planned_slots(),
    ]


def _read_prompt(screen: Any, prompt: str, maximum: int) -> str:
    """Read one short line in curses while temporarily disabling the UI timeout."""
    height, width = screen.getmaxyx()
    y = max(0, height - 2)
    screen.move(y, 0)
    screen.clrtoeol()
    _safe_addstr(screen, y, 2, prompt, curses.A_BOLD)
    screen.refresh()
    x = min(width - 2, len(prompt) + 3)
    previous_timeout = 250
    screen.timeout(-1)
    try:
        curses.echo()
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        value = screen.getstr(y, x, max(1, min(maximum, width - x - 1)))
    finally:
        curses.noecho()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.timeout(previous_timeout)
    return value.decode("utf-8", errors="replace").strip()


def _parse_local_datetime(value: str) -> float:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise ValueError("Enter local time without a timezone")
    return parsed.astimezone().timestamp()


def _parse_interval(value: str) -> int:
    hours_text, separator, minutes_text = value.strip().partition(":")
    if not separator or not hours_text.isdigit() or not minutes_text.isdigit():
        raise ValueError("Enter an interval as HH:MM")
    hours, minutes = int(hours_text), int(minutes_text)
    if minutes >= 60 or hours > 999 or (hours == 0 and minutes == 0):
        raise ValueError("Interval must be between 00:01 and 999:59")
    return hours * 3600 + minutes * 60


def _format_interval(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


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
    title_text = f"|  {title}"
    title_text = title_text.ljust(card_width - 1) + "|"
    title_attr = curses.A_BOLD
    rule_attr = curses.A_DIM
    _safe_addstr(screen, y, x, rule, rule_attr)
    _safe_addstr(screen, y + 1, x, title_text, title_attr)
    if not window:
        lines = ("|  Usage unavailable", "|  Reset time unavailable", "|")
    else:
        used = window.get("usedPercent")
        used_text = f"{used:g}% used" if isinstance(used, (int, float)) else "Usage unavailable"
        reset = window.get("resetsAt")
        lines = (f"|  {used_text}", f"|  In {remaining_text(reset)}", f"|  {local_time(reset)}")
    for offset, line in enumerate(lines, start=2):
        _safe_addstr(screen, y + offset, x, line)
    _safe_addstr(screen, y + 5, x, rule, rule_attr)


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
    export_running = False
    last_updated: float | None = None
    show_history = False
    history_hourly = False
    show_schedule = False
    planned_rows: list[dict[str, Any]] = []
    selected_slot_index = 0
    planned_interval = DEFAULT_PLAN_INTERVAL_SECONDS
    history_lines: list[str] = []
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
                if show_schedule:
                    try:
                        previous_anchor = planned_rows[0]["scheduled_at"] if planned_rows else None
                        schedule_store = HistoryStore()
                        planned_rows = _planned_rows(schedule_store, limits)
                        planned_interval = schedule_store.plan_interval()
                        if planned_rows and previous_anchor is None:
                            message = "Reset-based plan loaded"
                            message_attr = curses.color_pair(2) | curses.A_BOLD
                        elif planned_rows and planned_rows[0]["scheduled_at"] != previous_anchor:
                            message = "Codex reset changed; anchor and bands shifted with it"
                            message_attr = curses.color_pair(2) | curses.A_BOLD
                    except sqlite3.Error as exc:
                        message = f"Could not load reset time: {exc}"
                        message_attr = curses.color_pair(3) | curses.A_BOLD
            elif event["kind"] == "ping":
                message = event["text"]
                ping_running = event["state"] == "running"
                if event["state"] == "error":
                    message_attr = curses.color_pair(3) | curses.A_BOLD
                elif ping_running:
                    message_attr = curses.color_pair(4) | curses.A_BOLD
                else:
                    message_attr = curses.color_pair(2) | curses.A_BOLD
            elif event["kind"] == "export":
                export_running = False
                message = event["text"]
                message_attr = (
                    curses.color_pair(2) | curses.A_BOLD
                    if event["state"] == "done"
                    else curses.color_pair(3) | curses.A_BOLD
                )

        screen.erase()
        height, width = screen.getmaxyx()
        cyan = curses.color_pair(1) | curses.A_BOLD
        _safe_addstr(screen, 1, 2, "CODEX TIMER", cyan)
        now_text = dt.datetime.now().astimezone().strftime("%a %d %b  %H:%M:%S %Z")
        _safe_addstr(screen, 1, max(2, width - len(now_text) - 3), now_text, curses.A_DIM)
        _safe_addstr(screen, 2, 2, "=" * max(1, width - 4), curses.A_DIM)
        if show_schedule:
            interval_label = _format_interval(planned_interval)
            _safe_addstr(
                screen,
                3,
                2,
                f"RESET + PING BANDS · INTERVAL {interval_label}",
                curses.color_pair(2) | curses.A_BOLD,
            )
            if planned_rows:
                visible_rows = max(1, height - 12)
                first_row = min(
                    max(0, selected_slot_index - visible_rows + 1),
                    max(0, len(planned_rows) - visible_rows),
                )
                for index in range(first_row, min(len(planned_rows), first_row + visible_rows)):
                    slot = planned_rows[index]
                    scheduled_text = (
                        dt.datetime.fromtimestamp(slot["scheduled_at"])
                        .astimezone()
                        .strftime("%a %d %b  %H:%M %Z")
                    )
                    marker = ">" if index == selected_slot_index else " "
                    label = "RESET" if index == 0 else f"+PING {index:02}"
                    row = (
                        f"{marker} {label:<9} {scheduled_text}"
                        f"   ·   in {remaining_text(slot['scheduled_at'])}"
                    )
                    attr = curses.A_REVERSE | curses.A_BOLD if index == selected_slot_index else 0
                    _safe_addstr(screen, 5 + index - first_row, 3, row, attr)
                if len(planned_rows) == 1:
                    _safe_addstr(
                        screen, 7, 3, "No ping bands planned · 0 planned pings", curses.A_DIM
                    )
            else:
                _safe_addstr(
                    screen,
                    5,
                    3,
                    "Codex reset time unavailable. Press R to refresh before adding a band.",
                )
            _safe_addstr(screen, max(0, height - 6), 2, message, message_attr)
            _safe_addstr(
                screen,
                max(0, height - 4),
                2,
                "[↑↓] Select  [←→] Move selected and later rows ±5m",
                curses.A_BOLD,
            )
            _safe_addstr(
                screen,
                max(0, height - 3),
                2,
                "[E] Exact time  [I] Interval  [+] Add ping  [X] Delete  [0] Clear all",
                curses.A_BOLD,
            )
            _safe_addstr(
                screen,
                max(0, height - 2),
                2,
                "[S] Back · reset cannot move before Codex's next reset",
                curses.A_DIM,
            )
        elif show_history:
            title = "LOCAL USAGE HISTORY"
            _safe_addstr(screen, 3, 2, title, curses.color_pair(2) | curses.A_BOLD)
            for index, line in enumerate(history_lines[: max(0, height - 10)], start=5):
                _safe_addstr(screen, index, 3, line)
            _safe_addstr(screen, max(0, height - 5), 2, message, message_attr)
            _safe_addstr(
                screen,
                max(0, height - 3),
                2,
                "[T] View  [H] Back  [S] Pings  [E] PNG  [R] Refresh  [Q] Quit",
                curses.A_BOLD,
            )
        else:
            online = connection == "Connected to Codex"
            _safe_addstr(screen, 3, 2, connection, curses.color_pair(2 if online else 4))
            if width >= 70:
                card_width = (width - 7) // 2
                _draw_window_card(
                    screen,
                    2,
                    5,
                    card_width,
                    "5-HOUR WINDOW",
                    limits.get("primary"),
                )
                _draw_window_card(
                    screen,
                    card_width + 4,
                    5,
                    card_width,
                    "WEEKLY WINDOW",
                    limits.get("secondary"),
                )
                details_y = 13
            else:
                card_width = width - 4
                _draw_window_card(
                    screen,
                    2,
                    5,
                    card_width,
                    "5-HOUR WINDOW",
                    limits.get("primary"),
                )
                _draw_window_card(
                    screen,
                    2,
                    12,
                    card_width,
                    "WEEKLY WINDOW",
                    limits.get("secondary"),
                )
                details_y = 20

            if limits.get("planType"):
                _safe_addstr(screen, details_y, 2, f"Plan: {limits['planType']}")
            _safe_addstr(
                screen, details_y + 1, 2, f"Ping model: {DEFAULT_MODEL} · {DEFAULT_EFFORT} effort"
            )
            if last_updated is not None:
                updated_text = (
                    dt.datetime.fromtimestamp(last_updated).astimezone().strftime("%H:%M:%S %Z")
                )
                _safe_addstr(
                    screen, details_y + 2, 2, f"Usage refreshed: {updated_text}", curses.A_DIM
                )
            _safe_addstr(screen, max(0, height - 5), 2, message, message_attr)
            _safe_addstr(
                screen,
                max(0, height - 3),
                2,
                "[P] Ping now  [S] Pings  [H] History  [E] PNG  [R] Refresh  [Q] Quit",
                curses.A_BOLD,
            )
            _safe_addstr(
                screen,
                max(0, height - 2),
                2,
                "Scheduled pings run while this app is open · refresh every 60s",
                curses.A_DIM,
            )
        screen.refresh()

        key = screen.getch()
        if key in (ord("q"), ord("Q"), 27):
            keep_running = False
        elif show_schedule and key == curses.KEY_UP:
            selected_slot_index = max(0, selected_slot_index - 1)
        elif show_schedule and key == curses.KEY_DOWN:
            selected_slot_index = min(max(0, len(planned_rows) - 1), selected_slot_index + 1)
        elif show_schedule and key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            if not planned_rows:
                message = "Reset time unavailable. Press R to refresh."
                message_attr = curses.color_pair(4) | curses.A_BOLD
            else:
                try:
                    store = HistoryStore()
                    direction = "later" if key == curses.KEY_RIGHT else "earlier"
                    if selected_slot_index == 0:
                        previous = planned_rows[0]["scheduled_at"]
                        anchor = store.shift_plan_anchor(300 if key == curses.KEY_RIGHT else -300)
                        planned_rows = _planned_rows(store, limits)
                        if anchor == previous and direction == "earlier":
                            message = (
                                "Reset is already at Codex's next reset; it cannot move earlier"
                            )
                        else:
                            message = f"Reset and all bands moved 5 minutes {direction}"
                    else:
                        selected = planned_rows[selected_slot_index]
                        previous = selected["scheduled_at"]
                        store.shift_planned_slots(
                            selected["id"], 300 if key == curses.KEY_RIGHT else -300
                        )
                        planned_rows = _planned_rows(store, limits)
                        moved = planned_rows[selected_slot_index]["scheduled_at"] != previous
                        message = (
                            f"Selected band and later bands moved 5 minutes {direction}"
                            if moved
                            else "Band cannot move before the previous planned row"
                        )
                    message_attr = curses.color_pair(2) | curses.A_BOLD
                except sqlite3.Error as exc:
                    message = f"Could not move planned time: {exc}"
                    message_attr = curses.color_pair(3) | curses.A_BOLD
        elif show_schedule and key in (ord("e"), ord("E")) and planned_rows:
            selected = planned_rows[selected_slot_index]
            current_text = (
                dt.datetime.fromtimestamp(selected["scheduled_at"])
                .astimezone()
                .strftime("%Y-%m-%d %H:%M")
            )
            value = _read_prompt(
                screen,
                f"Time YYYY-MM-DD HH:MM (current {current_text}): ",
                16,
            )
            if value:
                try:
                    requested = _parse_local_datetime(value)
                    store = HistoryStore()
                    actual = (
                        store.set_plan_anchor(requested)
                        if selected_slot_index == 0
                        else store.set_planned_slot_time(selected["id"], requested)
                    )
                    planned_rows = _planned_rows(store, limits)
                    if actual is not None and actual != requested:
                        message = "Time adjusted to keep the schedule after its previous reset/row"
                        message_attr = curses.color_pair(4) | curses.A_BOLD
                    else:
                        message = "Selected time updated; later bands shifted by the same amount"
                        message_attr = curses.color_pair(2) | curses.A_BOLD
                except (ValueError, sqlite3.Error) as exc:
                    message = f"Invalid planned time: {exc}"
                    message_attr = curses.color_pair(3) | curses.A_BOLD
        elif show_schedule and key in (ord("i"), ord("I")):
            if not planned_rows:
                message = "Reset time unavailable. Press R before editing the band interval."
                message_attr = curses.color_pair(4) | curses.A_BOLD
            else:
                interval = _format_interval(planned_interval)
                value = _read_prompt(screen, f"Band interval HH:MM (now {interval}): ", 7)
            if planned_rows and value:
                try:
                    seconds = _parse_interval(value)
                    store = HistoryStore()
                    store.set_plan_interval(seconds)
                    planned_interval = seconds
                    planned_rows = _planned_rows(store, limits)
                    message = (
                        f"Band interval set to {_format_interval(seconds)}; existing bands respaced"
                    )
                    message_attr = curses.color_pair(2) | curses.A_BOLD
                except (ValueError, sqlite3.Error) as exc:
                    message = f"Invalid band interval: {exc}"
                    message_attr = curses.color_pair(3) | curses.A_BOLD
        elif show_schedule and key == ord("+"):
            if not planned_rows:
                message = "Reset time unavailable. Press R to refresh before adding a band."
                message_attr = curses.color_pair(4) | curses.A_BOLD
            else:
                try:
                    store = HistoryStore()
                    store.add_planned_slot(anchor_at=planned_rows[0]["scheduled_at"])
                    planned_interval = store.plan_interval()
                    planned_rows = _planned_rows(store, limits)
                    selected_slot_index = len(planned_rows) - 1
                    message = f"Added band {_format_interval(store.plan_interval())} after the previous row"
                    message_attr = curses.color_pair(2) | curses.A_BOLD
                except sqlite3.Error as exc:
                    message = f"Could not add band: {exc}"
                    message_attr = curses.color_pair(3) | curses.A_BOLD
        elif show_schedule and key in (ord("x"), ord("X")) and selected_slot_index > 0:
            try:
                store = HistoryStore()
                store.delete_planned_slot(planned_rows[selected_slot_index]["id"])
                planned_rows = _planned_rows(store, limits)
                selected_slot_index = min(selected_slot_index, max(0, len(planned_rows) - 1))
                message = "Deleted selected band"
                message_attr = curses.color_pair(2) | curses.A_BOLD
            except sqlite3.Error as exc:
                message = f"Could not delete band: {exc}"
                message_attr = curses.color_pair(3) | curses.A_BOLD
        elif show_schedule and key == ord("0"):
            try:
                removed = HistoryStore().clear_planned_slots()
                planned_rows = _planned_rows(HistoryStore(), limits)
                selected_slot_index = 0
                message = (
                    f"Cleared {removed} ping band{'s' if removed != 1 else ''}; 0 pings planned"
                )
                message_attr = curses.color_pair(2) | curses.A_BOLD
            except sqlite3.Error as exc:
                message = f"Could not clear planned bands: {exc}"
                message_attr = curses.color_pair(3) | curses.A_BOLD
        elif key in (ord("s"), ord("S")):
            show_schedule = not show_schedule
            show_history = False
            if show_schedule:
                try:
                    schedule_store = HistoryStore()
                    planned_rows = _planned_rows(schedule_store, limits)
                    planned_interval = schedule_store.plan_interval()
                    selected_slot_index = min(selected_slot_index, max(0, len(planned_rows) - 1))
                    message = (
                        "Reset-based plan opened"
                        if planned_rows
                        else "Reset time unavailable. Press R to refresh."
                    )
                    message_attr = curses.A_DIM if planned_rows else curses.color_pair(4)
                except sqlite3.Error as exc:
                    planned_rows = []
                    message = f"Could not read planned slots: {exc}"
                    message_attr = curses.color_pair(3) | curses.A_BOLD
            else:
                message = "Usage dashboard"
                message_attr = curses.A_DIM
        elif key in (ord("h"), ord("H")):
            show_history = not show_history
            show_schedule = False
            if show_history:
                try:
                    history_hourly = False
                    history_lines = _load_history_lines(False, width - 6)
                except (OSError, sqlite3.Error) as exc:
                    history_lines = [f"Could not read local history: {exc}"]
        elif show_history and key in (ord("t"), ord("T")):
            history_hourly = not history_hourly
            try:
                history_lines = _load_history_lines(
                    history_hourly, width - 6, hours=max(1, min(12, height - 12))
                )
            except (OSError, sqlite3.Error) as exc:
                history_lines = [f"Could not read local history: {exc}"]
        elif key in (ord("r"), ord("R")):
            worker.actions.put("refresh")
            message = "Refreshing usage…"
            message_attr = curses.A_DIM
            if show_history:
                try:
                    history_lines = _load_history_lines(
                        history_hourly, width - 6, hours=max(1, min(12, height - 12))
                    )
                except (OSError, sqlite3.Error) as exc:
                    history_lines = [f"Could not read local history: {exc}"]
        elif key in (ord("p"), ord("P")) and not ping_running:
            worker.actions.put("ping")
            ping_running = True
            message = "Starting hello ping…"
            message_attr = curses.color_pair(4) | curses.A_BOLD
        elif key in (ord("e"), ord("E")) and not export_running:
            export_running = True
            message = "Exporting usage curves to PNG…"
            message_attr = curses.color_pair(4) | curses.A_BOLD
            threading.Thread(target=_export_history, args=(worker.events,), daemon=True).start()

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
