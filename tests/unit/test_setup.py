#!/usr/bin/env python3
"""What setup.sh decides before anything is installed.

setup.sh is the documented way in — README's install section is `git clone`,
`./setup.sh`, `./anneal start` — and nothing exercised it. Steps 2 to 6 had
only ever run on the machine they were written on, and an independent review
(issue #38) found five defects on the paths a first install takes, the worst of
which sent `--root /no/such/parent/anneal` to `/anneal`: an install root nobody
typed, on the boot volume, chosen silently.

These drive the real script inside a copy of the repo, with HOME and the legacy
root pointed at throwaway directories, so the branches a stranger reaches are
the ones under test and nothing here can disturb this machine's `.anneal-root`
or its `env.local.sh`.

`--dry-run` is what makes that affordable: it reaches every step and decides
everything a real run decides. That it changes nothing is one of the claims
asserted below, because it used to write an API key while saying so.

The cases that get past step 1 need Apple silicon, uv and ffmpeg, and skip with
a reason when the script's own prerequisite check says they are missing — the
same shape as the acceptance suite skipping when no gateway answers. What runs
anywhere is the argument handling and `--models list`, which has to work before
gen-venv exists because README offers it as the way to price an install.

None of this says anything about a real install. No venv is built, no weight
moves, and a dry run that reads correctly is not an install that works.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOWHERE = "/nonexistent-anneal-legacy-volume"

# setup.sh and update.sh both take PYTHON as an override and default to
# /usr/bin/python3, which is the gateway's interpreter on the target machine
# and is also what runs this suite there. Passing it explicitly keeps the two
# the same interpreter on a machine where they would not be — CI installs its
# own, and a missing /usr/bin/python3 would otherwise be a module-level error
# rather than a skip.
PYTHON = sys.executable

# Copied out rather than run in place: setup.sh writes .anneal-root, env.sh
# writes env.local.sh, and both belong to the install this suite is running on.
# The two are excluded from the copy as well, so the sandbox starts as a fresh
# clone does — with neither.
SANDBOX_IGNORES = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", ".anneal-root", "env.local.sh")

with open(os.path.join(REPO, "setup.sh"), encoding="utf-8") as _handle:
    SETUP_SOURCE = _handle.read()

# Derived from the script, never listed here. An option that has a `--x=value`
# arm takes a value, and a step is whatever `say` announces; a copied list of
# either goes stale the moment one is added, which is how four tests in this
# repo passed while the thing they described had already changed.
VALUE_OPTIONS = re.findall(r"^\s*(--[a-z-]+)=\*\)", SETUP_SOURCE, re.M)
STEP_TITLES = re.findall(r'^say "([^"]+)"', SETUP_SOURCE, re.M)


def required_repo_ids():
    """The weights a bare `./setup.sh` would fetch, from the lockfile."""
    with open(os.path.join(REPO, "models.lock.json"), encoding="utf-8") as handle:
        lock = json.load(handle)
    return [repo for repo, spec in lock["models"].items()
            if spec.get("service") and spec.get("required", True)]


_PREREQS = None


def prerequisites_present():
    """setup.sh's own step 1, asked directly.

    Whatever it accepts is what the rest of the script gets to run under, so
    this is the same question rather than a second opinion about it.
    """
    global _PREREQS
    if _PREREQS is None:
        try:
            done = subprocess.run([PYTHON, os.path.join(REPO, "tools", "doctor.py"),
                                   "--prereqs"], capture_output=True)
            _PREREQS = done.returncode == 0
        except OSError:
            _PREREQS = False
    return _PREREQS


def sandbox_env(home, **extra):
    """The ambient environment with everything Anneal exports taken back out."""
    env = dict(os.environ)
    # The suite sandboxes itself by exporting these; inherited, they would
    # answer the question the script is being asked.
    for key in ("AIMUSIC_ROOT", "ACESTEP_DIR", "HF_HOME", "ANNEAL_DRY_RUN",
                "ACESTEP_CHECKPOINTS_DIR", "IMAGE_OUTPUT_DIR"):
        env.pop(key, None)
    env["HOME"] = home
    env["ANNEAL_LEGACY_ROOT"] = NOWHERE
    env["PYTHON"] = PYTHON
    env.update(extra)
    return env


class SandboxCase(unittest.TestCase):
    """A repo copy, a throwaway HOME, and no legacy volume to be adopted."""

    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.mkdtemp(prefix="anneal-setup-")
        cls.repo = os.path.join(cls.sandbox, "repo")
        cls.home = os.path.join(cls.sandbox, "home")
        shutil.copytree(REPO, cls.repo, ignore=SANDBOX_IGNORES, symlinks=True)
        os.makedirs(cls.home)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.sandbox, ignore_errors=True)

    def environment(self, **extra):
        return sandbox_env(self.home, **extra)

    def run_setup(self, *args, **extra):
        return subprocess.run(
            [os.path.join(self.repo, "setup.sh"), *args],
            cwd=self.repo, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, env=self.environment(**extra))

    def require_prerequisites(self):
        if not prerequisites_present():
            self.skipTest("setup.sh's prerequisite check fails here, so steps "
                          "2 onwards never run — build the prerequisites or "
                          "run this on the target hardware")


class TestArgumentHandling(SandboxCase):
    """Argument parsing happens before step 1, so these run on any machine."""

    def test_an_option_missing_its_value_names_the_option(self):
        """`./setup.sh --root` printed nothing and exited 1.

        The branch consumed a value that was not there and the loop's trailing
        `shift` then failed with nothing left to shift, which under `set -e`
        ends the script mid-parse. An unknown option was handled properly all
        along, which is the behaviour these assert against.
        """
        self.assertTrue(VALUE_OPTIONS, "no value-taking options found in setup.sh")
        for option in VALUE_OPTIONS:
            for form in (option, option + "="):
                with self.subTest(form=form):
                    done = self.run_setup(form)
                    self.assertEqual(done.returncode, 2, done.stderr)
                    self.assertIn(option, done.stderr)
                    self.assertIn("needs a value", done.stderr)

    def test_an_unknown_option_is_still_named(self):
        done = self.run_setup("--bogus")
        self.assertEqual(done.returncode, 2)
        self.assertIn("--bogus", done.stderr)

    def test_help_prints_the_usage_and_succeeds(self):
        done = self.run_setup("--help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--dry-run", done.stdout)
        self.assertNotIn("#!/usr/bin/env", done.stdout)


class TestChoosingTheRoot(SandboxCase):

    def setUp(self):
        self.require_prerequisites()

    def announced_root(self, output):
        found = re.findall(r"^\s*Root: (.+)$", output, re.M)
        return found[0] if found else None

    def test_a_root_whose_parent_is_missing_is_refused(self):
        """It used to become `/anneal`.

        `cd` to the parent is how a path that does not exist yet is made
        absolute; when that failed the substitution was empty and the basename
        was appended to nothing. The `|| ROOT="$ROOT"` guard could not fire,
        because the assignment took its status from `basename`. The case that
        produces it is an external volume that is not mounted, which is the
        state an external volume is in whenever it is unplugged.
        """
        missing = os.path.join(self.home, "not-mounted", "anneal")
        done = self.run_setup("--dry-run", "--yes", "--root", missing)
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn(os.path.dirname(missing), done.stderr)
        self.assertIsNone(self.announced_root(done.stdout),
                          "a root was chosen anyway:\n" + done.stdout)
        self.assertNotIn("mkdir", done.stdout)

    def assert_announced(self, output, expected):
        """The same directory, not necessarily the same spelling.

        setup.sh makes the root absolute through its parent and leaves symlinks
        alone, which is what paths.aimusic_root() does with the value it later
        reads back out of .anneal-root; on macOS that is the difference between
        /var and /private/var, and asserting the string would be asserting the
        temp directory's spelling.
        """
        announced = self.announced_root(output)
        self.assertIsNotNone(announced, output)
        self.assertTrue(os.path.isabs(announced), announced)
        self.assertEqual(os.path.realpath(announced), os.path.realpath(expected))

    def test_the_root_that_is_announced_is_the_root_that_was_asked_for(self):
        chosen = os.path.join(self.home, "chosen-root")
        done = self.run_setup("--dry-run", "--yes", "--root", chosen)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_announced(done.stdout, chosen)

    def test_a_relative_root_is_made_absolute(self):
        done = self.run_setup("--dry-run", "--yes", "--root", "./below-the-repo")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_announced(done.stdout,
                              os.path.join(self.repo, "below-the-repo"))

    def test_a_bare_slash_is_not_an_install_root(self):
        done = self.run_setup("--dry-run", "--yes", "--root", "/")
        self.assertNotEqual(done.returncode, 0)
        self.assertIsNone(self.announced_root(done.stdout), done.stdout)


class TestDryRunChangesNothing(SandboxCase):
    """The header says "say what it would do, change nothing"."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.before = cls.snapshot(cls.repo) if prerequisites_present() else None
        if cls.before is not None:
            cls.done = subprocess.run(
                [os.path.join(cls.repo, "setup.sh"), "--dry-run", "--yes"],
                cwd=cls.repo, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, env=sandbox_env(cls.home))
            cls.after = cls.snapshot(cls.repo)

    @staticmethod
    def snapshot(tree):
        seen = {}
        for base, _dirs, names in os.walk(tree):
            for name in names:
                path = os.path.join(base, name)
                info = os.lstat(path)
                seen[os.path.relpath(path, tree)] = (info.st_size, info.st_mtime_ns)
        return seen

    def setUp(self):
        self.require_prerequisites()

    def test_it_succeeds(self):
        self.assertEqual(self.done.returncode, 0,
                         self.done.stdout + self.done.stderr)

    def test_not_one_file_in_the_repo_changed(self):
        """env.local.sh — mode 600, a fresh API key — was written by a dry run.

        env.sh generates it on first use and setup.sh sources env.sh, which is
        not routed through the `run` wrapper that prints instead of doing.
        """
        self.assertEqual(self.before, self.after)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "env.local.sh")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".anneal-root")))

    def test_it_says_the_key_is_one_of_the_things_it_would_write(self):
        self.assertIn("env.local.sh", self.done.stdout)

    def test_the_root_is_described_and_not_created(self):
        root = os.path.join(self.home, "anneal")
        self.assertIn(root, self.done.stdout)
        self.assertFalse(os.path.exists(root))

    def test_it_prices_the_install_it_is_previewing(self):
        """Step 5 ran `update.sh --models list` from gen-venv, which a dry run
        has not built, so every preview of a first install ended in an error
        the reader could not avoid and had not caused. Listing reads the
        lockfile and stats a directory; it needs nothing from gen-venv.

        Asserted against the priced lines rather than the bare repo ids: the
        closing doctor report names every model too, so "the id appears" was
        satisfied by a run whose listing had failed."""
        for repo_id in required_repo_ids():
            self.assertRegex(self.done.stdout,
                             r"(?m)^\s+%s\s+[0-9.]+ GB" % re.escape(repo_id))

    def test_it_does_not_end_on_an_error_it_cannot_avoid(self):
        output = self.done.stdout + self.done.stderr
        self.assertNotIn("ERROR", output)
        self.assertNotIn("check(s) failed", output)


