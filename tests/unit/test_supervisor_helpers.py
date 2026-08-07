"""supervisor.py's pure helpers, exercised without a socket.

Importing supervisor is a real side effect — it opens two sqlite databases and
computes the roots it will serve files from — so `tests/unit/__init__.py` has
already redirected AIMUSIC_ROOT at a throwaway directory by the time this
module is imported.

Handler methods are called on an instance built with `__new__`, because
BaseHTTPRequestHandler.__init__ *is* the request loop and would want a socket.
The methods under test only touch attributes, which are supplied.
"""

from __future__ import annotations

import io
import json
import os
import unittest

import supervisor
from supervisor import Handler


def make_handler(path="/", headers=None):
    """A Handler with just enough state for the helpers that do not send."""
    handler = Handler.__new__(Handler)
    handler.path = path
    handler.headers = FakeHeaders(headers or {})
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 12345)
    handler.sent = []

    def send_response(code, message=None):
        handler.sent.append(("status", code))

    def send_header(key, value):
        handler.sent.append((key.lower(), value))

    handler.send_response = send_response
    handler.send_header = send_header
    handler.end_headers = lambda: None
    return handler


class FakeHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class FakeResponse:
    def __init__(self, headers):
        self._headers = {k.lower(): v for k, v in headers.items()}

    def getheader(self, name, default=None):
        return self._headers.get(name.lower(), default)


class TestParseSizeMb(unittest.TestCase):
    """`footprint` output, into megabytes. RSS is meaningless for MLX, so this
    is how the supervisor learns what a backend is actually holding."""

    def test_units(self):
        for text, want in [
            ("20 GB", 20 * 1024.0),
            ("512 MB", 512.0),
            ("1 TB", 1024.0 * 1024),
            ("1024 KB", 1.0),
            ("1048576 B", 1.0),
            ("21.4 GB", 21.4 * 1024),
        ]:
            self.assertAlmostEqual(supervisor._parse_size_mb(text), want, places=3, msg=text)

    def test_thousands_separators(self):
        self.assertAlmostEqual(supervisor._parse_size_mb("1,024 MB"), 1024.0)

    def test_a_bare_number_is_megabytes(self):
        self.assertEqual(supervisor._parse_size_mb("512"), 512.0)

    def test_an_unrecognised_unit_falls_back_to_megabytes(self):
        self.assertEqual(supervisor._parse_size_mb("512 QB"), 512.0)

    def test_lowercase_units(self):
        self.assertAlmostEqual(supervisor._parse_size_mb("2 gb"), 2048.0)

    def test_junk_is_zero_not_an_exception(self):
        # This runs inside status polling; raising here would take out /health.
        for text in ["", "   ", "unknown", "GB", "n/a"]:
            self.assertEqual(supervisor._parse_size_mb(text), 0.0, repr(text))


class TestIsStream(unittest.TestCase):
    """A streamed response must be relayed chunk by chunk. Transfer-Encoding is
    hop-by-hop and gets stripped, so misjudging this leaves a client hanging."""

    def test_event_stream_is_always_streamed(self):
        self.assertTrue(supervisor._is_stream(FakeResponse(
            {"Content-Type": "text/event-stream", "Content-Length": "10"})))
        self.assertTrue(supervisor._is_stream(FakeResponse(
            {"Content-Type": "text/event-stream; charset=utf-8"})))

    def test_chunked_with_no_length_is_streamed(self):
        self.assertTrue(supervisor._is_stream(FakeResponse(
            {"Content-Type": "application/json", "Transfer-Encoding": "chunked"})))

    def test_a_declared_length_is_not_streamed(self):
        self.assertFalse(supervisor._is_stream(FakeResponse(
            {"Content-Type": "application/json", "Content-Length": "42"})))

    def test_neither_length_nor_encoding_is_not_streamed(self):
        self.assertFalse(supervisor._is_stream(FakeResponse({"Content-Type": "audio/flac"})))


