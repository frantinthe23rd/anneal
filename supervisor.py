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
import re
import shutil
import tempfile
import zipfile
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
from services import (SERVICES, MUSIC_TIERS, DEFAULT_MUSIC_TIER,
                      COLD_START_SECONDS, resolve)  # noqa: E402
from jobstore import JobStore  # noqa: E402
import outputs  # noqa: E402
import paths  # noqa: E402
import vector  # noqa: E402
import builder  # noqa: E402  (capability_limits reports its constants)
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
# Finished job rows are only useful for as long as someone might still poll the
# task id, which MAX_REPLAY_AGE_SECONDS already puts at six hours. A week is
# generous and keeps recent history readable in the database.
JOB_RETENTION_SECONDS = int(os.environ.get("ANNEAL_JOB_RETENTION_SECONDS", str(7 * 24 * 3600)))
PRUNE_INTERVAL = 3600.0
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

# Nothing here has a legitimate multi-megabyte request body: the largest is a
# lyric sheet. Without a cap, `Content-Length: 4000000000` is a one-line memory
# exhaustion, because the body is read into a bytes object before anything looks
# at it. Generous enough that no real caller notices, small enough that a
# malicious one cannot page the machine out.
MAX_REQUEST_BYTES = int(os.environ.get("ANNEAL_MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))
# The planning LM's context is finite and a prompt this long is not a prompt.
MAX_PROMPT_CHARS = int(os.environ.get("ANNEAL_MAX_PROMPT_CHARS", "8000"))
# A press can ask for 8 tracks of 600s each. Nothing stopped it, and that is
# most of a day of generation on this hardware from a single unattended request.
MAX_PRESS_TOTAL_SECONDS = int(os.environ.get("ANNEAL_MAX_PRESS_SECONDS", "1800"))
# The album zip is assembled in a temp directory before anything is sent, so its
# size is a disk commitment. Judged from the FLAC masters, which are the largest
# thing that can go in.
MAX_ZIP_SOURCE_BYTES = int(os.environ.get("ANNEAL_MAX_ZIP_BYTES", str(4 * 1024 ** 3)))

# Generated files are served straight off disk, but only from under these roots.
# Reading back an old result must never wake the model that made it.
#
# AUDIO_ROOTS used to include the whole of AIMUSIC_ROOT, which is also where
# jobs.db, presses.db, the four log files, hf-cache and 9.4 GB of weights live.
# Any authenticated caller could read all of it through `/v1/audio?path=`. That
# is defensible on a personal tailnet and is precisely what #13 is about, so the
# root is now outputs/ — the only place under AIMUSIC_ROOT that holds audio —
# alongside the backend's own temp cache, which is where a take lives until
# _persist_music_takes copies it out.
AUDIO_ROOTS = [
    os.path.realpath(os.path.join(ACESTEP_DIR, ".cache")),
    os.path.realpath(os.path.join(ACESTEP_DIR, "gradio_outputs")),
    os.path.realpath(os.path.join(AIMUSIC_ROOT, "outputs")),
]
IMAGE_ROOTS = [os.path.realpath(os.path.join(AIMUSIC_ROOT, "outputs"))]

CONTENT_TYPES = {
    ".mp3": "audio/mpeg", ".flac": "audio/flac", ".wav": "audio/wav",
    ".opus": "audio/opus", ".aac": "audio/aac", ".m4a": "audio/mp4",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    # SVG is a document, not a picture: navigating to one runs whatever is in
    # it. vector.py sanitises before anything is written, and the download
    # handler adds Content-Disposition: attachment and a CSP on top, so three
    # separate things would have to fail for a generated file to execute.
    ".svg": "image/svg+xml",
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


def press_text(prompt, max_tokens=900, temperature=0.9):
    """0.9 is right for lyrics and titles, where sameness is the failure mode.

    It is wrong for anything that has to parse — see /v1/vector, which asks for
    markup and passes 0.2. Measured: at 0.9 this model produced well-formed SVG
    once in five attempts; at 0.2, four times in five.
    """
    d = _local_json("/v1/text", {"prompt": prompt, "max_tokens": max_tokens,
                                 "temperature": temperature, "thinking": False})
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


def press_image(prompt, size, wait_for_slot=300, what="the cover"):
    """Paint the cover, waiting for the music model to release the heavy slot.

    A track reports done the moment its result is stored, but the music service
    keeps reporting work in flight for a little longer, so an immediate image
    request is refused with a 409 by the eviction guard. That is the guard doing
    its job — the caller just has to wait rather than treat it as failure.
    """
    deadline = time.time() + wait_for_slot
    while True:
        try:
            d = _local_json("/v1/images/generations",
                            {"prompt": prompt, "size": size, "steps": 4, "n": 1,
                             "response_format": "path"})
            items = (d or {}).get("data") or []
            return items[0] if items else None
        except RuntimeError as exc:
            if "409" in str(exc) and time.time() < deadline:
                log("heavy slot busy, waiting to paint %s" % what)
                time.sleep(10)
                continue
            raise


PRESS = Press(PRESSES, press_text, press_music, press_image, log=log)
# How the queue actually starts work. Press does the admission and the ordering
# and knows nothing about threads; this is the one line that makes it real.
PRESS.spawn = lambda pid, resume=False: threading.Thread(
    target=PRESS.run, args=(pid,), kwargs={"resume": resume}, daemon=True).start()


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
    last_prune = 0.0
    while True:
        time.sleep(REAP_INTERVAL)

        # JobStore.prune() has existed since the job store was written and was
        # called from nowhere, so jobs.db only ever grew. It drops terminal rows
        # older than a week; a pending row is never touched, because that is the
        # replay queue the store exists for. The reaper is the right home — it
        # is the only thing already running on a timer, and pruning is exactly
        # the same kind of work as unloading an idle model.
        if time.time() - last_prune > PRUNE_INTERVAL:
            last_prune = time.time()
            try:
                JOBS.prune(JOB_RETENTION_SECONDS)
            except Exception as exc:
                log("job prune failed: %r" % exc)

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


def press_limits(payload):
    """Why this press must be refused, or None if it is within bounds.

    `builder.py` already clamps the track count to 8 and any single duration to
    600s, but nothing bounded their product. Eight ten-minute tracks is 80
    minutes of *audio*, which on this hardware is hours of generation from one
    unattended POST — and it holds the heavy slot for all of it, so nothing else
    on the machine runs meanwhile (#14).

    The cap is on the worst case the request can produce, not on the plan the LM
    eventually returns: the point is to answer at submit time, while there is
    still a caller listening, rather than to abort something halfway.
    """
    prompt = payload.get("prompt") or ""
    if len(prompt) > MAX_PROMPT_CHARS:
        return ("prompt is %d characters, over the %d character limit"
                % (len(prompt), MAX_PROMPT_CHARS))

    def _int(key, default):
        try:
            return int(payload.get(key, default))
        except (TypeError, ValueError):
            return default

    count = max(1, min(_int("tracks", 1), 8))
    target = _int("duration", 90)
    longest = min(600, _int("duration_max", round(target * 1.5)))
    worst_case = count * max(target, longest)
    if worst_case > MAX_PRESS_TOTAL_SECONDS:
        return ("%d track(s) of up to %ds each is %d seconds of audio, over the %d "
                "second limit for one press — reduce tracks or duration"
                % (count, longest, worst_case, MAX_PRESS_TOTAL_SECONDS))
    return None


# ------------------------------------------------------------------ sprites
# The interpreter that can cut and matte a sheet. It is not the one serving the
# models: matting needs a segmentation model, which pulls onnxruntime, and the
# model environment is version-pinned. Keeping them apart means the sprite
# dependency cannot break music generation, at the cost of a subprocess.
SPRITE_PYTHON = os.environ.get(
    "ANNEAL_SPRITE_PYTHON", os.path.join(AIMUSIC_ROOT, "tools-venv", "bin", "python"))
MAX_SPRITE_FRAMES = 8


def sprite_python():
    """The configured interpreter, or None if it is not usable."""
    return SPRITE_PYTHON if os.path.isfile(SPRITE_PYTHON) else None


def sheet_prompt(subject, frames=4, style="flat pixel art", poses=None):
    """Ask for one picture containing every frame.

    This wording is the whole reason the feature works. Generating N sprites
    separately produced N different characters — measured on both img2img and
    plain prompting — because each generation is independent. Frames that share
    one generation cannot diverge, so the prompt has to make it one picture and
    say plainly that the character does not change between poses.

    The second half is the harder half and it only half works. Asking for "the
    pose changing" gets identity for free and motion barely at all: a measured
    run returned five near-identical standing slimes. Naming the poses is what
    produces visible difference, so `poses` is passed straight through and the
    default wording leans on "clearly different" rather than "changing".
    """
    plan = ""
    if poses:
        plan = ", the frames in order: %s" % ", ".join(p.strip() for p in poses if p.strip())
    return ("sprite sheet for a 2D game: the same %s shown in %d separate frames "
            "in a single horizontal row, identical character design, colours and "
            "proportions in every frame, each frame a clearly and obviously "
            "different pose%s, %s, plain flat white background, no shadow, no "
            "scenery, no text, full body, even lighting"
            % (subject.strip(), int(frames), plan, style.strip()))


# How a sprite set is made. Two methods, and the licence is a row attribute
# because one of them is not the licence everything else here uses.
#
#   sheet   — one schnell generation containing every pose, then cut. Free,
#             Apache-2.0, already installed. Keeps the character identical and
#             produces very little motion; naming poses moves it at the cost of
#             design drift. Both measured.
#   kontext — one base sprite, then one *directed edit* per pose against it.
#             Identity comes from the reference image and the pose is an
#             instruction, so the two stop fighting. Costs a 9.6 GB download and
#             a non-commercial licence.
SPRITE_METHODS = {
    "sheet": {
        "licence": "Apache-2.0 (FLUX.1-schnell)",
        "needs_model": False,
        "label": "One generation, cut into frames",
    },
    "kontext": {
        "licence": "FLUX.1 [dev] Non-Commercial License — the model may not be "
                   "used commercially without a licence from Black Forest Labs. "
                   "Outputs are your own.",
        "needs_model": True,
        "label": "One base sprite, then a directed edit per pose",
    },
}
DEFAULT_SPRITE_METHOD = os.environ.get("ANNEAL_SPRITE_METHOD", "sheet")
KONTEXT_MODEL = os.environ.get(
    "ANNEAL_KONTEXT_MODEL", "akx/FLUX.1-Kontext-dev-mflux-4bit")


def kontext_prompt(pose):
    """Edit one sprite into one pose, changing nothing else.

    Everything except the pose is spelled out as unchanged, because Kontext
    edits what you mention and drifts on what you do not — and a sprite whose
    palette shifts between frames is the failure the sheet method already has.
    """
    return ("change only the pose: the same character, now %s. "
            "Identical character design, colours, proportions, outfit and art "
            "style. Keep the plain flat white background with no scenery, and "
            "keep the character fully in frame." % pose.strip())


def sprite_method_problem(method, model_path=None):
    """Why this host cannot use this method, or None."""
    spec = SPRITE_METHODS.get(method)
    if not spec:
        return "unknown method %r" % method
    if not spec.get("needs_model"):
        return None
    try:
        from huggingface_hub import snapshot_download  # noqa: F401
    except ImportError:
        pass
    path = model_path or _kontext_local_path()
    if not path or not os.path.isdir(path):
        return ("the kontext model is not installed (~9.6 GB, and a "
                "non-commercial licence). See 'Sprite animation' in README.md.")
    return None


def _kontext_local_path():
    """Where the converted Kontext weights live, without reaching the network."""
    root = os.environ.get("HF_HOME", os.path.join(AIMUSIC_ROOT, "hf-cache"))
    base = os.path.join(root, "hub", "models--" + KONTEXT_MODEL.replace("/", "--"),
                        "snapshots")
    if not os.path.isdir(base):
        return None
    for name in sorted(os.listdir(base)):
        return os.path.join(base, name)
    return None


def sprite_limits(payload):
    """Reject what cannot work, before spending minutes on the image model."""
    if not (payload.get("prompt") or "").strip():
        return "'prompt' is required"
    try:
        frames = int(payload.get("frames", 4))
    except (TypeError, ValueError):
        return "'frames' must be a number"
    if not 2 <= frames <= MAX_SPRITE_FRAMES:
        return ("'frames' must be between 2 and %d — more poses than that in one "
                "image leaves each too small to use" % MAX_SPRITE_FRAMES)
    poses = payload.get("poses")
    if poses is not None:
        if not isinstance(poses, list) or not all(isinstance(p, str) for p in poses):
            return "'poses' must be a list of strings, one per frame"
        if len(poses) > MAX_SPRITE_FRAMES:
            return "'poses' cannot be longer than %d" % MAX_SPRITE_FRAMES
    method = payload.get("method") or DEFAULT_SPRITE_METHOD
    if method not in SPRITE_METHODS:
        return ("unknown 'method' %r — one of: %s"
                % (method, ", ".join(SPRITE_METHODS)))
    if method == "kontext" and not [p for p in (poses or []) if p.strip()]:
        # Without an instruction per frame there is nothing to edit towards,
        # and falling back to a frame count would produce N identical copies.
        return ("'poses' is required for method 'kontext' — each pose is the "
                "instruction for one frame")
    return None


def capability_limits():
    """The numbers a client must respect, from wherever they actually live.

    Every value here previously existed as prose in the UI, a literal in
    openapi.json, or both — which is how the spec came to claim the high tier
    ran 32 steps while services.py said 50. Serving them means a page can ask
    rather than repeat, and tests/unit/test_capabilities.py fails if one is
    written down again. See #15.
    """
    return {
        "press": {
            "max_tracks": builder.MAX_TRACKS,
            "min_track_seconds": builder.MIN_TRACK_SECONDS,
            "max_track_seconds": builder.MAX_TRACK_SECONDS,
        },
        "music": {
            "tiers": {name: {"steps": t["steps"], "label": t["label"]}
                      for name, t in MUSIC_TIERS.items()},
            "default_tier": DEFAULT_MUSIC_TIER,
        },
        # Idle timeouts are deliberately absent: they are already reported
        # per-service, and a second copy is exactly the bug this closes.
        "cold_start_seconds": dict(COLD_START_SECONDS),
    }


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

    def _read_body_bytes(self):
        """The request body, or None having already answered 413.

        Every path that reads a body goes through here, so the cap cannot be
        applied to some routes and forgotten on others. On refusal the
        connection is closed rather than kept alive: the body is still sitting
        unread in the socket, and draining megabytes just to stay pipelined
        would defeat the point of refusing it.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_REQUEST_BYTES:
            self.close_connection = True
            self._send_json({"code": 413,
                             "error": "request body of %d bytes exceeds the %d byte limit"
                                      % (length, MAX_REQUEST_BYTES),
                             "limit_bytes": MAX_REQUEST_BYTES}, 413)
            return None
        return self.rfile.read(length) if length else b""

    def _body(self):
        raw = self._read_body_bytes()
        if raw is None:
            return None, None
        try:
            return json.loads(raw or b"{}"), raw
        except ValueError:
            return {}, raw

    def _make_sprites(self, payload, interpreter):
        """A brief becomes a set of matted frames, in two steps.

        Step one is *one* image generation. That is the whole design: separate
        generations of "the same character" produce different characters, so
        every frame has to come out of a single sample. It goes back through the
        gateway's own image endpoint rather than reaching into the service, so
        it inherits eviction, admission control and library persistence — the
        same reasoning as Press.

        Step two runs the cutter as a subprocess, because matting needs rembg
        and the environment that serves the models is version-pinned. Nothing
        crosses that boundary except a path and a line of JSON.
        """
        subject = payload["prompt"].strip()
        frames = int(payload.get("frames", 4))
        style = (payload.get("style") or "flat pixel art").strip()
        poses = payload.get("poses") or None
        if poses:
            # The list is the frame count: asking for four frames and naming six
            # poses is a contradiction the model resolves by ignoring one of them.
            frames = len(poses)
        # Wide by default: the frames are laid out in a row, and a square canvas
        # spends most of its pixels on empty background above and below them.
        size = payload.get("size") or "1344x768"

        try:
            reply = press_image(sheet_prompt(subject, frames, style, poses), size,
                                wait_for_slot=int(payload.get("wait", 300)),
                                what="the sprite sheet")
        except Exception as exc:
            self._send_json({"code": 502,
                             "error": "the sheet could not be generated: %s" % exc}, 502)
            return
        sheet = (reply or {}).get("path")
        if not sheet:
            self._send_json({"code": 502, "error": "the image service returned no sheet"}, 502)
            return
        # The sheet is already in the library with its sidecar: it went through
        # the gateway's own image route, which persists on the way back.

        out_dir = os.path.join(outputs.root(), "sprites", "%s-%s" % (
            time.strftime("%Y%m%d-%H%M%S"), re.sub(r"[^a-z0-9]+", "-", subject.lower())[:40].strip("-") or "sprite"))
        try:
            done = subprocess.run(
                [interpreter, os.path.join(HERE, "sprites.py"), sheet,
                 "--out", out_dir, "--json"],
                capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            self._send_json({"code": 504, "error": "cutting the sheet timed out"}, 504)
            return
        try:
            data = json.loads((done.stdout or "").strip() or "{}")
        except ValueError:
            data = {"error": (done.stderr or "the cutter printed nothing").strip()[:400]}
        if data.get("error") or not data.get("frames"):
            # The sheet is still worth returning: it cost minutes, it is in the
            # library, and a caller can cut it by hand or retry the cut alone.
            self._send_json({
                "code": 502,
                "error": "the sheet was generated but could not be cut: %s"
                         % (data.get("error") or "no frames were found in it"),
                "sheet": sheet,
                "sheet_url": "/v1/images/file?path=" + urllib.parse.quote(sheet, safe=""),
            }, 502)
            return

        for frame in data["frames"]:
            if frame.get("file"):
                frame["url"] = "/v1/outputs/file?path=" + urllib.parse.quote(frame["file"], safe="")
                outputs.adopt("sprites", frame["file"], {
                    "prompt": subject, "style": style, "service": "sprites",
                    "frame": frame["index"], "frames": len(data["frames"]),
                    "sheet": sheet, "request": dict(payload),
                })
        data["subject"] = subject
        data["style"] = style
        data["requested_frames"] = frames
        data["sheet"] = sheet
        data["sheet_url"] = "/v1/images/file?path=" + urllib.parse.quote(sheet, safe="")
        self._send_json({"data": data, "code": 200, "error": None})

    def _status_payload(self):
        return {
            "supervisor": "ok",
            "services": {name: svc.status() for name, svc in SERVICE_OBJECTS.items()},
            "system": system_memory(),
            # Nothing prunes outputs/ automatically — see tools/prune.py for
            # why. Reporting the size is the half that can be automated safely.
            "storage": outputs.usage(),
            "limits": capability_limits(),
        }

    # -- local endpoints --------------------------------------------------
    def _serve_file_from_disk(self, roots):
        """Serve a generated artefact off disk, so reading it never wakes a model."""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        raw = (params.get("path") or [""])[0]
        if not raw:
            return False
        path = paths.safe_file(raw, roots)
        if path is None:
            return False

        # Belt and braces with the root narrowing above: these endpoints exist
        # to hand back generated media, and every artefact they legitimately
        # serve has one of these extensions. A root that later grows to include
        # something else cannot then leak a database or a log through here.
        ctype = CONTENT_TYPES.get(os.path.splitext(path)[1].lower())
        if ctype is None:
            return False

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % os.path.basename(path))
        # Only SVG can execute anything, and it is sanitised before it is
        # written — but this response serves generated content from the same
        # origin as the UI, and that is worth a belt as well as braces. Inert
        # for every other type here.
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        return True

    # -- album download ---------------------------------------------------
    def _send_press_zip(self):
        """Bundle a press as a zip: audio, cover and tracklist.

        Masters are kept as FLAC, so anything else is transcoded on demand from
        the lossless original rather than from another lossy copy.
        """
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        pid = (q.get("id") or [""])[0]
        fmt = (q.get("format") or ["flac"])[0].lower()
        bitrate = (q.get("bitrate") or ["320k"])[0].lower()

        press = PRESSES.get(pid) if pid else None
        if not press:
            self._send_json({"code": 404, "error": "no such press"}, 404)
            return
        if fmt not in ("flac", "mp3", "aac", "wav"):
            self._send_json({"code": 400, "error": "format must be flac, mp3, aac or wav"}, 400)
            return
        if not re.match(r"^\d{2,3}k$", bitrate):
            bitrate = "320k"

        plan = press.get("plan") or {}
        title = plan.get("title") or "anneal-press"
        artist = plan.get("artist") or ""
        tracks = [t for t in (press.get("tracks") or []) if t.get("file") and t.get("state") == "done"]
        if not tracks:
            self._send_json({"code": 409, "error": "nothing finished to download yet"}, 409)
            return

        # The archive is built on disk before a byte is sent, so its size is a
        # disk commitment, not just bandwidth. Masters are FLAC: ten minutes is
        # roughly 60 MB, so a maximum-length press is gigabytes. Refuse up front
        # with a number rather than filling the temp volume and failing midway
        # through a response we have already started.
        source_bytes = 0
        for t in tracks:
            src = paths.safe_file(self._path_from_file_url(t.get("file")), AUDIO_ROOTS)
            if src:
                source_bytes += os.path.getsize(src)
        if source_bytes > MAX_ZIP_SOURCE_BYTES:
            self._send_json({"code": 413,
                             "error": "this press is %.1f GB of masters, over the %.1f GB "
                                      "download limit — fetch tracks individually"
                                      % (source_bytes / 1e9, MAX_ZIP_SOURCE_BYTES / 1e9),
                             "source_bytes": source_bytes}, 413)
            return

        workdir = tempfile.mkdtemp(prefix="press-zip-")
        zip_path = os.path.join(workdir, "album.zip")
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for t in tracks:
                    # The stored `file` is a URL the gateway wrote, but it round
                    # trips through sqlite and originates near model output, and
                    # it is about to become an argument to ffmpeg. Check it
                    # against the same roots the download endpoints use rather
                    # than trusting where it came from.
                    src = paths.safe_file(self._path_from_file_url(t["file"]), AUDIO_ROOTS)
                    if not src:
                        log("press zip: skipping track %s, path outside allowed roots"
                            % t.get("n"))
                        continue
                    stem = "%02d %s" % (t.get("n", 0), outputs.slugify(t.get("title", "track"), 60))
                    if fmt == "flac" and src.lower().endswith(".flac"):
                        zf.write(src, stem + ".flac")          # already the master
                        continue
                    out = os.path.join(workdir, stem + "." + fmt)
                    cmd = ["ffmpeg", "-v", "error", "-y", "-i", src]
                    if fmt == "mp3":
                        cmd += ["-c:a", "libmp3lame", "-b:a", bitrate]
                    elif fmt == "aac":
                        cmd += ["-c:a", "aac", "-b:a", bitrate]
                    elif fmt == "wav":
                        cmd += ["-c:a", "pcm_s24le"]
                    else:
                        cmd += ["-c:a", "flac"]
                    cmd.append(out)
                    try:
                        subprocess.run(cmd, check=True, timeout=300)
                        zf.write(out, os.path.basename(out))
                    except Exception as exc:
                        log("press zip: transcode failed for %s: %r" % (stem, exc))

                cover = press.get("cover") or {}
                cover_path = paths.safe_file(
                    cover.get("path") if isinstance(cover, dict) else None, IMAGE_ROOTS)
                if cover_path:
                    zf.write(cover_path, "cover" + os.path.splitext(cover_path)[1])

                listing = ["%s%s" % (title, " — " + artist if artist else ""), ""]
                if plan.get("concept"):
                    listing += [plan["concept"], ""]
                for t in tracks:
                    d = int(t.get("duration") or 0)
                    listing.append("%2d. %-44s %d:%02d" % (t.get("n", 0), t.get("title", "")[:44],
                                                           d // 60, d % 60))
                total = sum(int(t.get("duration") or 0) for t in tracks)
                listing += ["", "Total %d:%02d over %d track(s)" % (total // 60, total % 60, len(tracks))]
                zf.writestr("tracklist.txt", "\n".join(listing) + "\n")

            size = os.path.getsize(zip_path)
            name = outputs.slugify("%s %s" % (title, artist), 60) + "-" + fmt + ".zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
            self.end_headers()
            with open(zip_path, "rb") as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected during album download")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # -- vector -----------------------------------------------------------
    def _send_vector(self, payload):
        """Draw an SVG with the text model (#18).

        The first Anneal capability that is genuinely fast: SVG is markup, so
        this is text generation. Gemma is light, coexists with a heavy model,
        and answers in seconds — nothing is evicted and nothing cold-starts
        unless chat itself is cold.

        Nothing generated is returned or written until `vector.sanitise` has
        been through it. A `<script>` inside an SVG a game loads is a real
        hazard, so what the model drew and what a caller receives are two
        different documents by construction.
        """
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            self._send_json({"code": 400, "error": "'prompt' is required"}, 400)
            return
        if len(prompt) > MAX_PROMPT_CHARS:
            self._send_json({"code": 400, "error": "prompt is %d characters, over the %d "
                             "character limit" % (len(prompt), MAX_PROMPT_CHARS)}, 400)
            return

        mode = (payload.get("mode") or "draw").lower()
        if mode != "draw":
            # `trace` is the other half of #18 and needs vtracer or potrace,
            # neither of which is installed. Say which, rather than 400ing with
            # "unknown mode" and leaving the caller to guess.
            self._send_json({
                "code": 501,
                "error": "mode %r is not implemented. 'draw' uses the text model and is "
                         "available now; 'trace' would vectorise the image model's output "
                         "and needs vtracer or potrace installed on the host — neither is."
                         % mode}, 501)
            return

        style = (payload.get("style") or vector.DEFAULT_STYLE).lower()
        try:
            size = max(8, min(int(payload.get("size") or 24), 1024))
        except (TypeError, ValueError):
            size = 24

        started = time.time()
        drawn = vector.build_prompt(prompt, style, size)
        last_error, attempts = None, 0
        # Two attempts. A small instruct model returns something unparseable
        # often enough to be worth one retry, and at a couple of seconds a go
        # that is cheaper than making the caller decide.
        for attempts in (1, 2):
            try:
                # Low temperature, unlike every other text call here. This asks
                # for markup that has to parse, and sampling variety is the
                # enemy of that — measured 1-in-5 well-formed at 0.9 against
                # 4-in-5 at 0.2 on the same five subjects.
                reply = press_text(drawn, max_tokens=1400, temperature=0.2)
            except Exception as exc:
                self._send_json({"code": 502, "error": "text service failed: %s" % exc}, 502)
                return
            try:
                svg, removed = vector.sanitise(vector.extract(reply), size=size)
                break
            except vector.SvgRejected as exc:
                last_error = str(exc)
                log("vector: attempt %d rejected — %s" % (attempts, last_error))
        else:
            self._send_json({
                "code": 422,
                "error": "the model did not produce usable SVG after 2 attempts: %s"
                         % last_error,
                "hint": "shorter, more geometric subjects work best — this path is good "
                        "at icons and poor at illustration",
            }, 422)
            return

        if removed:
            # Worth logging rather than only reporting: a model that keeps
            # emitting script is a thing to know about the model.
            log("vector: sanitised out %s" % ", ".join(removed[:8]))

        meta = {"prompt": prompt, "service": "vector", "mode": mode, "style": style,
                "size": size, "model": TEXT_MODEL_PATH, "attempts": attempts,
                "sanitised_out": removed, "seconds": round(time.time() - started, 1),
                "request": dict(payload)}
        path = outputs.save_bytes("vectors", svg.encode("utf-8"), ".svg", meta)

        self._send_json({"data": {
            "svg": svg,
            "path": path,
            "url": ("/v1/outputs/file?path=" + urllib.parse.quote(path, safe="")) if path else None,
            "style": style, "size": size, "attempts": attempts,
            "sanitised_out": removed,
            "seconds": meta["seconds"],
        }, "code": 200, "error": None})

    @staticmethod
    def _path_from_file_url(file_url):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(file_url or "").query)
        return (params.get("path") or [""])[0] or None

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
                        # A variation is only interpretable next to what it came
                        # from, so the sidecar records the parent explicitly
                        # rather than leaving it implied by the request.
                        "derived_from": item.get("derived_from"),
                        "retention": item.get("retention"),
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
            body = self._read_body_bytes()
            if body is None:
                return                      # 413 already sent
            body = body or None
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
        if route.startswith("/assets/"):
            # Locally generated artwork and the two vendored browser libraries,
            # served from the repo. Kept as files rather than inlined so
            # ui.html stays readable, and served by us so the page still makes
            # no external requests.
            #
            # Subdirectories are allowed (assets/vendor/), so basename() is no
            # longer enough: resolve the path and require that it really lands
            # inside the assets directory.
            root = os.path.realpath(os.path.join(HERE, "assets"))
            rel = urllib.parse.unquote(route[len("/assets/"):])
            path = os.path.realpath(os.path.join(root, rel))
            if not rel or not (path == root or path.startswith(root + os.sep)) \
                    or not os.path.isfile(path):
                self._send_json({"code": 404, "error": "no such asset"}, 404)
                return
            name = os.path.basename(path)
            ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                     ".webp": "image/webp", ".svg": "image/svg+xml",
                     ".js": "text/javascript; charset=utf-8",
                     ".css": "text/css; charset=utf-8",
                     # KaTeX ships its own fonts; only woff2 is vendored, which
                     # every browser that can run this UI supports.
                     ".woff2": "font/woff2", ".woff": "font/woff",
                     ".ttf": "font/ttf"}.get(
                os.path.splitext(name)[1].lower(), "application/octet-stream")
            with open(path, "rb") as fh:
                blob = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
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
        if route == "/v1/press/download":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            self._send_press_zip()
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

            # A waiting press should say where it is in the line and roughly how
            # long that is. "queued" on its own reads like a stall.
            def annotate(record):
                place = PRESS.position(record["id"])
                if place:
                    record["queue_position"] = place
                    record["estimated_start_seconds"] = PRESS.estimated_wait(record["id"])
                return record

            if pid:
                data = annotate(data)
            else:
                data["presses"] = [annotate(r) for r in data["presses"]]
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

        if route == "/v1/sprites":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            payload, _ = self._body()
            if payload is None:
                return              # 413 already sent
            problem = sprite_limits(payload)
            if problem:
                self._send_json({"code": 400, "error": problem}, 400)
                return
            method = payload.get("method") or DEFAULT_SPRITE_METHOD
            problem = sprite_method_problem(method)
            if problem:
                self._send_json({"code": 503, "error": problem,
                                 "method": method,
                                 "licence": SPRITE_METHODS[method]["licence"]}, 503)
                return
            if method == "kontext":
                # The weights are here but the edit path is not wired yet.
                # Refusing plainly beats accepting and quietly doing something
                # else — three endpoints have shipped from this repo doing
                # something other than what they claimed.
                self._send_json({
                    "code": 501,
                    "error": "method 'kontext' is not wired up yet — the image "
                             "backend needs an edit endpoint first. Use the "
                             "default 'sheet' method.",
                }, 501)
                return
            interpreter = sprite_python()
            if not interpreter:
                self._send_json({
                    "code": 503,
                    "error": "no interpreter available to cut sheets. Matting uses "
                             "rembg, which is deliberately kept out of the pinned "
                             "environment that serves the models — point "
                             "ANNEAL_SPRITE_PYTHON at one that has it "
                             "(see tools/README.md).",
                }, 503)
                return
            self._make_sprites(payload, interpreter)
            return

        if route == "/v1/press/review":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            payload, _ = self._body()
            if payload is None:
                return              # 413 already sent
            pid = (payload.get("id") or "").strip()
            if not pid:
                self._send_json({"code": 400, "error": "'id' is required"}, 400)
                return
            if not PRESSES.get(pid):
                self._send_json({"code": 404, "error": "no such press"}, 404)
                return
            tracks = payload.get("tracks")
            if tracks is not None and not isinstance(tracks, list):
                self._send_json({"code": 400, "error": "'tracks' must be a list"}, 400)
                return
            plan = payload.get("plan")
            if plan is not None and not isinstance(plan, dict):
                self._send_json({"code": 400, "error": "'plan' must be an object"}, 400)
                return
            try:
                # Amend first, then approve, so one call can do both — which is
                # what a reviewer pressing "looks good" after an edit does.
                if plan or tracks:
                    PRESS.amend(pid, plan, tracks)
                record = PRESS.approve(pid) if payload.get("approve") else PRESSES.get(pid)
            except ValueError as exc:
                # Not awaiting review: it is running, finished, or never paused.
                self._send_json({"code": 409, "error": str(exc)}, 409)
                return
            self._send_json({"data": record, "code": 200, "error": None})
            return

        if route == "/v1/press":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            payload, _ = self._body()
            if payload is None:
                return              # 413 already sent
            if not (payload.get("prompt") or "").strip():
                self._send_json({"code": 400, "error": "'prompt' is required"}, 400)
                return
            problem = press_limits(payload)
            if problem:
                self._send_json({"code": 400, "error": problem}, 400)
                return
            # Admission goes through the queue: at most one press runs and the
            # rest wait, because Press assumes it owns the model ordering for
            # its whole run. Minutes to tens of minutes either way, so it still
            # cannot be a blocking request; the caller polls GET /v1/press?id=.
            pid = PRESS.submit(payload)
            position = PRESS.position(pid)
            body = {"press_id": pid, "poll": "/v1/press?id=" + pid,
                    "state": "queued" if position else "planning"}
            if position:
                body["queue_position"] = position
                body["estimated_start_seconds"] = PRESS.estimated_wait(pid)
            self._send_json({"data": body, "code": 200, "error": None})
            return

        if route == "/v1/press/resume":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            payload, _ = self._body()
            if payload is None:
                return              # 413 already sent
            pid = payload.get("id") or payload.get("press_id")
            press = PRESSES.get(pid) if pid else None
            if not press:
                self._send_json({"code": 404, "error": "no such press"}, 404)
                return
            if press["state"] in ("planning", "lyrics", "music", "art"):
                self._send_json({"code": 409, "error": "that press is already running"}, 409)
                return
            threading.Thread(target=PRESS.run, args=(pid,), kwargs={"resume": True},
                             daemon=True).start()
            self._send_json({"data": {"press_id": pid, "resuming": True}, "code": 200, "error": None})
            return

        if route == "/v1/press/cancel":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            payload, _ = self._body()
            if payload is None:
                return              # 413 already sent
            pid = payload.get("id") or payload.get("press_id")
            PRESS.cancel(pid)
            self._send_json({"data": {"cancelled": pid}, "code": 200, "error": None})
            return

        if route == "/v1/vector":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            payload, _ = self._body()
            if payload is None:
                return              # 413 already sent
            self._send_vector(payload)
            return

        if route == "/v1/text":
            # A deliberately small contract: prompt in, text out. The
            # OpenAI-shaped /v1/chat/completions remains for clients that want
            # messages, streaming and the rest.
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            payload, _ = self._body()
            if payload is None:
                return              # 413 already sent
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
            if payload is None:
                return              # 413 already sent
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
            drop_files = (q.get("files") or ["0"])[0] in ("1", "true")
            press = PRESSES.get(pid) if pid else None
            if not press:
                self._send_json({"code": 404, "error": "no such press"}, 404)
                return
            PRESS.cancel(pid)                       # stop it if still running
            removed = 0
            if drop_files:
                for t in press.get("tracks") or []:
                    p = self._path_from_file_url(t.get("file"))
                    if p and outputs.delete(p):
                        removed += 1
                cover = (press.get("cover") or {}).get("path")
                if cover and outputs.delete(cover):
                    removed += 1
            PRESSES.delete(pid)
            self._send_json({"data": {"deleted": pid, "files_removed": removed},
                             "code": 200, "error": None})
            return
        if route == "/v1/press/download":
            if not self._authorized():
                self._send_json({"code": 401, "error": "unauthorized"}, 401)
                return
            self._send_press_zip()
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

    # A press runs in a thread; a restart leaves its record claiming to be
    # working with nothing behind it. Reconcile before serving.
    try:
        PRESS.sweep_interrupted()
        # A queue that does not survive a restart is a queue that loses work.
        # Anything that was still waiting is still waiting, and now nothing is
        # running, so the next one goes.
        started = PRESS.start_next()
        if started:
            log("press %s started from the queue after restart" % started)
    except Exception as exc:
        log("press sweep failed: %r" % exc)

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
