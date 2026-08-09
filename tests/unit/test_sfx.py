#!/usr/bin/env python3
"""Sound effects: a one-shot from a description (#44).

Written before the handler, the way every endpoint here is meant to be — three
have shipped documented nowhere, and each time the thing that would have caught
it was naming the path, the payload and the response first.

The model is `sa3-sm-sfx`, run through Stability's pure-MLX runner. Two
properties shape the design and are asserted here rather than assumed:

**It is not a service.** Peak footprint measured at 1.49 GB for a 5s clip —
against 7 GB for music and 9 for image — and the runner loads and exits in one
pass. So there is no backend to keep warm, no port, no idle timeout and no
eviction: asking for a door slam must never cost a three-minute music reload.
It shells out, exactly as the sprite cutter does, and for the same reason: the
dependency is deliberately outside the pinned environment that serves models.

**It is licensed, and the licence has a condition.** The weights are Stability
AI Community — free for research, non-commercial, and commercial use below
US $1M annual revenue. That is recorded in one place and served from there.
"""

import json
import os
import unittest

from tests.context import REPO_ROOT

import outputs
import services
import sfx


class TestItIsNotAService(unittest.TestCase):
    def test_the_gateway_owns_the_route(self):
        self.assertIn("/v1/sfx", services.GATEWAY_ROUTES)
        self.assertIsNone(services.resolve("/v1/sfx"),
                          "resolving to a backend would mean starting one")

    def test_it_is_not_in_the_service_registry(self):
        # A registry entry would give it a port, an idle timer and — being
        # heavy or not — a place in the eviction logic. It has none of those.
        self.assertNotIn("sfx", services.SERVICES)

    def test_it_never_stops_another_model(self):
        src = open(os.path.join(REPO_ROOT, "sfx.py"), encoding="utf-8").read()
        for forbidden in ("stop_service", "start_service", "ensure_running"):
            self.assertNotIn(forbidden, src,
                             "a one-shot must not evict the music model")


class TestTheRequest(unittest.TestCase):
    def test_a_prompt_is_required(self):
        self.assertIsNotNone(sfx.problem({}))
        self.assertIsNotNone(sfx.problem({"prompt": "   "}))

    def test_a_plain_prompt_is_accepted(self):
        self.assertIsNone(sfx.problem({"prompt": "a door slamming"}))

    def test_the_duration_is_bounded(self):
        self.assertIsNone(sfx.problem({"prompt": "x", "seconds": sfx.MAX_SECONDS}))
        self.assertIsNotNone(sfx.problem({"prompt": "x", "seconds": sfx.MAX_SECONDS + 1}))
        self.assertIsNotNone(sfx.problem({"prompt": "x", "seconds": 0}))

    def test_a_duration_that_is_not_a_number_is_refused_rather_than_coerced(self):
        self.assertIsNotNone(sfx.problem({"prompt": "x", "seconds": "five"}))

    def test_the_bound_is_a_real_limit_not_decoration(self):
        # Generation is roughly realtime, so the cap is a time budget as much
        # as a length one. It has to be small enough that a request cannot sit
        # on the machine for minutes.
        self.assertLessEqual(sfx.MAX_SECONDS, 60)


class TestTheOutput(unittest.TestCase):
    def test_sfx_is_a_kind_the_library_knows(self):
        self.assertIn("sfx", outputs.KINDS)

    def test_the_kind_has_somewhere_to_live(self):
        self.assertTrue(outputs.root())


class TestTheLicenceIsRecordedOnce(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO_ROOT, "models.lock.json"), encoding="utf-8") as fh:
            self.lock = json.load(fh)

    def entry(self):
        return next((v for k, v in self.lock["models"].items() if "stable-audio" in k), None)

    def test_the_model_is_pinned(self):
        e = self.entry()
        self.assertIsNotNone(e, "the weights must be pinned like every other model")
        self.assertTrue(e.get("revision"), "an unpinned model is a different model later")

    def test_it_is_optional(self):
        # 1.8 GB for a modality not everyone wants. Nobody should have to fetch
        # it to try music.
        self.assertFalse(self.entry().get("required", False))

    def test_the_licence_names_its_condition(self):
        licence = "%s %s" % (self.entry().get("licence", ""), self.entry().get("note", ""))
        self.assertIn("Stability AI Community", licence)
        self.assertIn("1M", licence.replace("$", "").replace(" ", " "),
                      "the revenue condition is the part someone has to act on")


if __name__ == "__main__":
    unittest.main()
