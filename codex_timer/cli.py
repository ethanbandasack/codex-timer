"""Command-line entry points for Codex Timer."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from .app_server import DEFAULT_EFFORT, DEFAULT_MODEL, CodexServer
from .charts import ChartDependencyError, default_export_path, export_chart
from .histogram import render_histogram, render_hourly_histogram
from .history import HistoryStore, capture_history
from .schedule_tui import run_schedule_composer
from .scheduler import PromptSchedule, format_schedule_time, parse_schedule_time
from .tui import run_terminal_app
from .usage import print_status, watch


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Schedule Codex prompts and monitor account reset timers."
    )
    parser.add_argument("--codex-bin", help="Codex CLI executable (defaults to PATH).")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("tui", help="Open the full-screen terminal app (also the default).")
    commands.add_parser("schedule-ui", help="Open the scheduled prompt composer.")

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

    schedule = commands.add_parser("schedule", help="Schedule a prompt in a Codex chat.")
    schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True)
    add = schedule_commands.add_parser("add", help="Save a prompt for a future time.")
    add.add_argument(
        "--at", required=True, help="Run time in ISO format, e.g. 2026-10-02T09:30."
    )
    add.add_argument(
        "--directory", default=".", help="Chat working directory (default: current directory)."
    )
    add.add_argument("--model", default=DEFAULT_MODEL)
    add.add_argument("--effort", default=DEFAULT_EFFORT)
    add.add_argument("--prompt", required=True, help="Prompt text to send.")
    schedule_commands.add_parser("list", help="List scheduled prompts and their status.")
    cancel = schedule_commands.add_parser("cancel", help="Cancel a pending prompt by ID.")
    cancel.add_argument("job_id", type=int)
    runner = schedule_commands.add_parser("run", help="Run due prompts; keep this process open.")
    runner.add_argument("--poll-seconds", type=float, default=1.0)
    runner.add_argument("--timeout", type=float, default=3600)
    return parser


def main() -> int:
    args = make_parser().parse_args()
    if args.command in ("watch", "ping") and args.poll_seconds < 10:
        print("--poll-seconds must be at least 10", file=sys.stderr)
        return 2

    if args.command == "schedule":
        try:
            jobs = PromptSchedule()
            if args.schedule_command == "add":
                directory = Path(args.directory).expanduser().resolve()
                if not directory.is_dir():
                    print(f"Not a directory: {directory}", file=sys.stderr)
                    return 2
                if not args.prompt.strip():
                    print("--prompt cannot be empty", file=sys.stderr)
                    return 2
                scheduled_at = parse_schedule_time(args.at)
                job_id = jobs.add(
                    scheduled_at, str(directory), args.model, args.effort, args.prompt
                )
                print(f"Scheduled prompt {job_id} for {format_schedule_time(scheduled_at)}")
                return 0
            if args.schedule_command == "list":
                rows = jobs.list()
                if not rows:
                    print("No scheduled prompts.")
                for row in rows:
                    preview = " ".join(row["prompt"].split())
                    if len(preview) > 64:
                        preview = preview[:61] + "..."
                    print(
                        f"{row['id']:>4}  {format_schedule_time(row['scheduled_at'])}  "
                        f"{row['status']:<9}  {row['model']}  {row['directory']}  {preview}"
                    )
                return 0
            if args.schedule_command == "cancel":
                if jobs.cancel(args.job_id):
                    print(f"Cancelled prompt {args.job_id}.")
                    return 0
                print(f"Prompt {args.job_id} was not pending.", file=sys.stderr)
                return 1
            if args.poll_seconds <= 0 or args.timeout <= 0:
                print("--poll-seconds and --timeout must be positive", file=sys.stderr)
                return 2
            executable = args.codex_bin or shutil.which("codex")
            if not executable:
                print(
                    "Could not find the Codex CLI. Install it or pass --codex-bin.",
                    file=sys.stderr,
                )
                return 1
            recovered = jobs.recover_interrupted()
            if recovered:
                print(f"Requeued {recovered} prompt(s) interrupted by the previous runner.")
            print("Waiting for scheduled prompts. Press Ctrl+C to stop.", flush=True)
            while True:
                job = jobs.claim_due()
                if job is None:
                    time.sleep(args.poll_seconds)
                    continue
                print(
                    f"Running prompt {job['id']} in {job['directory']} with {job['model']}…",
                    flush=True,
                )
                try:
                    with CodexServer(executable) as server:
                        result = server.send_prompt(
                            job["prompt"],
                            job["directory"],
                            job["model"],
                            job["effort"],
                            args.timeout,
                        )
                    jobs.finish(job["id"], thread_id=result["thread_id"])
                    print(
                        f"Prompt {job['id']} completed; thread {result['thread_id']} "
                        "saved in Codex.",
                        flush=True,
                    )
                except (RuntimeError, TimeoutError, OSError, sqlite3.Error) as exc:
                    jobs.finish(job["id"], error=str(exc))
                    print(f"Prompt {job['id']} failed: {exc}", file=sys.stderr, flush=True)
        except (ValueError, OSError, sqlite3.Error) as exc:
            print(f"Could not manage scheduled prompts: {exc}", file=sys.stderr)
            return 1

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
    if args.command == "schedule-ui":
        return run_schedule_composer(args.codex_bin)
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

            print(f"Sending a tiny hello ping ({args.effort} effort)…", flush=True)
            used_model = server.ping(args.model, args.effort, args.timeout)
            if used_model != args.model:
                print(f"{args.model} is unavailable here; used {used_model} instead.")
            print(f"Codex replied with {used_model}; the in-memory chat is now closed.")
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


def schedule_main() -> int:
    """Entry point for the standalone codex-schedule composer."""
    return run_schedule_composer()
