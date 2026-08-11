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
import time

MAX_STEPS = 12
# A wall-clock stop as well as a step count: one generate_music call can take
# minutes, so twelve steps is not a bound on time.
MAX_SECONDS = 1800
MAX_READ_BYTES = 200 * 1024
MAX_WRITE_BYTES = 2 * 1024 * 1024
MAX_LISTING = 200


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
        media=None, on_step=None, system=None):
    """Drive the model until it stops calling tools, or a cap is reached.

    `chat(messages, tools)` is injected rather than imported: the loop is the
    part worth testing on its own, and it should not need a model to test.
    """
    messages = [{"role": "system", "content": system or SYSTEM},
                {"role": "user", "content": prompt}]
    started = time.time()
    trace, steps, stopped, reply = [], 0, None, ""

    while True:
        turn = chat(messages, TOOLS) or {}
        calls = turn.get("tool_calls") or []
        reply = (turn.get("content") or "").strip()
        if not calls:
            break

        messages.append({"role": "assistant", "content": turn.get("content") or "",
                         "tool_calls": calls})
        for call in calls:
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

        if steps >= max_steps:
            stopped = "step cap of %d reached" % max_steps
            break
        if time.time() - started > max_seconds:
            stopped = "time cap reached"
            break

    return {"reply": reply, "steps": steps, "trace": trace, "stopped": stopped,
            "seconds": round(time.time() - started, 1)}


SYSTEM = """You are working inside one folder on a local machine.

Use the tools to do what is asked. Write real files with write_file — do not
paste code into your reply and call it done. Check your work with list_files and
read_file.

You can generate images, speech and sound effects; each saves into the folder.
Describe what you want fully, because these are generators and not editors.

Everything is relative to the working folder. You cannot run commands, reach the
network, or touch anything outside it — do not claim you have.

When the task is finished, reply with a short plain-English summary of what you
made. No tool call in that reply is how the work ends."""
