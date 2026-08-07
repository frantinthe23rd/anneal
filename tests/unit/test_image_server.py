"""image_server.py — validation and naming, with FLUX never loaded.

`get_model()` is the only thing here that needs mflux, Metal or 4 GB of
weights, and it is reached solely from `generate()`. Stubbing `generate` lets
the real HTTP handler run in-process: size parsing, the retention window, the
step override for variations and the JSON envelope are all exercised against
the code that actually serves them, on CI hardware that has none of the above.
"""

from __future__ import annotations

import json
import http.client
import os
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import image_server


class TestParseSize(unittest.TestCase):
    def test_the_ordinary_case(self):
        self.assertEqual(image_server.parse_size("1024x1024"), (1024, 1024))

    def test_separators_and_whitespace(self):
        for raw in ["1024x1024", "1024X1024", "1024*1024", " 1024 x 1024 ", "1024 X 1024"]:
            self.assertEqual(image_server.parse_size(raw), (1024, 1024), raw)

    def test_non_square(self):
        self.assertEqual(image_server.parse_size("1024x512"), (1024, 512))

    def test_snapped_down_to_a_multiple_of_sixteen(self):
        # FLUX needs multiples of 16; anything else is silently floored.
        self.assertEqual(image_server.parse_size("1000x1000"), (992, 992))
        self.assertEqual(image_server.parse_size("1023x1025"), (1008, 1024))
        for w, h in [image_server.parse_size("777x999"), image_server.parse_size("513x513")]:
            self.assertEqual(w % 16, 0)
            self.assertEqual(h % 16, 0)

    def test_a_floor_of_256(self):
        self.assertEqual(image_server.parse_size("100x100"), (256, 256))
        self.assertEqual(image_server.parse_size("10x10"), (256, 256))

    def test_the_pixel_budget_is_enforced(self):
        self.assertEqual(image_server.parse_size("1536x1536"), (1536, 1536))
        for raw in ["1552x1552", "2048x2048", "99999x99999", "3072x1024"]:
            with self.assertRaises(ValueError, msg=raw):
                image_server.parse_size(raw)

    def test_the_budget_is_area_not_either_side(self):
        # A wide, short image within the budget is fine.
        self.assertEqual(image_server.parse_size("2304x1024"), (2304, 1024))

    def test_malformed_input_raises_rather_than_guessing(self):
        for raw in [None, "", "  ", "1024", "1024x", "x1024", "big", "1024by1024",
                    "-16x-16", "1024x1024x1024", "9x9", "1e3x1e3"]:
            with self.assertRaises(ValueError, msg=repr(raw)):
                image_server.parse_size(raw)

    def test_the_error_says_what_a_size_looks_like(self):
        with self.assertRaises(ValueError) as caught:
            image_server.parse_size("big")
        self.assertIn("1024x1024", str(caught.exception))


class OutputDirCase(unittest.TestCase):
    def setUp(self):
        # realpath: /var is a symlink to /private/var on macOS, and the code
        # under test resolves before comparing.
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="anneal-images-"))
        self._saved = image_server.OUTPUT_DIR
        image_server.OUTPUT_DIR = self.dir
        self.addCleanup(self._restore)

    def _restore(self):
        image_server.OUTPUT_DIR = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, name, body=b"png"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(body)
        return path


