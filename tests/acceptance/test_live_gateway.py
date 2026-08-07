"""The running gateway, exercised over HTTP without waking a model.

Every request here lands on a route the gateway answers in-process. The one
thing this suite must not do is cost three or four minutes and evict whatever
is resident, so anything that would generate is behind ANNEAL_TEST_HEAVY=1 and
skipped by default. `TestNothingWokeUp` and `tearDownModule` then check, against
a baseline taken when the suite began, that nothing was started.

Run against something other than the local gateway with
ANNEAL_TEST_BASE=https://host.
"""

from __future__ import annotations

import json
import os
import unittest

import outputs

from tests.acceptance.live import (HEAVY, HEAVY_REASON, LiveCase, baseline_services,
                                   request, running_services)

# Safe: answered by the gateway, never proxied to a backend.
PUBLIC = ["/health", "/supervisor/status", "/supervisor/auth", "/supervisor/whoami",
          "/v1/music/tiers", "/openapi.json", "/", "/docs"]

PROTECTED = [
    ("GET", "/v1/outputs"),
    ("GET", "/v1/outputs/file?path=/tmp/x"),
    ("GET", "/v1/images/file?path=/tmp/x"),
    ("GET", "/v1/audio?path=/tmp/x"),
    ("GET", "/v1/press"),
    ("GET", "/v1/press/download?id=x"),
    ("DELETE", "/v1/press?id=x"),
    ("DELETE", "/v1/outputs?path=/tmp/x"),
    ("POST", "/v1/press"),
    ("POST", "/v1/press/resume"),
    ("POST", "/v1/press/cancel"),
    ("POST", "/v1/text"),
    ("POST", "/supervisor/start"),
    ("POST", "/supervisor/stop"),
]

# Paths that read a file off disk from a caller-supplied `path`.
FILE_ENDPOINTS = ["/v1/outputs/file", "/v1/images/file", "/v1/audio"]

TRAVERSALS = [
    "/etc/passwd",
    "/etc/hosts",
    "../../../etc/passwd",
    "%2Fetc%2Fpasswd",
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "/Users",
    "/",
    "",
]


class TestEnvelopeContract(LiveCase):
    def test_public_endpoints_answer_with_the_envelope(self):
        for path in ["/health", "/supervisor/status", "/supervisor/auth",
                     "/supervisor/whoami", "/v1/music/tiers"]:
            status, _, body, _ = self.get(path)
            self.assertEnvelope(status, body, 200, path)

    def test_health_describes_every_service(self):
        _, _, body, _ = self.get("/health")
        services = body["data"]["services"]
        self.assertEqual(sorted(services), ["image", "music", "speech", "text"])
        for name, svc in services.items():
            self.assertIn(svc["state"], ("cold", "heating", "hot"), name)
            self.assertIsInstance(svc["running"], bool, name)
            self.assertIsInstance(svc["port"], int, name)

    def test_health_reports_memory_the_way_the_project_judges_it(self):
        # Paging rate and the kernel's own verdict, not swap volume.
        system = self.get("/health")[2]["data"]["system"]
        self.assertIn(system["pressure_level"], ("normal", "warning", "critical"))
        self.assertIsInstance(system["pressure"], bool)
        self.assertIn("free_mb", system)

    def test_tiers_are_reported_without_touching_the_model(self):
        _, _, body, _ = self.get("/v1/music/tiers")
        data = body["data"]
        self.assertIn(data["default"], data["tiers"])
        loaded = [n for n, t in data["tiers"].items() if t["loaded"]]
        self.assertEqual(len(loaded), 1, "exactly one tier can be resident")
        for name, tier in data["tiers"].items():
            self.assertEqual(sorted(tier), ["available", "label", "loaded", "model",
                                            "steps", "unavailable_reason"], name)

    def test_errors_carry_the_status_in_the_body(self):
        for path, want in [("/v1/outputs", 401), ("/assets/nope.png", 404),
                           ("/nothing-owns-this", 404)]:
            status, _, body, _ = self.get(path)
            self.assertEnvelope(status, body, want, path)

    def test_the_outputs_listing_is_shaped_as_documented(self):
        status, _, body, _ = self.authed("/v1/outputs?limit=3")
        self.assertEnvelope(status, body, 200, "/v1/outputs")
        data = body["data"]
        self.assertIsInstance(data["total"], int)
        self.assertLessEqual(len(data["items"]), 3)
        for item in data["items"]:
            self.assertEqual(sorted(item),
                             ["bytes", "created", "kind", "meta", "name", "path",
                              "prompt", "url"])
            # Against outputs.KINDS, not a literal: `vectors` was added as a
            # fourth kind and broke this while the listing was entirely correct.
            self.assertIn(item["kind"], outputs.KINDS)
            self.assertTrue(item["url"].startswith("/v1/outputs/file?path="))

    def test_the_press_list_is_shaped_as_documented(self):
        status, _, body, _ = self.authed("/v1/press")
        self.assertEnvelope(status, body, 200, "/v1/press")
        for press in body["data"]["presses"]:
            self.assertEqual(
                sorted(press),
                ["cover", "created", "error", "id", "plan", "request", "stage",
                 "state", "tracks", "updated"])

    def test_every_json_response_declares_its_content_type(self):
        for path in PUBLIC:
            status, headers, _, _ = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers.get("content-type"), path)

    def test_responses_are_close_delimited_or_length_delimited(self):
        # Transfer-Encoding is hop-by-hop and the tailscale relay strips it,
        # which leaves a client with no framing and a hang. Everything the
        # gateway answers itself must declare a length.
        for path in PUBLIC:
            _, headers, _, raw = self.get(path)
            self.assertIn("content-length", headers, path)
            self.assertEqual(int(headers["content-length"]), len(raw), path)


