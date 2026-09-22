"""Command-line entry points for Codex Timer."""

from __future__ import annotations

import argparse
import shutil
import sys

from .app_server import DEFAULT_EFFORT, DEFAULT_MODEL, CodexServer
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

    monitor = commands.add_parser("watch", help="Monitor reset times without pinging.")
    monitor.add_argument("--poll-seconds", type=int, default=60)
    monitor.add_argument("--no-notify", action="store_true")
    return parser


def main() -> int:
    args = make_parser().parse_args()
    if args.command in ("watch", "ping") and args.poll_seconds < 10:
        print("--poll-seconds must be at least 10", file=sys.stderr)
        return 2

    executable = args.codex_bin or shutil.which("codex")
    if not executable:
        print("Could not find the Codex CLI. Install it or pass --codex-bin.", file=sys.stderr)
        return 1

    try:
        if args.command in (None, "tui"):
            return run_terminal_app(executable)
        with CodexServer(executable) as server:
            if args.command == "status":
                print_status(server.rate_limits())
                return 0
            if args.command == "watch":
                watch(server, server.rate_limits(), args.poll_seconds, not args.no_notify)
                return 0

            print(f"Sending a tiny hello ping with {args.model} ({args.effort} effort)…", flush=True)
            server.ping(args.model, args.effort, args.timeout)
            print("Codex replied; the in-memory chat is now closed.")
            limits = server.rate_limits()
            print_status(limits)
            if args.watch:
                watch(server, limits, args.poll_seconds, not args.no_notify)
            return 0
    except FileNotFoundError:
        print(f"Codex executable not found: {executable}", file=sys.stderr)
        return 1
    except (RuntimeError, TimeoutError, OSError) as exc:
        print(f"Codex Timer: {exc}", file=sys.stderr)
        print("Check that Codex is signed in with your ChatGPT account.", file=sys.stderr)
        return 1