class TestServeFileFromDisk(unittest.TestCase):
    """The containment check behind /v1/audio, /v1/outputs/file and
    /v1/images/file. Returning False is what turns into a 404."""

    def setUp(self):
        self.root = os.path.realpath(os.path.join(supervisor.AIMUSIC_ROOT, "outputs"))
        os.makedirs(os.path.join(self.root, "music"), exist_ok=True)
        self.inside = os.path.join(self.root, "music", "take.flac")
        with open(self.inside, "wb") as fh:
            fh.write(b"fLaC-bytes")
        self.outside = os.path.join(supervisor.AIMUSIC_ROOT, "not-an-output.txt")
        with open(self.outside, "w") as fh:
            fh.write("private")

    def serve(self, raw_path):
        handler = make_handler("/v1/outputs/file?path=" + raw_path)
        return handler, handler._serve_file_from_disk([self.root])

    def test_serves_a_file_inside_the_root(self):
        handler, ok = self.serve(self.inside)
        self.assertTrue(ok)
        self.assertEqual(handler.wfile.getvalue(), b"fLaC-bytes")
        self.assertIn(("status", 200), handler.sent)
        self.assertIn(("content-type", "audio/flac"), handler.sent)
        self.assertIn(("content-length", "10"), handler.sent)

    def test_sets_a_download_filename(self):
        handler, _ = self.serve(self.inside)
        disposition = dict(handler.sent[1:])["content-disposition"]
        self.assertIn('filename="take.flac"', disposition)

    def test_an_unknown_extension_is_refused_rather_than_guessed(self):
        """Deliberate reversal of an earlier assertion.

        This used to expect a fallback to application/octet-stream. These
        endpoints exist to hand back generated media, and every artefact they
        legitimately serve has a known extension — so an unknown one means
        either a bug or a path that should never have resolved. Refusing is the
        second line of defence behind the root narrowing: a root that later
        grows to include something else still cannot leak a database or a log
        through here.
        """
        other = os.path.join(self.root, "music", "take.xyz")
        with open(other, "wb") as fh:
            fh.write(b"?")
        _, ok = self.serve(other)
        self.assertFalse(ok)

    def test_refuses_an_absolute_path_outside_the_root(self):
        for path in ["/etc/passwd", self.outside]:
            _, ok = self.serve(path)
            self.assertFalse(ok, path)

    def test_refuses_traversal_back_out_of_the_root(self):
        _, ok = self.serve(os.path.join(self.root, "music", "..", "..", "not-an-output.txt"))
        self.assertFalse(ok)

    def test_refuses_a_sibling_directory_sharing_the_prefix(self):
        sibling = self.root + "-elsewhere"
        os.makedirs(sibling, exist_ok=True)
        victim = os.path.join(sibling, "take.flac")
        with open(victim, "wb") as fh:
            fh.write(b"x")
        _, ok = self.serve(victim)
        self.assertFalse(ok)

    def test_refuses_the_root_directory_itself(self):
        _, ok = self.serve(self.root)
        self.assertFalse(ok)

    def test_refuses_a_directory_inside_the_root(self):
        _, ok = self.serve(os.path.join(self.root, "music"))
        self.assertFalse(ok)

    def test_refuses_a_missing_file(self):
        _, ok = self.serve(os.path.join(self.root, "music", "ghost.flac"))
        self.assertFalse(ok)

    def test_refuses_an_empty_or_absent_path_parameter(self):
        for query in ["/v1/outputs/file", "/v1/outputs/file?path=", "/v1/outputs/file?other=1"]:
            handler = make_handler(query)
            self.assertFalse(handler._serve_file_from_disk([self.root]), query)

    def test_percent_encoding_is_decoded_before_the_check(self):
        # parse_qs decodes, so an encoded traversal is the same traversal.
        _, ok = self.serve("%2Fetc%2Fpasswd")
        self.assertFalse(ok)

    def test_a_symlink_out_of_the_root_is_refused(self):
        link = os.path.join(self.root, "music", "escape.flac")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(self.outside, link)
        _, ok = self.serve(link)
        self.assertFalse(ok)


class TestAudioRoots(unittest.TestCase):
    def test_the_roots_are_absolute_and_resolved(self):
        for root in supervisor.AUDIO_ROOTS + supervisor.IMAGE_ROOTS:
            self.assertTrue(os.path.isabs(root), root)
            self.assertEqual(root, os.path.realpath(root), root)

    def test_images_are_confined_to_outputs(self):
        self.assertEqual(supervisor.IMAGE_ROOTS,
                         [os.path.realpath(os.path.join(supervisor.AIMUSIC_ROOT, "outputs"))])


