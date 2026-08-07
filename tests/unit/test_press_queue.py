"""At most one press runs; the rest wait their turn (#14).

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
    """One record, one singer (#28 follow-up).

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

if __name__ == "__main__":
    unittest.main()
