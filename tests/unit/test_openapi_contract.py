"""openapi.json against the code it documents.

The repo convention is that docs change with the code. This is the mechanical
half of that: every path the spec advertises must be one the gateway either
answers itself or routes to a backend, and every route the gateway answers
itself must be documented. Drift here is not cosmetic — `openapi.json` is what
integrators and the docs page are handed.

The route set is read out of supervisor.py with `ast`, not a regex: every
string literal in the module that looks like a path. That over-collects
slightly (error strings, content types) and never under-collects, which is the
right direction for a check whose failure mode should be "you added a route and
did not document it".
"""

from __future__ import annotations

import ast
import json
import os
import unittest

from tests import context

import services
from services import MUSIC_TIERS, resolve

SPEC_PATH = os.path.join(context.REPO_ROOT, "openapi.json")
SUPERVISOR_PATH = os.path.join(context.REPO_ROOT, "supervisor.py")

with open(SPEC_PATH) as _fh:
    SPEC = json.load(_fh)


def path_literals(source_path):
    """Every string constant in a module that looks like a request path."""
    with open(source_path) as fh:
        tree = ast.parse(fh.read(), source_path)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if value.startswith("/") and " " not in value and "\n" not in value:
                # Some literals are whole URLs the gateway hands back
                # ("/v1/press?id="); the path is the part that matters.
                found.add(value.split("?")[0].rstrip("/") or "/")
    return found


SUPERVISOR_PATHS = path_literals(SUPERVISOR_PATH)

METHODS = ("get", "post", "put", "patch", "delete")


def documented_operations():
    for path, item in SPEC["paths"].items():
        for method, op in item.items():
            if method in METHODS:
                yield path, method, op


class TestSpecIsWellFormed(unittest.TestCase):
    def test_it_is_openapi_3(self):
        self.assertTrue(SPEC["openapi"].startswith("3."))
        self.assertTrue(SPEC["info"]["title"])
        self.assertTrue(SPEC["info"]["version"])

    def test_every_operation_documents_a_response(self):
        for path, method, op in documented_operations():
            self.assertTrue(op.get("responses"), "%s %s" % (method.upper(), path))

    def test_every_operation_has_a_summary(self):
        for path, method, op in documented_operations():
            self.assertTrue(op.get("summary"), "%s %s" % (method.upper(), path))

    def test_every_referenced_schema_exists(self):
        components = SPEC.get("components", {}).get("schemas", {})
        missing = []

        def walk(node):
            if isinstance(node, dict):
                ref = node.get("$ref")
                if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                    if ref.rsplit("/", 1)[1] not in components:
                        missing.append(ref)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(SPEC)
        self.assertEqual(missing, [])

    def test_paths_are_absolute_and_unparameterised(self):
        # Anneal takes ids in the query string, not the path, so a templated
        # path would be a documentation error rather than a design choice.
        for path in SPEC["paths"]:
            self.assertTrue(path.startswith("/"), path)
            self.assertNotIn("{", path, path)


class TestDocumentedPathsExist(unittest.TestCase):
    """Every advertised path must actually go somewhere."""

    def test_each_documented_path_is_handled_or_routed(self):
        for path in SPEC["paths"]:
            handled = path in SUPERVISOR_PATHS
            routed = resolve(path) is not None
            self.assertTrue(handled or routed,
                            "%s is documented but neither handled by the gateway "
                            "nor routed to a service" % path)

    def test_gateway_owned_paths_are_handled_in_supervisor(self):
        for path in SPEC["paths"]:
            if resolve(path) is None:
                self.assertIn(path, SUPERVISOR_PATHS,
                              "%s resolves to no service, so the gateway must "
                              "answer it — and does not" % path)

    def test_the_gateway_routes_table_is_fully_documented(self):
        # GATEWAY_ROUTES is what the gateway claims for itself. An entry is
        # either a documented endpoint or a namespace prefix ("/supervisor")
        # under which documented endpoints live; anything else is undocumented.
        undocumented = []
        for route in services.GATEWAY_ROUTES:
            if route in SPEC["paths"]:
                continue
            if any(path.startswith(route + "/") for path in SPEC["paths"]):
                continue
            undocumented.append(route)
        self.assertEqual(undocumented, [],
                         "gateway routes with no entry in openapi.json")

    def test_the_endpoints_a_client_cannot_do_without_are_present(self):
        for path in ["/health", "/supervisor/status", "/v1/music/tiers",
                     "/v1/outputs", "/v1/outputs/file", "/v1/press",
                     "/release_task", "/query_result", "/v1/audio"]:
            self.assertIn(path, SPEC["paths"], path)

    def test_documented_methods_are_ones_the_gateway_implements(self):
        implemented = {"get", "post", "put", "delete"}
        for path, method, _ in documented_operations():
            self.assertIn(method, implemented, "%s %s" % (method.upper(), path))


class TestSecurityDocumentation(unittest.TestCase):
    def test_a_bearer_scheme_is_declared(self):
        schemes = SPEC.get("components", {}).get("securitySchemes", {})
        self.assertTrue(schemes, "no securitySchemes in the spec")
        self.assertTrue(any(s.get("scheme") == "bearer" for s in schemes.values()))

    def test_the_endpoints_that_serve_files_are_documented_as_protected(self):
        # These read straight off disk with a caller-supplied path. If the spec
        # advertised them as open, an integrator would build on that.
        default = SPEC.get("security")
        for path in ["/v1/outputs/file", "/v1/audio", "/v1/images/file"]:
            op = SPEC["paths"][path]["get"]
            self.assertTrue(op.get("security") or default,
                            "%s is not documented as needing a key" % path)


class TestMusicTierDocumentation(unittest.TestCase):
    def example(self):
        return (SPEC["paths"]["/v1/music/tiers"]["get"]["responses"]["200"]
                ["content"]["application/json"]["example"]["data"]["tiers"])

    def test_the_example_lists_the_tiers_that_exist(self):
        self.assertEqual(sorted(self.example()), sorted(MUSIC_TIERS))

    def test_the_example_names_the_right_models(self):
        for name, tier in self.example().items():
            self.assertEqual(tier["model"], MUSIC_TIERS[name]["model"], name)

    def test_the_example_step_counts_match_the_service_table(self):
        """Was issue #26: the spec said 32 steps where the code said 50.

        A caller sizing a timeout from the spec got it wrong by 60%. Kept as a
        live assertion so the two cannot drift apart again silently.
        """
        for name, tier in self.example().items():
            self.assertEqual(tier["steps"], MUSIC_TIERS[name]["steps"], name)


class TestSpecMatchesTheHandlerSurface(unittest.TestCase):
    """The other direction: routes the gateway answers that nothing documents.

    Only the paths the gateway owns outright are checked. Backend routes are
    upstream's surface, and the spec deliberately covers a curated subset.
    """

    OWN_PREFIXES = ("/v1/press", "/v1/outputs", "/supervisor/", "/v1/music/")
    # Served, but not part of the API contract: the UI, the docs page and the
    # spec itself. Documenting them would suggest they are integration points.
    NOT_API = {"/", "/ui", "/docs", "/openapi.json", "/openapi", "/assets",
               "/health"}

    def test_no_undocumented_gateway_endpoint(self):
        missing = []
        for path in sorted(SUPERVISOR_PATHS):
            if path in self.NOT_API or path in SPEC["paths"]:
                continue
            if path.startswith(self.OWN_PREFIXES):
                missing.append(path)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
