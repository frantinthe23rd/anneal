#!/usr/bin/env python3
"""What is missing, and how to fix it — before a stack trace has to say it.

Everything Anneal needs used to be discovered by failing: `uv` missing became a
FileNotFoundError from a subprocess; ffmpeg missing became `FileNotFoundError:
2` with nothing naming ffmpeg (see paths.ffmpeg_bin); an unbuilt gen-venv
became "no such file or directory" naming a path the reader had never seen; and
a fresh clone became "is the Storage SSD mounted?", which reads as a hardware
fault rather than a default nobody else can satisfy. Each of those is a
one-line check that names the thing and the command that installs it.

    tools/doctor.py                 everything, grouped
    tools/doctor.py --brief         one line per check
    tools/doctor.py --prereqs       only what setup needs before it can start
    tools/doctor.py --json          machine-readable

Exit status is 1 if any REQUIRED check failed, 0 otherwise — warnings do not
fail, because an optional model or a missing Tailscale is a choice rather than
a fault.

Stdlib only, and run by /usr/bin/python3. It has to work on a machine where
nothing has been installed yet, which rules out every dependency including the
ones Anneal itself uses.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import paths  # noqa: E402  — stdlib only

OK, WARN, FAIL = "ok", "warn", "fail"

# The tested floor, from README → What it runs on. 16 GB is not a
# recommendation: at 16 GB the music model's ~21 GB footprint already exceeds
# physical RAM and pages throughout a generation. Below it, FLUX alone wants
# ~11 GB and nothing about the design helps.
MIN_RAM_GB = 16
HARD_MIN_RAM_GB = 8

# Weights, virtualenvs and wheel cache, before outputs/ (which grows without
# bound). Everything: ~40 GB. A music-and-speech install: ~11 GB of weights.
FULL_DISK_GB = 45
MIN_DISK_GB = 20


class Report(object):
    def __init__(self):
        self.rows = []

    def add(self, group, name, status, detail, fix=None, required=True):
        self.rows.append({"group": group, "name": name, "status": status,
                          "detail": detail, "fix": fix, "required": required})
        return status

    def failed(self):
        return [r for r in self.rows if r["status"] == FAIL and r["required"]]


def _run(argv, timeout=10):
    """stdout of a command, or None. Never raises."""
    try:
        out = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", "replace").strip()


def _sysctl(name):
    return _run(["/usr/sbin/sysctl", "-n", name])


def _gb(n):
    return n / (1024.0 ** 3)


# ------------------------------------------------------------------ hardware

def check_hardware(rep):
    system = platform.system()
    if system != "Darwin":
        rep.add("hardware", "macOS", FAIL, "this is %s" % system,
                "Anneal is macOS + Apple silicon only. Speech, image and text all "
                "run through MLX, and the supervisor's memory management is four "
                "macOS-specific system calls. A port is welcome — see "
                "Contributions in README.md.")
        return
    rep.add("hardware", "macOS", OK, _run(["/usr/bin/sw_vers", "-productVersion"]) or "?")

    machine = platform.machine()
    if machine == "arm64":
        brand = _sysctl("machdep.cpu.brand_string") or "Apple silicon"
        rep.add("hardware", "Apple silicon", OK, brand)
    else:
        # Rosetta makes this worth stating precisely: an x86_64 Python on an
        # arm64 Mac reports x86_64, and the fix is different from "buy a Mac".
        native = _sysctl("hw.optional.arm64") == "1"
        if native:
            rep.add("hardware", "Apple silicon", FAIL,
                    "this Python is x86_64 under Rosetta on an arm64 Mac",
                    "Run doctor (and setup) with a native interpreter: "
                    "arch -arm64 /usr/bin/python3 tools/doctor.py")
        else:
            rep.add("hardware", "Apple silicon", FAIL, "Intel Mac (%s)" % machine,
                    "MLX is Apple-silicon only and there is no fallback path in "
                    "this repo. See 'What it runs on' in README.md.")

    memsize = _sysctl("hw.memsize")
    if memsize and memsize.isdigit():
        ram = _gb(int(memsize))
        if ram >= MIN_RAM_GB - 0.5:
            rep.add("hardware", "memory", OK, "%.0f GB unified" % ram)
        elif ram >= HARD_MIN_RAM_GB:
            rep.add("hardware", "memory", WARN, "%.0f GB — below the tested floor "
                    "of %d GB" % (ram, MIN_RAM_GB),
                    "Music and image will page heavily or fail to load. Speech and "
                    "text are small enough to be usable. Nothing stops you trying.",
                    required=False)
        else:
            rep.add("hardware", "memory", FAIL, "%.0f GB — FLUX alone wants ~11 GB" % ram,
                    "%d GB is the tested floor. Below %d GB no model here fits."
                    % (MIN_RAM_GB, HARD_MIN_RAM_GB))
    else:
        rep.add("hardware", "memory", WARN, "could not read hw.memsize", required=False)


def free_gb(path):
    """Free space on the volume holding the nearest existing ancestor of path."""
    probe = os.path.abspath(path)
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return _gb(shutil.disk_usage(probe).free)
    except OSError:
        return None


def check_disk(rep, root, advisory=False):
    """Free space where the install will go.

    `advisory` is for --prereqs, which setup.sh runs *before* the root has been
    chosen. Failing there would abort the one step whose whole job is to let
    someone point the install at a bigger volume — the internal disk on the
    reference machine has 17 GB free and the external one has 575, and the
    check would have refused the install that works.
    """
    free = free_gb(root)
    if free is None:
        rep.add("hardware", "free disk", WARN, "could not measure %s" % root,
                required=False)
        return
    where = root if os.path.exists(root) else "the volume that will hold " + root
    if free >= FULL_DISK_GB:
        rep.add("hardware", "free disk", OK, "%.0f GB on %s" % (free, where))
    elif free >= MIN_DISK_GB:
        rep.add("hardware", "free disk", WARN,
                "%.0f GB on %s — enough for music and speech, not for everything"
                % (free, where),
                "`./anneal models list` prints the size of each model. About "
                "%d GB is needed for the full set." % FULL_DISK_GB,
                required=False)
    else:
        rep.add("hardware", "free disk", WARN if advisory else FAIL,
                "%.0f GB on %s" % (free, where),
                "At least ~%d GB is needed for a music-and-speech install, ~%d GB "
                "for everything. Point the install at a bigger volume: "
                "./setup.sh --root /Volumes/Something/anneal" % (MIN_DISK_GB, FULL_DISK_GB),
                required=not advisory)


# --------------------------------------------------------------- prerequisites

def check_tools(rep):
    # /usr/bin/python3 is what the gateway runs under, and on a Mac that is the
    # Xcode command line tools' interpreter. Its absence is the CLT's absence.
    clt = _run(["/usr/bin/xcode-select", "-p"])
    if clt:
        rep.add("prereq", "Xcode command line tools", OK, clt)
    else:
        rep.add("prereq", "Xcode command line tools", FAIL, "not installed",
                "xcode-select --install")

    if os.path.exists("/usr/bin/python3"):
        version = _run(["/usr/bin/python3", "-c",
                        "import sys;print('.'.join(map(str,sys.version_info[:3])))"])
        info = tuple(int(p) for p in (version or "0.0.0").split("."))
        if info >= (3, 9):
            rep.add("prereq", "python3 (gateway)", OK, "/usr/bin/python3 %s" % version)
        else:
            rep.add("prereq", "python3 (gateway)", FAIL,
                    "/usr/bin/python3 is %s" % version,
                    "The gateway needs 3.9 or newer. It comes with the Xcode "
                    "command line tools: xcode-select --install")
    else:
        rep.add("prereq", "python3 (gateway)", FAIL, "/usr/bin/python3 missing",
                "xcode-select --install")

    uv = os.environ.get("UV_BIN") or shutil.which("uv")
    if not uv or not os.path.isfile(uv):
        uv = next((c for c in ("/opt/homebrew/bin/uv", "/usr/local/bin/uv")
                   if os.path.isfile(c)), None)
    if uv:
        rep.add("prereq", "uv", OK, "%s (%s)" % (uv, _run([uv, "--version"]) or "?"))
    else:
        rep.add("prereq", "uv", FAIL, "not found",
                "brew install uv   (or: curl -LsSf https://astral.sh/uv/install.sh | sh)\n"
                "  uv builds both virtualenvs and runs the upstream ACE-Step server.")

    try:
        ffmpeg = paths.ffmpeg_bin()
        rep.add("prereq", "ffmpeg", OK, ffmpeg)
    except RuntimeError:
        rep.add("prereq", "ffmpeg", FAIL, "not found",
                "brew install ffmpeg\n"
                "  Needed to transcode MP3 speech and any Press download that is "
                "not FLAC. Under launchd the PATH has no Homebrew on it, which is "
                "why the code resolves it by absolute path — but it still has to "
                "be installed.")

    git = shutil.which("git")
    if git:
        rep.add("prereq", "git", OK, git)
    else:
        rep.add("prereq", "git", FAIL, "not found",
                "xcode-select --install   (setup clones the upstream ACE-Step repo)")


# ------------------------------------------------------------------ install

def check_install(rep, root, lock):
    recorded = None
    try:
        with open(paths.root_file()) as handle:
            recorded = handle.read().strip()
    except OSError:
        pass
    detail = root
    if os.environ.get("AIMUSIC_ROOT"):
        detail += "  (from $AIMUSIC_ROOT)"
    elif recorded:
        detail += "  (from .anneal-root)"
    elif root == paths.LEGACY_ROOT:
        detail += "  (detected existing install)"
    else:
        detail += "  (default — ./setup.sh records a choice in .anneal-root)"

    if not os.path.isdir(root):
        rep.add("install", "root", FAIL, "%s does not exist" % detail,
                "./setup.sh          creates it and everything under it")
        return
    if not os.access(root, os.W_OK):
        rep.add("install", "root", FAIL, "%s is not writable" % detail,
                "chmod, or choose another: ./setup.sh --root <path>")
        return
    rep.add("install", "root", OK, detail)

    acestep = os.environ.get("ACESTEP_DIR") or os.path.join(root, "ACE-Step-1.5")
    pinned = (lock.get("upstream", {}).get("ACE-Step/ACE-Step-1.5", {}) or {}).get("commit")
    if not os.path.isdir(os.path.join(acestep, ".git")):
        rep.add("install", "ACE-Step checkout", FAIL, "%s is not a git checkout" % acestep,
                "./setup.sh   clones it at the pinned commit. Nothing in this repo "
                "used to do that, which is the single largest reason a clone could "
                "not be run by anyone else (#17).")
    else:
        head = _run(["git", "-C", acestep, "rev-parse", "HEAD"])
        if pinned and head == pinned:
            rep.add("install", "ACE-Step checkout", OK,
                    "%s at pinned %s" % (acestep, (head or "")[:12]))
        else:
            rep.add("install", "ACE-Step checkout", WARN,
                    "at %s, models.lock.json pins %s"
                    % ((head or "unknown")[:12], (pinned or "nothing")[:12]),
                    "git -C %s fetch && git -C %s checkout %s" % (acestep, acestep, pinned),
                    required=False)

    link = os.path.join(acestep, "checkpoints")
    if not os.path.isdir(acestep):
        pass
    elif os.path.islink(link):
        rep.add("install", "checkpoints symlink", OK, "%s -> %s" % (link, os.readlink(link)))
    else:
        rep.add("install", "checkpoints symlink", WARN,
                "missing — upstream hardcodes this path and would re-download 9.4 GB",
                "./start-api.sh recreates it on every start.", required=False)

    gen = os.path.join(root, "gen-venv", "bin", "python")
    if os.path.isfile(gen):
        rep.add("install", "gen-venv", OK, gen)
    else:
        rep.add("install", "gen-venv", FAIL, "%s missing" % gen,
                "./update.sh --deps   (or ./setup.sh). Speech, image and text all "
                "run from this environment, and --models needs it too.")

    key = os.path.join(REPO, "env.local.sh")
    if os.path.isfile(key):
        rep.add("install", "API key", OK, "%s" % key)
    else:
        rep.add("install", "API key", WARN, "not generated yet",
                "env.sh writes one the first time anything sources it.",
                required=False)


def check_models(rep, root, lock):
    ckpt = os.environ.get("ACESTEP_CHECKPOINTS_DIR") or os.path.join(root, "models")
    hf = os.environ.get("HF_HOME") or os.path.join(root, "hf-cache")
    for repo_id, spec in lock.get("models", {}).items():
        required = spec.get("required", True)
        service = spec.get("service", "?")
        size = spec.get("size_gb")
        target = spec.get("target", "")
        if target == "checkpoints_dir":
            present = os.path.isdir(ckpt) and bool(os.listdir(ckpt))
            where = ckpt
        elif target.startswith("checkpoints_dir/"):
            where = os.path.join(ckpt, target.split("/", 1)[1])
            present = os.path.isdir(where)
        else:
            where = paths.hf_snapshot(repo_id, spec.get("revision"), hf_root=hf)
            present = where is not None
        label = "%s (%s)" % (repo_id, service)
        if present:
            rep.add("models", label, OK, "%.1f GB" % (size or 0))
        else:
            rep.add("models", label, OK if not required else FAIL,
                    "not downloaded (%.1f GB)" % (size or 0),
                    "./anneal models %s" % service,
                    required=required)
            if not required:
                rep.rows[-1]["status"] = WARN


def check_optional(rep, root):
    sprite_python = os.environ.get("ANNEAL_SPRITE_PYTHON") or \
        os.path.join(root, "tools-venv", "bin", "python")
    if os.path.isfile(sprite_python):
        has_rembg = _run([sprite_python, "-c", "import rembg"]) is not None
        if has_rembg:
            rep.add("optional", "sprite matting (rembg)", OK, sprite_python, required=False)
        else:
            rep.add("optional", "sprite matting (rembg)", WARN,
                    "tools-venv exists but has no rembg",
                    "%s -m pip install 'rembg[cpu]' pillow" % sprite_python,
                    required=False)
    else:
        rep.add("optional", "sprite matting (rembg)", WARN, "tools-venv not built",
                "./setup.sh --tools   (POST /v1/sprites 503s without it; nothing "
                "else is affected). It is a separate environment on purpose — "
                "rembg pulls onnxruntime, and gen-venv is pinned because it "
                "serves the models.", required=False)

    playwright = os.path.join(root, "tools-venv", "bin", "playwright")
    if os.path.isfile(playwright):
        rep.add("optional", "playwright (UI screenshots)", OK, playwright, required=False)
    else:
        rep.add("optional", "playwright (UI screenshots)", WARN, "not installed",
                "./setup.sh --tools. Only needed to work on ui.html — see "
                "tools/README.md.", required=False)

    ts = os.environ.get("TS_BIN") or shutil.which("tailscale") or \
        ("/Applications/Tailscale.app/Contents/MacOS/Tailscale"
         if os.path.exists("/Applications/Tailscale.app/Contents/MacOS/Tailscale") else None)
    if ts:
        rep.add("optional", "tailscale", OK, ts, required=False)
    else:
        rep.add("optional", "tailscale", WARN, "not found — loopback only",
                "Optional. Without it Anneal serves on 127.0.0.1 and nothing else "
                "changes; ANNEAL_EXPOSE=tailnet is what puts it on a tailnet.",
                required=False)


def check_gateway(rep):
    import http.client
    port = os.environ.get("SUPERVISOR_PORT", "8001")
    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=3)
        conn.request("GET", "/health")
        response = conn.getresponse()
        body = response.read()
        if response.status == 200:
            hot = []
            try:
                # Every gateway response is a {data, code, error} envelope.
                # Reading the top level instead of `data` finds nothing and
                # reports "nothing warm" while a model is resident.
                data = json.loads(body.decode("utf-8")).get("data") or {}
                hot = [name for name, svc in (data.get("services") or {}).items()
                       if svc.get("running")]
            except (ValueError, AttributeError):
                pass
            rep.add("gateway", "answering", OK,
                    "127.0.0.1:%s — %s" % (port, (", ".join(hot) + " warm") if hot
                                           else "nothing warm"),
                    required=False)
            return
        rep.add("gateway", "answering", WARN, "HTTP %s on 127.0.0.1:%s"
                % (response.status, port), required=False)
    except Exception:
        rep.add("gateway", "answering", WARN, "nothing on 127.0.0.1:%s" % port,
                "./anneal start", required=False)


# -------------------------------------------------------------------- output

MARK = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}


def render(rep, brief=False):
    if brief:
        for row in rep.rows:
            print("%s  %-52s %s" % (MARK[row["status"]], row["name"], row["detail"]))
        return
    group = None
    for row in rep.rows:
        if row["group"] != group:
            group = row["group"]
            print("\n%s" % group)
        print("  %s  %-52s %s" % (MARK[row["status"]], row["name"], row["detail"]))
        if row["fix"] and row["status"] != OK:
            for line in row["fix"].splitlines():
                print("          %s" % line)


def main(argv):
    brief = "--brief" in argv
    as_json = "--json" in argv
    prereqs_only = "--prereqs" in argv

    root = paths.aimusic_root()
    try:
        with open(os.path.join(REPO, "models.lock.json")) as handle:
            lock = json.load(handle)
    except (OSError, ValueError):
        lock = {}

    rep = Report()
    check_hardware(rep)
    check_disk(rep, root, advisory=prereqs_only)
    check_tools(rep)
    if not prereqs_only:
        check_install(rep, root, lock)
        check_models(rep, root, lock)
        check_optional(rep, root)
        check_gateway(rep)

    if as_json:
        print(json.dumps({"root": root, "checks": rep.rows}, indent=2))
    else:
        render(rep, brief=brief)
        bad = rep.failed()
        print()
        if bad:
            print("%d required check(s) failed: %s"
                  % (len(bad), ", ".join(r["name"] for r in bad)))
        else:
            warns = [r for r in rep.rows if r["status"] == WARN]
            print("Everything required is present.%s"
                  % ("" if not warns else "  %d optional item(s) are not." % len(warns)))
    return 1 if rep.failed() else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
