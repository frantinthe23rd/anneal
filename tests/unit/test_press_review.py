#!/usr/bin/env python3
"""Stop after the plan and lyrics, let them be edited, then carry on (#22).

Press is one-shot: a brief goes in and twenty minutes later a record comes out.
If the tracklist is wrong or a lyric is weak, the whole run is wasted — and on
this hardware that is a real cost, not a rounding error. The expensive stage is
music; the cheap stages that decide what the music will be are planning and
lyrics, and both finish in under a minute.

So `review: true` parks the press between them. The plan and the lyrics can be
amended, then it resumes into the music stage with whatever the human left
behind. One-shot stays the default.

The constraint that shapes everything here: **a press waiting for a human must
not hold the heavy slot.** #14 made presses queue because only one can own the
model ordering; a press paused at review for an hour while its author has lunch
would block every other press behind it.
"""

import os
import shutil
import tempfile
import unittest

from tests.context import REPO_ROOT  # noqa: F401

import builder


class ReviewCase(unittest.TestCase):
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
        self.press.spawn = lambda pid, resume=False: self.started.append(pid)

    def submit(self, **kw):
        req = {"prompt": "a brief", "tracks": 2}
        req.update(kw)
        return self.press.submit(req)


class TestTheWaitingStateReleasesTheSlot(ReviewCase):
    """The part that would break #14 if it were wrong."""

    def test_awaiting_review_is_not_a_running_state(self):
        self.assertNotIn("awaiting-review", builder.Press.RUNNING_STATES)

    def test_it_is_not_terminal_either(self):
        """It is not finished — resuming must still be possible, and a sweep
        must not mistake it for a press that died."""
        self.assertNotIn("awaiting-review", builder.Press.TERMINAL_STATES)

    def test_a_press_parked_for_review_lets_the_next_one_start(self):
        first, second = self.submit(review=True), self.submit()
        self.store.update(first, state="awaiting-review")
        self.press.start_next()
        self.assertIn(second, self.started,
                      "a press waiting on a human must not block the queue")

    def test_a_parked_press_is_not_swept_as_interrupted(self):
        """sweep_interrupted() reconciles records that claim to be working with
        nothing behind them. A press waiting for review has nothing behind it
        by design, and marking it interrupted would lose the review."""
        pid = self.submit(review=True)
        self.store.update(pid, state="awaiting-review")
        self.press.sweep_interrupted()
        self.assertEqual(self.store.get(pid)["state"], "awaiting-review")


class TestAmending(ReviewCase):
    def setUp(self):
        ReviewCase.setUp(self)
        self.pid = self.submit(review=True)
        self.store.update(
            self.pid, state="awaiting-review",
            plan=builder.json.dumps({"title": "Old Title", "artist": "Old Band",
                                     "concept": "old", "voice": "a tenor"}),
            tracks=builder.json.dumps([
                {"n": 1, "title": "One", "lyrics": "old words", "state": "lyrics-done"},
                {"n": 2, "title": "Two", "lyrics": "more old words", "state": "lyrics-done"}]))

    def test_the_title_and_artist_can_be_changed(self):
        self.press.amend(self.pid, {"title": "New Title", "artist": "New Band"}, None)
        plan = self.store.get(self.pid)["plan"]
        self.assertEqual(plan["title"], "New Title")
        self.assertEqual(plan["artist"], "New Band")

    def test_fields_not_mentioned_are_left_alone(self):
        """An amendment is a patch, not a replacement. Sending only a title
        must not blank the voice that the vocal-consistency work depends on."""
        self.press.amend(self.pid, {"title": "New Title"}, None)
        self.assertEqual(self.store.get(self.pid)["plan"]["voice"], "a tenor")

    def test_lyrics_can_be_rewritten_per_track(self):
        self.press.amend(self.pid, None, [{"n": 2, "lyrics": "brand new words"}])
        tracks = self.store.get(self.pid)["tracks"]
        self.assertEqual(tracks[1]["lyrics"], "brand new words")
        self.assertEqual(tracks[0]["lyrics"], "old words", "track 1 was not mentioned")

    def test_a_track_title_can_be_changed_without_touching_its_lyrics(self):
        self.press.amend(self.pid, None, [{"n": 1, "title": "Renamed"}])
        tracks = self.store.get(self.pid)["tracks"]
        self.assertEqual(tracks[0]["title"], "Renamed")
        self.assertEqual(tracks[0]["lyrics"], "old words")

    def test_an_unknown_track_number_is_ignored_rather_than_appended(self):
        """Otherwise a typo silently adds a track nobody asked for, and the
        music stage records it."""
        self.press.amend(self.pid, None, [{"n": 99, "lyrics": "ghost"}])
        self.assertEqual(len(self.store.get(self.pid)["tracks"]), 2)

    def test_amending_a_press_that_is_not_awaiting_review_is_refused(self):
        """Editing the plan of a press already recording would silently
        disagree with the audio being produced from it."""
        self.store.update(self.pid, state="music")
        with self.assertRaises(ValueError):
            self.press.amend(self.pid, {"title": "Too late"}, None)


