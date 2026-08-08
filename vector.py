#!/usr/bin/env python3
"""SVG the text model draws — extracted, sanitised and normalised (#36).

SVG is markup, so drawing an icon is a text-generation task rather than an
image-generation one. That makes it the first Anneal capability that is fast:
2–7 seconds on an already-warm Gemma, no new weights, no heavy slot, nothing
evicted.

**Measured quality, and it is not what was assumed.** The expectation was
LLM-authored SVG to be "good at geometric, iconographic and UI work". On the
model that actually fits here — `gemma-4-e4b-it-4bit` — that is not what
happens. Against five subjects (gear, heart, compass rose, health bar, shield):

  temperature 0.9, plain instructions   1/5 well-formed
  temperature 0.2, rules named after
    the observed failures               5/5 well-formed
  the same, plus two worked examples    5/5 well-formed

and every one of those well-formed results was *rendered and looked at*. They
are not usable icons. The gear came back as a plain filled circle; the compass
rose as a single dot, its lines invisible for want of a stroke; the health bar
as one solid rectangle, its three segments abutting in the same colour; the
heart as an amorphous blob. Few-shot moved the *form* — correct line style,
strokes present, better structure — and did not move whether the drawing
resembles its subject.

So: well-formed is not good, in exactly the way this repo's audio verification
notes warn about. The endpoint exists, the safety work below is real and is
needed the moment this improves, and the capability is **not** presented in the
UI as something that works. Whether to keep it, and whether a larger text model
([#33](https://github.com/frantinthe23rd/anneal/issues/33)) changes the answer, is a decision rather than a fix.

**The output is untrusted markup.** That is the whole reason this module is
separate and tested. An SVG is not a picture; it is a document that can carry
script, fetch remote resources, and — if a game or a browser loads it — run
them. A model that has read the entire web will occasionally emit `<script>`,
`onload=`, or an `<image href="https://...">`, not out of malice but because
that is what SVG in the wild looks like. So nothing generated here is returned
or written to disk until it has been through `sanitise`.

The approach is an **allowlist**, not a blocklist. A blocklist is a list of the
attacks somebody thought of; the set of SVG elements a drawn icon legitimately
needs is small and closed, so everything outside it goes. Specifically kept out:

- `script`, and `foreignObject`, which is a hole straight back to HTML
- every `on*` handler attribute
- `href` / `xlink:href` to anything but a local `#fragment` — no remote fetches,
  no `javascript:`, no `data:`
- SMIL `animate`/`set` retargeting `href`, which is script execution wearing
  animation's clothes
- `<style>` blocks, which can `@import` and can carry `url()` — a drawn icon
  does not need them, and presentation attributes survive intact
- any `DOCTYPE` or `ENTITY` declaration, refused before parsing rather than
  handed to expat, because ElementTree is documented as vulnerable to entity
  expansion and the cheapest defence is not to parse it at all

`sanitise` returns the cleaned source plus a list of what it removed. The list
is not decoration: it is reported back to the caller and logged, because
"silently produced something different from what the model drew" is its own
kind of bug.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"

# Elements a drawn icon legitimately needs. Anything else is dropped whole.
ALLOWED_TAGS = {
    "svg", "g", "defs", "title", "desc", "symbol",
    "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan",
    "linearGradient", "radialGradient", "stop",
    "clipPath", "mask", "pattern", "marker",
}

# Attributes that carry geometry, paint or structure. Presentation attributes
# do everything a drawn icon needs, which is why <style> can be refused outright.
ALLOWED_ATTRS = {
    "id", "class", "viewBox", "xmlns", "width", "height",
    "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
    "d", "points", "transform", "gradientUnits", "gradientTransform",
    "offset", "spreadMethod", "patternUnits", "markerWidth", "markerHeight",
    "refX", "refY", "orient", "clipPathUnits", "maskUnits", "preserveAspectRatio",
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity",
    "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "stroke-dashoffset",
    "stroke-miterlimit", "opacity", "color", "stop-color", "stop-opacity",
    "font-family", "font-size", "font-weight", "font-style", "text-anchor",
    "dominant-baseline", "letter-spacing", "clip-path", "mask", "filter",
    "vector-effect", "paint-order", "shape-rendering",
}

# Values that must never appear inside an attribute, whatever the attribute is.
# `expression(` is old IE CSS; harmless now, but it costs nothing to refuse.
_DANGEROUS_VALUE = re.compile(
    r"javascript\s*:|vbscript\s*:|data\s*:(?!image/(png|jpeg|gif|webp);base64,)|expression\s*\(|<\s*script",
    re.I)
# url(#local) is fine; url(anything-else) reaches off the document.
_EXTERNAL_URL = re.compile(r"url\s*\(\s*['\"]?(?!#)", re.I)

_DOCTYPE_OR_ENTITY = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.I)

MAX_SVG_BYTES = 256 * 1024


class SvgRejected(Exception):
    """The model's output is not usable SVG, and no amount of cleaning fixes it."""


