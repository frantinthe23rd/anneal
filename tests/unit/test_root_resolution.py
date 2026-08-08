"""Where Anneal installs itself, and the two places that must agree about it.

`/Volumes/Storage/AIMusic` was the default in seven files. On the machine this
was built on that is an external SSD; on a fresh clone it produced

    ERROR: /Volumes/Storage/AIMusic not found — is the Storage SSD mounted?

which reads as a hardware fault rather than a default nobody else can satisfy
(#17). The default now resolves, and the interesting part is that it resolves
*twice*: once in bash (env.sh, which decides what the launchers do) and once in
Python (paths.aimusic_root, which decides where the gateway writes). A
disagreement between them would put the databases somewhere the scripts do not
look, and neither side would raise. So the tests below run the real env.sh in a
subprocess and compare, rather than reading both and believing they match.

Both halves take ANNEAL_LEGACY_ROOT for exactly one reason: on the machine that
*has* the external volume, the legacy branch always wins, so the two fallbacks
below it are unreachable and a test of them would be a test of nothing. env.sh
is exercised from a copy in a temp directory rather than in place, so nothing
here can disturb the real .anneal-root of a working install.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from tests import context

context.install()

import paths  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOWHERE = "/nonexistent-anneal-legacy-volume"

# Files that still default to the author's volume, with the fix each needs.
# They belong to a change in flight elsewhere, so they are named here rather
# than silently excluded — and `test_the_pending_list_is_current` fails once
# one is fixed, which is what makes this a ratchet rather than a permanent
# exemption.
PENDING = {}

# A line may name the volume if it carries this marker: the legacy-detection
# branch has to name it, and so does generate.py, which is deliberately
# standalone and cannot import paths.
ALLOWED_MARKER = "ANNEAL-LEGACY-ROOT"


def scan_for_hardcoded_root():
    """Repo-root scripts that still bake in the author's external volume."""
    found = {}
    for name in sorted(os.listdir(REPO)):
        if not name.endswith((".py", ".sh")) or name == "paths.py":
            continue
        with open(os.path.join(REPO, name), encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if "/Volumes/Storage/AIMusic" not in line:
                    continue
                if line.strip().startswith("#") or ALLOWED_MARKER in line:
                    continue
                found.setdefault(name, []).append("%d: %s" % (number, line.strip()))
    return found


class ResolutionCase(unittest.TestCase):
    """A sandbox in which every branch of the resolution is reachable."""

    def setUp(self):
        self._env = dict(os.environ)
        self._root_file = paths.root_file
        self._home = tempfile.mkdtemp(prefix="anneal-home-")
        self._recorded = os.path.join(self._home, ".anneal-root")
        paths.root_file = lambda: self._recorded
        os.environ.pop("AIMUSIC_ROOT", None)
        os.environ["HOME"] = self._home
        os.environ["ANNEAL_LEGACY_ROOT"] = NOWHERE

    def tearDown(self):
        paths.root_file = self._root_file
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self._home, ignore_errors=True)


