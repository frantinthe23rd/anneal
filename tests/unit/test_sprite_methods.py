#!/usr/bin/env python3
"""Sprite animation by directed editing rather than one lucky sheet (#37).

The sheet method works and its limit was measured: one generation containing
every pose keeps the character identical for free, and produces almost no
motion. Naming the poses moved it, at the cost of the design drifting between
frames. Both halves of that trade come from the same fact — every frame is
decided by a single sample nobody can steer.

Kontext splits it. One base sprite fixes the character; each frame is then a
separate *instruction* against that image — "same character, mid-jump" — so the
pose is a parameter rather than a hope, and identity comes from the reference
instead of from luck. Same shape as the speech answer: identity and performance
on separate knobs.

The cost is a licence. FLUX.1 Kontext [dev] is non-commercial, unlike the
Apache-2.0 schnell everything else here uses, so it is opt-in and never the
default, and it says so where someone can see it.
"""

import os
import unittest

from tests.context import REPO_ROOT  # noqa: F401

import poses as pose_edit
import supervisor


class TestTheMethodsAreDeclared(unittest.TestCase):
    def test_both_methods_exist(self):
        self.assertEqual(set(supervisor.SPRITE_METHODS), {"sheet", "edit"})

    def test_the_default_is_the_permissively_licensed_one(self):
        """schnell is Apache-2.0 and already installed. A non-commercial model
        must never become the default by accident — the same rule the video
        backend table used for exactly this reason."""
        self.assertEqual(supervisor.DEFAULT_SPRITE_METHOD, "sheet")
        self.assertIn("apache",
                      supervisor.SPRITE_METHODS["sheet"]["licence"].lower())

    def test_every_method_states_its_licence(self):
        for name, spec in supervisor.SPRITE_METHODS.items():
            self.assertTrue(spec.get("licence"), name)

    def test_no_method_carries_a_non_commercial_term(self):
        """This method was specified against FLUX.1 Kontext, which is
        non-commercial and was never wired up. It is implemented with
        FLUX.2-klein-4B, which is Apache-2.0 — so every row is now permissive
        and Anneal has no exception to explain. If a non-commercial model is
        ever added back, this fails and the licence has to be stated in the
        page and the spec deliberately rather than by accident."""
        for name, spec in supervisor.SPRITE_METHODS.items():
            self.assertNotIn("non-commercial", spec["licence"].lower(), name)


class TestValidation(unittest.TestCase):
    def bad(self, payload):
        return supervisor.sprite_limits(payload)

    def test_the_sheet_method_still_works_unchanged(self):
        self.assertIsNone(self.bad({"prompt": "a knight", "frames": 4}))

    def test_an_unknown_method_is_refused(self):
        problem = self.bad({"prompt": "a knight", "method": "wishful"})
        self.assertIsNotNone(problem)
        self.assertIn("method", problem)

    def test_edit_requires_named_poses(self):
        """Without them there is no instruction to edit towards, and the whole
        point of this method is that the pose is specified rather than hoped
        for. Falling back to 'frames' silently would give N copies."""
        problem = self.bad({"prompt": "a knight", "method": "edit"})
        self.assertIsNotNone(problem)
        self.assertIn("poses", problem)

    def test_edit_with_poses_passes(self):
        self.assertIsNone(self.bad({"prompt": "a knight", "method": "edit",
                                    "poses": ["idle", "mid-swing"]}))

    def test_the_pose_cap_still_applies(self):
        """Each pose is a full generation here rather than a share of one, so
        the cap matters more, not less."""
        self.assertIsNotNone(self.bad({
            "prompt": "a knight", "method": "edit",
            "poses": ["p"] * (supervisor.MAX_SPRITE_FRAMES + 1)}))


class TestTheEditInstruction(unittest.TestCase):
    """What actually holds the character still."""

    def test_the_pose_reaches_the_instruction(self):
        p = supervisor.pose_edit.edit_prompt("crouched to jump")
        self.assertIn("crouched to jump", p)

    def test_it_asks_for_the_character_to_be_unchanged(self):
        p = supervisor.pose_edit.edit_prompt("mid-jump").lower()
        self.assertTrue("same character" in p or "identical" in p,
                        "nothing pins the subject, which is the whole method")

    def test_it_asks_for_the_background_to_stay_plain(self):
        """The cutter finds frames by content against a flat background. An
        edit that invents scenery makes the sprite uncuttable."""
        self.assertIn("background", supervisor.pose_edit.edit_prompt("waving").lower())


class TestModelPresence(unittest.TestCase):
    """~9.6 GB, separate download. A host without it should be told which,
    not fail somewhere inside mflux."""

    def test_a_missing_model_is_reported_as_such(self):
        problem = supervisor.sprite_method_problem("edit", "/definitely/not/here")
        self.assertIsNotNone(problem)
        self.assertIn("edit", problem.lower())

    def test_the_sheet_method_needs_nothing_extra(self):
        self.assertIsNone(supervisor.sprite_method_problem("sheet", "/nope"))


if __name__ == "__main__":
    unittest.main()
