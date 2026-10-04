"""Email-style prompt composer for the persistent scheduler."""

from __future__ import annotations

import curses
import datetime as dt
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .app_server import DEFAULT_EFFORT, DEFAULT_MODEL, CodexServer
from .scheduler import PromptSchedule, format_schedule_time


HEADER_FIELDS = ("Send at", "Model", "Reasoning", "Directory")
FIELD_COUNT = len(HEADER_FIELDS) + 1  # Prompt body is the final field.


def _is_enter(key: Any) -> bool:
    return key in (curses.KEY_ENTER, "\r", 13)


def _safe_addstr(screen: Any, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = screen.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width:
        return
    try:
        screen.addnstr(y, x, text, max(0, width - x - 1), attr)
    except curses.error:
        pass


def _field_editor(screen: Any, label: str, current: str, maximum: int = 2048) -> str | None:
    """Edit a header value; Escape cancels the edit and Ctrl+C exits the composer."""
    value = list(current)
    cursor = len(value)
    height, width = screen.getmaxyx()
    row = height - 2
    screen.timeout(-1)
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    while True:
        screen.move(row, 0)
        screen.clrtoeol()
        prefix = f"{label}: "
        _safe_addstr(screen, row, 2, prefix + "".join(value), curses.A_BOLD)
        screen.move(row, min(width - 2, 2 + len(prefix) + cursor))
        screen.refresh()
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if key in ("\x1b", 27):
            return None
        if key in ("\x03", 3):
            raise KeyboardInterrupt
        if _is_enter(key):
            result = "".join(value).strip()
            return result or current
        if key in (curses.KEY_LEFT,):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_RIGHT,):
            cursor = min(len(value), cursor + 1)
        elif key == curses.KEY_HOME:
            cursor = 0
        elif key == curses.KEY_END:
            cursor = len(value)
        elif key in (curses.KEY_BACKSPACE, 127, 8, "\x7f", "\b"):
            if cursor:
                del value[cursor - 1]
                cursor -= 1
        elif key == curses.KEY_DC:
            if cursor < len(value):
                del value[cursor]
        elif isinstance(key, str) and key.isprintable() and len(value) < maximum:
            value.insert(cursor, key)
            cursor += 1


