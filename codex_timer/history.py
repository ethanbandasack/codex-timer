"""SQLite persistence for Codex quota snapshots and daily token activity."""

from __future__ import annotations

import datetime as dt
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
                """
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
            summary = db.execute(
                """SELECT observed_at, lifetime_tokens, peak_daily_tokens, current_streak_days,
                          longest_streak_days, longest_running_turn_sec
                   FROM token_usage_summary WHERE id = 1"""
            ).fetchone()
        return {
            "daily": [dict(row) for row in daily],
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

    def add_planned_slot(
        self,
        now: float | None = None,
        interval_seconds: int = DEFAULT_PLAN_INTERVAL_SECONDS,
    ) -> dict[str, Any]:
        if now is None:
            now = time.time()
        with self._connect() as db:
            previous = db.execute(
                "SELECT position, scheduled_at FROM planned_slots ORDER BY position DESC LIMIT 1"
            ).fetchone()
            position = previous["position"] + 1 if previous else 0
            scheduled_at = (
                previous["scheduled_at"] + interval_seconds if previous else now + interval_seconds
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
                "SELECT position FROM planned_slots WHERE id = ?", (slot_id,)
            ).fetchone()
            if slot is None:
                return 0
            cursor = db.execute(
                "UPDATE planned_slots SET scheduled_at = scheduled_at + ? WHERE position >= ?",
                (offset_seconds, slot["position"]),
            )
        return cursor.rowcount

    def delete_planned_slot(self, slot_id: int) -> bool:
        with self._connect() as db:
            slot = db.execute(
                "SELECT position FROM planned_slots WHERE id = ?", (slot_id,)
            ).fetchone()
            if slot is None:
                return False
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


def capture_history(server: Any, store: HistoryStore) -> dict[str, Any]:
    """Fetch quota/token data and persist whichever endpoints are available."""
    limits = server.rate_limits()
    observed_at = time.time()
    store.record_quota_snapshot(limits, observed_at)
    try:
        token_usage = server.token_usage()
    except (RuntimeError, TimeoutError, OSError):
        return limits
    store.record_token_activity(token_usage, observed_at)
    return limits
