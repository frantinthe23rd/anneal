#!/usr/bin/env python3
"""Write the album's own details into the files it produced.

A pressed track carried nothing: no title, no artist, no album, no cover. The
manifest beside it knew all four, which is fine for a directory listing and no
use at all once a file has been copied into a music library, where a track with
no tags is "Unknown Artist" and a grey square.

The cover is attached rather than referenced, because a file that leaves the
machine leaves the folder behind. That is also why this runs after the cover is
painted rather than as each track lands — Press paints last, so the artwork does
not exist while the music is being made.

ffmpeg rather than a tagging library, for the same reason `trim.py` uses it:
it is already required, it handles every container here, and adding a dependency
to write six strings is not a trade worth making.

Best-effort throughout, like trimming: tagging must never be the reason a take
is lost, so anything unexpected leaves the file exactly as it was.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import paths

# Containers that can carry a picture. WAV is deliberately absent: it has no
# agreed way to hold one, and ffmpeg will either refuse or write something no
# player reads.
PICTURE_FORMATS = (".flac", ".mp3", ".m4a", ".mp4", ".aac", ".ogg")

TAG_TIMEOUT = 120


def _tag_args(tags):
    out = []
    for key, value in (tags or {}).items():
        if value in (None, ""):
            continue
        out += ["-metadata", "%s=%s" % (key, value)]
    return out


def track_tags(plan, track, total=None, year=None):
    """The tags one pressed track should carry.

    Named separately from writing them so the mapping is testable without an
    encoder, and so the zip export can tag a transcode with exactly what the
    master got.
    """
    tags = {
        "title": (track or {}).get("title") or "",
        "artist": (plan or {}).get("artist") or "",
        "album_artist": (plan or {}).get("artist") or "",
        "album": (plan or {}).get("title") or "",
    }
    number = (track or {}).get("n")
    if number:
        tags["track"] = "%d/%d" % (number, total) if total else str(number)
    if year:
        tags["date"] = str(year)
    # The per-track style line is the closest thing to a genre the plan has, and
    # an empty genre is better than a wrong one.
    style = (track or {}).get("style") or ""
    if style:
        tags["genre"] = style.split(",")[0].strip()[:60]
    return tags


def embed(path, cover=None, tags=None, timeout=TAG_TIMEOUT):
    """Write `tags` — and `cover`, if the container can hold one — into `path`.

    Returns True if the file was rewritten. The audio is copied, never
    re-encoded: this is a metadata rewrite, and a lossless master must come out
    of it bit-identical.
    """
    if not path or not os.path.isfile(path):
        return False
    if not tags and not cover:
        return False

    ext = os.path.splitext(path)[1].lower()
    want_cover = bool(cover) and os.path.isfile(cover) and ext in PICTURE_FORMATS

    ffmpeg = paths.ffmpeg_bin()
    handle, tmp = tempfile.mkstemp(suffix=ext, dir=os.path.dirname(path))
    os.close(handle)
    try:
        cmd = [ffmpeg, "-v", "error", "-y", "-i", path]
        if want_cover:
            cmd += ["-i", cover]
        cmd += ["-map", "0:a"]
        if want_cover:
            cmd += ["-map", "1:v", "-c:v", "copy", "-disposition:v:0", "attached_pic",
                    "-metadata:s:v", "title=Album cover",
                    "-metadata:s:v", "comment=Cover (front)"]
        cmd += ["-c:a", "copy"]
        cmd += _tag_args(tags)
        cmd.append(tmp)
        subprocess.run(cmd, check=True, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # A rewrite that produced nothing is a failure ffmpeg did not report.
        if os.path.getsize(tmp) <= 0:
            raise RuntimeError("tagging produced an empty file")
        shutil.copystat(path, tmp)
        os.replace(tmp, path)
        return True
    except Exception:
        return False
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def has_cover(path):
    """True if `path` already carries an attached picture. For tests and for
    anything that wants to avoid a second rewrite."""
    try:
        out = subprocess.run(
            [paths.ffprobe_bin(), "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30)
        return bool(out.stdout.strip())
    except Exception:
        return False
