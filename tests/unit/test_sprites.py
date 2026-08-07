"""Cutting a generated sheet into frames (#19).

The sheet is one generation, which is what keeps every frame the same character.
What it is not is a grid: the model places poses at different sizes and
irregular spacing, so anything assuming equal cells slices characters in half.
These are the cases that produces.

Pillow lives in gen-venv rather than the system interpreter, so this skips
rather than fails where it is unavailable — the logic under test is pure and the
skip is loud.
"""
import os
import sys
import tempfile
import unittest

from tests.context import REPO_ROOT  # noqa: F401

try:
    from PIL import Image
    import sprites
    HAVE_PIL = True
except ImportError:  # pragma: no cover - depends on the interpreter in use
    HAVE_PIL = False


@unittest.skipUnless(HAVE_PIL, "needs Pillow — run under gen-venv's interpreter")
class SpriteCase(unittest.TestCase):
    def sheet(self, boxes, size=(400, 200), bg=(255, 255, 255)):
        """A white sheet with solid rectangles at known positions."""
        img = Image.new("RGB", size, bg)
        for (x, y, w, h) in boxes:
            for px in range(x, x + w):
                for py in range(y, y + h):
                    img.putpixel((px, py), (20, 160, 60))
        path = os.path.join(tempfile.mkdtemp(), "sheet.png")
        img.save(path)
        return path


class TestFinding(SpriteCase):
    def test_three_shapes_in_a_row_become_three_frames(self):
        path = self.sheet([(10, 40, 50, 80), (120, 40, 50, 80), (230, 40, 50, 80)])
        self.assertEqual(len(sprites.find_frames(path)), 3)

    def test_boxes_are_tight_around_the_content(self):
        path = self.sheet([(10, 40, 50, 80)])
        box = sprites.find_frames(path)[0]
        self.assertEqual((box["x"], box["y"], box["width"], box["height"]), (10, 40, 50, 80))

    def test_frames_of_different_sizes_are_all_found(self):
        """The robot sheet had one pose noticeably smaller than the others."""
        path = self.sheet([(10, 20, 30, 30), (100, 20, 90, 140)])
        frames = sprites.find_frames(path)
        self.assertEqual(len(frames), 2)
        self.assertNotEqual(frames[0]["width"], frames[1]["width"])

    def test_reading_order_is_left_to_right_then_top_to_bottom(self):
        path = self.sheet([(200, 10, 40, 40), (20, 10, 40, 40),
                           (200, 120, 40, 40), (20, 120, 40, 40)])
        xs = [(f["x"], f["y"]) for f in sprites.find_frames(path)]
        self.assertEqual(xs, [(20, 10), (200, 10), (20, 120), (200, 120)])

    def test_specks_are_ignored(self):
        """Stray pixels are compression noise, not a frame."""
        path = self.sheet([(10, 40, 50, 80), (300, 100, 2, 2)])
        self.assertEqual(len(sprites.find_frames(path)), 1)

    def test_a_blank_sheet_yields_nothing_rather_than_one_huge_frame(self):
        path = self.sheet([])
        self.assertEqual(sprites.find_frames(path), [])


class TestBackgroundDetection(SpriteCase):
    def test_an_off_white_background_still_reads_as_background(self):
        """Generated sheets are never pure #ffffff."""
        path = self.sheet([(10, 40, 50, 80)], bg=(250, 249, 247))
        self.assertEqual(len(sprites.find_frames(path)), 1)

    def test_a_light_grey_shadow_does_not_become_its_own_frame(self):
        """Every generated sprite sat on a soft drop shadow. Left alone, each
        shadow is a large light-grey blob that would be cut as a frame."""
        img = Image.new("RGB", (300, 200), (255, 255, 255))
        for px in range(40, 140):          # the character
            for py in range(20, 120):
                img.putpixel((px, py), (20, 160, 60))
        for px in range(30, 150):          # its shadow, only just off-white
            for py in range(125, 140):
                img.putpixel((px, py), (238, 238, 238))
        path = os.path.join(tempfile.mkdtemp(), "shadow.png")
        img.save(path)
        frames = sprites.find_frames(path)
        self.assertEqual(len(frames), 1, "the shadow was cut as a second frame")
        self.assertLess(frames[0]["height"], 120, "the shadow was included in the box")


