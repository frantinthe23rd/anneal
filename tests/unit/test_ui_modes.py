#!/usr/bin/env python3
"""Every studio tab is wired to the same five places in ui.html.

Adding a mode means touching a tab button, a panel, three lookup tables and the
dispatcher. Miss one and nothing raises: the tab renders, the panel appears, and
generating either does the wrong thing or silently files the result under
another tab. Both have happened here — session output landed under the wrong tab
until `RESULT_KIND_FOR_MODE` existed, and the forge strip silently omitted
`video` because it was built from a hardcoded list.

Structural, not behavioural. There is no JavaScript runtime on this machine, so
these assert the wiring is present, not that it works — `tools/lint-ui.py` and a
screenshot cover different halves of the same question.
"""

import os
import re
import unittest

from tests.context import REPO_ROOT

UI = os.path.join(REPO_ROOT, "ui.html")


def source():
    with open(UI, encoding="utf-8") as fh:
        return fh.read()


def table(src, name):
    """The keys of a `const NAME = { ... }` object literal."""
    m = re.search(r"const %s\s*=\s*\{(.*?)\n?\};" % name, src, re.S)
    assert m, "%s not found in ui.html" % name
    body = re.sub(r"//[^\n]*", "", m.group(1))
    return set(re.findall(r"(\w+)\s*:", body))


class TestEveryTabIsFullyWired(unittest.TestCase):
    def setUp(self):
        self.src = source()
        self.modes = set(re.findall(r'data-mode="(\w+)"', self.src))

    def test_every_tab_is_wired_everywhere_it_has_to_be(self):
        """This used to be a frozen set of five tab names, which is the same
        copied-list mistake the rest of this file exists to catch — it failed
        the moment Animation was added, and it would have failed identically if
        a tab had been added and wired correctly. What matters is that whatever
        tabs exist are wired, and the tests below assert exactly that. Kept
        here only to insist there are some."""
        self.assertTrue(self.modes, "no tabs found — the selector is wrong")
        self.assertIn("music", self.modes)

    def test_every_tab_has_a_panel(self):
        panels = set(re.findall(r'data-panel="(\w+)"', self.src))
        # Chat is deliberately outside the two-column layout, so it has a tab
        # and no panel in this column. Everything else must have both.
        self.assertEqual(self.modes - panels, {"chat"})

    def test_every_generating_tab_maps_to_a_service(self):
        """Without this the cold-start warning and the forge strip both point at
        nothing, so a three-minute wait starts with no warning at all."""
        for mode in self.modes - {"press"}:
            self.assertIn(mode, table(self.src, "SERVICE_FOR_MODE"), mode)

    def test_every_generating_tab_filters_its_own_session_output(self):
        """Missing here, a result files itself under whichever tab you switch to
        next — which is exactly the bug RESULT_KIND_FOR_MODE was added to fix."""
        # Every tab that generates into the session list. Press has its own
        # card list and Chat is a transcript, so neither files a result here.
        for mode in self.modes - {"press", "chat"}:
            self.assertIn(mode, table(self.src, "RESULT_KIND_FOR_MODE"), mode)

    def test_every_tab_maps_to_a_library_kind(self):
        for mode in self.modes - {"chat"}:
            self.assertIn(mode, table(self.src, "LIB_KIND_FOR_MODE"), mode)

    def test_every_generating_tab_has_an_empty_line(self):
        for mode in self.modes - {"chat"}:
            self.assertIn(mode, table(self.src, "EMPTY_LINE"), mode)

    def test_the_library_kinds_are_real_kinds(self):
        import outputs
        for kind in table(self.src, "LIB_KIND_FOR_MODE"):
            pass
        m = re.search(r"const LIB_KIND_FOR_MODE\s*=\s*\{(.*?)\n?\};", self.src, re.S)
        values = re.findall(r':\s*"(\w+)"', re.sub(r"//[^\n]*", "", m.group(1)))
        for value in values:
            if value == "press":
                continue          # its own store, not a library kind
            self.assertIn(value, outputs.KINDS, value)


class TestTheForgeStripIsNotAFrozenList(unittest.TestCase):
    """It was, and it silently omitted `video` the day video was added — the
    same frozen-literal failure the test suite has now fixed three times. Video
    is gone, the lesson is not: the strip must still come from /health."""

    def setUp(self):
        self.src = source()

    def test_the_strip_is_built_from_health_rather_than_a_literal(self):
        self.assertIn("function forgeServices", self.src)
        self.assertNotIn("const SERVICES = [", self.src,
                         "the hardcoded service list is back")

    def test_both_strips_use_it(self):
        self.assertEqual(self.src.count("forgeServices().forEach"), 2,
                         "the header strip and the hero strip must both use it")

    def test_host_stats_are_excluded(self):
        """`health` also carries __system, which is not a model. Deriving the
        list from health's keys turned it into a chip reading '__system cold'."""
        self.assertRegex(self.src, r'startsWith\("__"\)')

    def test_every_registered_service_is_in_the_display_order(self):
        """Order is allowed to be a literal — omission from it only affects
        placement, not presence — but a service missing here sorts to the end
        with no thought given to it, so keep them in step."""
        import services
        m = re.search(r"const SERVICE_ORDER\s*=\s*\[(.*?)\];", self.src, re.S)
        listed = set(re.findall(r'"(\w+)"', m.group(1)))
        self.assertFalse(set(services.SERVICES) - listed,
                         "not in SERVICE_ORDER: %s" % (set(services.SERVICES) - listed))


class TestTheMeasuredClaims(unittest.TestCase):
    """Copy that would become a lie if the behaviour changed under it."""

    def setUp(self):
        self.src = source()

    def test_sprites_are_discoverable_on_the_api_page_too(self):
        """The tab makes the set reviewable; the API page is still where a
        game-dev agent finds the endpoint."""
        self.assertIn("/v1/sprites", self.src)

    def test_the_animation_tab_shows_the_loop_and_the_frames(self):
        """It came back because the GIF is the only way to answer "does this
        read as one character moving", and the strip is the only way to answer
        "which frame is wrong". A tab that showed one without the other would
        be missing half the reason it exists."""
        self.assertIn("runSprites", self.src)
        self.assertIn("preview_url", self.src)
        self.assertIn("r.frames", self.src)
        self.assertIn(".frames .fr", self.src)

    def test_the_method_list_is_not_written_into_the_page(self):
        """It carries a licence — Kontext is non-commercial — and a licence in
        two places is the worst thing on the list of things that drift. The
        select is built from /health's limits block."""
        self.assertIn("renderSpriteMethods", self.src)
        self.assertNotIn("Non-Commercial", self.src)
        self.assertNotIn('value="kontext"', self.src)

    def test_no_dead_video_code_survives_its_removal(self):
        """Video was removed after measuring it: 9 frames in 3 min 9 s at a
        22 GB peak. What is left behind is a run handler, a <video> branch and
        a service chip that nothing can reach."""
        for dead in ("runVideo", "r.video", "/v1/videos"):
            self.assertNotIn(dead, self.src, dead)


if __name__ == "__main__":
    unittest.main()
