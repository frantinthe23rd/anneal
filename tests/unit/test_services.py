"""services.py — the routing table.

`resolve()` decides which backend owns a request, and getting it wrong means
either waking the wrong multi-gigabyte model or proxying a route the gateway
was supposed to answer itself. Two properties are checked over the whole table
rather than case by case, so adding a service cannot quietly introduce a
collision.
"""

from __future__ import annotations

import os
import unittest

import services
from services import GATEWAY_ROUTES, MUSIC_TIERS, SERVICES, resolve


class TestResolve(unittest.TestCase):
    def test_longest_prefix_wins(self):
        # The case the module docstring calls out: speech's longer route has to
        # beat music's shorter one, or every TTS request wakes ACE-Step.
        self.assertEqual(resolve("/v1/audio/speech"), "speech")
        self.assertEqual(resolve("/v1/audio"), "music")

    def test_known_routes(self):
        for path, service in [
            ("/release_task", "music"),
            ("/query_result", "music"),
            ("/create_random_sample", "music"),
            ("/v1/stats", "music"),
            ("/v1/models", "music"),
            ("/v1/lora", "music"),
            ("/v1/voices", "speech"),
            ("/v1/speech", "speech"),
            ("/v1/images", "image"),
            ("/v1/images/generations", "image"),
            ("/v1/images/file", "image"),
            ("/v1/chat/completions", "text"),
            ("/v1/completions", "text"),
            ("/v1/text", "text"),
        ]:
            self.assertEqual(resolve(path), service, path)

    def test_every_declared_route_resolves_to_its_own_service(self):
        """No service may shadow another's route. Checked over the real table so
        a new entry that collides fails here rather than in production."""
        for name, spec in SERVICES.items():
            for route in spec["routes"]:
                self.assertEqual(resolve(route), name,
                                 "%s declares %s but it resolves elsewhere" % (name, route))

    def test_gateway_routes_never_resolve_to_a_backend(self):
        """These are answered in-process. If one resolved to a service the proxy
        would forward it and the backend would 404 on a route it never had."""
        for route in GATEWAY_ROUTES:
            self.assertIsNone(resolve(route), route)
            self.assertIsNone(resolve(route + "/something"), route)

    def test_the_specific_gateway_routes_that_sit_under_a_backend_prefix(self):
        # /v1/outputs/file and /v1/music/tiers both look like backend routes.
        for path in ["/v1/press", "/v1/press/download", "/v1/press/resume",
                     "/v1/outputs", "/v1/outputs/file", "/v1/music/tiers",
                     "/health", "/supervisor/status", "/supervisor/auth"]:
            self.assertIsNone(resolve(path), path)

    def test_unknown_paths_resolve_to_nothing(self):
        for path in ["/", "/nope", "/v1", "/v2/audio", "/docs", "/openapi.json", ""]:
            self.assertIsNone(resolve(path), path)

    def test_subpaths_of_a_route_belong_to_the_same_service(self):
        self.assertEqual(resolve("/v1/audio/anything"), "music")
        self.assertEqual(resolve("/v1/images/edits"), "image")

    def test_prefix_match_is_not_anchored_at_a_separator(self):
        # Current behaviour: a route matches as a bare string prefix, so a
        # sibling path that merely starts with one is claimed by that service.
        # Nothing in the API is shaped like this today; asserted so that if a
        # future route makes it matter, this is where it shows up.
        self.assertEqual(resolve("/v1/audiobook"), "music")
        self.assertEqual(resolve("/v1/textual"), "text")

    def test_query_strings_are_not_passed_in(self):
        # resolve() is handed a parsed path; a raw one with a query would not
        # match, and this pins the contract the callers rely on.
        self.assertIsNone(resolve("/v1/press?id=abc"))