def _draw_menu(
    screen: Any,
    title: str,
    options: list[tuple[str, str]],
    selected: int,
    note: str = "",
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    _safe_addstr(screen, 0, 2, "Compose scheduled Codex prompt", curses.A_BOLD)
    top = 3
    _safe_addstr(screen, top, 2, title, curses.A_BOLD)
    if not options:
        _safe_addstr(screen, top + 2, 4, "No choices reported by this Codex CLI.")
    else:
        visible = max(1, height - top - 5)
        first = max(0, min(selected - visible + 1, len(options) - visible))
        first = max(0, first)
        for offset, (label, detail) in enumerate(options[first : first + visible]):
            index = first + offset
            attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
            suffix = f"  {detail}" if detail else ""
            _safe_addstr(screen, top + 2 + offset, 4, f"{label}{suffix}", attr)
    _safe_addstr(screen, height - 2, 2, note, curses.A_DIM)
    _safe_addstr(screen, height - 1, 2, "↑/↓: select  Enter: choose  Esc: cancel", curses.A_DIM)
    screen.refresh()


def _choose(
    screen: Any, title: str, options: list[tuple[str, str]], selected: int = 0
) -> int | None:
    """Show a keyboard selection menu and return its chosen option index."""
    if not options:
        return None
    selected = max(0, min(selected, len(options) - 1))
    screen.timeout(-1)
    while True:
        _draw_menu(screen, title, options, selected)
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if key in ("\x1b", 27):
            return None
        if key in ("\x03", 3):
            raise KeyboardInterrupt
        if key in (curses.KEY_UP,):
            selected = (selected - 1) % len(options)
        elif key in (curses.KEY_DOWN,):
            selected = (selected + 1) % len(options)
        elif key in (curses.KEY_HOME,):
            selected = 0
        elif key in (curses.KEY_END,):
            selected = len(options) - 1
        elif _is_enter(key):
            return selected


def _reasoning_options(model: dict[str, Any]) -> list[str]:
    reported = model.get("supportedReasoningEfforts") or []
    options = [
        item.get("reasoningEffort")
        for item in reported
        if isinstance(item, dict) and isinstance(item.get("reasoningEffort"), str)
    ]
    if not options and isinstance(model.get("defaultReasoningEffort"), str):
        options = [model["defaultReasoningEffort"]]
    return options or [DEFAULT_EFFORT]


def _format_datetime(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def _draw(
    screen: Any,
    scheduled: dt.datetime,
    model: str,
    effort: str,
    directory: str,
    lines: list[str],
    cursor_y: int,
    cursor_x: int,
    active: int,
    message: str,
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    try:
        curses.curs_set(1 if active == len(HEADER_FIELDS) else 0)
    except curses.error:
        pass
    _safe_addstr(screen, 0, 2, "Compose scheduled Codex prompt", curses.A_BOLD)
    _safe_addstr(screen, 1, 2, "From: codex-scheduler")
    fields = (_format_datetime(scheduled), model, effort, directory)
    for index, (label, value) in enumerate(zip(HEADER_FIELDS, fields)):
        attr = curses.A_REVERSE if active == index else curses.A_NORMAL
        _safe_addstr(screen, index + 2, 2, f"{label + ':':<12} {value}", attr)

    separator = len(HEADER_FIELDS) + 2
    body_title = separator + 1
    body_top = body_title + 1
    _safe_addstr(screen, separator, 2, "-" * max(1, width - 5), curses.A_DIM)
    _safe_addstr(
        screen,
        body_title,
        2,
        "Prompt:",
        curses.A_REVERSE if active == len(HEADER_FIELDS) else curses.A_NORMAL,
    )

    body_bottom = max(body_top + 1, height - 3)
    body_height = body_bottom - body_top
    first_line = max(0, min(cursor_y - body_height + 1, len(lines) - body_height))
    first_line = max(0, first_line)
    body_width = max(1, width - 5)
    for screen_offset in range(body_height):
        line_index = first_line + screen_offset
        if line_index >= len(lines):
            break
        _safe_addstr(screen, body_top + screen_offset, 3, lines[line_index][:body_width])
    _safe_addstr(screen, height - 2, 2, message, curses.A_DIM)
    controls = (
        "Tab/Shift+Tab switch | arrows move | Ctrl+J line | Ctrl+S save | Esc/Ctrl+C quit"
        if active == len(HEADER_FIELDS)
        else "Tab/Shift+Tab or ↑/↓ move | Enter edit/select | Ctrl+S save | Esc/Ctrl+C quit"
    )
    _safe_addstr(screen, height - 1, 2, controls, curses.A_DIM)
    if active == len(HEADER_FIELDS):
        y = body_top + cursor_y - first_line
        if body_top <= y < body_bottom:
            try:
                screen.move(y, min(width - 2, 3 + cursor_x))
            except curses.error:
                pass
    screen.refresh()


def _compose(
    screen: Any,
    default_directory: str,
    models: list[dict[str, Any]],
) -> tuple[float, str, str, str, str] | None:
    screen.keypad(True)
    curses.nonl()
    try:
        curses.curs_set(0)
    except curses.error:
        pass

    scheduled = dt.datetime.now().replace(second=0, microsecond=0) + dt.timedelta(minutes=10)
    model_ids = [info["id"] for info in models]
    default_index = next(
        (index for index, info in enumerate(models) if info.get("isDefault")),
        next((index for index, model_id in enumerate(model_ids) if model_id == DEFAULT_MODEL), 0),
    )
    model_index = default_index if models else -1
    model_info = models[model_index] if model_index >= 0 else {}
    model = model_info.get("id", DEFAULT_MODEL)
    effort_options = _reasoning_options(model_info)
    effort = model_info.get("defaultReasoningEffort") or (
        DEFAULT_EFFORT if DEFAULT_EFFORT in effort_options else effort_options[0]
    )
    directory = default_directory
    lines = [""]
    cursor_y = cursor_x = active = 0
    message = "Times are local. Header ↑/↓ changes fields; prompt arrows move within the text."
    screen.timeout(100)
    repeat_direction = 0
    repeat_started = 0.0
    repeat_last = 0.0

    while True:
        screen.timeout(100)
        _draw(
            screen,
            scheduled,
            model,
            effort,
            directory,
            lines,
            cursor_y,
            cursor_x,
            active,
            message,
        )
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if key == -1:
            continue
        if key in ("\x13", 19):  # Ctrl+S
            prompt = "\n".join(lines).strip()
            if not prompt:
                message = "Write a prompt before scheduling."
                continue
            resolved_directory = str(Path(directory).expanduser().resolve())
            if not Path(resolved_directory).is_dir():
                message = f"Directory does not exist: {resolved_directory}"
                continue
            timestamp = scheduled.astimezone().timestamp()
            return timestamp, model, effort, resolved_directory, prompt
        if key in ("\x03", 3):  # Ctrl+C
            return None
        if key in ("\x1b", 27):  # Escape
            return None
        if key in ("\t", 9):  # Tab
            active = (active + 1) % FIELD_COUNT
            message = ""
            repeat_direction = 0
            continue
        if key == getattr(curses, "KEY_BTAB", -1):  # Shift+Tab
            active = (active - 1) % FIELD_COUNT
            message = ""
            repeat_direction = 0
            continue

        if active < len(HEADER_FIELDS) and key in (curses.KEY_UP, curses.KEY_DOWN):
            active = (active - 1 if key == curses.KEY_UP else active + 1) % FIELD_COUNT
            message = ""
            repeat_direction = 0
            continue

        if active < len(HEADER_FIELDS):
            if active == 0 and key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                direction = -1 if key == curses.KEY_LEFT else 1
                now = time.monotonic()
                if direction != repeat_direction or now - repeat_last > 0.35:
                    repeat_direction = direction
                    repeat_started = now
                repeat_last = now
                held_for = now - repeat_started
                if held_for < 0.45:
                    multiplier = 1
                elif held_for < 1.0:
                    multiplier = 2
                elif held_for < 1.8:
                    multiplier = 5
                else:
                    multiplier = 12
                scheduled += dt.timedelta(minutes=direction * 5 * multiplier)
                message = "Date adjusted in five-minute steps; holding the arrow speeds up."
                continue
            repeat_direction = 0
            if _is_enter(key):
                if active == 0:
                    edited = _field_editor(
                        screen, "Send at (YYYY-MM-DD HH:MM)", _format_datetime(scheduled), 32
                    )
                    if edited is not None:
                        try:
                            scheduled = dt.datetime.strptime(edited, "%Y-%m-%d %H:%M")
                            message = "Send time updated."
                        except ValueError:
                            message = "Use date format YYYY-MM-DD HH:MM."
                elif active == 1:
                    selected = _choose(
                        screen,
                        "Choose a model from the Codex CLI catalog",
                        [(info["id"], info.get("description", "")) for info in models],
                        model_index,
                    )
                    if selected is not None:
                        model_index = selected
                        model_info = models[model_index]
                        model = model_info["id"]
                        effort_options = _reasoning_options(model_info)
                        effort = model_info.get("defaultReasoningEffort") or (
                            DEFAULT_EFFORT
                            if DEFAULT_EFFORT in effort_options
                            else effort_options[0]
                        )
                        message = f"Model set to {model}."
                elif active == 2:
                    effort_options = _reasoning_options(model_info)
                    options = [(option, "") for option in effort_options]
                    selected = _choose(
                        screen,
                        f"Choose reasoning level for {model}",
                        options,
                        effort_options.index(effort) if effort in effort_options else 0,
                    )
                    if selected is not None:
                        effort = effort_options[selected]
                        message = f"Reasoning set to {effort}."
                else:
                    edited = _field_editor(screen, "Directory", directory)
                    if edited is not None:
                        directory = edited
                        message = "Directory updated."
                continue
            continue

        if _is_enter(key) or key in ("\n", 10):
            lines.insert(cursor_y + 1, lines[cursor_y][cursor_x:])
            lines[cursor_y] = lines[cursor_y][:cursor_x]
            cursor_y += 1
            cursor_x = 0
            continue
        line = lines[cursor_y]
        if key == curses.KEY_LEFT:
            if cursor_x:
                cursor_x -= 1
            elif cursor_y:
                cursor_y -= 1
                cursor_x = len(lines[cursor_y])
        elif key == curses.KEY_RIGHT:
            if cursor_x < len(line):
                cursor_x += 1
            elif cursor_y + 1 < len(lines):
                cursor_y += 1
                cursor_x = 0
        elif key == curses.KEY_UP and cursor_y:
            cursor_y -= 1
            cursor_x = min(cursor_x, len(lines[cursor_y]))
        elif key == curses.KEY_DOWN and cursor_y + 1 < len(lines):
            cursor_y += 1
            cursor_x = min(cursor_x, len(lines[cursor_y]))
        elif key == curses.KEY_HOME:
            cursor_x = 0
        elif key == curses.KEY_END:
            cursor_x = len(line)
        elif key in (curses.KEY_BACKSPACE, 127, 8, "\x7f", "\b"):
            if cursor_x:
                lines[cursor_y] = line[: cursor_x - 1] + line[cursor_x:]
                cursor_x -= 1
            elif cursor_y:
                cursor_x = len(lines[cursor_y - 1])
                lines[cursor_y - 1] += lines.pop(cursor_y)
                cursor_y -= 1
        elif key == curses.KEY_DC:
            if cursor_x < len(line):
                lines[cursor_y] = line[:cursor_x] + line[cursor_x + 1 :]
            elif cursor_y + 1 < len(lines):
                lines[cursor_y] += lines.pop(cursor_y + 1)
        elif isinstance(key, str) and key.isprintable():
            lines[cursor_y] = line[:cursor_x] + key + line[cursor_x:]
            cursor_x += len(key)
        message = ""


def run_schedule_composer(executable: str | None = None) -> int:
    executable = executable or shutil.which("codex")
    if not executable:
        print("Could not find the Codex CLI. Install it or pass --codex-bin.")
        return 1
    try:
        with CodexServer(executable) as server:
            models = server.model_catalog()
        if not models:
            print("Codex did not report any available models.")
            return 1
        result = curses.wrapper(_compose, os.getcwd(), models)
    except KeyboardInterrupt:
        print("\nPrompt scheduling cancelled.")
        return 0
    except (FileNotFoundError, OSError, RuntimeError, TimeoutError) as exc:
        print(f"Could not load the Codex model list: {exc}")
        return 1
    if result is None:
        print("Prompt scheduling cancelled.")
        return 0
    scheduled_at, model, effort, directory, prompt = result
    job_id = PromptSchedule().add(scheduled_at, directory, model, effort, prompt)
    print(f"Scheduled prompt {job_id} for {format_schedule_time(scheduled_at)}")
    print("Install and start the codex-scheduler service to send it when due.")
    return 0
