"""outputs.py — the library, and the one thing in it that must never be wrong.

`delete()` takes a path straight off the wire. Everything else here is
convenience; that check is the boundary between "remove a track I made" and
"remove any file this process can write". It gets the most cases.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import outputs


class OutputsCase(unittest.TestCase):
    """Each test gets its own outputs root, because outputs.root() is read from
    the environment on every call rather than captured at import."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="anneal-outputs-")
        self._saved_root = os.environ.get("AIMUSIC_ROOT")
        os.environ["AIMUSIC_ROOT"] = self.base
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved_root is None:
            os.environ.pop("AIMUSIC_ROOT", None)
        else:
            os.environ["AIMUSIC_ROOT"] = self._saved_root
        shutil.rmtree(self.base, ignore_errors=True)

    def write(self, kind, name, body=b"x", meta=None, created=None):
        path = os.path.join(outputs.kind_dir(kind), name)
        with open(path, "wb") as fh:
            fh.write(body)
        if meta is not None or created is not None:
            meta = dict(meta or {})
            if created is not None:
                meta["created"] = created
            outputs.write_sidecar(path, meta)
        return path


class TestPaths(OutputsCase):
    def test_root_follows_the_environment(self):
        self.assertEqual(outputs.root(), os.path.join(self.base, "outputs"))

    def test_kind_dir_creates_and_buckets_unknown_kinds(self):
        for kind in outputs.KINDS:
            self.assertEqual(os.path.basename(outputs.kind_dir(kind)), kind)
            self.assertTrue(os.path.isdir(outputs.kind_dir(kind)))
        self.assertEqual(os.path.basename(outputs.kind_dir("video")), "other")
        self.assertEqual(os.path.basename(outputs.kind_dir(None)), "other")


class TestSlugify(OutputsCase):
    def test_cases(self):
        for raw, want in [
            ("Warm lo-fi hip hop", "warm-lo-fi-hip-hop"),
            ("  spaced  out  ", "spaced-out"),
            ("!!!", "untitled"),
            ("", "untitled"),
            (None, "untitled"),
            ("Trentemøller — Moan", "trentem-ller-moan"),
            ("a/b\\c?d&e", "a-b-c-d-e"),
        ]:
            self.assertEqual(outputs.slugify(raw), want, raw)

    def test_truncated_to_the_limit(self):
        self.assertEqual(len(outputs.slugify("x" * 200)), 48)
        self.assertEqual(len(outputs.slugify("x" * 200, limit=10)), 10)


class TestSidecars(OutputsCase):
    def test_write_sidecar_fills_in_created_and_file(self):
        path = self.write("music", "take.flac")
        outputs.write_sidecar(path, {"prompt": "a brief"})
        with open(path + outputs.SIDECAR_SUFFIX) as fh:
            meta = json.load(fh)
        self.assertEqual(meta["prompt"], "a brief")
        self.assertEqual(meta["file"], "take.flac")
        self.assertIsInstance(meta["created"], float)

    def test_write_sidecar_does_not_override_supplied_values(self):
        path = self.write("music", "take.flac")
        outputs.write_sidecar(path, {"created": 1.0, "file": "elsewhere.flac"})
        with open(path + outputs.SIDECAR_SUFFIX) as fh:
            meta = json.load(fh)
        self.assertEqual(meta["created"], 1.0)
        self.assertEqual(meta["file"], "elsewhere.flac")

    def test_write_sidecar_swallows_io_errors(self):
        # Metadata must never be the reason a generation request fails.
        outputs.write_sidecar(os.path.join(self.base, "no", "such", "dir", "x"), {})

    def test_save_bytes_writes_file_and_sidecar(self):
        path = outputs.save_bytes("speech", b"RIFF....", ".wav", {"prompt": "Hello there"})
        self.assertTrue(path.startswith(os.path.join(outputs.root(), "speech") + os.sep))
        self.assertTrue(path.endswith(".wav"))
        self.assertIn("hello-there", os.path.basename(path))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"RIFF....")
        self.assertTrue(os.path.isfile(path + outputs.SIDECAR_SUFFIX))

    def test_save_copy_round_trip(self):
        src = os.path.join(self.base, "source.flac")
        with open(src, "wb") as fh:
            fh.write(b"fLaC")
        path = outputs.save_copy("music", src, {"prompt": "a source"})
        self.assertTrue(path.endswith(".flac"))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"fLaC")

    def test_save_copy_of_a_missing_source_returns_none(self):
        self.assertIsNone(outputs.save_copy("music", os.path.join(self.base, "gone.flac"), {}))

    def test_adopt_attaches_metadata_without_moving_anything(self):
        path = self.write("images", "already-here.png")
        self.assertEqual(outputs.adopt("images", path, {"prompt": "a cover"}), path)
        with open(path + outputs.SIDECAR_SUFFIX) as fh:
            self.assertEqual(json.load(fh)["prompt"], "a cover")

    def test_adopt_leaves_an_existing_sidecar_alone(self):
        path = self.write("images", "already-here.png", meta={"prompt": "original"})
        outputs.adopt("images", path, {"prompt": "second thoughts"})
        with open(path + outputs.SIDECAR_SUFFIX) as fh:
            self.assertEqual(json.load(fh)["prompt"], "original")

    def test_adopt_rejects_nothing_and_missing_files(self):
        self.assertIsNone(outputs.adopt("images", None, {}))
        self.assertIsNone(outputs.adopt("images", "", {}))
        self.assertIsNone(outputs.adopt("images", os.path.join(self.base, "nope.png"), {}))


