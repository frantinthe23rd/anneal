"""A backend the supervisor did not start is still a backend that is up.

`stop-api.sh` missed three of the four services, so a restart left the old
process running and reparented to init. The supervisor forwarded to it happily
— `ensure_started()` adopts an open port — but recorded nothing, so every other
decision was made against an empty child handle: `/health` said cold, its
memory was unaccounted for, eviction had no handle on it, the idle reaper
skipped it, and a model switch rewrote the plan without restarting anything.

These use a real process on a real socket, because the thing under test is what
the supervisor believes about the machine.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import unittest

from tests import context  # noqa: F401

import services
import supervisor
from services import TEXT_MODELS


# A backend, in the only sense that matters here: it holds a port and answers.
FAKE_BACKEND = """
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

STATUS = int(sys.argv[2])


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"data": {"loaded": true}, "code": %d}' % STATUS
        self.send_response(STATUS)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Orphans(unittest.TestCase):
    """Spawns processes; kills them however the test ends."""

    def setUp(self):
        self.spawned = []

    def tearDown(self):
        for proc in self.spawned:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    def spawn(self, argv):
        # start_new_session, like the supervisor's own children: its own
        # process group, so stopping it cannot reach the test runner.
        proc = subprocess.Popen(argv, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.spawned.append(proc)
        return proc

    def backend(self, status=200):
        """A live listener on a free port, and the Service pointed at it."""
        port = free_port()
        proc = self.spawn(["/usr/bin/python3", "-c", FAKE_BACKEND,
                           str(port), str(status)])
        svc = supervisor.Service("fake", {
            "port": port, "heavy": False, "idle_timeout": 60, "busy_path": None,
            "cmd": ["/usr/bin/python3", "-c", FAKE_BACKEND],
            "cwd": os.getcwd(), "log": os.devnull,
            "ready_timeout": 10, "routes": [],
        })
        for _ in range(100):
            if svc.port_open():
                break
            time.sleep(0.05)
        return svc, proc


class TestWhatHealthReports(Orphans):
    def test_a_backend_it_did_not_start_is_not_reported_cold(self):
        svc, proc = self.backend()
        status = svc.status()
        self.assertTrue(status["running"], status)
        self.assertEqual(status["state"], "hot", status)
        self.assertEqual(svc.adopted_pid, proc.pid)

    def test_the_status_payload_keeps_its_shape(self):
        svc, _ = self.backend()
        self.assertEqual(sorted(svc.status()), [
            "heavy", "idle_seconds", "idle_timeout_seconds", "in_flight",
            "memory_mb", "peak_memory_mb", "port", "running", "state"])

    def test_memory_can_be_measured_for_it(self):
        """The point of recording the pid: `footprint` needs one. A fake
        backend holds kilobytes, so what is checked is that the process is in
        the set that gets measured — the model weights are not the test."""
        svc, proc = self.backend()
        svc.adopt_running()
        self.assertIn(str(proc.pid), svc._pgid_pids())

    def test_nothing_is_adopted_when_the_port_is_closed(self):
        svc = supervisor.Service("fake", {
            "port": free_port(), "heavy": False, "idle_timeout": 60,
            "busy_path": None, "cmd": ["/bin/true"], "cwd": os.getcwd(),
            "log": os.devnull, "ready_timeout": 1, "routes": [],
        })
        self.assertIsNone(svc.adopt_running())
        self.assertFalse(svc.is_running())
        self.assertEqual(svc.status()["state"], "cold")

    def test_a_corpse_is_not_adopted(self):
        """An open socket is not a live backend — after a stop it can still
        accept for a moment. Only a health response counts."""
        svc, _ = self.backend(status=500)
        self.assertIsNone(svc.adopt_running())
        self.assertFalse(svc.is_running())
        self.assertFalse(svc.status()["running"])


class TestTheIdleReaperAndEviction(Orphans):
    def test_the_check_they_both_key_off_is_true_for_it(self):
        """The reaper skips, and eviction ignores, anything `is_running()` says
        is down. That was every orphan, which is why one lived 17 hours."""
        svc, _ = self.backend()
        self.assertFalse(svc.is_running(), "nothing found it yet")
        svc.refresh_snapshot()          # what the reaper does on every pass
        self.assertTrue(svc.is_running())

    def test_stopping_it_actually_ends_it(self):
        svc, proc = self.backend()
        svc.adopt_running()
        svc.stop("test")
        self.assertIsNotNone(proc.poll(), "the adopted process is still alive")
        self.assertIsNone(svc.adopted_pid)
        self.assertFalse(svc.is_running())
        self.assertEqual(svc.status()["state"], "cold")


class TestTheTextModelSwitch(Orphans):
    """The worst of it: a caller asks for one model and is answered by another.

    `ensure_text_model()` compared the spec with what was asked for, and the
    spec only describes what is running while the supervisor owns it. Against a
    backend it did not start, the comparison rewrote the spec, reported the
    switch as done, and left the previous model serving every request.
    """

    def setUp(self):
        Orphans.setUp(self)
        self.svc = supervisor.SERVICE_OBJECTS["text"]
        self.cmd = self.svc.spec["cmd"]
        self.at = self.cmd.index("--model") + 1
        self.original = self.cmd[self.at]
        self.gemma = services.text_model_path(TEXT_MODELS["gemma"]["repo"])
        self.qwen = services.text_model_path(TEXT_MODELS["qwen-coder"]["repo"])

    def tearDown(self):
        self.cmd[self.at] = self.original
        self.svc.adopted_pid = None
        self.svc._argv = self.svc._argv_pid = None
        Orphans.tearDown(self)

    def orphan_running(self, model_path):
        """A process whose command line says which model it holds, adopted by
        the text service exactly as a leftover backend would be."""
        proc = self.spawn(["/usr/bin/python3", "-c", "import time; time.sleep(120)",
                           "server", "--model", model_path])
        self.svc.adopted_pid = proc.pid
        self.svc._argv = self.svc._argv_pid = None
        return proc

    def test_health_names_the_model_that_is_running(self):
        self.orphan_running(self.gemma)
        self.cmd[self.at] = self.qwen           # what was last asked for
        self.assertEqual(supervisor.loaded_text_model(), "gemma")

    def test_asking_for_another_model_restarts_the_backend(self):
        proc = self.orphan_running(self.gemma)
        self.assertEqual(supervisor.ensure_text_model("qwen-coder"), "qwen-coder")
        self.assertIsNotNone(proc.poll(), "gemma is still up and will answer")
        self.assertEqual(self.cmd[self.at], self.qwen)

    def test_asking_for_the_model_it_is_running_starts_nothing(self):
        proc = self.orphan_running(self.gemma)
        self.cmd[self.at] = self.qwen           # a spec that never happened
        self.assertEqual(supervisor.ensure_text_model("gemma"), "gemma")
        self.assertIsNone(proc.poll(), "restarted for the model it already had")
        self.assertEqual(self.cmd[self.at], self.gemma,
                         "the spec still disagrees with the process")

    def test_with_nothing_running_it_is_the_spec_that_decides(self):
        self.cmd[self.at] = self.gemma
        self.assertFalse(self.svc.is_running())
        self.assertEqual(supervisor.ensure_text_model("qwen-coder"), "qwen-coder")
        self.assertEqual(self.cmd[self.at], self.qwen)
        self.assertEqual(supervisor.loaded_text_model(), "qwen-coder")


if __name__ == "__main__":
    unittest.main()
