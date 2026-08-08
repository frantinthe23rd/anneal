#!/usr/bin/env python3
"""One containment check, in one place, with tests.

Four handlers took a caller-supplied `path`, resolved it, and compared it
against a list of allowed roots. Each did it slightly differently, and two
places that fed a path to a subprocess or a zip archive did not check at all —
they trusted a value that had made a round trip through sqlite and, before that,
through a language model's output.

The check itself is not subtle, but it is exactly the kind of thing that is
wrong in one of five copies. It is also the only thing standing between
`?path=../../../etc/passwd` and an open file, so it is worth being able to test
in isolation rather than only through a running server.

Two rules that the ad-hoc copies got right and are easy to get wrong:

- **Resolve before comparing.** `..` and symlinks are the whole attack; a
  string comparison against the unresolved path is decorative.
- **Compare against `root + os.sep`, never a bare prefix.** `/tmp/outputs-evil`
  starts with `/tmp/outputs` and must not be accepted.

A root itself is not contained in itself. Every caller here wants a *file*
under a root, never the root, so that is the useful definition.
"""

from __future__ import annotations

import os

__all__ = ["contained", "resolve_within", "safe_file", "ffmpeg_bin",
           "aimusic_root", "under_root", "hf_home", "hf_snapshot"]


def _real(path):
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def contained(path, roots):
    """Is `path` strictly underneath any of `roots`?

    Both sides are fully resolved first, so symlinks and `..` are followed
    before the comparison rather than after it.
    """
    if not path:
        return False
    try:
        real = _real(path)
    except (OSError, ValueError):
        return False
    for root in roots or ():
        try:
            root_real = _real(root)
        except (OSError, ValueError):
            continue
        if real == root_real:
            continue                       # the root itself is not a member
        if real.startswith(root_real.rstrip(os.sep) + os.sep):
            return True
    return False


def resolve_within(path, roots):
    """The resolved path if it is inside one of `roots`, else None.

    Callers should use the returned value rather than the one they passed in:
    it is the resolved one, so nothing downstream can be handed a path that was
    checked in one form and opened in another.
    """
    if not path:
        return None
    try:
        real = _real(path)
    except (OSError, ValueError):
        return None
    return real if contained(real, roots) else None


def safe_file(path, roots):
    """`resolve_within`, and it must be an existing regular file.

    A directory that passes containment is still not something to open, stream
    or hand to ffmpeg.
    """
    real = resolve_within(path, roots)
    if real is None:
        return None
    try:
        if not os.path.isfile(real):
            return None
    except OSError:
        return None
    return real


# --------------------------------------------------------------- binaries
# Resolved, not trusted to PATH.
#
# The gateway runs under launchd, which hands a job
# PATH=/usr/bin:/bin:/usr/sbin:/sbin — no /opt/homebrew/bin. Every `["ffmpeg",
# ...]` therefore died with FileNotFoundError(2) the moment Anneal started
# serving from the LaunchAgent rather than a terminal, and nothing caught it
# because every test and every manual check ran from a shell that had Homebrew
# on PATH. That error names nothing, which is how it reached a user.
FFMPEG_CANDIDATES = (
    "/opt/homebrew/bin/ffmpeg",     # Apple silicon Homebrew
    "/usr/local/bin/ffmpeg",        # Intel Homebrew, and most manual installs
    "/opt/local/bin/ffmpeg",        # MacPorts
    "/usr/bin/ffmpeg",
)
_FFMPEG = None


def ffmpeg_bin(candidates=FFMPEG_CANDIDATES, search_path=True):
    """Absolute path to ffmpeg, or a RuntimeError that says what is missing."""
    global _FFMPEG
    if _FFMPEG and search_path and candidates is FFMPEG_CANDIDATES:
        return _FFMPEG
    found = None
    if search_path:
        import shutil
        found = shutil.which("ffmpeg")
    if not found:
        for candidate in candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                found = candidate
                break
    if not found:
        raise RuntimeError(
            "ffmpeg was not found. It is needed to transcode audio (MP3 speech, "
            "and any press download that is not FLAC). Install it — `brew "
            "install ffmpeg` — or set PATH for the service. Looked on PATH and "
            "in: %s" % ", ".join(candidates))
    if search_path and candidates is FFMPEG_CANDIDATES:
        _FFMPEG = found
    return found


