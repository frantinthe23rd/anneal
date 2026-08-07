#!/usr/bin/env python3
"""Request and resource limits (#13).

Importing supervisor.py has side effects — it opens jobs.db and presses.db
under AIMUSIC_ROOT and resolves the roots it will serve files from — which is
why `tests/unit/__init__.py` sandboxes the environment before any app module is
imported. It binds no port at import time, so this is safe.
"""

import os
import sqlite3
import tempfile
import time
import unittest

import supervisor
from jobstore import JobStore


class PressLimitsTest(unittest.TestCase):
    """A single POST must not be able to book the machine for the rest of the day."""

    def test_modest_request_is_allowed(self):
        self.assertIsNone(supervisor.press_limits(
            {"prompt": "warm lo-fi", "tracks": 4, "duration": 90}))

    def test_default_single_track_is_allowed(self):
        self.assertIsNone(supervisor.press_limits({"prompt": "x"}))

    def test_eight_ten_minute_tracks_is_refused(self):
        """The worst case builder.py permits: 8 x 600s = 80 minutes of audio."""
        problem = supervisor.press_limits(
            {"prompt": "x", "tracks": 8, "duration": 600, "duration_max": 600})
        self.assertIsNotNone(problem)
        self.assertIn("over the", problem)

    def test_limit_uses_duration_max_not_just_duration(self):
        """builder.py plans up to duration_max, so that is what must be bounded."""
        self.assertIsNotNone(supervisor.press_limits(
            {"prompt": "x", "tracks": 8, "duration": 60, "duration_max": 600}))

    def test_track_count_is_clamped_the_same_way_builder_clamps_it(self):
        """Asking for 500 tracks is 8 tracks, not a refusal for the wrong reason."""
        self.assertIsNone(supervisor.press_limits(
            {"prompt": "x", "tracks": 500, "duration": 30, "duration_max": 30}))

    def test_overlong_prompt_is_refused(self):
        problem = supervisor.press_limits(
            {"prompt": "a" * (supervisor.MAX_PROMPT_CHARS + 1)})
        self.assertIsNotNone(problem)
        self.assertIn("character", problem)

    def test_prompt_at_the_limit_is_allowed(self):
        self.assertIsNone(supervisor.press_limits(
            {"prompt": "a" * supervisor.MAX_PROMPT_CHARS}))

    def test_garbage_numbers_do_not_raise(self):
        """A handler must answer 400, not 500, on {"tracks": "lots"}."""
        for payload in ({"prompt": "x", "tracks": "lots"},
                        {"prompt": "x", "duration": None},
                        {"prompt": "x", "duration_max": []}):
            supervisor.press_limits(payload)          # must not raise


class RequestSizeCapTest(unittest.TestCase):
    def test_cap_is_set_and_sane(self):
        self.assertGreater(supervisor.MAX_REQUEST_BYTES, 64 * 1024)
        self.assertLess(supervisor.MAX_REQUEST_BYTES, 64 * 1024 * 1024)

    def test_read_body_refuses_an_oversized_content_length(self):
        """The declared length is refused before a byte is read off the socket."""
        sent = {}

        class FakeHandler(supervisor.Handler):
            def __init__(self):                      # bypass BaseHTTPRequestHandler
                self.headers = {"Content-Length": str(supervisor.MAX_REQUEST_BYTES + 1)}
                self.close_connection = False
                self.rfile = None                    # reading it at all is the bug

            def _send_json(self, obj, status=200):
                sent["obj"], sent["status"] = obj, status

        h = FakeHandler()
        self.assertIsNone(h._read_body_bytes())
        self.assertEqual(sent["status"], 413)
        self.assertTrue(h.close_connection)

    def test_read_body_allows_a_normal_request(self):
        import io

        class FakeHandler(supervisor.Handler):
            def __init__(self):
                self.headers = {"Content-Length": "7"}
                self.close_connection = False
                self.rfile = io.BytesIO(b'{"a":1}trailing')

        # Exactly Content-Length bytes, never the rest of the socket.
        self.assertEqual(FakeHandler()._read_body_bytes(), b'{"a":1}')

    def test_non_numeric_content_length_is_treated_as_empty(self):
        class FakeHandler(supervisor.Handler):
            def __init__(self):
                self.headers = {"Content-Length": "banana"}
                self.close_connection = False
                self.rfile = None

        self.assertEqual(FakeHandler()._read_body_bytes(), b"")


class ServedExtensionTest(unittest.TestCase):
    """Roots hold more than media; the endpoints must only hand back media."""

    def test_databases_and_logs_are_not_serveable_types(self):
        for ext in (".db", ".log", ".json", ".txt", ".safetensors", ".pid", ".py", ""):
            self.assertNotIn(ext, supervisor.CONTENT_TYPES)

    def test_media_are(self):
        for ext in (".flac", ".mp3", ".wav", ".png", ".jpg", ".webp"):
            self.assertIn(ext, supervisor.CONTENT_TYPES)

    def test_audio_roots_no_longer_include_the_whole_storage_volume(self):
        """jobs.db, presses.db, the logs and 9.4 GB of weights live there."""
        root = os.path.realpath(supervisor.AIMUSIC_ROOT)
        self.assertNotIn(root, supervisor.AUDIO_ROOTS)


class JobPruneTest(unittest.TestCase):
    """prune() existed from the start and was called from nowhere (#13)."""

    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "jobs.db")
        self.store = JobStore(self.db)

    def _age(self, task_id, seconds):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE jobs SET updated_at = ?, created_at = ? WHERE task_id = ?",
                     (time.time() - seconds, time.time() - seconds, task_id))
        conn.commit()
        conn.close()

    def test_old_terminal_rows_go(self):
        self.store.record("old", {"prompt": "x"})
        self.store.complete("old")
        self._age("old", 30 * 24 * 3600)
        self.store.prune(7 * 24 * 3600)
        self.assertEqual(self.store.stats(), {})

    def test_recent_rows_stay(self):
        self.store.record("new", {"prompt": "x"})
        self.store.complete("new")
        self.store.prune(7 * 24 * 3600)
        self.assertEqual(self.store.stats(), {"done": 1})

    def test_pending_rows_are_never_pruned(self):
        """Pending is the replay queue; dropping it loses work, which is the
        exact failure the job store was written to prevent."""
        self.store.record("stuck", {"prompt": "x"})
        self._age("stuck", 365 * 24 * 3600)
        self.store.prune(7 * 24 * 3600)
        self.assertEqual(self.store.stats(), {"pending": 1})

    def test_the_reaper_is_wired_to_call_it(self):
        """The whole point of #13's item: it must actually be invoked."""
        import inspect
        source = inspect.getsource(supervisor.reaper)
        self.assertIn("JOBS.prune", source)


if __name__ == "__main__":
    unittest.main()
