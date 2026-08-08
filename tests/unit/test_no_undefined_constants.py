#!/usr/bin/env python3
"""Every CONSTANT a module uses is one the module defines.

`TRIM_SILENCE` shipped used-but-never-defined: an edit that added the reference
succeeded and the edit that added the constant died on a failed assertion, so
the reference stood alone. Python does not complain until the line runs, the
line only runs when music finishes generating, and the surrounding code caught
the NameError and carried on — so the feature silently did nothing and the only
symptom was audio that still had six seconds of dead air on it.

`tools/lint-ui.py` catches exactly this shape in the UI (a `$("id")` bound to
markup that was deleted) and its docstring explains why: these are the faults
that raise nothing until the wrong moment. This is the Python half.

Deliberately narrow. It checks ALL_CAPS names only — module-level constants —
because that is a shape with almost no false positives and it is the shape that
actually went wrong. It is not a general linter and should not grow into one;
adding a real one is a separate decision.
"""

import ast
import builtins
import os
import unittest

from tests.context import REPO_ROOT

MODULES = ["supervisor.py", "builder.py", "speech_server.py", "image_server.py",
           "outputs.py", "jobstore.py", "services.py", "paths.py", "sprites.py",
           "trim.py", "vector.py", "mcp_server.py"]


def bound_names(tree):
    """Every name the module binds, at any scope."""
    names = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.Global):
            names.update(node.names)
    return names


def used_constants(tree):
    return {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            and n.id.isupper() and len(n.id) > 2}


class TestConstantsAreDefined(unittest.TestCase):
    def test_every_module_defines_the_constants_it_uses(self):
        problems = []
        for name in MODULES:
            path = os.path.join(REPO_ROOT, name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=name)
            missing = used_constants(tree) - bound_names(tree)
            for constant in sorted(missing):
                problems.append("%s uses %s and never defines it" % (name, constant))
        self.assertEqual(problems, [], "\n".join(problems))

    def test_the_check_would_have_caught_the_bug_it_exists_for(self):
        """A guard that cannot fail is not a guard."""
        tree = ast.parse("def f():\n    if TRIM_SILENCE:\n        pass\n")
        self.assertIn("TRIM_SILENCE", used_constants(tree) - bound_names(tree))

    def test_it_does_not_flag_a_constant_that_is_imported(self):
        tree = ast.parse("from services import MUSIC_TIERS\nx = MUSIC_TIERS\n")
        self.assertEqual(used_constants(tree) - bound_names(tree), set())


if __name__ == "__main__":
    unittest.main()
