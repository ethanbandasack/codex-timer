"""Persistent scheduled prompt storage and execution."""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from pathlib import Path
from typing import Any

from .history import default_data_dir


class PromptSchedule:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_data_dir() / "history.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS prompt_jobs (
                    id INTEGER PRIMARY KEY,
                    scheduled_at REAL NOT NULL,
                    directory TEXT NOT NULL,
                    model TEXT NOT NULL,
                    effort TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    thread_id TEXT,
                    error TEXT
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS prompt_jobs_due ON prompt_jobs(status, scheduled_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def add(
        self, scheduled_at: float, directory: str, model: str, effort: str, prompt: str
    ) -> int:
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO prompt_jobs
                   (scheduled_at, directory, model, effort, prompt, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (scheduled_at, directory, model, effort, prompt, time.time()),
            )
            return int(cursor.lastrowid)

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM prompt_jobs ORDER BY scheduled_at, id"
            )]

    def cancel(self, job_id: int) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE prompt_jobs SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                (job_id,),
            )
            return cursor.rowcount == 1

    def claim_due(self, now: float | None = None) -> dict[str, Any] | None:
        now = time.time() if now is None else now
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM prompt_jobs WHERE status = 'pending' AND scheduled_at <= ? "
                "ORDER BY scheduled_at, id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            changed = db.execute(
                "UPDATE prompt_jobs SET status = 'running', started_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (now, row["id"]),
            ).rowcount
            if changed != 1:
                return None
            claimed = dict(row)
            claimed.update(status="running", started_at=now)
            return claimed

    def recover_interrupted(self) -> int:
        """Return jobs interrupted by a stopped runner to the pending queue."""
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE prompt_jobs SET status = 'pending', started_at = NULL,
                       error = 'Runner restarted before this job completed.'
                   WHERE status = 'running'"""
            )
            return cursor.rowcount

    def finish(
        self,
        job_id: int,
        thread_id: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE prompt_jobs SET status = ?, finished_at = ?, thread_id = ?, error = ?
                   WHERE id = ? AND status = 'running'""",
                ("failed" if error else "completed", time.time(), thread_id, error, job_id),
            )


def parse_schedule_time(value: str) -> float:
    """Parse ISO 8601; timezone-free input means local machine time."""
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def format_schedule_time(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="minutes")