class TestPythonResolution(ResolutionCase):

    def test_environment_wins(self):
        os.environ["AIMUSIC_ROOT"] = "/tmp/explicit-root"
        self.assertEqual(paths.aimusic_root(), "/tmp/explicit-root")

    def test_environment_is_expanded(self):
        os.environ["AIMUSIC_ROOT"] = "~/somewhere"
        self.assertEqual(paths.aimusic_root(), os.path.join(self._home, "somewhere"))

    def test_blank_environment_is_not_a_root(self):
        # An exported-but-empty AIMUSIC_ROOT is what a half-written shell
        # profile produces. Treating "" as a choice would install into "/".
        os.environ["AIMUSIC_ROOT"] = "   "
        self.assertEqual(paths.aimusic_root(), os.path.join(self._home, "anneal"))

    def test_falls_back_to_home(self):
        root = paths.aimusic_root()
        self.assertEqual(root, os.path.join(self._home, "anneal"))
        self.assertNotIn("/Volumes/", root)

    def test_recorded_file_beats_the_fallback(self):
        with open(self._recorded, "w") as handle:
            handle.write("  /tmp/chosen-root  \n")
        self.assertEqual(paths.aimusic_root(), "/tmp/chosen-root")

    def test_recorded_file_is_expanded(self):
        with open(self._recorded, "w") as handle:
            handle.write("~/elsewhere\n")
        self.assertEqual(paths.aimusic_root(), os.path.join(self._home, "elsewhere"))

    def test_legacy_volume_needs_a_marker(self):
        """A bare directory of that name is not somebody's install.

        Otherwise a stranger who happens to have a volume called Storage would
        silently inherit a layout they never chose.
        """
        with tempfile.TemporaryDirectory() as fake:
            os.environ["ANNEAL_LEGACY_ROOT"] = fake
            self.assertEqual(paths.aimusic_root(), os.path.join(self._home, "anneal"),
                             "an empty directory should not be adopted as a root")
            os.makedirs(os.path.join(fake, paths.LEGACY_MARKERS[0]))
            self.assertEqual(paths.aimusic_root(), fake)

    def test_recorded_file_beats_the_legacy_volume(self):
        with tempfile.TemporaryDirectory() as legacy:
            os.makedirs(os.path.join(legacy, paths.LEGACY_MARKERS[0]))
            os.environ["ANNEAL_LEGACY_ROOT"] = legacy
            with open(self._recorded, "w") as handle:
                handle.write("/tmp/chosen-root\n")
            self.assertEqual(paths.aimusic_root(), "/tmp/chosen-root")

    def test_environment_beats_everything(self):
        with tempfile.TemporaryDirectory() as legacy:
            os.makedirs(os.path.join(legacy, paths.LEGACY_MARKERS[0]))
            os.environ["ANNEAL_LEGACY_ROOT"] = legacy
            with open(self._recorded, "w") as handle:
                handle.write("/tmp/chosen-root\n")
            os.environ["AIMUSIC_ROOT"] = "/tmp/explicit-root"
            self.assertEqual(paths.aimusic_root(), "/tmp/explicit-root")


class TestNothingHardcodesTheAuthorsVolume(unittest.TestCase):

    def test_no_new_hardcoded_roots(self):
        offenders = {name: lines for name, lines in scan_for_hardcoded_root().items()
                     if name not in PENDING}
        detail = "\n".join("%s:%s" % (name, line)
                           for name, lines in sorted(offenders.items()) for line in lines)
        self.assertEqual(offenders, {},
                         "these default to one machine's external volume; resolve "
                         "through paths.aimusic_root() instead:\n" + detail)

    def test_the_pending_list_is_current(self):
        """When a pending file is fixed, delete its entry here.

        A permanent exemption stops being a ratchet and becomes a place bugs
        hide, which is how three undocumented endpoints got out.
        """
        still_bad = set(scan_for_hardcoded_root())
        fixed = sorted(set(PENDING) - still_bad)
        self.assertEqual(fixed, [],
                         "fixed — remove from PENDING in this file: %s" % ", ".join(fixed))


