#!/usr/bin/env python3
"""The UI linter has to catch a call to a function nobody defines.

It did not, and that cost a shipped bug. Removing the Animation tab deleted a
span of ui.html that also contained the two sound-effect handlers, because they
had been inserted inside it. Everything downstream passed: the page parsed,
every `$("id")` resolved, both linters were clean, the whole suite was green and
CI was green. The Effects tab then threw "Can't find variable: runSfx" the first
time it was opened.

A dangling id was already caught. A dangling *function* was not, and it is the
same class of failure — markup or code deleted out from under something that
still refers to it.
"""

import os
import subprocess
import sys
import tempfile
import unittest

from tests.context import REPO_ROOT

LINT = os.path.join(REPO_ROOT, "tools", "lint-ui.py")

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>t</title></head>
<body><div id="a"></div>
<script>
%s
</script>
</body></html>
"""


def run(body):
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
        fh.write(PAGE % body)
        path = fh.name
    try:
        done = subprocess.run([sys.executable, LINT, path],
                              capture_output=True, text=True)
        return done.returncode, done.stdout + done.stderr
    finally:
        os.unlink(path)


class TestCallsToNothing(unittest.TestCase):
    def test_a_call_with_no_definition_is_reported(self):
        code, out = run("function boot() { missing(); }\nboot();")
        self.assertEqual(code, 1, out)
        self.assertIn("undefined-call", out)
        self.assertIn("missing", out)

    def test_a_defined_function_is_not(self):
        code, out = run("function there() { return 1; }\nthere();")
        self.assertEqual(code, 0, out)

    def test_the_shape_that_actually_shipped(self):
        """A handler called from a dispatcher, with the definition deleted."""
        code, out = run('let mode = "sfx";\n'
                        'async function go() { if (mode === "sfx") await runSfx(); }\ngo();')
        self.assertEqual(code, 1, out)
        self.assertIn("runSfx", out)


class TestItDoesNotCryWolf(unittest.TestCase):
    """A linter with false positives is a linter people stop reading, which is
    worse than the gap it closes."""

    def test_prose_in_a_string_is_not_a_call(self):
        code, out = run('const s = "download a track (see the guide)";\nconsole.log(s);')
        self.assertEqual(code, 0, out)

    def test_prose_in_a_comment_is_not_a_call(self):
        code, out = run("// pause (briefly) before the next one\nconsole.log(1);")
        self.assertEqual(code, 0, out)

    def test_a_template_literal_is_not_code(self):
        code, out = run("const s = `turn (left) here`;\nconsole.log(s);")
        self.assertEqual(code, 0, out)

    def test_async_arrow_parameters_are_not_a_call(self):
        code, out = run("const f = async (a, b) => a + b;\nf(1, 2);")
        self.assertEqual(code, 0, out)

    def test_a_parameter_called_as_a_callback_is_not_undefined(self):
        code, out = run("function each(list, cb) { list.forEach((x) => cb(x)); }\n"
                        "each([1], (n) => n);")
        self.assertEqual(code, 0, out)

    def test_browser_globals_are_known(self):
        code, out = run("setTimeout(() => fetch('/x'), 10);\n"
                        "requestAnimationFrame(() => parseInt('1', 10));")
        self.assertEqual(code, 0, out)

    def test_the_real_page_is_clean(self):
        # The point of the check is that it holds against the file it guards.
        done = subprocess.run([sys.executable, LINT], capture_output=True, text=True,
                              cwd=REPO_ROOT)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