class TestOrphanDetection(unittest.TestCase):
    """ACE-Step's queue is in memory. A task issued before a restart is gone,
    but the backend keeps reporting status 0 — which reads as "still queued"
    forever. The epoch is how the gateway knows better."""

    def setUp(self):
        self.music = supervisor.SERVICE_OBJECTS["music"]
        self._epoch = self.music.epoch
        self._proc = self.music.proc
        supervisor._ISSUED.clear()
        self.addCleanup(self.restore)

    def restore(self):
        self.music.epoch = self._epoch
        self.music.proc = self._proc
        supervisor._ISSUED.clear()

    def test_a_task_issued_under_the_current_epoch_is_not_orphaned(self):
        self.music.epoch = 3
        supervisor._record_issued("t1")
        self.assertFalse(supervisor._is_orphaned("t1"))

    def test_a_task_issued_before_a_restart_is_orphaned(self):
        self.music.epoch = 3
        supervisor._record_issued("t1")
        self.music.epoch = 4                        # backend restarted
        self.assertTrue(supervisor._is_orphaned("t1"))

    def test_an_unknown_task_is_orphaned_when_the_backend_is_not_running(self):
        # A stopped backend holds no queue at all, so this case is decisive.
        self.music.proc = None
        self.assertTrue(supervisor._is_orphaned("never-seen"))

    def test_a_job_still_queued_for_replay_is_never_reported_orphaned(self):
        # Otherwise the poll says "failed" moments before the replay thread
        # resurrects it.
        self.music.epoch = 5
        supervisor._record_issued("t1")
        self.music.epoch = 6
        supervisor.JOBS.record("t1", {"prompt": "a brief"})
        self.addCleanup(supervisor.JOBS.complete, "t1", "done")
        self.assertFalse(supervisor._is_orphaned("t1"))

    def test_issued_ids_are_evicted_rather_than_growing_without_bound(self):
        for n in range(supervisor.MAX_TRACKED_JOBS + 10):
            supervisor._record_issued("t%d" % n)
        self.assertLessEqual(len(supervisor._ISSUED), supervisor.MAX_TRACKED_JOBS)
        self.assertIn("t%d" % (supervisor.MAX_TRACKED_JOBS + 9), supervisor._ISSUED)

    def test_a_blank_task_id_is_not_recorded(self):
        supervisor._record_issued("")
        supervisor._record_issued(None)
        self.assertEqual(supervisor._ISSUED, {})


class TestAnnotateOrphans(unittest.TestCase):
    def setUp(self):
        self.music = supervisor.SERVICE_OBJECTS["music"]
        self._epoch, self._proc = self.music.epoch, self.music.proc
        supervisor._ISSUED.clear()
        self.addCleanup(self.restore)

    def restore(self):
        self.music.epoch, self.music.proc = self._epoch, self._proc
        supervisor._ISSUED.clear()

    def body(self, rows):
        return json.dumps({"data": rows, "code": 200}).encode()

    def test_a_queued_orphan_is_rewritten_as_failed(self):
        self.music.epoch = 1
        supervisor._record_issued("t1")
        self.music.epoch = 2
        out = json.loads(supervisor._annotate_orphans(
            self.body([{"task_id": "t1", "status": 0}]), {"t1"}).decode())
        row = out["data"][0]
        self.assertEqual(row["status"], 2)
        self.assertTrue(row["orphaned"])
        self.assertIn("orphaned", json.loads(row["result"])[0]["error"])

    def test_a_genuinely_queued_job_is_left_alone(self):
        self.music.epoch = 1
        supervisor._record_issued("t1")
        original = self.body([{"task_id": "t1", "status": 0}])
        self.assertEqual(supervisor._annotate_orphans(original, {"t1"}), original)

    def test_ids_the_caller_did_not_ask_about_are_left_alone(self):
        self.music.epoch = 1
        supervisor._record_issued("t1")
        self.music.epoch = 2
        original = self.body([{"task_id": "t1", "status": 0}])
        self.assertEqual(supervisor._annotate_orphans(original, {"someone-else"}), original)

    def test_finished_and_failed_rows_are_left_alone(self):
        self.music.epoch = 2
        for status in (1, 2):
            original = self.body([{"task_id": "t1", "status": status}])
            self.assertEqual(supervisor._annotate_orphans(original, {"t1"}), original)

    def test_a_body_that_does_not_parse_comes_back_unchanged(self):
        # Never make a poll worse than it already was.
        for raw in [b"", b"not json", b"[]", json.dumps({"data": "text"}).encode()]:
            self.assertEqual(supervisor._annotate_orphans(raw, {"t1"}), raw)

    def test_non_dict_rows_are_survived(self):
        original = self.body(["a string", None, 7])
        self.assertEqual(supervisor._annotate_orphans(original, {"t1"}), original)


