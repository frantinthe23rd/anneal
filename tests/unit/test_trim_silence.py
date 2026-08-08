#!/usr/bin/env python3
"""Trailing digital silence is dead air, and every track had seconds of it.

Measured across finished tracks: the music stops and the file continues for
another two to eleven seconds of exact silence, padding to the requested
duration. In a player that gap reads as part of the track, which makes an
already-abrupt ending feel worse — and for a builder cutting a loop it is
simply wrong.

Trimming it is deterministic, touches no musical content, and is the one part
of "tracks end badly" that can be fixed with certainty rather than by asking a
model nicely.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
import wave

from tests.context import REPO_ROOT  # noqa: F401

import paths
import trim


def write_wav(path, samples, rate=16000):
    import array
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(array.array("h", [int(max(-1.0, min(1.0, s)) * 32000) for s in samples]).tobytes())


def tone(seconds, rate=16000, level=0.5):
    import math
    return [level * math.sin(2 * math.pi * 220 * i / rate) for i in range(int(seconds * rate))]


def duration(path):
    out = subprocess.run([paths.ffmpeg_bin().replace("ffmpeg", "ffprobe"),
                          "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True)
    return float(out.stdout.strip())


class TrimCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make(self, music_s, silence_s, name="t.wav"):
        path = os.path.join(self.tmp, name)
        write_wav(path, tone(music_s) + [0.0] * int(silence_s * 16000))
        return path


class TestTrimming(TrimCase):
    def test_trailing_silence_is_removed(self):
        path = self.make(3.0, 5.0)
        self.assertAlmostEqual(duration(path), 8.0, delta=0.2)
        trim.trim_trailing_silence(path)
        self.assertLess(duration(path), 4.0)

    def test_the_music_itself_is_not_shortened(self):
        """The failure that would matter: clipping the last note."""
        path = self.make(3.0, 5.0)
        trim.trim_trailing_silence(path)
        self.assertGreater(duration(path), 2.9)

    def test_a_short_tail_is_left_alone(self):
        """A little room after the last note is musical, not padding. Only a
        gap long enough to read as dead air is worth touching."""
        path = self.make(3.0, 0.3)
        before = duration(path)
        trim.trim_trailing_silence(path)
        self.assertAlmostEqual(duration(path), before, delta=0.15)

    def test_a_track_with_no_silence_is_untouched(self):
        path = self.make(3.0, 0.0)
        before = duration(path)
        trim.trim_trailing_silence(path)
        self.assertAlmostEqual(duration(path), before, delta=0.1)

    def test_leading_and_internal_silence_survive(self):
        """Only the tail. A rest inside the arrangement is the music, and
        removing it would be a far worse bug than the one being fixed."""
        path = os.path.join(self.tmp, "gap.wav")
        write_wav(path, [0.0] * 16000 + tone(1.0) + [0.0] * 32000 + tone(1.0) + [0.0] * 64000)
        trim.trim_trailing_silence(path)
        got = duration(path)
        self.assertGreater(got, 4.5, "the internal rest or the lead-in was eaten")
        self.assertLess(got, 6.0, "the trailing silence was not removed")

    def test_a_silent_file_is_left_alone_rather_than_emptied(self):
        """An all-silent take is a failed generation, not a file to reduce to
        zero bytes — losing the evidence makes it harder to diagnose."""
        path = os.path.join(self.tmp, "silent.wav")
        write_wav(path, [0.0] * 48000)
        trim.trim_trailing_silence(path)
        self.assertTrue(os.path.isfile(path))
        self.assertGreater(duration(path), 0.5)

    def test_it_returns_how_much_it_removed(self):
        path = self.make(3.0, 5.0)
        removed = trim.trim_trailing_silence(path)
        self.assertGreater(removed, 4.0)

    def test_a_missing_file_is_not_an_error(self):
        """Best-effort: trimming must never be the reason a take is lost."""
        self.assertEqual(trim.trim_trailing_silence(os.path.join(self.tmp, "nope.wav")), 0.0)

    def test_the_original_is_replaced_not_left_beside_a_temp_file(self):
        path = self.make(2.0, 4.0)
        trim.trim_trailing_silence(path)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["t.wav"])


class TestFormatIsPreserved(TrimCase):
    def test_a_flac_stays_flac_and_lossless(self):
        """Masters are FLAC and the whole point of that is losing nothing.
        Re-encoding through a lossy step here would be silent damage."""
        src = self.make(2.0, 4.0, "src.wav")
        flac = os.path.join(self.tmp, "master.flac")
        subprocess.run([paths.ffmpeg_bin(), "-v", "error", "-y", "-i", src, flac], check=True)
        trim.trim_trailing_silence(flac)
        out = subprocess.run([paths.ffmpeg_bin().replace("ffmpeg", "ffprobe"), "-v", "error",
                              "-show_entries", "stream=codec_name", "-of", "csv=p=0", flac],
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "flac")
        self.assertLess(duration(flac), 3.0)


if __name__ == "__main__":
    unittest.main()
