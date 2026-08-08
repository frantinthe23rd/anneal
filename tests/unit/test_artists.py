#!/usr/bin/env python3
"""Keep an artist and make more for them (#35).

Press invents a title, an artist, a voice and a lyric density, and throws all of
it away when the record finishes. "Another record from this artist" — same name,
same singer, same register — needs no model work: it is already in `presses.db`,
just not addressable.

Derived from the presses rather than stored in a second table. The plan is
already the source of truth for what an artist is; a parallel `artists` table
would be a second copy to keep in step, and this repo has been bitten five times
by exactly that. Deriving also means it works on records that already exist.
"""

import json
import os
import shutil
import tempfile
import unittest

from tests.context import REPO_ROOT  # noqa: F401

import builder


class ArtistCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = builder.PressStore(os.path.join(self.tmp, "presses.db"))

    def press(self, artist, title="A Record", voice="a British woman, warm alto",
              density=None, state="done", cover="/tmp/c.png", styles=("folk",),
              prompt="a brief"):
        pid = self.store.create({"prompt": prompt,
                                 **({"lyric_density": density} if density else {})})
        plan = {"title": title, "artist": artist, "voice": voice,
                "concept": "a through-line", "cover_art": "a cold shore"}
        self.store.update(pid, state=state, plan=json.dumps(plan),
                          cover=json.dumps({"path": cover}),
                          tracks=json.dumps([{"n": 1, "title": "One", "style": s,
                                              "state": "done"} for s in styles]))
        return pid


class TestDerivingArtists(ArtistCase):
    def test_a_finished_press_yields_its_artist(self):
        self.press("The Salt Line")
        got = builder.artists(self.store)
        self.assertEqual([a["name"] for a in got], ["The Salt Line"])

    def test_the_voice_is_carried(self):
        self.press("The Salt Line", voice="a British woman, warm alto")
        self.assertEqual(builder.artists(self.store)[0]["voice"],
                         "a British woman, warm alto")

    def test_two_records_by_one_artist_are_one_entry(self):
        self.press("The Salt Line", title="First")
        self.press("The Salt Line", title="Second")
        got = builder.artists(self.store)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["records"], 2)

    def test_the_most_recent_record_supplies_the_details(self):
        """An artist can drift across records; the latest is the current one."""
        self.press("The Salt Line", voice="an old voice")
        self.press("The Salt Line", voice="a newer voice")
        self.assertEqual(builder.artists(self.store)[0]["voice"], "a newer voice")

    def test_an_unfinished_press_is_not_an_artist_yet(self):
        """Half a record is not a body of work, and a failed one is not either."""
        self.press("Ghost Band", state="planning")
        self.press("Failed Band", state="failed")
        self.assertEqual(builder.artists(self.store), [])

    def test_the_planner_placeholder_is_not_an_artist(self):
        self.press("Unknown Artist")
        self.assertEqual(builder.artists(self.store), [])

    def test_an_instrumental_record_yields_an_artist_without_a_voice(self):
        self.press("Slow Static", voice="instrumental")
        got = builder.artists(self.store)
        self.assertEqual(len(got), 1)
        self.assertIsNone(got[0]["voice"])

    def test_a_cover_is_offered_so_the_list_is_not_all_text(self):
        self.press("The Salt Line", cover="/tmp/cover.png")
        self.assertEqual(builder.artists(self.store)[0]["cover"], "/tmp/cover.png")

    def test_styles_are_collected_across_the_records(self):
        self.press("The Salt Line", styles=("acoustic folk", "folk ballad"))
        self.assertIn("acoustic folk", builder.artists(self.store)[0]["styles"])

    def test_artists_are_newest_first(self):
        self.press("Older")
        self.press("Newer")
        self.assertEqual([a["name"] for a in builder.artists(self.store)],
                         ["Newer", "Older"])


class TestTheRegisterIsRecovered(ArtistCase):
    """Almost no record pins `lyric_density` — it is read off the brief at
    generation time. Reporting only what was pinned made every existing artist
    come back `null`, which is the field being useless in exactly the case it
    exists for."""

    def test_an_explicit_density_is_reported(self):
        self.press("The Salt Line", density="full")
        self.assertEqual(builder.artists(self.store)[0]["lyric_density"], "full")

    def test_the_brief_supplies_it_when_nothing_pinned_it(self):
        self.press("Nova Drift", prompt="a melodic techno record for late sets")
        self.assertEqual(builder.artists(self.store)[0]["lyric_density"], "sparse")

    def test_an_explicit_density_beats_the_brief(self):
        self.press("Nova Drift", prompt="a melodic techno record", density="full")
        self.assertEqual(builder.artists(self.store)[0]["lyric_density"], "full")

    def test_a_brief_naming_no_genre_reports_nothing(self):
        # lyric_density() has to answer, so it falls back to "moderate". Here
        # that fallback would be pinned onto every later record and stop its own
        # track styles being read, so it must stay unknown instead.
        self.press("The Chronoscrolls", prompt="something for the winter")
        self.assertIsNone(builder.artists(self.store)[0]["lyric_density"])

    def test_the_fallback_is_not_adopted(self):
        self.press("The Chronoscrolls", prompt="something for the winter")
        got = builder.adopt_artist(self.store, {"prompt": "another",
                                                "adopt_artist": "The Chronoscrolls"})
        self.assertNotIn("lyric_density", got)

    def test_one_rule_reads_the_genre(self):
        # The listing and the generator must agree about what "techno" means.
        # Two copies of the pattern table is the failure this repo keeps hitting.
        self.press("Nova Drift", prompt="a melodic techno record")
        self.assertEqual(builder.artists(self.store)[0]["lyric_density"],
                         builder.Press.lyric_density(None, {"prompt": "a melodic techno record"}))