class TestAuthorisation(LiveCase):
    def test_public_endpoints_need_no_key(self):
        for path in PUBLIC:
            self.assertEqual(self.get(path)[0], 200, path)

    def test_everything_else_refuses_an_anonymous_caller(self):
        for method, path in PROTECTED:
            status, _, body, _ = request(method, path, payload={} if method == "POST" else None)
            self.assertEqual(status, 401, "%s %s" % (method, path))
            self.assertEqual(body["error"], "unauthorized", path)

    def test_proxied_routes_refuse_an_anonymous_caller_before_starting_anything(self):
        # These resolve to a backend, and the auth check sits in front of
        # start_service — so a 401 here is also proof no model was woken.
        for path in ["/v1/stats", "/v1/voices", "/v1/models"]:
            status, _, body, _ = self.get(path)
            self.assertEqual(status, 401, path)
            self.assertEqual(body["error"], "unauthorized", path)

    def test_a_wrong_key_is_no_better_than_none(self):
        for key in ["wrong", "Bearer", "0" * 64]:
            self.assertEqual(request("GET", "/v1/outputs", key=key)[0], 401, key)

    def test_a_key_without_the_bearer_prefix_is_refused(self):
        if not self.key:
            self.skipTest("no ACESTEP_API_KEY available")
        status, _, _, _ = request("GET", "/v1/outputs", headers={"Authorization": self.key})
        self.assertEqual(status, 401)

    def test_the_real_key_is_accepted(self):
        self.assertEqual(self.authed("/v1/outputs?limit=1")[0], 200)

    def test_auth_reports_how_the_caller_authenticated(self):
        anonymous = self.get("/supervisor/auth")[2]["data"]
        self.assertIs(anonymous["authenticated"], False)
        self.assertIsNone(anonymous["via"])
        if self.key:
            with_key = request("GET", "/supervisor/auth", key=self.key)[2]["data"]
            self.assertIs(with_key["authenticated"], True)
            self.assertEqual(with_key["via"], "key")

    def test_the_key_is_never_echoed_back(self):
        if not self.key:
            self.skipTest("no ACESTEP_API_KEY available")
        for path in ["/supervisor/auth", "/supervisor/whoami", "/health", "/v1/outputs"]:
            _, headers, _, raw = request("GET", path, key=self.key)
            self.assertNotIn(self.key.encode(), raw, path)
            self.assertNotIn(self.key, json.dumps(headers), path)