class TestBashAndPythonAgree(unittest.TestCase):
    """env.sh resolves what paths.py resolves, in every branch.

    Run against a copy of env.sh in a temp directory: it derives its repo from
    BASH_SOURCE, so a copy has its own .anneal-root and its own env.local.sh
    and cannot touch the install this is running on.
    """

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="anneal-repo-")
        shutil.copy(os.path.join(REPO, "env.sh"), os.path.join(self.repo, "env.sh"))
        self.home = tempfile.mkdtemp(prefix="anneal-home-")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    def bash_root(self, **env):
        environ = dict(os.environ)
        for key in ("AIMUSIC_ROOT", "ACESTEP_DIR", "HF_HOME",
                    "ACESTEP_CHECKPOINTS_DIR"):
            environ.pop(key, None)
        environ["HOME"] = self.home
        environ["ANNEAL_LEGACY_ROOT"] = NOWHERE
        environ.update(env)
        out = subprocess.run(
            ["bash", "-c",
             'set -euo pipefail; source "$1/env.sh" >/dev/null; printf "%s" "$AIMUSIC_ROOT"',
             "_", self.repo],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environ)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        return out.stdout.decode("utf-8")

    def python_root(self, recorded_in, **env):
        """paths.aimusic_root() as it would resolve for that repo directory."""
        script = (
            "import os, sys; sys.path.insert(0, %r); import paths;"
            "paths.root_file = lambda: os.path.join(%r, '.anneal-root');"
            "print(paths.aimusic_root())" % (REPO, recorded_in))
        environ = dict(os.environ)
        for key in ("AIMUSIC_ROOT",):
            environ.pop(key, None)
        environ["HOME"] = self.home
        environ["ANNEAL_LEGACY_ROOT"] = NOWHERE
        environ.update(env)
        out = subprocess.run([os.sys.executable, "-c", script],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environ)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        return out.stdout.decode("utf-8").strip()

    def assert_agree(self, **env):
        self.assertEqual(self.bash_root(**env), self.python_root(self.repo, **env))
        return self.bash_root(**env)

    def test_environment_wins(self):
        self.assertEqual(self.assert_agree(AIMUSIC_ROOT="/tmp/explicit-root"),
                         "/tmp/explicit-root")

    def test_fallback_is_under_home(self):
        self.assertEqual(self.assert_agree(), os.path.join(self.home, "anneal"))

    def test_recorded_file_wins_and_is_trimmed_the_same_way(self):
        with open(os.path.join(self.repo, ".anneal-root"), "w") as handle:
            handle.write("  /tmp/chosen-root  \n")
        self.assertEqual(self.assert_agree(), "/tmp/chosen-root")

    def test_recorded_tilde_expands_the_same_way(self):
        with open(os.path.join(self.repo, ".anneal-root"), "w") as handle:
            handle.write("~/elsewhere\n")
        self.assertEqual(self.assert_agree(), os.path.join(self.home, "elsewhere"))

    def test_legacy_volume_needs_a_marker_in_bash_too(self):
        with tempfile.TemporaryDirectory() as fake:
            self.assertEqual(self.assert_agree(ANNEAL_LEGACY_ROOT=fake),
                             os.path.join(self.home, "anneal"))
            os.makedirs(os.path.join(fake, paths.LEGACY_MARKERS[0]))
            self.assertEqual(self.assert_agree(ANNEAL_LEGACY_ROOT=fake), fake)

    def test_everything_env_sh_exports_stays_under_the_root(self):
        """Relocating the install relocates all of it, not most of it."""
        environ = dict(os.environ)
        # The test sandbox exports ACESTEP_DIR for its own reasons. Left in
        # place it would be inherited and the assertion would pass on a value
        # env.sh never computed.
        for key in ("ACESTEP_DIR", "HF_HOME", "ACESTEP_CHECKPOINTS_DIR",
                    "UV_CACHE_DIR"):
            environ.pop(key, None)
        with tempfile.TemporaryDirectory() as root:
            out = subprocess.run(
                ["bash", "-c",
                 'set -euo pipefail; source "$1/env.sh" >/dev/null; '
                 'printf "%s\\n%s\\n%s\\n%s\\n" "$ACESTEP_DIR" "$HF_HOME" '
                 '"$ACESTEP_CHECKPOINTS_DIR" "$UV_CACHE_DIR"',
                 "_", self.repo],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=dict(environ, HOME=self.home, AIMUSIC_ROOT=root))
            self.assertEqual(out.returncode, 0, out.stderr.decode())
            lines = out.stdout.decode().strip().splitlines()
            self.assertEqual(len(lines), 4)
            for line in lines:
                self.assertTrue(line.startswith(root + os.sep),
                                "%s escaped AIMUSIC_ROOT=%s" % (line, root))


