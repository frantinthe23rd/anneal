"""At most one press runs; the rest wait their turn.

Press's whole design is that every text stage runs, then every music stage,
then the cover — so each heavy model loads exactly once. Two presses at a time
interleave those stages and force a model swap between them repeatedly, turning
a twenty-minute album into hours. They would also fight over the single heavy
slot, which `start_service` refuses with a 409 that a press has no way to act on.

Refusing the second submission was the cheaper fix and is worse than doing
nothing: you lose the brief you just typed. So they queue.
"""
import os
import shutil
import tempfile
import unittest

from tests.context import REPO_ROOT  # noqa: F401  (sandboxes the environment)

import builder


class QueueCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = builder.PressStore(os.path.join(self.tmp, "presses.db"))
        self.started = []
        self.press = builder.Press(
            self.store,
            call_text=lambda p, n=900: "{}",
            call_music=lambda payload: [],
            call_image=lambda p, size: {},
            log=lambda *a: None,
        )
        # Stand in for the thread the gateway would spawn, so ordering is
        # observable without running a real press.
        self.press.spawn = lambda pid, resume=False: self.started.append(pid)

    def submit(self, prompt="a brief"):
        return self.press.submit({"prompt": prompt, "tracks": 1})


class TestAdmission(QueueCase):
    def test_the_first_press_starts_immediately(self):
        pid = self.submit()
        self.assertEqual(self.started, [pid])
        self.assertNotEqual(self.store.get(pid)["state"], "queued")

    def test_a_second_press_is_queued_rather_than_started(self):
        first = self.submit()
        second = self.submit()
        self.assertEqual(self.started, [first], "only one worker should exist")
        self.assertEqual(self.store.get(second)["state"], "queued")

    def test_a_queued_press_keeps_its_brief(self):
        """Refusing would lose it, which is the whole argument for queueing."""
        self.submit()
        second = self.press.submit({"prompt": "a winter album", "tracks": 3})
        req = self.store.get(second)["request"]
        self.assertEqual(req["prompt"], "a winter album")
        self.assertEqual(req["tracks"], 3)

    def test_position_is_reported_and_counts_from_one(self):
        self.submit()
        second, third = self.submit(), self.submit()
        self.assertEqual(self.press.position(second), 1)
        self.assertEqual(self.press.position(third), 2)

    def test_a_running_press_has_no_position(self):
        first = self.submit()
        self.assertIsNone(self.press.position(first))


class TestHandover(QueueCase):
    def test_finishing_starts_the_next_in_line(self):
        first = self.submit()
        second = self.submit()
        self.press.finish(first, "done")
        self.assertEqual(self.started, [first, second])
        self.assertNotEqual(self.store.get(second)["state"], "queued")

    def test_failing_also_hands_over(self):
        """A queue that only advances on success stalls on the first failure."""
        first = self.submit()
        second = self.submit()
        self.press.finish(first, "failed")
        self.assertEqual(self.started, [first, second])

    def test_they_start_in_the_order_submitted(self):
        first = self.submit()
        second, third = self.submit(), self.submit()
        self.press.finish(first, "done")
        self.press.finish(second, "done")
        self.assertEqual(self.started, [first, second, third])

    def test_an_empty_queue_starts_nothing(self):
        first = self.submit()
        self.press.finish(first, "done")
        self.assertEqual(self.started, [first])


class TestCancellation(QueueCase):
    def test_cancelling_a_queued_press_removes_it_without_starting_it(self):
        first = self.submit()
        second = self.submit()
        self.press.cancel(second)
        self.assertEqual(self.store.get(second)["state"], "cancelled")
        self.press.finish(first, "done")
        self.assertNotIn(second, self.started, "a cancelled press must not start later")

    def test_cancelling_the_running_press_lets_the_next_through(self):
        first = self.submit()
        second = self.submit()
        self.press.cancel(first)
        self.press.finish(first, "cancelled")
        self.assertEqual(self.started, [first, second])


