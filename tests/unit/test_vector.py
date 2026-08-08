#!/usr/bin/env python3
"""The SVG sanitiser (#36).

The issue's words: "a generated `<script>` inside an SVG a game loads is a real
hazard, not a theoretical one." Everything here is a way that hazard arrives.
Each attack case asserts on the *output*, not on the removal log, because the
log is a convenience and the output is the contract.
"""

import unittest

import vector


class ExtractTest(unittest.TestCase):
    def test_bare_svg(self):
        self.assertEqual(vector.extract('<svg viewBox="0 0 1 1"></svg>'),
                         '<svg viewBox="0 0 1 1"></svg>')

    def test_fenced_block(self):
        reply = "Here you go:\n\n```svg\n<svg viewBox=\"0 0 1 1\"/>\n```\nHope that helps!"
        self.assertRaises(vector.SvgRejected, vector.extract, "no markup here")
        self.assertIn("<svg", vector.extract(reply))

    def test_prose_either_side(self):
        reply = 'Sure! <svg viewBox="0 0 2 2"><circle r="1"/></svg> Let me know.'
        self.assertEqual(vector.extract(reply),
                         '<svg viewBox="0 0 2 2"><circle r="1"/></svg>')

    def test_nothing_usable_is_an_error_not_a_guess(self):
        for junk in ("", None, "I can't draw that.", "<div>hello</div>"):
            self.assertRaises(vector.SvgRejected, vector.extract, junk)


class RejectionTest(unittest.TestCase):
    """Cases where the right answer is failure, not a repaired document."""

    def test_not_xml(self):
        self.assertRaises(vector.SvgRejected, vector.sanitise, "<svg><path d=></svg>")

    def test_wrong_root(self):
        self.assertRaises(vector.SvgRejected, vector.sanitise,
                          '<html><svg viewBox="0 0 1 1"/></html>')

    def test_doctype_is_refused_before_parsing(self):
        """ElementTree is documented as vulnerable to entity expansion, so the
        cheapest defence is never to hand it the document."""
        billion = ('<!DOCTYPE svg [<!ENTITY lol "lol">]>'
                   '<svg viewBox="0 0 1 1"><title>&lol;</title></svg>')
        self.assertRaises(vector.SvgRejected, vector.sanitise, billion)

    def test_entity_declaration_alone_is_refused(self):
        self.assertRaises(vector.SvgRejected, vector.sanitise,
                          '<!ENTITY x "y"><svg viewBox="0 0 1 1"/>')

    def test_oversized_output_is_refused(self):
        huge = '<svg viewBox="0 0 1 1">' + '<path d="M0 0"/>' * 40000 + "</svg>"
        self.assertRaises(vector.SvgRejected, vector.sanitise, huge)

    def test_empty(self):
        for junk in ("", "   ", None):
            self.assertRaises(vector.SvgRejected, vector.sanitise, junk)


class ScriptRemovalTest(unittest.TestCase):
    def _clean(self, src):
        out, removed = vector.sanitise(src)
        return out, removed

    def test_script_element_goes(self):
        out, removed = self._clean(
            '<svg viewBox="0 0 10 10"><script>alert(1)</script><circle r="4"/></svg>')
        self.assertNotIn("script", out.lower())
        self.assertNotIn("alert", out)
        self.assertIn("<circle", out)
        self.assertTrue(removed)

    def test_event_handler_attributes_go(self):
        for handler in ("onload", "onclick", "onmouseover", "onerror", "onfocusin"):
            out, _ = self._clean(
                '<svg viewBox="0 0 10 10" %s="alert(1)"><circle r="4"/></svg>' % handler)
            self.assertNotIn(handler, out.lower(), handler)
            self.assertNotIn("alert", out, handler)

    def test_event_handler_on_a_child_goes(self):
        out, _ = self._clean(
            '<svg viewBox="0 0 10 10"><circle r="4" onclick="fetch(1)"/></svg>')
        self.assertNotIn("onclick", out.lower())

    def test_foreignobject_goes(self):
        """A hole straight back to HTML, and therefore to <script>."""
        out, _ = self._clean('<svg viewBox="0 0 10 10"><foreignObject>'
                             '<body xmlns="http://www.w3.org/1999/xhtml">'
                             '<script>alert(1)</script></body></foreignObject></svg>')
        self.assertNotIn("foreignObject", out)
        self.assertNotIn("script", out.lower())

    def test_javascript_href_goes(self):
        out, _ = self._clean('<svg viewBox="0 0 10 10">'
                             '<a href="javascript:alert(1)"><circle r="4"/></a></svg>')
        self.assertNotIn("javascript", out.lower())

    def test_smil_animate_retargeting_href_goes(self):
        """Script execution wearing animation's clothes."""
        out, _ = self._clean(
            '<svg viewBox="0 0 10 10"><a><animate attributeName="href" '
            'values="javascript:alert(1)"/><circle r="4"/></a></svg>')
        self.assertNotIn("javascript", out.lower())
        self.assertNotIn("animate", out.lower())

    def test_style_element_goes(self):
        """<style> can @import and can carry url(); presentation attributes
        do everything a drawn icon needs."""
        out, _ = self._clean('<svg viewBox="0 0 10 10"><style>'
                             '@import url(https://evil.example/x.css);'
                             '</style><circle r="4"/></svg>')
        self.assertNotIn("@import", out)
        self.assertNotIn("evil.example", out)


