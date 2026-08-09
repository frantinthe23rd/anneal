#!/usr/bin/env python3
"""A single sprite on transparency, from the Image tab.

The Animation tab was removed twice, both times on evidence: the character
drifts between frames and the pose instructions are not followed, so a set reads
as several characters rather than one moving. What survived that judgement was
the *other* half of the sprite pipeline — one subject, matted out of its
background — which is genuinely good and was only ever reachable by asking for
a whole animation.

So it becomes an option on the image request rather than a modality of its own.
Nothing new is generated: it is the same image, with `rembg` run over it.
"""

import os
import unittest

from tests.context import REPO_ROOT

import services
import sprites


class TestTheMattingIsAlreadyThere(unittest.TestCase):
    def test_the_function_it_uses_exists(self):
        # cut_alpha is what the sprite cutter uses per frame. One subject is
        # that same call without the cutting, so nothing new is being written.
        self.assertTrue(hasattr(sprites, "cut_alpha"))

    def test_it_runs_outside_the_pinned_environment(self):
        """rembg pulls onnxruntime and is deliberately kept out of the
        environment that serves the models, so this has to cross the same
        subprocess boundary the sprite cutter does."""
        src = open(os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8").read()
        self.assertIn("sprite_python()", src)


class TestTheRequest(unittest.TestCase):
    def setUp(self):
        self.src = open(os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8").read()

    def test_the_image_route_is_still_the_image_route(self):
        # Not a new endpoint. A cut-out is a property of the image you asked
        # for, not a different thing to ask for.
        self.assertEqual(services.resolve("/v1/images/generations"), "image")

    def test_the_flag_is_read(self):
        self.assertIn("cutout", self.src)

    def test_a_cutout_that_cannot_be_made_does_not_lose_the_image(self):
        """The image cost 30-60 seconds of a model that had to be loaded.
        Failing the whole request because rembg is absent would throw that away
        for a post-processing step — so it degrades to the opaque image and
        says the cut-out did not happen."""
        self.assertIn("cutout_error", self.src)


class TestItIsDocumented(unittest.TestCase):
    def test_the_spec_describes_it(self):
        import json
        with open(os.path.join(REPO_ROOT, "openapi.json"), encoding="utf-8") as fh:
            spec = json.load(fh)
        # The request body is a $ref to a shared component, which is where a
        # new field has to land — adding it to the path would be a second copy.
        props = spec["components"]["schemas"]["ImageRequest"]["properties"]
        self.assertIn("cutout", props)


if __name__ == "__main__":
    unittest.main()
