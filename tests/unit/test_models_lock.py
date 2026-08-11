"""Every model a service can load is in models.lock.json, with a size.

Qwen3-TTS CustomVoice — the model behind directed speech, 2.2 GB — was absent
from the lockfile for its whole life. `./download-models.sh` therefore produced
an install where nine of the documented voices 503, and nothing said so: the
lockfile looked complete because there was nothing to compare it against.

That is the copied-list failure again, in a new place. So this does not hold a
list of models. It reads the repo ids out of the servers that load them —
`speech_server.py`, `image_server.py`, `services.py`, `supervisor.py` — and
requires each one to appear in the lockfile. Adding a model to a backend and
forgetting the pin now fails here rather than on somebody else's machine.
"""

from __future__ import annotations

import json
import os
import re
import unittest

from tests import context

context.install()

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCK_PATH = os.path.join(REPO, "models.lock.json")

# A Hub repo id as it appears in a default: os.environ.get("X_MODEL", "org/name").
# Deliberately narrow — two path components, and the same character classes the
# Hub itself allows — so a URL or a filesystem path is not mistaken for one.
DEFAULT_REPO_ID = re.compile(
    r'os\.environ\.get\(\s*"[A-Z0-9_]*MODEL[A-Z0-9_]*"\s*,\s*'
    r'"([A-Za-z0-9][\w.-]*/[\w.-]+)"')

# Where a model can be named. Each of these files loads weights.
SOURCES = ("speech_server.py", "image_server.py", "services.py", "supervisor.py")


