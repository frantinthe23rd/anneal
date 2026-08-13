#!/usr/bin/env python3
"""A pressed track carries its own album details, cover included.

Real files throughout — a one-second tone and a small PNG, both made by ffmpeg
in the sandbox. Mocking the encoder would test the argument list rather than the
thing that matters, which is whether a player finds a title and a picture. Two
of these fail against a mock and pass against ffmpeg.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tests.context import REPO_ROOT  # noqa: F401  (sandboxes the environment)

import paths
import tags


def _have_ffmpeg():
    try:
        paths.ffmpeg_bin()
        return True
    except Exception:
        return False


@unittest.skipUnless(_have_ffmpeg(), "ffmpeg is not installed")
class TagCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def tone(self, name="track.flac", seconds=1):
        path = os.path.join(self.tmp, name)
        subprocess.run([paths.ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=%d" % seconds, path],
                       check=True, timeout=60)
        return path

    def art(self, name="cover.png"):
        path = os.path.join(self.tmp, name)
        subprocess.run([paths.ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi",
                        "-i", "color=c=orange:s=64x64:d=1", "-frames:v", "1", path],
                       check=True, timeout=60)
        return path

    def read_tags(self, path):
        out = subprocess.run(
            [paths.ffprobe_bin(), "-v", "error", "-show_entries", "format_tags",
             "-of", "json", path], capture_output=True, text=True, timeout=30)
        fmt = (json.loads(out.stdout or "{}").get("format") or {})
        return {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}


class TestWhatATrackShouldSay(unittest.TestCase):
    """The mapping, without an encoder in the way."""

    PLAN = {"title": "Solstice Drive", "artist": "Kite Season"}

    def test_it_carries_the_album_and_the_artist(self):
        t = tags.track_tags(self.PLAN, {"n": 2, "title": "Eventide"}, total=4)
        self.assertEqual(t["title"], "Eventide")
        self.assertEqual(t["album"], "Solstice Drive")
        self.assertEqual(t["artist"], "Kite Season")
        self.assertEqual(t["album_artist"], "Kite Season")

    def test_the_track_number_knows_the_total(self):
        self.assertEqual(tags.track_tags(self.PLAN, {"n": 2}, total=9)["track"], "2/9")
        self.assertEqual(tags.track_tags(self.PLAN, {"n": 2})["track"], "2")

    def test_the_style_becomes_a_genre(self):
        t = tags.track_tags(self.PLAN, {"n": 1, "style": "Deep Techno, 125 BPM, pads"})
        self.assertEqual(t["genre"], "Deep Techno")

    def test_nothing_invented_when_the_plan_is_bare(self):
        t = tags.track_tags({}, {"n": 1})
        self.assertEqual(t["album"], "")
        self.assertNotIn("genre", t)
        self.assertNotIn("date", t)


class TestTheFileActuallyCarriesIt(TagCase):
    def test_tags_are_written(self):
        path = self.tone()
        self.assertTrue(tags.embed(path, tags={"title": "Eventide",
                                               "artist": "Kite Season",
                                               "album": "Solstice Drive"}))
        got = self.read_tags(path)
        self.assertEqual(got.get("title"), "Eventide")
        self.assertEqual(got.get("album"), "Solstice Drive")

    def test_the_cover_is_attached(self):
        path = self.tone()
        self.assertFalse(tags.has_cover(path))
        self.assertTrue(tags.embed(path, cover=self.art(), tags={"title": "x"}))
        self.assertTrue(tags.has_cover(path))

    def audio_md5(self, path):
        """Hash the decoded *audio* only. `-f md5` with no mapping hashes every
        output stream, so once a cover is attached it hashes the picture too and
        reports a difference that is not there."""
        out = subprocess.run(
            [paths.ffmpeg_bin(), "-v", "error", "-i", path, "-map", "0:a",
             "-f", "md5", "-"], capture_output=True, text=True, timeout=60)
        return out.stdout.strip()

    def test_the_audio_is_not_re_encoded(self):
        """A lossless master must survive a metadata rewrite bit for bit."""
        path = self.tone()
        before = self.audio_md5(path)
        tags.embed(path, cover=self.art(), tags={"title": "x"})
        after = self.audio_md5(path)
        self.assertTrue(before)
        self.assertEqual(before, after)

    def test_an_mp3_can_hold_one_too(self):
        path = self.tone("track.mp3")
        self.assertTrue(tags.embed(path, cover=self.art(), tags={"title": "x"}))
        self.assertTrue(tags.has_cover(path))

    def test_a_wav_is_tagged_but_not_pictured(self):
        """WAV has no agreed way to carry a picture, and writing one anyway
        produces a file some players refuse."""
        path = self.tone("track.wav")
        tags.embed(path, cover=self.art(), tags={"title": "Eventide"})
        self.assertFalse(tags.has_cover(path))

    # -- it must never cost a take ---------------------------------------
    def test_a_missing_cover_still_tags(self):
        path = self.tone()
        self.assertTrue(tags.embed(path, cover=os.path.join(self.tmp, "nope.png"),
                                   tags={"title": "Eventide"}))
        self.assertEqual(self.read_tags(path).get("title"), "Eventide")

    def test_a_file_that_is_not_audio_is_left_alone(self):
        path = os.path.join(self.tmp, "notes.txt")
        with open(path, "w") as fh:
            fh.write("not audio")
        self.assertFalse(tags.embed(path, tags={"title": "x"}))
        with open(path) as fh:
            self.assertEqual(fh.read(), "not audio")

    def test_a_missing_file_is_not_an_error(self):
        self.assertFalse(tags.embed(os.path.join(self.tmp, "gone.flac"),
                                    tags={"title": "x"}))

    def test_nothing_to_write_is_not_a_rewrite(self):
        self.assertFalse(tags.embed(self.tone()))

    def test_a_failure_leaves_the_original_untouched(self):
        """The whole point of the temp file and the replace."""
        path = self.tone()
        size = os.path.getsize(path)
        self.assertFalse(tags.embed(path, tags={"title": "x"}, timeout=0.0001))
        self.assertEqual(os.path.getsize(path), size)
        self.assertEqual(len([f for f in os.listdir(self.tmp)
                              if f.startswith("track") and f.endswith(".flac")]), 1)


@unittest.skipUnless(_have_ffmpeg(), "ffmpeg is not installed")
class TestPressTagsWhatItMade(TagCase):
    """The stage runs after the cover, which is the only moment both the audio
    and the artwork exist — Press paints last so the music model loads once."""

    def setUp(self):
        super().setUp()
        import builder
        self.builder = builder
        self.root = os.path.join(self.tmp, "outputs", "music")
        os.makedirs(self.root, exist_ok=True)
        self.press = builder.Press(
            builder.PressStore(os.path.join(self.tmp, "p.db")),
            call_text=lambda p, n=900: "{}",
            call_music=lambda payload: [],
            call_image=lambda p, size: {},
            log=lambda *a: None,
        )

    def track(self, n, title, state="done"):
        path = os.path.join(self.root, "t%d.flac" % n)
        subprocess.run([paths.ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=1", path],
                       check=True, timeout=60)
        return {"n": n, "title": title, "state": state, "duration": 60,
                "style": "Deep Techno, 125 BPM",
                "file": "/v1/audio?path=" + path if state == "done" else None}

    def run_stage(self, tracks, cover):
        import paths as p
        real = p.aimusic_root
        p.aimusic_root = lambda: self.tmp
        try:
            return self.press._tag_tracks(
                {"title": "Solstice Drive", "artist": "Kite Season"},
                tracks, cover, {})
        finally:
            p.aimusic_root = real

    def test_every_finished_track_gets_the_album_and_the_cover(self):
        tracks = [self.track(1, "Eventide"), self.track(2, "Crimson")]
        written = self.run_stage(tracks, {"path": self.art()})
        self.assertEqual(written, 2)
        for t in tracks:
            path = t["file"].split("path=", 1)[1]
            got = self.read_tags(path)
            self.assertEqual(got.get("album"), "Solstice Drive")
            self.assertEqual(got.get("artist"), "Kite Season")
            self.assertTrue(tags.has_cover(path))
        self.assertEqual(self.read_tags(tracks[0]["file"].split("path=", 1)[1])
                         .get("title"), "Eventide")

    def test_the_track_number_counts_the_whole_tracklist(self):
        tracks = [self.track(1, "One"), self.track(2, "Two"), self.track(3, "Three")]
        self.run_stage(tracks, {"path": self.art()})
        got = self.read_tags(tracks[1]["file"].split("path=", 1)[1])
        self.assertEqual(got.get("track"), "2/3")

    def test_a_press_with_no_cover_still_gets_its_tags(self):
        """Cover art off, or a cover that failed, is not a reason to ship an
        untitled file."""
        tracks = [self.track(1, "Eventide")]
        self.assertEqual(self.run_stage(tracks, None), 1)
        path = tracks[0]["file"].split("path=", 1)[1]
        self.assertEqual(self.read_tags(path).get("title"), "Eventide")
        self.assertFalse(tags.has_cover(path))

    def test_a_failed_track_is_skipped(self):
        tracks = [self.track(1, "Eventide"), self.track(2, "Gone", state="failed")]
        self.assertEqual(self.run_stage(tracks, {"path": self.art()}), 1)

    def test_nothing_finished_is_not_an_error(self):
        self.assertEqual(self.run_stage([self.track(1, "x", state="failed")], None), 0)

    def test_a_path_outside_outputs_is_refused(self):
        """The file field round trips through sqlite and becomes an ffmpeg
        argument, so it is checked against the output roots like any other."""
        outside = os.path.join(self.tmp, "elsewhere.flac")
        subprocess.run([paths.ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=1", outside],
                       check=True, timeout=60)
        t = {"n": 1, "title": "x", "state": "done", "duration": 60,
             "file": "/v1/audio?path=" + outside}
        self.assertEqual(self.run_stage([t], None), 0)
        self.assertEqual(self.read_tags(outside).get("title"), None)
