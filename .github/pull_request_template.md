## What changed, and why

<!-- The why is the part that is hard to recover later. Include what you
     measured and what you rejected: several decisions in this repo only make
     sense with the measurement attached. -->

## How it was verified

<!-- Reproduce a bug before fixing it and confirm the fix afterwards. Reading
     the code is not verification — several hypotheses here looked obviously
     right and were wrong. Paste the before/after where it fits. -->

- [ ] `tools/test.sh` passes
- [ ] Endpoint changes were written test-first, and `openapi.json` and
      `INTEGRATION.md` changed in the same commit
- [ ] `ui.html` changes were screenshotted, and `tools/lint-ui.py` passes
- [ ] No copied lists — service names, output kinds and health payloads are
      asserted against their source, not a frozen copy
