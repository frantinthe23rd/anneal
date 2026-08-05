#!/usr/bin/env python3
"""Durable record of music jobs, so a backend restart doesn't lose work.

ACE-Step keeps its queue in memory, and Anneal stops that process routinely —
on idle, on eviction, on a manual stop. Anything submitted at the wrong moment
was simply gone; the caller's only recourse was to notice and resubmit.

This records every job the gateway hands out, and replays whatever was still
outstanding when the backend next starts. The caller keeps polling the task_id
it was originally given: the gateway maps that to whatever id the replayed job
received, in both directions, so the indirection is invisible.

sqlite because it is in the stdlib, survives a crash, and this is a handful of
rows — nothing here justifies a real database.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    task_id     TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,     -- original /release_task body, for replay
    state       TEXT NOT NULL,     -- pending | done | failed | abandoned
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
-- A replayed job gets a fresh id from the backend. Callers keep using the
-- original, so we translate between them on the way in and on the way out.
CREATE TABLE IF NOT EXISTS aliases (
    original TEXT PRIMARY KEY,
    current  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS aliases_current ON aliases(current);
"""

# A job nobody has collected in this long is not worth re-running.
MAX_REPLAY_AGE_SECONDS = 6 * 3600
# Stop replaying a job that keeps dying — it is probably the reason for the crash.
MAX_ATTEMPTS = 3


class JobStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _exec(self, sql, args=(), fetch=None):
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall() if fetch else None
            self._conn.commit()
            return rows

    # -- writing ----------------------------------------------------------
    def record(self, task_id, payload):
        now = time.time()
        self._exec(
            "INSERT OR REPLACE INTO jobs (task_id, payload, state, attempts, created_at, updated_at)"
            " VALUES (?, ?, 'pending', COALESCE((SELECT attempts FROM jobs WHERE task_id = ?), 0), "
            " COALESCE((SELECT created_at FROM jobs WHERE task_id = ?), ?), ?)",
            (task_id, json.dumps(payload), task_id, task_id, now, now),
        )

    def complete(self, task_id, state="done"):
        self._exec(
            "UPDATE jobs SET state = ?, updated_at = ? WHERE task_id = ?",
            (state, time.time(), task_id),
        )

    def bump_attempt(self, task_id):
        self._exec(
            "UPDATE jobs SET attempts = attempts + 1, updated_at = ? WHERE task_id = ?",
            (time.time(), task_id),
        )

    def abandon(self, task_id):
        self.complete(task_id, "abandoned")

    # -- aliases ----------------------------------------------------------
    def set_alias(self, original, current):
        self._exec(
            "INSERT OR REPLACE INTO aliases (original, current) VALUES (?, ?)",
            (original, current),
        )

    def to_current(self, task_id):
        rows = self._exec("SELECT current FROM aliases WHERE original = ?", (task_id,), fetch=True)
        return rows[0][0] if rows else task_id

    def to_original(self, task_id):
        rows = self._exec("SELECT original FROM aliases WHERE current = ?", (task_id,), fetch=True)
        return rows[0][0] if rows else task_id

    # -- reading ----------------------------------------------------------
    def pending(self):
        """Jobs worth replaying, oldest first."""
        cutoff = time.time() - MAX_REPLAY_AGE_SECONDS
        rows = self._exec(
            "SELECT task_id, payload, attempts FROM jobs"
            " WHERE state = 'pending' AND created_at > ? AND attempts < ?"
            " ORDER BY created_at ASC",
            (cutoff, MAX_ATTEMPTS),
            fetch=True,
        ) or []
        out = []
        for task_id, payload, attempts in rows:
            try:
                out.append((task_id, json.loads(payload), attempts))
            except ValueError:
                self.abandon(task_id)
        return out

    def stats(self):
        rows = self._exec("SELECT state, COUNT(*) FROM jobs GROUP BY state", fetch=True) or []
        return {state: count for state, count in rows}

    def prune(self, older_than_seconds=7 * 24 * 3600):
        cutoff = time.time() - older_than_seconds
        self._exec("DELETE FROM jobs WHERE state != 'pending' AND updated_at < ?", (cutoff,))
