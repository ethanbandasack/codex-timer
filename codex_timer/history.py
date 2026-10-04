"""SQLite persistence for Codex quota snapshots and daily token activity."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from .usage import window_rows

RETENTION_DAYS = 90
DEFAULT_PLAN_INTERVAL_SECONDS = 5 * 3600 + 60


def default_data_dir() -> Path:
    configured = os.environ.get("CODEX_TIMER_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Codex Timer"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home).expanduser() / "codex-timer"
    return Path.home() / ".local" / "share" / "codex-timer"


class HistoryStore:
    """Store recent rate-limit snapshots and Codex's daily token activity."""

    def __init__(self, path: Path | None = None, retention_days: int = RETENTION_DAYS) -> None:
        self.path = path or default_data_dir() / "history.sqlite3"
        self.retention_days = max(1, retention_days)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS quota_samples (
                    id INTEGER PRIMARY KEY,
                    captured_at REAL NOT NULL,
                    window TEXT NOT NULL,
                    used_percent REAL,
                    resets_at INTEGER,
                    window_duration_mins INTEGER,
                    plan_type TEXT
                );
                CREATE INDEX IF NOT EXISTS quota_samples_window_time
                    ON quota_samples(window, captured_at);

                CREATE TABLE IF NOT EXISTS daily_token_usage (
                    usage_date TEXT PRIMARY KEY,
                    tokens INTEGER NOT NULL CHECK(tokens >= 0),
                    observed_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS local_session_usage (
                    event_key TEXT PRIMARY KEY,
                    captured_at REAL NOT NULL,
                    session_id TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL CHECK(total_tokens >= 0)
                );
                CREATE INDEX IF NOT EXISTS local_session_usage_time
                    ON local_session_usage(captured_at);

                CREATE TABLE IF NOT EXISTS session_log_offsets (
                    file_key TEXT PRIMARY KEY,
                    byte_offset INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS token_usage_summary (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    observed_at REAL NOT NULL,
                    lifetime_tokens INTEGER,
                    peak_daily_tokens INTEGER,
                    current_streak_days INTEGER,
                    longest_streak_days INTEGER,
                    longest_running_turn_sec INTEGER
                );

                CREATE TABLE IF NOT EXISTS planned_slots (
                    id INTEGER PRIMARY KEY,
                    position INTEGER NOT NULL UNIQUE,
                    scheduled_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS plan_settings (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    reset_anchor_at REAL NOT NULL,
                    source_reset_at REAL,
                    interval_seconds INTEGER NOT NULL DEFAULT 18060
                );

                CREATE TABLE IF NOT EXISTS auto_ping_attempts (
                    slot_id INTEGER PRIMARY KEY,
                    scheduled_at REAL NOT NULL,
                    attempted_at REAL NOT NULL
                );
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(plan_settings)")}
            if "interval_seconds" not in columns:
                db.execute(
                    "ALTER TABLE plan_settings ADD COLUMN interval_seconds INTEGER NOT NULL "
                    f"DEFAULT {DEFAULT_PLAN_INTERVAL_SECONDS}"
                )

    def record_quota_snapshot(
        self, limits: dict[str, Any], captured_at: float | None = None
    ) -> int:
        if captured_at is None:
            captured_at = time.time()
        plan_type = limits.get("planType")
        rows = [
            (
                captured_at,
                label,
                window.get("usedPercent"),
                window.get("resetsAt"),
                window.get("windowDurationMins"),
                plan_type,
            )
            for label, window in window_rows(limits)
        ]
        if not rows:
            return 0
        with self._connect() as db:
            db.executemany(
                """INSERT INTO quota_samples
                   (captured_at, window, used_percent, resets_at, window_duration_mins, plan_type)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )
            self._prune(db, captured_at)
        return len(rows)

    def record_token_activity(self, usage: dict[str, Any], observed_at: float | None = None) -> int:
        if observed_at is None:
            observed_at = time.time()
        summary = usage.get("summary") or {}
        buckets = usage.get("dailyUsageBuckets") or []
        accepted = []
        for bucket in buckets:
            usage_date = bucket.get("startDate")
            tokens = bucket.get("tokens")
            if not isinstance(usage_date, str) or not isinstance(tokens, int) or tokens < 0:
                continue
            try:
                dt.date.fromisoformat(usage_date)
            except ValueError:
                continue
            accepted.append((usage_date, tokens, observed_at))

        with self._connect() as db:
            if accepted:
                db.executemany(
                    """INSERT INTO daily_token_usage (usage_date, tokens, observed_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(usage_date) DO UPDATE SET
                           tokens = excluded.tokens,
                           observed_at = excluded.observed_at""",
                    accepted,
                )
            if summary:
                db.execute(
                    """INSERT INTO token_usage_summary
                       (id, observed_at, lifetime_tokens, peak_daily_tokens, current_streak_days,
                        longest_streak_days, longest_running_turn_sec)
                       VALUES (1, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           observed_at = excluded.observed_at,
                           lifetime_tokens = excluded.lifetime_tokens,
                           peak_daily_tokens = excluded.peak_daily_tokens,
                           current_streak_days = excluded.current_streak_days,
                           longest_streak_days = excluded.longest_streak_days,
                           longest_running_turn_sec = excluded.longest_running_turn_sec""",
                    (
                        observed_at,
                        summary.get("lifetimeTokens"),
                        summary.get("peakDailyTokens"),
                        summary.get("currentStreakDays"),
                        summary.get("longestStreakDays"),
                        summary.get("longestRunningTurnSec"),
                    ),
                )
            self._prune(db, observed_at)
        return len(accepted)

    def record_local_session_usage(self, root: Path | None = None, now: float | None = None) -> int:
        """Import token-count metadata from rollout logs, discarding conversation content."""
        root = root or Path.home() / ".codex" / "sessions"
        now = now if now is not None else time.time()
        if not root.is_dir():
            return 0

        cutoff = now - self.retention_days * 86400
        imported = 0
        files = sorted(root.rglob("rollout-*.jsonl"))
        with self._connect() as db:
            for path in files:
                try:
                    if path.stat().st_mtime < cutoff:
                        continue
                    file_key = path.relative_to(root).as_posix()
                    state = db.execute(
                        "SELECT byte_offset FROM session_log_offsets WHERE file_key = ?",
                        (file_key,),
                    ).fetchone()
                    offset = int(state["byte_offset"]) if state else 0
                    if offset > path.stat().st_size:
                        offset = 0

                    with path.open("rb") as stream:
                        stream.seek(offset)
                        while True:
                            line_offset = stream.tell()
                            line = stream.readline()
                            if not line:
                                offset = stream.tell()
                                break
                            if not line.endswith(b"\n"):
                                offset = line_offset
                                break
                            offset = stream.tell()
                            try:
                                row = json.loads(line)
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                continue
                            if not isinstance(row, dict):
                                continue
                            payload = row.get("payload") or {}
                            if not isinstance(payload, dict):
                                continue
                            if (
                                row.get("type") != "event_msg"
                                or payload.get("type") != "token_count"
                            ):
                                continue
                            info = payload.get("info") or {}
                            if not isinstance(info, dict):
                                continue
                            usage = info.get("last_token_usage") or {}
                            if not isinstance(usage, dict):
                                continue
                            total = usage.get("total_tokens")
                            timestamp = row.get("timestamp")
                            if (
                                not isinstance(total, int)
                                or isinstance(total, bool)
                                or total < 0
                                or not isinstance(timestamp, str)
                            ):
                                continue
                            try:
                                captured_at = dt.datetime.fromisoformat(
                                    timestamp.replace("Z", "+00:00")
                                ).timestamp()
                            except (AttributeError, TypeError, ValueError):
                                continue
                            if captured_at < cutoff:
                                continue
                            usage_values = {}
                            for key in (
                                "input_tokens",
                                "cached_input_tokens",
                                "output_tokens",
                                "reasoning_output_tokens",
                            ):
                                value = usage.get(key)
                                usage_values[key] = (
                                    value
                                    if isinstance(value, int)
                                    and not isinstance(value, bool)
                                    and value >= 0
                                    else 0
                                )
                            cursor = db.execute(
                                """INSERT OR IGNORE INTO local_session_usage
                                   (event_key, captured_at, session_id, input_tokens,
                                    cached_input_tokens, output_tokens, reasoning_output_tokens,
                                    total_tokens)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    f"{file_key}:{line_offset}",
                                    captured_at,
                                    path.stem.removeprefix("rollout-"),
                                    usage_values.get("input_tokens", 0),
                                    usage_values.get("cached_input_tokens", 0),
                                    usage_values.get("output_tokens", 0),
                                    usage_values.get("reasoning_output_tokens", 0),
                                    total,
                                ),
                            )
                            imported += cursor.rowcount

                    db.execute(
                        """INSERT INTO session_log_offsets (file_key, byte_offset) VALUES (?, ?)
                           ON CONFLICT(file_key) DO UPDATE SET byte_offset = excluded.byte_offset""",
                        (file_key, offset),
                    )
                except OSError:
                    continue
            self._prune(db, now)
        return imported

    def history(self, days: int = 30) -> dict[str, Any]:
        days = min(365, max(1, days))
        today = dt.datetime.now().astimezone().date()
        start_date = (today - dt.timedelta(days=days - 1)).isoformat()
        start_time = time.time() - days * 86400
        with self._connect() as db:
            daily = db.execute(
                """SELECT usage_date, tokens, observed_at FROM daily_token_usage
                   WHERE usage_date >= ? ORDER BY usage_date""",
                (start_date,),
            ).fetchall()
            quota = db.execute(
                """SELECT captured_at, window, used_percent, resets_at, window_duration_mins
                   FROM quota_samples WHERE captured_at >= ? ORDER BY captured_at""",
                (start_time,),
            ).fetchall()
            local_tokens = db.execute(
                """SELECT captured_at, session_id, input_tokens, cached_input_tokens,
                          output_tokens, reasoning_output_tokens, total_tokens
                   FROM local_session_usage WHERE captured_at >= ? ORDER BY captured_at""",
                (start_time,),
            ).fetchall()
            summary = db.execute(
                """SELECT observed_at, lifetime_tokens, peak_daily_tokens, current_streak_days,
                          longest_streak_days, longest_running_turn_sec
                   FROM token_usage_summary WHERE id = 1"""
            ).fetchone()
        return {
            "daily": [dict(row) for row in daily],
            "local_tokens": [dict(row) for row in local_tokens],
            "quota": [dict(row) for row in quota],
            "summary": dict(summary) if summary else None,
            "days": days,
        }

    def planned_slots(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, position, scheduled_at FROM planned_slots ORDER BY position"
            ).fetchall()
        return [dict(row) for row in rows]

    def plan_anchor(self) -> float | None:
        with self._connect() as db:
            row = db.execute("SELECT reset_anchor_at FROM plan_settings WHERE id = 1").fetchone()
        return row["reset_anchor_at"] if row else None

    def plan_interval(self) -> int:
        with self._connect() as db:
            row = db.execute("SELECT interval_seconds FROM plan_settings WHERE id = 1").fetchone()
        return int(row["interval_seconds"]) if row else DEFAULT_PLAN_INTERVAL_SECONDS

    def claim_planned_ping(
        self, slot_id: int, scheduled_at: float, attempted_at: float | None = None
    ) -> bool:
        """Claim one scheduled band so retries or app restarts cannot ping it twice."""
        if attempted_at is None:
            attempted_at = time.time()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO auto_ping_attempts "
                "(slot_id, scheduled_at, attempted_at) VALUES (?, ?, ?)",
                (slot_id, scheduled_at, attempted_at),
            )
        return cursor.rowcount == 1

    def ensure_plan_anchor(self, reset_at: float | None) -> float | None:
        """Persist the server reset as the plan anchor, preserving existing bands."""
        with self._connect() as db:
            row = db.execute(
                "SELECT reset_anchor_at, source_reset_at FROM plan_settings WHERE id = 1"
            ).fetchone()
            if row:
                anchor_at = row["reset_anchor_at"]
                source_reset_at = row["source_reset_at"]
                if reset_at is not None:
                    if source_reset_at is not None:
                        anchor_at += reset_at - source_reset_at
                    anchor_at = max(anchor_at, reset_at)
                    shift = anchor_at - row["reset_anchor_at"]
                    db.execute(
                        "UPDATE plan_settings SET reset_anchor_at = ?, source_reset_at = ? "
                        "WHERE id = 1",
                        (anchor_at, reset_at),
                    )
                    if shift:
                        db.execute(
                            "UPDATE planned_slots SET scheduled_at = scheduled_at + ?", (shift,)
                        )
                return anchor_at

            bands = db.execute(
                "SELECT scheduled_at FROM planned_slots ORDER BY position LIMIT 1"
            ).fetchone()
            if bands:
                inferred_anchor = bands["scheduled_at"] - DEFAULT_PLAN_INTERVAL_SECONDS
                anchor_at = reset_at if reset_at is not None else inferred_anchor
                db.execute(
                    "UPDATE planned_slots SET scheduled_at = scheduled_at + ?",
                    (anchor_at - inferred_anchor,),
                )
            elif reset_at is not None:
                anchor_at = reset_at
            else:
                return None

            db.execute(
                "INSERT INTO plan_settings (id, reset_anchor_at, source_reset_at, interval_seconds) "
                "VALUES (1, ?, ?, ?)",
                (anchor_at, reset_at, DEFAULT_PLAN_INTERVAL_SECONDS),
            )
        return anchor_at

    def shift_plan_anchor(self, offset_seconds: float) -> float | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT reset_anchor_at, source_reset_at FROM plan_settings WHERE id = 1"
            ).fetchone()
            if row is None:
                return None
            old_anchor = row["reset_anchor_at"]
            anchor_at = old_anchor + offset_seconds
            floor = row["source_reset_at"] if row["source_reset_at"] is not None else old_anchor
            anchor_at = max(anchor_at, floor)
            shift = anchor_at - old_anchor
            db.execute("UPDATE plan_settings SET reset_anchor_at = ? WHERE id = 1", (anchor_at,))
            if shift:
                db.execute("UPDATE planned_slots SET scheduled_at = scheduled_at + ?", (shift,))
        return anchor_at

    def set_plan_anchor(self, scheduled_at: float) -> float | None:
        """Set the local reset anchor without allowing it before Codex's next reset."""
        with self._connect() as db:
            row = db.execute(
                "SELECT reset_anchor_at, source_reset_at FROM plan_settings WHERE id = 1"
            ).fetchone()
            if row is None:
                return None
            floor = (
                row["source_reset_at"]
                if row["source_reset_at"] is not None
                else row["reset_anchor_at"]
            )
            anchor_at = max(scheduled_at, floor)
            shift = anchor_at - row["reset_anchor_at"]
            db.execute("UPDATE plan_settings SET reset_anchor_at = ? WHERE id = 1", (anchor_at,))
            if shift:
                db.execute("UPDATE planned_slots SET scheduled_at = scheduled_at + ?", (shift,))
        return anchor_at

    def add_planned_slot(
        self,
        now: float | None = None,
        interval_seconds: int | None = None,
        anchor_at: float | None = None,
    ) -> dict[str, Any]:
        if now is None:
            now = time.time()
        with self._connect() as db:
            if interval_seconds is None:
                setting = db.execute(
                    "SELECT interval_seconds FROM plan_settings WHERE id = 1"
                ).fetchone()
                interval_seconds = (
                    int(setting["interval_seconds"]) if setting else DEFAULT_PLAN_INTERVAL_SECONDS
                )
            interval_seconds = max(60, int(interval_seconds))
            previous = db.execute(
                "SELECT position, scheduled_at FROM planned_slots ORDER BY position DESC LIMIT 1"
            ).fetchone()
            if anchor_at is None:
                anchor = db.execute(
                    "SELECT reset_anchor_at FROM plan_settings WHERE id = 1"
                ).fetchone()
                anchor_at = anchor["reset_anchor_at"] if anchor else None
            position = previous["position"] + 1 if previous else 0
            scheduled_at = (
                previous["scheduled_at"] + interval_seconds
                if previous
                else (anchor_at if anchor_at is not None else now) + interval_seconds
            )
            cursor = db.execute(
                "INSERT INTO planned_slots (position, scheduled_at) VALUES (?, ?)",
                (position, scheduled_at),
            )
            slot_id = cursor.lastrowid
        return {"id": slot_id, "position": position, "scheduled_at": scheduled_at}

    def shift_planned_slots(self, slot_id: int, offset_seconds: int) -> int:
        with self._connect() as db:
            slot = db.execute(
                "SELECT position, scheduled_at FROM planned_slots WHERE id = ?", (slot_id,)
            ).fetchone()
            if slot is None:
                return 0
            previous = db.execute(
                "SELECT scheduled_at FROM planned_slots WHERE position < ? "
                "ORDER BY position DESC LIMIT 1",
                (slot["position"],),
            ).fetchone()
            if previous is None:
                anchor = db.execute(
                    "SELECT reset_anchor_at FROM plan_settings WHERE id = 1"
                ).fetchone()
                previous_at = anchor["reset_anchor_at"] if anchor else None
            else:
                previous_at = previous["scheduled_at"]
            new_time = slot["scheduled_at"] + offset_seconds
            if previous_at is not None:
                new_time = max(new_time, previous_at + 60)
            actual_shift = new_time - slot["scheduled_at"]
            cursor = db.execute(
                "UPDATE planned_slots SET scheduled_at = scheduled_at + ? WHERE position >= ?",
                (actual_shift, slot["position"]),
            )
        return cursor.rowcount

    def set_planned_slot_time(self, slot_id: int, scheduled_at: float) -> float | None:
        with self._connect() as db:
            slot = db.execute(
                "SELECT position, scheduled_at FROM planned_slots WHERE id = ?", (slot_id,)
            ).fetchone()
            if slot is None:
                return None
            previous = db.execute(
                "SELECT scheduled_at FROM planned_slots WHERE position < ? "
                "ORDER BY position DESC LIMIT 1",
                (slot["position"],),
            ).fetchone()
            if previous is None:
                anchor = db.execute(
                    "SELECT reset_anchor_at FROM plan_settings WHERE id = 1"
                ).fetchone()
                previous_at = anchor["reset_anchor_at"] if anchor else None
            else:
                previous_at = previous["scheduled_at"]
            target = (
                max(scheduled_at, previous_at + 60) if previous_at is not None else scheduled_at
            )
            shift = target - slot["scheduled_at"]
            db.execute(
                "UPDATE planned_slots SET scheduled_at = scheduled_at + ? WHERE position >= ?",
                (shift, slot["position"]),
            )
        return target

    def set_plan_interval(self, interval_seconds: int) -> int:
        """Save a custom band interval and place each band at that interval from reset."""
        interval_seconds = int(interval_seconds)
        if interval_seconds < 60:
            raise ValueError("Band interval must be at least one minute")
        with self._connect() as db:
            anchor = db.execute("SELECT reset_anchor_at FROM plan_settings WHERE id = 1").fetchone()
            if anchor is None:
                raise ValueError("Cannot set a band interval before the reset anchor is available")
            db.execute(
                "UPDATE plan_settings SET interval_seconds = ? WHERE id = 1", (interval_seconds,)
            )
            slots = db.execute(
                "SELECT id, position FROM planned_slots ORDER BY position"
            ).fetchall()
            for index, slot in enumerate(slots, start=1):
                db.execute(
                    "UPDATE planned_slots SET scheduled_at = ? WHERE id = ?",
                    (anchor["reset_anchor_at"] + interval_seconds * index, slot["id"]),
                )
        return interval_seconds

    def clear_planned_slots(self) -> int:
        with self._connect() as db:
            db.execute("DELETE FROM auto_ping_attempts")
            cursor = db.execute("DELETE FROM planned_slots")
        return cursor.rowcount

    def delete_planned_slot(self, slot_id: int) -> bool:
        with self._connect() as db:
            slot = db.execute(
                "SELECT position FROM planned_slots WHERE id = ?", (slot_id,)
            ).fetchone()
            if slot is None:
                return False
            db.execute("DELETE FROM auto_ping_attempts WHERE slot_id = ?", (slot_id,))
            db.execute("DELETE FROM planned_slots WHERE id = ?", (slot_id,))
            db.execute(
                "UPDATE planned_slots SET position = -position - 1 WHERE position > ?",
                (slot["position"],),
            )
            db.execute("UPDATE planned_slots SET position = -position - 2 WHERE position < -1")
        return True

    def _prune(self, db: sqlite3.Connection, now: float) -> None:
        cutoff = now - self.retention_days * 86400
        today = dt.datetime.fromtimestamp(now).astimezone().date()
        cutoff_date = (today - dt.timedelta(days=self.retention_days)).isoformat()
        db.execute("DELETE FROM quota_samples WHERE captured_at < ?", (cutoff,))
        db.execute("DELETE FROM daily_token_usage WHERE usage_date < ?", (cutoff_date,))
        db.execute("DELETE FROM local_session_usage WHERE captured_at < ?", (cutoff,))


def capture_history(server: Any, store: HistoryStore) -> dict[str, Any]:
    """Fetch quota/token data and persist whichever endpoints are available."""
    limits = server.rate_limits()
    observed_at = time.time()
    store.record_quota_snapshot(limits, observed_at)
    try:
        store.record_local_session_usage(now=observed_at)
    except (OSError, sqlite3.Error):
        pass
    try:
        token_usage = server.token_usage()
    except (RuntimeError, TimeoutError, OSError):
        return limits
    store.record_token_activity(token_usage, observed_at)
    return limits
