"""What `./stop-api.sh` kills, checked against the service registry.

The script named two ACE-Step processes and nothing else. Speech, image and
text were never in that list — they survived every stop, were reparented to
init, and went on holding their weights while `/health` called them cold. The
list was right when it was written; it went stale the moment a backend was
added, and nothing here or in CI could tell.

So the patterns are derived from `services.SERVICES` now, and these are the
checks that a new service is covered the moment it is declared. They assert
against the table, never against a copy of it.
"""

from __future__ import annotations

import os
import re
import subprocess
import unittest

from tests.context import REPO_ROOT  # noqa: F401

import services
from services import SERVICES

STOP = os.path.join(REPO_ROOT, "stop-api.sh")

with open(STOP, encoding="utf-8") as _fh:
    STOP_SOURCE = _fh.read()


class TestEveryServiceIsCovered(unittest.TestCase):
    def test_each_registered_command_matches_a_stop_pattern(self):
        """The property that would have caught this: for every service in the
        table, something in the stop path matches the process it starts."""
        patterns = services.stop_patterns()
        for name, spec in SERVICES.items():
            argv = " ".join(spec["cmd"])
            self.assertTrue(
                any(re.search(re.escape(p), argv) for p in patterns),
                "nothing in the stop path matches %s: %s" % (name, argv),
            )

    def test_a_new_service_is_covered_without_touching_the_script(self):
        """A service added to the table, with a command shaped like none of the
        existing ones, is still killed — because the pattern comes from `cmd`."""
        spec = {"cmd": ["/opt/venv/bin/python", "/srv/anneal/video_server.py",
                        "--port", "8015"]}
        pattern = services.stop_pattern(spec)
        self.assertEqual(pattern, "/srv/anneal/video_server.py")
        self.assertIn(pattern, " ".join(spec["cmd"]))

    def test_a_pattern_is_specific_enough_to_be_worth_running(self):
        """`pkill -f ''` would match every process on the machine, and `pkill -f
        python` most of this one's. Neither is a stop path."""
        for pattern in services.stop_patterns():
            self.assertGreater(len(pattern), 4, pattern)
            self.assertNotIn(os.path.basename(pattern), ("python", "python3", "uv"))

    def test_the_module_prints_them_for_the_shell(self):
        """stop-api.sh cannot import Python, so it asks."""
        done = subprocess.run(
            ["/usr/bin/python3", os.path.join(REPO_ROOT, "services.py"),
             "--stop-patterns"],
            capture_output=True, text=True, env=dict(os.environ),
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        printed = [line for line in done.stdout.splitlines() if line.strip()]
        self.assertEqual(printed, services.stop_patterns())


class TestTheScriptUsesThem(unittest.TestCase):
    def test_it_asks_the_service_table_rather_than_naming_backends(self):
        self.assertIn("--stop-patterns", STOP_SOURCE)

    def test_no_backend_is_named_in_the_script(self):
        """A literal here is the bug: it cannot know about a service added
        later. The supervisor is the one exception — it is not in the table."""
        killed = re.findall(r'pkill\s+-f\s+"([^"]+)"', STOP_SOURCE)
        self.assertIn("supervisor.py", killed)
        for pattern in killed:
            if pattern == "supervisor.py":
                continue
            self.assertEqual(
                pattern, "$pattern",
                "stop-api.sh names %r; derive it from services.SERVICES instead"
                % pattern)

    def test_it_says_so_when_the_table_cannot_be_read(self):
        """Silence would look exactly like a clean stop."""
        self.assertIn("WARNING", STOP_SOURCE)

    def test_the_supervisor_is_stopped_before_the_backends(self):
        """Otherwise its reaper, or a request in flight, restarts one of them
        between the two kills."""
        self.assertLess(STOP_SOURCE.index('pkill -f "supervisor.py"'),
                        STOP_SOURCE.index("--stop-patterns"))


if __name__ == "__main__":
    unittest.main()
