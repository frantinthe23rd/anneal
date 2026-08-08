#!/usr/bin/env python3
"""Count the words that characterise the writing instead of informing the reader.

Reports; it does not fail. Every word here has a legitimate use and the
judgement is the whole point — a build that blocks on "deliberately" would just
teach people to write around it. What this fixes is not knowing: three review
passes were needed to find this pattern by hand, and by then it was in the
README, the API spec, the UI and the issue tracker.

Two lists, because they need different treatment:

  DROP     Almost always self-characterisation. "Experimental, and honestly so"
           asks the reader for credit for the word "experimental". A document is
           honest by being accurate; announcing it argues the other way.

  SUSPECT  Context decides. In a code comment "this is deliberate" is
           load-bearing — it stops the next person fixing what is not broken. In
           user-facing copy it usually introduces a defence of a decision nobody
           challenged.

By default it reads only what a stranger sees, and reads it the way they do:
script blocks and HTML comments are stripped from ui.html, and only `summary`
and `description` strings are taken from openapi.json. Pass --all to include
code and tests, where a higher count is expected and often correct.

    tools/lint-prose.py              user-facing prose
    tools/lint-prose.py --all        everything tracked
    tools/lint-prose.py --strict     exit 1 if anything in DROP is present
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DROP = [
    "honest", "honestly", "frankly", "to be fair", "in fairness",
    "worth knowing", "worth recording", "worth noting", "the useful part",
    "the real question", "needless to say", "it goes without saying",
]
SUSPECT = ["deliberately", "deliberate", "genuinely", "actually", "of course",
           "importantly", "crucially", "notably"]

# What a visitor reads. Everything else is for contributors, where the same
# words are usually doing real work.
PUBLIC = ["README.md", "INTEGRATION.md", "ui.html", "openapi.json",
          "tools/README.md", "design/DESIGN.md", "assets/vendor/README.md"]


def visible_text(path):
    """The prose a reader actually sees, with code stripped out.

    Without this the tool flags its own kind of comment: ui.html's JavaScript is
    full of "this is deliberate" notes that exist precisely to stop someone
    undoing a fix, and openapi.json is mostly keys and schemas.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    if path.endswith(".json"):
        try:
            doc = json.loads(raw)
        except ValueError:
            return raw
        out = []

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("description", "summary", "title") and isinstance(value, str):
                        out.append(value)
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
        walk(doc)
        return "\n".join(out)
    if path.endswith(".html"):
        raw = re.sub(r"<script\b.*?</script>", "", raw, flags=re.S | re.I)
        raw = re.sub(r"<style\b.*?</style>", "", raw, flags=re.S | re.I)
        raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    return raw


def scan(paths):
    findings = []
    for path in paths:
        full = os.path.join(HERE, path)
        if not os.path.isfile(full):
            continue
        text = visible_text(full)
        skipping = False
        for lineno, line in enumerate(text.splitlines(), 1):
            # Documentation about this tool has to quote the words it looks for,
            # and a checker that flags its own manual teaches people to ignore
            # it. `lint-prose: off` / `on` brackets a block; `lint-prose: skip`
            # exempts one line.
            if "lint-prose: off" in line:
                skipping = True
                continue
            if "lint-prose: on" in line:
                skipping = False
                continue
            if skipping or "lint-prose: skip" in line:
                continue
            low = line.lower()
            for group, words in (("drop", DROP), ("suspect", SUSPECT)):
                for word in words:
                    if re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(word), low):
                        findings.append((path, lineno, group, word, line.strip()))
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="include code and tests")
    ap.add_argument("--strict", action="store_true", help="exit 1 if any DROP word is present")
    ap.add_argument("paths", nargs="*", help="specific files instead of the defaults")
    args = ap.parse_args(argv)

    if args.paths:
        paths = args.paths
    elif args.all:
        tracked = subprocess.run(["git", "ls-files"], cwd=HERE,
                                 capture_output=True, text=True).stdout.split()
        paths = [p for p in tracked
                 if p.endswith((".md", ".py", ".sh", ".html", ".json"))
                 and "assets/vendor/" not in p or p == "assets/vendor/README.md"]
    else:
        paths = PUBLIC

    findings = scan(paths)
    if not findings:
        print("Nothing flagged in %d file(s)." % len(paths))
        return 0

    drops = [f for f in findings if f[2] == "drop"]
    for path, lineno, group, word, line in findings:
        mark = "DROP   " if group == "drop" else "suspect"
        print("%s %s:%d  %s" % (mark, path, lineno, line[:96]))

    per_file = {}
    for path, _l, group, _w, _t in findings:
        per_file.setdefault(path, [0, 0])[0 if group == "drop" else 1] += 1
    print("\n%-24s %6s %8s" % ("", "drop", "suspect"))
    for path in sorted(per_file):
        print("%-24s %6d %8d" % (path, per_file[path][0], per_file[path][1]))
    print("\n%d to drop, %d to weigh. Neither is a build failure; read them."
          % (len(drops), len(findings) - len(drops)))
    return 1 if (args.strict and drops) else 0


if __name__ == "__main__":
    sys.exit(main())