def extract(text):
    """Pull the SVG document out of a reply that may be wrapped in prose.

    Instruct models are asked for bare SVG and produce a fenced code block, or
    a sentence of preamble, roughly one time in three. Mirrors
    `builder.extract_json`, which exists for the same reason.
    """
    if not text:
        raise SvgRejected("the model returned nothing")
    fenced = re.search(r"```(?:svg|xml|html)?\s*(<svg\b.*?(?:</svg\s*>|/>))\s*```",
                       text, re.S | re.I)
    if fenced:
        return fenced.group(1)
    start = text.lower().find("<svg")
    if start < 0:
        raise SvgRejected("no <svg> element in the model's reply")
    end = text.lower().rfind("</svg")
    if end > start:
        return text[start:text.index(">", end) + 1]
    # A root that closes itself: <svg …/>. Rare from a model, but the fallback
    # must not turn "unusual" into "no output".
    close = text.find(">", start)
    if close > 0 and text[close - 1] == "/":
        return text[start:close + 1]
    raise SvgRejected("the <svg> element in the model's reply is not closed")


def _localname(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def _attr_name(name):
    """`{ns}href` -> `xlink:href` for the namespaces that matter, else local."""
    if name.startswith("{"):
        ns, local = name[1:].split("}", 1)
        if ns == "http://www.w3.org/1999/xlink":
            return "xlink:" + local
        if ns == SVG_NS:
            return local
        return "%s:%s" % (ns, local)
    return name


def _clean_element(elem, removed):
    """Strip disallowed attributes here, then recurse, dropping bad children.

    Returns True if the element itself may stay.
    """
    if _localname(elem.tag) not in ALLOWED_TAGS:
        removed.append("<%s>" % _localname(elem.tag))
        return False

    for raw_name in list(elem.attrib):
        name = _attr_name(raw_name)
        value = elem.attrib[raw_name] or ""
        lowered = name.lower()

        if lowered.startswith("on"):
            removed.append("%s=" % name)
            del elem.attrib[raw_name]
            continue
        # href is the one attribute worth allowing conditionally: <use href="#a">
        # and clip-path references are ordinary, anything else reaches out.
        if lowered in ("href", "xlink:href"):
            if not value.strip().startswith("#"):
                removed.append("%s=%s" % (name, value[:40]))
                del elem.attrib[raw_name]
            continue
        if lowered.split(":")[0] in ("xmlns", "xml") or lowered == "xmlns":
            continue                       # namespace declarations are structural
        if name not in ALLOWED_ATTRS:
            removed.append("%s=" % name)
            del elem.attrib[raw_name]
            continue
        if _DANGEROUS_VALUE.search(value) or _EXTERNAL_URL.search(value):
            removed.append("%s=%s" % (name, value[:40]))
            del elem.attrib[raw_name]

    for child in list(elem):
        if not _clean_element(child, removed):
            elem.remove(child)
    return True


def _viewbox_from(root, fallback):
    vb = root.get("viewBox")
    if vb and len(vb.split()) == 4:
        return vb
    # Fall back to the declared width/height if they are plain numbers, then to
    # the requested size. A missing viewBox is what stops an SVG scaling, which
    # is the entire reason to want SVG.
    try:
        w = float(re.sub(r"[^\d.]", "", root.get("width") or ""))
        h = float(re.sub(r"[^\d.]", "", root.get("height") or ""))
        if w > 0 and h > 0:
            return "0 0 %g %g" % (w, h)
    except (TypeError, ValueError):
        pass
    return "0 0 %d %d" % (fallback, fallback)


def sanitise(source, size=24):
    """Clean and normalise generated SVG.

    Returns `(svg_source, removed)`. Raises `SvgRejected` if what came back is
    not a single well-formed SVG document at all — that is a generation failure
    to report, not something to patch up and pass off as a result.
    """
    if not source or not source.strip():
        raise SvgRejected("empty output")
    if len(source.encode("utf-8")) > MAX_SVG_BYTES:
        raise SvgRejected("output is %d bytes, over the %d byte limit"
                          % (len(source.encode("utf-8")), MAX_SVG_BYTES))
    if _DOCTYPE_OR_ENTITY.search(source):
        # Refused before parsing: ElementTree is documented as vulnerable to
        # entity expansion, and a drawn icon has no reason to declare either.
        raise SvgRejected("DOCTYPE or ENTITY declaration is not allowed")

    try:
        root = ET.fromstring(source)
    except ET.ParseError as exc:
        raise SvgRejected("not well-formed XML: %s" % exc)

    if _localname(root.tag) != "svg":
        raise SvgRejected("root element is <%s>, not <svg>" % _localname(root.tag))

    removed = []
    _clean_element(root, removed)

    # Normalise. A viewBox with no absolute width/height is what makes the file
    # scale to wherever it is placed, which is the point of asking for vector.
    root.set("viewBox", _viewbox_from(root, size))
    root.attrib.pop("width", None)
    root.attrib.pop("height", None)
    # Put the tree in the SVG namespace and let the serializer declare it.
    # Setting a literal xmlns attribute here instead emitted it *twice* when the
    # model had already declared it — which is not well-formed XML, so the
    # sanitiser was producing documents that would not parse. Caught by running
    # against the live text model rather than by reading the code.
    if "}" not in root.tag:
        for elem in root.iter():
            if "}" not in elem.tag:
                elem.tag = "{%s}%s" % (SVG_NS, elem.tag)
    root.attrib.pop("xmlns", None)

    # Only if the drawing specifies no paint at all: an icon that came back
    # with no fill anywhere renders as solid black and cannot be restyled,
    # whereas forcing currentColor onto a deliberately multi-coloured drawing
    # would flatten it. So this fills in a gap rather than overriding a choice.
    body = ET.tostring(root, encoding="unicode")
    if not re.search(r'\b(fill|stroke)\s*=\s*"(?!none)', body):
        root.set("fill", "currentColor")

    ET.register_namespace("", SVG_NS)
    out = ET.tostring(root, encoding="unicode")
    # ElementTree writes ns0: prefixes for anything it does not know; the file
    # should read as something a person could edit by hand afterwards.
    out = re.sub(r'\sxmlns:ns\d+="[^"]*"', "", out)
    out = re.sub(r"<(/?)ns\d+:", r"<\1", out)
    return out.strip(), removed


# Written against what this model actually does wrong, not against what a
# capable model might need told. Measured failures on gemma-4-e4b-4bit were, in
# order of frequency: unbalanced tags (a <defs> closed with </polygon>, a
# <use> after </svg>), an unescaped quote inside a path's `d`, and attributes
# invented for an element that has no such attribute (`width` on a <polygon>).
# So the rules name those specifically, and the shape list is deliberately
# short — every element offered is one more chance to close the wrong tag.
DRAW_PROMPT = """Draw one SVG icon.

Subject: {prompt}
Style: {style}
Canvas: viewBox="0 0 {size} {size}"

Output rules — follow exactly:
- Output ONLY the SVG markup. No explanation, no code fence, no commentary.
- Exactly one <svg> root with viewBox="0 0 {size} {size}" and no width or height.
- Nothing after </svg>.
- Use only these elements: path, rect, circle, ellipse, line, polygon, g.
- Every tag must be closed by its own matching tag, or be self-closing (`/>`).
- Never use <defs>, <use>, <script>, <style>, <image>, or any URL.
- Attribute values must be simple: no quotes inside them, plain numbers only.
- Use fill="currentColor" so it can be recoloured where it is used.

Drawing rules:
- Six shapes at most. Fewer is better.
- It must read at small sizes: bold silhouette, no fine detail, no text."""

STYLES = {
    "flat": "flat, solid shapes, no outline",
    "line": "line art, stroked outlines only, fill none, stroke-width around 1.5",
    "duotone": "two tones, a solid base shape with a lighter accent",
    "geometric": "strict geometry, circles and straight lines, poster-like",
}
DEFAULT_STYLE = "flat"


def build_prompt(prompt, style=DEFAULT_STYLE, size=24):
    return DRAW_PROMPT.format(prompt=prompt,
                              style=STYLES.get(style, STYLES[DEFAULT_STYLE]),
                              size=size)