def load_lock():
    with open(LOCK_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def repo_ids_in_source():
    """Every Hub repo id that a backend defaults to loading."""
    found = {}
    for name in SOURCES:
        path = os.path.join(REPO, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as handle:
            for match in DEFAULT_REPO_ID.finditer(handle.read()):
                found.setdefault(match.group(1), []).append(name)
    return found


class TestLockfileShape(unittest.TestCase):

    def setUp(self):
        self.lock = load_lock()

    def test_every_model_has_the_fields_the_downloader_reads(self):
        for repo, spec in self.lock["models"].items():
            for field in ("revision", "target", "service", "size_gb"):
                self.assertIn(field, spec, "%s is missing %r" % (repo, field))
            self.assertRegex(spec["revision"], r"^[0-9a-f]{40}$",
                             "%s: a pin has to be a full sha, not a tag — a tag "
                             "moves, which is the whole thing the lockfile "
                             "prevents" % repo)
            self.assertGreater(float(spec["size_gb"]), 0,
                               "%s: size_gb is what lets the downloader state the "
                               "cost before spending it" % repo)

    def test_targets_are_ones_the_downloader_understands(self):
        for repo, spec in self.lock["models"].items():
            target = spec["target"]
            self.assertTrue(
                target == "hf_cache" or target.startswith("checkpoints_dir"),
                "%s: unknown target %r — update.sh --models would silently put "
                "it in the wrong place" % (repo, target))

    def test_required_models_cover_every_service_that_has_one(self):
        """A service whose models are all optional cannot be installed at all."""
        by_service = {}
        for spec in self.lock["models"].values():
            by_service.setdefault(spec["service"], []).append(spec)
        import services as service_registry
        for service, specs in by_service.items():
            if service not in service_registry.SERVICES:
                # Not a backend the supervisor starts. Sprites cut an image the
                # image service already made; sound effects are a subprocess
                # one-shot. Both are extras the gateway offers on top of the
                # services, and an extra that is entirely optional is a choice
                # rather than an uninstallable service. Derived from the
                # registry rather than a list of names that grows quietly.
                continue
            self.assertTrue(any(s.get("required", True) for s in specs),
                            "%s has no required model" % service)

    def test_a_non_commercial_model_says_so(self):
        """Nobody should discover the licence after downloading 9 GB."""
        for repo, spec in self.lock["models"].items():
            self.assertIn("licence", spec, "%s has no licence recorded" % repo)
            if "Kontext" in repo:
                self.assertIn("Non-Commercial", spec["licence"])
                self.assertIn("NON-COMMERCIAL", spec["note"].upper())

    def test_the_upstream_checkout_is_pinned_with_somewhere_to_clone_from(self):
        """setup.sh clones this; before it existed, nothing consumed the pin."""
        upstream = self.lock["upstream"]["ACE-Step/ACE-Step-1.5"]
        self.assertRegex(upstream["commit"], r"^[0-9a-f]{40}$")
        self.assertTrue(upstream.get("url", "").startswith("https://"),
                        "setup.sh needs a URL to clone")


class TestLockfileCoversWhatTheServersLoad(unittest.TestCase):

    def setUp(self):
        self.lock = load_lock()

    def test_the_scan_finds_something(self):
        """A regex that matches nothing would make the next test vacuous."""
        self.assertTrue(repo_ids_in_source(),
                        "no model repo ids found in %s — the pattern has rotted "
                        "and the coverage test below is now asserting nothing"
                        % ", ".join(SOURCES))

    def test_every_model_a_backend_defaults_to_is_pinned(self):
        missing = {repo: where for repo, where in repo_ids_in_source().items()
                   if repo not in self.lock["models"]}
        self.assertEqual(
            missing, {},
            "these are loaded by a backend but not pinned in models.lock.json, "
            "so nothing downloads them and the feature 503s on a fresh install "
            "(exactly what happened to Qwen3-TTS CustomVoice):\n" +
            "\n".join("  %s  (%s)" % (repo, ", ".join(where))
                      for repo, where in sorted(missing.items())))

    def test_the_music_tiers_have_weights_to_load(self):
        """MUSIC_TIERS names checkpoint directories, not Hub repos.

        The high tier is a separate download; asserting it against
        services.MUSIC_TIERS rather than a copy is the point.
        """
        import services
        targets = {spec["target"] for spec in self.lock["models"].values()}
        for tier, spec in services.MUSIC_TIERS.items():
            model = spec["model"]
            covered = ("checkpoints_dir/" + model in targets
                       or "checkpoints_dir" in targets)
            self.assertTrue(covered, "music tier %r wants %s and nothing in "
                                     "models.lock.json provides it" % (tier, model))


if __name__ == "__main__":
    unittest.main()


class TestPartialRepos(unittest.TestCase):
    """One lockfile entry can point at a repo holding far more than this
    machine can run. `stable-audio-3-optimized` ships TensorRT, ONNX, TFLite
    and MLX builds — about 100 GB — against 1.8 GB for the MLX bundle an Apple
    silicon Mac uses. Without allow_patterns reaching the downloader, asking
    for sound effects fetches all of it."""

    def setUp(self):
        with open(LOCK_PATH, encoding="utf-8") as fh:
            self.lock = json.load(fh)
        with open(os.path.join(REPO, "update.sh"), encoding="utf-8") as fh:
            self.update = fh.read()

    def test_the_downloader_honours_allow_patterns(self):
        self.assertIn("allow_patterns", self.update,
                      "a lockfile entry can name allow_patterns and nothing reads it")

    def test_every_entry_that_names_them_is_a_list_of_strings(self):
        for repo, spec in self.lock["models"].items():
            pats = spec.get("allow_patterns")
            if pats is None:
                continue
            self.assertIsInstance(pats, list, repo)
            self.assertTrue(pats, "%s: an empty allow_patterns fetches nothing" % repo)
            for pat in pats:
                self.assertIsInstance(pat, str, repo)

    def test_a_partial_repo_declares_the_size_of_the_part(self):
        # size_gb drives the "about N GB" the installer prints before asking.
        # Reporting the whole repo for a partial fetch would be a wrong number
        # in the one place someone decides whether to proceed.
        for repo, spec in self.lock["models"].items():
            if spec.get("allow_patterns"):
                self.assertLess(float(spec["size_gb"]), 20,
                                "%s: size_gb looks like the whole repo" % repo)


class TestSelectingOneModel(unittest.TestCase):
    """`./anneal models <service>` was a sensible unit when a service had one
    or two models. `text` now has four, so asking for a 4.3 GB coder downloads
    22.6 GB — and the 503 a caller gets for a missing model named exactly that
    command, which is misleading rather than merely coarse.

    A selection token may now be a repo id as well as a service name.
    """

    def setUp(self):
        with open(os.path.join(REPO, "update.sh"), encoding="utf-8") as fh:
            self.update = fh.read()

    def run_list(self, selection):
        import subprocess
        done = subprocess.run(["./anneal", "models", "list", selection],
                              cwd=REPO, capture_output=True, text=True)
        return done.returncode, done.stdout + done.stderr

    def test_a_service_still_works(self):
        code, out = self.run_list("text")
        self.assertEqual(code, 0, out)
        self.assertIn("gemma", out)

    def test_a_repo_id_selects_only_that_model(self):
        repo = "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
        code, out = self.run_list(repo)
        self.assertEqual(code, 0, out)
        self.assertIn("Qwen2.5-Coder", out)
        # The point: the other text models are not in the plan.
        plan = out.split("Not fetching")[0]
        self.assertNotIn("gemma", plan)
        self.assertNotIn("gpt-oss", plan)

    def test_an_unknown_token_says_what_is_accepted(self):
        code, out = self.run_list("not-a-thing")
        self.assertNotEqual(code, 0)
        self.assertIn("not-a-thing", out)

    def test_the_error_names_both_kinds_of_token(self):
        code, out = self.run_list("not-a-thing")
        self.assertIn("service", out.lower())
        self.assertIn("model", out.lower())
