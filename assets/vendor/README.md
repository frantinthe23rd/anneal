# Vendored browser libraries

`ui.html` makes **no external requests**, so anything the page needs is served
by the gateway from here rather than from a CDN. These two are used only to
render the chat transcript: the local model replies in Markdown, and rendering
model output as HTML without a sanitiser is not something to hand-roll.

| File | Upstream | Version | Licence |
| --- | --- | --- | --- |
| `marked.min.js` | [markedjs/marked](https://github.com/markedjs/marked) | 15.0.7 | MIT |
| `dompurify.min.js` | [cure53/DOMPurify](https://github.com/cure53/DOMPurify) | 3.2.4 | Apache-2.0 / MPL-2.0 |

Fetched 2026-08-06 from jsDelivr, unmodified. Verify before trusting a copy:

```
934e3e36e9e2da0afb1a6e75075bb0f09af05293a844e84a7477ef40911c349a  marked.min.js
8eb41b658831fab175fad9bcd00fcb2d84e0ed3a25a55053d4ecd4444b8b43a0  dompurify.min.js
```

```bash
shasum -a 256 assets/vendor/*.js
```

To take a newer version, download it, replace the file, update the version and
hash above, and check the chat transcript still renders — `marked.parse()` output
is passed through `DOMPurify.sanitize()` in `ui.html`, and both APIs have moved
between major versions before.