class TestPathTraversal(LiveCase):
    """Every file-serving endpoint takes a path from the caller."""

    def test_file_endpoints_refuse_paths_outside_their_roots(self):
        if not self.key:
            self.skipTest("no ACESTEP_API_KEY available")
        for endpoint in FILE_ENDPOINTS:
            for raw in TRAVERSALS:
                path = "%s?path=%s" % (endpoint, raw)
                status, _, body, blob = request("GET", path, key=self.key)
                self.assertIn(status, (400, 404), path)
                self.assertNotIn(b"root:", blob, path)
                self.assertTrue(body and body.get("error"), path)

    def test_file_endpoints_refuse_them_anonymously_too(self):
        for endpoint in FILE_ENDPOINTS:
            for raw in TRAVERSALS[:3]:
                status, _, _, blob = self.get("%s?path=%s" % (endpoint, raw))
                self.assertEqual(status, 401, endpoint)
                self.assertNotIn(b"root:", blob)

    def test_assets_refuses_traversal(self):
        for raw in ["../supervisor.py", "../../etc/passwd", "..%2fsupervisor.py",
                    "%2e%2e%2fsupervisor.py", "vendor/../../supervisor.py",
                    "./../supervisor.py", "/etc/passwd"]:
            path = "/assets/" + raw
            status, _, body, blob = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertEqual(body["error"], "no such asset", path)
            self.assertNotIn(b"import http.client", blob, path)
            self.assertNotIn(b"root:", blob, path)

    def test_assets_still_serves_what_it_should(self):
        status, headers, _, blob = self.get("/assets/favicon.svg")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/svg+xml")
        self.assertIn(b"<svg", blob)

    def test_deleting_an_output_outside_the_root_is_refused(self):
        if not self.key:
            self.skipTest("no ACESTEP_API_KEY available")
        for raw in ["/etc/passwd", "../../etc/passwd", "/Volumes/Storage/AIMusic/jobs.db"]:
            status, _, body, _ = request("DELETE", "/v1/outputs?path=" + raw, key=self.key)
            self.assertEqual(status, 404, raw)
            self.assertIs(body["data"]["deleted"], False, raw)
        self.assertTrue(os.path.isfile("/etc/passwd"))

    def test_a_double_encoded_audio_path_is_diagnosed_rather_than_404ed(self):
        if not self.key:
            self.skipTest("no ACESTEP_API_KEY available")
        status, _, body, _ = request(
            "GET", "/v1/audio?path=%252Fv1%252Faudio%253Fpath%253Dx", key=self.key)
        self.assertEqual(status, 400)
        self.assertIn("double-encoded", body["error"])


class TestServedFilesRoundTrip(LiveCase):
    """A file the library lists must be retrievable by the URL it advertises —
    and reading it must not wake the model that made it."""

    def test_the_first_listed_output_can_be_fetched(self):
        status, _, body, _ = self.authed("/v1/outputs?limit=1")
        self.assertEqual(status, 200)
        items = body["data"]["items"]
        if not items:
            self.skipTest("the library is empty on this host")
        item = items[0]
        status, headers, _, blob = self.authed(item["url"])
        self.assertEqual(status, 200)
        self.assertEqual(len(blob), item["bytes"])
        self.assertIn("attachment", headers.get("content-disposition", ""))

    def test_fetching_it_did_not_start_a_backend(self):
        before = self.get("/health")[2]["data"]["services"]
        status, _, body, _ = self.authed("/v1/outputs?limit=1")
        items = body["data"]["items"] if status == 200 else []
        if not items:
            self.skipTest("the library is empty on this host")
        self.authed(items[0]["url"])
        after = self.get("/health")[2]["data"]["services"]
        for name in after:
            self.assertEqual(after[name]["running"], before[name]["running"], name)


