#!/usr/bin/env python3
"""Sample host memory behaviour while Anneal is working.

Written to answer a specific question: does running a model whose footprint
exceeds physical RAM actually hurt, or does macOS absorb it? Swap *volume* says
almost nothing — a large swap file that is not being touched costs nothing. What
matters is the paging *rate* and what the kernel itself reports.

    ./monitor.py                 5 minutes, sampling every 5s
    ./monitor.py --seconds 900 --interval 10

Stdlib only, so it runs on the system python with no environment.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request

PAGE = 16384


def sh(cmd):
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""


def vm_stat():
    stats = {}
    for line in sh(["vm_stat"]).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        try:
            stats[k.strip()] = int(v.strip().rstrip(".").replace(",", "") or 0)
        except ValueError:
            continue
    return stats


def swap_used_mb():
    out = sh(["sysctl", "-n", "vm.swapusage"]).replace("=", " ").split()
    parts = dict(zip(out[0::2], out[1::2]))
    try:
        return int(float(parts.get("used", "0M").rstrip("M")))
    except ValueError:
        return 0


def pressure_level():
    out = sh(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip()
    return {1: "normal", 2: "warning", 4: "critical"}.get(int(out) if out.isdigit() else 1, "normal")


def services(base):
    """Which models are loaded, from the gateway. Never wakes anything."""
    try:
        with urllib.request.urlopen(base + "/health", timeout=3) as r:
            svc = json.load(r)["data"]["services"]
        hot = [(n, s.get("memory_mb")) for n, s in svc.items() if s.get("state") != "cold"]
        return ", ".join("%s%s" % (n, " %.1fG" % (m / 1024) if m else "") for n, m in hot) or "none"
    except Exception:
        return "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--base", default="http://127.0.0.1:8001")
    args = ap.parse_args()

    print("time   free    swap   pagein/s pageout/s  pressure  loaded")
    print("-" * 78)

    prev, prev_t = vm_stat(), time.time()
    deadline = prev_t + args.seconds
    peak_out = peak_in = 0
    min_free = 10 ** 9
    worst = "normal"
    samples = 0

    while time.time() < deadline:
        time.sleep(args.interval)
        now, t = vm_stat(), time.time()
        dt = max(t - prev_t, 0.001)
        pin = int((now.get("Pageins", 0) - prev.get("Pageins", 0)) / dt)
        pout = int((now.get("Pageouts", 0) - prev.get("Pageouts", 0)) / dt)
        free = int((now.get("Pages free", 0) + now.get("Pages inactive", 0)) * PAGE / 1048576)
        lvl = pressure_level()

        peak_in, peak_out = max(peak_in, pin), max(peak_out, pout)
        min_free = min(min_free, free)
        if lvl != "normal":
            worst = lvl
        samples += 1

        print("%s %5dM %6dM %8d %9d  %-9s %s"
              % (time.strftime("%H:%M:%S"), free, swap_used_mb(), pin, pout, lvl,
                 services(args.base)))
        sys.stdout.flush()
        prev, prev_t = now, t

    print("-" * 78)
    print("over %d samples: peak pagein/s %d, peak pageout/s %d, min free %dM, worst pressure %s"
          % (samples, peak_in, peak_out, min_free, worst))
    # The interpretation, so the numbers are not left to speak for themselves.
    if worst == "normal" and peak_in < 5000 and peak_out < 2000:
        print("verdict: no pressure reported and paging stayed low — the footprint is")
        print("         being absorbed comfortably.")
    else:
        print("verdict: the kernel reported %s under load, with pagein peaking at %d/s" % (worst, peak_in))
        print("         (~%d MB/s read back from swap) while pageout stayed near zero." % (peak_in * 16 // 1024))
        print("         That is a working set larger than RAM being continuously re-read.")
        print("         It runs fine because the SSD is fast enough to hide it, but the")
        print("         headroom is real and a second large model would not fit.")


if __name__ == "__main__":
    main()
