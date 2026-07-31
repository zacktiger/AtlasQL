# Vendored libraries

Checked in rather than installed, because the frontend deliberately has no build
step and no package manager: `atlasql.api` serves `frontend/` as static files
and that is the whole deployment story. Two UMD bundles, 53 kB together:

| File | Version | Why |
|---|---|---|
| `d3-geo.v3.min.js` | d3-geo 3.1.1 | Orthographic projection, spherical clipping at the horizon, `geoPath` to canvas, `geoBounds`/`geoCentroid`/`geoContains` on the sphere. |
| `d3-array.v3.min.js` | d3-array 3.2.4 | The only dependency of d3-geo. |

Both are ISC-licensed, Copyright Mike Bostock. Load `d3-array` first: each
bundle attaches to `globalThis.d3`, and d3-geo's UMD wrapper resolves its
dependency from that same object.

Spherical clipping is the reason for the dependency rather than hand-rolled
projection maths. Projecting a lon/lat to a globe is a dozen lines; correctly
clipping a polygon that runs over the horizon, and rendering the antimeridian
without a country smearing across the screen, is not.

Upstream: <https://cdn.jsdelivr.net/npm/d3-geo@3/dist/d3-geo.min.js>,
<https://cdn.jsdelivr.net/npm/d3-array@3/dist/d3-array.min.js>
