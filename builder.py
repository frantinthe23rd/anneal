#!/usr/bin/env python3
"""Press — one prompt to a finished single or album.

Chains the four services: Gemma plans a concept and writes lyrics, ACE-Step
records the tracks, FLUX paints the cover.

The stage order is the whole design. Doing lyrics->music->art per track would
evict and reload a multi-gigabyte model between every step; on 16 GB that costs
three or four minutes each time and dominates the run. So every text stage runs
first, then every music stage, then the artwork — each heavy model loads exactly
once, whatever the track count:

    plan (text) -> lyrics x N (text) -> music x N (music) -> cover (image)

State lives in sqlite because a five-track album is twenty minutes of work and
must survive a gateway restart, an idle unload, or a browser being closed.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid

# What ACE-Step is asked to render at most, and the floor below which a
# track is not worth the model load. Both are enforced on every derived bound.
MAX_TRACK_SECONDS = 600
MIN_TRACK_SECONDS = 20
# The most tracks one brief may ask for. Was a literal inside the clamp below,
# which meant the UI, the spec and INTEGRATION.md each carried their own copy.
MAX_TRACKS = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS presses (
    id         TEXT PRIMARY KEY,
    request    TEXT NOT NULL,
    state      TEXT NOT NULL,     -- planning | lyrics | music | art | done | failed | cancelled
    stage_note TEXT,
    plan       TEXT,              -- album concept from the planning model
    tracks     TEXT,              -- per-track state and results
    cover      TEXT,
    error      TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

# Asked for as JSON. The planning model is a 0.6B-class instruct model, so the
# schema is kept flat and the parser forgiving rather than assuming compliance.
PLAN_PROMPT = """You are planning {what}.

Brief: {prompt}

Reply with ONLY a JSON object, no commentary, in exactly this shape:
{{
  "title": "album or song title",
  "artist": "invented band or artist name that suits the music",
  "voice": "the lead vocalist in a few words — gender, accent, timbre. The SAME performer sings every track. Say 'instrumental' only if the brief asks for no vocals",
  "concept": "one sentence on the through-line",
  "cover_art": "a vivid visual description for the cover, no text or lettering in the image",
  "tracks": [
    {{"title": "track title", "theme": "what this song is about, one line",
      "style": "genre, instruments, mood, tempo feel. Do NOT describe the singer here — the voice field covers that for every track",
      "duration_seconds": 90}}
  ]
}}

Give exactly {count} track(s). Each style should suit the brief but vary between tracks.

Vary the durations deliberately, as a real record does — between {dmin} and {dmax}
seconds. A short opener or interlude might sit near {dmin}; a centrepiece or closer
near {dmax}. Do not give every track the same length."""

LYRIC_PROMPT = """Write song lyrics.

Album: {album} — {concept}
This song: "{title}" — {theme}
Musical style: {style}

