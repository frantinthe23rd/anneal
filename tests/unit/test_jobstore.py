"""jobstore.py — the durability layer that survives a backend restart.

The behaviour that matters is not "does sqlite work" but the three policies
encoded on top of it: what is still worth replaying, how a caller's original
task id keeps working after a replay gave the job a new one, and what prune()
actually removes.

prune() is defined and never called from anywhere in the tree (issue #27). It
is tested here on the assumption that it is meant to be wired up, and the tests
record exactly what it does and does not clean.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

import jobstore
from jobstore import JobStore


class StoreCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix="anneal-jobs-", suffix=".db")
        os.close(fd)
        os.unlink(self.path)                        # let sqlite create it
        self.store = JobStore(self.path)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def age(self, task_id, seconds):
        """Backdate a row, so the age policies can be tested without waiting."""
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE jobs SET created_at = created_at - ?, updated_at = updated_at - ?"
                    " WHERE task_id = ?", (seconds, seconds, task_id))
        raw.commit()
        raw.close()


class TestLifecycle(StoreCase):
    def test_a_recorded_job_is_pending_with_its_payload(self):
        self.store.record("t1", {"prompt": "a brief", "audio_duration": 90})
        self.assertEqual(self.store.pending(),
                         [("t1", {"prompt": "a brief", "audio_duration": 90}, 0)])

    def test_completing_takes_it_out_of_the_replay_set(self):
        self.store.record("t1", {})
        self.store.complete("t1")
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.stats(), {"done": 1})

    def test_failed_and_abandoned_are_terminal_too(self):
        self.store.record("t1", {})
        self.store.record("t2", {})
        self.store.complete("t1", "failed")
        self.store.abandon("t2")
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.stats(), {"abandoned": 1, "failed": 1})

    def test_re_recording_keeps_the_original_created_at_and_attempts(self):
        self.store.record("t1", {"v": 1})
        self.store.bump_attempt("t1")
        self.age("t1", 60)
        before = sqlite3.connect(self.path).execute(
            "SELECT created_at FROM jobs WHERE task_id = 't1'").fetchone()[0]
        self.store.record("t1", {"v": 2})
        after = sqlite3.connect(self.path).execute(
            "SELECT created_at, attempts FROM jobs WHERE task_id = 't1'").fetchone()
        self.assertEqual(after[0], before)          # not reset to now
        self.assertEqual(after[1], 1)               # attempts carried over
        self.assertEqual(self.store.payload_for("t1"), {"v": 2})

    def test_pending_is_oldest_first(self):
        for tid in ("a", "b", "c"):
            self.store.record(tid, {})
        self.age("a", 300)
        self.age("b", 100)
        self.assertEqual([t for t, _, _ in self.store.pending()], ["a", "b", "c"])

    def test_payload_for_unknown_and_corrupt(self):
        self.assertIsNone(self.store.payload_for("nope"))
        self.store.record("t1", {})
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE jobs SET payload = '{oops' WHERE task_id = 't1'")
        raw.commit()
        raw.close()
        self.assertIsNone(self.store.payload_for("t1"))

    def test_stats_counts_by_state(self):
        self.store.record("t1", {})
        self.store.record("t2", {})
        self.store.complete("t2")
        self.assertEqual(self.store.stats(), {"pending": 1, "done": 1})


class TestReplayPolicy(StoreCase):
    def test_a_job_older_than_the_replay_window_is_not_replayed(self):
        self.store.record("stale", {})
        self.age("stale", jobstore.MAX_REPLAY_AGE_SECONDS + 60)
        self.assertEqual(self.store.pending(), [])

    def test_a_job_just_inside_the_window_still_is(self):
        self.store.record("fresh", {})
        self.age("fresh", jobstore.MAX_REPLAY_AGE_SECONDS - 60)
        self.assertEqual([t for t, _, _ in self.store.pending()], ["fresh"])

    def test_a_job_that_keeps_dying_is_given_up_on(self):
        self.store.record("t1", {})
        for attempt in range(jobstore.MAX_ATTEMPTS - 1):
            self.store.bump_attempt("t1")
            self.assertEqual([t for t, _, _ in self.store.pending()], ["t1"],
                             "gave up after %d attempts" % (attempt + 1))
        self.store.bump_attempt("t1")               # now at MAX_ATTEMPTS
        self.assertEqual(self.store.pending(), [])

    def test_pending_reports_the_attempt_count(self):
        self.store.record("t1", {})
        self.store.bump_attempt("t1")
        self.assertEqual(self.store.pending()[0][2], 1)

    def test_an_unparseable_payload_is_abandoned_rather_than_retried_forever(self):
        self.store.record("t1", {})
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE jobs SET payload = 'not json' WHERE task_id = 't1'")
        raw.commit()
        raw.close()
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.stats(), {"abandoned": 1})


class TestIdTranslation(StoreCase):
    """A replayed job gets a new id from the backend. The caller never learns
    that, so both directions of the mapping have to hold."""

    def test_unknown_ids_translate_to_themselves(self):
        self.assertEqual(self.store.to_current("never-seen"), "never-seen")
        self.assertEqual(self.store.to_original("never-seen"), "never-seen")

    def test_a_full_restart_cycle(self):
        original = "task-issued-before-the-crash"
        self.store.record(original, {"prompt": "a brief"})

        # Backend restarts; the replay resubmits and gets a different id back.
        replayed = "task-handed-out-after"
        self.store.set_alias(original, replayed)
        self.store.bump_attempt(original)

        # A poll for the original must reach the replayed job...
        self.assertEqual(self.store.to_current(original), replayed)
        # ...and the answer must come back wearing the caller's own id.
        self.assertEqual(self.store.to_original(replayed), original)
        # The payload is still reachable under the id it was recorded with.
        self.assertEqual(self.store.payload_for(original), {"prompt": "a brief"})

    def test_a_second_replay_repoints_the_alias(self):
        self.store.set_alias("orig", "first")
        self.store.set_alias("orig", "second")
        self.assertEqual(self.store.to_current("orig"), "second")
        self.assertEqual(self.store.to_original("second"), "orig")
        # `original` is the primary key, so the row is replaced rather than
        # added to: there is no stale second mapping to trip over.
        self.assertEqual(self.store.to_original("first"), "first")

    def test_translation_survives_reopening_the_database(self):
        self.store.set_alias("orig", "current")
        reopened = JobStore(self.path)
        self.assertEqual(reopened.to_current("orig"), "current")
        self.assertEqual(reopened.to_original("current"), "orig")


class TestSavedPaths(StoreCase):
    def test_round_trip(self):
        self.store.set_saved("t1", ["/a/one.flac", "/a/two.flac"])
        self.assertEqual(self.store.get_saved("t1"), ["/a/one.flac", "/a/two.flac"])

    def test_unknown_returns_none_not_an_empty_list(self):
        # The caller distinguishes "never saved" from "saved nothing".
        self.assertIsNone(self.store.get_saved("t1"))

    def test_overwriting_replaces_rather_than_appends(self):
        self.store.set_saved("t1", ["/a/one.flac"])
        self.store.set_saved("t1", ["/a/two.flac"])
        self.assertEqual(self.store.get_saved("t1"), ["/a/two.flac"])

    def test_corrupt_row_reads_as_none(self):
        self.store.set_saved("t1", ["/a/one.flac"])
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE saved SET paths = 'not json' WHERE task_id = 't1'")
        raw.commit()
        raw.close()
        self.assertIsNone(self.store.get_saved("t1"))


class TestPrune(StoreCase):
    """prune() has no caller anywhere in the tree. These tests pin down what it
    would do if wired up — including what it leaves behind."""

    def rows(self, table):
        raw = sqlite3.connect(self.path)
        n = raw.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        raw.close()
        return n

    def test_removes_old_terminal_jobs(self):
        for tid, state in [("d", "done"), ("f", "failed"), ("a", "abandoned")]:
            self.store.record(tid, {})
            self.store.complete(tid, state)
            self.age(tid, 30 * 24 * 3600)
        self.assertEqual(self.rows("jobs"), 3)
        self.store.prune()
        self.assertEqual(self.rows("jobs"), 0)

    def test_keeps_recent_terminal_jobs(self):
        self.store.record("d", {})
        self.store.complete("d")
        self.store.prune()
        self.assertEqual(self.rows("jobs"), 1)

    def test_never_removes_a_pending_job_however_old(self):
        self.store.record("p", {})
        self.age("p", 365 * 24 * 3600)
        self.store.prune()
        self.assertEqual(self.rows("jobs"), 1)
        self.assertEqual(self.store.stats(), {"pending": 1})

    def test_the_cutoff_is_the_argument(self):
        self.store.record("d", {})
        self.store.complete("d")
        self.age("d", 3600)
        self.store.prune(older_than_seconds=7200)
        self.assertEqual(self.rows("jobs"), 1)
        self.store.prune(older_than_seconds=60)
        self.assertEqual(self.rows("jobs"), 0)

    def test_prune_does_not_reach_the_alias_and_saved_tables(self):
        # Issue #27: aliases and saved rows outlive the job they belong to, so
        # those two tables grow without bound. Asserted so that the day it is
        # fixed, this test says so.
        self.store.record("orig", {})
        self.store.set_alias("orig", "current")
        self.store.set_saved("orig", ["/a/one.flac"])
        self.store.complete("orig")
        self.age("orig", 30 * 24 * 3600)
        self.store.prune()
        self.assertEqual(self.rows("jobs"), 0)
        self.assertEqual(self.rows("aliases"), 1)
        self.assertEqual(self.rows("saved"), 1)


class TestConcurrency(StoreCase):
    def test_writes_from_several_threads_all_land(self):
        # The store is shared by the proxy threads and the replay thread, so the
        # single connection is guarded by a lock rather than opened per thread.
        import threading
        errors = []

        def worker(base):
            try:
                for i in range(25):
                    self.store.record("%s-%d" % (base, i), {"i": i})
                    self.store.set_alias("%s-%d" % (base, i), "c-%s-%d" % (base, i))
            except Exception as exc:                # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=("w%d" % n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.pending()), 100)


class TestSchemaConstants(unittest.TestCase):
    def test_the_policy_numbers_are_what_the_docstring_claims(self):
        self.assertEqual(jobstore.MAX_REPLAY_AGE_SECONDS, 6 * 3600)
        self.assertEqual(jobstore.MAX_ATTEMPTS, 3)

    def test_schema_is_idempotent(self):
        fd, path = tempfile.mkstemp(prefix="anneal-jobs-", suffix=".db")
        os.close(fd)
        os.unlink(path)
        try:
            JobStore(path).record("t1", {})
            JobStore(path)                          # reopening must not wipe or fail
            self.assertEqual(JobStore(path).stats(), {"pending": 1})
        finally:
            os.path.exists(path) and os.unlink(path)


if __name__ == "__main__":
    unittest.main()