class TestResolveInitImage(OutputDirCase):
    """img2img takes a path from the caller. Accepting an arbitrary one would
    hand a network client the ability to read anything this process can."""

    def test_nothing_means_nothing(self):
        self.assertIsNone(image_server.resolve_init_image(None))
        self.assertIsNone(image_server.resolve_init_image(""))

    def test_accepts_a_file_this_server_produced(self):
        path = self.make("earlier.png")
        self.assertEqual(image_server.resolve_init_image(path), path)

    def test_refuses_paths_outside_the_output_directory(self):
        outside = os.path.join(os.path.dirname(self.dir), "elsewhere.png")
        with open(outside, "wb") as fh:
            fh.write(b"png")
        self.addCleanup(os.unlink, outside)
        for raw in ["/etc/passwd", outside, os.path.join(self.dir, "..", "elsewhere.png")]:
            with self.assertRaises(ValueError, msg=raw):
                image_server.resolve_init_image(raw)

    def test_refuses_a_sibling_directory_sharing_the_prefix(self):
        sibling = self.dir + "-elsewhere"
        os.makedirs(sibling, exist_ok=True)
        self.addCleanup(shutil.rmtree, sibling, True)
        victim = os.path.join(sibling, "x.png")
        with open(victim, "wb") as fh:
            fh.write(b"png")
        with self.assertRaises(ValueError):
            image_server.resolve_init_image(victim)

    def test_refuses_a_missing_file_inside_the_directory(self):
        with self.assertRaises(ValueError):
            image_server.resolve_init_image(os.path.join(self.dir, "never-made.png"))

    def test_refuses_the_directory_itself(self):
        with self.assertRaises(ValueError):
            image_server.resolve_init_image(self.dir)

    def test_refuses_a_symlink_pointing_out(self):
        target = os.path.join(os.path.dirname(self.dir), "secret.txt")
        with open(target, "w") as fh:
            fh.write("secret")
        self.addCleanup(os.unlink, target)
        link = os.path.join(self.dir, "innocent.png")
        os.symlink(target, link)
        with self.assertRaises(ValueError):
            image_server.resolve_init_image(link)

    def test_the_error_explains_the_rule(self):
        with self.assertRaises(ValueError) as caught:
            image_server.resolve_init_image("/etc/passwd")
        self.assertIn("previously generated here", str(caught.exception))


class TestUniquePath(OutputDirCase):
    """A variation shares both the prompt prefix and the seed with its source,
    so without this it overwrote the very image it derived from."""

    def test_the_first_name_is_the_plain_one(self):
        self.assertEqual(image_server.unique_path("a-prompt", 42),
                         os.path.join(self.dir, "a-prompt-42.png"))

    def test_a_collision_gets_a_suffix(self):
        self.make("a-prompt-42.png")
        self.assertEqual(image_server.unique_path("a-prompt", 42),
                         os.path.join(self.dir, "a-prompt-42-2.png"))

    def test_suffixes_keep_counting(self):
        for name in ["a-prompt-42.png", "a-prompt-42-2.png", "a-prompt-42-3.png"]:
            self.make(name)
        self.assertEqual(image_server.unique_path("a-prompt", 42),
                         os.path.join(self.dir, "a-prompt-42-4.png"))

    def test_different_seeds_do_not_collide(self):
        self.make("a-prompt-42.png")
        self.assertEqual(image_server.unique_path("a-prompt", 43),
                         os.path.join(self.dir, "a-prompt-43.png"))

    def test_the_returned_path_never_already_exists(self):
        for _ in range(5):
            path = image_server.unique_path("a-prompt", 42)
            self.assertFalse(os.path.exists(path))
            self.make(os.path.basename(path))


class TestRetentionArithmetic(unittest.TestCase):
    """Why a variation defaults to 8 steps rather than schnell's 4.

    mflux spends `int(steps * retention)` of the budget reproducing the init
    image. At 4 steps the three retention levels the UI offers collapse onto
    two budgets, so two of them produce byte-identical output; at 8 they are
    genuinely distinct. If the default ever changes, this is the reasoning it
    has to survive.
    """

    OFFERED = (0.85, 0.7, 0.55)

    def budgets(self, steps):
        return {int(steps * r) for r in self.OFFERED}

    def test_schnell_still_defaults_to_four_steps(self):
        self.assertEqual(image_server.DEFAULT_STEPS, 4)

    def test_four_steps_cannot_separate_the_offered_levels(self):
        self.assertLess(len(self.budgets(4)), len(self.OFFERED))
        self.assertEqual(int(4 * 0.7), int(4 * 0.55))     # the identical pair

    def test_eight_steps_gives_each_level_its_own_budget(self):
        self.assertEqual(len(self.budgets(8)), len(self.OFFERED))
        self.assertEqual(sorted(self.budgets(8)), [4, 5, 6])

    def test_something_is_always_left_for_the_prompt(self):
        # At the maximum retention there must still be a step to act on the
        # prompt, or the result is just the init image again.
        for steps in range(2, image_server.MAX_STEPS + 1):
            self.assertLess(int(steps * 0.95), steps, steps)


