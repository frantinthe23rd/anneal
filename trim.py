#!/usr/bin/env python3
"""Remove trailing digital silence from a generated take.

Measured across finished tracks: the music stops and the file runs on for
another two to eleven seconds of exact silence, padding out the requested
duration. In a player that gap reads as part of the track and makes an already
abrupt ending feel worse; for a builder cutting a loop it is simply wrong.

Only the tail. A rest inside the arrangement is the music, and a lead-in is a
choice — removing either would be a far worse bug than the one being fixed. So
this finds the last audible sample and cuts after it, rather than reaching for
ffmpeg's `silenceremove`, which happily eats silence anywhere.

Best-effort throughout: trimming must never be the reason a take is lost, so
anything unexpected leaves the file exactly as it was.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

import paths

# Below this, a sample is silence rather than a quiet note. Generated silence is
# exactly zero, so this only has to clear the noise floor of a lossy round trip.
SILENCE_DB = -60.0
# Leave this much after the last audible sample: a hard cut on the final sample
# clicks, and a real ending has decay worth keeping.
TAIL_PAD_SECONDS = 0.35
# Below this there is nothing worth doing. A little room after the last note is
# musical; only a gap long enough to read as dead air is padding.
MIN_TRIM_SECONDS = 0.75


def _probe_duration(path):
    out = subprocess.run(
        [paths.ffprobe_bin(), "-v", "error",
         "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True)
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def last_audible(path, threshold_db=SILENCE_DB, total=None):
    """Seconds at which the last non-silent audio occurs, or None.

    `silencedetect` reports every silent run, including ones inside the
    arrangement. The trailing one is the run that reaches the end of the file —
    ffmpeg closes it with a silence_end at EOF, so it cannot be identified by
    being unterminated, only by where it finishes.
    """
    total = total if total is not None else _probe_duration(path)
    if not total:
        return None
    done = subprocess.run(
        [paths.ffmpeg_bin(), "-v", "info", "-i", path,
         "-af", "silencedetect=noise=%ddB:d=0.3" % int(threshold_db),
         "-f", "null", "-"],
        capture_output=True, text=True)
    runs, start = [], None
    for line in (done.stderr or "").splitlines():
        if "silence_start:" in line:
            try: start = float(line.rsplit("silence_start:", 1)[1].strip())
            except ValueError: start = None
        elif "silence_end:" in line and start is not None:
            try:
                end = float(line.rsplit("silence_end:", 1)[1].split("|")[0].strip())
                runs.append((start, end))
            except ValueError:
                pass
            start = None
    if start is not None:                       # unterminated: runs to EOF
        runs.append((start, total))
    if not runs:
        return None
    begin, end = runs[-1]
    if end < total - 0.25:                      # the last silence is internal
        return None
    return begin


def trim_trailing_silence(path, pad=TAIL_PAD_SECONDS, minimum=MIN_TRIM_SECONDS):
    """Cut dead air off the end, in place. Returns the seconds removed."""
    if not path or not os.path.isfile(path):
        return 0.0
    try:
        total = _probe_duration(path)
        cut_at = last_audible(path, total=total)
        if not total or cut_at is None:
            return 0.0
        keep = min(total, cut_at + pad)
        removed = total - keep
        if removed < minimum:
            return 0.0
        # An all-silent take is a failed generation, not a file to reduce to
        # nothing — the evidence is worth more than the bytes.
        if keep <= pad:
            return 0.0

        stem, ext = os.path.splitext(path)
        handle, tmp = tempfile.mkstemp(suffix=ext, dir=os.path.dirname(path))
        os.close(handle)
        # Try a stream copy first — it is instant and cannot alter a sample.
        # But a copy can only cut on a frame boundary, and FLAC frames are long
        # enough that it silently keeps seconds more than asked for, so the
        # result is checked rather than trusted.
        def cut(extra):
            return subprocess.run(
                [paths.ffmpeg_bin(), "-v", "error", "-y", "-i", path,
                 "-t", "%.3f" % keep] + extra + [tmp],
                capture_output=True, text=True)

        done = cut(["-c", "copy"])
        accurate = (not done.returncode and os.path.getsize(tmp)
                    and _probe_duration(tmp) <= keep + 0.3)
        if not accurate:
            # Re-encode in the same codec. The extension decides it, so a FLAC
            # master is re-encoded as FLAC and stays lossless.
            done = cut([])
        if done.returncode or not os.path.getsize(tmp):
            os.unlink(tmp)
            return 0.0
        removed = total - _probe_duration(tmp)
        os.replace(tmp, path)
        return removed
    except Exception:
        return 0.0                     # never lose a take over housekeeping


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    for p in args.paths:
        if args.dry_run:
            total, cut = _probe_duration(p), last_audible(p)
            print("%s  %.1fs, last audible %s" % (p, total, "%.1fs" % cut if cut else "n/a"))
        else:
            print("%s  removed %.2fs" % (p, trim_trailing_silence(p)))
