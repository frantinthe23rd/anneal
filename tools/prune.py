#!/usr/bin/env python3
"""Delete old generated output — deliberately, never automatically.

Nothing in Anneal removes anything on its own, and that is a decision rather
than an omission. Generation is not deterministic, so a deleted take cannot be
regenerated: an automatic policy that removes the wrong one is unrecoverable.
`/health` reports how much space `outputs/` is using so the problem is visible;
this is the command you run when you want to act on it.

Two safeguards follow from that:

  * **It does not delete unless you say so.** Without `--delete` it prints what
    it would remove and exits.
  * **It will not orphan a record.** Press keeps the paths of its tracks and
    cover, so removing those files by age alone leaves an album in the Library
    that cannot play. Referenced files are skipped unless you pass
    `--include-pressed`, and the summary says how many were spared.

    ./tools/prune.py --older-than 90              # what would go
    ./tools/prune.py --older-than 90 --delete     # actually go
    ./tools/prune.py --older-than 365 --kind music --delete
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import outputs  # noqa: E402
import paths    # noqa: E402

AIMUSIC_ROOT = paths.aimusic_root()
PRESS_DB = os.path.join(AIMUSIC_ROOT, "presses.db")


def referenced_paths(db_path=None):
    """Every output path a press record points at.

    Track files are stored as the request URL the UI plays, not as a bare
    path — `/v1/audio?path=%2FVolumes%2F...` — so the path has to be pulled
    back out of the query string. The cover is stored plainly. Missing either
    of those would make this quietly useless, which is why it is tested.
    """
    # Resolved at call time, not bound as a default: a default argument freezes
    # PRESS_DB as it was when this module was imported, so anything that points
    # it elsewhere afterwards would leave this reading a database that is not
    # the one in use — and silently protecting nothing.
    db_path = db_path or PRESS_DB
    found = set()
    if not os.path.exists(db_path):
        return found
    try:
        db = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        rows = db.execute("SELECT tracks, cover FROM presses").fetchall()
        db.close()
    except sqlite3.Error:
        # A locked or unreadable database is not a licence to delete the files
        # it might be protecting.
        raise
    for tracks, cover in rows:
        for t in json.loads(tracks or "[]"):
            ref = t.get("file") or t.get("path")
            if not ref:
                continue
            if "path=" in ref:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(ref).query)
                ref = (q.get("path") or [""])[0]
            if ref:
                found.add(os.path.realpath(ref))
        c = json.loads(cover or "null") or {}
        if c.get("path"):
            found.add(os.path.realpath(c["path"]))
    return found


def candidates(older_than_days, kinds, now=None):
    """Files older than the cutoff, newest-first within each kind."""
    now = time.time() if now is None else now
    cutoff = now - older_than_days * 86400
    out = []
    for kind in kinds:
        d = os.path.join(outputs.root(), kind)
        # Walk: sprites write a directory per set, and offering the directory as
        # a candidate would report a nonsense size and then fail to delete it,
        # since outputs.delete uses os.remove. Fails safe, but reports wrongly.
        for folder, _dirs, names in os.walk(d):
            for name in names:
                if name.endswith(outputs.SIDECAR_SUFFIX):
                    continue       # goes with its file, not on its own
                path = os.path.join(folder, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    out.append({"path": os.path.realpath(path), "kind": kind,
                                "bytes": st.st_size, "mtime": st.st_mtime})
    return sorted(out, key=lambda f: f["mtime"])


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--older-than", type=int, default=90, metavar="DAYS",
                    help="delete output last modified more than DAYS ago (default 90)")
    ap.add_argument("--kind", action="append", choices=list(outputs.KINDS),
                    help="limit to one kind; repeatable. Default: all")
    ap.add_argument("--include-pressed", action="store_true",
                    help="also remove files a press record points at, breaking those records")
    ap.add_argument("--delete", action="store_true",
                    help="actually delete. Without this, nothing is removed")
    args = ap.parse_args(argv)

    kinds = args.kind or list(outputs.KINDS)
    files = candidates(args.older_than, kinds)
    if not files:
        print("Nothing older than %d days in %s." % (args.older_than, ", ".join(kinds)))
        return 0

    keep = set() if args.include_pressed else referenced_paths()
    doomed = [f for f in files if f["path"] not in keep]
    spared = len(files) - len(doomed)
    total = sum(f["bytes"] for f in doomed)

    for f in doomed:
        age = int((time.time() - f["mtime"]) / 86400)
        print("  %-7s %9s  %4dd  %s" % (f["kind"], human(f["bytes"]), age,
                                        os.path.basename(f["path"])))

    print("\n%d file(s), %s%s" % (len(doomed), human(total),
          "" if not spared else ", and %d kept because a press still points at them" % spared))

    if not args.delete:
        print("Nothing deleted. Pass --delete to remove them.")
        return 0

    removed = errors = 0
    for f in doomed:
        try:
            outputs.delete(f["path"])      # takes the sidecar with it, and re-checks the root
            removed += 1
        except Exception as exc:
            errors += 1
            print("  could not remove %s: %s" % (os.path.basename(f["path"]), exc),
                  file=sys.stderr)
    print("Deleted %d file(s)%s." % (removed, "" if not errors else ", %d failed" % errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