# ---------------------------------------------------------------- live handler
class StubGenerate:
    """Records what the handler decided, and writes a file where a real
    generation would have."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.calls = []

    def __call__(self, prompt, width, height, steps, seed, init_path=None, retention=None):
        self.calls.append({"prompt": prompt, "width": width, "height": height,
                           "steps": steps, "seed": seed, "init_path": init_path,
                           "retention": retention})
        path = os.path.join(self.out_dir, "stub-%d.png" % len(self.calls))
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG stub")
        return path, 0.1


class TestImageHandler(unittest.TestCase):
    """The real handler, on a real socket, with generation stubbed. No model is
    ever resolved, downloaded or loaded."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), image_server.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="anneal-images-"))
        self._saved_dir = image_server.OUTPUT_DIR
        self._saved_generate = image_server.generate
        image_server.OUTPUT_DIR = self.dir
        self.generate = StubGenerate(self.dir)
        image_server.generate = self.generate
        self.addCleanup(self._restore)

    def _restore(self):
        image_server.OUTPUT_DIR = self._saved_dir
        image_server.generate = self._saved_generate
        shutil.rmtree(self.dir, ignore_errors=True)

    def request(self, method, path, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            conn.request(method, path, body=body,
                         headers={"Content-Type": "application/json"} if body else {})
            resp = conn.getresponse()
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8"))
            except ValueError:
                return resp.status, raw
        finally:
            conn.close()

    def post(self, payload):
        return self.request("POST", "/v1/images/generations", payload)

    # -- health -----------------------------------------------------------
    def test_health_reports_without_loading_anything(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "image")
        self.assertIs(body["loaded"], False)

    def test_busy_reports_the_in_flight_count(self):
        status, body = self.request("GET", "/busy")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["in_flight"], 0)

    def test_unknown_routes_are_404(self):
        self.assertEqual(self.request("GET", "/nope")[0], 404)
        self.assertEqual(self.request("POST", "/nope", {})[0], 404)

    # -- validation -------------------------------------------------------
    def test_a_prompt_is_required(self):
        for payload in [{}, {"prompt": ""}, {"prompt": "   "}, {"prompt": None}]:
            status, body = self.post(payload)
            self.assertEqual(status, 400, payload)
            self.assertIn("prompt", body["error"])
        self.assertEqual(self.generate.calls, [])

    def test_an_unparseable_body_is_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/v1/images/generations", body=b"{not json",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode())
        conn.close()
        self.assertEqual(resp.status, 400)
        self.assertIn("JSON", body["error"])

    def test_a_bad_size_is_400_and_generates_nothing(self):
        status, body = self.post({"prompt": "a cover", "size": "enormous"})
        self.assertEqual(status, 400)
        self.assertIn("1024x1024", body["error"])
        self.assertEqual(self.generate.calls, [])

    def test_an_oversized_request_is_400(self):
        status, body = self.post({"prompt": "a cover", "size": "4096x4096"})
        self.assertEqual(status, 400)
        self.assertIn("too large", body["error"])

    def test_an_init_image_outside_the_output_directory_is_400(self):
        status, body = self.post({"prompt": "a cover", "init_image": "/etc/passwd"})
        self.assertEqual(status, 400)
        self.assertIn("previously generated here", body["error"])
        self.assertEqual(self.generate.calls, [])

    def test_retention_outside_the_window_is_400(self):
        init = os.path.join(self.dir, "source.png")
        with open(init, "wb") as fh:
            fh.write(b"png")
        for retention in (0.1, 0.29, 0.96, 1.5):
            status, body = self.post({"prompt": "a cover", "init_image": init,
                                      "retention": retention})
            self.assertEqual(status, 400, retention)
            self.assertIn("between 0.3 and 0.95", body["error"])

    def test_retention_is_ignored_without_an_init_image(self):
        # Nothing to retain, so an out-of-range value is not an error.
        status, _ = self.post({"prompt": "a cover", "retention": 5.0})
        self.assertEqual(status, 200)

    # -- defaults and clamping --------------------------------------------
    def test_defaults(self):
        status, body = self.post({"prompt": "a cover"})
        self.assertEqual(status, 200)
        call = self.generate.calls[0]
        self.assertEqual((call["width"], call["height"]), (1024, 1024))
        self.assertEqual(call["steps"], image_server.DEFAULT_STEPS)
        self.assertIsNone(call["init_path"])
        self.assertEqual(len(body["data"]), 1)

    def test_steps_are_clamped_to_the_allowed_range(self):
        for asked, want in [(0, image_server.DEFAULT_STEPS), (1, 1), (-5, 1),
                            (image_server.MAX_STEPS + 50, image_server.MAX_STEPS)]:
            self.generate.calls[:] = []
            self.post({"prompt": "a cover", "steps": asked})
            self.assertEqual(self.generate.calls[0]["steps"], want, asked)

    def test_n_is_clamped_to_four_and_seeds_increment(self):
        status, body = self.post({"prompt": "a cover", "n": 99, "seed": 100})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]), 4)
        self.assertEqual([c["seed"] for c in self.generate.calls], [100, 101, 102, 103])

    def test_an_absent_seed_is_random_but_reported(self):
        _, body = self.post({"prompt": "a cover"})
        self.assertIsInstance(body["data"][0]["seed"], int)
        self.assertEqual(body["data"][0]["seed"], self.generate.calls[0]["seed"])

    def test_a_variation_raises_the_step_budget_to_eight(self):
        init = os.path.join(self.dir, "source.png")
        with open(init, "wb") as fh:
            fh.write(b"png")
        self.post({"prompt": "a cover", "init_image": init})
        call = self.generate.calls[0]
        self.assertEqual(call["steps"], 8)
        self.assertEqual(call["init_path"], init)
        self.assertEqual(call["retention"], 0.7)

    def test_an_explicit_step_count_still_wins_for_a_variation(self):
        init = os.path.join(self.dir, "source.png")
        with open(init, "wb") as fh:
            fh.write(b"png")
        self.post({"prompt": "a cover", "init_image": init, "steps": 4})
        self.assertEqual(self.generate.calls[0]["steps"], 4)

    # -- response shape ---------------------------------------------------
    def test_the_envelope(self):
        status, body = self.post({"prompt": "a cover", "response_format": "path"})
        self.assertEqual(status, 200)
        self.assertEqual(body["code"], 200)
        self.assertIsNone(body["error"])
        self.assertIsInstance(body["created"], int)
        entry = body["data"][0]
        self.assertEqual(sorted(entry), ["path", "seconds", "seed", "url"])
        self.assertEqual(entry["url"], "/v1/images/file?path=" + entry["path"])

    def test_b64_is_the_default_response_format(self):
        _, body = self.post({"prompt": "a cover"})
        self.assertIn("b64_json", body["data"][0])

    def test_a_variation_records_where_it_came_from(self):
        init = os.path.join(self.dir, "source.png")
        with open(init, "wb") as fh:
            fh.write(b"png")
        _, body = self.post({"prompt": "a cover", "init_image": init,
                             "retention": 0.85, "response_format": "path"})
        self.assertEqual(body["data"][0]["derived_from"], init)
        self.assertEqual(body["data"][0]["retention"], 0.85)

    def test_a_generation_failure_is_a_500_with_the_reason(self):
        def boom(*args, **kwargs):
            raise RuntimeError("Metal ran out")
        image_server.generate = boom
        status, body = self.post({"prompt": "a cover"})
        self.assertEqual(status, 500)
        self.assertIn("Metal ran out", body["error"])

    def test_the_in_flight_counter_returns_to_zero_after_a_failure(self):
        # The reaper reads this to decide whether it may evict the model.
        def boom(*args, **kwargs):
            raise RuntimeError("no")
        image_server.generate = boom
        self.post({"prompt": "a cover"})
        self.assertEqual(self.request("GET", "/busy")[1]["data"]["in_flight"], 0)

    # -- file serving -----------------------------------------------------
    def test_serving_a_generated_file(self):
        _, body = self.post({"prompt": "a cover", "response_format": "path"})
        path = body["data"][0]["path"]
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/v1/images/file?path=" + path)
        resp = conn.getresponse()
        blob = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "image/png")
        self.assertEqual(blob, b"\x89PNG stub")

    def test_file_serving_refuses_anything_outside_the_output_directory(self):
        for path in ["/etc/passwd", os.path.join(self.dir, "..", "escape.png"), self.dir, ""]:
            status, _ = self.request("GET", "/v1/images/file?path=" + path)
            self.assertEqual(status, 404, path)


if __name__ == "__main__":
    unittest.main()