class ExternalReferenceTest(unittest.TestCase):
    """The page is supposed to make no external requests; a generated asset
    must not be the thing that changes that."""

    def test_remote_image_href_goes(self):
        out, _ = vector.sanitise('<svg viewBox="0 0 10 10">'
                                 '<image href="https://tracker.example/pixel.png"/></svg>')
        self.assertNotIn("tracker.example", out)

    def test_remote_use_href_goes(self):
        out, _ = vector.sanitise('<svg viewBox="0 0 10 10">'
                                 '<use xmlns:xlink="http://www.w3.org/1999/xlink" '
                                 'xlink:href="https://evil.example/x.svg#a"/></svg>')
        self.assertNotIn("evil.example", out)

    def test_local_fragment_reference_survives(self):
        out, _ = vector.sanitise(
            '<svg viewBox="0 0 10 10"><defs><clipPath id="c"><rect width="5" height="5"/>'
            '</clipPath></defs><circle r="4" clip-path="url(#c)"/></svg>')
        self.assertIn("url(#c)", out)

    def test_external_url_in_fill_goes(self):
        out, _ = vector.sanitise('<svg viewBox="0 0 10 10">'
                                 '<circle r="4" fill="url(https://evil.example/p.svg#g)"/></svg>')
        self.assertNotIn("evil.example", out)

    def test_data_uri_script_goes(self):
        out, _ = vector.sanitise('<svg viewBox="0 0 10 10">'
                                 '<circle r="4" fill="data:text/html,&lt;script&gt;"/></svg>')
        self.assertNotIn("data:text/html", out)


class NormalisationTest(unittest.TestCase):
    def test_viewbox_is_preserved(self):
        out, _ = vector.sanitise('<svg viewBox="0 0 32 32"><circle r="4"/></svg>')
        self.assertIn('viewBox="0 0 32 32"', out)

    def test_missing_viewbox_is_derived_from_width_and_height(self):
        out, _ = vector.sanitise('<svg width="48" height="48"><circle r="4"/></svg>')
        self.assertIn('viewBox="0 0 48 48"', out)

    def test_missing_viewbox_falls_back_to_requested_size(self):
        out, _ = vector.sanitise("<svg><circle r='4'/></svg>", size=64)
        self.assertIn('viewBox="0 0 64 64"', out)

    def test_absolute_dimensions_are_dropped(self):
        """A width in pixels is what stops an SVG scaling, which is the entire
        reason to have asked for vector."""
        out, _ = vector.sanitise('<svg viewBox="0 0 24 24" width="24" height="24">'
                                 '<circle r="4"/></svg>')
        self.assertNotIn("width=", out.split(">")[0])
        self.assertNotIn("height=", out.split(">")[0])

    def test_currentcolor_is_added_when_nothing_specifies_paint(self):
        out, _ = vector.sanitise('<svg viewBox="0 0 10 10"><circle r="4"/></svg>')
        self.assertIn('fill="currentColor"', out)

    def test_existing_colours_are_not_flattened(self):
        """Filling in a gap is right; overriding a deliberate choice is not."""
        out, _ = vector.sanitise('<svg viewBox="0 0 10 10">'
                                 '<circle r="4" fill="#e0574a"/><rect fill="#2b8"/></svg>')
        self.assertIn("#e0574a", out)
        self.assertIn("#2b8", out)
        self.assertNotIn("currentColor", out)

    def test_the_svg_namespace_is_declared(self):
        out, _ = vector.sanitise("<svg viewBox='0 0 10 10'><circle r='4'/></svg>")
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', out)

    def test_no_ns0_prefixes_leak_into_the_output(self):
        """It should read as something a person could edit afterwards."""
        out, _ = vector.sanitise(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            '<circle r="4"/></svg>')
        self.assertNotIn("ns0", out)

    def test_output_is_still_parseable(self):
        import xml.etree.ElementTree as ET
        out, _ = vector.sanitise('<svg viewBox="0 0 10 10" onload="x()">'
                                 '<script>y()</script><g><path d="M0 0L1 1"/></g></svg>')
        ET.fromstring(out)                  # must not raise

    def test_drawing_survives_the_cleaning(self):
        """The point is a usable icon, not an empty document."""
        out, _ = vector.sanitise(
            '<svg viewBox="0 0 24 24"><g><path d="M12 2L22 22H2Z" fill="#fff"/>'
            '<circle cx="12" cy="16" r="2" fill="#000"/></g></svg>')
        self.assertIn('d="M12 2L22 22H2Z"', out)
        self.assertIn("<circle", out)
        self.assertIn("<g", out)


