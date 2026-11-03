# Vendored front-end libraries

Served from `/static/vendor/` so pages load nothing from a CDN (ARCHITECTURE §1, §9).
Files are byte-for-byte copies from the npm registry tarballs; do not edit them.

| File | Package | Version | Licence | Bytes | SHA-256 |
| --- | --- | --- | --- | --- | --- |
| `htmx.min.js` | `htmx.org` (`dist/htmx.min.js`) | 2.0.11 | 0BSD (`LICENSE-htmx.txt`) | 52182 | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |
| `chart.umd.min.js` | `chart.js` (`dist/chart.umd.min.js`, UMD build) | 4.5.1 | MIT (`LICENSE-chart.js.md`) | 208522 | `48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a` |
| `LICENSE-htmx.txt` | `htmx.org` (`LICENSE`) | 2.0.11 | | | `d3d2456f76414f2456104660ebd65aff1c04cd7966b942bdabd63f3cdb316a38` |
| `LICENSE-chart.js.md` | `chart.js` (`LICENSE.md`) | 4.5.1 | | | `41a84aa2caba645f966a18d9c2056b73e6d3a81d80bc0046bc0011a2634d4cce` |

## Sources

| Package | Tarball | Tarball integrity (npm `dist.integrity`, verified) |
| --- | --- | --- |
| htmx.org 2.0.11 | https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz | `sha512-Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==` |
| chart.js 4.5.1 | https://registry.npmjs.org/chart.js/-/chart.js-4.5.1.tgz | `sha512-GIjfiT9dbmHRiYi6Nl2yFCq7kkwdkp1W/lp2J99rX0yo9tgJGn3lKQATztIjb5tVtevcBtIdICNWqlq5+E8/Pw==` |

Fetched with `npm pack <package>@<version>` into a scratch directory (never into the repo); the
tarballs' SHA-512 matched the registry's `dist.integrity`, and the files were copied unchanged.

Notes
- `chart.umd.min.js` ends with `//# sourceMappingURL=chart.umd.min.js.map`. The map is not
  vendored, so browser devtools may log one harmless 404 for it when they are open.
- htmx is configured in `static/js/app.js` (`allowEval=false`, no history cache in browser
  storage, 422 responses swapped) and `base.html` disables its injected indicator styles via
  `<meta name="htmx-config">`. `allowScriptTags` deliberately stays **on**: with it off, htmx
  deletes every `<script>` in swapped HTML, including the `<script type="application/json">`
  chart configs. Inline JavaScript arriving in a swap is still blocked by the CSP
  (`script-src 'self'`); never put `<script src>` in a partial.

## Upgrading

1. `cd $(mktemp -d) && npm pack htmx.org@<version> chart.js@<version>`
2. Compare each tarball's SHA-512 with `npm view <package>@<version> dist.integrity`.
3. Extract, copy `dist/htmx.min.js`, `dist/chart.umd.min.js` and the licence files here.
4. Update this table (`shasum -a 256 <file>`), and `ARCHITECTURE.md` §9 if versions change.
