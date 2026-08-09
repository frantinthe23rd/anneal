# Contributing

`CLAUDE.md` is the working notes for this repo — layout, conventions, and the
traps that have already cost time. Read it before starting; it is short, and
most of it exists because something got out.

The parts that most often come up in review:

**Verify against the running system.** Reproduce a bug before fixing it and
confirm the fix afterwards. Several hypotheses here looked obviously right and
were wrong, including one that stood in the README for months.

**API endpoints are written test-first.** Before the handler exists: the
request shape, the `{data, code, error}` envelope, what an unauthenticated call
gets, and each failure mode with its status. Three endpoints have shipped
documented nowhere, and a test written first catches that — you cannot write
one without naming the path, the payload and the response.

**Docs change with code, in the same commit.** An endpoint that gains a
parameter, a status code or a whole path updates `openapi.json` and
`INTEGRATION.md` with it. "Document it next" is how those three got out.

**`ui.html` needs a screenshot, not a read.** A light theme that painted
near-black text onto a near-black plate, a strip that never rendered on first
load, and a scrollbar that scrolled nothing all shipped without raising an
error. `tools/README.md` has the command. Run `tools/lint-ui.py` too — it
catches a different half of the file.

**Never assert against a copied list.** Service names, output kinds and the
health payload all went stale in tests that froze a copy of them, each time
failing after the change shipped rather than during it. Assert against
`services.SERVICES`, `outputs.KINDS` or the source file.

**Commit messages explain why**, including what was measured and what was
rejected.

## Running the tests

```bash
tools/test.sh          # unit always; acceptance too when a gateway answers on 8001
tools/lint-ui.py       # before committing ui.html
tools/lint-prose.py    # reports; --strict fails on the first list
```

The unit suite needs no network, no models and no running server, which is what
lets CI run it with nothing installed. Keep it that way: a test that needs a
binary should skip when it is absent, and the part of it that can be proved
without one should be split out and kept running.

## Pull requests

`main` requires a pull request and a green build. Anything that touches
generated output should say how it was checked — a metric moving the expected
way is not evidence on its own, because noise is broadband and raises
high-frequency energy too.
