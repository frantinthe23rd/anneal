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

The tests are split by what they need, deliberately. The regression was
PATH-shaped, not ffmpeg-shaped: what matters is *where* the resolver looks and
in what order, and that can be proved against fabricated directories and stub
files with nothing installed. Those run everywhere, including CI, which is the
one place a PATH assumption is most likely to be reintroduced. Only "this
machine really has an ffmpeg" needs the binary, and that part skips.

CI states in its own header that it installs nothing, because the suite and
everything it imports are stdlib — the same constraint the gateway is built
under. Requiring a binary here broke that promise and, worse, left the repo
with a build that was always red and therefore reported nothing.
"""

import os
import shutil
import stat
import tempfile
import unittest

from tests.context import REPO_ROOT

import paths

def have_ffmpeg():
    """The resolver's own answer, so this skips only where there is genuinely
    no binary — not merely where PATH does not mention one."""
    try:
        paths.ffmpeg_bin()
        return True
    except RuntimeError:
        return False


HAVE_FFMPEG = have_ffmpeg()


def stub(directory, name="ffmpeg"):
    """An executable file that is not ffmpeg. The resolver checks for a file
    that exists and is executable, which is all this needs to be."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class ResolverCase(unittest.TestCase):
    """Fabricated PATH and fabricated candidates, so nothing here depends on
    what is installed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(setattr, paths, "_FFMPEG", paths._FFMPEG)
        paths._FFMPEG = None            # the resolver memoises; start cold
        self._path = os.environ.get("PATH")
        self.addCleanup(self.restore_path)

    def restore_path(self):
        if self._path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self._path

    def on_path(self, *dirs):
        os.environ["PATH"] = os.pathsep.join(dirs)


class TestWhereItLooks(ResolverCase):
    def test_PATH_is_tried_first(self):
        wanted = stub(os.path.join(self.tmp, "bin"))
        self.on_path(os.path.join(self.tmp, "bin"))
        self.assertEqual(paths.ffmpeg_bin(candidates=[stub(os.path.join(self.tmp, "other"))]),
                         wanted)

    def test_a_launchd_shaped_PATH_still_finds_homebrew(self):
        """The regression itself. launchd hands a job
        PATH=/usr/bin:/bin:/usr/sbin:/sbin — no /opt/homebrew/bin — so the
        candidate list is the only thing that can find the binary."""
        brew = stub(os.path.join(self.tmp, "opt", "homebrew", "bin"))
        self.on_path("/usr/bin", "/bin", "/usr/sbin", "/sbin")
        self.assertEqual(paths.ffmpeg_bin(candidates=[brew]), brew)

    def test_the_candidates_are_tried_in_order(self):
        first = stub(os.path.join(self.tmp, "a"))
        second = stub(os.path.join(self.tmp, "b"))
        self.on_path(os.path.join(self.tmp, "empty"))
        self.assertEqual(paths.ffmpeg_bin(candidates=[first, second]), first)

    def test_a_candidate_that_is_not_executable_is_not_it(self):
        plain = os.path.join(self.tmp, "c", "ffmpeg")
        os.makedirs(os.path.dirname(plain))
        open(plain, "w").close()          # exists, not executable
        good = stub(os.path.join(self.tmp, "d"))
        self.on_path(os.path.join(self.tmp, "empty"))
        self.assertEqual(paths.ffmpeg_bin(candidates=[plain, good]), good)

    def test_it_returns_an_absolute_path(self):
        """A bare name is exactly what fails under launchd."""
        found = stub(os.path.join(self.tmp, "bin"))
        self.on_path(os.path.join(self.tmp, "bin"))
        self.assertTrue(os.path.isabs(paths.ffmpeg_bin(candidates=[found])))

    def test_the_real_candidate_list_names_homebrew(self):
        # Asserted against the shipped constant rather than a copy of it: the
        # list is the fix, and a candidate dropped from it is the regression
        # coming back.
        self.assertTrue(any("homebrew" in c for c in paths.FFMPEG_CANDIDATES),
                        paths.FFMPEG_CANDIDATES)
        self.assertTrue(all(os.path.isabs(c) for c in paths.FFMPEG_CANDIDATES))


class TestWhenThereIsNone(ResolverCase):
    def test_a_missing_binary_says_so_rather_than_erroring_obscurely(self):
        """FileNotFoundError(2, 'No such file or directory') names nothing, and
        that is precisely what a user saw."""
        self.on_path(os.path.join(self.tmp, "empty"))
        with self.assertRaises(RuntimeError) as caught:
            paths.ffmpeg_bin(candidates=["/nowhere/at/all"], search_path=False)
        message = str(caught.exception)
        self.assertIn("ffmpeg", message.lower())
        self.assertIn("brew install ffmpeg", message)
        self.assertIn("/nowhere/at/all", message, "it should say where it looked")


@unittest.skipUnless(HAVE_FFMPEG, "no ffmpeg on this machine")
class TestOnAMachineThatHasOne(unittest.TestCase):
    """The only part that needs the binary. Everything above proves the
    resolution rules without it."""

    def test_it_finds_ffmpeg_on_this_machine(self):
        self.assertTrue(os.path.isfile(paths.ffmpeg_bin()))


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
