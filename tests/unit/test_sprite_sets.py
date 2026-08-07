#!/usr/bin/env python3
"""A sprite set is one asset, not a scattering of PNGs (#19).

Cutting a sheet produces a directory of frames. The library listed each frame as
its own row, so a four-pose walk cycle arrived as four unrelated entries sorted
between other people's album art — and the one thing that makes a frame sequence
legible, seeing it move, was not available anywhere.

So the set is the unit. A directory carrying `set.json` is one library item,
represented by an animated GIF built from its own frames, with the frames still
on disk underneath because those are what a game actually loads.
"""

import json
import os
import shutil
import tempfile
import unittest

from tests.context import REPO_ROOT  # noqa: F401

import outputs

try:
    from PIL import Image
    import sprites
    HAVE_PIL = True
except ImportError:                                   # pragma: no cover
    HAVE_PIL = False


class SetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._root = outputs.root
        outputs.root = lambda: os.path.join(self.tmp, "outputs")
        self.addCleanup(setattr, outputs, "root", self._root)

    def make_set(self, name="a-slime", frames=3, gif=True):
        d = os.path.join(outputs.kind_dir("sprites"), name)
        os.makedirs(d, exist_ok=True)
        for i in range(frames):
            with open(os.path.join(d, "frame-%02d.png" % i), "wb") as fh:
                fh.write(b"png" * 10)
        if gif:
            with open(os.path.join(d, "preview.gif"), "wb") as fh:
                fh.write(b"gif" * 10)
        outputs.write_set(d, {"prompt": "a slime", "frames": frames,
                              "preview": os.path.join(d, "preview.gif")})
        return d


class TestTheSetIsOneItem(SetCase):
    def test_a_set_appears_once_not_once_per_frame(self):
        self.make_set(frames=4)
        items = outputs.listing(kind="sprites")["items"]
        self.assertEqual(len(items), 1, [i["name"] for i in items])

    def test_the_item_points_at_a_real_file(self):
        """The bug this replaces: the directory was listed, and its
        /v1/outputs/file URL 404d because a directory is not a file."""
        self.make_set()
        item = outputs.listing(kind="sprites")["items"][0]
        self.assertTrue(os.path.isfile(item["path"]), item["path"])
        self.assertTrue(item["path"].endswith(".gif"))

    def test_the_item_says_how_many_frames_it_has(self):
        self.make_set(frames=5)
        item = outputs.listing(kind="sprites")["items"][0]
        self.assertEqual(item["meta"].get("frames"), 5)

    def test_two_sets_are_two_items(self):
        self.make_set("slime"); self.make_set("robot")
        self.assertEqual(len(outputs.listing(kind="sprites")["items"]), 2)

    def test_a_loose_file_beside_a_set_is_still_listed(self):
        """Sets are a sprite thing. Nothing else should start disappearing."""
        self.make_set()
        with open(os.path.join(outputs.kind_dir("sprites"), "loose.png"), "wb") as fh:
            fh.write(b"x")
        names = [i["name"] for i in outputs.listing(kind="sprites")["items"]]
        self.assertIn("loose.png", names)

    def test_a_set_without_a_preview_falls_back_rather_than_vanishing(self):
        """A cut that produced frames but no GIF must still be findable —
        losing the work silently is worse than showing it without a preview."""
        d = os.path.join(outputs.kind_dir("sprites"), "no-gif")
        os.makedirs(d)
        with open(os.path.join(d, "frame-00.png"), "wb") as fh:
            fh.write(b"png")
        outputs.write_set(d, {"prompt": "x", "frames": 1})
        items = outputs.listing(kind="sprites")["items"]
        self.assertEqual(len(items), 1)
        self.assertTrue(os.path.isfile(items[0]["path"]))

    def test_other_kinds_are_untouched(self):
        os.makedirs(outputs.kind_dir("images"), exist_ok=True)
        with open(os.path.join(outputs.kind_dir("images"), "a.png"), "wb") as fh:
            fh.write(b"x")
        self.assertEqual(len(outputs.listing(kind="images")["items"]), 1)


class TestUsageCountsTheWholeSet(SetCase):
    def test_the_frames_still_count_towards_disk(self):
        """Collapsing the *listing* must not make the storage figure lie — that
        is the one thing about retention here that is automated."""
        self.make_set(frames=4)
        outputs._USAGE_CACHE.update({"at": 0.0, "value": None})
        usage = outputs.usage(now=1.0)
        self.assertEqual(usage["by_kind"]["sprites"]["files"], 5)   # 4 frames + gif


@unittest.skipUnless(HAVE_PIL, "needs Pillow — run under gen-venv or tools-venv")
class TestTheGif(unittest.TestCase):
    def frames(self, n=3, size=(24, 24)):
        out = tempfile.mkdtemp()
        paths = []
        for i in range(n):
            img = Image.new("RGBA", size, (0, 0, 0, 0))
            for x in range(4 + i, 12 + i):
                for y in range(4, 12):
                    img.putpixel((x, y), (20, 160, 60, 255))
            p = os.path.join(out, "f%02d.png" % i)
            img.save(p); paths.append(p)
        return paths

    def test_it_writes_an_animated_gif_with_every_frame(self):
        paths = self.frames(4)
        gif = os.path.join(tempfile.mkdtemp(), "preview.gif")
        sprites.animate(paths, gif, fps=8)
        self.assertTrue(os.path.isfile(gif))
        with Image.open(gif) as im:
            self.assertEqual(getattr(im, "n_frames", 1), 4)

    def test_frames_of_different_sizes_are_padded_not_squashed(self):
        """Cut frames are never the same size — the model spaces poses
        irregularly. Resizing each to the canvas would make the character
        pulse; padding keeps it still."""
        out = tempfile.mkdtemp()
        a = Image.new("RGBA", (20, 40), (0, 0, 0, 0)); a.putpixel((5, 5), (255, 0, 0, 255))
        b = Image.new("RGBA", (30, 20), (0, 0, 0, 0)); b.putpixel((5, 5), (255, 0, 0, 255))
        pa, pb = os.path.join(out, "a.png"), os.path.join(out, "b.png")
        a.save(pa); b.save(pb)
        gif = os.path.join(out, "p.gif")
        sprites.animate([pa, pb], gif, fps=6)
        with Image.open(gif) as im:
            self.assertEqual(im.size, (30, 40))       # the bounding box of both

    def test_transparency_survives(self):
        paths = self.frames(2)
        gif = os.path.join(tempfile.mkdtemp(), "p.gif")
        sprites.animate(paths, gif, fps=6)
        with Image.open(gif) as im:
            self.assertIn("transparency", im.info)

    def test_no_frames_writes_nothing_rather_than_an_empty_file(self):
        gif = os.path.join(tempfile.mkdtemp(), "p.gif")
        self.assertIsNone(sprites.animate([], gif, fps=6))
        self.assertFalse(os.path.exists(gif))


if __name__ == "__main__":
    unittest.main()
