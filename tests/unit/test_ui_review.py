#!/usr/bin/env python3
"""The review pause needs a surface, and needed one at both ends (#22).

`POST /v1/press/review` and the `awaiting-review` state shipped with no way to
reach either from the page: nothing asked for the pause, and a record that
somehow entered it showed "Waiting for review" with no control to review it.
The record was stuck — the endpoint existed, the button did not.

So this asserts both halves, and that the pause stays opt-in: Forge is still
one press for anyone who does not want to be asked.
"""

import os
import re
import unittest

from tests.context import REPO_ROOT

UI = open(os.path.join(REPO_ROOT, "ui.html"), encoding="utf-8").read()


class TestAskingForTheReview(unittest.TestCase):
    def test_the_form_offers_it(self):
        self.assertIn('id="pReview"', UI)

    def test_it_is_off_by_default(self):
        # One-hit Forge is the default path. A checked box here would make
        # every record stop and wait for someone to come back to it.
        box = re.search(r'<input[^>]*id="pReview"[^>]*>', UI).group(0)
        self.assertNotIn("checked", box)

    def test_the_request_carries_it(self):
        self.assertRegex(UI, r'review:\s*\$\("pReview"\)\.checked')


class TestDoingTheReview(unittest.TestCase):
    def test_the_endpoint_is_called(self):
        self.assertIn("/v1/press/review", UI)

    def test_a_paused_record_offers_the_editor(self):
        self.assertIn("buildReview", UI)

    def test_both_saving_and_approving_exist(self):
        # Amend-only matters: a reviewer rewriting five sets of lyrics should
        # not have to approve to keep the first four.
        review = UI[UI.index("function buildReview"):]
        review = review[:review.index("\nfunction ")]
        self.assertRegex(review, r"send\(\s*save\s*,\s*false\s*\)")
        self.assertRegex(review, r"send\(\s*ok\s*,\s*true\s*\)")

    def test_the_lyrics_are_editable_not_just_readable(self):
        # buildPressDetails already renders lyrics into a <pre>. Reading them
        # is not reviewing them.
        review = UI[UI.index("function buildReview"):]
        review = review[:review.index("\nfunction ")]
        self.assertIn("textarea", review)

    def test_it_is_reachable_after_navigating_away(self):
        # The bug: the editor existed only in the flow that started the press,
        # so leaving the page stranded the record. It has to hang off the
        # library card, which is built from whatever the server reports.
        self.assertIn("buildReview(p, plan)", UI)


class TestThePauseIsNamedOnce(unittest.TestCase):
    def test_the_state_string_matches_the_server(self):
        import builder
        self.assertIn('"%s"' % builder.Press.REVIEW_STATE, UI)


if __name__ == "__main__":
    unittest.main()


class TestThePollDoesNotEatTheEdit(unittest.TestCase):
    """The records list polls every six seconds and rebuilds any card whose
    signature moved. A reviewer rewriting a verse is holding text the server has
    never seen, so a rebuild mid-edit discards it and drops the caret."""

    def test_a_busy_card_is_left_alone(self):
        self.assertIn("reviewBusy", UI)
        render = UI[UI.index("function renderPresses"):]
        self.assertLess(render.index("reviewBusy(already)"),
                        render.index("dataset.sig === pressSignature"),
                        "the guard has to run before the signature check, or a "
                        "changed signature rebuilds the card anyway")

    def test_busy_means_focus_or_unsent_text(self):
        fn = UI[UI.index("function reviewBusy"):]
        fn = fn[:fn.index("\nfunction ")]
        self.assertIn("document.activeElement", fn)
        self.assertIn("dataset.was", fn)


class TestKeepingAnArtist(unittest.TestCase):
    """`GET /v1/artists` and `adopt_artist` are of no use to anyone who never
    leaves the page, so the form has to offer them (#35)."""

    def test_the_form_offers_the_artists_it_has(self):
        self.assertIn('id="pAdopt"', UI)
        self.assertIn("/v1/artists", UI)

    def test_the_request_carries_the_adoption(self):
        self.assertRegex(UI, r"adopt_artist:")

    def test_the_list_is_built_from_the_server(self):
        # Not a hardcoded roster. Four bugs in this repo have come from a copied
        # list going stale the moment something was added to the real one.
        fn = UI[UI.index("function loadArtists"):]
        fn = fn[:fn.index("\nfunction ")]
        self.assertIn("/v1/artists", fn)
        self.assertNotRegex(fn, r'\[\s*"[A-Z]')