class TestOpenAPISpec(LiveCase):
    """The spec the gateway actually serves, against the gateway serving it."""

    @classmethod
    def setUpClass(cls):
        LiveCase.setUpClass()
        cls.spec = request("GET", "/openapi.json")[2]

    def test_the_spec_names_this_host(self):
        urls = [s["url"] for s in self.spec["servers"]]
        self.assertTrue(urls)
        self.assertTrue(any(u.startswith("http") for u in urls))

    def test_it_matches_the_file_in_the_repo(self):
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(here, "openapi.json")) as fh:
            on_disk = json.load(fh)
        # `servers` is filled in per host; everything else must be identical, or
        # the running gateway is not the code in this checkout.
        served = dict(self.spec)
        served.pop("servers")
        on_disk.pop("servers", None)
        self.assertEqual(served, on_disk,
                         "the running gateway is serving a different spec — "
                         "it probably predates the checkout")

    def test_every_documented_path_exists(self):
        """No documented path may 404 as unrouted.

        Only GET operations, and only ones that cannot generate: a documented
        path that the gateway does not know about answers "no service owns X",
        which is the failure being looked for. 401 counts as existing.
        """
        skip = {"/v1/audio"}                 # covered by the traversal tests
        for path, item in self.spec["paths"].items():
            if "get" not in item or path in skip:
                continue
            status, _, body, _ = self.get(path)
            self.assertNotEqual(status, 404, "%s is documented but unrouted" % path)
            if body and body.get("error"):
                self.assertNotIn("no service owns", body["error"], path)

    def test_documented_paths_that_need_a_backend_are_reachable_but_guarded(self):
        # These resolve to a service. Anonymously they must 401 — which proves
        # the route exists without starting anything.
        for path in ["/v1/stats", "/v1/voices"]:
            self.assertIn(path, self.spec["paths"], path)
            self.assertEqual(self.get(path)[0], 401, path)


class TestUI(LiveCase):
    def test_the_page_is_served_from_disk(self):
        status, headers, _, blob = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn(b"<!doctype html", blob[:200].lower())

    def test_the_page_references_only_local_assets(self):
        # The UI is supposed to fetch nothing externally; the two browser
        # libraries are vendored under assets/vendor/.
        blob = self.get("/")[3].decode("utf-8", "replace")
        for marker in ["src=\"https://", "href=\"https://cdn", "unpkg.com"]:
            self.assertNotIn(marker, blob, marker)

    def test_the_vendored_libraries_are_served(self):
        for path in ["/assets/vendor/marked.min.js", "/assets/vendor/dompurify.min.js",
                     "/assets/vendor/katex/katex.min.css"]:
            status, headers, _, blob = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(blob, path)
            self.assertNotEqual(headers["content-type"], "application/octet-stream", path)


class TestNothingWokeUp(LiveCase):
    """The contract of this whole suite: it starts nothing.

    Compared against a baseline taken when the suite began, not against zero —
    this is a desktop, and the owner may well be generating something while the
    tests run. `tearDownModule` repeats the check once everything has finished.
    """

    def test_no_backend_that_was_cold_has_been_started(self):
        woken = woken_since_baseline()
        self.assertEqual(woken, set(), "started by an acceptance test: %s" % woken)


def woken_since_baseline():
    before, after = baseline_services(), running_services()
    if before is None or after is None:                 # pragma: no cover
        return set()
    return after - before


def tearDownModule():
    """The last word: nothing here may leave a model loaded that was not."""
    if baseline_services() is None:                     # suite was skipped
        return
    woken = woken_since_baseline()
    if woken:                                           # pragma: no cover
        raise AssertionError(
            "the acceptance suite started %s — that costs minutes and evicts "
            "resident work, and no test here is allowed to do it" % ", ".join(sorted(woken)))


@unittest.skipUnless(HEAVY, HEAVY_REASON)
class TestGeneration(LiveCase):
    """Only with ANNEAL_TEST_HEAVY=1.

    These load models. On this machine that is three to four minutes for a cold
    start, and starting one heavy service stops the other — so running this
    while something else is generating throws that work away.
    """

    def test_a_short_text_completion(self):
        status, _, body, _ = request(
            "POST", "/v1/text", key=self.key, timeout=900,
            payload={"prompt": "Reply with the single word: ready.", "max_tokens": 16})
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["text"].strip())

    def test_a_small_image(self):
        status, _, body, _ = request(
            "POST", "/v1/images/generations", key=self.key, timeout=1800,
            payload={"prompt": "a grey square", "size": "256x256", "steps": 1,
                     "response_format": "path"})
        self.assertEqual(status, 200)
        self.assertTrue(os.path.isfile(body["data"][0]["path"]))

    def test_a_music_job_is_accepted_and_pollable(self):
        status, _, body, _ = request(
            "POST", "/release_task", key=self.key, timeout=900,
            payload={"prompt": "solo piano, slow", "lyrics": "[instrumental]",
                     "audio_duration": 20, "batch_size": 1})
        self.assertEqual(status, 200)
        self.assertTrue(json.dumps(body))


if __name__ == "__main__":
    unittest.main()
