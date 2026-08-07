#!/usr/bin/env python3
"""The path containment check, tested in isolation.

This is the only thing between `?path=` and an arbitrary file, and it was
previously four near-copies inside request handlers where it could only be
exercised by running a server. Every case here is one of those copies' possible
mistakes.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paths                                                       # noqa: E402


class ContainmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "outputs")
        os.makedirs(os.path.join(self.root, "music"))
        self.inside = os.path.join(self.root, "music", "take.flac")
        with open(self.inside, "w") as fh:
            fh.write("x")
        self.outside = os.path.join(self.tmp, "secret.txt")
        with open(self.outside, "w") as fh:
            fh.write("x")

    def test_file_under_root_is_contained(self):
        self.assertTrue(paths.contained(self.inside, [self.root]))

    def test_file_outside_root_is_not(self):
        self.assertFalse(paths.contained(self.outside, [self.root]))

    def test_dotdot_escape_is_rejected(self):
        escape = os.path.join(self.root, "music", "..", "..", "secret.txt")
        self.assertFalse(paths.contained(escape, [self.root]))

    def test_absolute_escape_is_rejected(self):
        self.assertFalse(paths.contained("/etc/passwd", [self.root]))

    def test_sibling_with_shared_prefix_is_rejected(self):
        """The bug a bare `startswith` has: /x/outputs-evil vs /x/outputs."""
        evil = self.root + "-evil"
        os.makedirs(evil)
        victim = os.path.join(evil, "take.flac")
        with open(victim, "w") as fh:
            fh.write("x")
        self.assertFalse(paths.contained(victim, [self.root]))

    def test_symlink_pointing_out_is_rejected(self):
        """Resolution has to happen before the comparison, not after."""
        link = os.path.join(self.root, "music", "escape.flac")
        os.symlink(self.outside, link)
        self.assertFalse(paths.contained(link, [self.root]))

    def test_symlinked_root_still_matches(self):
        """A root reached through a symlink is the same root."""
        alias = os.path.join(self.tmp, "alias")
        os.symlink(self.root, alias)
        self.assertTrue(paths.contained(os.path.join(alias, "music", "take.flac"),
                                        [self.root]))

    def test_root_itself_is_not_contained(self):
        self.assertFalse(paths.contained(self.root, [self.root]))

    def test_trailing_separator_on_root_is_tolerated(self):
        self.assertTrue(paths.contained(self.inside, [self.root + os.sep]))

    def test_any_of_several_roots_suffices(self):
        self.assertTrue(paths.contained(self.inside, ["/nonexistent", self.root]))

    def test_empty_and_none_are_rejected(self):
        for bad in (None, "", []):
            self.assertFalse(paths.contained(bad, [self.root]))
        self.assertFalse(paths.contained(self.inside, []))
        self.assertFalse(paths.contained(self.inside, None))

    def test_nul_byte_is_rejected_not_raised(self):
        """A handler must answer 404, not die with ValueError."""
        self.assertFalse(paths.contained("/tmp/take\0.flac", [self.root]))


class ResolveWithinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "outputs")
        os.makedirs(self.root)
        self.f = os.path.join(self.root, "a.flac")
        with open(self.f, "w") as fh:
            fh.write("x")

    def test_returns_the_resolved_path(self):
        """Callers must use the returned value, so it has to be the real one."""
        alias = os.path.join(self.tmp, "alias")
        os.symlink(self.root, alias)
        got = paths.resolve_within(os.path.join(alias, "a.flac"), [self.root])
        self.assertEqual(got, os.path.realpath(self.f))

    def test_returns_none_when_outside(self):
        self.assertIsNone(paths.resolve_within("/etc/passwd", [self.root]))

    def test_nonexistent_path_inside_root_still_resolves(self):
        """Containment is not existence — delete() wants to know both separately."""
        self.assertIsNotNone(paths.resolve_within(os.path.join(self.root, "gone.flac"),
                                                  [self.root]))


class SafeFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "outputs")
        os.makedirs(os.path.join(self.root, "sub"))
        self.f = os.path.join(self.root, "a.flac")
        with open(self.f, "w") as fh:
            fh.write("x")

    def test_regular_file_passes(self):
        self.assertEqual(paths.safe_file(self.f, [self.root]), os.path.realpath(self.f))

    def test_directory_is_refused(self):
        """Contained, but not something to open or hand to ffmpeg."""
        self.assertIsNone(paths.safe_file(os.path.join(self.root, "sub"), [self.root]))

    def test_missing_file_is_refused(self):
        self.assertIsNone(paths.safe_file(os.path.join(self.root, "gone"), [self.root]))


class OutputsDeleteTest(unittest.TestCase):
    """outputs.delete() had its own copy of the check; it now shares this one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["AIMUSIC_ROOT"] = self.tmp
        import outputs
        self.outputs = outputs
        self.music = os.path.join(self.tmp, "outputs", "music")
        os.makedirs(self.music)

    def _make(self, name):
        p = os.path.join(self.music, name)
        with open(p, "w") as fh:
            fh.write("x")
        return p

    def test_deletes_output_and_sidecar(self):
        p = self._make("a.flac")
        with open(p + ".json", "w") as fh:
            fh.write("{}")
        self.assertTrue(self.outputs.delete(p))
        self.assertFalse(os.path.exists(p))
        self.assertFalse(os.path.exists(p + ".json"))

    def test_refuses_outside_outputs(self):
        stranger = os.path.join(self.tmp, "keepme")
        with open(stranger, "w") as fh:
            fh.write("x")
        self.assertFalse(self.outputs.delete(stranger))
        self.assertTrue(os.path.exists(stranger))

    def test_refuses_traversal(self):
        stranger = os.path.join(self.tmp, "keepme2")
        with open(stranger, "w") as fh:
            fh.write("x")
        self.assertFalse(self.outputs.delete(os.path.join(self.music, "..", "..", "keepme2")))
        self.assertTrue(os.path.exists(stranger))


if __name__ == "__main__":
    unittest.main()