class TestResuming(ReviewCase):
    def test_approving_starts_the_press_again(self):
        pid = self.submit(review=True)
        self.store.update(pid, state="awaiting-review")
        self.started.clear()
        self.press.approve(pid)
        self.assertIn(pid, self.started)

    def test_approving_queues_behind_a_running_press(self):
        """It has to re-enter the queue like anything else — it gave up the
        slot when it parked, and something else may hold it now."""
        running = self.submit()
        parked = self.submit(review=True)
        self.store.update(parked, state="awaiting-review")
        self.started.clear()
        self.press.approve(parked)
        self.assertNotIn(parked, self.started)
        self.assertEqual(self.store.get(parked)["state"], "queued")

    def test_approving_something_not_awaiting_review_is_refused(self):
        pid = self.submit()
        with self.assertRaises(ValueError):
            self.press.approve(pid)


class TestTheRequestFlag(ReviewCase):
    def test_review_is_off_by_default(self):
        """One-shot stays the default; #22 asked for both, not a replacement."""
        self.assertFalse(self.press.wants_review({"prompt": "x"}))

    def test_review_is_on_when_asked_for(self):
        self.assertTrue(self.press.wants_review({"prompt": "x", "review": True}))

    def test_an_instrumental_press_still_reviews_the_tracklist(self):
        """There are no lyrics to check, but the titles, the artist and the
        per-track styles are still worth seeing before twenty minutes of music."""
        self.assertTrue(self.press.wants_review(
            {"prompt": "x", "review": True, "instrumental": True}))


class TestTheEndpointContract(unittest.TestCase):
    """`POST /v1/press/review` — written before the handler, as endpoints here
    are. Three have shipped undocumented in this repo by skipping this."""

    def source(self):
        with open(os.path.join(REPO_ROOT, "supervisor.py"), encoding="utf-8") as fh:
            return fh.read()

    def test_the_route_exists_and_is_the_gateways_own(self):
        import services
        self.assertIn("/v1/press/review", self.source())
        self.assertIsNone(services.resolve("/v1/press/review"),
                          "it must never be proxied to a backend")

    def test_it_is_declared_as_a_gateway_route(self):
        import services
        self.assertTrue(any(r == "/v1/press/review" for r in services.GATEWAY_ROUTES))

    def test_it_is_in_the_spec(self):
        """`/v1/press/cancel` shipped and was documented nowhere. Not again."""
        import json
        with open(os.path.join(REPO_ROOT, "openapi.json"), encoding="utf-8") as fh:
            spec = json.load(fh)
        self.assertIn("/v1/press/review", spec["paths"])
        post = spec["paths"]["/v1/press/review"]["post"]
        props = post["requestBody"]["content"]["application/json"]["schema"]["properties"]
        for field in ("id", "plan", "tracks", "approve"):
            self.assertIn(field, props, field)
        for code in ("200", "400", "401", "404", "409"):
            self.assertIn(code, post["responses"], code)

    def test_the_review_flag_is_in_the_press_spec(self):
        import json
        with open(os.path.join(REPO_ROOT, "openapi.json"), encoding="utf-8") as fh:
            spec = json.load(fh)
        props = (spec["paths"]["/v1/press"]["post"]["requestBody"]["content"]
                 ["application/json"]["schema"]["properties"])
        self.assertIn("review", props)

    def test_it_is_in_the_integration_guide(self):
        with open(os.path.join(REPO_ROOT, "INTEGRATION.md"), encoding="utf-8") as fh:
            guide = fh.read()
        self.assertIn("/v1/press/review", guide)


if __name__ == "__main__":
    unittest.main()
