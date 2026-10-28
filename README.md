# DingHai basemap generators

This repository holds two development-side producers. Neither runs in the App.

- **GSHHG marine background** (`gshhg_product.py`, `audit_product.py`) —
  python-product-2, described first below.
- **Seamap land cells** (`land_product.py`, `audit_land.py`) — land-product-1:
  one pmtiles per Seamap z8 cell with simplified OpenStreetMap land and
  illustrative offshore depth, described in
  [Seamap land cells](#seamap-land-cells--land-product-1).

## GSHHG marine background — python-product-2

This is the DingHai marine-background producer. It runs on a development computer,
not in the App. It extends Marine Imagery's existing Python/Shapely projection,
world splitting and UInt16 encoding with official binary records, five LODs
and a disk-backed fragment spool. It does not import any
other Marine Imagery module. No App, map renderer, accounts, chart converter,
Clipper/C++ runtime or general GIS framework is included.

```text
official gshhg-bin-2.3.7.zip (verified whole body)
  → select exactly five gshhs_{c,l,i,h,f}.b members; verify ZIP CRC and bytes
  → one binary record → longitude continuity → polar/latitude/world clipping
      coast: L1–4, every L5 → land L1, every L6 excluded (ice-front)
      fill: all retained hierarchy levels, polar closure only for fill
      stroke: L1 and L5 original source paths only; no inland L2–4 outlines
  → row then tile clipping → 65535-unit UInt16 coordinates
  → disk fragments → ordered one-group aggregation → schema 2 → quick_check
  → atomically replace DingHaiCoastlines.sqlite
```

The adopted marine-background scope is exactly
`family='coast' AND (kind=0 OR level=1)`: all five resolutions of sea/land/lake
fills and sea coasts/island outlines remain. Rivers, national/administrative
borders and inland water outlines are not produced or downloaded. There is no
full-product mode, legacy fallback, dual write or App-side hiding switch.
The official input ZIP still contains WDB files, but the producer never
extracts or converts them.

The geometry operations are Python/GEOS, not the App's integer Clipper engine.
The retained geometry pipeline and quantization are unchanged from
python-product-1; release verification compares every retained SQL field and
geometry byte against that published Python product. Whole-database hashes
are expected to change with scope and provenance metadata. Neither this
comparison nor the fixtures promise exhaustive worldwide geography or
equivalence to the former App-produced database.

### Rebuild in an empty directory

Unpack the corresponding-source ZIP into an empty directory. The source archive
contains this directory only; no private repository checkout is required.
The verified environment is CPython **3.12.7**, Shapely **2.0.6**, NumPy **2.5.3**,
GEOS **3.11.4** (macOS arm64 wheel), system SQLite **3.45.3**. These are desktop
measurements, not iPhone costs. Install a matching Python interpreter first.
No GDAL, GMT, compiler, App SDK, parallel pool or service is needed.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
curl --fail --location \
  https://www.soest.hawaii.edu/pwessel/gshhg/gshhg-bin-2.3.7.zip \
  --output gshhg-bin-2.3.7.zip
.venv/bin/python gshhg_product.py \
  --official-archive gshhg-bin-2.3.7.zip --source-version 2.3.7 \
  --output DingHaiCoastlines.sqlite
.venv/bin/python audit_product.py DingHaiCoastlines.sqlite
```

The CLI verifies the official archive before conversion:

- URL: `https://www.soest.hawaii.edu/pwessel/gshhg/gshhg-bin-2.3.7.zip`
- Whole body: **118617033 bytes**
- SHA-256: `28600e8f7a08645aab43079326df6504212ec5ccb2b4bcf3b5f4f12ed60e82bc`
- Selected members: `gshhs_{c,l,i,h,f}.b`, exactly once each, flat paths only.

ZIP member CRC/bytes and each member's actual SHA-256, record/level counts are
written to database metadata, along with the archive hash, source version and
generator file hash. No source-master revision is claimed as binary identity.
The official archive is the editable data input; obtain the fixed body above,
not a source-checkout or Shapefile package. Dependencies are not vendored:
Shapely and NumPy distributions and source are available from their versioned
PyPI projects; no dependency implementation is incorporated in the SQLite.

For the included small **official-source** fixture conversion:

```sh
.venv/bin/python gshhg_product.py --source-version 2.3.7-fixture \
  --input coast:c:tests/fixtures/gshhs_c.b \
  --input coast:f:tests/fixtures/gshhs_f-controls.b \
  --output fixture.sqlite
```

Explicit-file mode records its scope as `explicit-file-inputs`, never as the
official five-member source selection. Release metadata uses
`source_scope=official-coast-5`, `product_scope=marine-background` and the
retained-row predicate above, never the old `official-complete-15` label.
The fixture set contains the unchanged whole c coast member and two f polar
records (L5 ice-front and L6 grounding). Routing c into other LODs in tests
exercises chunk/LOD behavior only; it is not full-global equivalence evidence.

### Output contract and release

| Display zoom | Resolution | Chunk zoom | Family |
| --- | --- | --- | --- |
| 0–3 | c | 2 | coast |
| 4–5 | l | 3 | coast |
| 6–7 | i | 4 | coast |
| 8–10 | h | 5 | coast |
| 11+ (stored maximum 30) | f | 6 | coast |

Schema 2 uses `metadata`, `lods`, `features`; the latter keys a blob by
resolution/tile/family/level/kind. Kind 0 is polygon rings; kind 1 is source
polylines, present only at level 1. Encoding is
`dinghai-quantized-web-mercator-v1`, UInt16 maximum 65535,
with little-endian UInt32 counts. Higher coast hierarchy levels overlay lower
ones; odd levels are land and even levels water. The `family` field remains
part of schema 2 and is always `coast`. The spool is dropped from the finished
database.

Release packaging is a ZIP containing only `DingHaiCoastlines.sqlite`, DEFLATE
level 9, fixed member timestamp `1980-01-01 00:00:00`, regular-file mode 0644.
Record actual DB/ZIP bytes and SHA-256, compression time, then physically unpack
and rehash before release. The corresponding-source ZIP and NOTICE/license
texts must be offered alongside the data. `audit_product.py` independently
uses only Python stdlib to decode every stored blob and check integrity,
routing, marine-only scope, five official source members, counts, polar
strokes and necessary full-resolution land/sea controls.
It is not a proof of exhaustive worldwide geography or App display behavior.

**Payload/manifest hosting decision: Cloudflare only.** The resource root is
`https://resources.dinghai.app/gshhg/`, with the file entry point
`https://resources.dinghai.app/gshhg/manifest.json`. That manifest declares
datasetVersion `gshhg-2.3.7-python-product-2` with payload
`https://resources.dinghai.app/gshhg/releases/gshhg-2.3.7-python-product-2/gshhg-2.3.7-python-product-2.zip`
(82470346 bytes, SHA-256
`0389dd824f875179048d8aa1165a8d194d2407e875360a87fe272a376a7000b3`).
The corresponding-source host is one **independent minimal public GitHub
repository**, `https://github.com/jkqq147-team/dinghai-basemap-generator`. It contains only
this standalone generator, audit, necessary tests/fixtures, rebuild instructions,
dependency lock and licenses, never the DingHai App or all of Marine Imagery.
For each dataset version, use the same fixed tag/release and attach the matching
minimal source ZIP. No basemap payload ZIP is uploaded to GitHub, and there is
no CF source fallback or second payload source for this new version.

The fixed tag `gshhg-2.3.7-python-product-2` points at commit
`51e1c2fd33c139d4b6f3972d0e71b6d4ef68752b`. Its published GitHub release
carries `gshhg-product-2.3.7-python-product-2-source.zip` (2680446 bytes,
SHA-256 `159d67ddceb98133706d651a40968ff5d6a893d6c117b048c6adf4ee903d306f`).
The CF manifest's `correspondingSource` names that release asset with the same
bytes and SHA-256; its `noticeURL` and `licenseURLs` point at `NOTICE.txt` and
`Licenses/` at the fixed tag:

```text
dataset/tag/release: gshhg-2.3.7-python-product-2
  CF payload/manifest → gshhg-2.3.7-python-product-2.zip / manifest.json
  GitHub release      → gshhg-product-2.3.7-python-product-2-source.zip
  GitHub fixed tag    → NOTICE.txt / Licenses/
```

The README.md and NOTICE.txt inside that tag and source ZIP were written before
publication and still describe these links as planned and unpublished. They are
left unchanged so the published tag and ZIP stay fixed; this text on `main`
describes the published state.

Corresponding source and licenses must remain accessible for the exact data
version, without relying on a resource HTML page or a source/license entry
on the offline chart page. The DingHai App reads this manifest and accepts only
the marine-background scope. Since App commit `b5d94ab`, the GSHHG section of
its existing legal notices links the installed basemap's manifest
`correspondingSource` URL, labelled with its dataset version; the offline chart
page no longer carries that link and retains basemap download management.
This repository makes no App change.
No App support/privacy page or broader privacy/legal work is part of this release.
Do not overwrite python-product-1 artifacts; do not delete the existing v1 CF
source. This tool neither reads credentials nor creates repositories, pushes,
uploads, deploys or creates services. Do not publish the private repository or
unrelated tools. Anonymous resource delivery is not a promise that a host
cannot see request metadata.

A fixed product can remain static. Regenerate only for an adopted official-data
version, a conversion fix or a reader-format change; keep download links and
corresponding source/licensing available. There is no scheduled rebuild/CI
framework here and no promise of permanent zero maintenance.

### License and corresponding source

GSHHG is by **Paul Wessel and Walter H. F. Smith**, distributed under
**LGPL-3.0-or-later** (official `Licenses/GSHHG-NOTICE.txt`). This derived resource
uses version 3. This standalone generation code and modifications are offered
under **LGPL-3.0-only**; the same grant applies to the audit and tests. GNU LGPLv3
incorporates GPLv3; both full texts are included. There is no Clipper dependency
or Clipper code in this implementation.

GPLv3 §1 defines Corresponding Source as the preferred form for modification,
including scripts needed to generate the covered work; §6(d) requires equivalent
source access when conveying object code over a network. LGPLv3 incorporates
those terms and adds its permissions. Conservatively, this distribution offers
the generator, audit, tests, fixed build/input instructions and license notices
alongside the transformed data. This is a **minimal generator/data source
boundary**, not a claim that the private App is open source or that this notice
is an adjudication of every legal classification. Publication must retain that
boundary and source availability.

GSHHG reference: Wessel, P., and W. H. F. Smith (1996), *A global,
self-consistent, hierarchical, high-resolution shoreline database*,
J. Geophys. Res., 101(B4), 8741–8743. This is a general geographic basemap,
**not an ENC or a navigation chart**, provided without warranty or support.

## Seamap land cells — land-product-1

`land_product.py` cuts one pmtiles per Seamap z8 cell (the App's
`SeamapCell`, `gridZoom = 8`: XYZ tile x/y at zoom 8), z8–12, from local
copies of one Protomaps build and the GEBCO_2026 grid. It runs on a
development computer; it reads local files only and never uploads.

```text
local z0-12 extract of build B ─┬─ z8-12 tiles of the cell ── landuse / roads / places
                                │                              (+ z12 earth: depth land mask)
local z13 peaks of build B ─────┴─ z13 pois kind=peak ─────── lifted into z12 peaks
GEBCO_2026.nc ── bilinear depth on z12 world-pixel nodes ──── depth bands
          │
          └→ releases/<release>/cells/8/{x}/{y}.pmtiles
             releases/<release>/cells.json, manifest.json
```

### Layers

| Layer | Source | Contents |
| --- | --- | --- |
| `landuse` | Protomaps `landuse` | `class` wood/park/farmland/urban/industrial/beach, merged per class per tile and clipped to that tile's Protomaps `earth` |
| `roads` | Protomaps `roads` | `class` highway; major (trunk/primary); secondary (secondary/tertiary, z11+) |
| `peaks` | Protomaps `pois` kind=peak | `ele` (integer m); z12 holds the build's z12 peaks plus those of the peaks file's zooms (z13 for releases), deduplicated; peaks without a numeric elevation are dropped |
| `places` | Protomaps `earth` kind=island points, `places` kind=locality with kind_detail town/village/locality | `kind`, `name` = `name:zh-Hans`, else `name:zh`, else `name` |
| `depth` | GEBCO_2026 | `min` = band floor (20/30/50/100/200 m), cumulative polygons |

Not produced: `earth`, `water`, `boundaries`, `buildings`, `transit`, other
`pois`, minor roads, paths, rail, ferries. `earth` is left out because Seamap
already draws land, and both uses of a land mask are applied here at
generation time (landuse is clipped to land; depth is ≥1.5 km offshore). The
App therefore needs no second land polygon from this file; keeping it would
cost about 0.12 MB per cell (prototype measurement) and could disagree with
Seamap's own coastline.

Protomaps carries islands as `earth` points, not in `places`; Protomaps
4.15.2 has no `name:zh` key (only `name:zh-Hans`/`name:zh-Hant`), so the
`name:zh` step only applies if a build adds it.

### Depth: illustrative, not surveyed

Depth bands are interpolated from the GEBCO_2026 15″ grid. They are
**illustrative, not a survey and not for navigation** (`示意、非测量、不可用于导航`);
the metadata of every cell and the manifest say so.

A point is coloured with band *L* only if, at the zoom it is drawn:

- bilinear GEBCO depth ≥ *L* (m below GEBCO's mean-sea-level reference), and
- ground distance to OSM land (Protomaps z12 `earth`) ≥ 1500 m,

and pieces smaller than 1 km² are dropped (holes are kept: filling a hole
would claim depth where GEBCO is shallower). The producer works on z12 world
pixels (4096 per cell side) and holds margins so that rasterising,
contouring, generalising and MVT rounding can only shrink a band: +2 m depth,
3 px distance, and each zoom's lattice (stride *s* = 2^(12−z)) takes the
minimum over a Chebyshev radius *s*+2. The land mask includes 1–3 rings of
neighbouring z12 tiles (at least 1.25 × 1.7 km at the cell's latitude), so a cell
sees the same coast as its neighbours, and GEBCO is read beyond the cell, so
nothing is truncated at the cell edge. Depth pooling takes the exact minimum
of the continuous bilinear GEBCO field over each rectangle, not just its
integer z12 samples: original GEBCO latitude/longitude node lines split the
rectangle into bilinear subrectangles whose minima occur at corners. The
producer enumerates these corners for each z12 pixel rectangle, then pools
those minima over the requested rectangle. Longitudes wrap periodically at
±180°; the existing +2 m margin is unchanged. Bands are contoured on the shared
lattice, snapped, then clipped to tiles and end exactly on the cell edge; two
adjacent cells produce the same edge (`tests/test_land.py` seam test).

Fragments are judged on a shared window: each cell computes its lattice
256 z12 px (one z12 tile; ≈8.6 km at 28° N, ≈4.9 km at 60°) beyond its
edges and finds connected vector components after final integer snapping
and geometry repair, before clipping to tiles. Area is the snapped polygon
area (holes subtracted), converted using the minimum ground scale over its
latitude extent. A narrow connection that collapses during snapping therefore
leaves separate components, each judged once; there is no pre-snap fragment
filter. The keep/drop decision is a
property of the whole component, not of the cell asking: the touched cells
are those whose closed extents, including shared seams, intersect it.
For a continuous closed extent [lo, hi], their indices run from
ceil(lo / 4096) − 1 through floor(hi / 4096): a point exactly on a seam belongs
to both cells, but even 1/16 px inside it belongs only to that side.
Only a complete component strictly inside every one of those cells' windows
is eligible for area-based dropping; if it covers < 1 km² it is dropped
everywhere, otherwise it is kept. A component clipped by an observation
window, or extending beyond the shared measurable window, is kept by every
cell that draws it. This includes thin fragments extending beyond the window
even when they touch no border; that is intentional, consistent behaviour.
Strict inequalities and inclusive seam-cell membership are both required:
an exact border point is potentially truncated, and a seam-start point belongs
to both adjacent cells (`FragmentPropertyTests`).

### Inputs (one release)

1. **Protomaps z0–12**, one local extract of build *B* (only five range
   requests; build list at `https://build-metadata.protomaps.dev/builds.json`,
   builds are kept about one week):
   `pmtiles extract https://build.protomaps.com/B.pmtiles protomaps-B-z0-12.pmtiles --maxzoom=12`
   — about 18 GB (Main's go-pmtiles 1.31.2 dry run of 20261025: 6,316,878 tiles).
2. **Peaks z13** of the same build, only over the coastal cells, reduced to
   `pois` kind=peak:
   `pmtiles extract https://build.protomaps.com/B.pmtiles peaks-raw-B.pmtiles --region=coastal-region.geojson --minzoom=13 --maxzoom=13`,
   then `land_product.py peaks peaks-raw-B.pmtiles --output peaks-B.pmtiles`
   (`--zooms 13` is the default; the file records its zooms and the manifest
   repeats them). Dry run of 20261025 over the 14,335 coastal cells:
   12,403 requests, 18 GB transferred for a 17 GB archive. The raw extract is
   transient; the peaks file is small (sample, z13: 10 kB for Dachen's cell,
   39 kB for Hong Kong's). `peaks` refuses inputs from another build or that
   do not cover the requested zooms. In the samples z13 holds all 207 of
   Dachen's peaks and 1883 of the 1956 Hong Kong peaks at z14 (96 %); adding
   z14 (`--maxzoom=14`, `--zooms 13,14`) would cost 48 GB and 14,090 requests
   for those 4 %. z12-only peaks are not an option: the prototype measured 0
   (Dachen) and 18 (Hong Kong) peaks at z≤12. The test fixture keeps z13–14
   so the lift is tested over both allowed zooms.
3. **GEBCO_2026** netCDF (`GEBCO_2026.nc`, 7,466,018,396 bytes per the
   CEDA response recorded for the prototype), from
   `https://dap.ceda.ac.uk/bodc/gebco/global/gebco_2026/ice_surface_elevation/netcdf/GEBCO_2026.zip?download=1`.
4. **Coastal cell list** from a local Seamap z6 extract (`seamap-z6.pmtiles`,
   `land` and `seamark`/`light` layers): `land_product.py cells` marks a
   z8 cell coastal when its 16×16 px part of the z6 land raster is partly
   land, plus cells with a z6 seamark/light point. The 2026-09-28 Seamap z6
   extract gives 14,196 coastal cells, 14,335 with seamark cells.

   The App's `SeamapStore.swift` derives the archive URL from the TileJSON:
   the date in `description` (`"Open Waters Seamap 2026-09-28, from
   OpenStreetMap data 2026-09-21"`), appended as `<date>.pmtiles` to the
   TileJSON's directory. The following commands reproduce that derivation
   and extract only z6 from the full archive:

   ```sh
   curl --fail --location -o seamap-tiles.json \
     https://tiles.openwaters.io/seamap/tiles.json
   SEAMAP_DATE=$(python3 -c 'import json,re; d=json.load(open("seamap-tiles.json")); print(re.search(r"Seamap ([0-9-]+)", d["description"]).group(1))')
   pmtiles extract "https://tiles.openwaters.io/seamap/${SEAMAP_DATE}.pmtiles" \
     seamap-z6.pmtiles --minzoom=6 --maxzoom=6
   ```

   On 2026-10-26 the date was `2026-09-28` and the archive HEAD reported
   25,959,907,224 bytes. The z6 extract took five requests and is 31,401,941
   bytes, SHA-256
   `78cbe067445484bca4c688d53119a4fcabe3e85bdecb65719536beb819cea3f8`.
   It is byte-for-byte identical to the prototype's
   `/tmp/dh-seamap-r2/data/seamap-z6.pmtiles`: same pmtiles header and
   metadata, same 2,508 tiles with identical decompressed tile hashes.
   Its derived coastal inventory is also identical (14,196 coastal cells,
   14,335 including seamark cells; 0 added and 0 removed). The prototype
   file therefore came from this same `2026-09-28` archive, not an older
   one. The cell list and `coastal-region.geojson` follow whichever Seamap
   build `tiles.json` currently points at; always derive the date at run
   time, record it, and regenerate both together when that build changes.

Protomaps' own advice is to copy a build rather than hotlink it; each input
above is read from the build once per release, never per cell.

### Rebuild in an empty directory

Same interpreter as above (CPython 3.12.7, macOS arm64 wheels in
`requirements.txt`). Additionally needed: go-pmtiles (verified with 1.31.2)
and about 60 GB free disk (below).

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
B=20261025
pmtiles extract https://build.protomaps.com/$B.pmtiles protomaps-$B-z0-12.pmtiles --maxzoom=12
curl --fail --location -o seamap-tiles.json https://tiles.openwaters.io/seamap/tiles.json
SEAMAP_DATE=$(python3 -c 'import json,re; d=json.load(open("seamap-tiles.json")); print(re.search(r"Seamap ([0-9-]+)", d["description"]).group(1))')
pmtiles extract "https://tiles.openwaters.io/seamap/${SEAMAP_DATE}.pmtiles" seamap-z6.pmtiles --minzoom=6 --maxzoom=6
.venv/bin/python land_product.py cells --seamap-z6 seamap-z6.pmtiles \
  --output coastal-cells.json --region-output coastal-region.geojson
pmtiles extract https://build.protomaps.com/$B.pmtiles peaks-raw-$B.pmtiles \
  --region=coastal-region.geojson --minzoom=13 --maxzoom=13
.venv/bin/python land_product.py peaks peaks-raw-$B.pmtiles --output peaks-$B.pmtiles
rm peaks-raw-$B.pmtiles
curl --fail --location -o GEBCO_2026.zip \
  'https://dap.ceda.ac.uk/bodc/gebco/global/gebco_2026/ice_surface_elevation/netcdf/GEBCO_2026.zip?download=1'
unzip GEBCO_2026.zip GEBCO_2026.nc
.venv/bin/python land_product.py build \
  --protomaps protomaps-$B-z0-12.pmtiles --protomaps-build $B.pmtiles --peaks peaks-$B.pmtiles \
  --gebco GEBCO_2026.nc --gebco-grid GEBCO_2026 \
  --release $B --cells-file coastal-cells.json \
  --output out/land --state-dir state/$B --jobs 14 --max-rss-mb 3000
.venv/bin/python audit_land.py out/land \
  --protomaps protomaps-$B-z0-12.pmtiles --gebco GEBCO_2026.nc --jobs 14
```

`build` hashes both Protomaps inputs and the GEBCO file once (cached in the
state directory) and records them in the manifest; compare the official
`b3sum` in `builds.json` separately if needed — this tool does not compute
BLAKE3. Builds are resumable: every finished cell leaves a state record keyed
by the inputs, generator hash and release; rerunning the same command skips
them. Cells run in parallel worker processes (`--jobs`), one cell per task,
and the run stops when a worker's peak RSS exceeds `--max-rss-mb` (finished
cells are kept). `cells.json` and `manifest.json` are written only after
every selected cell is built. Cells with neither land features nor depth get
no file and no `cells.json` entry.

### Output contract

```text
out/land/manifest.json
out/land/releases/<release>/cells.json          [{"x","y","bytes","sha256"}, …]
out/land/releases/<release>/cells/8/{x}/{y}.pmtiles
```

The manifest follows the hosting draft (id, release, zooms, layers, sources
with build/version/bytes/sha256, depth parameters, attribution, licences,
`generatorIdentity` = `land_product.py:land-product-1:sha256:<file hash>`,
`cellsURL`, `cells`, plus `cellsBytes`/`cellsSHA256`/`cellCount`/`totalBytes`).
Each cell's metadata repeats the licence, attribution, GEBCO DOI, depth
notice, Protomaps build, GEBCO grid and generator hash.

`audit_land.py` re-checks a release without importing the producer: its own
pmtiles/MVT decoding (stdlib) for layer/field/zoom whitelists, tile placement,
metadata, bytes and sha256 against `cells.json`/`manifest.json` and stray
files; with `--protomaps`/`--gebco` it also re-derives, for every depth
polygon at every zoom, bilinear GEBCO depth on its densified boundary and at
every GEBCO node inside it (≥ band floor), exact distance from that boundary
to the Protomaps z12 `earth` polygons (≥ 1500 m, with a poleward-conservative
metre scale), no land inside, and ≥ 1 km² for pieces that do not touch the
cell edge. Across two cells in the same release, every positive-length depth
segment on their shared edge must have a counterpart on the other side;
one-sided segments are errors. Point-only contacts are allowed, and the
audit does not infer land or missing GEBCO coverage from the polygons.

### Measured on this machine (Apple M5 Max, 18 cores, 48 GB)

Sample cells, rebuilt from the prototype's z8–12 extracts of 20261025 and
z13 peaks from a 20261024 extract (only copy at hand; relabelled for the
sample run), using the full `/tmp/dh-land-gebco/GEBCO_2026.nc` directly and
read-only (no subset was needed). One worker, measured 2026-10-26. Time is
the build's per-cell worker time; each second run used independent output
and state directories. Load is the 1-minute average before → after each run.

| Cell | With places | Without places | Time (two runs) | Load (two runs) | Peak RSS (max) |
| --- | --- | --- | --- | --- | --- |
| Dachen 8/214/106 | 1,489,365 B | 1,434,864 B | 15.9 s / 16.2 s | 2.6→3.5 / 2.6→2.6 | 1.23 GB |
| Hong Kong 8/209/111 | 2,921,286 B | 2,839,448 B | 33.4 s / 33.1 s | 3.0→2.8 / 2.4→2.2 | 1.37 GB |

"Without places" is the same cell with the `places` layer removed after
the fact (measurement only; the producer has no such mode). Both independent
rebuilds were byte-identical. Both cells passed `audit_land.py` with
geography against the same full GEBCO file:

| Cell | Edge fragments | Smallest checked edge fragment | windowExempt | openExempt (unverifiable) | Min. offshore |
| --- | --- | --- | --- | --- | --- |
| Dachen 8/214/106 | 26 | 2.134 km² | 0 | 0 | 1689.77 m |
| Hong Kong 8/209/111 | 31 | 2.217 km² | 0 | 2 | 1698.72 m |

`openExempt` counts fragments continuing into a cell not present in that
one-cell release, so the audit cannot verify their other side; it is not an
exemption for a one-sided edge between two listed cells. The most poleward
coastal cells (3 rings of neighbour tiles, 34 of 14,335) peaked at 0.73 GB
without land (measured before the fragment window was added).

Earlier estimate for 14,335 coastal cells with `--jobs 14` (not a
full-window measurement): the two
sample cells are 2.2× and 4.4× the prototype's 40-cell random coastal mean
raw size (1.24 MB z8–12). Scaling the least-loaded single-worker times
(22.1 s, 49.7 s) by raw size gives about 10–11 s per average cell, i.e.
14,335 × 11 s / 14 ≈ **3 h** wall if 14 workers did not slow each other
down. They do: an earlier 14-way run took 2.4–2.9× the single-worker time
per cell, but that run overlapped a runaway process of mine, so the factor
is not reliable; with 3× it would be ≈ **9 h**. Output: the samples are
0.39× their raw z8–12 size, so about 0.5 MB per average cell, ≈ **7 GB**.

Disk for one release: z0–12 extract 18 GB + GEBCO zip and netCDF ≈ 15 GB
(zip size not measured; assumed no smaller than the netCDF) + transient
z13 extract 17 GB (deleted after `peaks`) + output ≈ 7 GB + state and
peaks file < 1 GB — about 60 GB at the peak, against 898 GB free on this
machine.

### Licences

Land layers are derived from OpenStreetMap via Protomaps and are under the
**Open Database License 1.0** (`Licenses/ODbL-1.0.txt`,
https://opendatacommons.org/licenses/odbl/1-0/); attribution
"© OpenStreetMap contributors · Protomaps". This repository is the public
description of the derivation (ODbL 4.6). The depth layer is derived from the
GEBCO_2026 Grid, placed in the public domain under GEBCO's terms
(`Licenses/GEBCO-terms-of-use.txt`); attribution: GEBCO Bathymetric
Compilation Group 2026 (2026). The GEBCO_2026 Grid - a continuous terrain
model for oceans and land at 15 arc-second intervals.
doi:10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa. GEBCO's terms say the grid
must not be used for navigation or for any purpose involving safety at sea.
The test fixtures under `tests/fixtures/` (`protomaps-dachen*.pmtiles`,
`gebco-dachen.nc`) are small extracts of the same sources under the same
terms. `land_product.py`, `audit_land.py` and their tests are offered under
LGPL-3.0-only like the rest of this repository.