class TestRestart(QueueCase):
    """A queue that does not survive a restart is a queue that loses work."""

    def test_a_queued_press_is_not_swept_as_interrupted(self):
        self.submit()
        second = self.submit()
        self.press.sweep_interrupted()
        self.assertEqual(self.store.get(second)["state"], "queued",
                         "it never started, so there is nothing to interrupt")

    def test_the_queue_resumes_after_a_sweep(self):
        first = self.submit()
        second = self.submit()
        self.started.clear()
        self.press.sweep_interrupted()      # first becomes interrupted
        self.assertEqual(self.store.get(first)["state"], "interrupted")
        self.press.start_next()
        self.assertEqual(self.started, [second])

class TestVoiceConsistency(QueueCase):
    """One record, one singer.

    A brief asking for a British female lead came back with male vocals on
    three of four tracks. The music prompt was the planner's per-track `style`
    alone, which is defined as genre, instruments, mood and tempo — it says
    nothing about who is singing, and each track is a separate generation, so
    the model chose a voice per track.
    """

    def prompt(self, plan, track, req):
        return builder.Press.track_prompt(plan, track, req)

    def test_the_voice_is_appended_to_every_track(self):
        plan = {"voice": "a British woman, warm alto"}
        for style in ("melancholy folk", "uptempo indie rock", "sparse piano ballad"):
            out = self.prompt(plan, {"style": style}, {"prompt": "a brief"})
            self.assertIn("a British woman, warm alto", out, style)
            self.assertIn(style, out)

    def test_the_brief_carries_through_when_the_planner_omits_a_voice(self):
        """The 0.6B planner does not always answer the schema. Dropping the
        only statement of intent there is would be the worst response to that."""
        out = self.prompt({}, {"style": "folk"}, {"prompt": "british female led vocals"})
        self.assertIn("british female led vocals", out)

    def test_an_instrumental_record_gets_no_vocal_clause(self):
        self.assertNotIn("Lead vocal",
                         self.prompt({"voice": "instrumental"}, {"style": "ambient"},
                                     {"prompt": "x"}))
        self.assertNotIn("Lead vocal",
                         self.prompt({"voice": "a tenor"}, {"style": "ambient"},
                                     {"prompt": "x", "instrumental": True}))

    def test_a_track_without_a_style_falls_back_to_the_brief(self):
        out = self.prompt({"voice": "a tenor"}, {}, {"prompt": "a winter album"})
        self.assertIn("a winter album", out)

