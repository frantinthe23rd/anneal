"""The numbers a client would otherwise hardcode, served instead.

Written before the endpoint, per the convention in CLAUDE.md. The point is not
that /health grows a field — it is that every number in this file has exactly
one home, and that a page repeating one of them can be caught mechanically.

The failures this exists to prevent have all happened: openapi.json said the
high tier ran 32 steps while services.py said 50; the UI Guide states cold-start
timings and idle timeouts in prose that nothing checks; and INTEGRATION.md,
README.md and two UI pages each carry their own copy of the tier table.
"""
import json
import os
import re
import unittest

from tests.context import REPO_ROOT

import builder
import services
import supervisor


class TestServedLimits(unittest.TestCase):
    """/health carries the constraints a caller has to respect."""

    def setUp(self):
        self.limits = supervisor.capability_limits()

    def test_press_bounds_come_from_builder(self):
        self.assertEqual(self.limits["press"]["max_tracks"], builder.MAX_TRACKS)
        self.assertEqual(self.limits["press"]["min_track_seconds"], builder.MIN_TRACK_SECONDS)
        self.assertEqual(self.limits["press"]["max_track_seconds"], builder.MAX_TRACK_SECONDS)

    def test_music_tiers_carry_their_step_counts(self):
        for name, tier in self.limits["music"]["tiers"].items():
            self.assertEqual(tier["steps"], services.MUSIC_TIERS[name]["steps"], name)

    def test_cold_start_estimates_exist_for_every_service(self):
        """The Guide quotes these. If they are served, it can stop guessing."""
        for name in services.SERVICES:
            self.assertIn(name, self.limits["cold_start_seconds"])
            self.assertGreater(self.limits["cold_start_seconds"][name], 0)

    def test_idle_timeouts_are_not_restated_here(self):
        """They are already per-service in /health; a second copy is the bug."""
        self.assertNotIn("idle_timeout_seconds", self.limits)
        self.assertNotIn("idle", json.dumps(self.limits))


class TestTheUiFetchesRatherThanRepeats(unittest.TestCase):
    """The half that stops this decaying back.

    An earlier version of this test hunted for bare numbers in the doc pages.
    It found the real duplications and also flagged "8 steps" in the img2img
    note, which is the image step default — a different 8. A number on its own
    does not carry enough meaning to police.

    So the rule is positive instead: facts the API serves are marked in the
    markup with `data-cap="path.through.limits"`, filled at runtime, and must
    contain no digits of their own. That is checkable without guessing, and it
    fails the moment someone types the value back in.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "ui.html"), encoding="utf-8") as fh:
            cls.src = fh.read()
        cls.slots = re.findall(r'data-cap="([^"]+)"[^>]*>([^<]*)<', cls.src)

    def resolve(self, path):
        node = supervisor.capability_limits()
        for part in path.split("."):
            self.assertIn(part, node, "data-cap=%r: no %r in the served limits" % (path, part))
            node = node[part]
        return node

    def test_the_pages_use_at_least_the_facts_that_drifted(self):
        """Tier steps and the press bounds are the ones that actually diverged."""
        paths = {p for p, _ in self.slots}
        for required in ("music.tiers.draft.steps", "music.tiers.high.steps",
                         "press.max_tracks", "press.max_track_seconds"):
            self.assertIn(required, paths,
                          "the value that drifted should be fetched, not written")

    def test_every_slot_names_something_the_api_actually_serves(self):
        for path, _ in self.slots:
            self.resolve(path)

    def test_no_slot_carries_its_own_copy_of_the_value(self):
        """Placeholder text is fine; a digit means someone wrote the answer in."""
        for path, text in self.slots:
            self.assertFalse(re.search(r"\d", text),
                             "data-cap=%r contains %r — it is filled at runtime, "
                             "so a number here is a second copy" % (path, text.strip()))


if __name__ == "__main__":
    unittest.main()


class TestSpriteMethodsAreServed(unittest.TestCase):
    """The Animation form has to offer the methods this host actually has, and
    say which are unavailable and why. A list written into ui.html would be the
    fifth copied list in this repo — and this one carries a licence, which is
    the worst kind of thing to let drift.
    """

    def setUp(self):
        self.node = supervisor.capability_limits().get("sprites")

    def test_it_is_reported_at_all(self):
        self.assertIsNotNone(self.node, "the page cannot build the form without it")

    def test_every_method_the_server_knows_is_offered(self):
        self.assertEqual(set(self.node["methods"]), set(supervisor.SPRITE_METHODS),
                         "derived from SPRITE_METHODS, never a copy of it")

    def test_each_one_carries_its_label_and_licence(self):
        for name, spec in supervisor.SPRITE_METHODS.items():
            served = self.node["methods"][name]
            self.assertEqual(served["label"], spec["label"])
            self.assertEqual(served["licence"], spec["licence"])

    def test_it_says_which_are_usable_here(self):
        # Kontext needs weights that may not be downloaded, and 'sheet' needs an
        # interpreter with rembg. A form that offers a method this host cannot
        # run produces a 501 or a 503 the user could have been spared.
        for name in supervisor.SPRITE_METHODS:
            self.assertIn("available", self.node["methods"][name])
            self.assertIsInstance(self.node["methods"][name]["available"], bool)

    def test_the_default_is_one_of_them(self):
        self.assertIn(self.node["default_method"], self.node["methods"])

    def test_the_licence_is_not_written_down_anywhere_else(self):
        # The non-commercial term on Kontext is the one claim here with legal
        # weight. If ui.html states it too, the two can disagree.
        ui = open(os.path.join(REPO_ROOT, "ui.html"), encoding="utf-8").read()
        self.assertNotIn("Non-Commercial License", ui)
