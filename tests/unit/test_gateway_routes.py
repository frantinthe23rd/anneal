"""The gateway's own endpoints, served by the real Handler on a throwaway port.

This is `supervisor.Handler` on a real socket, not a mock — but bound to an
ephemeral port with AIMUSIC_ROOT redirected, so it shares nothing with the
gateway running on 8001.

**Nothing here may reach a route that resolves to a backend.** `_proxy()` calls
`start_service()`, which on this machine means a three-to-four minute model load
that evicts whatever else is resident. Every request below is either a route the
gateway answers itself, or one that resolves to no service at all and 404s
before the proxy is entered. `SUPERVISOR_PORT` is pointed at a closed port by
`tests/context`, so even a mistake cannot reach the real gateway.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer

from tests import context

import supervisor

KEY = context.TEST_API_KEY

# Answered by the gateway itself and reachable without credentials. Everything
# else that the gateway owns must refuse an anonymous caller.
PUBLIC = ["/health", "/supervisor/status", "/supervisor/auth", "/supervisor/whoami",
          "/v1/music/tiers", "/openapi.json", "/", "/docs"]

PROTECTED = ["/v1/outputs", "/v1/outputs/file?path=/x", "/v1/images/file?path=/x",
             "/v1/audio?path=/x", "/v1/press", "/v1/press/download?id=x"]


class GatewayCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def get(self, path, key=None, headers=None):
        return self.request("GET", path, key=key, headers=headers)

    def request(self, method, path, key=None, payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            head = dict(headers or {})
            if key:
                head["Authorization"] = "Bearer " + key
            body = None
            if payload is not None:
                body = json.dumps(payload).encode()
                head["Content-Type"] = "application/json"
            conn.request(method, path, body=body, headers=head)
            resp = conn.getresponse()
            raw = resp.read()
            ctype = resp.getheader("Content-Type") or ""
            parsed = None
            if "json" in ctype:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except ValueError:
                    parsed = None
            return resp.status, ctype, parsed, raw
        finally:
            conn.close()


class TestEnvelope(GatewayCase):
    """`{data, code, error}` on success; `{code, error}` on failure, with `code`
    agreeing with the HTTP status. Clients branch on this."""

    def assert_ok_envelope(self, path, key=None):
        status, ctype, body, _ = self.get(path, key=key)
        self.assertEqual(status, 200, path)
        self.assertIn("json", ctype, path)
        self.assertEqual(sorted(body), ["code", "data", "error"], path)
        self.assertEqual(body["code"], 200, path)
        self.assertIsNone(body["error"], path)
        return body["data"]

    def test_health(self):
        data = self.assert_ok_envelope("/health")
        self.assertEqual(data["supervisor"], "ok")
        self.assertEqual(sorted(data["services"]), sorted(supervisor.SERVICE_OBJECTS))
        self.assertIn("system", data)

    def test_supervisor_status_matches_health(self):
        self.assertEqual(sorted(self.assert_ok_envelope("/supervisor/status")),
                         ["services", "supervisor", "system"])

    def test_music_tiers(self):
        data = self.assert_ok_envelope("/v1/music/tiers")
        self.assertEqual(data["default"], supervisor.DEFAULT_MUSIC_TIER)
        self.assertEqual(sorted(data["tiers"]), sorted(supervisor.MUSIC_TIERS))
        for name, tier in data["tiers"].items():
            self.assertEqual(sorted(tier), ["available", "label", "loaded", "model",
                                            "steps", "unavailable_reason"], name)
        self.assertEqual([n for n, t in data["tiers"].items() if t["loaded"]],
                         [supervisor.DEFAULT_MUSIC_TIER])

    def test_auth_reports_anonymity_without_demanding_credentials(self):
        data = self.assert_ok_envelope("/supervisor/auth")
        self.assertIs(data["authenticated"], False)
        self.assertIsNone(data["via"])

    def test_auth_reports_a_valid_key(self):
        data = self.assert_ok_envelope("/supervisor/auth", key=KEY)
        self.assertIs(data["authenticated"], True)
        self.assertEqual(data["via"], "key")

    def test_error_envelope_carries_the_status_in_the_body(self):
        for path, want in [("/assets/nope.png", 404), ("/v1/outputs", 401),
                           ("/nothing/owns/this", 404)]:
            status, _, body, _ = self.get(path)
            self.assertEqual(status, want, path)
            self.assertEqual(body["code"], status, path)
            self.assertTrue(body["error"], path)

    def test_a_listing_comes_back_as_data(self):
        data = self.assert_ok_envelope("/v1/outputs", key=KEY)
        self.assertEqual(sorted(data), ["items", "total"])

    def test_press_list(self):
        data = self.assert_ok_envelope("/v1/press", key=KEY)
        self.assertIn("presses", data)


class TestAuthorisation(GatewayCase):
    def test_public_routes_need_nothing(self):
        for path in PUBLIC:
            status, _, _, _ = self.get(path)
            self.assertEqual(status, 200, path)

    def test_protected_routes_refuse_an_anonymous_caller(self):
        for path in PROTECTED:
            status, _, body, _ = self.get(path)
            self.assertEqual(status, 401, path)
            self.assertEqual(body["error"], "unauthorized", path)

    def test_a_wrong_key_is_no_better_than_none(self):
        for key in ["wrong", KEY + "x", KEY[:-1], ""]:
            status, _, _, _ = self.get("/v1/outputs", key=key or None)
            self.assertEqual(status, 401, repr(key))

    def test_the_right_key_gets_through(self):
        for path in ["/v1/outputs", "/v1/press"]:
            self.assertEqual(self.get(path, key=KEY)[0], 200, path)

    def test_a_bearer_prefix_is_required(self):
        status, _, _, _ = self.request("GET", "/v1/outputs", headers={"Authorization": KEY})
        self.assertEqual(status, 401)

    def test_a_tailnet_identity_authenticates_on_loopback(self):
        # `tailscale serve` stamps this, and it is trusted only because the
        # listener is loopback-only — which it is here.
        self.assertTrue(supervisor.TRUST_TAILSCALE_IDENTITY)
        status, _, body, _ = self.get(
            "/v1/outputs", headers={"Tailscale-User-Login": "someone@example.com"})
        self.assertEqual(status, 200)
        via = self.get("/supervisor/auth",
                       headers={"Tailscale-User-Login": "someone@example.com"})[2]
        self.assertEqual(via["data"]["via"], "tailscale")

    def test_an_allowlist_restricts_which_tailnet_users_are_accepted(self):
        original = supervisor.ALLOWED_LOGINS
        supervisor.ALLOWED_LOGINS = {"allowed@example.com"}
        try:
            self.assertEqual(self.get(
                "/v1/outputs", headers={"Tailscale-User-Login": "denied@example.com"})[0], 401)
            self.assertEqual(self.get(
                "/v1/outputs", headers={"Tailscale-User-Login": "ALLOWED@example.com"})[0], 200)
        finally:
            supervisor.ALLOWED_LOGINS = original

    def test_a_route_no_service_owns_is_404_not_401(self):
        # Current behaviour: the proxy resolves before it authorises, so route
        # existence is discoverable anonymously. Recorded, not endorsed.
        self.assertEqual(self.get("/nothing/owns/this")[0], 404)


class TestAssetContainment(GatewayCase):
    """`/assets/` allows subdirectories (assets/vendor/), so basename() is not
    enough — the resolved path has to land inside the assets directory."""

    def test_a_real_asset_is_served_with_its_content_type(self):
        status, ctype, _, blob = self.get("/assets/favicon.svg")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/svg+xml")
        self.assertTrue(blob)

    def test_a_nested_asset_is_served(self):
        status, ctype, _, _ = self.get("/assets/vendor/katex/katex.min.css")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "text/css; charset=utf-8")

    def test_assets_are_cacheable(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("GET", "/assets/favicon.svg")
        resp = conn.getresponse()
        resp.read()
        self.assertIn("max-age", resp.getheader("Cache-Control") or "")
        conn.close()

    def test_traversal_is_refused(self):
        for path in [
            "/assets/../supervisor.py",
            "/assets/../../etc/passwd",
            "/assets/vendor/../../supervisor.py",
            "/assets/..%2fsupervisor.py",
            "/assets/%2e%2e%2fsupervisor.py",
            "/assets/....//supervisor.py",
            "/assets//etc/passwd",
            "/assets/./../supervisor.py",
        ]:
            status, _, body, blob = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertEqual(body["error"], "no such asset", path)
            self.assertNotIn(b"import http.client", blob, path)

    def test_an_absolute_path_does_not_escape_the_join(self):
        # os.path.join(root, "/etc/passwd") would be "/etc/passwd" without the
        # containment check that follows it.
        status, _, _, _ = self.get("/assets//etc/passwd")
        self.assertEqual(status, 404)

    def test_the_assets_directory_itself_is_not_served(self):
        for path in ["/assets/", "/assets/vendor/", "/assets/vendor"]:
            self.assertEqual(self.get(path)[0], 404, path)

    def test_a_missing_asset_is_404(self):
        self.assertEqual(self.get("/assets/never-existed.png")[0], 404)


class TestFileServingContainment(GatewayCase):
    """Every file-serving endpoint takes a caller-supplied path."""

    ESCAPES = ["/etc/passwd", "/etc/hosts", "../../etc/passwd",
               "%2Fetc%2Fpasswd", "/", ""]

    def test_outputs_file(self):
        for raw in self.ESCAPES:
            status, _, body, _ = self.get("/v1/outputs/file?path=" + raw, key=KEY)
            self.assertEqual(status, 404, raw)
            self.assertEqual(body["error"], "no such output", raw)

    def test_images_file(self):
        for raw in self.ESCAPES:
            status, _, body, _ = self.get("/v1/images/file?path=" + raw, key=KEY)
            self.assertEqual(status, 404, raw)
            self.assertEqual(body["error"], "no such image", raw)

    def test_audio(self):
        for raw in self.ESCAPES:
            status, _, body, _ = self.get("/v1/audio?path=" + raw, key=KEY)
            self.assertEqual(status, 404, raw)
            self.assertIn("no such audio file", body["error"], raw)

    def test_a_double_encoded_audio_path_gets_a_diagnosis_rather_than_a_404(self):
        # The `file` field from /query_result is already a complete request
        # path; re-encoding it is the common client mistake.
        status, _, body, _ = self.get(
            "/v1/audio?path=%252Fv1%252Faudio%253Fpath%253Dx", key=KEY)
        self.assertEqual(status, 400)
        self.assertIn("double-encoded", body["error"])

    def test_traversal_out_of_the_outputs_root_is_refused(self):
        root = os.path.realpath(supervisor.AIMUSIC_ROOT)
        secret = os.path.join(root, "..", "escape-target.txt")
        escape = os.path.join(root, "outputs", "music", "..", "..", "..", "escape-target.txt")
        with open(secret, "w") as fh:
            fh.write("private")
        self.addCleanup(os.unlink, os.path.realpath(secret))
        self.assertEqual(self.get("/v1/outputs/file?path=" + escape, key=KEY)[0], 404)

    def test_a_genuine_output_is_served(self):
        import outputs
        path = outputs.save_bytes("music", b"fLaC-real", ".flac", {"prompt": "a take"})
        self.addCleanup(outputs.delete, path)
        status, ctype, _, blob = self.get("/v1/outputs/file?path=" + path, key=KEY)
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "audio/flac")
        self.assertEqual(blob, b"fLaC-real")


class TestStaticDocuments(GatewayCase):
    def test_the_ui_is_read_from_disk_on_every_request(self):
        status, ctype, _, blob = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "text/html; charset=utf-8")
        self.assertIn(b"<!doctype html", blob[:200].lower())
        with open(supervisor.UI_PATH, "rb") as fh:
            self.assertEqual(blob, fh.read())

    def test_the_ui_is_also_at_slash_ui(self):
        for path in ["/ui", "/ui/"]:
            self.assertEqual(self.get(path)[0], 200, path)

    def test_the_spec_is_served_with_this_hosts_own_servers(self):
        status, ctype, spec, _ = self.get("/openapi.json")
        self.assertEqual(status, 200)
        self.assertIn("json", ctype)
        self.assertIn("paths", spec)
        urls = [s["url"] for s in spec["servers"]]
        self.assertIn("http://127.0.0.1:%d" % supervisor.LISTEN_PORT, urls)

    def test_the_spec_is_also_at_slash_openapi(self):
        self.assertEqual(self.get("/openapi")[0], 200)

    def test_the_docs_page_loads(self):
        status, ctype, _, blob = self.get("/docs")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"/openapi.json", blob)


class TestPressEndpoint(GatewayCase):
    """Only the paths that cannot start a run. POSTing a valid brief would
    launch a Press thread that calls the gateway back and generates for
    minutes; that belongs behind ANNEAL_TEST_HEAVY, not here."""

    def test_a_press_without_a_prompt_is_rejected_before_anything_starts(self):
        for payload in [{}, {"prompt": ""}, {"prompt": "   "}]:
            status, _, body, _ = self.request("POST", "/v1/press", key=KEY, payload=payload)
            self.assertEqual(status, 400, payload)
            self.assertIn("prompt", body["error"])

    def test_an_unknown_press_id_is_404(self):
        for method, path in [("GET", "/v1/press?id=nope"),
                             ("DELETE", "/v1/press?id=nope"),
                             ("GET", "/v1/press/download?id=nope")]:
            status, _, body, _ = self.request(method, path, key=KEY)
            self.assertEqual(status, 404, path)
            self.assertEqual(body["error"], "no such press", path)

    def test_resuming_an_unknown_press_is_404(self):
        status, _, body, _ = self.request("POST", "/v1/press/resume", key=KEY,
                                          payload={"id": "nope"})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "no such press")

    def test_press_endpoints_all_require_a_key(self):
        for method, path in [("POST", "/v1/press"), ("POST", "/v1/press/resume"),
                             ("POST", "/v1/press/cancel"), ("DELETE", "/v1/press?id=x"),
                             ("GET", "/v1/press/download?id=x")]:
            status, _, _, _ = self.request(method, path, payload={})
            self.assertEqual(status, 401, "%s %s" % (method, path))

    def test_download_of_a_press_with_nothing_finished_is_409(self):
        pid = supervisor.PRESSES.create({"prompt": "a brief"})
        self.addCleanup(supervisor.PRESSES.delete, pid)
        status, _, body, _ = self.get("/v1/press/download?id=" + pid, key=KEY)
        self.assertEqual(status, 409)
        self.assertIn("nothing finished", body["error"])

    def test_an_unsupported_download_format_is_rejected(self):
        pid = supervisor.PRESSES.create({"prompt": "a brief"})
        self.addCleanup(supervisor.PRESSES.delete, pid)
        status, _, body, _ = self.get(
            "/v1/press/download?id=%s&format=ogg" % pid, key=KEY)
        self.assertEqual(status, 400)
        self.assertIn("flac, mp3, aac or wav", body["error"])

    def test_deleting_a_press_reports_what_it_removed(self):
        pid = supervisor.PRESSES.create({"prompt": "a brief"})
        status, _, body, _ = self.request("DELETE", "/v1/press?id=" + pid, key=KEY)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"], {"deleted": pid, "files_removed": 0})
        self.assertIsNone(supervisor.PRESSES.get(pid))


class TestOutputsEndpoint(GatewayCase):
    def test_listing_accepts_limit_and_offset(self):
        status, _, body, _ = self.get("/v1/outputs?limit=1&offset=0", key=KEY)
        self.assertEqual(status, 200)
        self.assertLessEqual(len(body["data"]["items"]), 1)

    def test_junk_paging_falls_back_to_the_defaults_rather_than_500ing(self):
        for query in ["?limit=abc", "?offset=abc", "?limit=-5", "?limit=99999"]:
            status, _, body, _ = self.get("/v1/outputs" + query, key=KEY)
            self.assertEqual(status, 200, query)
            self.assertLessEqual(len(body["data"]["items"]), 1000, query)

    def test_deleting_something_outside_outputs_is_refused(self):
        for raw in ["/etc/passwd", "../../etc/passwd", ""]:
            status, _, body, _ = self.request(
                "DELETE", "/v1/outputs?path=" + raw, key=KEY)
            self.assertEqual(status, 404, raw)
            self.assertIs(body["data"]["deleted"], False, raw)
        self.assertTrue(os.path.isfile("/etc/passwd"))

    def test_deleting_a_real_output_works(self):
        import outputs
        path = outputs.save_bytes("music", b"x", ".flac", {"prompt": "doomed"})
        status, _, body, _ = self.request("DELETE", "/v1/outputs?path=" + path, key=KEY)
        self.assertEqual(status, 200)
        self.assertIs(body["data"]["deleted"], True)
        self.assertFalse(os.path.exists(path))


class TestProxyRefusal(GatewayCase):
    """A route no service owns must never reach start_service()."""

    def test_unowned_routes_404(self):
        for path in ["/nope", "/v2/audio", "/typo_release_task", "/favicon.ico"]:
            status, _, body, _ = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertIn("no service owns", body["error"], path)

    def test_an_owned_route_stops_at_the_auth_check(self):
        # Belt and braces: even a route that *does* resolve must be refused
        # before the proxy could start anything, when no key is presented.
        status, _, body, _ = self.get("/v1/stats")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_no_backend_was_started_by_any_test_in_this_module(self):
        # The guard on the whole file: if one of these tests had reached the
        # proxy, a multi-gigabyte model would be loading right now.
        for name, svc in supervisor.SERVICE_OBJECTS.items():
            self.assertFalse(svc.is_running(), name)
            self.assertEqual(svc.epoch, 0, name)


if __name__ == "__main__":
    unittest.main()