class TestLyricDensity(QueueCase):
    """How many words a genre wants.

    An electronic record came back with full verse-chorus-verse lyrics on every
    track. That is not what the style does: house, techno and most club music
    carry a hook and a handful of lines, and a wall of text sung over them
    sounds wrong in a way no amount of good writing fixes. The lyric prompt said
    "two verses and a chorus is plenty" to every genre alike.

    Density is therefore chosen per track. The planner is asked for it, and
    where it does not answer — the 0.6B model frequently does not, which is why
    the voice fix has a fallback too — it is derived from the track's own style
    line rather than defaulting to the densest option.
    """

    def density(self, track, request=None):
        return builder.Press.lyric_density(track, request or {})

    def test_club_genres_are_sparse(self):
        for style in ("deep house, warm pads, 122 bpm", "driving techno, hypnotic",
                      "drum and bass, rolling breaks", "melodic trance, euphoric",
                      "ambient electronic, beatless"):
            self.assertEqual(self.density({"style": style}), "sparse", style)

    def test_wordy_genres_stay_full(self):
        for style in ("acoustic folk ballad, fingerpicked guitar",
                      "boom bap hip hop, dusty samples",
                      "singer-songwriter, piano and voice"):
            self.assertEqual(self.density({"style": style}), "full", style)

    def test_ordinary_song_forms_sit_in_between(self):
        for style in ("indie rock, jangly guitars", "synth pop, bright chorus"):
            self.assertEqual(self.density({"style": style}), "moderate", style)

    def test_lofi_hip_hop_is_sparse_despite_the_words_hip_hop(self):
        """The trap in matching on genre words: lo-fi hip hop is a beat with an
        occasional vocal chop, not a rap record."""
        self.assertEqual(self.density({"style": "lo-fi hip hop, dusty, mellow"}), "sparse")

    def test_the_planner_can_say_so_explicitly(self):
        self.assertEqual(self.density({"style": "indie rock", "lyric_density": "sparse"}),
                         "sparse")

    def test_a_nonsense_value_from_the_planner_is_ignored_rather_than_trusted(self):
        self.assertEqual(self.density({"style": "deep house", "lyric_density": "banana"}),
                         "sparse")

    def test_an_unrecognisable_style_does_not_default_to_a_wall_of_words(self):
        self.assertEqual(self.density({"style": "something nobody has named yet"}),
                         "moderate")

    def test_arrangement_words_are_not_mistaken_for_genre(self):
        """Measured on a real folk record: a track described as "Minimalist
        arrangement, sparse guitar, high violin" was given club-music lyric
        density, because "minimal" is both a techno subgenre and the most
        ordinary word in English for a quiet arrangement."""
        self.assertEqual(self.density(
            {"style": "Minimalist arrangement, sparse guitar, high violin melody"},
            {"prompt": "an album in the folk pop genre"}), "full")

    def test_the_brief_supplies_the_genre_when_a_track_style_names_none(self):
        """Track styles routinely describe instruments and mood without ever
        saying what the music is. The brief usually does say."""
        self.assertEqual(self.density({"style": "builds slowly, warm and close"},
                                      {"prompt": "a deep house record"}), "sparse")
        self.assertEqual(self.density({"style": "builds slowly, warm and close"},
                                      {"prompt": "an acoustic folk album"}), "full")

    def test_the_track_style_still_wins_over_the_brief(self):
        """A record can have an outlier, and the more specific line is the
        better evidence when it does say something."""
        self.assertEqual(self.density({"style": "a techno interlude"},
                                      {"prompt": "an acoustic folk album"}), "sparse")

    def test_the_brief_can_override_every_track(self):
        """If someone asks for sparse lyrics, that is the answer for the record,
        not a per-track guess."""
        self.assertEqual(self.density({"style": "acoustic folk"},
                                      {"lyric_density": "sparse"}), "sparse")


