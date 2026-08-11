#!/usr/bin/env python3
"""Choosing which text model answers (#55).

Tool calling, streaming and a 131k context already work — measured against the
running gateway — so the API can drive a coding agent today. What it could not
do was choose: one model, fixed at startup by an environment variable, serving
two jobs that want different things. A coder-specialised model suits agent work;
a small one is faster for lyric planning, where quality matters less than the
wait.

Switching costs a restart and a cold load, exactly as switching music tier does,
because text is heavy and only one heavy model fits. That is surfaced rather
than hidden — the same decision ensure_music_tier() already made.
"""

import os
import unittest

from tests.context import REPO_ROOT

import services
import supervisor


class TestTheRegistry(unittest.TestCase):
    def test_more_than_one_model_is_offered(self):
        self.assertGreater(len(services.TEXT_MODELS), 1)

    def test_the_default_is_one_of_them(self):
        self.assertIn(services.DEFAULT_TEXT_MODEL, services.TEXT_MODELS)

    def test_every_entry_names_a_repo_a_label_and_a_licence(self):
        for name, spec in services.TEXT_MODELS.items():
            for key in ("repo", "label", "licence"):
                self.assertTrue(spec.get(key), "%s is missing %s" % (name, key))

    def test_every_model_is_pinned_in_the_lockfile(self):
        import json
        with open(os.path.join(REPO_ROOT, "models.lock.json")) as fh:
            lock = json.load(fh)["models"]
        for name, spec in services.TEXT_MODELS.items():
            self.assertIn(spec["repo"], lock, name)
            self.assertRegex(lock[spec["repo"]]["revision"], r"^[0-9a-f]{40}$",
                             "%s: a tag moves, which is what the lockfile prevents" % name)

    def test_only_the_default_is_required(self):
        """Three text models is 11 GB. Nobody should have to fetch a coder to
        write lyrics."""
        import json
        with open(os.path.join(REPO_ROOT, "models.lock.json")) as fh:
            lock = json.load(fh)["models"]
        for name, spec in services.TEXT_MODELS.items():
            required = lock[spec["repo"]].get("required", True)
            self.assertEqual(required, name == services.DEFAULT_TEXT_MODEL, name)


class TestChoosingOne(unittest.TestCase):
    def test_an_unknown_name_is_refused(self):
        self.assertIsNotNone(supervisor.text_model_problem("not-a-model"))

    def test_a_known_name_is_not_rejected_as_unknown(self):
        """Whether it is *downloaded* is a different question, and the suite
        redirects HF_HOME to an empty directory — so assert on the complaint,
        not on this machine's cache."""
        problem = supervisor.text_model_problem(services.DEFAULT_TEXT_MODEL) or ""
        self.assertNotIn("unknown model", problem)

    def test_the_repo_id_works_as_well_as_the_short_name(self):
        """A client written against the OpenAI shape sends whatever `model` it
        was told; both spellings should mean the same thing."""
        repo = services.TEXT_MODELS[services.DEFAULT_TEXT_MODEL]["repo"]
        self.assertEqual(supervisor.resolve_text_model(repo), services.DEFAULT_TEXT_MODEL)
        self.assertEqual(supervisor.resolve_text_model(services.DEFAULT_TEXT_MODEL),
                         services.DEFAULT_TEXT_MODEL)

    def test_nothing_asked_for_means_the_default(self):
        self.assertEqual(supervisor.resolve_text_model(None), services.DEFAULT_TEXT_MODEL)
        self.assertEqual(supervisor.resolve_text_model(""), services.DEFAULT_TEXT_MODEL)

    def test_an_unknown_name_resolves_to_nothing_rather_than_the_default(self):
        """Silently answering with a different model than the one asked for is
        the failure this whole feature exists to remove."""
        self.assertIsNone(supervisor.resolve_text_model("gpt-4"))


class TestItIsServed(unittest.TestCase):
    def setUp(self):
        self.node = supervisor.capability_limits().get("text")

    def test_the_block_exists(self):
        self.assertIsNotNone(self.node)

    def test_every_model_reports_whether_it_is_installed(self):
        for name in services.TEXT_MODELS:
            self.assertIn("available", self.node["models"][name])

    def test_it_carries_the_label_and_the_licence(self):
        for name, spec in services.TEXT_MODELS.items():
            served = self.node["models"][name]
            self.assertEqual(served["label"], spec["label"])
            self.assertEqual(served["licence"], spec["licence"])

    def test_it_says_which_one_is_loaded_now(self):
        """Switching costs a cold load, so a client that cares about latency
        needs to know whether it is about to pay for one."""
        self.assertIn("loaded", self.node)

    def test_the_default_is_named(self):
        self.assertEqual(self.node["default"], services.DEFAULT_TEXT_MODEL)


class TestSwitchingIsHonest(unittest.TestCase):
    def test_it_restarts_rather_than_pretending(self):
        src = open(os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8").read()
        block = src[src.index("def ensure_text_model"):]
        block = block[:block.index("\ndef ")]
        self.assertIn("stop(", block)

    def test_a_busy_model_is_refused_rather_than_killed(self):
        src = open(os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8").read()
        block = src[src.index("def ensure_text_model"):]
        block = block[:block.index("\ndef ")]
        self.assertIn("ServiceBusy", block)
        self.assertIn("has_work", block)


if __name__ == "__main__":
    unittest.main()
