#!/usr/bin/env python3
"""ffmpeg has to be found from a launchd job, not just from a shell.

Boot persistence put the gateway under launchd, and launchd hands a job
PATH=/usr/bin:/bin:/usr/sbin:/sbin — no /opt/homebrew/bin. Every ffmpeg call
therefore started failing with FileNotFoundError the moment the machine started
serving from the LaunchAgent instead of a terminal: MP3 speech, directed speech,
and transcoding a press to anything but FLAC.

Nothing caught it because every test and every manual check ran from a shell
that had Homebrew on PATH. The fix is to resolve the binary rather than trust
the environment, and to say which binary is missing when it is.
"""

import os
import re
import unittest

from tests.context import REPO_ROOT

import paths


class TestResolving(unittest.TestCase):
    def test_it_finds_ffmpeg_on_this_machine(self):
        self.assertTrue(os.path.isfile(paths.ffmpeg_bin()))

    def test_it_returns_an_absolute_path(self):
        """A bare name is exactly what fails under launchd."""
        self.assertTrue(os.path.isabs(paths.ffmpeg_bin()))

    def test_it_looks_beyond_PATH(self):
        """With a launchd-shaped PATH it must still find a Homebrew install."""
        old = os.environ.get("PATH")
        os.environ["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        try:
            self.assertTrue(os.path.isfile(paths.ffmpeg_bin()))
        finally:
            if old is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = old

    def test_a_missing_binary_says_so_rather_than_erroring_obscurely(self):
        """FileNotFoundError(2, 'No such file or directory') names nothing, and
        that is precisely what a user saw."""
        with self.assertRaises(RuntimeError) as caught:
            paths.ffmpeg_bin(candidates=["/nowhere/at/all"], search_path=False)
        self.assertIn("ffmpeg", str(caught.exception).lower())


class TestNothingInvokesItByBareName(unittest.TestCase):
    """The regression, pinned. A bare "ffmpeg" in argv works from a terminal and
    fails from launchd, which is the worst combination to test by hand."""

    def served_sources(self):
        for name in ("speech_server.py", "supervisor.py", "builder.py",
                     "image_server.py"):
            path = os.path.join(REPO_ROOT, name)
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    yield name, fh.read()

    def test_no_served_module_shells_out_to_a_bare_ffmpeg(self):
        for name, src in self.served_sources():
            self.assertNotRegex(
                src, r'\[\s*["\']ffmpeg["\']',
                "%s invokes ffmpeg by bare name; use paths.ffmpeg_bin()" % name)


if __name__ == "__main__":
    unittest.main()
