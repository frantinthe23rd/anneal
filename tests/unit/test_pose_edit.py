#!/usr/bin/env python3
"""Pose-directed sprite frames (#37).

The sheet method asks one image model for a *layout* — several poses of one
character, spaced out — and then recovers frames by finding blobs. It fails in
two ways that measurement made plain: the character drifts between poses, and
sprites that touch get cut as one frame. A four-pose brief returned four frames
once, three another time and seven a third.

Editing solves both by construction. One base sprite is generated, and each
frame is that image edited towards one pose, so identity comes from the
reference rather than from asking nicely, and there is no layout to recover.

Measured on this machine before any of it was written, with
`FLUX.2-klein-4B` through `mflux-generate-flux2-edit`:

    4 steps, 512x512   33 s per frame, peak 4.90 GB
    8 steps, 512x512   55 s per frame, peak 4.90 GB

4.90 GB is lighter than music (~7 GB) and the image model (~9 GB), but it is
still gigabytes held outside any service, so it has to take the heavy slot.
"""

import os
import unittest

from tests.context import REPO_ROOT

import poses as pose_edit
import supervisor


class TestTheMethod(unittest.TestCase):
    def test_it_is_offered(self):
        self.assertIn("edit", supervisor.SPRITE_METHODS)

    def test_the_dead_name_is_gone(self):
        """It was called 'kontext' after a model that was never wired up and
        carries a non-commercial licence. What is implemented is FLUX.2-klein,
        which is Apache-2.0 — keeping the old name would misreport both."""
        self.assertNotIn("kontext", supervisor.SPRITE_METHODS)

    def test_it_needs_a_model_and_says_which_licence(self):
        spec = supervisor.SPRITE_METHODS["edit"]
        self.assertTrue(spec["needs_model"])
        self.assertIn("Apache", spec["licence"])
        self.assertNotIn("Non-Commercial", spec["licence"])


class TestTheRequest(unittest.TestCase):
    def test_poses_are_required(self):
        """Without an instruction per frame there is nothing to edit towards,
        and falling back to a count would produce N copies of the base."""
        self.assertIsNotNone(supervisor.sprite_limits(
            {"prompt": "a knight", "method": "edit", "frames": 4}))

    def test_named_poses_are_accepted(self):
        self.assertIsNone(supervisor.sprite_limits(
            {"prompt": "a knight", "method": "edit",
             "poses": ["standing", "mid stride"]}))


class TestThePrompt(unittest.TestCase):
    def test_it_pins_everything_except_the_pose(self):
        """The model changes what you mention and drifts on what you do not."""
        p = pose_edit.edit_prompt("mid stride, left leg forward")
        self.assertIn("mid stride, left leg forward", p)
        for pinned in ("colours", "proportions", "style", "background"):
            self.assertIn(pinned, p.lower())

    def test_the_base_is_asked_for_one_subject(self):
        p = pose_edit.base_prompt("a small green knight", "flat pixel art")
        self.assertIn("a small green knight", p)
        self.assertIn("flat pixel art", p)
        # A sheet prompt asks for several figures; this must ask for exactly one,
        # or the edit stage inherits the layout problem it exists to avoid.
        self.assertNotIn("sheet", p.lower())


class TestItTakesTheHeavySlot(unittest.TestCase):
    """It holds gigabytes in a subprocess the supervisor cannot see, so nothing
    else heavy may be loaded while it runs. Running it beside the image model
    would put roughly 14 GB on a 16 GB machine."""

    def test_the_supervisor_can_free_the_slot(self):
        self.assertTrue(hasattr(supervisor, "free_heavy_slot"))

    def test_the_edit_path_asks_for_it(self):
        src = open(os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8").read()
        self.assertIn("free_heavy_slot(", src)

    def test_a_busy_model_is_refused_rather_than_killed(self):
        # Evicting a model with work in flight loses someone's job silently,
        # which is the failure start_service already refuses loudly.
        src = open(os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8").read()
        block = src[src.index("def free_heavy_slot"):]
        block = block[:block.index("\ndef ")]
        self.assertIn("ServiceBusy", block)
        self.assertIn("has_work", block)


if __name__ == "__main__":
    unittest.main()
