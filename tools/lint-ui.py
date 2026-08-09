#!/usr/bin/env python3
"""Lint ui.html — the faults this interface has actually shipped.

`ui.html` is one 3,700-line file of HTML, CSS and JavaScript served straight off
disk, so nothing between the editor and the browser looks at it. Every check
here corresponds to a bug that reached the running page and raised no error:

    html            unbalanced or stray tags
    duplicate-id    two elements answering to one getElementById
    js-parse        a syntax error in an inline script, which kills the file
                    from that point on and takes the rest of the page with it
    dangling-ref    $("thing") for markup that no longer exists — a handler
                    outliving the button it was bound to
    css-var         var(--x) with no definition, which silently computes to
                    nothing rather than failing
    external        an http(s) subresource. "The page fetches nothing
                    externally" is a promise made in README.md; it is worth a
                    machine check, not a habit
    theme-parity    a colour token declared in the dark :root and not in the
                    light one falls back to the dark value. That is exactly how
                    near-black text ended up on a near-black plate

No Node, no npm, no dependencies: standard library, plus the JavaScriptCore
shell that ships with macOS for the parse check. There is deliberately no
JavaScript toolchain on this host and this file must not become the reason to
install one — it does not lint style, only correctness that has cost time here.

    tools/lint-ui.py [path/to/ui.html] [--verbose]

Silent and exit 0 when clean; findings with line numbers and exit 1 otherwise.
A screenshot is still required for anything visual (tools/README.md) — this
checks what is true about the file, not what the page looks like.
"""
import bisect
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSC = ("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A"
       "/Helpers/jsc")

# Elements with no end tag. Closing one is a mistake worth reporting.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}

# Elements HTML lets you leave open. Not reported as unclosed, and closed
# implicitly by the start tags listed below, so ordinary list and table markup
# does not produce a page of noise.
OPTIONAL_END = {"p", "li", "dt", "dd", "option", "optgroup", "thead", "tbody",
                "tfoot", "tr", "td", "th", "rt", "rp", "caption", "colgroup"}

BLOCK = {"address", "article", "aside", "blockquote", "details", "div", "dl",
         "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
         "h3", "h4", "h5", "h6", "header", "hr", "main", "nav", "ol", "p",
         "pre", "section", "table", "ul"}

IMPLIED_CLOSE = {
    "li": {"li"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"},
    "option": {"option"}, "optgroup": {"option", "optgroup"},
    "tr": {"td", "th", "tr"}, "td": {"td", "th"}, "th": {"td", "th"},
    "tbody": {"td", "th", "tr", "thead", "tbody", "caption", "colgroup"},
    "tfoot": {"td", "th", "tr", "thead", "tbody", "caption", "colgroup"},
    "thead": {"caption", "colgroup"},
}

# Colour literals. A token whose value contains one has to be restated for the
# light theme; --radius, --ease and the spacing scale do not.
COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b"
                    r"|\b(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch)\s*\("
                    r"|\b(?:white|black|silver|gray|grey|red|blue|green)\b")

ABSOLUTE = re.compile(r"^\s*(?:https?:)?//", re.I)


