#!/usr/bin/env python3
"""Durable, browsable storage for everything Anneal generates.

Until now the only reliable copy of a result was whatever the client happened to
save. The UI held results in browser memory, speech was never written to disk at
all, and music lived in a temp cache under UUID filenames that the backend
prunes. A good take could be lost by reloading a tab.

Everything the gateway proxies is now written here, named after the prompt, with
a JSON sidecar carrying the parameters that produced it. Sidecars rather than a
database because the metadata then travels with the file: copy the directory
elsewhere and it is still self-describing, and losing an index can't orphan
anything.

    outputs/
      music/  2026-08-06T09-14-02_warm-lo-fi-hip-hop_a1b2c3.mp3
              2026-08-06T09-14-02_warm-lo-fi-hip-hop_a1b2c3.mp3.json
      speech/ ...
      images/ ...
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid

import paths

KINDS = ("music", "speech", "images")
SIDECAR_SUFFIX = ".json"


def root():
    base = os.environ.get("AIMUSIC_ROOT", "/Volumes/Storage/AIMusic")
    return os.path.join(base, "outputs")


def kind_dir(kind):
    path = os.path.join(root(), kind if kind in KINDS else "other")
    os.makedirs(path, exist_ok=True)
    return path


def slugify(text, limit=48):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return slug[:limit] or "untitled"


def _stamp():
    return time.strftime("%Y-%m-%dT%H-%M-%S")


def _target(kind, prompt, ext):
    name = "%s_%s_%s%s" % (_stamp(), slugify(prompt), uuid.uuid4().hex[:6], ext)
    return os.path.join(kind_dir(kind), name)


def write_sidecar(path, meta):
    meta = dict(meta or {})
    meta.setdefault("created", time.time())
    meta.setdefault("file", os.path.basename(path))
    try:
        with open(path + SIDECAR_SUFFIX, "w") as fh:
            json.dump(meta, fh, indent=2)
    except OSError:
        pass                                   # never fail a request over metadata


def save_bytes(kind, blob, ext, meta):
    """Persist raw bytes returned by a service. Returns the path, or None."""
    try:
        path = _target(kind, (meta or {}).get("prompt"), ext)
        with open(path, "wb") as fh:
            fh.write(blob)
        write_sidecar(path, meta)
        return path
    except OSError:
        return None


def save_copy(kind, src_path, meta):
    """Copy a file a backend produced into durable storage. Returns the path."""
    try:
        if not os.path.isfile(src_path):
            return None
        ext = os.path.splitext(src_path)[1] or ".bin"
        path = _target(kind, (meta or {}).get("prompt"), ext)
        shutil.copy2(src_path, path)
        write_sidecar(path, meta)
        return path
    except OSError:
        return None


def adopt(kind, existing_path, meta):
    """Record something a backend already wrote in the right place.

    The image service writes straight into outputs/images, so there is nothing
    to copy — it just needs its sidecar.
    """
    if not existing_path or not os.path.isfile(existing_path):
        return None
    if not os.path.exists(existing_path + SIDECAR_SUFFIX):
        write_sidecar(existing_path, meta)
    return existing_path


# ------------------------------------------------------------------ listing
def _entry(path, kind):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    meta = {}
    try:
        with open(path + SIDECAR_SUFFIX) as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        pass
    return {
        "path": path,
        "name": os.path.basename(path),
        "kind": kind,
        "bytes": stat.st_size,
        # Sidecar time is when it was generated; mtime is the fallback for
        # anything produced before this existed.
        "created": meta.get("created", stat.st_mtime),
        "prompt": meta.get("prompt", ""),
        "meta": {k: v for k, v in meta.items() if k not in ("created", "file")},
        "url": "/v1/outputs/file?path=" + path,
    }


def listing(kind=None, limit=200, offset=0):
    """Everything saved, newest first."""
    wanted = [kind] if kind in KINDS else list(KINDS)
    entries = []
    for k in wanted:
        directory = os.path.join(root(), k)
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if name.endswith(SIDECAR_SUFFIX) or name.startswith("."):
                continue
            entry = _entry(os.path.join(directory, name), k)
            if entry:
                entries.append(entry)
    entries.sort(key=lambda e: e["created"], reverse=True)
    return {"total": len(entries), "items": entries[offset:offset + limit]}


def delete(path):
    """Remove an output and its sidecar. Refuses anything outside outputs/."""
    real = paths.resolve_within(path, [root()])
    if real is None:
        return False
    try:
        os.remove(real)
    except OSError:
        return False
    try:
        os.remove(real + SIDECAR_SUFFIX)
    except OSError:
        pass
    return True
