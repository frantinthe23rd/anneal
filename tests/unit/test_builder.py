"""builder.py — Press, driven end to end with the three services stubbed out.

`Press` takes its three callables by injection, which is the whole reason this
is testable without a model: the stubs record what they were asked for and
return canned replies, so a "twenty-minute album" runs in milliseconds and the
plan normalisation — track counts, durations, stage ordering — can be asserted
on directly.

Nothing here touches the network. The real callables reach the gateway on
loopback; these do not exist outside the test.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import builder
from builder import Press, PressStore, extract_json, slug


class FakeText:
    """Stands in for the planning/lyric model. Returns the plan JSON for the
    first call and lyrics thereafter, and keeps every prompt it was given."""

    def __init__(self, plan=None, lyrics="[verse]\nwords\n"):
        self.plan = plan
        self.lyrics = lyrics
        self.prompts = []

    def __call__(self, prompt, max_tokens=900):
        self.prompts.append(prompt)
        if prompt.startswith("You are planning"):
            return json.dumps(self.plan) if self.plan is not None else "no json here"
        return self.lyrics

    @property
    def plan_prompt(self):
        return self.prompts[0]

    @property
    def lyric_prompts(self):
        return [p for p in self.prompts if p.startswith("Write song lyrics")]


class FakeMusic:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def __call__(self, payload):
        self.calls.append(payload)
        if len(self.calls) in self.fail_on:
            raise RuntimeError("backend said no")
        return [{"file": "/v1/audio?path=%%2Ftmp%%2Ftake-%d.flac" % len(self.calls)}]


class FakeImage:
    def __init__(self):
        self.calls = []

    def __call__(self, prompt, size):
        self.calls.append((prompt, size))
        return {"path": "/tmp/cover.png", "seed": 1}


PLAN = {
    "title": "Concrete Season",
    "artist": "The Grey Estate",
    "concept": "winter in a new town",
    "cover_art": "a frozen car park at dusk",
    "tracks": [{"title": "Opener", "theme": "arriving", "style": "post-punk",
                "duration_seconds": 60},
               {"title": "Centrepiece", "theme": "staying", "style": "shoegaze",
                "duration_seconds": 200},
               {"title": "Closer", "theme": "leaving", "style": "ambient",
                "duration_seconds": 120}],
}


class PressCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="anneal-press-")
        self._saved_root = os.environ.get("AIMUSIC_ROOT")
        # _write_manifest reads AIMUSIC_ROOT at call time and writes under
        # outputs/albums; keep that inside the test's own directory.
        os.environ["AIMUSIC_ROOT"] = self.dir
        self.store = PressStore(os.path.join(self.dir, "presses.db"))
        self.text = FakeText(plan=PLAN)
        self.music = FakeMusic()
        self.image = FakeImage()
        self.press = Press(self.store, self.text, self.music, self.image,
                           log=lambda *a: None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved_root is None:
            os.environ.pop("AIMUSIC_ROOT", None)
        else:
            os.environ["AIMUSIC_ROOT"] = self._saved_root
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_press(self, request):
        pid = self.store.create(request)
        self.press.run(pid)
        return pid, self.store.get(pid)


class TestHelpers(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(slug("Concrete Season"), "concrete-season")
        self.assertEqual(slug(""), "untitled")
        self.assertEqual(slug("!!!"), "untitled")
        self.assertEqual(len(slug("x" * 100)), 48)

    def test_extract_json_from_a_bare_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_extract_json_from_prose(self):
        self.assertEqual(extract_json('Sure! Here you go:\n{"a": 1}\nHope that helps.'),
                         {"a": 1})

    def test_extract_json_from_a_fenced_block(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('```\n{"a": 1}\n```'), {"a": 1})

    def test_extract_json_handles_nesting(self):
        self.assertEqual(extract_json('prose {"a": {"b": [1, 2]}} more prose'),
                         {"a": {"b": [1, 2]}})

    def test_extract_json_gives_up_cleanly(self):
        for raw in [None, "", "no braces at all", "{unclosed", "{not: valid}"]:
            self.assertIsNone(extract_json(raw), repr(raw))


class TestTrackCountClamping(PressCase):
    def test_absent_track_count_means_a_single(self):
        _, press = self.run_press({"prompt": "a brief"})
        self.assertEqual(len(press["tracks"]), 1)

    def test_zero_and_negative_are_raised_to_one(self):
        for asked in (0, -1, -100):
            _, press = self.run_press({"prompt": "a brief", "tracks": asked})
            self.assertEqual(len(press["tracks"]), 1, asked)

    def test_more_than_the_cap_is_capped(self):
        """Against `builder.MAX_TRACKS`, not a copy of it. Written as a literal
        8, this passed while the gateway had been raised to 20 and went on
        planning eight-track albums for briefs asking for ten."""
        for asked in (builder.MAX_TRACKS + 1, 50, 10000):
            _, press = self.run_press({"prompt": "a brief", "tracks": asked})
            self.assertEqual(len(press["tracks"]), builder.MAX_TRACKS, asked)

    def test_the_range_between_is_honoured(self):
        for asked in range(1, builder.MAX_TRACKS + 1):
            _, press = self.run_press({"prompt": "a brief", "tracks": asked})
            self.assertEqual(len(press["tracks"]), asked)

    def test_a_numeric_string_is_accepted(self):
        _, press = self.run_press({"prompt": "a brief", "tracks": "3"})
        self.assertEqual(len(press["tracks"]), 3)

    def test_a_plan_short_of_tracks_is_padded_rather_than_failed(self):
        # The planning model does not reliably honour the count, and a
        # twenty-minute job must not die over a formatting lapse.
        self.text.plan = dict(PLAN, tracks=[PLAN["tracks"][0]])
        _, press = self.run_press({"prompt": "a brief", "tracks": 4})
        self.assertEqual(len(press["tracks"]), 4)
        self.assertEqual([t["title"] for t in press["tracks"]][1:],
                         ["Untitled 2", "Untitled 3", "Untitled 4"])

    def test_a_plan_with_too_many_tracks_is_truncated(self):
        _, press = self.run_press({"prompt": "a brief", "tracks": 2})
        self.assertEqual([t["title"] for t in press["tracks"]], ["Opener", "Centrepiece"])

    def test_an_unparseable_plan_still_produces_the_requested_tracks(self):
        self.text.plan = None                       # FakeText then returns prose
        _, press = self.run_press({"prompt": "a brief", "tracks": 3})
        self.assertEqual(len(press["tracks"]), 3)
        self.assertEqual(press["plan"]["artist"], "Unknown Artist")
        self.assertEqual(press["plan"]["title"], "a brief")


class TestDurationDerivation(PressCase):
    """`duration` is a nominal length the plan varies around. dmin/dmax are
    derived from it unless given, and every planned track is clamped into
    that window."""

    def bounds(self):
        """Pull the window back out of the prompt the planner was handed."""
        import re
        match = re.search(r"between (\d+) and (\d+)\s*\nseconds", self.text.plan_prompt)
        return (int(match.group(1)), int(match.group(2))) if match else None

    def test_defaults_are_sixty_percent_and_one_and_a_half_times_the_target(self):
        self.run_press({"prompt": "a brief", "tracks": 3, "duration": 100})
        self.assertEqual(self.bounds(), (60, 150))

    def test_the_floor_is_twenty_seconds(self):
        self.run_press({"prompt": "a brief", "tracks": 3, "duration": 30})
        self.assertEqual(self.bounds(), (20, 45))   # 18 would be too short to be music

    def test_the_ceiling_is_ten_minutes(self):
        self.run_press({"prompt": "a brief", "tracks": 3, "duration": 500})
        self.assertEqual(self.bounds(), (300, 600))  # 750 clamped to 600

    def test_explicit_bounds_win(self):
        self.run_press({"prompt": "a brief", "tracks": 3,
                        "duration": 100, "duration_min": 45, "duration_max": 200})
        self.assertEqual(self.bounds(), (45, 200))

    def test_an_inverted_window_is_widened_rather_than_rejected(self):
        self.run_press({"prompt": "a brief", "tracks": 3,
                        "duration_min": 200, "duration_max": 100})
        self.assertEqual(self.bounds(), (200, 230))

    def test_planned_durations_are_clamped_into_the_window(self):
        _, press = self.run_press({"prompt": "a brief", "tracks": 3,
                                   "duration": 100, "duration_min": 80, "duration_max": 150})
        # The plan asked for 60 / 200 / 120.
        self.assertEqual([t["duration"] for t in press["tracks"]], [80, 150, 120])

    def test_a_track_with_no_or_junk_duration_falls_back_to_the_target(self):
        self.text.plan = dict(PLAN, tracks=[
            {"title": "A", "style": "s"},
            {"title": "B", "style": "s", "duration_seconds": "not a number"},
            {"title": "C", "style": "s", "duration_seconds": None},
        ])
        _, press = self.run_press({"prompt": "a brief", "tracks": 3, "duration": 90})
        self.assertEqual([t["duration"] for t in press["tracks"]], [90, 90, 90])

    def test_a_float_duration_from_the_plan_is_accepted(self):
        self.text.plan = dict(PLAN, tracks=[{"title": "A", "style": "s",
                                             "duration_seconds": 95.6}])
        _, press = self.run_press({"prompt": "a brief", "tracks": 1, "duration": 90})
        self.assertEqual(press["tracks"][0]["duration"], 90)   # single: target wins

    def test_a_single_uses_the_target_exactly_and_ignores_the_plan(self):
        # There is nothing to vary against with one track, so the caller's
        # number is used as given.
        _, press = self.run_press({"prompt": "a brief", "tracks": 1, "duration": 45})
        self.assertEqual(press["tracks"][0]["duration"], 45)
        self.assertIn("between 45 and 45", self.text.plan_prompt)

    def test_the_duration_reaches_the_music_call(self):
        _, press = self.run_press({"prompt": "a brief", "tracks": 2, "duration": 100})
        self.assertEqual([c["audio_duration"] for c in self.music.calls],
                         [t["duration"] for t in press["tracks"]])

    def test_the_ten_minute_ceiling_cannot_be_breached(self):
        """Clamping only dmax let the `dmax <= dmin` guard push
        the window back out, planning tracks past the ceiling the clamp exists
        to enforce."""
        self.run_press({"prompt": "a brief", "tracks": 3, "duration": 1000})
        self.assertLessEqual(self.bounds()[1], 600)

    def test_a_single_is_bounded_by_the_same_ceiling_as_an_album_track(self):
        """A single took `duration` verbatim, so one track could
        ask ACE-Step for an hour while the same request with two was capped."""
        _, press = self.run_press({"prompt": "a brief", "tracks": 1, "duration": 3600})
        self.assertLessEqual(press["tracks"][0]["duration"], 600)


class TestStageOrdering(PressCase):
    """The order is the whole design: every text call, then every music call,
    then the cover — so each heavy model loads exactly once."""

    def test_all_lyrics_precede_all_music(self):
        order = []
        self.press.call_text = lambda p, n=900: (
            order.append("plan" if p.startswith("You are planning") else "lyrics")
            or (json.dumps(PLAN) if p.startswith("You are planning") else "words"))
        self.press.call_music = lambda payload: order.append("music") or [{"file": "f"}]
        self.press.call_image = lambda prompt, size: order.append("art") or {"path": "p"}
        self.run_press({"prompt": "a brief", "tracks": 3})
        self.assertEqual(order, ["plan"] + ["lyrics"] * 3 + ["music"] * 3 + ["art"])

    def test_instrumental_skips_the_lyric_stage_entirely(self):
        _, press = self.run_press({"prompt": "a brief", "tracks": 3, "instrumental": True})
        self.assertEqual(self.text.lyric_prompts, [])
        self.assertTrue(all(t["lyrics"] is None for t in press["tracks"]))
        self.assertTrue(all(c["lyrics"] == "[instrumental]" for c in self.music.calls))

    def test_art_false_skips_the_cover(self):
        _, press = self.run_press({"prompt": "a brief", "art": False})
        self.assertEqual(self.image.calls, [])
        self.assertIsNone(press["cover"])

    def test_the_cover_brief_is_nudged_towards_album_art(self):
        self.run_press({"prompt": "a brief"})
        prompt, size = self.image.calls[0]
        self.assertTrue(prompt.startswith("a frozen car park at dusk"))
        self.assertIn("album cover art, no text or lettering", prompt)
        self.assertEqual(size, "1024x1024")

    def test_a_brief_that_already_says_album_is_left_alone(self):
        self.text.plan = dict(PLAN, cover_art="an album cover of a frozen car park")
        self.run_press({"prompt": "a brief"})
        self.assertEqual(self.image.calls[0][0], "an album cover of a frozen car park")

    def test_art_size_is_passed_through(self):
        self.run_press({"prompt": "a brief", "art_size": "512x512"})
        self.assertEqual(self.image.calls[0][1], "512x512")


class TestMusicRequestDefaults(PressCase):
    def test_the_anneal_defaults_are_applied(self):
        self.run_press({"prompt": "a brief"})
        call = self.music.calls[0]
        self.assertEqual(call["quality"], "draft")
        self.assertEqual(call["audio_format"], "flac")
        self.assertEqual(call["batch_size"], 1)
        self.assertIs(call["thinking"], True)

    def test_the_caller_can_override_them(self):
        self.run_press({"prompt": "a brief", "quality": "high", "audio_format": "mp3"})
        self.assertEqual(self.music.calls[0]["quality"], "high")
        self.assertEqual(self.music.calls[0]["audio_format"], "mp3")

    def test_the_music_prompt_is_the_tracks_style_plus_the_records_voice(self):
        """Deliberate change of behaviour, not a regression.

        This asserted that the prompt was the track's style *alone*. That was
        the bug: a brief asking for a British female lead produced male vocals
        on three of four tracks, because style is defined as genre, instruments,
        mood and tempo and never mentions a singer — so each track, being its
        own generation, got whatever voice the model chose.
        """
        self.run_press({"prompt": "a brief", "tracks": 3})
        prompts = [c["prompt"] for c in self.music.calls]
        for style, prompt in zip(["post-punk", "shoegaze", "ambient"], prompts):
            self.assertTrue(prompt.startswith(style), prompt)
        # Whatever the fake planner said about the voice, every track carries
        # the same one — that is the property worth holding.
        clauses = {p.split("Lead vocal:")[-1] for p in prompts if "Lead vocal:" in p}
        self.assertLessEqual(len(clauses), 1, "the singer changed between tracks")


class TestFailureHandling(PressCase):
    def test_one_failed_track_does_not_lose_the_rest(self):
        self.press.call_music = FakeMusic(fail_on=(2,))
        _, press = self.run_press({"prompt": "a brief", "tracks": 3})
        self.assertEqual([t["state"] for t in press["tracks"]], ["done", "failed", "done"])
        self.assertEqual(press["state"], "done")
        self.assertEqual(press["stage"], "2/3 track(s) recorded")

    def test_a_music_call_returning_nothing_marks_the_track_failed(self):
        self.press.call_music = lambda payload: []
        _, press = self.run_press({"prompt": "a brief"})
        self.assertEqual(press["tracks"][0]["state"], "failed")

    def test_a_failed_cover_does_not_fail_the_press(self):
        def boom(prompt, size):
            raise RuntimeError("no image model today")
        self.press.call_image = boom
        _, press = self.run_press({"prompt": "a brief"})
        self.assertEqual(press["state"], "done")
        self.assertIn("no image model today", press["cover"]["error"])

    def test_a_failed_plan_call_fails_the_press_rather_than_raising(self):
        def boom(prompt, max_tokens=900):
            raise RuntimeError("text model unreachable")
        self.press.call_text = boom
        _, press = self.run_press({"prompt": "a brief"})
        self.assertEqual(press["state"], "failed")
        self.assertIn("text model unreachable", press["error"])

    def test_cancelling_stops_the_run_and_records_it(self):
        pid = self.store.create({"prompt": "a brief", "tracks": 3})
        self.press.cancel(pid)
        self.press.run(pid)
        self.assertEqual(self.store.get(pid)["state"], "cancelled")
        self.assertEqual(self.music.calls, [])


class TestResume(PressCase):
    def test_finished_tracks_are_not_re_recorded(self):
        _, press = self.run_press({"prompt": "a brief", "tracks": 3})
        pid = press["id"]
        tracks = press["tracks"]
        tracks[1]["state"], tracks[1]["file"] = "failed", None
        self.store.update(pid, tracks=json.dumps(tracks), state="interrupted")
        before = len(self.music.calls)

        self.press.run(pid, resume=True)
        resumed = self.store.get(pid)
        self.assertEqual(len(self.music.calls), before + 1)   # only the missing one
        self.assertEqual([t["state"] for t in resumed["tracks"]], ["done"] * 3)
        self.assertEqual(resumed["state"], "done")

    def test_an_existing_cover_is_not_repainted(self):
        _, press = self.run_press({"prompt": "a brief"})
        before = len(self.image.calls)
        self.press.run(press["id"], resume=True)
        self.assertEqual(len(self.image.calls), before)

    def test_lyrics_already_written_are_kept(self):
        _, press = self.run_press({"prompt": "a brief", "tracks": 2})
        before = len(self.text.lyric_prompts)
        self.press.run(press["id"], resume=True)
        self.assertEqual(len(self.text.lyric_prompts), before)

    def test_resume_without_a_plan_starts_over(self):
        pid = self.store.create({"prompt": "a brief", "tracks": 2})
        self.press.run(pid, resume=True)
        press = self.store.get(pid)
        self.assertEqual(press["state"], "done")
        self.assertEqual(len(press["tracks"]), 2)


class TestSweepInterrupted(PressCase):
    def test_a_press_whose_worker_died_is_marked_interrupted(self):
        pid = self.store.create({"prompt": "a brief"})
        self.store.update(pid, state="music", tracks=json.dumps(
            [{"state": "done"}, {"state": "pending"}]))
        self.assertEqual(self.press.sweep_interrupted(), 1)
        press = self.store.get(pid)
        self.assertEqual(press["state"], "interrupted")
        self.assertEqual(press["stage"], "interrupted at 'music' — 1/2 track(s) done")

    def test_terminal_presses_are_left_alone(self):
        for state in ("done", "failed", "cancelled", "interrupted"):
            pid = self.store.create({"prompt": state})
            self.store.update(pid, state=state)
        self.assertEqual(self.press.sweep_interrupted(), 0)

    def test_every_non_terminal_state_is_swept(self):
        for state in ("planning", "lyrics", "music", "art"):
            pid = self.store.create({"prompt": state})
            self.store.update(pid, state=state)
        self.assertEqual(self.press.sweep_interrupted(), 4)


class TestManifest(PressCase):
    def test_a_tracklist_is_written_beside_the_audio(self):
        _, press = self.run_press({"prompt": "a brief", "tracks": 2, "duration": 100})
        albums = os.path.join(self.dir, "outputs", "albums")
        folders = os.listdir(albums)
        self.assertEqual(len(folders), 1)
        self.assertTrue(folders[0].endswith("_concrete-season"))
        with open(os.path.join(albums, folders[0], "tracklist.json")) as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["press_id"], press["id"])
        self.assertEqual(manifest["title"], "Concrete Season")
        self.assertEqual(manifest["artist"], "The Grey Estate")
        self.assertEqual(manifest["brief"], "a brief")
        self.assertEqual(len(manifest["tracks"]), 2)
        self.assertEqual(manifest["total_seconds"],
                         sum(t["duration"] for t in press["tracks"]))

    def test_a_readable_tracklist_is_written_too(self):
        self.run_press({"prompt": "a brief", "tracks": 2})
        albums = os.path.join(self.dir, "outputs", "albums")
        folder = os.path.join(albums, os.listdir(albums)[0])
        self.assertIn("tracklist.txt", os.listdir(folder))


class TestPressStore(PressCase):
    def test_create_returns_a_short_opaque_id(self):
        pid = self.store.create({"prompt": "a brief"})
        self.assertEqual(len(pid), 12)
        self.assertNotIn(pid, ("", None))

    def test_a_new_press_starts_queued_for_planning(self):
        press = self.store.get(self.store.create({"prompt": "a brief"}))
        self.assertEqual(press["state"], "planning")
        self.assertEqual(press["stage"], "queued")
        self.assertEqual(press["tracks"], [])
        self.assertIsNone(press["plan"])

    def test_get_of_an_unknown_id_is_none(self):
        self.assertIsNone(self.store.get("does-not-exist"))

    def test_recent_is_newest_first_and_limited(self):
        pids = [self.store.create({"prompt": "n%d" % n}) for n in range(5)]
        recent = self.store.recent(limit=3)
        self.assertEqual([p["id"] for p in recent], list(reversed(pids))[:3])

    def test_delete_removes_it(self):
        pid = self.store.create({"prompt": "a brief"})
        self.store.delete(pid)
        self.assertIsNone(self.store.get(pid))

    def test_corrupt_json_columns_read_as_defaults_rather_than_raising(self):
        pid = self.store.create({"prompt": "a brief"})
        self.store._exec("UPDATE presses SET request = 'x', tracks = 'y', plan = 'z'"
                         " WHERE id = ?", (pid,))
        press = self.store.get(pid)
        self.assertEqual(press["request"], {})
        self.assertEqual(press["tracks"], [])
        self.assertIsNone(press["plan"])

    def test_update_with_no_fields_is_a_no_op(self):
        pid = self.store.create({"prompt": "a brief"})
        before = self.store.get(pid)["updated"]
        self.store.update(pid)
        self.assertEqual(self.store.get(pid)["updated"], before)


class TestPromptTemplates(unittest.TestCase):
    def test_the_plan_prompt_asks_for_the_fields_the_parser_reads(self):
        for field in ("title", "artist", "concept", "cover_art", "tracks",
                      "duration_seconds", "theme", "style"):
            self.assertIn(field, builder.PLAN_PROMPT)

    def test_the_lyric_prompt_asks_for_the_section_tags_ace_step_wants(self):
        for tag in ("[verse]", "[chorus]", "[bridge]"):
            self.assertIn(tag, builder.LYRIC_PROMPT)

    def test_both_templates_format_with_the_keys_the_caller_supplies(self):
        builder.PLAN_PROMPT.format(what="a single song", prompt="p", count=1,
                                   dmin=60, dmax=90)
        builder.LYRIC_PROMPT.format(album="a", concept="c", title="t",
                                    theme="th", style="s",
                                    density=builder.LYRIC_DENSITY["moderate"],
                                    ending=builder.LYRIC_ENDING)


if __name__ == "__main__":
    unittest.main()
