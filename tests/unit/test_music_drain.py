#!/usr/bin/env python3
"""Collect finished music before the backend that holds it is stopped.

A job is only marked done, and its audio only copied out of the backend's
prunable cache, when a client polls /query_result. ACE-Step keeps results in
memory, so if the backend stops before anyone polls — the 600 s idle unload, or
an image request evicting it — the result is gone. The job stays `pending` for
ever, the caller polls an id the restarted backend has never heard of and gets
an empty list, and the audio sits orphaned in a cache the backend prunes.

Measured twice in one session, both times costing a cold start plus minutes of
generation. The second time it destroyed a measurement I was running.

The fix is to stop depending on someone polling at the right moment: drain
finished work *before* the backend goes, and periodically while it is up.
"""

import json
import os
import shutil
import tempfile
import unittest

from tests.context import REPO_ROOT  # noqa: F401

import jobstore
import supervisor


class DrainCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = jobstore.JobStore(os.path.join(self.tmp, "jobs.db"))
        self.asked = []
        self.persisted = []

    def backend(self, rows):
        """Stand in for the backend's /query_result."""
        def call(ids):
            self.asked.append(list(ids))
            return [r for r in rows if r["task_id"] in ids]
        return call

    def drain(self, rows):
        return supervisor.drain_music_results(
            store=self.store, ask=self.backend(rows),
            persist=lambda tid, takes: self.persisted.append((tid, takes)) or True)

    def record(self, tid, state="pending"):
        self.store.record(tid, {"prompt": "x"})
        if state != "pending":
            self.store.complete(tid, state)


class TestDraining(DrainCase):
    def test_a_finished_job_is_marked_done(self):
        self.record("a")
        self.drain([{"task_id": "a", "status": 1, "result": "[]"}])
        self.assertEqual(self.store.stats().get("done"), 1)

    def test_its_audio_is_persisted_out_of_the_prunable_cache(self):
        """The whole point: the backend's cache is temporary, and the only copy
        of a good take must not live in it."""
        self.record("a")
        takes = [{"file": "/v1/audio?path=%2Ftmp%2Fx.flac"}]
        self.drain([{"task_id": "a", "status": 1, "result": json.dumps(takes)}])
        self.assertEqual(len(self.persisted), 1)
        self.assertEqual(self.persisted[0][0], "a")

    def test_a_failed_job_is_recorded_as_failed_not_left_pending(self):
        self.record("a")
        self.drain([{"task_id": "a", "status": 2, "result": "boom"}])
        self.assertEqual(self.store.stats().get("failed"), 1)

    def test_a_running_job_is_left_alone(self):
        self.record("a")
        self.drain([{"task_id": "a", "status": 0}])
        self.assertEqual(self.store.stats().get("pending"), 1)
        self.assertFalse(self.persisted)

    def test_nothing_pending_means_the_backend_is_not_asked(self):
        """Draining runs on every stop, including the common case of an idle
        backend with no work. It must be free then."""
        self.drain([])
        self.assertEqual(self.asked, [])

    def test_it_asks_using_the_replayed_id(self):
        """A replayed job lives under a new id on the backend. Asking with the
        caller's original returns nothing — which is the bug, not the fix."""
        self.record("original")
        self.store.set_alias("original", "current")
        self.drain([{"task_id": "current", "status": 1, "result": "[]"}])
        self.assertEqual(self.asked, [["current"]])
        self.assertEqual(self.store.stats().get("done"), 1)

    def test_an_unreachable_backend_leaves_the_store_untouched(self):
        """Draining is best-effort. A backend already dead must not turn
        recoverable work into lost work."""
        self.record("a")
        def boom(ids):
            raise RuntimeError("connection refused")
        supervisor.drain_music_results(store=self.store, ask=boom,
                                       persist=lambda *a: True)
        self.assertEqual(self.store.stats().get("pending"), 1)

    def test_a_persist_failure_does_not_mark_the_job_done(self):
        """Marking done while the audio is still only in the prunable cache
        would lose it at the next prune, silently and for ever."""
        self.record("a")
        takes = [{"file": "/v1/audio?path=%2Ftmp%2Fx.flac"}]
        supervisor.drain_music_results(
            store=self.store, ask=self.backend(
                [{"task_id": "a", "status": 1, "result": json.dumps(takes)}]),
            persist=lambda tid, t: False)
        self.assertEqual(self.store.stats().get("pending"), 1)


class TestItRunsBeforeTheBackendGoes(unittest.TestCase):
    def test_stopping_music_drains_first(self):
        """The ordering is the fix. Draining after the process is gone asks a
        backend that no longer exists."""
        src = open(os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8").read()
        body = src[src.index("    def stop(self, reason=\"idle\"):"):]
        body = body[:body.index("\n    def ")]
        self.assertIn("drain_music_results", body)
        self.assertLess(body.index("drain_music_results"), body.index("SIGTERM"),
                        "results must be collected before the process is killed")

    def test_housekeeping_drains_periodically(self):
        """Belt and braces: a backend killed outside stop() — crash, OOM, a
        kill -9 — never reaches the hook above."""
        src = open(os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8").read()
        self.assertGreaterEqual(src.count("drain_music_results"), 3,
                                "expected the helper, the stop hook and the sweep")


if __name__ == "__main__":
    unittest.main()
