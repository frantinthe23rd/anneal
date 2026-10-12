#!/usr/bin/env python3
"""Everything on the page sits inside the page's own column.

`.wrap` is what gives the interface its maximum width and its gutters, and it
is what the header, the nav and the tab strip are positioned by. Anything left
outside it keeps its own width and starts hard against the viewport edge, a
long way from the row of tabs directly above it.

This is not the fault `lint-ui.py` catches. The markup was perfectly balanced —
`.wrap` opened once and closed once, and every tag matched. The doc pages were
simply *after* the close rather than before it, indented two spaces as though
they were inside, which is exactly what makes it invisible when reading.
Measured before the fix, at a 1920 viewport: the header at x=360 and the About
page at x=0.

Parsed rather than pattern-matched, because "is this element inside that one"
is a question about the tree and a regex cannot answer it.
"""

from __future__ import annotations

import os
import unittest
from html.parser import HTMLParser

from tests.context import REPO_ROOT

UI = os.path.join(REPO_ROOT, "ui.html")

# Everything that is part of the page proper. The key gate is deliberately
# absent: it is a modal overlay and covers the viewport by design.
MUST_BE_INSIDE_WRAP = ("pageGuide", "pageApi", "pageAbout")


class Nesting(HTMLParser):
    """Records, for every id and class of interest, the stack it was opened in."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr", "path", "circle", "rect",
            "line", "polygon", "polyline", "ellipse", "use", "stop", "animate"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.found = {}          # id -> list of ancestor descriptors
        self.classes = {}        # class name -> list of ancestor descriptors

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        ident = a.get("id") or ""
        cls = (a.get("class") or "").split()
        where = list(self.stack)
        if ident:
            self.found.setdefault(ident, where)
        for c in cls:
            self.classes.setdefault(c, where)
        if tag not in self.VOID:
            self.stack.append("%s#%s.%s" % (tag, ident, ".".join(cls)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID and self.stack:
            self.stack.pop()

    def handle_endtag(self, tag):
        # Pop to the nearest matching tag, so one stray close cannot desync
        # every later answer.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].split("#", 1)[0] == tag:
                del self.stack[i:]
                return


def parse():
    with open(UI, encoding="utf-8") as fh:
        p = Nesting()
        p.feed(fh.read())
        return p


class DocPagesAreInsideThePageColumnTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = parse()

    def assertInsideWrap(self, ancestors, what):
        self.assertTrue(
            any(".wrap" in a for a in ancestors),
            "%s is outside .wrap — it will start at the viewport edge instead of "
            "lining up with the header and the tabs. Ancestors: %s" % (what, ancestors))

    def test_every_doc_page_is_inside_the_wrap(self):
        for ident in MUST_BE_INSIDE_WRAP:
            self.assertIn(ident, self.doc.found, "#%s is missing from ui.html" % ident)
            self.assertInsideWrap(self.doc.found[ident], "#" + ident)

    def test_the_footer_is_inside_the_wrap(self):
        """It spanned the whole viewport, under a page that does not."""
        self.assertIn("sitefoot", self.doc.classes)
        self.assertInsideWrap(self.doc.classes["sitefoot"], ".sitefoot")

    def test_the_studio_column_is_too(self):
        """The one that was always right, so a change that moves the boundary
        the other way is caught as well."""
        for ident in ("modebar", "cols"):
            self.assertIn(ident, self.doc.found)
            self.assertInsideWrap(self.doc.found[ident], "#" + ident)

    def test_the_key_gate_is_deliberately_outside(self):
        """A modal covers the viewport. If it is ever moved inside, it stops
        doing that, and this says so rather than leaving it to a screenshot."""
        self.assertIn("gate", self.doc.found)
        self.assertFalse(any(".wrap" in a for a in self.doc.found["gate"]),
                         "#gate is inside .wrap; an overlay has to cover the page")


class TheTwoTabRowsShareALeftEdgeTest(unittest.TestCase):
    """The mode strip is a `.panel`, whose 1px border insets its contents. The
    page nav had no border, so the two rows of tabs sat a pixel apart —
    measured, Studio's button at x=125 against Music's at x=126."""

    def setUp(self):
        with open(UI, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_the_page_nav_carries_the_same_inset(self):
        block = self.src[self.src.index(".pagetabs {"):]
        block = block[:block.index("}") + 1]
        self.assertIn("border", block,
                      ".pagetabs needs a 1px border to match the .panel the mode "
                      "strip sits in, or the two rows are a pixel out")