class TestListing(OutputsCase):
    def setUp(self):
        OutputsCase.setUp(self)
        self.write("music", "old.flac", created=1000.0, meta={"prompt": "old one"})
        self.write("music", "new.flac", created=3000.0, meta={"prompt": "new one"})
        self.write("images", "mid.png", created=2000.0, meta={"prompt": "a picture"})

    def test_newest_first_across_all_kinds(self):
        got = outputs.listing()
        self.assertEqual(got["total"], 3)
        self.assertEqual([i["name"] for i in got["items"]], ["new.flac", "mid.png", "old.flac"])

    def test_filtering_by_kind(self):
        got = outputs.listing(kind="images")
        self.assertEqual(got["total"], 1)
        self.assertEqual(got["items"][0]["kind"], "images")

    def test_unknown_kind_falls_back_to_everything(self):
        # Current behaviour, asserted so a change to it is a deliberate one:
        # an unrecognised `kind` widens the query rather than emptying it.
        self.assertEqual(outputs.listing(kind="video")["total"], 3)

    def test_sidecars_and_dotfiles_are_not_themselves_items(self):
        self.write("music", ".DS_Store")
        names = [i["name"] for i in outputs.listing()["items"]]
        self.assertNotIn(".DS_Store", names)
        self.assertFalse([n for n in names if n.endswith(".json")])

    def test_limit_and_offset(self):
        page = outputs.listing(limit=1, offset=1)
        self.assertEqual(page["total"], 3)               # total is the whole set
        self.assertEqual([i["name"] for i in page["items"]], ["mid.png"])

    def test_entry_shape(self):
        item = outputs.listing(kind="images")["items"][0]
        self.assertEqual(
            sorted(item), ["bytes", "created", "kind", "meta", "name", "path", "prompt", "url"])
        self.assertEqual(item["url"], "/v1/outputs/file?path=" + item["path"])
        # `created` and `file` are the sidecar's own bookkeeping, not metadata
        # about the generation, so they are not echoed into `meta`.
        self.assertNotIn("created", item["meta"])
        self.assertNotIn("file", item["meta"])

    def test_missing_sidecar_falls_back_to_mtime(self):
        path = self.write("speech", "bare.wav")
        item = [i for i in outputs.listing()["items"] if i["name"] == "bare.wav"][0]
        self.assertAlmostEqual(item["created"], os.stat(path).st_mtime, places=3)
        self.assertEqual(item["prompt"], "")

    def test_corrupt_sidecar_does_not_lose_the_item(self):
        path = self.write("speech", "bad.wav")
        with open(path + outputs.SIDECAR_SUFFIX, "w") as fh:
            fh.write("{not json")
        self.assertIn("bad.wav", [i["name"] for i in outputs.listing()["items"]])

    def test_missing_kind_directory_is_not_an_error(self):
        shutil.rmtree(os.path.join(outputs.root(), "speech"), ignore_errors=True)
        self.assertEqual(outputs.listing()["total"], 3)


class TestDeleteContainment(OutputsCase):
    """The security boundary. Anything that is not demonstrably inside
    outputs/ must be refused, and refusal must not raise."""

    def test_removes_the_file_and_its_sidecar(self):
        path = self.write("music", "gone.flac", meta={"prompt": "bye"})
        self.assertTrue(outputs.delete(path))
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(path + outputs.SIDECAR_SUFFIX))

    def test_missing_sidecar_is_not_a_failure(self):
        path = self.write("music", "nosidecar.flac")
        self.assertTrue(outputs.delete(path))

    def test_refuses_absolute_paths_outside_the_root(self):
        for path in ["/etc/passwd", "/etc/hosts", os.path.join(self.base, "outside.txt")]:
            self.assertFalse(outputs.delete(path), path)

    def test_refuses_traversal_out_of_the_root(self):
        escape = os.path.join(outputs.root(), "music", "..", "..", "escapee.txt")
        with open(os.path.join(self.base, "escapee.txt"), "w") as fh:
            fh.write("still here")
        self.assertFalse(outputs.delete(escape))
        self.assertTrue(os.path.isfile(os.path.join(self.base, "escapee.txt")))

    def test_refuses_the_root_itself(self):
        outputs.kind_dir("music")                   # make sure the tree exists
        self.assertFalse(outputs.delete(outputs.root()))
        self.assertFalse(outputs.delete(outputs.root() + os.sep))
        self.assertTrue(os.path.isdir(outputs.root()))

    def test_refuses_a_sibling_directory_that_shares_the_prefix(self):
        # `startswith(root)` without the separator would accept this; the
        # separator is what makes the check a containment check.
        sibling = outputs.root() + "-elsewhere"
        os.makedirs(sibling, exist_ok=True)
        victim = os.path.join(sibling, "victim.flac")
        with open(victim, "w") as fh:
            fh.write("x")
        self.assertFalse(outputs.delete(victim))
        self.assertTrue(os.path.isfile(victim))

    def test_refuses_a_symlink_that_points_out_of_the_root(self):
        target = os.path.join(self.base, "secret.txt")
        with open(target, "w") as fh:
            fh.write("secret")
        link = os.path.join(outputs.kind_dir("music"), "innocent.flac")
        os.symlink(target, link)
        self.assertFalse(outputs.delete(link))
        self.assertTrue(os.path.isfile(target))

    def test_refuses_empty_and_junk_input(self):
        for path in ["", ".", "..", "relative/thing.flac"]:
            self.assertFalse(outputs.delete(path), repr(path))

    def test_deleting_something_that_is_not_there_is_false_not_an_exception(self):
        self.assertFalse(outputs.delete(os.path.join(outputs.root(), "music", "ghost.flac")))


if __name__ == "__main__":
    unittest.main()
