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
-- Which durable files a finished job produced, so repeated polls return the
-- same copies instead of writing a fresh one every time.
CREATE TABLE IF NOT EXISTS saved (
    task_id TEXT PRIMARY KEY,
    paths   TEXT NOT NULL
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

    def set_saved(self, task_id, paths):
        self._exec("INSERT OR REPLACE INTO saved (task_id, paths) VALUES (?, ?)",
                   (task_id, json.dumps(paths)))

    def get_saved(self, task_id):
        rows = self._exec("SELECT paths FROM saved WHERE task_id = ?", (task_id,), fetch=True)
        if not rows:
            return None
        try:
            return json.loads(rows[0][0])
        except ValueError:
            return None

    def payload_for(self, task_id):
        """The original request body, for naming and metadata."""
        rows = self._exec("SELECT payload FROM jobs WHERE task_id = ?", (task_id,), fetch=True)
        if not rows:
            return None
        try:
            return json.loads(rows[0][0])
        except ValueError:
            return None

    def stats(self):
        rows = self._exec("SELECT state, COUNT(*) FROM jobs GROUP BY state", fetch=True) or []
        return {state: count for state, count in rows}

    def prune(self, older_than_seconds=7 * 24 * 3600):
        """Drop settled jobs past the cutoff, and everything that hung off them.

        `aliases` and `saved` carry no timestamp of their own, so they cannot be
        aged directly — deleting from them by cutoff would clear them wholesale
        on every sweep and break every live job. They are removed by reference
        instead: a row survives exactly as long as the job it describes.

        An alias is matched on both sides. A replayed job has the caller's
        original id and the backend's new one, and the job may be recorded under
        either, so matching one column leaves half the orphans behind — and an
        alias pointing at a pruned job is worse than a leak, because it resolves
        to an id the store no longer knows.
        """
        cutoff = time.time() - older_than_seconds
        gone = "SELECT task_id FROM jobs WHERE state != 'pending' AND updated_at < ?"
        self._exec("DELETE FROM saved WHERE task_id IN (%s)" % gone, (cutoff,))
        self._exec("DELETE FROM aliases WHERE original IN (%s) OR current IN (%s)"
                   % (gone, gone), (cutoff, cutoff))
        self._exec("DELETE FROM jobs WHERE state != 'pending' AND updated_at < ?", (cutoff,))
