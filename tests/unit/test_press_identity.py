#!/usr/bin/env python3
"""Naming a record, or being offered names, before the expensive part (#35).

Press invents a title and an artist and you find out what they are twenty
minutes later, alongside the audio. Two things follow from that, and both are
cheap because the planner already produces them in under a minute:

  * If you already know what the record is called, say so and have it honoured
    rather than overwritten.
  * If you do not, ask for a few suggestions first. That is one text call, and
    it costs nothing next to the music.

A supplied name must survive the planner, and the planner's own answer must
survive a partial override — asking for a title should not blank the artist.
"""

import os
import shutil
import tempfile
import unittest

from tests.context import REPO_ROOT  # noqa: F401

import builder


class IdentityCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = builder.PressStore(os.path.join(self.tmp, "presses.db"))
        self.press = builder.Press(
            self.store,
            call_text=lambda p, n=900: "{}",
            call_music=lambda payload: [],
            call_image=lambda p, size: {},
            log=lambda *a: None,
        )
        self.press.spawn = lambda pid, resume=False: None


class TestSuppliedNamesWin(IdentityCase):
    """The planner is a fallback for the parts you did not name."""

    def apply(self, plan, request):
        return builder.Press.apply_identity(plan, request)

    def test_a_supplied_title_replaces_the_invented_one(self):
        plan = self.apply({"title": "Invented", "artist": "Some Band"},
                          {"title": "Winter Roads"})
        self.assertEqual(plan["title"], "Winter Roads")

    def test_a_supplied_artist_replaces_the_invented_one(self):
        plan = self.apply({"title": "Invented", "artist": "Some Band"},
                          {"artist": "The Salt Line"})
        self.assertEqual(plan["artist"], "The Salt Line")

    def test_naming_one_leaves_the_other_alone(self):
        """A partial override is a patch, not a reset — the same rule the
        review endpoint follows."""
        plan = self.apply({"title": "Invented", "artist": "Some Band"},
                          {"title": "Winter Roads"})
        self.assertEqual(plan["artist"], "Some Band")

    def test_naming_neither_keeps_the_plan(self):
        plan = self.apply({"title": "Invented", "artist": "Some Band"}, {})
        self.assertEqual((plan["title"], plan["artist"]), ("Invented", "Some Band"))

    def test_blank_and_whitespace_are_not_a_name(self):
        """An empty field in a form must not blank what the planner chose."""
        for value in ("", "   ", None):
            plan = self.apply({"title": "Invented", "artist": "Some Band"},
                              {"title": value, "artist": value})
            self.assertEqual(plan["title"], "Invented")
            self.assertEqual(plan["artist"], "Some Band")

    def test_names_are_trimmed(self):
        plan = self.apply({}, {"title": "  Winter Roads  ", "artist": " The Salt Line "})
        self.assertEqual(plan["title"], "Winter Roads")
        self.assertEqual(plan["artist"], "The Salt Line")

    def test_a_supplied_name_survives_an_empty_plan(self):
        """The 0.6B planner returns nothing usable often enough to matter."""
        plan = self.apply({}, {"title": "Winter Roads", "artist": "The Salt Line"})
        self.assertEqual(plan["title"], "Winter Roads")
        self.assertEqual(plan["artist"], "The Salt Line")

    def test_an_over_long_name_is_cut_rather_than_refused(self):
        """It reaches a filename and a cover prompt; losing the whole press to a
        pasted paragraph would be the worse failure."""
        plan = self.apply({}, {"title": "x" * 500})
        self.assertLessEqual(len(plan["title"]), builder.MAX_NAME_CHARS)


class TestSuggesting(IdentityCase):
    """One text call before anything expensive starts."""

    def suggest(self, reply, n=5):
        self.press.call_text = lambda p, tokens=900: reply
        return self.press.suggest_names({"prompt": "a winter album"}, count=n)

    def test_it_returns_pairs_of_title_and_artist(self):
        got = self.suggest('{"names": [{"title": "Winter Roads", "artist": "The Salt Line"},'
                           ' {"title": "Low Tide", "artist": "Harbour Lights"}]}')
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["title"], "Winter Roads")
        self.assertEqual(got[0]["artist"], "The Salt Line")

    def test_it_asks_for_the_number_requested(self):
        seen = {}
        self.press.call_text = lambda p, tokens=900: seen.setdefault("p", p) or '{"names": []}'
        self.press.suggest_names({"prompt": "x"}, count=7)
        self.assertIn("7", seen["p"])

    def test_the_brief_reaches_the_prompt(self):
        seen = {}
        self.press.call_text = lambda p, tokens=900: seen.setdefault("p", p) or '{"names": []}'
        self.press.suggest_names({"prompt": "a winter album about leaving"}, count=3)
        self.assertIn("a winter album about leaving", seen["p"])

    def test_junk_from_the_model_yields_nothing_rather_than_raising(self):
        """This runs before a press exists; a formatting lapse must not be an
        error the caller has to handle."""
        self.assertEqual(self.suggest("sorry, I cannot do that"), [])

    def test_entries_missing_a_field_are_dropped(self):
        got = self.suggest('{"names": [{"title": "Only A Title"},'
                           ' {"title": "Good", "artist": "Also Good"}]}')
        self.assertEqual([n["title"] for n in got], ["Good"])

    def test_more_than_asked_for_is_truncated(self):
        got = self.suggest('{"names": [%s]}' % ",".join(
            '{"title": "T%d", "artist": "A%d"}' % (i, i) for i in range(20)), n=3)
        self.assertEqual(len(got), 3)

    def test_the_count_is_bounded(self):
        """It is cheap, not free, and the field is caller-supplied."""
        self.press.call_text = lambda p, tokens=900: '{"names": []}'
        self.press.suggest_names({"prompt": "x"}, count=999)   # must not raise


class TestTheEndpointContract(unittest.TestCase):
    """`POST /v1/press/names` — written before the handler."""

    def source(self, name):
        with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as fh:
            return fh.read()

    def test_the_route_is_the_gateways_own(self):
        import services
        self.assertIn("/v1/press/names", self.source("supervisor.py"))
        self.assertIsNone(services.resolve("/v1/press/names"))
        self.assertIn("/v1/press/names", services.GATEWAY_ROUTES)

    def test_it_is_in_the_spec_with_its_failure_modes(self):
        import json
        spec = json.loads(self.source("openapi.json"))
        self.assertIn("/v1/press/names", spec["paths"])
        post = spec["paths"]["/v1/press/names"]["post"]
        props = post["requestBody"]["content"]["application/json"]["schema"]["properties"]
        self.assertIn("prompt", props)
        self.assertIn("count", props)
        for code in ("200", "400", "401"):
            self.assertIn(code, post["responses"])

    def test_title_and_artist_are_in_the_press_schema(self):
        """They were accepted by the code and absent from the spec on three
        previous endpoints; a generated client could not send them."""
        import json
        spec = json.loads(self.source("openapi.json"))
        props = (spec["paths"]["/v1/press"]["post"]["requestBody"]["content"]
                 ["application/json"]["schema"]["properties"])
        self.assertIn("title", props)
        self.assertIn("artist", props)

    def test_it_is_in_the_integration_guide(self):
        self.assertIn("/v1/press/names", self.source("INTEGRATION.md"))

    def test_suggesting_needs_no_heavy_model(self):
        """The whole point is that it is cheap enough to run before committing.
        It must route to text, not wake music or image."""
        block = self.source("supervisor.py")
        i = block.index('if route == "/v1/press/names"')
        self.assertNotIn("start_service(\"music\"", block[i:i + 1500])


if __name__ == "__main__":
    unittest.main()