Use [verse], [chorus] and [bridge] tags on their own lines. Two verses and a
chorus is plenty. Output ONLY the lyrics — no title, no commentary, no notes."""


def slug(text, limit=48):
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return s[:limit] or "untitled"


def extract_json(text):
    """Pull a JSON object out of a model reply that may be wrapped in prose."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, depth = text.find("{"), 0
        if start < 0:
            return None
        for i, ch in enumerate(text[start:], start):
            depth += (ch == "{") - (ch == "}")
            if depth == 0:
                candidate = text[start:i + 1]
                break
    try:
        return json.loads(candidate) if candidate else None
    except ValueError:
        return None


class PressStore:
    def __init__(self, path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _exec(self, sql, args=(), fetch=False):
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall() if fetch else None
            self._conn.commit()
            return rows

    def create(self, request):
        pid = uuid.uuid4().hex[:12]
        now = time.time()
        self._exec("INSERT INTO presses (id, request, state, stage_note, tracks, created_at, updated_at)"
                   " VALUES (?, ?, 'planning', 'queued', '[]', ?, ?)",
                   (pid, json.dumps(request), now, now))
        return pid

    def update(self, pid, **fields):
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join("%s = ?" % k for k in fields)
        self._exec("UPDATE presses SET %s WHERE id = ?" % cols,
                   tuple(fields.values()) + (pid,))

    def get(self, pid):
        rows = self._exec("SELECT id, request, state, stage_note, plan, tracks, cover, error,"
                          " created_at, updated_at FROM presses WHERE id = ?", (pid,), fetch=True)
        return self._row(rows[0]) if rows else None

    def recent(self, limit=25):
        rows = self._exec("SELECT id, request, state, stage_note, plan, tracks, cover, error,"
                          " created_at, updated_at FROM presses ORDER BY created_at DESC LIMIT ?",
                          (limit,), fetch=True) or []
        return [self._row(r) for r in rows]

    def delete(self, pid):
        self._exec("DELETE FROM presses WHERE id = ?", (pid,))

    @staticmethod
    def _row(r):
        def j(v, default):
            try:
                return json.loads(v) if v else default
            except ValueError:
                return default
        return {
            "id": r[0], "request": j(r[1], {}), "state": r[2], "stage": r[3],
            "plan": j(r[4], None), "tracks": j(r[5], []), "cover": j(r[6], None),
            "error": r[7], "created": r[8], "updated": r[9],
        }


class Press:
    """Runs one press job. `call` is injected so this stays testable and the
    gateway keeps ownership of auth, model routing and lifecycle."""

    def __init__(self, store, call_text, call_music, call_image, log=print):
        self.store = store
        self.call_text = call_text        # (prompt, max_tokens) -> str
        self.call_music = call_music      # (payload) -> list of take dicts
        self.call_image = call_image      # (prompt, size) -> dict
        self.log = log
        self._cancelled = set()
        # Submission and handover both decide "is anything running", and they
        # can race: two requests arriving together would each see an idle
        # machine and both start.
        self._queue_lock = threading.RLock()

    # Non-terminal states a worker is actually inside. `queued` is deliberately
    # not here: it has no worker, so it is neither running nor interrupted.
    RUNNING_STATES = ("planning", "lyrics", "music", "art")
    TERMINAL_STATES = ("done", "failed", "cancelled", "interrupted")

    def spawn(self, pid, resume=False):
        """Start the worker. Replaced in tests, and by the gateway with a thread."""
        raise NotImplementedError("the caller must supply a way to run a press")

    def submit(self, request):
        """Accept a press, and start it only if nothing else is running.

        Press assumes it owns the machine for its whole run: every text stage,
        then every music stage, then the cover, so each heavy model loads once.
        A second press interleaved with the first would swap models between
        every stage. Refusing it was the cheap fix and is worse than nothing —
        the brief you just typed is gone. So it waits, and keeps its request.
        """
        with self._queue_lock:
            # Asked before the row exists: create() inserts straight into
            # 'planning', so a press created first would see itself as the one
            # already running and never start.
            busy = self._running_id() is not None
            pid = self.store.create(request)
            if busy:
                self.store.update(pid, state="queued",
                                  stage_note="waiting for the press ahead of it")
            else:
                self.spawn(pid)
        return pid

    def _running_id(self):
        for p in self.store.recent(200):
            if p["state"] in self.RUNNING_STATES:
                return p["id"]
        return None

    def _queued(self):
        """Oldest first — submission order is the only fair one here."""
        return sorted((p for p in self.store.recent(200) if p["state"] == "queued"),
                      key=lambda p: p.get("created") or 0)

    def position(self, pid):
        """1-based place in the queue, or None if it is not waiting."""
        for i, p in enumerate(self._queued(), start=1):
            if p["id"] == pid:
                return i
        return None

    def estimated_wait(self, pid):
        """Roughly how long until this press starts, in seconds.

        Deliberately crude: the number that matters to someone staring at a
        queue is the order of magnitude — minutes or an hour — and pretending
        to more precision than the machine can offer would be worse than
        useless. Built from what each request asked for rather than from
        history, because there is rarely enough history to mean anything.
        """
        place = self.position(pid)
        if place is None:
            return None
        seconds = 0
        running = self._running_id()
        if running:
            seconds += self._cost(self.store.get(running)) // 2   # already part-done
        for record in self._queued()[:place - 1]:
            seconds += self._cost(record)
        return int(seconds)

    @staticmethod
    def _cost(record):
        """A press's rough wall-clock cost from its own request."""
        if not record:
            return 0
        req = record.get("request") or {}
        tracks = max(1, int(req.get("tracks", 1) or 1))
        # Planning and lyrics, then roughly two and a half times real time per
        # track on the draft tier, then the cover.
        per_track = int(req.get("duration", 90) or 90) * 2.5
        return int(120 + tracks * per_track + 60)

    def start_next(self):
        """Hand the machine to whatever has been waiting longest."""
        with self._queue_lock:
            if self._running_id() is not None:
                return None
            waiting = self._queued()
            if not waiting:
                return None
            pid = waiting[0]["id"]
            # Leave 'queued' here rather than waiting for the worker's first
            # write. Otherwise there is a window where the press has been handed
            # the machine but still reports as waiting, with no position — which
            # is exactly when a caller is polling hardest.
            self.store.update(pid, state="planning", stage_note="starting")
            self.spawn(pid)
            return pid

    def finish(self, pid, state, **fields):
        """Record a terminal state and let the next press through.

        Every exit goes through here — done, failed and cancelled alike. A queue
        that only advances on success stalls permanently on the first failure,
        which is the failure mode most likely to happen unattended.
        """
        self.store.update(pid, state=state, **fields)
        self.start_next()

    def cancel(self, pid):
        self._cancelled.add(pid)
        # A queued press has no worker to notice the flag, so it is retired here
        # and will not be started by a later handover.
        record = self.store.get(pid)
        if record and record["state"] == "queued":
            self.store.update(pid, state="cancelled",
                              stage_note="cancelled before it started")

    def _check(self, pid):
        if pid in self._cancelled:
            raise RuntimeError("cancelled")

    def sweep_interrupted(self):
        """Mark presses whose worker died as interrupted.

        A press lives in sqlite but runs in a thread. If the gateway restarts
        mid-run the record survives and the worker does not, leaving a job that
        claims to be recording and never will be. Anything non-terminal at
        startup is by definition orphaned.
        """
        stuck = [p for p in self.store.recent(200)
                 if p["state"] in self.RUNNING_STATES]
        for p in stuck:
            done = sum(1 for t in p["tracks"] if t.get("state") == "done")
            self.store.update(p["id"], state="interrupted",
                              stage_note="interrupted at '%s' — %d/%d track(s) done"
                                         % (p["state"], done, len(p["tracks"])))
        if stuck:
            self.log("marked %d interrupted press(es) from a previous run" % len(stuck))
        return len(stuck)

    @staticmethod
    def track_prompt(plan, track, request):
        """The style for one track, with the record's voice pinned to it.

        A brief like "British female lead vocal" shapes the plan and then used
        to vanish: the music prompt was the planner's per-track `style` alone,
        which is defined as genre, instruments, mood and tempo and says nothing
        about who is singing. Each track is a separate generation, so with no
        voice in the prompt the model chose one per track — three of four came
        back male on a brief that asked for female.

        The voice is therefore appended to every track rather than trusted to
        appear in each style line, and the brief itself is the fallback when the
        planner omits it. This makes the request consistent; it cannot make the
        performance identical, because nothing in this path conditions on a
        speaker. Expect the same *described* singer, not the same voice.
        """
        style = (track.get("style") or request.get("prompt") or "").strip()
        voice = (plan.get("voice") or "").strip()
        if not voice:
            # The planner did not answer, so carry the brief through verbatim
            # rather than dropping the only statement of intent there is.
            voice = (request.get("prompt") or "").strip()
        if not voice or voice.lower().startswith("instrumental"):
            return style
        if request.get("instrumental"):
            return style
        return "%s. Lead vocal: %s" % (style.rstrip(". "), voice)

    def run(self, pid, resume=False):
        try:
            self._run(pid, resume=resume)
        except Exception as exc:
            state = "cancelled" if str(exc) == "cancelled" else "failed"
            self.log("press %s %s: %s" % (pid, state, exc))
            self.finish(pid, state, error=str(exc), stage_note="")

    # -- stages -----------------------------------------------------------
    def _run(self, pid, resume=False):
        press = self.store.get(pid)
        req = press["request"]
        # Resuming reuses the plan and keeps finished tracks; only the missing
        # work is redone. A five-track album is twenty minutes, so restarting
        # from scratch because of one lost track is not acceptable.
        if resume and press.get("plan") and press.get("tracks"):
            return self._resume(pid, press, req)
        count = max(1, min(int(req.get("tracks", 1)), MAX_TRACKS))
        single = count == 1

        # 1. Plan — one text call for the whole record.
        self._check(pid)
        self.store.update(pid, state="planning", stage_note="Planning the record")
        # A nominal length that the plan varies around, rather than one length
        # imposed on every track.
        # MAX_TRACK_SECONDS is the ceiling ACE-Step is asked to honour, and
        # every derived bound has to sit under it — including the lower one.
        # Clamping only dmax did not work: the `dmax <= dmin` guard below then
        # pushed the window straight back out, so duration=2000 planned tracks
        # of 1200-1230s against a 600s cap. Issue #25.
        target = min(MAX_TRACK_SECONDS, max(MIN_TRACK_SECONDS, int(req.get("duration", 90))))
        dmin = int(req.get("duration_min", round(target * 0.6)))
        dmax = int(req.get("duration_max", round(target * 1.5)))
        dmin = max(MIN_TRACK_SECONDS, min(MAX_TRACK_SECONDS, dmin))
        dmax = max(MIN_TRACK_SECONDS, min(MAX_TRACK_SECONDS, dmax))
        if dmax <= dmin:
            # Widen downwards if there is no room left above, so the guard can
            # never lift the window past the ceiling.
            dmax = min(MAX_TRACK_SECONDS, dmin + 30)
            if dmax <= dmin:
                dmin = max(MIN_TRACK_SECONDS, dmax - 30)
        raw = self.call_text(PLAN_PROMPT.format(
            what="a single song" if single else "an album of %d songs" % count,
            prompt=req["prompt"], count=count,
            dmin=dmin if not single else target, dmax=dmax if not single else target), 1400)
        plan = extract_json(raw) or {}
        tracks_plan = plan.get("tracks") or []
        # The planning model does not always honour the count; make it so rather
        # than failing a twenty-minute job over a formatting lapse.
        while len(tracks_plan) < count:
            tracks_plan.append({"title": "Untitled %d" % (len(tracks_plan) + 1),
                                "theme": req["prompt"], "style": req["prompt"]})
        tracks_plan = tracks_plan[:count]
        plan.setdefault("title", req["prompt"][:60])
        plan.setdefault("artist", "Unknown Artist")
        plan.setdefault("concept", "")
        plan.setdefault("cover_art", req["prompt"])
        plan["tracks"] = tracks_plan
        self.store.update(pid, plan=json.dumps(plan))

        def planned_duration(t):
            """Honour the plan's length, but never outside the caller's bounds."""
            if single:
                return target      # already clamped to the ceiling above
            try:
                v = int(float(t.get("duration_seconds") or target))
            except (TypeError, ValueError):
                v = target
            return max(dmin, min(dmax, v))

        tracks = [{"n": i + 1,
                   "title": t.get("title") or "Untitled %d" % (i + 1),
                   "theme": t.get("theme", ""), "style": t.get("style", req["prompt"]),
                   "duration": planned_duration(t),
                   "state": "pending", "lyrics": None, "file": None}
                  for i, t in enumerate(tracks_plan)]
        self.store.update(pid, tracks=json.dumps(tracks))

        # 2. All lyrics, while the text model is already loaded.
        if not req.get("instrumental"):
            self.store.update(pid, state="lyrics")
            for i, t in enumerate(tracks):
                self._check(pid)
                self.store.update(pid, stage_note="Writing lyrics %d/%d — %s"
                                  % (i + 1, len(tracks), t["title"]))
                t["lyrics"] = (self.call_text(LYRIC_PROMPT.format(
                    album=plan["title"], concept=plan["concept"],
                    title=t["title"], theme=t["theme"], style=t["style"]), 900) or "").strip()
                t["state"] = "lyrics-done"
                self.store.update(pid, tracks=json.dumps(tracks))

        # 3. All music, so the music model loads once.
        self.store.update(pid, state="music")
        for i, t in enumerate(tracks):
            self._check(pid)
            self.store.update(pid, stage_note="Recording %d/%d — %s"
                              % (i + 1, len(tracks), t["title"]))
            payload = {
                "prompt": self.track_prompt(plan, t, req),
                "lyrics": t["lyrics"] or "[instrumental]",
                "audio_duration": t["duration"],
                "batch_size": 1,
                "quality": req.get("quality", "draft"),
                "audio_format": req.get("audio_format", "flac"),
                "thinking": True,
            }
            try:
                takes = self.call_music(payload)
                t["file"] = (takes[0] or {}).get("file") if takes else None
                t["state"] = "done" if t["file"] else "failed"
            except Exception as exc:
                # One bad track should not lose the rest of an album.
                t["state"], t["error"] = "failed", str(exc)[:200]
                self.log("press %s track %d failed: %s" % (pid, i + 1, exc))
            self.store.update(pid, tracks=json.dumps(tracks))

        # 4. Cover last — this is the eviction, and by now music is finished.
        cover = None
        if req.get("art", True):
            self._check(pid)
            self.store.update(pid, state="art", stage_note="Painting the cover")
            try:
                brief = plan["cover_art"]
                if "album" not in brief.lower():
                    brief += ", album cover art, no text or lettering"
                cover = self.call_image(brief, req.get("art_size", "1024x1024"))
            except Exception as exc:
                self.log("press %s cover failed: %s" % (pid, exc))
                cover = {"error": str(exc)[:200]}
            self.store.update(pid, cover=json.dumps(cover))

        ok = sum(1 for t in tracks if t["state"] == "done")
        self._write_manifest(pid, plan, tracks, cover, req)
        self.finish(pid, "done", stage_note="%d/%d track(s) recorded" % (ok, len(tracks)))
        self.log("press %s finished: %d/%d tracks" % (pid, ok, len(tracks)))

    def _resume(self, pid, press, req):
        plan, tracks = press["plan"], press["tracks"]
        self.log("press %s resuming — %d/%d track(s) already done"
                 % (pid, sum(1 for t in tracks if t.get("state") == "done"), len(tracks)))

        if not req.get("instrumental"):
            self.store.update(pid, state="lyrics")
            for i, t in enumerate(tracks):
                if t.get("lyrics") or t.get("state") == "done":
                    continue
                self._check(pid)
                self.store.update(pid, stage_note="Writing lyrics %d/%d — %s"
                                  % (i + 1, len(tracks), t["title"]))
                t["lyrics"] = (self.call_text(LYRIC_PROMPT.format(
                    album=plan["title"], concept=plan.get("concept", ""),
                    title=t["title"], theme=t.get("theme", ""),
                    style=t.get("style", req["prompt"])), 900) or "").strip()
                self.store.update(pid, tracks=json.dumps(tracks))

        self.store.update(pid, state="music")
        for i, t in enumerate(tracks):
            if t.get("state") == "done" and t.get("file"):
                continue
            self._check(pid)
            self.store.update(pid, stage_note="Recording %d/%d — %s"
                              % (i + 1, len(tracks), t["title"]))
            try:
                takes = self.call_music({
                    "prompt": self.track_prompt(plan, t, req),
                    "lyrics": t.get("lyrics") or "[instrumental]",
                    "audio_duration": t.get("duration", req.get("duration", 90)),
                    "batch_size": 1, "quality": req.get("quality", "draft"),
                    "audio_format": req.get("audio_format", "flac"), "thinking": True,
                })
                t["file"] = (takes[0] or {}).get("file") if takes else None
                t["state"] = "done" if t["file"] else "failed"
            except Exception as exc:
                t["state"], t["error"] = "failed", str(exc)[:200]
            self.store.update(pid, tracks=json.dumps(tracks))

        cover = press.get("cover")
        if req.get("art", True) and not (cover or {}).get("path"):
            self._check(pid)
            self.store.update(pid, state="art", stage_note="Painting the cover")
            try:
                cover = self.call_image(plan.get("cover_art") or req["prompt"],
                                        req.get("art_size", "1024x1024"))
            except Exception as exc:
                cover = {"error": str(exc)[:200]}
            self.store.update(pid, cover=json.dumps(cover))

        ok = sum(1 for t in tracks if t.get("state") == "done")
        self._write_manifest(pid, plan, tracks, cover, req)
        self.finish(pid, "done", stage_note="%d/%d track(s) recorded" % (ok, len(tracks)))
        self.log("press %s resumed to completion: %d/%d" % (pid, ok, len(tracks)))

    def _write_manifest(self, pid, plan, tracks, cover, req):
        """Write the tracklist beside the audio, so the record is self-describing
        on disk and not only inside the gateway's database."""
        try:
            root = os.path.join(os.environ.get("AIMUSIC_ROOT", "/Volumes/Storage/AIMusic"),
                                "outputs", "albums", "%s_%s" % (time.strftime("%Y-%m-%d"), slug(plan["title"])))
            os.makedirs(root, exist_ok=True)
            total = sum(t["duration"] for t in tracks if t["state"] == "done")
            manifest = {
                "press_id": pid, "title": plan["title"], "artist": plan.get("artist"),
                "concept": plan["concept"],
                "brief": req.get("prompt"), "created": time.time(),
                "total_seconds": total,
                "cover": (cover or {}).get("path") if isinstance(cover, dict) else None,
                "tracks": [{"n": t["n"], "title": t["title"], "duration_seconds": t["duration"],
                            "style": t["style"], "theme": t["theme"],
                            "state": t["state"], "file": t.get("file")} for t in tracks],
            }
            with open(os.path.join(root, "tracklist.json"), "w") as fh:
                json.dump(manifest, fh, indent=2)

            lines = ["%s — %s" % (plan["title"], plan.get("artist", "Unknown Artist"))]
            if plan.get("concept"):
                lines.append(plan["concept"])
            lines.append("")
            for t in tracks:
                mark = "" if t["state"] == "done" else "   (%s)" % t["state"]
                lines.append("%2d. %-44s %d:%02d%s"
                             % (t["n"], t["title"][:44], t["duration"] // 60, t["duration"] % 60, mark))
            lines.append("")
            lines.append("Total %d:%02d over %d track(s)" % (total // 60, total % 60, len(tracks)))
            with open(os.path.join(root, "tracklist.txt"), "w") as fh:
                fh.write("\n".join(lines) + "\n")
            self.log("press %s manifest written to %s" % (pid, root))
        except OSError as exc:
            self.log("press %s manifest failed: %s" % (pid, exc))
