"""Retention is manual, and the tool that does it must not surprise anyone.

Two properties carry the whole design and both are easy to break silently:
it deletes nothing without --delete, and it refuses to orphan a press record.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

from tests.context import REPO_ROOT

sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
import outputs                      # noqa: E402
import prune                        # noqa: E402


class PruneCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._root = outputs.root
        outputs.root = lambda: self.tmp
        self.addCleanup(setattr, outputs, "root", self._root)
        for kind in outputs.KINDS:
            os.makedirs(os.path.join(self.tmp, kind), exist_ok=True)

    def write(self, kind, name, age_days=0, size=16):
        path = os.path.join(self.tmp, kind, name)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        with open(path + outputs.SIDECAR_SUFFIX, "w") as fh:
            json.dump({"prompt": name}, fh)
        when = time.time() - age_days * 86400
        os.utime(path, (when, when))
        return path

    def press_db(self, referenced=(), cover=None):
        db_path = os.path.join(self.tmp, "presses.db")
        db = sqlite3.connect(db_path)
        db.execute("CREATE TABLE presses (tracks TEXT, cover TEXT)")
        tracks = [{"file": "/v1/audio?path=" + p.replace("/", "%2F")} for p in referenced]
        db.execute("INSERT INTO presses VALUES (?, ?)",
                   (json.dumps(tracks), json.dumps({"path": cover}) if cover else None))
        db.commit()
        db.close()
        return db_path


class TestWhatCounts(PruneCase):
    def test_only_files_past_the_cutoff(self):
        self.write("music", "old.flac", age_days=100)
        self.write("music", "new.flac", age_days=1)
        got = [os.path.basename(f["path"]) for f in prune.candidates(90, ["music"])]
        self.assertEqual(got, ["old.flac"])

    def test_sidecars_are_not_candidates_in_their_own_right(self):
        self.write("music", "old.flac", age_days=100)
        got = [os.path.basename(f["path"]) for f in prune.candidates(90, ["music"])]
        self.assertEqual(got, ["old.flac"], "the .json must travel with its file")

    def test_kinds_can_be_limited(self):
        self.write("music", "old.flac", age_days=100)
        self.write("images", "old.png", age_days=100)
        got = [f["kind"] for f in prune.candidates(90, ["images"])]
        self.assertEqual(got, ["images"])

    def test_oldest_first(self):
        self.write("music", "older.flac", age_days=300)
        self.write("music", "old.flac", age_days=100)
        got = [os.path.basename(f["path"]) for f in prune.candidates(90, ["music"])]
        self.assertEqual(got, ["older.flac", "old.flac"])


class TestPressReferences(PruneCase):
    """A press stores its tracks as the URL the player fetches, not as a path.
    Failing to decode that would make the protection silently do nothing."""

    def test_a_track_url_is_decoded_back_to_its_path(self):
        track = self.write("music", "kept.flac", age_days=200)
        db = self.press_db(referenced=[track])
        self.assertIn(os.path.realpath(track), prune.referenced_paths(db))

    def test_a_cover_is_found_too(self):
        cover = self.write("images", "cover.png", age_days=200)
        db = self.press_db(referenced=[], cover=cover)
        self.assertIn(os.path.realpath(cover), prune.referenced_paths(db))

    def test_no_database_means_nothing_is_protected_rather_than_an_error(self):
        self.assertEqual(prune.referenced_paths(os.path.join(self.tmp, "nope.db")), set())

    def test_an_unreadable_database_raises_rather_than_deleting(self):
        """Silence here would delete exactly the files it exists to protect."""
        bad = os.path.join(self.tmp, "corrupt.db")
        with open(bad, "wb") as fh:
            fh.write(b"not a database")
        with self.assertRaises(sqlite3.Error):
            prune.referenced_paths(bad)


class TestItDoesNotSurprise(PruneCase):
    def run_main(self, *argv):
        from io import StringIO
        held, sys.stdout = sys.stdout, StringIO()
        try:
            code = prune.main(list(argv))
            return code, sys.stdout.getvalue()
        finally:
            sys.stdout = held

    def test_nothing_is_deleted_without_the_flag(self):
        path = self.write("music", "old.flac", age_days=200)
        code, out = self.run_main("--older-than", "90")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(path), "a dry run must not remove anything")
        self.assertIn("Nothing deleted", out)

    def test_delete_removes_the_file_and_its_sidecar(self):
        path = self.write("music", "old.flac", age_days=200)
        prune.PRESS_DB = os.path.join(self.tmp, "absent.db")
        code, _ = self.run_main("--older-than", "90", "--delete")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(path + outputs.SIDECAR_SUFFIX))

    def test_a_pressed_track_is_spared_by_default(self):
        kept = self.write("music", "pressed.flac", age_days=200)
        loose = self.write("music", "loose.flac", age_days=200)
        prune.PRESS_DB = self.press_db(referenced=[kept])
        code, out = self.run_main("--older-than", "90", "--delete")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(kept), "would have orphaned a press record")
        self.assertFalse(os.path.exists(loose))
        self.assertIn("kept because a press still points at them", out)

    def test_include_pressed_takes_them_anyway(self):
        kept = self.write("music", "pressed.flac", age_days=200)
        prune.PRESS_DB = self.press_db(referenced=[kept])
        self.run_main("--older-than", "90", "--include-pressed", "--delete")
        self.assertFalse(os.path.exists(kept))


class TestUsageReporting(PruneCase):
    """The half that is automated: knowing, so nobody finds out by filling a disk."""

    def test_counts_bytes_and_files_per_kind(self):
        self.write("music", "a.flac", size=100)
        self.write("images", "b.png", size=50)
        u = outputs.usage(now=time.time() + 10_000)     # past any cache
        self.assertEqual(u["by_kind"]["music"]["files"], 1)
        self.assertEqual(u["by_kind"]["music"]["bytes"], 100)
        self.assertEqual(u["bytes"], 150)

    def test_sidecars_are_not_counted_as_files(self):
        self.write("music", "a.flac", size=100)
        u = outputs.usage(now=time.time() + 20_000)
        self.assertEqual(u["files"], 1, "the .json is metadata, not an output")


if __name__ == "__main__":
    unittest.main()
