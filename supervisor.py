#!/usr/bin/env python3
"""Anneal — on-demand gateway for the local generation services.

The machine has 16 GB of unified memory. The music model holds ~7 GB once
loaded and the image model about the same, so neither can stay resident and the
two can never be resident together. None of the backends offer idle-unload of
their own, so the only way to give the memory back is to end the process.

This is a small always-on proxy (a few tens of MB) that owns the public port and
manages every backend's lifecycle:

  * request arrives -> start the owning service if needed, wait for it, forward
  * starting a *heavy* service first stops any other heavy service
  * no traffic for the service's idle timeout, and nothing queued -> stop it

Services are declared in services.py; this file is generic.

Local endpoints, answered without waking anything:
  GET  /health                    aggregate state
  GET  /supervisor/status         per-service detail
  POST /supervisor/start          {"service": "music"} — pre-warm, blocks
  POST /supervisor/stop           {"service": "music"} or all — free memory now
  GET  /v1/audio?path=            already-generated music, read off disk

Stdlib only, and 3.9-compatible so it runs on the system python.
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from services import SERVICES, resolve  # noqa: E402

LISTEN_HOST = os.environ.get("SUPERVISOR_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("SUPERVISOR_PORT", "8001"))
API_KEY = os.environ.get("ACESTEP_API_KEY", "")
AIMUSIC_ROOT = os.environ.get("AIMUSIC_ROOT", "/Volumes/Storage/AIMusic")
ACESTEP_DIR = os.environ.get("ACESTEP_DIR", os.path.join(AIMUSIC_ROOT, "ACE-Step-1.5"))

REAP_INTERVAL = 20.0
PROXY_TIMEOUT = float(os.environ.get("PROXY_TIMEOUT", "1800"))

# /v1/audio is served straight off disk, but only from under these roots.
AUDIO_ROOTS = [
    os.path.realpath(os.path.join(ACESTEP_DIR, ".cache")),
    os.path.realpath(os.path.join(ACESTEP_DIR, "gradio_outputs")),
    os.path.realpath(AIMUSIC_ROOT),
]

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

OPENAPI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openapi.json")

# Swagger UI pulled from a CDN — the spec itself is served locally, and this page
# is only ever opened by a browser on the tailnet that has normal internet access.
DOCS_HTML = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Anneal — API docs</title>
    <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
    <style>body { margin: 0 } .topbar { display: none }</style>
  </head>
  <body>
    <div id="swagger"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
      window.ui = SwaggerUIBundle({
        url: "/openapi.json",
        dom_id: "#swagger",
        deepLinking: true,
        persistAuthorization: true,
        defaultModelsExpandDepth: 1,
        tryItOutEnabled: true
      });
    </script>
  </body>
</html>
"""