class TestMusicTiers(unittest.TestCase):
    def test_both_tiers_are_present_and_complete(self):
        self.assertEqual(sorted(MUSIC_TIERS), ["draft", "high"])
        for name, tier in MUSIC_TIERS.items():
            self.assertTrue(tier["model"], name)
            self.assertIsInstance(tier["steps"], int)
            self.assertGreater(tier["steps"], 0)
            self.assertTrue(tier["label"], name)

    def test_the_default_tier_exists(self):
        self.assertIn(services.DEFAULT_MUSIC_TIER, MUSIC_TIERS)

    def test_draft_is_the_distilled_model_and_high_is_not(self):
        self.assertIn("turbo", MUSIC_TIERS["draft"]["model"])
        self.assertNotIn("turbo", MUSIC_TIERS["high"]["model"])

    def test_high_carries_the_cfg_settings_turbo_ignores(self):
        # Non-turbo needs real CFG; without these the sft model produces mush.
        extra = MUSIC_TIERS["high"]["extra_params"]
        self.assertGreater(extra["guidance_scale"], 1.0)
        self.assertEqual(extra["cfg_interval_start"], 0.0)
        self.assertEqual(extra["cfg_interval_end"], 1.0)
        self.assertIs(extra["use_adg"], False)

    def test_draft_carries_no_overrides(self):
        self.assertNotIn("extra_params", MUSIC_TIERS["draft"])

    def test_the_music_service_boots_on_the_default_tier(self):
        self.assertEqual(SERVICES["music"]["env"]["ACESTEP_CONFIG_PATH"],
                         MUSIC_TIERS[services.DEFAULT_MUSIC_TIER]["model"])

    def test_the_two_tiers_use_different_models(self):
        # A tier switch costs a backend restart; two tiers on one model would
        # make that cost buy nothing.
        self.assertNotEqual(MUSIC_TIERS["draft"]["model"], MUSIC_TIERS["high"]["model"])


class TestServiceTable(unittest.TestCase):
    REQUIRED = ("routes", "port", "cmd", "cwd", "heavy", "ready_timeout",
                "idle_timeout", "busy_path", "log")

    def test_every_service_declares_the_fields_the_supervisor_reads(self):
        for name, spec in SERVICES.items():
            for field in self.REQUIRED:
                self.assertIn(field, spec, "%s is missing %s" % (name, field))
            self.assertTrue(spec["routes"], name)
            self.assertIsInstance(spec["heavy"], bool)

    def test_ports_are_unique(self):
        ports = [spec["port"] for spec in SERVICES.values()]
        self.assertEqual(len(ports), len(set(ports)))

    def test_no_service_shares_the_gateway_port(self):
        gateway = int(os.environ.get("SUPERVISOR_PORT", "8001"))
        for name, spec in SERVICES.items():
            self.assertNotEqual(spec["port"], gateway, name)

    def test_only_the_services_that_hold_gigabytes_are_heavy(self):
        # Only one heavy service fits in 16 GB, so this flag is what decides
        # whether a request evicts another model.
        self.assertEqual(sorted(n for n, s in SERVICES.items() if s["heavy"]),
                         ["image", "music"])

    def test_heavy_services_can_be_asked_whether_they_are_busy(self):
        # Without a busy_path the idle reaper would kill a running generation.
        for name, spec in SERVICES.items():
            if spec["heavy"]:
                self.assertTrue(spec["busy_path"], name)

    def test_music_does_not_claim_the_shared_documentation_routes(self):
        # The gateway serves one spec covering every service; routing /docs or
        # /openapi.json to ACE-Step would replace it with the backend's own.
        for route in SERVICES["music"]["routes"]:
            self.assertNotIn(route, ("/docs", "/openapi.json"))

    def test_text_does_not_claim_v1_models(self):
        # mlx_lm serves /v1/models too, and it already belongs to music.
        self.assertNotIn("/v1/models", SERVICES["text"]["routes"])
        self.assertEqual(SERVICES["text"]["health_path"], "/v1/models")


if __name__ == "__main__":
    unittest.main()