class Doc(HTMLParser):
    """One pass over the file: tag balance, ids, and the inline script/style."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.text = text
        self.findings = []
        self.ids = []                  # (name, line)
        self.scripts = []              # (start_line, source)
        self.styles = []               # (start_line, source)
        self.inline_styles = []        # (line, value)
        self.attrs_seen = []           # (line, tag, name, value)
        self.stack = []                # (tag, line)
        self._cdata = None             # (kind, start_line)
        self.feed(text)
        self.close()
        for tag, line in reversed(self.stack):
            if tag not in OPTIONAL_END:
                self.flag(line, "html", "<%s> is never closed" % tag)

    def flag(self, line, check, message):
        self.findings.append((line, check, message))

    def line(self):
        return self.getpos()[0]

    def handle_starttag(self, tag, attrs):
        line = self.line()
        for name, value in attrs:
            if value is None:
                continue
            if name == "id":
                self.ids.append((value, line))
            elif name == "style":
                self.inline_styles.append((line, value))
            self.attrs_seen.append((line, tag, name, value))

        if tag in ("script", "style"):
            self._cdata = (tag, line, dict(attrs))
        if tag in VOID:
            return
        while self.stack and self.stack[-1][0] in OPTIONAL_END:
            top = self.stack[-1][0]
            if top in IMPLIED_CLOSE.get(tag, set()) or (top == "p" and tag in BLOCK):
                self.stack.pop()
            else:
                break
        self.stack.append((tag, line))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_data(self, data):
        if self._cdata is None:
            return
        kind, _, attrs = self._cdata
        # The line the text starts on, which is the line after <script> unless
        # the tag and the code share one.
        start = self.line()
        if kind == "script" and not attrs.get("src"):
            self.scripts.append((start, data))
        elif kind == "style":
            self.styles.append((start, data))

    def handle_endtag(self, tag):
        line = self.line()
        if tag in ("script", "style"):
            self._cdata = None
        if tag in VOID:
            self.flag(line, "html", "</%s> — <%s> has no end tag" % (tag, tag))
            return
        depth = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                depth = i
                break
        if depth is None:
            if tag not in OPTIONAL_END:
                self.flag(line, "html", "</%s> closes nothing that is open" % tag)
            return
        for open_tag, open_line in self.stack[depth + 1:]:
            if open_tag not in OPTIONAL_END:
                self.flag(open_line, "html",
                          "<%s> is not closed before </%s> on line %d"
                          % (open_tag, tag, line))
        del self.stack[depth:]


# Anything that is legitimately not defined in the page: language built-ins,
# browser globals, and the handful of DOM constructors used here. A name absent
# from this list and from the file is a call that will throw the moment the
# branch containing it runs.
JS_GLOBALS = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "function",
    "new", "await", "delete", "void", "in", "of", "do", "else", "throw",
    "Array", "Boolean", "Date", "Error", "JSON", "Map", "Math", "Number",
    "Object", "Promise", "RegExp", "Set", "String", "Symbol", "WeakMap",
    "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
    "decodeURIComponent", "encodeURI", "decodeURI", "escape", "unescape",
    "setTimeout", "setInterval", "clearTimeout", "clearInterval", "fetch",
    "alert", "confirm", "prompt", "requestAnimationFrame", "cancelAnimationFrame",
    "matchMedia", "getComputedStyle", "structuredClone", "queueMicrotask",
    "Blob", "File", "FileReader", "FormData", "Headers", "Request", "Response",
    "URL", "URLSearchParams", "AbortController", "Image", "Audio", "Event",
    "CustomEvent", "IntersectionObserver", "MutationObserver", "ResizeObserver",
    "TextEncoder", "TextDecoder", "Intl", "BigInt", "Proxy", "Reflect",
    "console", "document", "window", "navigator", "location", "localStorage",
    "sessionStorage", "history", "screen", "performance", "crypto",
    # Not a call — `async (a, b) => …` puts an identifier before a paren.
    "async",
}

# `foo(` where foo is a bare identifier — not `.foo(`, not `new foo(`.
CALL_RE = re.compile(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(")
# Everything that introduces a name into scope somewhere in the file. Broad on
# purpose: a false "defined" is a missed bug, but a false "undefined" is a
# linter that cries wolf and gets ignored, which is worse.
DEF_RES = [
    re.compile(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)"),
    # Destructuring, parameters and catch bindings: any identifier inside the
    # parentheses of a definition, and anything bound by => or catch.
    re.compile(r"\bcatch\s*\(\s*([A-Za-z_$][\w$]*)"),
    re.compile(r"([A-Za-z_$][\w$]*)\s*=>"),
    re.compile(r"\bfor\s*\(\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)"),
]
PARAM_RE = re.compile(r"(?:function\s*\*?\s*[A-Za-z_$][\w$]*\s*|=>\s*|\bfunction\s*)?\(([^()]*)\)\s*(?:=>|\{)")
IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


def blank_js_literals(src):
    """Replace comments, strings and template literals with spaces.

    Positions are preserved so line numbers still map, and nothing inside a
    string can be mistaken for code — which is most of what a naive scan finds:
    "a track (see below)" is prose, not a call to `track`.
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c in "\"'`":
            quote, j = c, i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == quote:
                    j += 1
                    break
                j += 1
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def undefined_calls(src):
    """Every `name(` whose `name` is defined nowhere in the file.

    The gap this closes cost a shipped bug: removing a tab took the sound-effect
    handlers out with it, because they sat inside the span being deleted. The
    page parsed, every id resolved, both linters passed, and `runSfx` threw the
    moment the tab was opened. A dangling id was already caught; a dangling
    function was not.
    """
    src = blank_js_literals(src)
    defined = set(JS_GLOBALS)
    for pattern in DEF_RES:
        defined.update(pattern.findall(src))
    for params in PARAM_RE.findall(src):
        defined.update(IDENT_RE.findall(params))
    # Object literal keys — `foo: function` and shorthand methods — are reached
    # through a property, but a bare call to one is still worth not flagging.
    defined.update(re.findall(r"([A-Za-z_$][\w$]*)\s*:\s*(?:async\s*)?function", src))

    seen = []
    for m in CALL_RE.finditer(src):
        name = m.group(1)
        if name in defined:
            continue
        # `new Foo(` and `.foo(` are already excluded by the pattern; keywords
        # that take a parenthesis are in the globals list.
        seen.append((m.start(1), name))
    return seen


