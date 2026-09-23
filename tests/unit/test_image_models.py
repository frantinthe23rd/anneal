#!/usr/bin/env python3
"""Choosing which image model draws.

One model, fixed at startup by an environment variable. So the image service
gets what music and text already have: a registry, a `model` on the request,
and a switch that costs a restart because only one heavy model fits and mflux
holds its weights for the life of the process. The shape is deliberately the
same as TEXT_MODELS — the decision is the same decision, and a second spelling
of it would be a second thing to learn.

The second entry was added expecting to lift a restriction: schnell was assumed
not to draw an unclothed figure. Measured on the same prompt, seed and size, it
does, and so does the finetune. Nothing here works around a filter, and the
registry must not say it does — what separates the two is photographic realism
against speed. Recorded because the assumption is the obvious one to make again.

Two things are specific to images and are what the tests below are mostly
about:

  the step count   schnell is distilled for four steps and Z-Image Turbo for
                   nine. This is not a preference: four steps of Z-Image is
                   mush and nine of schnell is five wasted. So the default
                   follows the model rather than being one constant, and it is
                   served so the form can follow the choice.
  the loader       these are different architectures. mflux has a class for
                   each, and picking the wrong one does not degrade — it
                   raises somewhere inside weight loading, a minute in.
"""

import json
import os
import unittest

from tests.context import REPO_ROOT

import image_server
import services
import supervisor


class TestTheRegistry(unittest.TestCase):
    def test_more_than_one_model_is_offered(self):
        self.assertGreater(len(services.IMAGE_MODELS), 1)

    def test_the_default_is_one_of_them(self):
        self.assertIn(services.DEFAULT_IMAGE_MODEL, services.IMAGE_MODELS)

    def test_every_entry_names_what_a_caller_has_to_decide_on(self):
        for name, spec in services.IMAGE_MODELS.items():
            for key in ("repo", "label", "licence", "loader", "steps"):
                self.assertTrue(spec.get(key), "%s is missing %s" % (name, key))

    def test_every_loader_is_one_the_backend_can_build(self):
        """A loader name with no branch behind it fails a minute into a cold
        load, on the machine, after the weights are already resident."""
        src = open(os.path.join(REPO_ROOT, "image_server.py"), encoding="utf-8").read()
        block = src[src.index("def build_model"):]
        block = block[:block.index("\ndef ")]
        for name, spec in services.IMAGE_MODELS.items():
            self.assertIn('"%s"' % spec["loader"], block,
                          "%s wants loader %r and build_model() has no branch "
                          "for it" % (name, spec["loader"]))

    def test_the_step_counts_differ_because_the_models_do(self):
        """If they were all the same, the per-model default would be ceremony
        and the next person would collapse it back to one constant."""
        counts = {spec["steps"] for spec in services.IMAGE_MODELS.values()}
        self.assertGreater(len(counts), 1)

    def test_every_model_is_pinned_in_the_lockfile(self):
        with open(os.path.join(REPO_ROOT, "models.lock.json")) as fh:
            lock = json.load(fh)["models"]
        for name, spec in services.IMAGE_MODELS.items():
            self.assertIn(spec["repo"], lock, name)
            self.assertRegex(lock[spec["repo"]]["revision"], r"^[0-9a-f]{40}$",
                             "%s: a tag moves, which is what the lockfile prevents" % name)

    def test_only_the_default_is_required(self):
        """The second model is 5.9 GB. Nobody should fetch it to make a cover."""
        with open(os.path.join(REPO_ROOT, "models.lock.json")) as fh:
            lock = json.load(fh)["models"]
        for name, spec in services.IMAGE_MODELS.items():
            required = lock[spec["repo"]].get("required", True)
            self.assertEqual(required, name == services.DEFAULT_IMAGE_MODEL, name)

    def test_a_model_whose_licence_is_not_the_permissive_default_says_so(self):
        """The default is Apache-2.0. A caller who picks something else is
        picking different terms, and should not find that out afterwards."""
        for name, spec in services.IMAGE_MODELS.items():
            if spec["licence"] == "Apache-2.0":
                continue
            self.assertIn("restriction", (spec.get("note") or "").lower(),
                          "%s carries %s and the note does not say what that "
                          "costs" % (name, spec["licence"]))


class TestChoosingOne(unittest.TestCase):
    def test_an_unknown_name_is_refused(self):
        self.assertIsNotNone(supervisor.image_model_problem("not-a-model"))

    def test_a_known_name_is_not_rejected_as_unknown(self):
        """Whether it is *downloaded* is a different question, and the suite
        points HF_HOME at an empty directory — so assert on the complaint, not
        on this machine's cache."""
        problem = supervisor.image_model_problem(services.DEFAULT_IMAGE_MODEL) or ""
        self.assertNotIn("unknown model", problem)

    def test_a_missing_model_names_the_command_that_fetches_it(self):
        problem = supervisor.image_model_problem(services.DEFAULT_IMAGE_MODEL) or ""
        self.assertIn("./anneal models", problem)

    def test_a_downloaded_model_has_no_problem(self):
        """The suite's HF_HOME is empty, so every model reads as missing. The
        other branch still needs covering, or the 503 is the only path ever
        exercised and 'available' could be wired to a constant."""
        from unittest import mock
        with mock.patch.object(services, "image_model_path",
                               return_value="/somewhere/on/disk"):
            for name in services.IMAGE_MODELS:
                self.assertIsNone(supervisor.image_model_problem(name), name)
            node = supervisor.capability_limits()["image"]
            for name in services.IMAGE_MODELS:
                self.assertTrue(node["models"][name]["available"], name)

    def test_the_repo_id_works_as_well_as_the_short_name(self):
        for name, spec in services.IMAGE_MODELS.items():
            self.assertEqual(supervisor.resolve_image_model(spec["repo"]), name)
            self.assertEqual(supervisor.resolve_image_model(name), name)

    def test_nothing_asked_for_means_the_default(self):
        self.assertEqual(supervisor.resolve_image_model(None), services.DEFAULT_IMAGE_MODEL)
        self.assertEqual(supervisor.resolve_image_model(""), services.DEFAULT_IMAGE_MODEL)

    def test_an_unknown_name_resolves_to_nothing_rather_than_the_default(self):
        """Silently drawing with a different model than the one asked for is
        the failure this whole feature exists to remove."""
        self.assertIsNone(supervisor.resolve_image_model("dall-e-3"))


