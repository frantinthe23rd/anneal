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

__all__ = ["contained", "resolve_within", "safe_file"]


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
