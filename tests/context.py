"""Put the app modules on the path and point them at a throwaway root.

`supervisor.py` does real work at import time: it reads AIMUSIC_ROOT, opens
`jobs.db` and `presses.db`, and computes the roots it will later serve files
from. Importing it with the ambient environment would therefore open the live
databases on the external volume — so the environment is replaced *before* any
app module is imported. `tests/unit/__init__.py` calls `install()`, which means
importing anything under `tests.unit` has already done it.

Two of the substitutions are safety rather than hygiene:

  SUPERVISOR_PORT   The gateway calls itself on this port (Press goes back
                    through the front door). Left at 8001 a stray unit test
                    would drive the real gateway on this machine and could
                    start a three-minute model load. Pointed at a closed port
                    instead, so any such call fails immediately and loudly.
  ACESTEP_API_KEY   Fixed to a known value so auth can be exercised without
                    ever reading env.local.sh.
  the backend ports Each service's port is moved into the privileged range,
                    where no backend of this machine's can be listening. The
                    supervisor now probes those ports to answer /health — a
                    backend it did not start is still one that is up — so left
                    at their real values a unit test would report on, and could
                    reach, whatever this machine happens to be serving.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Not a secret: it exists only inside the test process.
TEST_API_KEY = "test-key-not-a-secret"

# Deliberately not a port anything listens on.
CLOSED_PORT = "9"

# Likewise for the backends. Below 1024, so nothing a user on this machine
# started can be listening there, and distinct from each other and from
# CLOSED_PORT because the service table requires unique ports.
CLOSED_BACKEND_PORTS = {
    "ACESTEP_BACKEND_PORT": "10",
    "SPEECH_PORT": "11",
    "TEXT_PORT": "12",
    "IMAGE_PORT": "13",
}

_installed = {}


def install():
    """Idempotently sandbox the environment. Returns the sandbox root."""
    if _installed:
        return _installed["root"]

    root = os.environ.get("ANNEAL_TEST_ROOT")
    if root:
        os.makedirs(root, exist_ok=True)
    else:
        root = tempfile.mkdtemp(prefix="anneal-tests-")
        atexit.register(shutil.rmtree, root, ignore_errors=True)

    os.environ["AIMUSIC_ROOT"] = root
    os.environ["ACESTEP_DIR"] = os.path.join(root, "ACE-Step-1.5")
    os.makedirs(os.environ["ACESTEP_DIR"], exist_ok=True)
    os.environ["IMAGE_OUTPUT_DIR"] = os.path.join(root, "outputs", "images")
    os.environ["ACESTEP_API_KEY"] = TEST_API_KEY
    os.environ["SUPERVISOR_HOST"] = "127.0.0.1"
    os.environ["SUPERVISOR_PORT"] = CLOSED_PORT
    os.environ.update(CLOSED_BACKEND_PORTS)
    os.environ["TAILNET_HOST"] = ""
    # Inherited values would make the auth and tier tests depend on the shell
    # that launched them.
    os.environ.pop("ANNEAL_ALLOWED_LOGINS", None)
    os.environ.pop("ANNEAL_MUSIC_TIER", None)

    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    _installed["root"] = root
    return root


def sandbox_root():
    return install()