class TestServicesFollowTheRoot(unittest.TestCase):
    """The service registry is derived from the root, not from one machine.

    `services.py` carried an absolute snapshot path for the text model —
    including the sha of one person's download — so a second install would
    start mlx_lm against a directory that does not exist there.
    """

    def test_text_model_follows_the_cache_it_is_told_about(self):
        """Derived, not written down.

        Asserting "not under /Volumes/Storage" would be wrong on a machine
        that legitimately installs there — which is this one. What matters is
        that pointing HF_HOME somewhere else moves the model with it.
        """
        import importlib
        import services
        with tempfile.TemporaryDirectory() as home:
            revision = services._locked_revision(services.TEXT_MODEL_REPO)
            snapshot = os.path.join(
                home, "hub", "models--" + services.TEXT_MODEL_REPO.replace("/", "--"),
                "snapshots", revision)
            os.makedirs(snapshot)
            saved = os.environ.get("HF_HOME")
            os.environ["HF_HOME"] = home
            try:
                importlib.reload(services)
                cmd = services.SERVICES["text"]["cmd"]
                self.assertEqual(cmd[cmd.index("--model") + 1], snapshot)
            finally:
                if saved is None:
                    os.environ.pop("HF_HOME", None)
                else:
                    os.environ["HF_HOME"] = saved
                importlib.reload(services)

    def test_text_model_resolves_under_hf_home(self):
        import services
        revision = services._locked_revision(services.TEXT_MODEL_REPO)
        self.assertIsNotNone(revision, "models.lock.json should pin the text model")
        with tempfile.TemporaryDirectory() as home:
            snapshot = os.path.join(
                home, "hub", "models--" + services.TEXT_MODEL_REPO.replace("/", "--"),
                "snapshots", revision)
            os.makedirs(snapshot)
            self.assertEqual(
                paths.hf_snapshot(services.TEXT_MODEL_REPO, revision, hf_root=home),
                snapshot)

    def test_undownloaded_text_model_names_where_it_will_go(self):
        """And keeps supervisor's display name derivable.

        `supervisor.TEXT_MODEL_NAME` reads the repo id back out of this path's
        grandparent directory. A bare repo id made it the empty string, so
        /health reported `"model": ""` on any machine that had not downloaded
        the text model — found by running this suite inside a fresh clone.
        """
        import services
        saved_home = os.environ.get("HF_HOME")
        saved_override = os.environ.pop("ANNEAL_TEXT_MODEL", None)
        with tempfile.TemporaryDirectory() as empty:
            os.environ["HF_HOME"] = empty
            try:
                path = services.text_model_path()
                self.assertTrue(path.startswith(empty), path)
                self.assertFalse(os.path.exists(path),
                                 "it should name where the model will go, not "
                                 "claim it is there")
                derived = (os.path.basename(os.path.dirname(os.path.dirname(path)))
                           .replace("models--", "").replace("--", "/"))
                self.assertEqual(derived, services.TEXT_MODEL_REPO)
            finally:
                if saved_home is None:
                    os.environ.pop("HF_HOME", None)
                else:
                    os.environ["HF_HOME"] = saved_home
                if saved_override is not None:
                    os.environ["ANNEAL_TEXT_MODEL"] = saved_override

    def test_every_service_command_stays_under_the_root(self):
        """Whatever a service is launched with, it is launched from the install.

        Asserted against services.SERVICES rather than the four names that
        exist today — the copied-list mistake this repo has made five times,
        most recently in the UI where no test could see it.
        """
        import importlib
        import services
        saved = {key: os.environ.get(key) for key in ("AIMUSIC_ROOT", "ACESTEP_DIR",
                                                      "HF_HOME", "ANNEAL_TEXT_MODEL")}
        previous_root = services.AIMUSIC_ROOT
        with tempfile.TemporaryDirectory() as root:
            os.environ["AIMUSIC_ROOT"] = root
            # Both would otherwise be inherited from the ambient install and
            # the assertion would be about this machine, not about the code.
            os.environ.pop("ACESTEP_DIR", None)
            os.environ["HF_HOME"] = os.path.join(root, "hf-cache")
            try:
                importlib.reload(services)
                self.assertTrue(services.SERVICES, "no services to check")
                # The claim is that moving the root moves everything Anneal
                # owns — so nothing may still reference a *different* root.
                # System tools resolved on PATH (uv, and whatever a backend
                # shells out to) are correctly outside all of them, which is
                # why this is stated as "no other root" rather than "under
                # this one". An earlier form said "not under /Volumes/Storage"
                # and failed merely because the checkout was on that volume.
                foreign = [r for r in (previous_root, paths.LEGACY_ROOT)
                           if r and not root.startswith(r)]
                for name, spec in services.SERVICES.items():
                    self.assertTrue(spec["cwd"].startswith((root, REPO)),
                                    "%s runs in %s" % (name, spec["cwd"]))
                    self.assertTrue(spec["log"].startswith(root + os.sep),
                                    "%s logs to %s" % (name, spec["log"]))
                    for value in list(spec["cmd"]) + [spec["cwd"], spec["log"]]:
                        for other in foreign:
                            self.assertNotIn(other, str(value),
                                             "%s still references %s"
                                             % (name, other))
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                os.environ["AIMUSIC_ROOT"] = context.sandbox_root()
                importlib.reload(services)


if __name__ == "__main__":
    unittest.main()
