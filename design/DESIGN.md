# Anneal — design scheme

Reference for anyone changing `ui.html`.

Files: `design/tokens.css`, `assets/favicon.svg`, `assets/brand/anneal-mark.svg`,
`assets/brand/anneal-mark-mono.svg`, `assets/brand/social-preview.png`.

---

## The one rule

**Heat means hot.** Orange is reserved for six things:

1. the primary action (`Forge`)
2. a service chip that is heating
3. a service chip that is hot
4. live progress
5. the focus ring
6. the wordmark and the mark

Everything else — kind tags, library filters, the selected tab, link hovers, row
hovers — is steel (`--quench`, `--cold`) or plain `--text`. `ui.html` currently
spends orange on about twenty things, which is why a genuinely loaded backend
does not stand out from a filter chip.

## The mark

A broken ring cooling from orange through to steel, around a core that is still
hot: heat applied, then let go of slowly. The gap is the mouth of the forge —
where work goes in. Deliberately not an anvil or a hammer; neither survives 16
pixels, and both have been drawn a thousand times.

Three cuts, not one scaled file:

| File | Use | Geometry |
| --- | --- | --- |
| `anneal-mark.svg` | 32px and up | `r=22`, stroke `9`, gap `16.2`, core `r=5.5`, gradient |
| `favicon.svg` | 16–24px | `r=20`, stroke `11–12`, gap `21.7`, core `r=7`, flat `#ff5f1f`, on a `#100d0c` plate with `rx=14` |
| `anneal-mark-mono.svg` | empty states, print | `currentColor` throughout |

Below 32px the gradient muddies, so the small cut is flat, the stroke thickens
and the core grows. The favicon carries its own dark plate so it holds on a light
and a dark tab strip alike.

On light backgrounds the ramp swaps to its darkened pair (`#e08a12 → #d94a10 →
#5c6d79`) and the core stops being lighter than the ring.

Rotation is `rotate(-24 32 32)`, which puts the gap at the upper right. Keep it —
the gap at the top or the bottom reads as a broken circle rather than an opening.

**Don't:** put the mark inside another container shape, recolour the core to
anything but heat, close the gap, or add a second gap.

## Colour

`design/tokens.css` is authoritative. Paste it over the `:root` block in
`ui.html`; all existing names are preserved, so nothing downstream breaks.

Dark is the default. Light is full parity — the same names, new values, under
`:root[data-theme="light"]`.

Two traps in the light theme:

- `#ffb03a` as text on white is 2.3:1. Light uses `--heat-2: #9a5205`.
- The bottom heat bloom (`body::before`) is dark-only; on light it reads as a
  stain. `tokens.css` already disables it.

`--faint` is for machine metadata — timestamps, byte counts, durations. Never for
anything clickable.

## Type

The system stack stays. Anneal is a local tool with nothing on the network path;
a webfont would be a download at the exact moment the machine is busy, and San
Francisco is the right face for a Mac utility. What was missing is a scale.

| Role | Size | Weight | Tracking |
| --- | --- | --- | --- |
| Hero wordmark | `clamp(38px, 7vw, 76px)` | 600 | `.16em` upper |
| Header wordmark | 19px | 600 | `.14em` upper |
| Album title, sheet heading | 16px | 600 | 0 |
| Body | 15px / 1.5 | 400 | 0 |
| Card body, hints | 13.5px | 400 | 0 |
| Section heading | 12px | 600 | `.11em` upper, `--muted` |
| Field label | 11.5px | 400 | `.09em` upper, `--muted` |
| Button | 13px | 650 | `.1em` upper |
| Machine metadata | 11.5px mono | 400 | 0, `--faint` |

Mono is not decoration: it marks anything the machine measured — durations,
sizes, seeds, model ids, elapsed time. If a human wrote it, it is sans.

## Space, shape, motion

4px grid. In practice: `20` panel padding, `14` card padding and field gap, `24`
column gap, `8` between chips and buttons.

Radius: `10` panels, sheets and images; `8` inputs and buttons; `6` tabs and kind
tags; `999` service chips and filters.

Motion: `.16s` hover, press, focus · `.28s` panel change, drawer · `.5s` hero and
theme change. One easing, `cubic-bezier(.4,0,.2,1)`. Motion only ever shows state
moving — cold to hot, closed to open, unknown to measured. Everything routes
through the three variables so `prefers-reduced-motion` switches the whole
interface to still in one query.

