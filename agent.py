#!/usr/bin/env python3
"""Agent mode: a working folder, and the tools that are already here.

Chat could call tools — `finish_reason: tool_calls` with well-formed arguments,
measured against the running gateway — and nothing was on the other end of a
call. This puts Anneal's own generators there, gives the model a folder to write
into, and runs the loop until it stops asking or a cap is hit.

**Everything happens inside one directory and nothing executes.** There is no
shell tool and no network tool, so the worst a confused model can do is fill a
folder with bad files. That only holds if the folder actually contains it, which
is what `safe_path` is for and most of what the tests are about: `..`, absolute
paths, symlinks out, and the sibling directory whose name merely starts the same.

The loop is deliberately dull. It calls, it executes, it feeds the result back,
it stops at a cap. Everything interesting is in the tools, and the tools are the
ones the rest of Anneal already exposes.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid

MAX_STEPS = 12
# A wall-clock stop as well as a step count: one generate_music call can take
# minutes, so twelve steps is not a bound on time.
MAX_SECONDS = 1800
MAX_READ_BYTES = 200 * 1024
MAX_WRITE_BYTES = 2 * 1024 * 1024
MAX_LISTING = 200

# What one trace row keeps once it is written down. The live result is already
# cut at 4000 characters for the model; the stored copy is smaller again,
# because the record is read whole on every poll and a twelve-step run whose
# results are file contents is not small. `args` is cut too: a write_file call
# carries the entire file in `args.content`, which would put a second copy of
# everything the run made into the row.
TRACE_RESULT_CHARS = 1200
TRACE_ARG_CHARS = 300

# How much of the folder's past goes in front of a new prompt. A follow-up used
# to arrive as a brand-new task (#70), and the fix is bounded on purpose: the
# whole history is the context window, and the trace already holds enough of a
# write_file call to replay a site byte for byte if nothing cut it.
HISTORY_RUNS = 6
HISTORY_PROMPT_CHARS = 600
HISTORY_REPLY_CHARS = 800
HISTORY_TOOLS = 20
HISTORY_FILES = 40

# The one non-terminal state. A run is a single loop with no stages to be in.
RUNNING = "running"
TERMINAL_STATES = ("done", "failed", "cancelled", "interrupted")


def safe_path(root, rel):
    """An absolute path inside `root`, or ValueError.

    Resolved with realpath before the check, so a symlink pointing out is caught
    — joining and comparing strings is not enough. The separator on the prefix
    matters too: `/x/job-1-evil` starts with `/x/job-1` and is a different
    directory.
    """
    if not isinstance(rel, str) or not rel.strip():
        return _refuse("a path is required")
    rel = rel.strip()
    if os.path.isabs(rel):
        return _refuse("%r is an absolute path; only names inside the working "
                       "folder are allowed" % rel)
    if rel in (".", ".."):
        return _refuse("%r is not a file name" % rel)
    root_real = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root_real, rel))
    if full != root_real and not full.startswith(root_real + os.sep):
        return _refuse("%r is outside the working folder" % rel)
    return full


def _refuse(message):
    raise ValueError(message)


TOOLS = [
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a file in the working folder. "
                       "Parent directories are created. Use this for code, "
                       "text, markup — anything you author yourself.",
        "parameters": {"type": "object", "required": ["path", "content"],
                       "properties": {
                           "path": {"type": "string",
                                    "description": "Relative to the working folder, e.g. src/main.py"},
                           "content": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file from the working folder.",
        "parameters": {"type": "object", "required": ["path"],
                       "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List what is in the working folder, with sizes.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "A subdirectory. Omit for the top."}}}}},
    {"type": "function", "function": {
        "name": "generate_image",
        "description": "Generate an image and save it into the working folder. "
                       "Describe it fully; this is a text-to-image model, not an editor.",
        "parameters": {"type": "object", "required": ["prompt", "path"],
                       "properties": {
                           "prompt": {"type": "string"},
                           "path": {"type": "string", "description": "Where to save it, e.g. art/logo.png"},
                           "size": {"type": "string", "description": "e.g. 1024x1024"},
                           "cutout": {"type": "boolean",
                                      "description": "Return the subject on transparency"}}}}},
    {"type": "function", "function": {
        "name": "generate_speech",
        "description": "Speak a line and save it as audio in the working folder.",
        "parameters": {"type": "object", "required": ["text", "path"],
                       "properties": {"text": {"type": "string"},
                                      "path": {"type": "string"},
                                      "voice": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "generate_sfx",
        "description": "Generate a sound effect and save it in the working folder. "
                       "Describe it physically — 'a heavy door slamming in a stone hallway'.",
        "parameters": {"type": "object", "required": ["prompt", "path"],
                       "properties": {"prompt": {"type": "string"},
                                      "path": {"type": "string"},
                                      "seconds": {"type": "number"}}}}},
]

TOOL_NAMES = [t["function"]["name"] for t in TOOLS]


def _text(value, limit=4000):
    out = value if isinstance(value, str) else json.dumps(value)
    return out if len(out) <= limit else out[:limit] + "\n…truncated"


def run_tool(name, args, root, media=None):
    """Execute one tool. Returns (ok, result-as-text).

    Never raises for anything the model did: a refusal is a result it can read
    and act on, which is the point of a loop. Only a genuine fault propagates.
    """
    try:
        if name == "write_file":
            path = safe_path(root, args.get("path"))
            content = args.get("content")
            if not isinstance(content, str):
                return False, "content must be a string"
            if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
                return False, "that file is too large to write"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            return True, "wrote %s (%d bytes)" % (args["path"], len(content))

        if name == "read_file":
            path = safe_path(root, args.get("path"))
            if not os.path.isfile(path):
                return False, "no such file: %s" % args.get("path")
            if os.path.getsize(path) > MAX_READ_BYTES:
                return False, "that file is too large to read"
            with open(path, encoding="utf-8", errors="replace") as fh:
                return True, fh.read()

        if name == "list_files":
            base = safe_path(root, args["path"]) if args.get("path") else os.path.realpath(root)
            if not os.path.isdir(base):
                return False, "no such directory"
            rows = []
            for folder, _dirs, names in os.walk(base):
                for n in sorted(names):
                    full = os.path.join(folder, n)
                    rows.append("%s (%d bytes)" % (os.path.relpath(full, root),
                                                  os.path.getsize(full)))
                    if len(rows) >= MAX_LISTING:
                        break
                if len(rows) >= MAX_LISTING:
                    break
            return True, "\n".join(rows) or "the working folder is empty"

        if name in ("generate_image", "generate_speech", "generate_sfx"):
            if not media:
                return False, "generation is not available here"
            path = safe_path(root, args.get("path"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            return media(name, dict(args), path)

        return False, ("unknown tool %r — the tools are: %s"
                       % (name, ", ".join(TOOL_NAMES)))
    except ValueError as exc:
        return False, str(exc)
    except Exception as exc:                     # noqa: BLE001 - reported, not raised
        return False, "%s failed: %s" % (name, exc)


def run(prompt, root, chat, max_steps=MAX_STEPS, max_seconds=MAX_SECONDS,
        media=None, on_step=None, system=None, should_stop=None, history=None):
    """Drive the model until it stops calling tools, or a cap is reached.

    `chat(messages, tools)` is injected rather than imported: the loop is the
    part worth testing on its own, and it should not need a model to test.

    `should_stop()` is asked before every tool call, and between turns. Not
    during a model call: one already in flight is not interruptible from here,
    so cancelling a run waiting on its first token takes until that token
    arrives. A tool call already running finishes too.
    """
    # `history` is earlier turns in this same folder, oldest first — see
    # `conversation`. Without them a follow-up is a brand-new task: "now wire
    # the image in" reaching the model with no idea what the image is, what the
    # files are called, or that anything happened here before (#70).
    messages = [{"role": "system", "content": system or SYSTEM}]
    messages.extend(list(history or []))
    messages.append({"role": "user", "content": prompt})
    started = time.time()
    trace, steps, stopped, reply = [], 0, None, ""
    nudged = False

    while True:
        if should_stop and should_stop():
            stopped = "cancelled"
            break
        turn = chat(messages, TOOLS) or {}
        calls = turn.get("tool_calls") or []
        reply = (turn.get("content") or "").strip()
        if not calls:
            # A smaller model often answers the first turn with a *plan* — "I
            # will create index.html, then a stylesheet" — and stops, because
            # describing the work reads like doing it. Nudge once, and only
            # once: if it says the same thing again it means it, and asking
            # repeatedly would be a loop of its own.
            if not trace and not nudged:
                nudged = True
                messages.append({"role": "assistant", "content": turn.get("content") or ""})
                messages.append({"role": "user", "content": NUDGE})
                continue
            break

        messages.append({"role": "assistant", "content": turn.get("content") or "",
                         "tool_calls": calls})
        for call in calls:
            # Asked before each call, not only between turns. A model can emit
            # a whole batch in one turn — measured here: cancelled at 4s with
            # nothing done yet, and the run went on to take 19 steps, 9 of them
            # write_file, after being asked to stop. Between turns is not
            # between steps when a turn carries nineteen of them.
            if should_stop and should_stop():
                stopped = "cancelled"
                break
            fn = (call.get("function") or {})
            name = fn.get("name") or ""
            # Bound before the try: a call whose arguments will not parse still
            # has to be recorded, and reaching for `args` in that branch was an
            # UnboundLocalError — a crash in the handler for a model's mistake.
            args = {}
            try:
                parsed = json.loads(fn.get("arguments") or "{}")
                if not isinstance(parsed, dict):
                    raise ValueError("arguments must be an object")
                args = parsed
            except ValueError as exc:
                ok, result = False, "could not read the arguments: %s" % exc
            else:
                ok, result = run_tool(name, args, root, media)

            steps += 1
            entry = {"step": steps, "tool": name, "args": args,
                     "ok": ok, "result": _text(result)}
            trace.append(entry)
            if on_step:
                on_step(entry)
            messages.append({"role": "tool", "tool_call_id": call.get("id") or "",
                             "name": name, "content": _text(result)})

        if stopped or (should_stop and should_stop()):
            stopped = "cancelled"
            break
        if steps >= max_steps:
            stopped = "step cap of %d reached" % max_steps
            break
        if time.time() - started > max_seconds:
            stopped = "time cap reached"
            break

    return {"reply": reply, "steps": steps, "trace": trace, "stopped": stopped,
            "seconds": round(time.time() - started, 1)}


NUDGE = ("Do it now — call the tools. A plan is not the work: nothing exists "
         "until write_file has run. Start with the first file.")

SYSTEM = """You are working inside one folder on a local machine.