# ------------------------------------------------------------- installation root
# Everything bulky — models, virtualenvs, logs, generated output, the upstream
# ACE-Step checkout — lives under one directory. On the machine this was built
# on that is an external SSD, because the internal disk is nearly full. Baking
# that path in as the default made a fresh clone fail with
#
#     ERROR: /Volumes/Storage/AIMusic not found — is the Storage SSD mounted?
#
# which reads as a hardware fault rather than "this default is one person's
# external disk" (#17). The resolution order below keeps that machine working
# without asking a stranger to own the same disk:
#
#   1. $AIMUSIC_ROOT              explicit wins, always
#   2. <repo>/.anneal-root        written by setup.sh, gitignored
#   3. /Volumes/Storage/AIMusic   only if it exists *and* looks like an install
#   4. ~/anneal                   the default for everyone else
#
# Step 3 is deliberately narrow. A bare directory of that name is not enough —
# it has to contain something Anneal put there — so a stranger who happens to
# have a volume called Storage does not silently inherit someone else's layout.

# Overridable only so both halves of the resolution can be tested on the very
# machine the constant describes: with the real volume mounted, branches 3 and
# 4 are unreachable and a test of them would be a test of nothing.
LEGACY_ROOT = os.environ.get("ANNEAL_LEGACY_ROOT") or "/Volumes/Storage/AIMusic"
LEGACY_MARKERS = ("models", "gen-venv", "hf-cache", "ACE-Step-1.5", "outputs")
DEFAULT_ROOT = "~/anneal"
ROOT_FILE = ".anneal-root"


def repo_dir():
    """The checkout this file belongs to."""
    return os.path.dirname(os.path.abspath(__file__))


def root_file():
    """Path of the file setup.sh writes the chosen root into."""
    return os.path.join(repo_dir(), ROOT_FILE)


def _looks_installed(path):
    try:
        if not os.path.isdir(path):
            return False
        return any(os.path.exists(os.path.join(path, m)) for m in LEGACY_MARKERS)
    except OSError:
        return False


def aimusic_root():
    """Where models, venvs, logs and output live. Never raises.

    Resolved on every call rather than cached: the tests change AIMUSIC_ROOT
    between cases, and a module-level constant computed at import time is
    exactly the bug that made `tests/context.py` have to run before any app
    module could be imported.
    """
    env = (os.environ.get("AIMUSIC_ROOT") or "").strip()
    if env:
        return os.path.expanduser(env)
    try:
        with open(root_file()) as handle:
            recorded = handle.read().strip()
        if recorded:
            return os.path.expanduser(recorded)
    except OSError:
        pass
    legacy = os.environ.get("ANNEAL_LEGACY_ROOT") or LEGACY_ROOT
    if _looks_installed(legacy):
        return legacy
    return os.path.expanduser(DEFAULT_ROOT)


def under_root(*parts):
    """A path under the installation root."""
    return os.path.join(aimusic_root(), *parts)


def hf_home():
    return os.environ.get("HF_HOME") or under_root("hf-cache")


def hf_snapshot(repo_id, revision=None, hf_root=None):
    """The local snapshot directory for a Hub repo, without touching the network.

    Returns None when nothing is downloaded. `revision` is preferred when it is
    present; otherwise the newest snapshot wins, because a cache holding two
    revisions of the same repo has no other way to choose and the alternative
    (alphabetical by sha) is arbitrary in a way that looks deliberate.
    """
    base = os.path.join(hf_root or hf_home(), "hub",
                        "models--" + repo_id.replace("/", "--"), "snapshots")
    if revision:
        pinned = os.path.join(base, revision)
        if os.path.isdir(pinned):
            return pinned
    try:
        names = [n for n in os.listdir(base) if os.path.isdir(os.path.join(base, n))]
    except OSError:
        return None
    if not names:
        return None
    names.sort(key=lambda n: os.path.getmtime(os.path.join(base, n)), reverse=True)
    return os.path.join(base, names[0])
