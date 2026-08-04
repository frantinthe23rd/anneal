#!/usr/bin/env python3
"""On-demand supervisor for the ACE-Step music API.

ACE-Step holds ~7 GB resident once its models are loaded, which is a lot to keep
pinned on a 16 GB machine. It has no idle-unload of its own, so the only way to
actually give the memory back is to end the process.

This is a tiny always-on proxy (a few tens of MB) that owns the public port:

  * a request arrives -> start the real server if it isn't up, wait for it, forward
  * no requests for IDLE_TIMEOUT, and no queued/running jobs -> stop it

So the model is resident only while it is being used. The cost is a cold start of
roughly 3-4 minutes on the first request after an idle period; while warm,
subsequent requests are immediate.

Two things are answered locally so they never wake the model:
  * GET /health          - supervisor + backend state
  * GET /v1/audio?path=  - already-generated files are just read off disk

Extra endpoints:
  * GET/POST /supervisor/status  - state, idle seconds, backend RSS
  * POST     /supervisor/start   - pre-warm (blocks until models are loaded)
  * POST     /supervisor/stop    - stop the backend now, releasing memory

Stdlib only, and kept 3.9-compatible so it can run on the system python.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = os.environ.get("SUPERVISOR_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("SUPERVISOR_PORT", "8001"))
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = int(os.environ.get("ACESTEP_BACKEND_PORT", "8011"))

ACESTEP_DIR = os.environ.get("ACESTEP_DIR", "/Volumes/Storage/AIMusic/ACE-Step-1.5")
UV_BIN = os.environ.get("UV_BIN", "/opt/homebrew/bin/uv")
API_KEY = os.environ.get("ACESTEP_API_KEY", "")

IDLE_TIMEOUT = float(os.environ.get("ACESTEP_IDLE_TIMEOUT", "600"))
REAP_INTERVAL = 20.0
# Cold start loads ~9.4 GB of weights; the first generation request blocks on it.
BACKEND_READY_TIMEOUT = float(os.environ.get("ACESTEP_READY_TIMEOUT", "900"))
PROXY_TIMEOUT = float(os.environ.get("ACESTEP_PROXY_TIMEOUT", "1800"))

# /v1/audio is served straight off disk, but only from under these roots.
AUDIO_ROOTS = [
    os.path.realpath(os.path.join(ACESTEP_DIR, ".cache")),
    os.path.realpath(os.path.join(ACESTEP_DIR, "gradio_outputs")),
    os.path.realpath("/Volumes/Storage/AIMusic"),
]

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


def log(msg: str) -> None:
    print("[supervisor] %s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class Backend:
    """Owns the ACE-Step server process lifecycle."""

    def __init__(self) -> None:
        self.proc = None  # type: ignore[var-annotated]
        self.lock = threading.Lock()
        self.last_activity = time.time()
        self.in_flight = 0
        self.flight_lock = threading.Lock()
        self.peak_rss_mb = 0

    # -- state ------------------------------------------------------------
    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def port_open(self) -> bool:
        sock = socket.socket()
        sock.settimeout(1.0)
        try:
            sock.connect((BACKEND_HOST, BACKEND_PORT))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def touch(self) -> None:
        self.last_activity = time.time()

    def rss_mb(self):
        """Total RSS of the whole backend process group.

        `uv run acestep-api` is a thin wrapper; the weights live in its child,
        so measuring only self.proc reports a misleading few MB.
        """
        if not self.is_running():
            return None
        try:
            pgid = os.getpgid(self.proc.pid)
            out = subprocess.check_output(["ps", "-A", "-o", "pgid=,rss="], text=True)
        except Exception:
            return None
        total = 0
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                if int(parts[0]) == pgid:
                    total += int(parts[1])
        return total // 1024 if total else None

    # -- lifecycle --------------------------------------------------------
    def ensure_started(self) -> None:
        """Start the backend and block until it answers /health."""
        with self.lock:
            if self.is_running() and self.port_open():
                return
            if self.port_open():
                # Something else already holds the port (e.g. a manual run).
                log("backend port already open, adopting it")
                return

            env = dict(os.environ)
            env["ACESTEP_API_HOST"] = BACKEND_HOST
            env["ACESTEP_API_PORT"] = str(BACKEND_PORT)

            log("starting backend on port %d ..." % BACKEND_PORT)
            self.proc = subprocess.Popen(
                [UV_BIN, "run", "acestep-api"],
                cwd=ACESTEP_DIR,
                env=env,
                stdout=open(os.path.join(os.path.dirname(ACESTEP_DIR), "api-server.log"), "ab"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            deadline = time.time() + BACKEND_READY_TIMEOUT
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        "backend exited during startup (code %s); see api-server.log"
                        % self.proc.returncode
                    )
                if self._health_ok():
                    log("backend ready (%.0fs)" % (BACKEND_READY_TIMEOUT - (deadline - time.time())))
                    self.touch()
                    return
                time.sleep(1.0)
            raise RuntimeError("backend did not become ready within %ds" % BACKEND_READY_TIMEOUT)

    def stop(self, reason: str = "idle") -> None:
        with self.lock:
            if not self.is_running():
                self.proc = None
                return
            # Report the high-water mark: MLX hands its buffers back once a job
            # finishes, so RSS sampled at stop time understates what was held.
            log("stopping backend (%s), peak RSS was ~%s MB" % (reason, self.peak_rss_mb or self.rss_mb()))
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            for _ in range(30):
                if self.proc.poll() is not None:
                    break
                time.sleep(1.0)
            if self.proc.poll() is None:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                self.proc.wait(timeout=10)
            self.proc = None
            self.peak_rss_mb = 0
            log("backend stopped")

    # -- helpers ----------------------------------------------------------
    def _get_json(self, path: str, timeout: float = 5.0):
        conn = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=timeout)
        try:
            headers = {"Authorization": "Bearer %s" % API_KEY} if API_KEY else {}
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                return None
            return json.loads(body.decode("utf-8"))
        except Exception:
            return None
        finally:
            conn.close()

    def _health_ok(self) -> bool:
        return self._get_json("/health", timeout=3.0) is not None

    def has_work(self) -> bool:
        """True if the backend has queued or running jobs (never kill mid-generation)."""
        stats = self._get_json("/v1/stats")
        if stats is None:
            # Can't tell — assume busy rather than risk killing a live job.
            return True
        data = stats.get("data") or {}
        jobs = data.get("jobs") or {}
        return bool(
            jobs.get("queued") or jobs.get("running") or data.get("queue_size")
        )


backend = Backend()


def reaper() -> None:
    while True:
        time.sleep(REAP_INTERVAL)
        try:
            if not backend.is_running():
                continue
            sample = backend.rss_mb()
            if sample and sample > backend.peak_rss_mb:
                backend.peak_rss_mb = sample
            with backend.flight_lock:
                busy = backend.in_flight > 0
            if busy:
                backend.touch()
                continue
            idle_for = time.time() - backend.last_activity
            if idle_for < IDLE_TIMEOUT:
                continue
            if backend.has_work():
                backend.touch()
                continue
            backend.stop("idle %.0fs" % idle_for)
        except Exception as exc:  # a reaper crash must not wedge the proxy
            log("reaper error: %r" % exc)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ACEStepSupervisor/1.0"

    def log_message(self, fmt, *args):  # quieter default logging
        pass

    # -- responses --------------------------------------------------------
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not API_KEY:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and header[7:] == API_KEY:
            return True
        return False

    # -- local endpoints --------------------------------------------------
    def _status_payload(self):
        return {
            "supervisor": "ok",
            "backend_running": backend.is_running(),
            "backend_rss_mb": backend.rss_mb(),
            "backend_peak_rss_mb": backend.peak_rss_mb or None,
            "idle_seconds": round(time.time() - backend.last_activity, 1),
            "idle_timeout_seconds": IDLE_TIMEOUT,
            "in_flight": backend.in_flight,
        }

    def _serve_audio_from_disk(self) -> bool:
        """Serve /v1/audio straight off disk so downloads don't wake the model."""
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        raw = (params.get("path") or [""])[0]
        if not raw:
            return False
        path = os.path.realpath(raw)
        if not any(path.startswith(root + os.sep) for root in AUDIO_ROOTS):
            return False
        if not os.path.isfile(path):
            return False

        ctype = {
            ".mp3": "audio/mpeg", ".flac": "audio/flac", ".wav": "audio/wav",
            ".opus": "audio/opus", ".aac": "audio/aac", ".m4a": "audio/mp4",
        }.get(os.path.splitext(path)[1].lower(), "application/octet-stream")

        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % os.path.basename(path))
        self.end_headers()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        return True

    # -- proxying ---------------------------------------------------------
    def _proxy(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        try:
            backend.ensure_started()
        except Exception as exc:
            self._send_json({"code": 503, "error": "backend start failed: %s" % exc}, 503)
            return

        backend.touch()
        with backend.flight_lock:
            backend.in_flight += 1
        try:
            headers = {}
            for key, value in self.headers.items():
                if key.lower() in HOP_BY_HOP or key.lower() == "host":
                    continue
                headers[key] = value
            headers["Host"] = "%s:%d" % (BACKEND_HOST, BACKEND_PORT)

            conn = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=PROXY_TIMEOUT)
            try:
                conn.request(method, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                payload = resp.read()

                self.send_response(resp.status)
                for key, value in resp.getheaders():
                    if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            finally:
                conn.close()
        except Exception as exc:
            self._send_json({"code": 502, "error": "proxy error: %s" % exc}, 502)
        finally:
            with backend.flight_lock:
                backend.in_flight -= 1
            backend.touch()

    # -- verbs ------------------------------------------------------------
    def do_GET(self) -> None:
        route = urllib.parse.urlparse(self.path).path

        if route == "/health":
            self._send_json({"data": self._status_payload(), "code": 200, "error": None})
            return
        if route == "/supervisor/status":
            self._send_json({"data": self._status_payload(), "code": 200, "error": None})
            return
        if route == "/v1/audio":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            if self._serve_audio_from_disk():
                return  # served without waking the backend
        self._proxy("GET")

    def do_POST(self) -> None:
        route = urllib.parse.urlparse(self.path).path

        if route == "/supervisor/status":
            self._send_json({"data": self._status_payload(), "code": 200, "error": None})
            return
        if route == "/supervisor/start":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            try:
                backend.ensure_started()
            except Exception as exc:
                self._send_json({"code": 503, "error": str(exc)}, 503)
                return
            self._send_json({"data": self._status_payload(), "code": 200, "error": None})
            return
        if route == "/supervisor/stop":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            backend.stop("requested")
            self._send_json({"data": self._status_payload(), "code": 200, "error": None})
            return
        self._proxy("POST")

    def do_PUT(self) -> None:
        self._proxy("PUT")

    def do_DELETE(self) -> None:
        self._proxy("DELETE")


def main() -> None:
    def shutdown(signum, frame):
        log("received signal %d, stopping backend" % signum)
        backend.stop("supervisor exiting")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    threading.Thread(target=reaper, daemon=True).start()

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True
    log("listening on %s:%d -> backend %s:%d (idle timeout %.0fs)"
        % (LISTEN_HOST, LISTEN_PORT, BACKEND_HOST, BACKEND_PORT, IDLE_TIMEOUT))
    try:
        server.serve_forever()
    finally:
        backend.stop("supervisor exiting")


if __name__ == "__main__":
    main()
