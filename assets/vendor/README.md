# Vendored browser libraries

`ui.html` makes **no external requests**, so anything the page needs is served
by the gateway from here rather than from a CDN. These two are used only to
render the chat transcript: the local model replies in Markdown, and rendering
model output as HTML without a sanitiser is not something to hand-roll.

| File | Upstream | Version | Licence |
| --- | --- | --- | --- |
| `marked.min.js` | [markedjs/marked](https://github.com/markedjs/marked) | 15.0.7 | MIT |
| `dompurify.min.js` | [cure53/DOMPurify](https://github.com/cure53/DOMPurify) | 3.2.4 | Apache-2.0 / MPL-2.0 |
| `katex/` | [KaTeX](https://github.com/KaTeX/KaTeX) | 0.16.22 | MIT |

`katex/` is the typesetter for equations in chat replies, and it is the heaviest
thing the page loads: 272 KB of JavaScript, 24 KB of CSS and 296 KB of fonts.
That is justified here and would not be on the open internet — the page is served
by the same machine that renders it, over a tailnet, with no metered link in
between.

**Only the `.woff2` fonts are vendored**, which takes the font payload from
1.1 MB to 296 KB. KaTeX's CSS lists `woff2`, `woff` and `ttf` in that order in
every `@font-face`, and a browser stops at the first format it supports, so the
absent files are never requested. Anything old enough to lack woff2 cannot run
this interface anyway. Serving them needs the font content types in
`supervisor.py`'s asset route.

Fetched 2026-08-06 (marked, DOMPurify) and 2026-08-07 (KaTeX) from npm and
jsDelivr, unmodified. Verify before trusting a copy:

```
934e3e36e9e2da0afb1a6e75075bb0f09af05293a844e84a7477ef40911c349a  marked.min.js
8eb41b658831fab175fad9bcd00fcb2d84e0ed3a25a55053d4ecd4444b8b43a0  dompurify.min.js
e8d885505949f3a5f4abdd5dd0d53696bd1371ad26ffbf4f310dcd77c8cdae89  katex/katex.min.js
19095127357ed6d29fe0a63a6b000c913a89f7f1963b765dd3715e97c9852e75  katex/katex.min.css
```

```bash
shasum -a 256 assets/vendor/*.js
```

To take a newer version, download it, replace the file, update the version and
hash above, and check the chat transcript still renders — `marked.parse()` output
is passed through `DOMPurify.sanitize()` in `ui.html`, and both APIs have moved
between major versions before.