def log(msg: str) -> None:
    print("[supervisor] %s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class Service:
    """Owns one backend process."""

    def __init__(self, name, spec):
        self.name = name
        self.spec = spec
        self.port = spec["port"]
        self.heavy = spec["heavy"]
        self.proc = None
        self.lock = threading.Lock()
        self.last_activity = time.time()
        self.in_flight = 0
        self.flight_lock = threading.Lock()
        self.peak_rss_mb = 0

    # -- state ------------------------------------------------------------
    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def port_open(self):
        sock = socket.socket()
        sock.settimeout(1.0)
        try:
            sock.connect(("127.0.0.1", self.port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def touch(self):
        self.last_activity = time.time()

    def rss_mb(self):
        """Total RSS of the backend's process group.

        Launchers like `uv run` are thin wrappers — the weights live in a child,
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
    def ensure_started(self):
        with self.lock:
            if self.is_running() and self.port_open():
                return
            if self.port_open():
                log("%s: port %d already open, adopting it" % (self.name, self.port))
                return

            env = dict(os.environ)
            env.update(self.spec.get("env") or {})
            if self.spec.get("port_env"):
                env[self.spec["port_env"]] = str(self.port)

            log("%s: starting on port %d ..." % (self.name, self.port))
            logfile = open(self.spec["log"], "ab")
            self.proc = subprocess.Popen(
                self.spec["cmd"],
                cwd=self.spec["cwd"],
                env=env,
                stdout=logfile,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            timeout = self.spec["ready_timeout"]
            started = time.time()
            while time.time() - started < timeout:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        "%s exited during startup (code %s); see %s"
                        % (self.name, self.proc.returncode, self.spec["log"])
                    )
                if self._get_json("/health", timeout=3.0) is not None:
                    log("%s: ready in %.0fs" % (self.name, time.time() - started))
                    self.touch()
                    return
                time.sleep(1.0)
            raise RuntimeError("%s did not become ready within %ds" % (self.name, timeout))

    def stop(self, reason="idle"):
        with self.lock:
            if not self.is_running():
                self.proc = None
                return
            # Report the high-water mark: MLX hands buffers back once a job
            # finishes, so RSS sampled at stop time understates what was held.
            log("%s: stopping (%s), peak RSS was ~%s MB"
                % (self.name, reason, self.peak_rss_mb or self.rss_mb()))
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
                try:
                    self.proc.wait(timeout=10)
                except Exception:
                    pass
            self.proc = None
            self.peak_rss_mb = 0
            log("%s: stopped" % self.name)

    # -- helpers ----------------------------------------------------------
    def _get_json(self, path, timeout=5.0):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
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

    def has_work(self):
        """True if the backend has queued or running work.

        Errs towards 'busy': if the check can't be made we would rather keep the
        process alive than kill a job mid-generation.
        """
        busy_path = self.spec.get("busy_path")
        if not busy_path:
            return False
        payload = self._get_json(busy_path)
        if payload is None:
            return True
        data = payload.get("data") or {}
        jobs = data.get("jobs") or {}
        return bool(
            jobs.get("queued") or jobs.get("running")
            or data.get("queue_size") or data.get("in_flight")
        )

    def status(self):
        return {
            "running": self.is_running(),
            "heavy": self.heavy,
            "port": self.port,
            "rss_mb": self.rss_mb(),
            "peak_rss_mb": self.peak_rss_mb or None,
            "idle_seconds": round(time.time() - self.last_activity, 1),
            "idle_timeout_seconds": self.spec["idle_timeout"],
            "in_flight": self.in_flight,
        }


SERVICE_OBJECTS = {name: Service(name, spec) for name, spec in SERVICES.items()}
# Serialises heavy-service swaps so two cold requests can't both load 7 GB.
_heavy_lock = threading.Lock()


def start_service(name):
    """Start `name`, first evicting any other heavy service."""
    svc = SERVICE_OBJECTS[name]
    if svc.heavy:
        with _heavy_lock:
            for other_name, other in SERVICE_OBJECTS.items():
                if other_name != name and other.heavy and other.is_running():
                    log("evicting heavy service %r to make room for %r" % (other_name, name))
                    other.stop("evicted by %s" % name)
            svc.ensure_started()
    else:
        svc.ensure_started()


def reaper():
    while True:
        time.sleep(REAP_INTERVAL)
        for name, svc in SERVICE_OBJECTS.items():
            try:
                if not svc.is_running():
                    continue
                sample = svc.rss_mb()
                if sample and sample > svc.peak_rss_mb:
                    svc.peak_rss_mb = sample
                with svc.flight_lock:
                    busy = svc.in_flight > 0
                if busy:
                    svc.touch()
                    continue
                idle_for = time.time() - svc.last_activity
                if idle_for < svc.spec["idle_timeout"]:
                    continue
                if svc.has_work():
                    svc.touch()
                    continue
                svc.stop("idle %.0fs" % idle_for)
            except Exception as exc:  # a reaper crash must not wedge the proxy
                log("reaper error on %s: %r" % (name, exc))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GenSupervisor/2.0"

    def log_message(self, fmt, *args):
        pass

    # -- responses --------------------------------------------------------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not API_KEY:
            return True
        header = self.headers.get("Authorization", "")
        return header.startswith("Bearer ") and header[7:] == API_KEY

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw or b"{}"), raw
        except ValueError:
            return {}, raw

    def _status_payload(self):
        return {
            "supervisor": "ok",
            "services": {name: svc.status() for name, svc in SERVICE_OBJECTS.items()},
        }

    # -- local endpoints --------------------------------------------------
    def _serve_audio_from_disk(self):
        """Serve /v1/audio off disk so downloads never wake the music model."""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
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

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % os.path.basename(path))
        self.end_headers()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        return True

    # -- proxying ---------------------------------------------------------
    def _proxy(self, method, body=None):
        route = urllib.parse.urlparse(self.path).path
        name = resolve(route)
        if name is None:
            self._send_json({"code": 404, "error": "no service owns %s" % route}, 404)
            return

        if body is None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None

        svc = SERVICE_OBJECTS[name]
        try:
            start_service(name)
        except Exception as exc:
            self._send_json({"code": 503, "error": "%s failed to start: %s" % (name, exc)}, 503)
            return

        svc.touch()
        with svc.flight_lock:
            svc.in_flight += 1
        try:
            headers = {}
            for key, value in self.headers.items():
                if key.lower() in HOP_BY_HOP or key.lower() == "host":
                    continue
                headers[key] = value
            headers["Host"] = "127.0.0.1:%d" % svc.port

            conn = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=PROXY_TIMEOUT)
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
            with svc.flight_lock:
                svc.in_flight -= 1
            svc.touch()

    # -- verbs ------------------------------------------------------------
    def _send_bytes(self, blob, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        route = urllib.parse.urlparse(self.path).path

        if route in ("/health", "/supervisor/status"):
            self._send_json({"data": self._status_payload(), "code": 200, "error": None})
            return
        if route in ("/openapi.json", "/openapi"):
            try:
                with open(OPENAPI_PATH, "rb") as fh:
                    self._send_bytes(fh.read(), "application/json")
            except OSError as exc:
                self._send_json({"code": 500, "error": "spec unavailable: %s" % exc}, 500)
            return
        if route in ("/docs", "/docs/", "/"):
            self._send_bytes(DOCS_HTML.encode(), "text/html; charset=utf-8")
            return
        if route == "/v1/audio":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            if self._serve_audio_from_disk():
                return  # served without waking anything
        self._proxy("GET")

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path

        if route in ("/supervisor/start", "/supervisor/stop", "/supervisor/status"):
            payload, _ = self._body()
            if route == "/supervisor/status":
                self._send_json({"data": self._status_payload(), "code": 200, "error": None})
                return
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return

            wanted = payload.get("service")
            if wanted and wanted not in SERVICE_OBJECTS:
                self._send_json({"code": 400, "error": "unknown service %r" % wanted}, 400)
                return

            if route == "/supervisor/start":
                targets = [wanted] if wanted else ["music"]
                try:
                    for name in targets:
                        start_service(name)
                except Exception as exc:
                    self._send_json({"code": 503, "error": str(exc)}, 503)
                    return
            else:
                targets = [wanted] if wanted else list(SERVICE_OBJECTS)
                for name in targets:
                    SERVICE_OBJECTS[name].stop("requested")

            self._send_json({"data": self._status_payload(), "code": 200, "error": None})
            return

        self._proxy("POST")

    def do_PUT(self):
        self._proxy("PUT")

    def do_DELETE(self):
        self._proxy("DELETE")


def main():
    def shutdown(signum, frame):
        log("received signal %d, stopping all services" % signum)
        for svc in SERVICE_OBJECTS.values():
            svc.stop("supervisor exiting")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    threading.Thread(target=reaper, daemon=True).start()

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True
    log("listening on %s:%d — services: %s"
        % (LISTEN_HOST, LISTEN_PORT,
           ", ".join("%s:%d%s" % (n, s.port, "" if s.heavy else " (light)")
                     for n, s in SERVICE_OBJECTS.items())))
    try:
        server.serve_forever()
    finally:
        for svc in SERVICE_OBJECTS.values():
            svc.stop("supervisor exiting")


if __name__ == "__main__":
    main()