class TestEndings(QueueCase):
    """Tracks stopped mid-phrase (#33).

    Measured over fourteen finished tracks: none were truncated at the sample
    level — every file fades to digital silence. What actually happens is worse
    and less obvious. The music plays at full level right up to the last bar,
    stops dead, and the file is then padded with two to six seconds of silence
    to reach the requested duration. Five of eight recent tracks did exactly
    that. So the model fills the time it was given, runs out of material, and
    ends nowhere in particular.

    Nothing in the prompt ever asked for an ending. `style` is defined as genre,
    instruments, mood and tempo; the lyric prompt asks for verses and a chorus.
    Neither says the piece has to finish.
    """

    def prompt(self, plan, track, req):
        return builder.Press.track_prompt(plan, track, req)

    def test_a_press_asks_for_an_ending_by_default(self):
        """A record should finish. It is the default for Press specifically."""
        for style in ("melancholy folk", "driving melodic techno", "indie rock"):
            out = self.prompt({"voice": "a tenor"}, {"style": style},
                              {"prompt": "a brief"}).lower()
            self.assertTrue("outro" in out or "resolve" in out,
                            "nothing asks %r to finish" % style)

    def test_an_instrumental_track_still_asks_for_an_ending(self):
        """The ending is a property of the arrangement, not the vocal — and
        instrumental club tracks were the worst offenders in the measurement."""
        out = self.prompt({}, {"style": "ambient techno"},
                          {"prompt": "x", "instrumental": True}).lower()
        self.assertTrue("outro" in out or "resolve" in out)

    def test_it_can_be_turned_off(self):
        """Not every record wants a resolved ending — and nobody should have to
        type the clause by hand to get one, or delete it to avoid one."""
        out = self.prompt({}, {"style": "ambient techno"},
                          {"prompt": "x", "outro": False}).lower()
        self.assertNotIn("outro", out)
        self.assertNotIn("resolve", out)

    def test_turning_it_off_keeps_everything_else(self):
        out = self.prompt({"voice": "a British woman, warm alto"},
                          {"style": "folk"}, {"prompt": "x", "outro": False})
        self.assertIn("a British woman, warm alto", out)
        self.assertIn("folk", out)

    def test_the_raw_music_endpoint_is_not_given_an_outro(self):
        """A builder asking /release_task for a two-bar loop wants it to loop.
        An outro clause welded onto every music request would ruin exactly the
        use this API exists for, so the default lives in Press and nowhere else.
        """
        src = open(os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8").read()
        self.assertNotIn("ENDING_CLAUSE", src,
                         "the outro belongs to Press, not to the music endpoint")

    def test_the_ending_clause_does_not_displace_the_voice(self):
        """Both are appended to the same line. The vocal fix came first and must
        survive this one."""
        out = self.prompt({"voice": "a British woman, warm alto"},
                          {"style": "folk"}, {"prompt": "x"})
        self.assertIn("a British woman, warm alto", out)
        self.assertIn("folk", out)

    def test_the_style_still_leads_the_prompt(self):
        """ACE-Step weights the front of the prompt most. Genre must not end up
        behind housekeeping."""
        out = self.prompt({}, {"style": "driving melodic techno"}, {"prompt": "x"})
        self.assertTrue(out.lower().startswith("driving melodic techno"), out[:60])

    def test_the_lyric_prompt_asks_for_a_closing_section_when_wanted(self):
        self.assertIn("[outro]", builder.LYRIC_ENDING)
        self.assertIn("{ending}", builder.LYRIC_PROMPT)


class TestLyricInstruction(QueueCase):
    def test_each_density_produces_different_guidance(self):
        seen = {builder.LYRIC_DENSITY[d] for d in ("sparse", "moderate", "full")}
        self.assertEqual(len(seen), 3)

    def test_the_sparse_instruction_actually_asks_for_fewer_words(self):
        text = builder.LYRIC_DENSITY["sparse"].lower()
        self.assertTrue("repeat" in text or "few" in text or "short" in text,
                        "sparse guidance has to say something concrete")

    def test_every_call_site_fills_every_placeholder(self):
        """str.format raises on a missing key, and the resume path is the one
        nobody exercises by hand — a press only takes it after a restart mid-run.
        Adding {density} to the prompt broke it silently until this."""
        import re
        src = open(os.path.join(REPO_ROOT, "builder.py"), encoding="utf-8").read()
        needed = set(re.findall(r"\{(\w+)\}", builder.LYRIC_PROMPT))
        calls = re.findall(r"LYRIC_PROMPT\.format\((.*?)\), \d+\)", src, re.S)
        self.assertTrue(calls, "the prompt is formatted somewhere")
        for call in calls:
            supplied = set(re.findall(r"(\w+)=", call))
            self.assertFalse(needed - supplied,
                             "missing %s in: %s" % (needed - supplied, call[:120]))

    def test_the_density_reaches_the_prompt(self):
        filled = builder.LYRIC_PROMPT.format(
            album="A", concept="B", title="C", theme="D", style="deep house",
            density=builder.LYRIC_DENSITY["sparse"], ending=builder.LYRIC_ENDING)
        self.assertIn(builder.LYRIC_DENSITY["sparse"], filled)


if __name__ == "__main__":
    unittest.main()