class TestTheAdoptedVoiceReachesTheMusic(ArtistCase):
    """Adoption is only real if the voice survives into the track prompts.

    The planner writes its own `voice` into the plan, and `track_prompt` reads it
    from there. A voice on the request that never reaches the plan would look
    adopted in the record and sound like a different singer.
    """

    def test_a_requested_voice_overrides_the_planners(self):
        plan = builder.Press.apply_identity({"voice": "a tenor"},
                                            {"voice": "a British woman, warm alto"})
        self.assertEqual(plan["voice"], "a British woman, warm alto")

    def test_it_then_reaches_every_track_prompt(self):
        plan = builder.Press.apply_identity({"voice": "a tenor"},
                                            {"voice": "a British woman, warm alto"})
        out = builder.Press.track_prompt(plan, {"style": "folk"}, {"prompt": "x"})
        self.assertIn("a British woman, warm alto", out)

    def test_no_requested_voice_leaves_the_planners_alone(self):
        plan = builder.Press.apply_identity({"voice": "a tenor"}, {})
        self.assertEqual(plan["voice"], "a tenor")


class TestAdopting(ArtistCase):
    """What a new press inherits, and what still wins over it."""

    def adopt(self, request, name="The Salt Line"):
        self.press(name, voice="a British woman, warm alto", density="sparse")
        return builder.adopt_artist(self.store, dict(request))

    def test_the_name_and_voice_come_across(self):
        req = self.adopt({"prompt": "a new record", "adopt_artist": "The Salt Line"})
        self.assertEqual(req["artist"], "The Salt Line")
        self.assertEqual(req["voice"], "a British woman, warm alto")

    def test_the_lyric_density_comes_across(self):
        """A sparse club act must not come back as a folk singer."""
        req = self.adopt({"prompt": "x", "adopt_artist": "The Salt Line"})
        self.assertEqual(req["lyric_density"], "sparse")

    def test_the_title_is_not_inherited(self):
        """A new record needs its own name; inheriting one would be a mistake
        that looks like a feature."""
        req = self.adopt({"prompt": "x", "adopt_artist": "The Salt Line"})
        self.assertNotIn("title", req)

    def test_an_explicit_field_still_wins(self):
        req = self.adopt({"prompt": "x", "adopt_artist": "The Salt Line",
                          "voice": "a completely different singer"})
        self.assertEqual(req["voice"], "a completely different singer")
        self.assertEqual(req["artist"], "The Salt Line")

    def test_an_unknown_artist_is_reported_rather_than_ignored(self):
        """Silently making a fresh artist would look like it worked."""
        with self.assertRaises(ValueError):
            builder.adopt_artist(self.store, {"prompt": "x", "adopt_artist": "Nobody"})

    def test_matching_ignores_case_and_padding(self):
        req = self.adopt({"prompt": "x", "adopt_artist": "  the salt LINE "})
        self.assertEqual(req["artist"], "The Salt Line")

    def test_a_request_without_adoption_is_untouched(self):
        before = {"prompt": "x", "tracks": 2}
        self.assertEqual(builder.adopt_artist(self.store, dict(before)), before)


class TestTheEndpointContract(unittest.TestCase):
    """`GET /v1/artists`, and `adopt_artist` on a press."""

    def source(self, name):
        with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as fh:
            return fh.read()

    def test_the_route_is_the_gateways_own(self):
        import services
        self.assertIn('"/v1/artists"', self.source("supervisor.py"))
        self.assertIsNone(services.resolve("/v1/artists"))
        self.assertIn("/v1/artists", services.GATEWAY_ROUTES)

    def test_listing_wakes_nothing(self):
        """It reads sqlite. A list of artists that costs a cold start is a list
        nobody opens."""
        src = self.source("supervisor.py")
        i = src.index('if route == "/v1/artists"')
        self.assertNotIn("start_service", src[i:i + 900])

    def test_it_is_in_the_spec(self):
        spec = json.loads(self.source("openapi.json"))
        self.assertIn("/v1/artists", spec["paths"])
        get = spec["paths"]["/v1/artists"]["get"]
        for code in ("200", "401"):
            self.assertIn(code, get["responses"])

    def test_adopt_artist_is_in_the_press_schema(self):
        spec = json.loads(self.source("openapi.json"))
        props = (spec["paths"]["/v1/press"]["post"]["requestBody"]["content"]
                 ["application/json"]["schema"]["properties"])
        self.assertIn("adopt_artist", props)
        self.assertIn("voice", props)

    def test_an_unknown_artist_is_a_400_not_a_silent_new_one(self):
        src = self.source("supervisor.py")
        i = src.index("adopt_artist")
        self.assertIn("400", src[i:i + 900])

    def test_it_is_in_the_integration_guide(self):
        self.assertIn("/v1/artists", self.source("INTEGRATION.md"))


if __name__ == "__main__":
    unittest.main()