Hit targets: 36px minimum, 44px under a coarse pointer.

### Depth

Six shadow tokens, both themes: `--sh-1` quiet buttons and chips, `--sh-2`
cards and status blocks, `--sh-3` panels and sheets, `--sh-inset` fields, wells and the tab strip, `--sh-press` anything held
or busy, `--sh-hot` the primary button — which carries a warm glow as well as a
drop shadow, because it is the only element in the interface that is meant to look
lit from inside.

Surfaces are a shallow vertical gradient rather than a flat fill — 2–3% lighter at
the top (`--fill-surface`, `--fill-surface-2`). Raised things have a 1px light
edge on top and a dark one underneath; recessed things — text fields, the tab
strip, the audio well, empty states — take `--sh-inset` and read as cut into the
panel. Pressed things swap the light edge for an inner shadow and shift down 1px. Nothing in the interface
should be raised and pressed at once, and nothing sits on more than one level
above its parent.

## Components

Summary:

- **Primary button** — one per view. Heat gradient fill, `--heat-ink` text,
  `brightness(1.09)` on hover, `translateY(1px)` on press. Busy state drops to
  `--surface-2` with an inset hairline; disabled is `opacity .45`.
- **Secondary button** — hairline on `--surface`, `--muted` text going to
  `--text` on hover. The accented variant (`Write for me`) is the only quiet
  button allowed heat, because it starts a generation.
- **Text button** — no chrome, `--muted` to `--heat-2` on hover; `--bad` for
  Delete.
- **Tabs** — the selected tab lifts: a vertical gradient a shade lighter than the
  strip, a hairline ring, a light top edge and a small drop shadow. No accent
  colour, so the tab bar never competes with the heat. Arrow keys must work;
  `role="tablist"` already promises it.
- **Fields** — `--bg` (dark) / `--surface` (light) fill, hairline border, border
  goes `--heat-1` on focus plus the 2px ring at 2px offset. Error borders
  `--bad`, message in `--bad-ink` mono.
- **Service chip** — soft 8px rounded chip, no border, a tinted fill instead: dot,
  plain sentence-case name, mono state. Four states:
  cold (steel dot), heating (`--heat-2` dot, 1.4s breath), hot (`--heat-1` dot
  with a soft 6px bloom, 1.15s pulse), pressure (`--bad`). This is where the heat
  lives; the strip is the story the theme exists to tell.
- **Status block** — message, mono elapsed, then a bar. Indeterminate is the
  sweeping gradient; the moment the backend reports a percentage, switch to
  `--pct` and a solid heat gradient. Add the expected range beside elapsed — a
  five-minute album behind an indeterminate bar reads as hung.
- **Output card** — one component for music, speech, image and chat. Kind tag
  (steel), prompt, mono duration, the payload, then a footer of text buttons with
  mono metadata pushed right. Pending is the same card with a dashed border.
- **Empty state** — the one-colour mark at 40px, `opacity .3`, and a line naming
  the next action *for that tab*.

## While it is generating

Generation takes minutes, so the page itself heats while a job is in flight: two
layers of slow rings rise out of a hearth along the bottom edge and fade out
before they reach the content, over an ember glow that breathes on 5.5s.

- 7s ring cycle, second layer offset 3.5s, so rings never stop arriving.
- Masked out above 74% height — it never touches the panels or the text.
- Fades in over 600ms on the first job, out over 900ms when the queue empties.
- Two composited layers, no per-frame JS. It must not compete with generation.
- Reduced motion drops the rings and the breath; the ember stays as a static
  glow so the state is still readable.
- Dark theme only.

It replaces the existing `body::before` bloom, and `.on` is toggled from the same
place that sets `busy`. Full CSS is in section 07 of the live scheme.

## Applied

The scheme above is what `ui.html` implements: the mark and favicon, the tokens
from `design/tokens.css`, the theme toggle resolved before first paint, orange
rationed to the six uses listed, per-tab empty states, `⌘↵` to forge, arrow keys
across tabs, `Escape` to close any layer, and the hearth behind a generation.

`tools/lint-ui.py` checks token parity between the two themes and fails on a
`var(--x)` with no definition, which is how a colour declared in dark and
forgotten in light gets caught.
