"""`./anneal`, `./setup.sh` and `tools/doctor.py` — the parts a stranger meets.

Ten executable entry points sat in the repo root with no documented order
and there was no path from clone to running at all. What is worth
testing about the fix is not the wording: it is that the front door still
delegates to files that exist, that `--help` lists commands the script actually
implements, and that the doctor names a prerequisite rather than letting it
surface as a stack trace three steps later.

Everything here is stdlib and nothing starts a model. `doctor` is run as a
subprocess because that is how it is used, and because it has to work under an
interpreter with no repo on the path.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import tempfile
import unittest

from tests import context

context.install()

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ANNEAL = os.path.join(REPO, "anneal")
SETUP = os.path.join(REPO, "setup.sh")
DOCTOR = os.path.join(REPO, "tools", "doctor.py")
PYTHON = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else os.sys.executable

# CI runs these on Linux, where the hardware section of the doctor stops at
# "this is not macOS" and setup.sh correctly refuses to go any further. The
# checks that read the scripts as text still run everywhere; only the two
# that need a macOS host to get past the first gate are skipped.
DARWIN = platform.system() == "Darwin"
NOT_DARWIN = "needs a macOS host: doctor stops at the platform check"


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def run(argv, env=None, cwd=None, timeout=120):
    environ = dict(os.environ)
    environ.update(env or {})
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=environ, cwd=cwd or REPO, timeout=timeout)


class TestEntryPointsExist(unittest.TestCase):

    def test_the_front_door_is_executable(self):
        for path in (ANNEAL, SETUP, DOCTOR):
            self.assertTrue(os.path.isfile(path), "%s is missing" % path)
            self.assertTrue(os.access(path, os.X_OK), "%s is not executable" % path)

    def test_every_command_in_the_help_is_implemented(self):
        """The help text and the case statement cannot drift apart.

        Both are read out of the file rather than listed here — a copied list
        of subcommands would go stale exactly the way four others in this repo
        did, and the failure would be a command that documents itself and then
        says "unknown command".
        """
        source = read(ANNEAL)
        header = source.split("set -euo pipefail", 1)[0]
        documented = set()
        for line in header.splitlines():
            match = re.match(r"#\s+\./anneal ([a-z|]+)", line)
            if match:
                documented.update(match.group(1).split("|"))

        implemented = set()
        for line in source.splitlines():
            match = re.match(r"^([a-z|\-]+)\)", line)
            if match:
                implemented.update(part for part in match.group(1).split("|")
                                   if not part.startswith("-"))

        self.assertTrue(documented, "no commands found in the header")
        self.assertEqual(documented - implemented, set(),
                         "documented but not implemented")

    def test_every_delegated_script_exists(self):
        """`anneal` is a lid on the toolbox, so the toolbox has to be there."""
        source = read(ANNEAL)
        missing = []
        for target in re.findall(r'"\$HERE/([\w./-]+)"', source):
            if not os.path.exists(os.path.join(REPO, target)):
                missing.append(target)
        self.assertEqual(missing, [], "anneal delegates to files that do not exist")

    def test_help_prints_only_the_header(self):
        """A line-numbered `sed` range started printing shell code as help the
        first time a subcommand was added. It is anchored to the comment block
        now, and this is what would notice if that regressed."""
        out = run([ANNEAL, "help"])
        self.assertEqual(out.returncode, 0, out.stderr.decode())
        text = out.stdout.decode()
        self.assertIn("./anneal setup", text)
        for leak in ("set -euo pipefail", "$HERE", "case ", "#!/"):
            self.assertNotIn(leak, text, "help leaked the implementation")

    def test_an_unknown_command_fails_loudly(self):
        out = run([ANNEAL, "definitely-not-a-command"])
        self.assertEqual(out.returncode, 2)
        self.assertIn("unknown command", out.stderr.decode())

    def test_bare_models_lists_rather_than_downloads(self):
        """A bare noun should not start fetching tens of gigabytes."""
        source = read(ANNEAL)
        models_block = source.split("\nmodels)", 1)[1].split(";;", 1)[0]
        self.assertIn("--models list", models_block)

    def test_every_shell_entry_point_parses(self):
        for name in sorted(os.listdir(REPO)):
            if not name.endswith(".sh") and name != "anneal":
                continue
            path = os.path.join(REPO, name)
            out = run(["bash", "-n", path])
            self.assertEqual(out.returncode, 0,
                             "%s: %s" % (name, out.stderr.decode()))


class TestDoctor(unittest.TestCase):

    def test_json_output_is_parseable_and_names_the_root(self):
        out = run([PYTHON, DOCTOR, "--json"])
        payload = json.loads(out.stdout.decode())
        self.assertIn("root", payload)
        self.assertTrue(payload["checks"], "doctor checked nothing")
        for row in payload["checks"]:
            for field in ("group", "name", "status", "detail", "required"):
                self.assertIn(field, row)
            self.assertIn(row["status"], ("ok", "warn", "fail"))

    def test_a_failed_required_check_sets_the_exit_status(self):
        """Otherwise setup.sh would sail past a missing prerequisite."""
        out = run([PYTHON, DOCTOR, "--json"])
        payload = json.loads(out.stdout.decode())
        required_failures = [r for r in payload["checks"]
                             if r["status"] == "fail" and r["required"]]
        plain = run([PYTHON, DOCTOR])
        self.assertEqual(plain.returncode, 1 if required_failures else 0)

    def test_every_failure_says_how_to_fix_it(self):
        """A check that reports a problem and no remedy is a stack trace with
        better formatting."""
        out = run([PYTHON, DOCTOR, "--json"])
        for row in json.loads(out.stdout.decode())["checks"]:
            if row["status"] in ("fail", "warn"):
                self.assertTrue(row["fix"] or row["detail"],
                                "%s reports a problem with no remedy" % row["name"])

    def test_it_reports_on_a_root_that_does_not_exist_yet(self):
        """The first ever run is against an empty directory — that is the whole
        point, and it must not raise."""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "not-created-yet")
            out = run([PYTHON, DOCTOR, "--json"], env={"AIMUSIC_ROOT": root})
            self.assertEqual(out.stderr.decode(), "")
            payload = json.loads(out.stdout.decode())
            self.assertEqual(payload["root"], root)
            names = {r["name"]: r for r in payload["checks"]}
            self.assertEqual(names["root"]["status"], "fail")
            self.assertIn("setup.sh", names["root"]["fix"])

    def test_prereqs_only_stops_before_the_install(self):
        """setup.sh runs this before it has anywhere to install to."""
        out = run([PYTHON, DOCTOR, "--prereqs", "--json"])
        groups = {r["group"] for r in json.loads(out.stdout.decode())["checks"]}
        self.assertEqual(groups & {"install", "models"}, set())
        self.assertIn("prereq", groups)

    @unittest.skipUnless(DARWIN, NOT_DARWIN)
    def test_it_checks_the_prerequisites_that_have_actually_bitten(self):
        """ffmpeg missing under launchd, uv missing, no Xcode CLT, and the
        16 GB floor. Each of these reached someone as an unrelated error."""
        out = run([PYTHON, DOCTOR, "--json"])
        names = " ".join(r["name"] for r in json.loads(out.stdout.decode())["checks"])
        for needed in ("ffmpeg", "uv", "Xcode", "memory", "free disk",
                       "Apple silicon", "gen-venv"):
            self.assertIn(needed, names, "doctor does not check %r" % needed)

    def test_it_checks_every_model_in_the_lockfile(self):
        """Not a list of the ones that existed when this was written."""
        with open(os.path.join(REPO, "models.lock.json"), encoding="utf-8") as handle:
            pinned = set(json.load(handle)["models"])
        out = run([PYTHON, DOCTOR, "--json"])
        checked = " ".join(r["name"] for r in json.loads(out.stdout.decode())["checks"])
        for repo in pinned:
            self.assertIn(repo, checked, "doctor does not check %s" % repo)


class TestSetup(unittest.TestCase):

    def test_help_needs_nothing_installed(self):
        out = run([SETUP, "--help"])
        self.assertEqual(out.returncode, 0, out.stderr.decode())
        self.assertIn("--root", out.stdout.decode())

    def test_it_refuses_an_unknown_option(self):
        out = run([SETUP, "--frobnicate"])
        self.assertEqual(out.returncode, 2)

    @unittest.skipUnless(DARWIN, NOT_DARWIN)
    def test_dry_run_changes_nothing_under_a_fresh_root(self):
        """The proof that a fresh-machine assumption is gone: point the root at
        an empty temp directory and setup must plan an install there rather
        than reaching for the author's external volume."""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "anneal")
            env = {"AIMUSIC_ROOT": "", "ACESTEP_DIR": "", "HF_HOME": ""}
            out = run([SETUP, "--dry-run", "--yes", "--no-models", "--root", root],
                      env=env, timeout=300)
            text = out.stdout.decode() + out.stderr.decode()
            self.assertEqual(out.returncode, 0, text)
            self.assertIn(root, text)

            # Every action it plans lands under the chosen root. Asserting on
            # the whole transcript would be wrong: the prerequisite check runs
            # before a root is chosen, so it legitimately reports free space on
            # whatever the default resolved to — on the reference machine, the
            # external volume. What must not leak is the *work*.
            planned = [line.split("would run: ", 1)[1]
                       for line in text.splitlines() if "would run: " in line]
            self.assertTrue(planned, "--dry-run planned nothing")
            self.assertTrue(any(p.startswith("git clone") for p in planned),
                            "nothing clones ACE-Step: %s" % planned)
            # Each planned action touches the chosen root, or runs a script
            # from this checkout. "does not mention /Volumes/Storage" was the
            # first form of this and it was wrong — it fails when the checkout
            # is itself on that volume, which proves nothing.
            for action in planned:
                self.assertTrue(root in action or REPO in action,
                                "planned work outside the chosen root: %s" % action)
            # Nothing was created, and in particular the real .anneal-root of
            # the machine running the tests was not rewritten.
            self.assertFalse(os.path.exists(root), "--dry-run created the root")

    def test_the_documented_order_is_the_implemented_order(self):
        """--deps before --models. Reversed, it fails with a path nobody
        recognises, which is how the ordering stayed tribal knowledge."""
        source = read(SETUP)
        self.assertLess(source.index("--deps"), source.index("--models list"),
                        "gen-venv must be built before the weights are fetched")

    def test_it_clones_the_pinned_commit(self):
        """models.lock.json recorded the commit long before anything used it."""
        source = read(SETUP)
        self.assertIn("git clone", source)
        self.assertIn("models.lock.json", source)
        self.assertIn("ACESTEP_COMMIT", source)

    def test_it_records_the_root_it_chose(self):
        """Without .anneal-root, boot.sh cannot detect an unmounted external
        volume before it is mounted, and would resolve somewhere else."""
        self.assertIn(".anneal-root", read(SETUP))


if __name__ == "__main__":
    unittest.main()
