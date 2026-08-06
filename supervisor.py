#!/usr/bin/env python3
"""Anneal — on-demand gateway for the local generation services.

The machine has 16 GB of unified memory. Measured by phys_footprint, the music
model holds ~21 GB once loaded and the image model ~11 GB, so music necessarily
swaps and the two can never be resident together. None of the backends offer
idle-unload of their own, so the only way to give the memory back is to end the
process.

(RSS is worthless for measuring this: MLX allocates through Metal, so `ps`
reports ~120 MB for a backend actually holding 21 GB. See Service.memory_mb.)

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
from services import SERVICES, MUSIC_TIERS, DEFAULT_MUSIC_TIER, resolve  # noqa: E402
from jobstore import JobStore  # noqa: E402
import outputs  # noqa: E402
from builder import Press, PressStore  # noqa: E402

LISTEN_HOST = os.environ.get("SUPERVISOR_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("SUPERVISOR_PORT", "8001"))
API_KEY = os.environ.get("ACESTEP_API_KEY", "")
AIMUSIC_ROOT = os.environ.get("AIMUSIC_ROOT", "/Volumes/Storage/AIMusic")
ACESTEP_DIR = os.environ.get("ACESTEP_DIR", os.path.join(AIMUSIC_ROOT, "ACE-Step-1.5"))
TAILNET_HOST = os.environ.get("TAILNET_HOST", "")

# mlx_lm names its model by the path it loaded, so the gateway rewrites the
# `model` field of completion requests to match. Callers send anything.
def _text_model_path():
    cmd = SERVICES.get("text", {}).get("cmd") or []
    return cmd[cmd.index("--model") + 1] if "--model" in cmd else ""


TEXT_MODEL_PATH = _text_model_path()
TEXT_MODEL_NAME = os.path.basename(os.path.dirname(os.path.dirname(TEXT_MODEL_PATH))) \
    .replace("models--", "").replace("--", "/") if TEXT_MODEL_PATH else ""

REAP_INTERVAL = 20.0
PROXY_TIMEOUT = float(os.environ.get("PROXY_TIMEOUT", "1800"))

# Refuse to start a heavy model below this much free RAM. Deliberately modest:
# these models are expected to consume most of the machine, so this guards
# against "nothing left at all", not against "it will be tight".
MIN_FREE_MB_FOR_HEAVY = int(os.environ.get("ANNEAL_MIN_FREE_MB", "1200"))

# `tailscale serve` stamps Tailscale-User-* headers on what it proxies, which
# lets the browser authenticate as a tailnet user instead of holding a token.
# Only safe while we listen on loopback, because then the serve proxy is the
# only way in; on any other bind address those headers could be forged.
TRUST_TAILSCALE_IDENTITY = LISTEN_HOST in ("127.0.0.1", "localhost", "::1")
# Comma-separated logins to accept. Empty means any tailnet member, which is the
# right default for a personal tailnet — reaching the port at all requires being
# on it. Set ANNEAL_ALLOWED_LOGINS to restrict on a shared tailnet.
ALLOWED_LOGINS = {
    s.strip().lower() for s in os.environ.get("ANNEAL_ALLOWED_LOGINS", "").split(",") if s.strip()
}

# Generated files are served straight off disk, but only from under these roots.
# Reading back an old result must never wake the model that made it.
AUDIO_ROOTS = [
    os.path.realpath(os.path.join(ACESTEP_DIR, ".cache")),
    os.path.realpath(os.path.join(ACESTEP_DIR, "gradio_outputs")),
    os.path.realpath(AIMUSIC_ROOT),
]
IMAGE_ROOTS = [os.path.realpath(os.path.join(AIMUSIC_ROOT, "outputs"))]

CONTENT_TYPES = {
    ".mp3": "audio/mpeg", ".flac": "audio/flac", ".wav": "audio/wav",
    ".opus": "audio/opus", ".aac": "audio/aac", ".m4a": "audio/mp4",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
}

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

HERE = os.path.dirname(os.path.abspath(__file__))
OPENAPI_PATH = os.path.join(HERE, "openapi.json")
UI_PATH = os.path.join(HERE, "ui.html")

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


def _is_stream(resp):
    """Whether a backend response should be relayed chunk by chunk.

    Server-sent events, or any response with no declared length — both mean the
    body arrives over time and must not be collected before forwarding.
    """
    ctype = (resp.getheader("Content-Type") or "").lower()
    if "text/event-stream" in ctype:
        return True
    return resp.getheader("Content-Length") is None and resp.getheader("Transfer-Encoding")


def log(msg: str) -> None:
    print("[supervisor] %s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


_SIZE_UNITS = {"B": 1.0 / (1024 * 1024), "KB": 1.0 / 1024, "MB": 1.0, "GB": 1024.0, "TB": 1024.0 * 1024}


def _parse_size_mb(text):
    """'20 GB' / '512 MB' -> megabytes."""
    parts = text.replace(" ", " ").split()
    if not parts:
        return 0.0
    try:
        value = float(parts[0].replace(",", ""))
    except ValueError:
        return 0.0
    unit = (parts[1].upper() if len(parts) > 1 else "MB")
    return value * _SIZE_UNITS.get(unit, 1.0)


_page_sample = {"t": 0.0, "pageouts": 0, "pageins": 0}


def _pressure_level():
    """macOS's own verdict: 1 normal, 2 warning, 4 critical.

    Far better than anything inferred from free bytes, because the kernel knows
    what it is doing with compression and what it can reclaim.
    """
    try:
        out = subprocess.check_output(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], text=True)
        return int(out.strip())
    except Exception:
        return 1


def system_memory():
    """Memory state, judged by activity rather than by how much swap exists.

    A large swap *file* is not a problem. This machine routinely runs a model
    whose footprint exceeds physical RAM, macOS pages it out once, and then sits
    at a pageout rate of about one page per second — which is why it feels
    completely normal in use. Alarming on swap volume meant warning during
    ordinary operation, and a warning that fires when nothing is wrong is worse
    than none: it teaches you to ignore the one that matters.

    What actually indicates trouble is sustained paging, or the kernel itself
    saying so.
    """
    info = {}
    try:
        out = subprocess.check_output(["vm_stat"], text=True)
        page, stats = 16384, {}
        for line in out.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            # The header line's "value" is prose — skip it rather than losing
            # the whole read.
            try:
                stats[k.strip()] = int(v.strip().rstrip(".").replace(",", "") or 0)
            except ValueError:
                continue
        free = (stats.get("Pages free", 0) + stats.get("Pages inactive", 0)) * page
        info["free_mb"] = int(free / (1024 * 1024))

        # Paging *rate* between calls — the flow, not the stock. Both
        # directions: under load here pageouts stay near zero while pageins hit
        # thousands per second, because the working set is being re-read from
        # swap rather than newly written to it. Watching only pageouts would
        # have missed the entire effect.
        now = time.time()
        outs, ins = stats.get("Pageouts", 0), stats.get("Pageins", 0)
        prev_t = _page_sample["t"]
        if prev_t and now > prev_t:
            if outs >= _page_sample["pageouts"]:
                info["pageouts_per_sec"] = int((outs - _page_sample["pageouts"]) / (now - prev_t))
            if ins >= _page_sample["pageins"]:
                info["pageins_per_sec"] = int((ins - _page_sample["pageins"]) / (now - prev_t))
        _page_sample.update({"t": now, "pageouts": outs, "pageins": ins})
    except Exception:
        pass

    try:
        out = subprocess.check_output(["sysctl", "-n", "vm.swapusage"], text=True)
        parts = dict(zip(out.replace("=", " ").split()[0::2],
                         out.replace("=", " ").split()[1::2]))
        info["swap_used_mb"] = int(float(parts.get("used", "0M").rstrip("M")))
        info["swap_total_mb"] = int(float(parts.get("total", "0M").rstrip("M")))
    except Exception:
        pass

    level = _pressure_level()
    info["pressure_level"] = {1: "normal", 2: "warning", 4: "critical"}.get(level, "normal")
    # Trust the kernel first. The rate thresholds are a backstop for the case
    # where it has not caught up yet; pagein is the direction that actually
    # moves under a model load on this machine.
    info["pressure"] = bool(
        level >= 2
        or info.get("pageins_per_sec", 0) > 5000
        or info.get("pageouts_per_sec", 0) > 2000
    )
    return info


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
        # Bumped every time the backend starts. Job ids issued under an older
        # epoch cannot still be queued — the in-memory queue died with the
        # previous process. This is what lets us spot orphaned jobs.
        self.epoch = 0
        self.started_at = 0.0
        self._snapshot = {"state": "cold", "memory_mb": None}
        self._snapshot_at = 0.0

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

    def _pgid_pids(self):
        try:
            pgid = os.getpgid(self.proc.pid)
            out = subprocess.check_output(["ps", "-A", "-o", "pgid=,pid="], text=True)
        except Exception:
            return []
        pids = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                if int(parts[0]) == pgid:
                    pids.append(parts[1])
        return pids

    def memory_mb(self):
        """Physical footprint of the backend, in MB.

        RSS is useless here. MLX allocates its tensors through Metal/IOKit, which
        `ps` does not attribute to the process: a music backend holding ~20 GB
        reports an RSS of ~120 MB, and the number jitters as ordinary heap moves
        around. `footprint` reports phys_footprint — the same figure Activity
        Monitor shows — which actually tracks the model.

        Costs ~150 ms per process, so callers should use the cached snapshot
        rather than invoking this on every request.
        """
        if not self.is_running():
            return None
        total = 0
        for pid in self._pgid_pids():
            try:
                out = subprocess.check_output(
                    ["/usr/bin/footprint", "-p", pid], text=True, stderr=subprocess.DEVNULL
                )
            except Exception:
                continue
            for line in out.splitlines():
                if "phys_footprint:" in line and "peak" not in line:
                    total += _parse_size_mb(line.split(":", 1)[1].strip())
                    break
        return int(total) if total else None

    def model_state(self):
        """cold | heating | hot.

        `running` only means the process answered — for music the weights then
        load lazily for another three or four minutes. Reporting that as "hot"
        told users the model was ready when it was not.
        """
        if not self.is_running():
            return "cold"
        payload = self._get_json(self.spec.get("health_path", "/health"), timeout=2.0)
        if payload is None:
            return "heating"
        data = payload.get("data", payload) or {}
        for key in ("models_initialized", "loaded"):
            if key in data:
                return "hot" if data[key] else "heating"
        return "hot"

    # -- lifecycle --------------------------------------------------------
    def ensure_started(self):
        with self.lock:
            if self.is_running() and self.port_open():
                return
            if self.port_open():
                # A TCP connect alone is not proof of a live backend: straight
                # after a stop the socket can still accept briefly, and we would
                # "adopt" a corpse. Require a health response before believing it.
                if self._get_json(self.spec.get("health_path", "/health"), timeout=3.0) is not None:
                    log("%s: port %d already open, adopting it" % (self.name, self.port))
                    return
                log("%s: port %d open but not healthy; starting our own" % (self.name, self.port))

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
                if self._get_json(self.spec.get("health_path", "/health"), timeout=3.0) is not None:
                    log("%s: ready in %.0fs" % (self.name, time.time() - started))
                    self.epoch += 1
                    self.started_at = time.time()
                    # Force the next status read to re-measure: a cached "cold"
                    # would otherwise linger for the cache window after start.
                    self._snapshot_at = 0.0
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
            log("%s: stopping (%s), peak memory was ~%s MB"
                % (self.name, reason, self.peak_rss_mb or self.memory_mb()))
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
            self._snapshot = {"state": "cold", "memory_mb": None}
            self._snapshot_at = time.time()
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

    def refresh_snapshot(self):
        """Recompute state + memory. ~150ms per process, so call it sparingly."""
        state = self.model_state()
        mem = self.memory_mb() if state != "cold" else None
        if mem and mem > self.peak_rss_mb:
            self.peak_rss_mb = mem
        self._snapshot = {"state": state, "memory_mb": mem}
        self._snapshot_at = time.time()
        return self._snapshot

    def snapshot(self, max_age=8.0):
        if not self.is_running():
            self._snapshot = {"state": "cold", "memory_mb": None}
            self._snapshot_at = time.time()
        elif time.time() - self._snapshot_at > max_age:
            self.refresh_snapshot()
        return self._snapshot

    def status(self):
        snap = self.snapshot()
        return {
            "running": self.is_running(),
            "state": snap["state"],                 # cold | heating | hot
            "heavy": self.heavy,
            "port": self.port,
            "memory_mb": snap["memory_mb"],         # phys_footprint, not RSS
            "peak_memory_mb": self.peak_rss_mb or None,
            "idle_seconds": round(time.time() - self.last_activity, 1),
            "idle_timeout_seconds": self.spec["idle_timeout"],
            "in_flight": self.in_flight,
        }


SERVICE_OBJECTS = {name: Service(name, spec) for name, spec in SERVICES.items()}
# Serialises heavy-service swaps so two cold requests can't both start loading.
_heavy_lock = threading.Lock()

# task_id -> music epoch it was issued under. ACE-Step's queue lives in memory,
# so anything issued under an older epoch is gone; the backend cannot tell the
# difference and keeps reporting status 0 (queued), which reads as "just slow"
# forever. We remember what we handed out so we can say so.
_ISSUED = {}
_issued_lock = threading.Lock()
MAX_TRACKED_JOBS = 5000

JOBS = JobStore(os.path.join(AIMUSIC_ROOT, "jobs.db"))
PRESSES = PressStore(os.path.join(AIMUSIC_ROOT, "presses.db"))


def _local_json(path, payload=None, method=None, timeout=1800):
    """Call our own gateway on loopback.

    Press goes back through the front door rather than reaching into the
    services directly, so it inherits tier switching, admission control, the
    durable job queue and library persistence without duplicating any of it.
    """
    body = json.dumps(payload).encode() if payload is not None else None
    conn = http.client.HTTPConnection("127.0.0.1", LISTEN_PORT, timeout=timeout)
    try:
        headers = {"Content-Type": "application/json"}
        if API_KEY:
            headers["Authorization"] = "Bearer %s" % API_KEY
        conn.request(method or ("POST" if body else "GET"), path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status >= 400:
            raise RuntimeError("%s -> %d %s" % (path, resp.status, raw[:200].decode("utf-8", "replace")))
        return json.loads(raw.decode("utf-8"))
    finally:
        conn.close()


def press_text(prompt, max_tokens=900):
    d = _local_json("/v1/text", {"prompt": prompt, "max_tokens": max_tokens,
                                 "temperature": 0.9, "thinking": False})
    return ((d or {}).get("data") or {}).get("text", "")


def press_music(payload):
    """Submit and wait. Poll rather than block so a stalled backend surfaces."""
    task_id = ((_local_json("/release_task", payload) or {}).get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError("no task_id returned")
    deadline = time.time() + 2400
    while time.time() < deadline:
        time.sleep(6)
        try:
            rows = (_local_json("/query_result", {"task_id_list": [task_id]}) or {}).get("data") or []
        except Exception:
            continue                      # transient; never resubmit
        row = next((r for r in rows if r.get("task_id") == task_id), None)
        if not row:
            continue
        if row.get("status") == 2:
            raise RuntimeError(str(row.get("result"))[:200])
        if row.get("status") == 1:
            return json.loads(row["result"]) if isinstance(row.get("result"), str) else []
    raise RuntimeError("timed out waiting for track")


def press_image(prompt, size):
    d = _local_json("/v1/images/generations",
                    {"prompt": prompt, "size": size, "steps": 4, "n": 1,
                     "response_format": "path"})
    items = (d or {}).get("data") or []
    return items[0] if items else None


PRESS = Press(PRESSES, press_text, press_music, press_image, log=log)


def replay_pending_music_jobs():
    """Resubmit jobs that were outstanding when the backend last stopped.

    Callers keep polling the id they were originally given; set_alias maps that
    to the new id in both directions, so from outside the job simply took a
    while. Runs in a thread so a slow replay never blocks the request that
    triggered the start.
    """
    music = SERVICE_OBJECTS["music"]
    outstanding = JOBS.pending()
    if not outstanding:
        return
    log("replaying %d outstanding music job(s) after restart" % len(outstanding))

    for original_id, payload, attempts in outstanding:
        if not music.is_running():
            return                       # stopped again; leave the rest pending
        try:
            body = json.dumps(payload).encode()
            conn = http.client.HTTPConnection("127.0.0.1", music.port, timeout=120)
            try:
                headers = {"Content-Type": "application/json"}
                if API_KEY:
                    headers["Authorization"] = "Bearer %s" % API_KEY
                conn.request("POST", "/release_task", body=body, headers=headers)
                resp = conn.getresponse()
                data = json.loads(resp.read().decode("utf-8")).get("data") or {}
            finally:
                conn.close()
            new_id = data.get("task_id") or data.get("taskId")
            if not new_id:
                continue
            # Count the attempt only once the backend has actually accepted the
            # job. A failed connection means it was never tried, and shouldn't
            # burn one of the few retries a crash-looping job is allowed.
            JOBS.bump_attempt(original_id)
            JOBS.set_alias(original_id, new_id)
            _record_issued(new_id)
            log("replayed %s -> %s (attempt %d)" % (original_id, new_id, attempts + 1))
        except Exception as exc:
            log("replay of %s failed: %r" % (original_id, exc))
# Grace period after a backend start before we trust /v1/stats to say "idle" —
# a job can be accepted a moment before it shows up in the queue counters.
ORPHAN_GRACE_SECONDS = 20.0


def _record_issued(task_id):
    if not task_id:
        return
    with _issued_lock:
        if len(_ISSUED) >= MAX_TRACKED_JOBS:
            for stale in list(_ISSUED)[: MAX_TRACKED_JOBS // 5]:
                _ISSUED.pop(stale, None)
        _ISSUED[task_id] = SERVICE_OBJECTS["music"].epoch


def _is_orphaned(task_id):
    """True when a task cannot possibly still be queued or running."""
    music = SERVICE_OBJECTS["music"]
    with _issued_lock:
        issued_epoch = _ISSUED.get(task_id)

    # A job the store still holds as pending is queued for replay, not lost.
    # Without this check, orphan detection would report it failed moments before
    # the replay thread resurrects it.
    if any(job_id == task_id for job_id, _, _ in JOBS.pending()):
        return False

    if issued_epoch is not None:
        # We handed this id out; if the backend has restarted since, it's gone.
        return issued_epoch < music.epoch

    # We have no record — either a different gateway instance issued it, or the
    # id is simply wrong. A stopped backend holds no queue at all, so that case
    # is decisive; otherwise fall back to the same check a careful client would
    # make, once the counters have had a moment to catch up.
    if not music.is_running():
        return True
    if time.time() - music.started_at < ORPHAN_GRACE_SECONDS:
        return False
    return not music.has_work()


def _annotate_orphans(body, requested):
    """Rewrite status 0 to status 2 for jobs that no longer exist.

    Leaves genuinely-queued jobs untouched. Returns the original bytes on any
    parse failure — never make a poll worse than it already was.
    """
    try:
        payload = json.loads(body.decode("utf-8"))
        rows = payload.get("data")
        if not isinstance(rows, list):
            return body
        changed = False
        for row in rows:
            if not isinstance(row, dict) or row.get("status") != 0:
                continue
            task_id = row.get("task_id")
            if task_id in requested and _is_orphaned(task_id):
                row["status"] = 2
                row["result"] = json.dumps([{
                    "status": 2,
                    "error": "orphaned: the generation queue is held in memory and "
                             "was lost when the music backend restarted. This job is "
                             "not running and never will. Resubmit it.",
                }])
                row["orphaned"] = True
                changed = True
        if not changed:
            return body
        log("query_result: reported %d orphaned job(s) as failed"
            % sum(1 for r in rows if isinstance(r, dict) and r.get("orphaned")))
        return json.dumps(payload).encode()
    except Exception:
        return body


class HostExhausted(Exception):
    """The machine has no room to load another model right now.

    Anneal is not the only thing running on this desktop. Starting a multi-GB
    load into a machine that is already out of memory produces a job that dies
    slowly and confusingly; refusing up front is kinder and much faster to
    diagnose.
    """

    def __init__(self, info, wanted):
        self.info = info
        self.wanted = wanted
        Exception.__init__(
            self,
            "not enough memory to load %s: %d MB free with %d MB of swap in use. "
            "Close other applications, or stop a loaded model with "
            "POST /supervisor/stop." % (wanted, info.get("free_mb", 0), info.get("swap_used_mb", 0)),
        )


class ServiceBusy(Exception):
    """Another heavy service is mid-job and must not be evicted."""

    def __init__(self, holder, wanted):
        self.holder = holder
        self.wanted = wanted
        Exception.__init__(
            self,
            "%s is mid-generation and only one heavy model fits in memory. "
            "Retry once it finishes, or POST /supervisor/stop {\"service\": \"%s\"} "
            "to abandon its work and free the slot." % (holder, holder),
        )


def ensure_music_tier(tier):
    """Make the music backend run the model for `tier`, restarting if needed.

    ACE-Step fixes its DiT at startup and can only route between models already
    resident, which on 16 GB is one. So switching tiers costs a restart and a
    fresh cold load — deliberate, and surfaced to the caller rather than hidden.
    """
    spec = MUSIC_TIERS.get(tier)
    if not spec:
        return None
    svc = SERVICE_OBJECTS["music"]
    wanted = spec["model"]
    if svc.spec["env"].get("ACESTEP_CONFIG_PATH") == wanted:
        return wanted                      # already configured for this tier

    if svc.is_running():
        with svc.flight_lock:
            busy = svc.in_flight
        if busy or svc.has_work():
            raise ServiceBusy("music", "music (%s tier)" % tier)
        log("music: switching tier to %r (%s) — restarting" % (tier, wanted))
        svc.stop("tier switch to %s" % tier)
    svc.spec["env"]["ACESTEP_CONFIG_PATH"] = wanted
    return wanted


def start_service(name):
    """Start `name`, first evicting any other *idle* heavy service.

    Touches the service before doing anything slow. Starting a backend takes
    seconds to minutes, and without this the reaper can decide to stop it based
    on an idle time measured before the request arrived — killing the very
    service the caller is waiting on, mid-stream.

    Eviction used to be unconditional, which silently killed in-flight music
    jobs whenever an image was requested — and because a dead queue still
    reports status 0, the client polled forever on work that no longer existed.
    Refusing loudly is far better than losing someone's job quietly.
    """
    svc = SERVICE_OBJECTS[name]
    svc.touch()
    if not svc.heavy:
        svc.ensure_started()
        return

    # Already up: nothing to load, so neither eviction nor a headroom check
    # applies. Without this, merely polling a running job could be refused for
    # low memory — memory that the running model itself is legitimately using.
    if svc.is_running() and svc.port_open():
        return

    with _heavy_lock:
        for other_name, other in SERVICE_OBJECTS.items():
            if other_name == name or not other.heavy or not other.is_running():
                continue
            with other.flight_lock:
                in_flight = other.in_flight
            if in_flight or other.has_work():
                raise ServiceBusy(other_name, name)
            log("evicting idle heavy service %r to make room for %r" % (other_name, name))
            other.stop("evicted by %s" % name)

        # Check headroom only after eviction — the freed model was very likely
        # the thing occupying the memory we are about to need.
        info = system_memory()
        if info.get("free_mb") is not None and info["free_mb"] < MIN_FREE_MB_FOR_HEAVY:
            raise HostExhausted(info, name)
        was_down = not svc.is_running()
        svc.ensure_started()
        if name == "music" and was_down:
            threading.Thread(target=replay_pending_music_jobs, daemon=True).start()


def reaper():
    while True:
        time.sleep(REAP_INTERVAL)
        for name, svc in SERVICE_OBJECTS.items():
            try:
                if not svc.is_running():
                    continue
                svc.refresh_snapshot()
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
                # has_work() involves a network round trip, so re-read the clock
                # before acting: a request may have arrived while we asked.
                if time.time() - svc.last_activity < svc.spec["idle_timeout"]:
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

    def _tailscale_identity(self):
        """The tailnet user Tailscale says is calling, if it told us.

        `tailscale serve` stamps these headers on requests it proxies from
        tailnet peers. They are trustworthy only because the supervisor binds to
        loopback: the sole path in is the serve proxy, so nothing off-machine
        can set them. Anything already running locally could forge them — but it
        could equally read env.local.sh, so that changes nothing.

        If the listener is ever widened past loopback that reasoning collapses,
        so the headers are ignored outright in that case.
        """
        if not TRUST_TAILSCALE_IDENTITY:
            return ""
        return self.headers.get("Tailscale-User-Login") or ""

    def _auth_method(self):
        """How this request is authenticated: 'key', 'tailscale', or None."""
        if not API_KEY:
            return "open"
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and header[7:] == API_KEY:
            return "key"
        identity = self._tailscale_identity()
        if identity and (not ALLOWED_LOGINS or identity.lower() in ALLOWED_LOGINS):
            return "tailscale"
        return None

    def _authorized(self):
        return self._auth_method() is not None

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
            "system": system_memory(),
        }

    # -- local endpoints --------------------------------------------------
    def _serve_file_from_disk(self, roots):
        """Serve a generated artefact off disk, so reading it never wakes a model."""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        raw = (params.get("path") or [""])[0]
        if not raw:
            return False
        path = os.path.realpath(raw)
        if not any(path.startswith(root + os.sep) for root in roots):
            return False
        if not os.path.isfile(path):
            return False

        ctype = CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")

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

    # -- persistence ------------------------------------------------------
    def _persist_output(self, route, request_body, response_body, content_type):
        """Keep a durable, named copy of whatever a service just produced.

        Best-effort by design: a storage problem must never turn a successful
        generation into a failed request.
        """
        try:
            req = {}
            if request_body:
                try:
                    req = json.loads(request_body.decode("utf-8"))
                except Exception:
                    req = {}

            if route.startswith("/v1/audio/speech") or route.startswith("/v1/speech"):
                if not isinstance(response_body, bytes) or b"json" in content_type.encode():
                    return
                ext = {"mp3": ".mp3", "wav": ".wav", "flac": ".flac",
                       "opus": ".opus", "aac": ".aac"}.get(
                    (req.get("response_format") or "wav").lower(), ".wav")
                outputs.save_bytes("speech", response_body, ext, {
                    "prompt": req.get("input") or req.get("text") or "",
                    "voice": req.get("voice"), "speed": req.get("speed"),
                    "service": "speech", "request": req,
                })

            elif route.startswith("/v1/images"):
                payload = json.loads(response_body.decode("utf-8"))
                for item in payload.get("data") or []:
                    # The image service already writes into outputs/images, so
                    # this only needs to attach the metadata.
                    outputs.adopt("images", item.get("path"), {
                        "prompt": req.get("prompt", ""), "seed": item.get("seed"),
                        "size": req.get("size"), "steps": req.get("steps"),
                        "seconds": item.get("seconds"), "service": "image",
                        "request": dict(req, seed=item.get("seed")),
                    })
        except Exception as exc:
            log("persist failed for %s: %r" % (route, exc))

    def _persist_music_takes(self, task_id, takes):
        """Copy finished music out of the backend's prunable temp cache.

        Idempotent: a finished job can be polled any number of times, and each
        poll used to write another copy of every take.
        """
        already = JOBS.get_saved(task_id)
        if already and len(already) == len(takes):
            for take, path in zip(takes, already):
                if os.path.isfile(path):
                    take["file"] = "/v1/audio?path=" + urllib.parse.quote(path, safe="")
            return already

        payload = JOBS.payload_for(task_id) or {}
        prompt = payload.get("prompt", "")
        saved = []
        for take in takes:
            file_url = take.get("file") or ""
            params = urllib.parse.parse_qs(urllib.parse.urlparse(file_url).query)
            src = (params.get("path") or [""])[0]
            if not src:
                continue
            meta = take.get("metas") or {}
            path = outputs.save_copy("music", src, {
                "prompt": prompt or take.get("prompt", ""),
                "lyrics": payload.get("lyrics"),
                # These come from the planning LM — what it asked the DiT for,
                # not an analysis of the audio produced. The LM can emit values
                # plainly at odds with the result (300 bpm for a folk ballad),
                # so they are labelled rather than presented as fact.
                "bpm_planned": meta.get("bpm"),
                "key_scale_planned": meta.get("keyscale"),
                "metadata_source": "planning-lm (requested, not measured)",
                "duration": meta.get("duration"), "seed": take.get("seed_value"),
                "service": "music", "task_id": task_id,
                # Which model actually produced it — the tier alone is ambiguous
                # once tiers or patches change underneath.
                "dit_model": take.get("dit_model"),
                "lm_model": take.get("lm_model"),
                # The full submitted request, so a result can be reproduced or
                # used as a starting point without retyping it.
                "request": payload,
            })
            if path:
                # Point the caller at the durable copy rather than the temp file
                # the backend is free to prune.
                take["file"] = "/v1/audio?path=" + urllib.parse.quote(path, safe="")
                saved.append(path)
        if saved:
            JOBS.set_saved(task_id, saved)
        return saved

    # -- job tracking -----------------------------------------------------
    def _track_music_jobs(self, route, request_body, response_body):
        """Remember issued task_ids, and flag ones that no longer exist."""
        if route == "/release_task":
            try:
                data = json.loads(response_body.decode("utf-8")).get("data") or {}
                task_id = data.get("task_id") or data.get("taskId")
                _record_issued(task_id)
                if task_id and request_body:
                    JOBS.record(task_id, json.loads(request_body.decode("utf-8")))
            except Exception:
                pass
            return response_body

        if route == "/query_result":
            # What the caller asked for, captured before the ids were rewritten.
            requested = getattr(self, "_polled_originals", None)
            if requested is None:
                try:
                    asked = json.loads((request_body or b"{}").decode("utf-8")).get("task_id_list")
                    if isinstance(asked, str):
                        asked = json.loads(asked)
                    requested = set(asked or [])
                except Exception:
                    return response_body
            response_body = self._restore_original_ids(response_body, requested)
            return _annotate_orphans(response_body, requested)

        return response_body

    def _rewrite_polled_ids(self, request_body):
        """Point a poll at the replayed job, if this id was replayed.

        Records what the caller actually asked for, because the rewritten body
        no longer says — and the response has to be translated back to it.
        """
        self._polled_originals = set()
        if not request_body:
            return request_body
        try:
            payload = json.loads(request_body.decode("utf-8"))
            asked = payload.get("task_id_list")
            if isinstance(asked, str):
                asked = json.loads(asked)
            if not asked:
                return request_body
            self._polled_originals = set(asked)
            mapped = [JOBS.to_current(t) for t in asked]
            if mapped == list(asked):
                return request_body
            payload["task_id_list"] = mapped
            return json.dumps(payload).encode()
        except Exception:
            return request_body

    def _restore_original_ids(self, response_body, requested):
        """Answer with the id the caller asked about, not the replayed one."""
        try:
            payload = json.loads(response_body.decode("utf-8"))
            rows = payload.get("data")
            if not isinstance(rows, list):
                return response_body
            changed = False
            for row in rows:
                if not isinstance(row, dict):
                    continue
                current = row.get("task_id")
                original = JOBS.to_original(current)
                if original != current and original in requested:
                    row["task_id"] = original
                    changed = True
                # A terminal state means it never needs replaying again.
                if row.get("status") in (1, 2):
                    JOBS.complete(original, "done" if row.get("status") == 1 else "failed")
                if row.get("status") == 1 and isinstance(row.get("result"), str):
                    try:
                        takes = json.loads(row["result"])
                        if self._persist_music_takes(original, takes):
                            row["result"] = json.dumps(takes)
                            changed = True
                    except Exception as exc:
                        log("persist music failed: %r" % exc)
            return json.dumps(payload).encode() if changed else response_body
        except Exception:
            return response_body

    # -- proxying ---------------------------------------------------------
    def _proxy(self, method, body=None, transform=None):
        route = urllib.parse.urlparse(self.path).path
        name = resolve(route)
        if name is None:
            self._send_json({"code": 404, "error": "no service owns %s" % route}, 404)
            return

        # Every proxied route needs auth. This used to be left to the backends,
        # which only worked by accident: ACE-Step enforces its own key, but the
        # speech and image servers have none, so those endpoints were reachable
        # by anything that could reach the gateway.
        if not self._authorized():
            self._send_json({"code": 401, "error": "unauthorized"}, 401)
            return

        if body is None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
        if route == "/query_result":
            body = self._rewrite_polled_ids(body)
        elif route in ("/v1/chat/completions", "/v1/completions") and body:
            # mlx_lm identifies its model by the filesystem path it was loaded
            # from, and 404s on anything else. Nobody should have to send a
            # snapshot path, so accept whatever the caller wrote — or nothing —
            # and substitute the model actually loaded.
            try:
                payload = json.loads(body.decode("utf-8"))
                payload["model"] = TEXT_MODEL_PATH
                body = json.dumps(payload).encode()
            except Exception:
                pass
        elif route == "/release_task" and body:
            try:
                payload = json.loads(body.decode("utf-8"))
                tier = (payload.pop("quality", None) or DEFAULT_MUSIC_TIER)
                if tier not in MUSIC_TIERS:
                    self._send_json({"code": 400, "error": "unknown quality %r; expected one of %s"
                                     % (tier, ", ".join(MUSIC_TIERS))}, 400)
                    return
                if MUSIC_TIERS[tier].get("available") is False:
                    self._send_json({"code": 400, "error": MUSIC_TIERS[tier]["unavailable_reason"],
                                     "quality": tier, "available": False}, 400)
                    return
                ensure_music_tier(tier)
                payload.setdefault("inference_steps", MUSIC_TIERS[tier]["steps"])
                # Anneal-level defaults, deliberately different from upstream's.
                # A bare API call otherwise gets thinking=false and lossy audio —
                # the two settings we already established make output materially
                # worse — so every integrator would rediscover them separately.
                # Both are plain overrides: send the field to get the old behaviour.
                payload.setdefault("thinking", True)
                payload.setdefault("audio_format", "flac")
                # Non-turbo models need CFG; turbo ignores these entirely.
                for k, v in (MUSIC_TIERS[tier].get("extra_params") or {}).items():
                    payload.setdefault(k, v)
                body = json.dumps(payload).encode()
            except ServiceBusy as busy:
                self._send_json({"code": 409, "error": str(busy),
                                 "busy_service": busy.holder}, 409)
                return
            except Exception:
                pass

        svc = SERVICE_OBJECTS[name]
        try:
            start_service(name)
        except ServiceBusy as busy:
            # 409, not 503: nothing is broken and retrying immediately won't help.
            self.send_response(409)
            payload = json.dumps({
                "code": 409, "error": str(busy),
                "busy_service": busy.holder, "requested_service": busy.wanted,
            }).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "60")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        except HostExhausted as exhausted:
            self._send_json({
                "code": 503, "error": str(exhausted),
                "reason": "host_memory_exhausted", "system": exhausted.info,
            }, 503)
            return
        except Exception as exc:
            self._send_json({"code": 503, "error": "%s failed to start: %s" % (name, exc)}, 503)
            return

        svc.touch()
        with svc.flight_lock:
            svc.in_flight += 1
        started = False          # once headers are out, an error page is impossible
        try:
            headers = {}
            for key, value in self.headers.items():
                if key.lower() in HOP_BY_HOP or key.lower() == "host":
                    continue
                if key.lower() == "content-length":
                    continue      # set from the body we actually send, below
                headers[key] = value
            headers["Host"] = "127.0.0.1:%d" % svc.port
            # The gateway is the trust boundary; the backends are internal,
            # loopback-only services. Having already authenticated the caller —
            # by key or by tailnet identity — present the backend's own
            # credential rather than passing the client's through. Without this,
            # a browser authenticated by Tailscale reached ACE-Step with no
            # Authorization header at all and got its 401, which looked to the
            # user like Anneal asking for a key it had just said wasn't needed.
            if API_KEY:
                headers["Authorization"] = "Bearer %s" % API_KEY
            # Rewriting task ids changes the body length, so Content-Length must
            # be recomputed. Forwarding the client's value truncated the body by
            # a byte, which corrupted the backend's JSON *and* desynced
            # keep-alive on this connection.
            if body is not None:
                headers["Content-Length"] = str(len(body))

            conn = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=PROXY_TIMEOUT)
            try:
                conn.request(method, self.path, body=body, headers=headers)
                resp = conn.getresponse()

                # Token streams must be relayed as they arrive. Buffering an SSE
                # response defeats the entire point of streaming — the caller
                # would wait for the full completion and then receive it at once.
                if _is_stream(resp):
                    started = True
                    self.send_response(resp.status)
                    for key, value in resp.getheaders():
                        if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                            continue
                        self.send_header(key, value)
                    # Transfer-Encoding is hop-by-hop so it was stripped above,
                    # and there is no Content-Length for a stream. That leaves
                    # the response with no framing at all: on HTTP/1.1 the
                    # connection stays open and the client waits forever for an
                    # end that never comes. Close-delimit it instead, so end of
                    # stream is end of connection.
                    self.send_header("Connection", "close")
                    self.close_connection = True
                    self.end_headers()
                    try:
                        while True:
                            # read1, not read: read(n) blocks until n bytes
                            # exist, which re-buffers the stream we are relaying.
                            chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(1)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        # The caller hung up — cancelling a stream is normal, not
                        # an error. Stop relaying and let the backend finish.
                        log("client disconnected mid-stream on %s" % route)
                    return

                payload = resp.read()

                if resp.status == 200 and transform == "text":
                    # Unwrap the OpenAI envelope down to the text itself.
                    try:
                        d = json.loads(payload.decode("utf-8"))
                        msg = (d["choices"][0].get("message") or {})
                        payload = json.dumps({
                            "data": {"text": (msg.get("content") or "").strip(),
                                     "model": TEXT_MODEL_NAME,
                                     "usage": d.get("usage")},
                            "code": 200, "error": None}).encode()
                    except Exception as exc:
                        payload = json.dumps({"code": 502,
                                              "error": "unexpected text response: %s" % exc}).encode()
                elif resp.status == 200:
                    payload = self._track_music_jobs(route, body, payload)
                    self._persist_output(route, body, payload,
                                         resp.getheader("Content-Type") or "")

                started = True
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
        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected on %s" % route)
        except Exception as exc:
            # Only if nothing has been written yet. Sending an error body after
            # headers are out corrupts the response and desyncs the connection.
            if started:
                log("proxy error after response began on %s: %r" % (route, exc))
                self.close_connection = True
            else:
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
        if route == "/v1/music/tiers":
            svc = SERVICE_OBJECTS["music"]
            current = svc.spec["env"].get("ACESTEP_CONFIG_PATH")
            self._send_json({"data": {
                "default": DEFAULT_MUSIC_TIER,
                "loaded_model": current,
                "tiers": {k: {"model": v["model"], "steps": v["steps"], "label": v["label"],
                              "loaded": v["model"] == current,
                              "available": v.get("available", True),
                              "unavailable_reason": v.get("unavailable_reason")}
                          for k, v in MUSIC_TIERS.items()},
            }, "code": 200, "error": None})
            return
        if route == "/supervisor/auth":
            # Lets the UI find out whether it needs to ask for a key at all.
            # Safe to leave unauthenticated: it only reflects who you already are.
            method = self._auth_method()
            self._send_json({"data": {
                "authenticated": method is not None,
                "via": method,
                "user": self.headers.get("Tailscale-User-Name") or self._tailscale_identity() or None,
                "tailscale_identity_trusted": TRUST_TAILSCALE_IDENTITY,
            }, "code": 200, "error": None})
            return
        if route == "/supervisor/whoami":
            # What Tailscale tells us about the caller. Useful on its own, and
            # the basis for letting the UI authenticate without a pasted token.
            self._send_json({"data": {
                "tailscale_identity": self._tailscale_identity(),
                "headers": {k: v for k, v in self.headers.items()
                            if k.lower().startswith("tailscale-")},
                "client": self.client_address[0],
            }, "code": 200, "error": None})
            return
        if route in ("/openapi.json", "/openapi"):
            try:
                with open(OPENAPI_PATH, "rb") as fh:
                    spec = json.loads(fh.read().decode("utf-8"))
                # Fill in this host's own URLs rather than shipping whichever
                # machine the spec was written on.
                servers = []
                if TAILNET_HOST and TAILNET_HOST != "localhost":
                    servers.append({"url": "https://%s" % TAILNET_HOST, "description": "Tailnet (TLS)"})
                servers.append({"url": "http://127.0.0.1:%d" % LISTEN_PORT,
                                "description": "On the host itself"})
                spec["servers"] = servers
                self._send_bytes(json.dumps(spec).encode(), "application/json")
            except Exception as exc:
                self._send_json({"code": 500, "error": "spec unavailable: %s" % exc}, 500)
            return
        if route in ("/", "/ui", "/ui/"):
            try:
                with open(UI_PATH, "rb") as fh:
                    self._send_bytes(fh.read(), "text/html; charset=utf-8")
            except OSError as exc:
                self._send_json({"code": 500, "error": "UI unavailable: %s" % exc}, 500)
            return
        if route in ("/docs", "/docs/"):
            self._send_bytes(DOCS_HTML.encode(), "text/html; charset=utf-8")
            return
        if route == "/v1/press":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pid = (q.get("id") or [""])[0]
            data = PRESSES.get(pid) if pid else {"presses": PRESSES.recent()}
            if pid and data is None:
                self._send_json({"code": 404, "error": "no such press"}, 404)
                return
            self._send_json({"data": data, "code": 200, "error": None})
            return

        if route == "/v1/outputs":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                limit = min(int((q.get("limit") or ["200"])[0]), 1000)
                offset = max(int((q.get("offset") or ["0"])[0]), 0)
            except ValueError:
                limit, offset = 200, 0
            data = outputs.listing((q.get("kind") or [None])[0], limit, offset)
            self._send_json({"data": data, "code": 200, "error": None})
            return

        if route == "/v1/outputs/file":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            if self._serve_file_from_disk([os.path.realpath(outputs.root())]):
                return          # off disk, so browsing the library wakes nothing
            self._send_json({"code": 404, "error": "no such output"}, 404)
            return

        if route == "/v1/images/file":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            if self._serve_file_from_disk(IMAGE_ROOTS):
                return  # served without waking the image model
            self._send_json({"code": 404, "error": "no such image"}, 404)
            return

        if route == "/v1/audio":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            if self._serve_file_from_disk(AUDIO_ROOTS):
                return  # served without waking anything
            # Don't fall through to the backend: it answers a malformed path
            # with "Access denied: path outside allowed directory", which reads
            # like a permissions problem rather than a client-side encoding bug.
            raw = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                   .get("path") or [""])[0]
            if "/v1/audio" in raw or "?path=" in raw or raw.startswith("%2F"):
                self._send_json({
                    "code": 400,
                    "error": "the `path` value looks double-encoded. The `file` field "
                             "returned by /query_result is already a complete request "
                             "path including its query string — append it to the base "
                             "URL as-is, do not URL-encode it again.",
                    "received": raw[:200],
                }, 400)
                return
            self._send_json({
                "code": 404,
                "error": "no such audio file. Generated files live in a cache the "
                         "server prunes — persist the bytes when you download them.",
                "received": raw[:200],
            }, 404)
            return
        self._proxy("GET")

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path

        if route == "/v1/press":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            payload, _ = self._body()
            if not (payload.get("prompt") or "").strip():
                self._send_json({"code": 400, "error": "'prompt' is required"}, 400)
                return
            pid = PRESSES.create(payload)
            # Runs for minutes to tens of minutes, so it cannot be a blocking
            # request; the caller polls GET /v1/press?id=.
            threading.Thread(target=PRESS.run, args=(pid,), daemon=True).start()
            self._send_json({"data": {"press_id": pid, "state": "planning",
                                      "poll": "/v1/press?id=" + pid},
                             "code": 200, "error": None})
            return

        if route == "/v1/press/cancel":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            payload, _ = self._body()
            pid = payload.get("id") or payload.get("press_id")
            PRESS.cancel(pid)
            self._send_json({"data": {"cancelled": pid}, "code": 200, "error": None})
            return

        if route == "/v1/text":
            # A deliberately small contract: prompt in, text out. The
            # OpenAI-shaped /v1/chat/completions remains for clients that want
            # messages, streaming and the rest.
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            payload, _ = self._body()
            prompt = (payload.get("prompt") or "").strip()
            if not prompt:
                self._send_json({"code": 400, "error": "'prompt' is required"}, 400)
                return
            messages = []
            if payload.get("system"):
                messages.append({"role": "system", "content": payload["system"]})
            messages.append({"role": "user", "content": prompt})
            upstream = {
                "model": TEXT_MODEL_PATH,
                "messages": messages,
                "max_tokens": min(int(payload.get("max_tokens") or 800), 4096),
                "temperature": float(payload.get("temperature", 0.8)),
                "stream": False,
                # Gemma 4 reasons at length by default and would spend the token
                # budget before answering. Callers wanting that can use
                # /v1/chat/completions and set it themselves.
                "chat_template_kwargs": {"enable_thinking": bool(payload.get("thinking", False))},
            }
            self.path = "/v1/chat/completions"
            self._proxy("POST", body=json.dumps(upstream).encode(), transform="text")
            return

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
        route = urllib.parse.urlparse(self.path).path
        if route == "/v1/press":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pid = (q.get("id") or [""])[0]
            data = PRESSES.get(pid) if pid else {"presses": PRESSES.recent()}
            if pid and data is None:
                self._send_json({"code": 404, "error": "no such press"}, 404)
                return
            self._send_json({"data": data, "code": 200, "error": None})
            return

        if route == "/v1/outputs":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            path = (q.get("path") or [""])[0]
            ok = outputs.delete(path) if path else False
            self._send_json({"data": {"deleted": ok, "path": path},
                             "code": 200 if ok else 404,
                             "error": None if ok else "not found or outside outputs/"},
                            200 if ok else 404)
            return
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