class TestTheBackendIsToldWhichOne(unittest.TestCase):
    """The model is on the command line, not only in the environment.

    A backend can outlive the supervisor, and then the spec describes what the
    *next* start would use rather than what is answering. `ps` is the only
    source that survives, which is why the supervisor reads it back from there.
    """

    def test_the_launch_command_carries_it(self):
        cmd = services.SERVICES["image"]["cmd"]
        self.assertIn("--model", cmd)
        self.assertIn(cmd[cmd.index("--model") + 1], services.IMAGE_MODELS)

    def test_the_stop_pattern_still_matches_the_backend(self):
        """stop_pattern() cuts at the first flag. A model appended after one
        that does not start with '-' would silently widen the pkill."""
        pattern = services.stop_pattern(services.SERVICES["image"])
        self.assertTrue(pattern.endswith("image_server.py"), pattern)

    def test_what_is_loaded_is_read_from_the_process_not_the_spec(self):
        src = open(os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8").read()
        block = src[src.index("def loaded_image_model"):]
        block = block[:block.index("\ndef ")]
        self.assertIn("running_argv", block)


class TestSwitchingIsHonest(unittest.TestCase):
    def test_it_restarts_rather_than_pretending(self):
        src = open(os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8").read()
        block = src[src.index("def ensure_image_model"):]
        block = block[:block.index("\ndef ")]
        self.assertIn("stop(", block)

    def test_a_busy_model_is_refused_rather_than_killed(self):
        src = open(os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8").read()
        block = src[src.index("def ensure_image_model"):]
        block = block[:block.index("\ndef ")]
        self.assertIn("ServiceBusy", block)
        self.assertIn("has_work", block)


class TestItIsServed(unittest.TestCase):
    def setUp(self):
        self.node = supervisor.capability_limits().get("image")

    def test_the_block_exists(self):
        self.assertIsNotNone(self.node)

    def test_every_model_reports_whether_it_is_installed(self):
        for name in services.IMAGE_MODELS:
            self.assertIn("available", self.node["models"][name])

    def test_it_carries_the_label_the_licence_and_the_step_count(self):
        for name, spec in services.IMAGE_MODELS.items():
            served = self.node["models"][name]
            self.assertEqual(served["label"], spec["label"])
            self.assertEqual(served["licence"], spec["licence"])
            self.assertEqual(served["steps"], spec["steps"])

    def test_it_says_which_one_is_loaded_now(self):
        """Switching costs a cold load, so a client that cares about latency
        needs to know whether it is about to pay for one."""
        self.assertIn("loaded", self.node)

    def test_the_default_is_named(self):
        self.assertEqual(self.node["default"], services.DEFAULT_IMAGE_MODEL)


class TestTheBackendServesOneModel(unittest.TestCase):
    """image_server holds one model for the life of the process."""

    def setUp(self):
        self.was = image_server.MODEL_NAME
        self.addCleanup(image_server.select_model, self.was)

    def test_selecting_sets_the_repo_the_loader_and_the_step_default(self):
        for name, spec in services.IMAGE_MODELS.items():
            image_server.select_model(name)
            self.assertEqual(image_server.MODEL_NAME, name)
            self.assertEqual(image_server.MODEL_REPO, spec["repo"])
            self.assertEqual(image_server.MODEL_SPEC["loader"], spec["loader"])
            self.assertEqual(image_server.DEFAULT_STEPS, spec["steps"])

    def test_an_unknown_name_raises_rather_than_falling_back(self):
        with self.assertRaises(ValueError):
            image_server.select_model("not-a-model")

    def test_selecting_drops_any_model_already_loaded(self):
        """The weights belong to the model that was chosen, not to the process."""
        image_server.select_model(services.DEFAULT_IMAGE_MODEL)
        image_server._model = object()
        image_server.select_model(services.DEFAULT_IMAGE_MODEL)
        self.assertIsNone(image_server._model)

    def test_a_bare_repo_id_still_overrides_where_weights_come_from(self):
        """IMAGE_MODEL used to be the repo and nothing else; env.sh and older
        installs still set it."""
        image_server.select_model(services.DEFAULT_IMAGE_MODEL, repo="someone/a-mirror")
        self.assertEqual(image_server.MODEL_REPO, "someone/a-mirror")
        self.assertEqual(image_server.MODEL_SPEC["loader"],
                         services.IMAGE_MODELS[services.DEFAULT_IMAGE_MODEL]["loader"])

    def test_a_repo_in_the_registry_read_from_the_environment_selects_that_entry(self):
        for name, spec in services.IMAGE_MODELS.items():
            os.environ["IMAGE_MODEL"] = spec["repo"]
            self.addCleanup(os.environ.pop, "IMAGE_MODEL", None)
            self.assertEqual(image_server._model_from_env(), (name, None))
            os.environ.pop("IMAGE_MODEL")


if __name__ == "__main__":
    unittest.main()
