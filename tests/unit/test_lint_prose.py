#!/usr/bin/env python3
"""The prose linter has to catch the sentences that prompted it.

A checker nobody has tested against a known-bad input is a checker that reports
zero and is believed. These use the actual sentences that shipped.
"""

import os
import subprocess
import sys
import tempfile
import unittest

from tests.context import REPO_ROOT

LINT = os.path.join(REPO_ROOT, "tools", "lint-prose.py")
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))


def run(*args):
    return subprocess.run([sys.executable, LINT] + list(args),
                          capture_output=True, text=True, cwd=REPO_ROOT)


class TestItCatchesWhatShipped(unittest.TestCase):
    def check(self, text, suffix=".md"):
        with tempfile.NamedTemporaryFile("w", suffix=suffix, dir=REPO_ROOT,
                                         delete=False, encoding="utf-8") as fh:
            fh.write(text); path = fh.name
        self.addCleanup(os.unlink, path)
        return run(os.path.basename(path))

    def test_the_sentence_that_started_this(self):
        out = self.check("**Experimental, and honestly so**: the output is not recognisable.")
        self.assertIn("DROP", out.stdout)

    def test_worth_knowing(self):
        self.assertIn("DROP", self.check("### Defaults worth knowing").stdout)

    def test_deliberately_is_flagged_but_only_as_suspect(self):
        out = self.check("Sprites are **API-only, deliberately** — there is no tab.")
        self.assertIn("suspect", out.stdout)
        self.assertNotIn("DROP", out.stdout)

    def test_plain_prose_is_left_alone(self):
        out = self.check("Nine voices take a written direction. Kokoro's do not.")
        self.assertIn("Nothing flagged", out.stdout)

    def test_a_word_inside_another_word_is_not_a_match(self):
        """'candidate' contains no finding, and 'transparency' is about alpha."""
        out = self.check("Candidates are listed. PNG transparency is preserved.")
        self.assertIn("Nothing flagged", out.stdout)


class TestItReadsWhatAVisitorReads(unittest.TestCase):
    def test_script_blocks_in_html_are_ignored(self):
        """ui.html's JavaScript is full of 'this is deliberate' comments that
        exist to stop someone undoing a fix. Flagging them would train people to
        delete the note rather than think."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("lint_prose", LINT)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        with tempfile.NamedTemporaryFile("w", suffix=".html", dir=REPO_ROOT,
                                         delete=False, encoding="utf-8") as fh:
            fh.write("<p>Plain copy.</p>\n<script>// honestly deliberate</script>\n")
            path = fh.name
        self.addCleanup(os.unlink, path)
        self.assertNotIn("honestly", mod.visible_text(path))
        self.assertIn("Plain copy", mod.visible_text(path))

    def test_only_descriptions_are_taken_from_the_spec(self):
        import importlib.util, json
        spec = importlib.util.spec_from_file_location("lint_prose", LINT)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        with tempfile.NamedTemporaryFile("w", suffix=".json", dir=REPO_ROOT,
                                         delete=False, encoding="utf-8") as fh:
            json.dump({"paths": {"/x": {"get": {"description": "honestly fine",
                                                "operationId": "deliberately"}}}}, fh)
            path = fh.name
        self.addCleanup(os.unlink, path)
        text = mod.visible_text(path)
        self.assertIn("honestly fine", text)
        self.assertNotIn("operationId", text)


class TestTheRepoStaysClean(unittest.TestCase):
    def test_no_drop_words_survive_in_public_prose(self):
        """The ratchet. This is the check that keeps the convention real."""
        out = run("--strict")
        self.assertEqual(out.returncode, 0,
                         "self-characterising words are back:\n" + out.stdout)


if __name__ == "__main__":
    unittest.main()
