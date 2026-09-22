"""Formatting and monitoring for Codex account reset windows."""

from __future__ import annotations

import datetime as dt
import math
import subprocess
import sys
import time
from typing import Any, Callable


def window_rows(limits: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    rows = []
    for key, label in (("primary", "5-hour"), ("secondary", "weekly")):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        duration = window.get("windowDurationMins")
        if duration == 300:
            label = "5-hour"
        elif duration == 10080:
            label = "weekly"
        elif duration:
            label = f"{duration}-minute"
        rows.append((label, window))
    return rows


def local_time(timestamp: float | None) -> str:
    if timestamp is None:
        return "unknown"
    return dt.datetime.fromtimestamp(timestamp).astimezone().strftime("%a %d %b %H:%M:%S %Z")


def remaining_text(timestamp: float | None, now: float | None = None) -> str:
    if timestamp is None:
        return "unknown"
    seconds = max(0, math.ceil(timestamp - (time.time() if now is None else now)))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02}h {minutes:02}m"
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def reset_map(limits: dict[str, Any]) -> dict[str, int | float]:
    return {
        label: window["resetsAt"]
        for label, window in window_rows(limits)
        if isinstance(window.get("resetsAt"), (int, float))
    }


def print_status(limits: dict[str, Any]) -> None:
    if not limits:
        print("Codex usage data is unavailable. Check that Codex is signed in with ChatGPT.")
        return
    plan = limits.get("planType")
    print(f"Codex usage ({plan} plan)" if plan else "Codex usage")
    rows = window_rows(limits)
    if not rows:
        print("  No reset windows were returned.")
    for label, window in rows:
        used = window.get("usedPercent")
        used_text = f"{used:g}% used" if isinstance(used, (int, float)) else "usage unavailable"
        reset = window.get("resetsAt")
        print(
            f"  {label}: {used_text}; resets {local_time(reset)} ({remaining_text(reset)} remaining)"
        )
    if limits.get("rateLimitReachedType"):
        print(f"  Limit state: {limits['rateLimitReachedType']}")


def short_status(limits: dict[str, Any], now: float) -> str:
    rows = window_rows(limits)
    if not rows:
        return "Codex reset data unavailable"
    pieces = []
    for label, window in rows:
        reset = window.get("resetsAt")
        used = window.get("usedPercent")
        used_text = f"{used:g}%" if isinstance(used, (int, float)) else "?%"
        pieces.append(
            f"{label} {remaining_text(reset, now)} left ({used_text}, {local_time(reset)})"
        )
    return " | ".join(pieces)


def notify_desktop(title: str, message: str) -> bool:
    if sys.platform != "darwin":
        return False
    escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
    escaped_message = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{escaped_message}" with title "{escaped_title}"'
    result = subprocess.run(
        ["osascript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def announce_reset(label: str, *, terminal_bell: bool = True) -> None:
    message = f"Codex {label} usage window reset"
    print(f"\n{message}.", flush=True)
    if not notify_desktop("Codex Timer", message) and terminal_bell:
        print("\a", end="", flush=True)


def watch(
    server: Any,
    initial_limits: dict[str, Any],
    poll_seconds: int,
    notify: bool,
    refresh: Callable[[], dict[str, Any]] | None = None,
) -> None:
    limits = initial_limits
    previous_resets = reset_map(limits)
    next_poll = time.monotonic() + poll_seconds
    interactive = sys.stdout.isatty()
    if not interactive:
        print("Watching Codex reset windows. Ctrl-C to stop.")
        print_status(limits)

    try:
        while True:
            now = time.time()
            if time.monotonic() >= next_poll:
                try:
                    refreshed = refresh() if refresh else server.rate_limits()
                    current_resets = reset_map(refreshed)
                    for label, previous in previous_resets.items():
                        current = current_resets.get(label)
                        if current and current > previous and previous <= now + poll_seconds:
                            if notify:
                                announce_reset(label)
                            else:
                                print(f"\nCodex {label} usage window reset.", flush=True)
                    limits = refreshed
                    previous_resets = current_resets
                except (RuntimeError, TimeoutError, OSError) as exc:
                    print(f"\nCould not refresh usage data: {exc}", file=sys.stderr)
                next_poll = time.monotonic() + poll_seconds

            if interactive:
                print(f"\r{short_status(limits, now)}   ", end="", flush=True)
                time.sleep(1)
            else:
                time.sleep(max(0.1, next_poll - time.monotonic()))
                print(short_status(limits, time.time()), flush=True)
    except KeyboardInterrupt:
        if interactive:
            print()
        print("Stopped watching.")