Use the tools to do what is asked. Write real files with write_file — do not
paste code into your reply and call it done, and do not describe a plan and stop:
nothing exists until write_file has run. Make the first file on your first turn. Check your work with list_files and
read_file.

You can generate images, speech and sound effects; each saves into the folder.
Describe what you want fully, because these are generators and not editors.

Everything is relative to the working folder. You cannot run commands, reach the
network, or touch anything outside it — do not claim you have.

Nothing you make is delivered until something points at it. An image, a track or
a voice line you generate, and any page you add to a site, has to be referenced
from the page that should link to it: read that page, and write it back with the
reference in. Writing the new file is half the job — a file sitting in the folder
that nothing links to is work that was asked for and not received.

Earlier turns may be shown above. They are work you already did and the files
are still in the folder, so this may be a follow-up rather than a fresh start.
Call list_files before assuming what is there: the folder is the truth, and the
summary you wrote last time is not.

When the task is finished, reply with a short plain-English summary of what you
made. No tool call in that reply is how the work ends."""


# ------------------------------------------------------------- the record
#
# A run used to live entirely in the request handler: the loop ran there and
# streamed as it went, so it was exactly as durable as the tab. Closing it left
# the loop running with nothing able to re-attach — the files landed in the
# folder and the steps and the summary were gone (#67).
#
# The shape is Press's, deliberately: sqlite, one row per run, updated as each
# step completes, terminal states and an `interrupted` sweep at startup. Press
# solved this problem already and a second design would be a second thing to
# keep working.

RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id         TEXT PRIMARY KEY,
    job        TEXT NOT NULL,      -- the working folder, as agent_root named it
    prompt     TEXT NOT NULL,
    model      TEXT NOT NULL,
    state      TEXT NOT NULL,      -- running | done | failed | cancelled | interrupted
    stage      TEXT,               -- what it is doing, for a page that just arrived
    steps      INTEGER NOT NULL DEFAULT 0,
    max_steps  INTEGER NOT NULL,
    trace      TEXT NOT NULL DEFAULT '[]',
    reply      TEXT,
    stopped    TEXT,
    files      TEXT,
    error      TEXT,
    seconds    REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_job ON runs(job);
CREATE INDEX IF NOT EXISTS runs_state ON runs(state);
"""

