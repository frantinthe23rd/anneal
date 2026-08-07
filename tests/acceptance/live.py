"""Talking to a running gateway.

The key comes from the environment, or from env.local.sh if the shell did not
already export it. It is read into memory and never written anywhere — not into
a failure message, not into a log line, not into a file.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import unittest
import urllib.parse

BASE = os.environ.get("ANNEAL_TEST_BASE", "http://127.0.0.1:8001")

# Generation costs three to four minutes and evicts whatever model is resident.
# Nothing that triggers it runs unless this is set.
HEAVY = os.environ.get("ANNEAL_TEST_HEAVY") == "1"
HEAVY_REASON = ("would load a model — costs minutes and evicts other work; "
                "set ANNEAL_TEST_HEAVY=1 to run")

_KEY = {}


def api_key():
    """The gateway's API key, from the environment or env.local.sh."""
    if "value" in _KEY:
        return _KEY["value"]
    key = os.environ.get("ACESTEP_API_KEY", "")
    if not key:
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        secrets = os.path.join(here, "env.local.sh")
        try:
            with open(secrets) as fh:
                for line in fh:
                    match = re.match(r"""\s*(?:export\s+)?ACESTEP_API_KEY=["']?([^"'\s#]+)""", line)
                    if match:
                        key = match.group(1)
                        break
        except OSError:
            key = ""
    _KEY["value"] = key
    return key


def reachable():
    try:
        status, _, _, _ = request("GET", "/health", timeout=3)
        return status == 200
    except Exception:
        return False


def request(method, path, key=None, payload=None, headers=None, timeout=30):
    """One request. Returns (status, headers dict, parsed JSON or None, raw)."""
    parts = urllib.parse.urlparse(BASE)
    conn_class = (http.client.HTTPSConnection if parts.scheme == "https"
                  else http.client.HTTPConnection)
    conn = conn_class(parts.hostname, parts.port, timeout=timeout)
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
        got = {k.lower(): v for k, v in resp.getheaders()}
        parsed = None
        if "json" in (got.get("content-type") or ""):
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except ValueError:
                parsed = None
        return resp.status, got, parsed, raw
    finally:
        conn.close()


_BASELINE = {}


def running_services():
    """Which backends are up right now, as a set of names."""
    try:
        status, _, body, _ = request("GET", "/health", timeout=10)
    except Exception:
        return None
    if status != 200 or not body:
        return None
    return {name for name, svc in body["data"]["services"].items() if svc["running"]}


def baseline_services():
    """What was already running when this suite started.

    Anneal is a desktop the owner also uses. Asserting that nothing at all is
    loaded would fail whenever they happen to be generating, which is a test
    that cries wolf. What this suite actually promises is narrower and testable:
    it starts nothing that was not already started.
    """
    if "value" not in _BASELINE:
        _BASELINE["value"] = running_services()
    return _BASELINE["value"]


class LiveCase(unittest.TestCase):
    """Skips itself, with a reason, when nothing is listening."""

    @classmethod
    def setUpClass(cls):
        if not reachable():
            raise unittest.SkipTest("no gateway answering /health at %s" % BASE)
        cls.key = api_key()
        baseline_services()

    def get(self, path, key=None, headers=None, timeout=30):
        return request("GET", path, key=key, headers=headers, timeout=timeout)

    def authed(self, path, timeout=30):
        if not self.key:
            self.skipTest("no ACESTEP_API_KEY available (env or env.local.sh)")
        return request("GET", path, key=self.key, timeout=timeout)

    def assertEnvelope(self, status, body, expect=200, path=""):
        """`{data, code, error}` on success, `{code, error}` on failure, with
        `code` agreeing with the HTTP status."""
        self.assertEqual(status, expect, path)
        self.assertIsNotNone(body, "%s did not return JSON" % path)
        self.assertEqual(body.get("code"), status, path)
        if status == 200:
            self.assertIn("data", body, path)
            self.assertIsNone(body.get("error"), path)
        else:
            self.assertTrue(body.get("error"), path)
