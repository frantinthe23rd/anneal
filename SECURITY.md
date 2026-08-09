# Security

## What this software is

Anneal runs generative models on one machine and serves them over HTTP. It is
built to be reached from a private tailnet, not from the internet.

Two properties it relies on, both of which you can check:

- **The gateway binds `127.0.0.1` only.** Reach beyond loopback comes from
  `tailscale serve`, which is opt-in (`ANNEAL_EXPOSE=tailnet`). Nothing here
  listens on a LAN interface.
- **Every request needs a bearer token**, generated on first run into
  `env.local.sh`, which is gitignored. Requests arriving over the tailnet are
  additionally identified by Tailscale.

**Do not expose port 8001 to the internet.** There is no rate limiting, no
per-caller quota and no multi-user isolation — a caller with the key can occupy
the machine's only heavy-model slot indefinitely. Fair queueing is tracked as
an enhancement, not implemented.

## Reporting a vulnerability

Open a [private security advisory](https://github.com/frantinthe23rd/anneal/security/advisories/new).
Please do not open a public issue for anything that would let someone reach the
API or the filesystem without the key.

Include what you did, what happened, and whether it needs the key or not. A
report that needs no key is the one worth sending first.

## What is out of scope

- Anything reachable only with a valid API key that costs the machine time
  rather than data — the key is the trust boundary, and there is one shared key
  rather than per-caller credentials.
- The generated output itself. The models are third-party weights, listed with
  their licences in `models.lock.json`; what they produce is not something this
  project constrains.