def line_index(text):
    """Offset -> 1-based line number, for the CSS walker."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return lambda pos: bisect.bisect_right(starts, pos)


def strip_comments(css):
    """Blank out /* */ but keep every newline, so line numbers survive."""
    out = []
    i, n = 0, len(css)
    while i < n:
        j = css.find("/*", i)
        if j < 0:
            out.append(css[i:])
            break
        out.append(css[i:j])
        k = css.find("*/", j + 2)
        k = n if k < 0 else k + 2
        out.append("\n" * css.count("\n", j, k))
        i = k
    return "".join(out)


NESTED_AT = re.compile(r"^@(media|supports|layer|container|scope|document)\b", re.I)


def iter_rules(css, base=0):
    """Yield (selector, body, selector_offset), descending into @media."""
    i, n = 0, len(css)
    while i < n:
        start = i
        depth = 0
        quote = None
        prelude_end = None
        statement = False
        while i < n:
            ch = css[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "{":
                if depth == 0:
                    prelude_end = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth <= 0:
                    break
            elif ch == ";" and depth == 0:
                statement = True    # @import / @charset — no block of its own
                break
            i += 1
        if statement:
            i += 1
            continue
        if prelude_end is None:
            break
        selector = css[start:prelude_end].strip()
        body = css[prelude_end + 1:i]
        if NESTED_AT.match(selector):
            yield from iter_rules(body, base + prelude_end + 1)
        else:
            yield selector, body, base + start
        i += 1


DECL = re.compile(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;}]*)")


def declarations(body):
    return DECL.findall(body)


def is_root(selector):
    return re.fullmatch(r":root", selector.strip()) is not None


def is_light_root(selector):
    return re.fullmatch(r""":root\[data-theme\s*=\s*["']?light["']?\]""",
                        selector.strip()) is not None


def check_js(scripts, findings, verbose):
    """Parse each inline script with JavaScriptCore. No Node required.

    `new Function(src)` compiles without running, which is what we want — the
    page's JS cannot execute outside a browser. It wraps the source in a
    function, so reported line numbers are shifted by a constant; the probe
    measures that constant rather than assuming it.
    """
    if not scripts:
        return
    if not os.path.exists(JSC):
        print("lint-ui: jsc not found at %s — skipping the JavaScript parse "
              "check" % JSC, file=sys.stderr)
        return
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for n, (_, src) in enumerate(scripts):
            path = os.path.join(tmp, "script%d.js" % n)
            with open(path, "w") as fh:
                fh.write(src)
            paths.append(path)
        driver = os.path.join(tmp, "check.js")
        with open(driver, "w") as fh:
            fh.write(JS_DRIVER % json.dumps(paths))
        try:
            proc = subprocess.run([JSC, driver], capture_output=True, text=True,
                                  timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            print("lint-ui: could not run jsc (%s) — skipping the JavaScript "
                  "parse check" % e, file=sys.stderr)
            return
        if proc.returncode != 0:
            print("lint-ui: jsc failed: %s" % (proc.stderr.strip() or
                                               proc.stdout.strip()),
                  file=sys.stderr)
            return
        for row in proc.stdout.splitlines():
            parts = row.split("\t")
            if parts[0] != "FAIL":
                continue
            n, rel, message = int(parts[1]), int(parts[2]), parts[3]
            start = scripts[n][0]
            line = start + max(rel, 1) - 1
            findings.append((line, "js-parse", message))
        if verbose:
            print("js-parse: %d inline script(s) compiled" % len(scripts))


JS_DRIVER = r"""
// Measure how far new Function() shifts reported line numbers, rather than
// hardcoding it: the wrapper is an implementation detail of the engine.
var shift = 0;
try { new Function("\n\n\nvar 1x;"); }
catch (e) { if (typeof e.line === "number") shift = e.line - 4; }
var files = %s;
for (var i = 0; i < files.length; i++) {
  try {
    new Function(readFile(files[i]));
    print("OK\t" + i);
  } catch (e) {
    var line = (typeof e.line === "number") ? e.line - shift : 0;
    print("FAIL\t" + i + "\t" + line + "\t" + String(e).replace(/\s+/g, " "));
  }
}
"""


REF_PATTERNS = [
    re.compile(r"""\$\(\s*["']([^"'\s]+)["']\s*\)"""),
    re.compile(r"""getElementById\(\s*["']([^"'\s]+)["']\s*\)"""),
    re.compile(r"""querySelector(?:All)?\(\s*["']#([A-Za-z0-9_-]+)["']\s*\)"""),
]

# Every way an id can come into existence, including ones built in JavaScript
# and injected with innerHTML — those are real elements and must not be
# reported as dangling.
ID_SOURCES = [
    re.compile(r"""\bid\s*=\s*["']([^"'\s]+)["']"""),
    re.compile(r"""\bid\s*=\s*\\["']([^"'\\\s]+)\\["']"""),
    re.compile(r"""\.id\s*=\s*["']([^"'\s]+)["']"""),
    re.compile(r"""setAttribute\(\s*["']id["']\s*,\s*["']([^"'\s]+)["']"""),
]


def lint(path, verbose=False):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()

    doc = Doc(text)
    findings = list(doc.findings)

    # ------------------------------------------------------------ ids
    seen = defaultdict(list)
    for name, line in doc.ids:
        seen[name].append(line)
    for name, lines in seen.items():
        for line in lines[1:]:
            findings.append((line, "duplicate-id",
                             'id "%s" is already used on line %d'
                             % (name, lines[0])))

    known_ids = set()
    for pattern in ID_SOURCES:
        known_ids.update(pattern.findall(text))

    # ------------------------------------------------------------ JavaScript
    check_js(doc.scripts, findings, verbose)

    for start, src in doc.scripts:
        offset = line_index(src)
        for pattern in REF_PATTERNS:
            for m in pattern.finditer(src):
                name = m.group(1)
                if name in known_ids:
                    continue
                findings.append((start + offset(m.start()) - 1, "dangling-ref",
                                 '%s refers to id "%s", which no markup defines'
                                 % (m.group(0), name)))

    # --------------------------------------------------- calls to nothing
    for start, src in doc.scripts:
        offset = line_index(src)
        for pos, name in undefined_calls(src):
            findings.append((start + offset(pos) - 1, "undefined-call",
                             "%s() is called and defined nowhere" % name))

    # ------------------------------------------------------------ CSS
    css = "\n".join(strip_comments(s) for _, s in doc.styles)
    css_line = None
    if doc.styles:
        # Offsets are within the concatenation; map back through the block that
        # contains them. One <style> is the normal case, so keep it simple.
        joined_starts = []
        pos = 0
        for start, s in doc.styles:
            joined_starts.append((pos, start))
            pos += len(strip_comments(s)) + 1

        def css_line(off):
            block_off, block_line = joined_starts[0]
            for b_off, b_line in joined_starts:
                if off >= b_off:
                    block_off, block_line = b_off, b_line
            return block_line + css.count("\n", block_off, off)

    root_defined, dark, light = set(), {}, set()
    all_defined = {}
    for selector, body, off in iter_rules(css):
        decls = declarations(body)
        for name, value in decls:
            all_defined.setdefault(name, selector.strip())
        if is_root(selector):
            root_defined.update(n for n, _ in decls)
            for name, value in decls:
                dark.setdefault(name, (value.strip(), css_line(off)))
        elif is_light_root(selector):
            light.update(n for n, _ in decls)

    # Custom properties set at runtime, or on an element in the markup, are
    # legitimately absent from :root. Found rather than listed, so --pct does
    # not have to be spelled out here and the next one does not get missed.
    runtime = set()
    for _, src in doc.scripts:
        runtime.update(re.findall(
            r"""setProperty\(\s*["'](--[A-Za-z0-9_-]+)["']""", src))
        runtime.update(re.findall(r"""(--[A-Za-z0-9_-]+)\s*:""", src))
    for _, value in doc.inline_styles:
        runtime.update(re.findall(r"""(--[A-Za-z0-9_-]+)\s*:""", value))

    used = []
    for m in re.finditer(r"var\(\s*(--[A-Za-z0-9_-]+)", css):
        used.append((m.group(1), css_line(m.start())))
    for line, value in doc.inline_styles:
        for m in re.finditer(r"var\(\s*(--[A-Za-z0-9_-]+)", value):
            used.append((m.group(1), line))

    reported = set()
    for name, line in used:
        if name in root_defined or name in runtime or name in reported:
            continue
        reported.add(name)
        if name in all_defined:
            findings.append((line, "css-var",
                             "var(%s) resolves only inside `%s`, not from :root"
                             % (name, all_defined[name])))
        else:
            findings.append((line, "css-var",
                             "var(%s) has no definition and computes to nothing"
                             % name))

    # ------------------------------------------------------------ theme parity
    if light:
        for name, (value, line) in sorted(dark.items()):
            if name in light or not COLOUR.search(value):
                continue
            findings.append((line, "theme-parity",
                             '%s is a colour with no :root[data-theme="light"] '
                             "override — the light theme gets the dark value"
                             % name))

    # ------------------------------------------------------------ external
    for line, tag, name, value in doc.attrs_seen:
        if not ABSOLUTE.match(value):
            continue
        if name == "href" and tag in ("a", "area"):
            continue      # navigation the user chooses, not a subresource
        if name in ("src", "href", "srcset", "poster", "data", "action",
                    "formaction", "content"):
            if name == "content" and tag == "meta":
                continue
            findings.append((line, "external",
                             "<%s %s=\"%s\"> is fetched off this machine"
                             % (tag, name, value[:60])))

    imports = []
    for m in re.finditer(r"""@import\s+(?:url\(\s*)?["']?((?:https?:)?//[^"')\s]+)""",
                         css):
        imports.append(m.span())
        findings.append((css_line(m.start()), "external",
                         "@import %s is fetched off this machine" % m.group(1)))
    for m in re.finditer(r"""url\(\s*["']?((?:https?:)?//[^"')\s]+)""", css):
        if any(a <= m.start() < b for a, b in imports):
            continue        # already reported as the @import it belongs to
        findings.append((css_line(m.start()), "external",
                         "url(%s) is fetched off this machine" % m.group(1)))

    for start, src in doc.scripts:
        offset = line_index(src)
        for m in re.finditer(
                r"""(?:fetch|importScripts|open)\(\s*["']((?:https?:)?//[^"'\s]+)""",
                src):
            findings.append((start + offset(m.start()) - 1, "external",
                             "%s is requested off this machine" % m.group(1)))

    return findings


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    path = args[0] if args else os.path.join(HERE, "ui.html")
    if not os.path.exists(path):
        print("lint-ui: no such file: %s" % path, file=sys.stderr)
        return 2

    findings = lint(path, verbose)
    name = os.path.basename(path)
    for line, check, message in sorted(findings, key=lambda f: (f[0], f[1])):
        print("%s:%d  %-13s %s" % (name, line, check, message))
    if findings:
        print("\n%d problem%s" % (len(findings), "" if len(findings) == 1 else "s"),
              file=sys.stderr)
        return 1
    if verbose:
        print("%s: clean" % name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