class TestIdRewriting(unittest.TestCase):
    """The caller keeps polling the id it was originally given; the gateway
    translates in both directions so the replay stays invisible."""

    def setUp(self):
        self.original, self.replayed = "orig-abc", "new-def"
        supervisor.JOBS.set_alias(self.original, self.replayed)

    def test_a_poll_is_pointed_at_the_replayed_job(self):
        handler = make_handler("/query_result")
        body = json.dumps({"task_id_list": [self.original]}).encode()
        out = json.loads(handler._rewrite_polled_ids(body).decode())
        self.assertEqual(out["task_id_list"], [self.replayed])
        self.assertEqual(handler._polled_originals, {self.original})

    def test_an_untranslated_poll_is_passed_through_byte_for_byte(self):
        handler = make_handler("/query_result")
        body = json.dumps({"task_id_list": ["untouched"]}).encode()
        self.assertEqual(handler._rewrite_polled_ids(body), body)
        self.assertEqual(handler._polled_originals, {"untouched"})

    def test_a_task_id_list_sent_as_a_json_string_is_understood(self):
        handler = make_handler("/query_result")
        body = json.dumps({"task_id_list": json.dumps([self.original])}).encode()
        out = json.loads(handler._rewrite_polled_ids(body).decode())
        self.assertEqual(out["task_id_list"], [self.replayed])

    def test_a_malformed_body_is_passed_through(self):
        handler = make_handler("/query_result")
        for body in [b"", None, b"not json", json.dumps({}).encode()]:
            self.assertEqual(handler._rewrite_polled_ids(body), body)

    def test_the_answer_wears_the_id_the_caller_asked_about(self):
        handler = make_handler("/query_result")
        response = json.dumps({"data": [{"task_id": self.replayed, "status": 0}]}).encode()
        out = json.loads(handler._restore_original_ids(response, {self.original}).decode())
        self.assertEqual(out["data"][0]["task_id"], self.original)

    def test_an_id_the_caller_did_not_ask_about_is_not_rewritten(self):
        handler = make_handler("/query_result")
        response = json.dumps({"data": [{"task_id": self.replayed, "status": 0}]}).encode()
        self.assertEqual(handler._restore_original_ids(response, set()), response)

    def test_a_terminal_status_marks_the_job_complete(self):
        supervisor.JOBS.record("done-job", {"prompt": "x"})
        handler = make_handler("/query_result")
        response = json.dumps({"data": [{"task_id": "done-job", "status": 2}]}).encode()
        handler._restore_original_ids(response, {"done-job"})
        self.assertEqual([t for t, _, _ in supervisor.JOBS.pending()
                          if t == "done-job"], [])

    def test_a_response_that_does_not_parse_comes_back_unchanged(self):
        handler = make_handler("/query_result")
        for raw in [b"", b"not json", json.dumps({"data": {}}).encode()]:
            self.assertEqual(handler._restore_original_ids(raw, {"x"}), raw)


class TestPathFromFileUrl(unittest.TestCase):
    def test_extracts_the_path_from_an_audio_url(self):
        self.assertEqual(
            Handler._path_from_file_url("/v1/audio?path=%2Fa%2Fb%2Ftake.flac"),
            "/a/b/take.flac")

    def test_missing_and_malformed(self):
        for url in [None, "", "/v1/audio", "not a url"]:
            self.assertIn(Handler._path_from_file_url(url), (None, ""))


class TestServiceStatusShape(unittest.TestCase):
    """/health and /supervisor/status embed this for every service, and neither
    may touch a model or shell out when nothing is running."""

    def test_a_cold_service_reports_cold(self):
        for name, svc in supervisor.SERVICE_OBJECTS.items():
            if svc.is_running():                    # pragma: no cover - dev machine
                continue
            status = svc.status()
            self.assertEqual(sorted(status), [
                "heavy", "idle_seconds", "idle_timeout_seconds", "in_flight",
                "memory_mb", "peak_memory_mb", "port", "running", "state"])
            self.assertFalse(status["running"], name)
            self.assertEqual(status["state"], "cold", name)
            self.assertIsNone(status["memory_mb"], name)

    def test_epochs_start_at_zero(self):
        for svc in supervisor.SERVICE_OBJECTS.values():
            self.assertGreaterEqual(svc.epoch, 0)


class TestTextModelRewrite(unittest.TestCase):
    def test_the_model_path_comes_from_the_service_table(self):
        cmd = supervisor.SERVICES["text"]["cmd"]
        self.assertEqual(supervisor.TEXT_MODEL_PATH, cmd[cmd.index("--model") + 1])

    def test_the_display_name_is_derived_from_the_snapshot_path(self):
        # models--mlx-community--gemma-... -> mlx-community/gemma-...
        self.assertNotIn("models--", supervisor.TEXT_MODEL_NAME)
        if supervisor.TEXT_MODEL_PATH:
            self.assertTrue(supervisor.TEXT_MODEL_NAME)


if __name__ == "__main__":
    unittest.main()