class TestStoppingSaysWhatIsAlreadyDone(SandboxCase):
    """A run killed by `set -e` said nothing about what it had finished.

    Every step is idempotent and the header's whole design is that re-running
    resumes — none of which reached the terminal. The step-5 case in the report
    was a `update.sh --models list` that exits non-zero after steps 1 to 4 have
    completed; this reaches the same trap at step 2, where it can be provoked
    without an install.
    """

    def setUp(self):
        self.require_prerequisites()

    def test_it_names_the_step_and_the_steps_before_it(self):
        parent = os.path.join(self.home, "read-only-parent")
        os.makedirs(parent, exist_ok=True)
        os.chmod(parent, 0o500)
        self.addCleanup(os.chmod, parent, 0o700)
        try:
            os.mkdir(os.path.join(parent, "probe"))
        except OSError:
            pass
        else:
            self.skipTest("this user can write into a read-only directory, so "
                          "the failure this provokes does not happen")

        done = self.run_setup("--yes", "--root", os.path.join(parent, "anneal"))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Stopped during: %s" % STEP_TITLES[1], done.stderr)
        self.assertIn(STEP_TITLES[0], done.stderr)
        self.assertIn("setup.sh again", done.stderr)


class TestListingBeforeThereIsAnythingInstalled(SandboxCase):
    """README: "`./anneal models list` prints every model, its size, and
    whether it is optional, before anything is downloaded". Before setup.sh has
    run there is no gen-venv, and that is exactly when the sizes decide whether
    to run it at all."""

    def test_the_sizes_are_available_without_gen_venv(self):
        empty = os.path.join(self.sandbox, "empty-root")
        os.makedirs(empty, exist_ok=True)
        done = subprocess.run(
            [os.path.join(self.repo, "update.sh"), "--models", "list"],
            cwd=self.repo, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, env=self.environment(AIMUSIC_ROOT=empty, ANNEAL_DRY_RUN="1"))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(os.path.exists(os.path.join(empty, "gen-venv")))
        for repo_id in required_repo_ids():
            self.assertRegex(done.stdout,
                             r"(?m)^\s+%s\s+[0-9.]+ GB" % re.escape(repo_id))


class TestTheScriptsParse(unittest.TestCase):
    """Every shell script in the repo root, found rather than listed.

    A syntax error in setup.sh is a first install that ends before it starts,
    and `bash -n` costs nothing.
    """

    def test_every_shell_script_at_the_top_level_is_valid_bash(self):
        scripts = sorted(n for n in os.listdir(REPO) if n.endswith(".sh"))
        self.assertTrue(scripts)
        for name in scripts:
            with self.subTest(script=name):
                done = subprocess.run(["bash", "-n", os.path.join(REPO, name)],
                                      capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
