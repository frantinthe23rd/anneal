"""POST /v1/sprites — a brief becomes a cut, matted sprite sheet (#37).

Written before the endpoint, per CLAUDE.md.

The shape follows what the experiments established rather than what seemed
obvious at the start:

  * The sheet is **one** image generation, because separate generations of "the
    same character" produce different characters — measured twice.
  * Frames are found by content, because the model spaces poses irregularly and
    at different sizes.
  * Matting is a segmentation model, because colour distance loses a pale
    sprite on a pale background.

That last one puts rembg in the loop, and rembg is not in the pinned virtualenv
that serves the models. The cut therefore runs as a subprocess against a
configurable interpreter, and the tests pin that arrangement so nobody
accidentally imports it into the gateway.
"""
import json
import os
import unittest

from tests.context import REPO_ROOT  # noqa: F401

import supervisor


class TestSheetPrompt(unittest.TestCase):
    """One generation has to be asked for as one picture."""

    def test_the_frame_count_reaches_the_prompt(self):
        p = supervisor.sheet_prompt("a knight in blue armour", frames=6, style="pixel art")
        self.assertIn("6", p)

    def test_the_character_and_style_are_both_present(self):
        p = supervisor.sheet_prompt("a knight in blue armour", frames=4, style="flat vector")
        self.assertIn("knight in blue armour", p)
        self.assertIn("flat vector", p)

    def test_it_asks_for_one_character_across_the_frames(self):
        """The whole reason for generating a sheet rather than N sprites."""
        p = supervisor.sheet_prompt("a slime", frames=4, style="pixel art").lower()
        self.assertTrue("identical" in p or "same character" in p,
                        "nothing tells the model to keep the character")

    def test_it_asks_for_a_plain_background(self):
        p = supervisor.sheet_prompt("a slime", frames=4, style="pixel art").lower()
        self.assertIn("background", p)


class TestNamedPoses(unittest.TestCase):
    """Identity comes free; motion does not.

    The first live run asked for four frames of a slime "with only the pose
    changing" and got five near-identical standing slimes. The character was
    perfect and the animation was nothing. Naming the poses is the lever, so it
    has to reach the prompt and it has to set the frame count — asking for four
    frames while naming six poses is a contradiction the model resolves by
    dropping one at random.
    """

    def test_named_poses_reach_the_prompt(self):
        p = supervisor.sheet_prompt("a slime", frames=3, style="pixel art",
                                    poses=["standing still", "mid-jump", "landing"])
        for pose in ("standing still", "mid-jump", "landing"):
            self.assertIn(pose, p)

    def test_the_default_asks_for_visibly_different_poses(self):
        p = supervisor.sheet_prompt("a slime", frames=4, style="pixel art").lower()
        self.assertIn("different pose", p,
                      "'only the pose changing' measured as no pose change at all")

    def test_poses_must_be_a_list_of_strings(self):
        self.assertIsNotNone(supervisor.sprite_limits({"prompt": "x", "poses": "walking"}))
        self.assertIsNotNone(supervisor.sprite_limits({"prompt": "x", "poses": [1, 2]}))
        self.assertIsNone(supervisor.sprite_limits({"prompt": "x", "poses": ["a", "b"]}))

    def test_too_many_poses_are_refused(self):
        self.assertIsNotNone(supervisor.sprite_limits(
            {"prompt": "x", "poses": ["p"] * (supervisor.MAX_SPRITE_FRAMES + 1)}))


class TestValidation(unittest.TestCase):
    def test_a_prompt_is_required(self):
        self.assertIsNotNone(supervisor.sprite_limits({"frames": 4}))

    def test_frames_are_bounded(self):
        """A sheet of thirty poses is a picture of nothing at this resolution."""
        self.assertIsNone(supervisor.sprite_limits({"prompt": "x", "frames": 4}))
        self.assertIsNotNone(supervisor.sprite_limits({"prompt": "x", "frames": 0}))
        self.assertIsNotNone(supervisor.sprite_limits({"prompt": "x", "frames": 99}))

    def test_a_sane_request_passes(self):
        self.assertIsNone(supervisor.sprite_limits({"prompt": "a knight"}))


class TestTheCutRunsOutOfProcess(unittest.TestCase):
    """rembg pulls onnxruntime, and the environment that serves the models is
    version-pinned. Importing it into the gateway would couple the two."""

    def test_the_gateway_does_not_import_rembg(self):
        with open(os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("import rembg", src)
        self.assertNotIn("from rembg", src)

    def test_the_interpreter_is_configurable(self):
        """It differs per machine and must not be a hardcoded path."""
        self.assertTrue(callable(supervisor.sprite_python))
        self.assertIn("ANNEAL_SPRITE_PYTHON", open(
            os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8").read())


class TestOutputsKnowsAboutSprites(unittest.TestCase):
    def test_sprites_is_a_kind(self):
        import outputs
        self.assertIn("sprites", outputs.KINDS)


if __name__ == "__main__":
    unittest.main()
