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
  "concept": "one sentence on the through-line",
  "cover_art": "a vivid visual description for the cover, no text or lettering in the image",
  "tracks": [
    {{"title": "track title", "theme": "what this song is about, one line",
      "style": "genre, instruments, mood, tempo feel",
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

    def cancel(self, pid):
        self._cancelled.add(pid)

    def _check(self, pid):
        if pid in self._cancelled:
            raise RuntimeError("cancelled")

    def run(self, pid):
        try:
            self._run(pid)
        except Exception as exc:
            state = "cancelled" if str(exc) == "cancelled" else "failed"
            self.log("press %s %s: %s" % (pid, state, exc))
            self.store.update(pid, state=state, error=str(exc), stage_note="")

    # -- stages -----------------------------------------------------------
    def _run(self, pid):
        press = self.store.get(pid)
        req = press["request"]
        count = max(1, min(int(req.get("tracks", 1)), 8))
        single = count == 1

        # 1. Plan — one text call for the whole record.
        self._check(pid)
        self.store.update(pid, state="planning", stage_note="Planning the record")
        # A nominal length that the plan varies around, rather than one length
        # imposed on every track.
        target = int(req.get("duration", 90))
        dmin = max(20, int(req.get("duration_min", round(target * 0.6))))
        dmax = min(600, int(req.get("duration_max", round(target * 1.5))))
        if dmax <= dmin:
            dmax = dmin + 30
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
                return target
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
                "prompt": t["style"],
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
        self.store.update(pid, state="done", stage_note="%d/%d track(s) recorded" % (ok, len(tracks)))
        self.log("press %s finished: %d/%d tracks" % (pid, ok, len(tracks)))

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