class TestMatting(SpriteCase):
    """Colour-distance matting is the fallback, and its limit is the reason.

    It was the default until a white robot on a white background came out
    see-through — the backdrop readable through its head. That is not an edge
    case: pale characters are ordinary. These pin the behaviour so nobody
    promotes it back without knowing.
    """

    def test_a_contrasting_sprite_mattes_cleanly(self):
        from PIL import Image
        img = Image.new("RGB", (60, 60), (255, 255, 255))
        for x in range(20, 40):
            for y in range(20, 40):
                img.putpixel((x, y), (20, 60, 200))       # strongly different
        out = sprites.matte(img, (255, 255, 255))
        self.assertEqual(out.getpixel((30, 30))[3], 255, "the subject should be solid")
        self.assertEqual(out.getpixel((2, 2))[3], 0, "the background should be gone")

    def test_a_pale_sprite_on_a_pale_background_is_lost(self):
        """The measured failure, asserted rather than described.

        If this ever starts passing as 'solid', colour distance has been made
        smarter and can be reconsidered as the default.
        """
        from PIL import Image
        img = Image.new("RGB", (60, 60), (255, 255, 255))
        for x in range(20, 40):
            for y in range(20, 40):
                img.putpixel((x, y), (247, 247, 247))     # a white robot's body
        out = sprites.matte(img, (255, 255, 255))
        self.assertLess(out.getpixel((30, 30))[3], 255,
                        "this is the hole the segmentation model exists to avoid")

    def test_the_edge_is_soft_rather_than_jagged(self):
        from PIL import Image
        img = Image.new("RGB", (60, 60), (255, 255, 255))
        for x in range(20, 40):
            for y in range(20, 40):
                img.putpixel((x, y), (20, 60, 200))
        img.putpixel((19, 30), (240, 240, 240))           # just inside the ramp
        alpha = sprites.matte(img, (255, 255, 255)).getpixel((19, 30))[3]
        self.assertGreater(alpha, 0, "antialiasing was thrown away")
        self.assertLess(alpha, 255, "antialiasing was made fully opaque")


class TestEmptyFramesAreDropped(SpriteCase):
    """A region that mattes away to nothing is not a frame.

    Measured in the UI on the first real run: asking for four poses returned a
    107x10 strip at 0% opacity alongside two good sprites — the content pass
    found a faint smear, and the segmentation model then correctly removed all
    of it. Finding and matting each did their job; nothing was checking the
    result, so a blank cell rendered in the output and a blank PNG went into the
    library.
    """

    def test_a_frame_that_mattes_to_nothing_is_not_written(self):
        from PIL import Image
        img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        self.assertTrue(sprites.is_blank(img))

    def test_a_frame_with_a_subject_is_kept(self):
        from PIL import Image
        img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        for x in range(10, 30):
            for y in range(10, 30):
                img.putpixel((x, y), (20, 160, 60, 255))
        self.assertFalse(sprites.is_blank(img))

    def test_a_few_stray_opaque_pixels_still_count_as_blank(self):
        """Matting leaves speckle. Three surviving pixels is not a sprite."""
        from PIL import Image
        img = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
        for x in range(3):
            img.putpixel((x, 0), (255, 255, 255, 255))
        self.assertTrue(sprites.is_blank(img))

    def test_the_atlas_and_the_files_stay_in_step(self):
        """The bug this could turn into: drop a file but keep its atlas entry,
        and every later frame's metadata is off by one."""
        path = self.sheet([(10, 40, 50, 80), (120, 40, 50, 80)])
        out = tempfile.mkdtemp()
        data = sprites.pipeline(path, out, use_model=False)
        self.assertEqual(len(data["frames"]), len(os.listdir(out)) - 
                         len([f for f in os.listdir(out) if f == "atlas.json"]))
        for i, frame in enumerate(data["frames"]):
            self.assertEqual(frame["index"], i, "indices must be contiguous")
            self.assertTrue(os.path.isfile(frame["file"]))


class TestCutting(SpriteCase):
    def test_each_frame_is_written_out(self):
        path = self.sheet([(10, 40, 50, 80), (120, 40, 50, 80)])
        out = tempfile.mkdtemp()
        written = sprites.cut(path, out, transparent=False)
        self.assertEqual(len(written), 2)
        for f in written:
            self.assertTrue(os.path.exists(f))
            self.assertEqual(Image.open(f).size, (50, 80))

    def test_an_atlas_describes_where_each_frame_came_from(self):
        path = self.sheet([(10, 40, 50, 80), (120, 40, 50, 80)])
        atlas = sprites.atlas(path)
        self.assertEqual(atlas["source_size"], [400, 200])
        self.assertEqual(len(atlas["frames"]), 2)
        self.assertEqual(atlas["frames"][0]["x"], 10)


if __name__ == "__main__":
    unittest.main()