class LiveModelOutputTest(unittest.TestCase):
    """Verbatim replies from gemma-4-e4b-it-4bit through the running gateway.

    These are the actual failures the sanitiser has to survive, kept because
    they are what the model does rather than what a test author imagined. All
    five were produced at temperature 0.9 with the first version of the prompt;
    four of them are malformed, which is the measurement that moved the
    endpoint to temperature 0.2.
    """

    # Closes a <defs> with </polygon>, then a stray </polygon>.
    UNBALANCED = ('<svg viewBox="0 0 24 24">\n<g fill="currentColor">\n<defs>\n'
                  '<polygon id="gear-tooth" points="22,22 22,22" />\n</polygon>\n'
                  '</g>\n<polygon points="22,22" />\n</svg>')
    # A quote inside the path data closes the attribute early.
    UNESCAPED_QUOTE = ('<svg viewBox="0 0 24 24">'
                       '<path d="M12 21.35l-2.62-2.22c-.22-.19-.22-.62 0-".82l2.62"/></svg>')
    # <use> after the root has closed.
    TRAILING_USE = ('<svg viewBox="0 0 32 32"><defs><polygon id="s" points="2 2 29 2"/>'
                    '</svg><use xlink:href="#s" /></svg>')

    def test_malformed_output_is_reported_not_repaired(self):
        for name, src in (("unbalanced", self.UNBALANCED),
                          ("unescaped quote", self.UNESCAPED_QUOTE)):
            with self.assertRaises(vector.SvgRejected, msg=name):
                vector.sanitise(vector.extract(src))

    def test_trailing_content_after_the_root_does_not_smuggle_anything(self):
        """extract() takes to the last </svg>, so anything after the first one
        must still end up rejected or cleaned, never silently included."""
        try:
            out, _ = vector.sanitise(vector.extract(self.TRAILING_USE))
        except vector.SvgRejected:
            return                          # rejecting it is a fine answer
        self.assertNotIn("xlink:href", out)

    # The well-formed one, verbatim. Its problem is that it is not a health bar
    # — three abutting rects in one colour render as a single rectangle — which
    # no amount of sanitising fixes and is why this is not in the UI.
    WELL_FORMED = ('<svg viewBox="0 0 64 64" fill="none" '
                   'xmlns="http://www.w3.org/2000/svg">\n<g>\n'
                   '<rect x="12" y="22" width="40" height="22" rx="5" fill="currentColor" />\n'
                   '<rect x="12" y="22" width="40" height="22" rx="5" opacity="0.2" />\n'
                   '</g>\n</svg>')

    def test_a_model_declared_namespace_is_not_emitted_twice(self):
        """This was a real bug: setting a literal xmlns attribute alongside the
        serializer's own declaration produced a document that would not parse.
        Found by running against the live model, not by reading the code."""
        import xml.etree.ElementTree as ET
        out, _ = vector.sanitise(self.WELL_FORMED, size=64)
        self.assertEqual(out.count("xmlns="), 1)
        ET.fromstring(out)                  # must not raise

    def test_output_without_a_declared_namespace_gains_one(self):
        out, _ = vector.sanitise('<svg viewBox="0 0 10 10"><circle r="4"/></svg>')
        self.assertEqual(out.count("xmlns="), 1)
        self.assertIn(vector.SVG_NS, out)


class PromptTest(unittest.TestCase):
    def test_known_style_is_used(self):
        p = vector.build_prompt("a gear", "line", 32)
        self.assertIn("a gear", p)
        self.assertIn("stroked outlines", p)
        self.assertIn("0 0 32 32", p)

    def test_unknown_style_falls_back_rather_than_raising(self):
        p = vector.build_prompt("a gear", "baroque-oil-painting", 24)
        self.assertIn(vector.STYLES[vector.DEFAULT_STYLE], p)


if __name__ == "__main__":
    unittest.main()
