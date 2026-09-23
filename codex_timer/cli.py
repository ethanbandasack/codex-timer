"""Command-line entry points for Codex Timer."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

from .app_server import DEFAULT_EFFORT, DEFAULT_MODEL, CodexServer
from .charts import ChartDependencyError, default_export_path, export_chart
from .histogram import render_histogram, render_hourly_histogram
from .history import HistoryStore, capture_history
from .tui import run_terminal_app
from .usage import print_status, watch


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ping Codex and monitor account reset timers.")
    parser.add_argument("--codex-bin", help="Codex CLI executable (defaults to PATH).")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("tui", help="Open the full-screen terminal app (also the default).")

    ping = commands.add_parser("ping", help="Send a one-word hello and close the temporary chat.")
    ping.add_argument("--model", default=DEFAULT_MODEL)
    ping.add_argument("--effort", default=DEFAULT_EFFORT, choices=("low", "medium", "max"))
    ping.add_argument("--timeout", type=float, default=30)
    ping.add_argument("--watch", action="store_true", help="Keep watching after the ping.")
    ping.add_argument("--poll-seconds", type=int, default=60)
    ping.add_argument("--no-notify", action="store_true")

    commands.add_parser("status", help="Read current reset times without sending a model request.")

    history = commands.add_parser("history", help="Show the local terminal usage histogram.")
    history.add_argument("--days", type=int, default=14, help="History period (1 to 365 days).")
    history.add_argument(
        "--hourly",
        action="store_true",
        help="Show local per-hour token counts for the last 24 hours.",
    )

    export = commands.add_parser("export", help="Export local usage curves as a PNG chart.")
    export.add_argument("--days", type=int, default=30, help="History period (1 to 365 days).")
    export.add_argument(
        "--output", type=Path, help="PNG destination (defaults to app data exports)."
    )

    monitor = commands.add_parser("watch", help="Monitor reset times without pinging.")
    monitor.add_argument("--poll-seconds", type=int, default=60)
    monitor.add_argument("--no-notify", action="store_true")
    return parser


def main() -> int:
    args = make_parser().parse_args()
    if args.command in ("watch", "ping") and args.poll_seconds < 10:
        print("--poll-seconds must be at least 10", file=sys.stderr)
        return 2

    if args.command == "history":
        try:
            store = HistoryStore()
            store.record_local_session_usage()
            report = store.history(2 if args.hourly else args.days)
            width = shutil.get_terminal_size((80, 24)).columns
            lines = (
                render_hourly_histogram(report, width)
                if args.hourly
                else render_histogram(report, width)
            )
        except (OSError, sqlite3.Error) as exc:
            print(f"Could not read usage history: {exc}", file=sys.stderr)
            return 1
        print("\n".join(lines))
        print(f"\nHistory database: {store.path}")
        return 0
    if args.command == "export":
        try:
            store = HistoryStore()
            store.record_local_session_usage()
            output = export_chart(store.history(args.days), args.output or default_export_path())
        except ChartDependencyError as exc:
            print(f"Codex Timer: {exc}", file=sys.stderr)
            return 1
        except (OSError, sqlite3.Error) as exc:
            print(f"Could not export usage history: {exc}", file=sys.stderr)
            return 1
        print(f"PNG saved to {output}")
        return 0

    executable = args.codex_bin or shutil.which("codex")
    if not executable:
        print("Could not find the Codex CLI. Install it or pass --codex-bin.", file=sys.stderr)
        return 1

    try:
        history = HistoryStore()
        if args.command in (None, "tui"):
            return run_terminal_app(executable)
        with CodexServer(executable) as server:
            if args.command == "status":
                limits = capture_history(server, history)
                print_status(limits)
                print(f"Local history: {history.path}")
                return 0
            if args.command == "watch":
                limits = capture_history(server, history)
                watch(
                    server,
                    limits,
                    args.poll_seconds,
                    not args.no_notify,
                    refresh=lambda: capture_history(server, history),
                )
                return 0

            print(
                f"Sending a tiny hello ping with {args.model} ({args.effort} effort)…", flush=True
            )
            server.ping(args.model, args.effort, args.timeout)
            print("Codex replied; the in-memory chat is now closed.")
            limits = capture_history(server, history)
            print_status(limits)
            print(f"Local history: {history.path}")
            if args.watch:
                watch(
                    server,
                    limits,
                    args.poll_seconds,
                    not args.no_notify,
                    refresh=lambda: capture_history(server, history),
                )
            return 0
    except FileNotFoundError:
        print(f"Codex executable not found: {executable}", file=sys.stderr)
        return 1
    except (RuntimeError, TimeoutError, OSError, sqlite3.Error) as exc:
        print(f"Codex Timer: {exc}", file=sys.stderr)
        print("Check that Codex is signed in with your ChatGPT account.", file=sys.stderr)
        return 1