RUN_RETENTION_SECONDS = 14 * 24 * 3600


def trimmed_step(entry):
    """One trace row, small enough to store and to re-read on every poll."""
    args = {}
    for key, value in (entry.get("args") or {}).items():
        args[key] = _text(value, TRACE_ARG_CHARS) if isinstance(value, str) else value
    return {"step": entry.get("step"), "tool": entry.get("tool"), "args": args,
            "ok": bool(entry.get("ok")),
            "result": _text(entry.get("result") or "", TRACE_RESULT_CHARS)}


def looks_like_a_printed_tool_call(reply):
    """Is this reply a tool call the model typed out instead of making?

    qwen-coder does this on a request that mixes kinds of work — writing a page
    and generating an image — and the run settles having done nothing (#73).
    That bug is upstream of here, but history is where it compounds: replayed
    as an assistant turn, the block becomes the example the next turn imitates,
    and one bad run poisons every follow-up in the folder. Measured: turn three
    reproduced turn two's JSON verbatim and called nothing.
    """
    text = (reply or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    if not text or text[0] not in "[{":
        return False
    try:
        parsed = json.loads(text)
    except ValueError:
        # A truncated block is the common case — it is cut at the reply limit —
        # so fall back to the shape rather than to the parse.
        return bool(re.match(r'^[\[{]\s*(\{\s*)?"(name|function|tool_calls)"\s*:', text))
    items = parsed if isinstance(parsed, list) else [parsed]
    return any(isinstance(i, dict) and "name" in i and "arguments" in i for i in items)


def run_summary(record):
    """What one finished run says for itself, put back as an assistant turn.

    Its own reply and nothing else. An earlier version added the tool trace and
    the file list here — "I called: write_file index.html. The folder then held:
    index.html." — and measured against qwen-coder that was worse than no
    history at all: the follow-up came back as that sentence repeated, verbatim,
    with zero tool calls. A small model imitates the last assistant turn, so
    anything machine-shaped put in its mouth is a template it will fill in
    rather than a fact it will use. Folder state is real and belongs in the
    system prompt, where it is the environment talking; see `folder_note`.
    """
    parts = []
    reply = (record.get("reply") or "").strip()
    if reply and not looks_like_a_printed_tool_call(reply):
        parts.append(_text(reply, HISTORY_REPLY_CHARS))

    state = record.get("state")
    if state and state != "done":
        # A cancelled run's files are in the folder either way, so hiding that
        # it stopped early would describe a folder that does not exist.
        parts.append("(That run did not finish — it was %s after %d step(s).)"
                     % (state, record.get("steps") or 0))

    # Never empty: an assistant turn with no content is one some backends
    # reject outright, and a run can settle having said nothing at all.
    return "\n\n".join(parts) or "(That run ended without a summary.)"


def folder_note(files):
    """What is in the working folder, for the end of the system prompt.

    The environment stating a fact, rather than the assistant being made to
    claim it. This is also the honest source: the trace says what a run did,
    the folder says what is there now, and after six runs those differ.
    """
    names = [f.get("path") for f in (files or []) if f.get("path")]
    if not names:
        return ""
    shown = names[:HISTORY_FILES]
    more = "" if len(names) <= HISTORY_FILES else ", and %d more" % (len(names) - HISTORY_FILES)
    return ("\n\nThe folder already contains: %s%s. That work is done and those "
            "files are real — read one before changing it, and do not write it "
            "again from scratch unless you were asked to."
            % (", ".join(shown), more))


def conversation(records):
    """Earlier runs in a folder, as turns to put in front of a new prompt.

    One run becomes two turns — what was asked, and what came back — because
    that is the shape the loop already speaks and the shape a chat model is
    trained on. Oldest first, so the last thing before the new prompt is the
    most recent thing that happened.
    """
    turns = []
    for record in records or []:
        turns.append({"role": "user",
                      "content": _text(record.get("prompt") or "", HISTORY_PROMPT_CHARS)})
        turns.append({"role": "assistant", "content": run_summary(record)})
    return turns


class RunStore:
    """Every agent run, durable, readable by anything that has the id."""

    def __init__(self, path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(RUN_SCHEMA)
        self._conn.commit()
        # Cancellation is in memory on purpose: it asks a worker in this
        # process to stop, and a restart takes the worker with it — after which
        # the run is `interrupted`, not still waiting to be cancelled.
        self._cancelled = set()

    def _exec(self, sql, args=(), fetch=False):
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall() if fetch else None
            self._conn.commit()
            return rows

    # -- writing ----------------------------------------------------------
    def create(self, job, prompt, model, max_steps):
        rid = uuid.uuid4().hex[:12]
        now = time.time()
        self._exec(
            "INSERT INTO runs (id, job, prompt, model, state, stage, steps, max_steps,"
            " trace, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'starting', 0, ?, '[]', ?, ?)",
            (rid, job, prompt, model, RUNNING, int(max_steps), now, now))
        return rid

    def update(self, rid, **fields):
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join("%s = ?" % k for k in fields)
        self._exec("UPDATE runs SET %s WHERE id = ?" % cols,
                   tuple(fields.values()) + (rid,))

    def append_step(self, rid, entry):
        """Record one tool call. Read-modify-write, so it holds the lock.

        The trace is a JSON column rather than a table of its own: it is read
        whole every time and never queried across runs, and a dozen rows per
        run does not justify the join.
        """
        with self._lock:
            record = self.get(rid)
            if not record:
                return
            trace = record["trace"] + [trimmed_step(entry)]
            self.update(rid, trace=json.dumps(trace), steps=len(trace),
                        stage="step %d: %s" % (len(trace), entry.get("tool") or "?"))

    def finish(self, rid, state, reply=None, stopped=None, seconds=None,
               files=None, error=None):
        self.update(rid, state=state, stage=stopped or error or state,
                    reply=reply, stopped=stopped, seconds=seconds,
                    files=json.dumps(files or []), error=error)
        self._cancelled.discard(rid)

    def forget(self, rid):
        self._exec("DELETE FROM runs WHERE id = ?", (rid,))
        self._cancelled.discard(rid)

    # -- cancelling -------------------------------------------------------
    def cancel(self, rid):
        """Ask a run to stop. False if there is nothing to stop.

        Needed because a folder now holds at most one run: without this a
        wedged run keeps its folder until the wall-clock cap half an hour away.
        """
        record = self.get(rid)
        if not record or record["state"] != RUNNING:
            return False
        self._cancelled.add(rid)
        self.update(rid, stage="stopping")
        return True

    def cancelled(self, rid):
        return rid in self._cancelled

    # -- reading ----------------------------------------------------------
    COLUMNS = ("id, job, prompt, model, state, stage, steps, max_steps, trace,"
               " reply, stopped, files, error, seconds, created_at, updated_at")

    def get(self, rid):
        rows = self._exec("SELECT %s FROM runs WHERE id = ?" % self.COLUMNS,
                          (rid,), fetch=True)
        return self._row(rows[0]) if rows else None

    def recent(self, limit=25):
        rows = self._exec("SELECT %s FROM runs ORDER BY created_at DESC LIMIT ?"
                          % self.COLUMNS, (limit,), fetch=True) or []
        return [self._row(r) for r in rows]

    def latest_for(self, job):
        rows = self._exec("SELECT %s FROM runs WHERE job = ? ORDER BY created_at DESC"
                          " LIMIT 1" % self.COLUMNS, (job,), fetch=True)
        return self._row(rows[0]) if rows else None

    def history_for(self, job, limit=HISTORY_RUNS, exclude=None):
        """The runs this folder has already had, oldest last-N first.

        Server-side, so it survives the reconnect the page's own copy does not
        — which is why the record is the source for history rather than
        anything the client sends (#70). `exclude` is the run being started:
        `create()` has already written its row by the time this is asked, and a
        run must not be given itself as its own past.
        """
        rows = self._exec("SELECT %s FROM runs WHERE job = ? AND id IS NOT ?"
                          " ORDER BY created_at DESC, rowid DESC LIMIT ?"
                          % self.COLUMNS, (job, exclude, int(limit)), fetch=True) or []
        return [self._row(r) for r in reversed(rows)]

    def active_for(self, job):
        """The id of the run working in this folder, or None.

        Two loops writing into one directory is two models editing the same
        files with no idea about each other.
        """
        rows = self._exec("SELECT id FROM runs WHERE job = ? AND state = ?"
                          " ORDER BY created_at DESC LIMIT 1", (job, RUNNING), fetch=True)
        return rows[0][0] if rows else None

    @staticmethod
    def _row(r):
        def j(value, default):
            try:
                return json.loads(value) if value else default
            except ValueError:
                return default
        return {"id": r[0], "job": r[1], "prompt": r[2], "model": r[3],
                "state": r[4], "stage": r[5], "steps": r[6], "max_steps": r[7],
                "trace": j(r[8], []), "reply": r[9], "stopped": r[10],
                "files": j(r[11], []), "error": r[12], "seconds": r[13],
                "created": r[14], "updated": r[15]}

    # -- reconciling ------------------------------------------------------
    def sweep_interrupted(self):
        """Mark runs whose worker died as interrupted.

        A run lives in sqlite and runs in a thread. A restart keeps the record
        and loses the worker, leaving a row that claims to be working with
        nothing behind it — the same failure Press had, and the same fix.
        """
        stuck = [r for r in self.recent(200) if r["state"] == RUNNING]
        for record in stuck:
            self.update(record["id"], state="interrupted",
                        stage="interrupted by a restart after %d step(s)" % record["steps"])
        return len(stuck)

    def prune(self, older_than_seconds=RUN_RETENTION_SECONDS):
        """Drop settled runs past the cutoff. A running one is never touched."""
        self._exec("DELETE FROM runs WHERE state != ? AND updated_at < ?",
                   (RUNNING, time.time() - older_than_seconds))


# ---------------------------------------------------------------- previewing
LOCAL_LINK = re.compile(
    r"""<link\b[^>]*?href=["']([^"':?#]+\.css)["'][^>]*>""", re.I)
LOCAL_SCRIPT = re.compile(
    r"""<script\b[^>]*?src=["']([^"':?#]+\.js)["'][^>]*>\s*</script>""", re.I)


def inline_site(html, assets):
    """Fold same-folder CSS and JS into one page.

    A static site is the obvious thing to ask for, and it is the one output a
    blob URL cannot show — `<link href="style.css">` does not resolve inside a
    blob, so the page renders unstyled. Only local, extension-matched paths are
    replaced: anything with a scheme is left exactly as it is, because nothing
    here fetches from the network and the preview must not either. A file that
    is missing is left as its original tag rather than blanked, so the page
    still says what it was reaching for.
    """
    def css(m):
        body = assets.get(m.group(1))
        return "<style>%s</style>" % body if body is not None else m.group(0)

    def js(m):
        body = assets.get(m.group(1))
        return "<script>%s</script>" % body if body is not None else m.group(0)

    return LOCAL_SCRIPT.sub(js, LOCAL_LINK.sub(css, html or ""))
