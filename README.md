# DingHai basemap generator

This repository holds DingHai's development-side offline basemap producer,
**seamap-cell-3**. It does not run in the App. One Planetiler compile of one
OpenStreetMap snapshot yields, as standard PMTiles (one gzip per tile, no
DingHai-specific format):

- a **global overview**, z0–7, one file: land and inland water, the sea being
  everything else;
- one **cell** file per z8 tile (the App's `SeamapCell`, `gridZoom = 8`), z8–14,
  for all 65,536 cells: seamarks, land, water, wetland, sea areas, land use,
  roads, peaks, places and illustrative GEBCO depth.

```text
planet-YYMMDD.osm.pbf ──────────────────────────────────────────┐
land_polygons.shp ─┬────────────────────────────────────────────┤ Planetiler 0.10.2
                   └ depth_product.py index → land-index.sqlite │ planetiler/ SeamapCompiler
GEBCO_2026.nc ─────── depth_product.py build ─── depth/x*.shp ──┘ → seamap.pmtiles, z0–14
                        (offshore distance to the same land polygons)
seamap.pmtiles ─ seamap_product.py ─┬─ objects/<sha256>.pmtiles   overview z0–7 / cells z8–14
                                  ├─ index/<sha256>.json       z8 cells grouped by z4 parent
                                  ├─ releases/<release>.json  immutable provenance record
                                  └─ manifest.json            only mutable publication pointer
```

No Protomaps build, no per-cell merge of a second product and no third-party
Seamap archive is read, redistributed or credited. Tiles are copied from the
compiled archive into the overview and cells, never re-encoded.

| File | Role |
| --- | --- |
| `planetiler/` | `SeamapCompiler.java`, the only layer rules; Planetiler pinned in `pom.xml` |
| `depth_product.py` | land-polygon index and GEBCO depth bands, an input of the compile |
| `seamap_product.py` | cuts the compiled archive into immutable objects, index shards and release records; `manifest.json` |
| `audit_seamap.py` | stdlib-only structural audit of a release |
| `land_subset.py` | pilots only: selects whole land polygons around pilot boxes |
| `satellite_product.py` | coastal satellite: estimate, CLI verification, ETag-pinned extraction and v3 release |
| `satellite_overview.py` | adds the z0–6 satellite overview to a finished, unpublished local release |
| `satellite_mosaics.py` | renders native-tile mosaics of three random published satellite cells for visual review |
| `audit_satellite.py` | structural audit of a satellite release, decoding every WebP image |

## Layers and App-facing fields

The overview (z0–7) has two layers without fields. They carry sea, land, lake
fills and the sea coast including island outlines, and nothing more: no depth
bands and no sea-area names, which the App shows only from z8 in cells.

| Overview layer | Source | App use |
| --- | --- | --- |
| `land` | OSM land polygons, simplified per zoom (0.25 px) | land fill; its own edges are the coastline and island outlines (tile-clipped edges fall in the tile buffer, as for cells) |
| `water` | the cell `water` rule without river polygons (`waterway=riverbank`, `water=river/canal/stream/ditch/drain/oxbow`), no fields, under 2 px dropped per zoom | lake and reservoir fill, drawn in the sea colour and not stroked |

Cells retain the ten layer names, fields and per-layer zoom ranges
(`vector_layers`). Publication uses schemaVersion 3 and content addresses,
independent of the release label.

| Cell layer | Zooms | OSM rule (this repository's own) | Fields |
| --- | --- | --- | --- |
| `seamark` | 8–14 | every `seamark:type`, nodes, ways and areas alike (overhead cables and pipelines are ways); piers, breakwaters, groynes, quays, wharves, pontoons from `man_made` | original `seamark:*`, names and `function`; `osm_id`, tile `zoom`; normalized `type`, `category`, `shape`, `color`, `color_pattern`, `topmark_shape`, `topmark_color`, `light_color`, `family`; on seamarks with numbered or sector lights, nodes or ways alike, `dh_sectors`, `dh_light_colour`, `dh_allround_colour` (below); on bridges, overhead cables and overhead pipelines, `dh_clearance`, `dh_clearance_safe`, `dh_clearance_closed`, `dh_clearance_open` (below) |
| `land` | 8–14 | `land-polygons-complete-4326` | none: fill and coastline stroke |
| `water` | 8–14 | `natural=water`, `waterway=riverbank`, `landuse=reservoir/basin`, `water=reservoir/basin/water` | `name`, `water` |
| `wetland` | 8–14 | `natural=wetland` + mangrove/tidalflat/saltmarsh | `type=wetland`, `category`, `name`, `surface` |
| `sea_area` | 8–14 | `place=sea`, `natural=bay`; polygons labelled at a point on surface | `category`, `name`, `name:zh`, `name:zh-Hans` |
| `landuse` | 8–12 | `landuse`, `leisure`, `natural` mapped to wood/park/farmland/urban/industrial/beach; ≥ 2 px; merged by class per tile, then clipped to the tile's `land` | `class` |
| `roads` | 8–12 | `highway=motorway(_link)` → highway; trunk/primary(_link) → major; secondary/tertiary(_link) → secondary from z11; merged by class per tile | `class` |
| `peaks` | 11–12 | `natural=peak` nodes with a parseable `ele` | `ele` (integer m) |
| `places` | 8–12 | named `place=island` polygons as an innermost point once ≥ 20 px; `place=town` nodes from z9, `village` from z11, `locality` at z12 | `kind`, `name` = `name:zh-Hans`, else `name:zh`, else `name` |
| `depth` | 8–12 | `depth_product.py` (below) | `min` = band floor (20/30/50/100/200 m), cumulative polygons |

The land-layer rules cover only what the App reads (its style draws
`island` from z8, `town` z9, `village` z11, `locality` z12 and peaks from z11),
so features are emitted from those zooms. Protomaps' basemap profile is
BSD-3-Clause (compatible with this LGPL-3.0-only code) but was not reused: it
brings its own schema and extra inputs (Natural Earth, QRank) that this
single-snapshot product does not want. `light` is not separate: lights are
seamarks with `seamark:light:*` tags (`light_color` normalizes only the
unnumbered `seamark:light:colour`). `waterway` lines are not emitted because
the App does not read them.

Complete, not split, OSM land polygons are deliberate. The official split
product has overlapping processing chunks; since the App strokes every land
polygon edge, artificial chunk edges could become false inland coasts.
Planetiler clips complete polygons to tile buffers instead.

### Numbered, sector and directional lights

OSM tags a sectored or directional light, and a seamark with several lights,
as numbered lights, `seamark:light:N:*` (N ≥ 1, even for one sector), usually
without an unnumbered `seamark:light:colour` and so without `light_color`.
Such seamarks, nodes or ways alike, also get the station fields of DingHai's S-57 light
stations, in the same encoding (dinghai-legal `chartset/tiles.md` §4.2), so
the App's rose decoder and drawing read them unchanged and colour every light
from one field without expanding N:

| Field | Value |
| --- | --- |
| `dh_sectors` | only with at least one sector or direction: one segment per such light, `;`-separated: sector `s:<start>,<end>,<visibility>,<colours>`, direction `d:<bearing>,<visibility>,<colours>`. Never `hub`: one OSM seamark is the whole station. |
| `dh_light_colour` | the station's colour for its overview dot and lamp: `3` red, `4` green, `6` yellow when every light that is not obscured has that one glow colour, else `multi` (magenta). The lights are every numbered light, all-round ones included, and the unnumbered light when it is a sector or direction (an unnumbered `range` or `reference` beside numbered lights is not a light). Written on every seamark with a numbered light or a `dh_sectors`, except when the unnumbered `seamark:light:colour` (`light_color`) is tagged: that already colours it. |
| `dh_allround_colour` | the glow of the all-round lights beside the rose, which S-52 draws with their own lamp and glow over the sectors: `3`, `4` or `6` when every all-round light that is not obscured has that one glow colour, else `multi`; colours as for `dh_light_colour`. An all-round light is a numbered light that is neither a sector nor a direction, or the unnumbered light when it is neither and its `seamark:light:colour` is tagged. Written only with `dh_sectors` and when there is such a light. |

Lights are read in the order: the unnumbered `seamark:light:*`, then each
`seamark:light:N:*` by ascending N. A light is a sector when `sector_start`
and `sector_end` are distinct plain decimal degrees in 0–360; otherwise a
direction when its `category` lists `directional` or `moire` (S-57 CATLIT 1,
16) with such an `orientation` and it is not obscured; any other light (an
all-round light, malformed or out-of-range bearings, 0 to 360) is not in
`dh_sectors`, as the App's resolver of S-57 `LIGHTS` decides. Degrees are written as
tagged, without trailing zeros, 360 as 0. OSM `sector_start`/`sector_end` are
S-57 SECTR1/SECTR2: bearings from seaward towards the light, clockwise
([Seamarks/Lights](https://wiki.openstreetmap.org/wiki/Seamarks/Lights), whose
obscured sector is "not visible from seaward"; the
[sectored-light diagram](https://wiki.openstreetmap.org/wiki/Seamarks/Sectored_and_Directional_Lights)
puts 0° below the light).

| OSM tag | Encoded |
| --- | --- |
| `visibility` | `o` when it lists `obscured` (colours then empty), `f` for `faint` or `part_obscured`, else `l` |
| `colour` | `red` → `3`, `green` → `4`, `white`/`yellow`/`amber`/`orange` → `6`; alternating colours `+`-joined in order, a repeated glow once; empty (magenta) when a colour has no glow, more than two glows remain or none is tagged |

Nothing else is derived: the characteristic, range and the other light tags
stay only as the original `seamark:light:N:*` tags; the App writes the light
label from them. For example OSM node
1293080019 (Xiya Island, `seamark:type=light_major`,
`seamark:light:1:colour=white`, `sector_start=272`, `sector_end=245`) gets
`dh_sectors=s:272,245,l,6`, `dh_light_colour=6`; node 6816680116 (white
165–320, red 320–165) gets `s:165,320,l,6;s:320,165,l,3` and `multi`; node
1136085219 (Cape Collinson: a white sector 156.5–337 and a red numbered light
without bearings) gets `s:156.5,337,l,6`, `multi` and `dh_allround_colour=3`; way
822828466 (Fan Lau lighthouse, its white 261–154 sector tagged on the building
outline) gets `s:261,154,l,6` and `6` on its seamark polygon.

### Vertical clearances

A bridge, overhead cable or overhead pipeline (`seamark:type=bridge`,
`cable_overhead`, `pipeline_overhead`) also gets its vertical clearances as
numbers in metres, so the App reads a number instead of parsing the OSM
string (a style's `to-number` would read `15'` as 15 m, about three times the
real clearance):

| Field | OSM tag ([Seamark Attributes](https://wiki.openstreetmap.org/wiki/Seamarks/Seamark_Attributes)) | S-57 |
| --- | --- | --- |
| `dh_clearance` | `seamark:<type>:clearance_height` | VERCLR |
| `dh_clearance_safe` | `seamark:<type>:clearance_height_safe` | VERCSA |
| `dh_clearance_closed` | `seamark:<type>:clearance_height_closed` | VERCCL |
| `dh_clearance_open` | `seamark:<type>:clearance_height_open` | VERCOP |

A clearance must never come out larger than tagged. Accepted, after trimming
surrounding spaces: a plain decimal (`17`, `54.6`, `.5`), metres, optionally
followed by `m` (`5 m`); feet as OSM writes them, `15'`, `15 ft` (any case) or
`15'6"` (inches below 12), at 0.3048 m per foot and 0.0254 m per inch. The result is
rounded down to the centimetre (`15'` → 4.57, `15'6"` → 4.72, `12.349` →
12.34). Anything else — `M` (nautical miles), `feet`, a decimal comma, lists
(`10;12`), `1m30`, `unknown`, words before or after the number — and a value
that is not above 0 once rounded leaves that field unwritten; nothing is
guessed or taken from another tag. The original `seamark:<type>:clearance_*`
strings stay as tagged.

In the Hong Kong cells 8/208/111 and 8/209/111 compiled from the Guangdong,
Hong Kong and Macau extracts of 2026-11-13, eleven bridge nodes tag
`clearance_height` as plain metres (node 2187066770, `54.6` →
`dh_clearance=54.6`) and one overhead cable node tags
`seamark:cable_overhead:clearance_height_safe=12` (node 2192703195,
`dh_clearance_safe=12`); no overhead cable or pipeline is mapped as a way there.

## Depth: illustrative, not surveyed

Depth bands are interpolated from the GEBCO_2026 15″ grid. They are
**illustrative, not a survey and not for navigation** (`示意、非测量、不可用于导航`);
the metadata of every cell and the manifest say so.

A point is coloured with band *L* only if, at the zoom it is drawn:

- bilinear GEBCO depth ≥ *L* (m below GEBCO's mean-sea-level reference), and
- ground distance to OSM land (the same `land_polygons.shp` as the `land`
  layer) ≥ 1500 m,

and pieces smaller than 1 km² are dropped (holes are kept: filling a hole
would claim depth where GEBCO is shallower). The producer works on z12 world
pixels (4096 per cell side) and holds margins so that rasterising,
contouring, generalising and MVT rounding can only shrink a band: +2 m depth,
3 px distance, and each zoom's lattice (stride *s* = 2^(12−z)) takes the
minimum over a Chebyshev radius *s*+2. Depth pooling takes the exact minimum
of the continuous bilinear GEBCO field over each rectangle: original GEBCO
node lines split it into bilinear subrectangles whose minima occur at
corners. Longitudes wrap periodically at ±180°.

`depth_product.py index` cuts the land polygons into z8-cell pieces once
(SQLite, z12 world pixels). Each cell rasterises the pieces of its 3×3
neighbourhood over 1–3 rings of z12 tiles (at least 1.25 × 1.7 km at the
cell's latitude), polygon by polygon so islands inside other polygons' holes
stay land, so a cell sees the same coast as its neighbours.

Fragments are judged on a shared window reaching 256 z12 px (one z12 tile;
≈8.6 km at 28° N) beyond the cell: connected components after final integer
snapping, area with holes subtracted at the minimum ground scale over the
component's latitude extent. The keep/drop decision is a property of the whole
component: the touched cells are those whose closed extents, including shared
seams, intersect it (indices ceil(lo / 4096) − 1 through floor(hi / 4096)).
Only a component strictly inside every touched cell's window is eligible for
dropping; one clipped by a window is kept by every cell that draws it
(`FragmentPropertyTests`). Bands are then clipped to the cell; two adjacent
cells produce the same edge (`SeamTests`).

Cells whose GEBCO nodes are all no deeper than 22 m colour nothing; cells
whose nodes are all deeper than 202 m and that have no land piece in their
3×3 neighbourhood fill every band at every zoom. Both are decided from the
enclosing GEBCO nodes without contouring and equal what contouring draws
(`test_uniform_cells_skip_contouring_with_the_regions_contouring_draws`). Of
the 65,536 cells, 23,975 are dry, 27,705 uniformly deep and 13,856 need
contouring (GEBCO_2026 and the 2026-10-31 land polygons).

`depth_product.py build` writes one Shapefile per z8 column
(`depth/x{X}.shp`, fields `min`, `zoom`; one EPSG:4326 record per cell, zoom
and band) plus `x{X}.json` listing the column's finished cells, and
`identity.json` (code hash, parameters, GEBCO and land-polygon identity). A
rerun skips finished columns and refuses a directory built from other
inputs. Shapefiles, not a GeoPackage: Planetiler 0.10.2 reads an EPSG:4326
GeoPackage latitude-first (its target CRS decodes north/east), which put a
pilot's depth thousands of kilometres away. The compiler keeps every depth
vertex (`depth_product.py` already generalised each zoom on its own lattice,
on the 4096-per-tile grid of that zoom) and drops no small pieces.

## Rebuild from the repository root

Run these commands in the repository root. Git ignores the inputs placed there
and everything the commands write (`.venv/`, `land-polygons-complete/`,
`land-index.sqlite`, `depth/`, `seamap.pmtiles`, `provenance.json`, `out/`,
`state/`, and a pilot's `pilot/`).

Verified environment: CPython 3.12.7 with `requirements.txt` (macOS arm64
wheels), Java 21, Maven, osmium-tool. Inputs: a dated planet PBF (record URL,
replication date, bytes and SHA-256; about 95.6 GB), OSM Data's
`land-polygons-complete-4326.zip` (https://osmdata.openstreetmap.de/data/land-polygons.html)
and GEBCO_2026 (`GEBCO_2026.nc`, 7,466,018,396 bytes, from
`https://dap.ceda.ac.uk/bodc/gebco/global/gebco_2026/ice_surface_elevation/netcdf/GEBCO_2026.zip?download=1`).

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
brew install openjdk@21 maven osmium-tool
export JAVA_HOME=$(brew --prefix openjdk@21) PATH="$(brew --prefix openjdk@21)/bin:$PATH"
mvn -q -f planetiler/pom.xml package
unzip land-polygons-complete-4326.zip -d land-polygons-complete
LAND=land-polygons-complete/land-polygons-complete-4326/land_polygons.shp
.venv/bin/python depth_product.py index --land $LAND --output land-index.sqlite
.venv/bin/python depth_product.py build --gebco GEBCO_2026.nc --gebco-grid GEBCO_2026 \
  --land-index land-index.sqlite --output depth --all-cells --jobs 12
java -Xmx24g -jar planetiler/target/dinghai-seamap-compiler-1.0.0.jar \
  --osm=planet-YYMMDD.osm.pbf \
  --osm_url=https://planet.openstreetmap.org/pbf/planet-YYMMDD.osm.pbf \
  --land=$LAND --depth=depth --output=seamap.pmtiles \
  --minzoom=0 --maxzoom=14 --threads=12 --tmpdir=/tmp/dinghai-seamap-planetiler
.venv/bin/python seamap_product.py --archive seamap.pmtiles --provenance provenance.json \
  --depth depth --release YYYYMMDD --all-cells --output out --state-dir state --jobs 8
.venv/bin/python audit_seamap.py out
```

`provenance.json` names the two OSM inputs (`osm` may be a list when a pilot
combines extracts):

```json
{
  "osm": {"url": "https://…/planet-YYMMDD.osm.pbf", "date": "YYYY-MM-DD…", "bytes": 0, "sha256": "…"},
  "landPolygons": {"url": "https://osmdata.openstreetmap.de/download/land-polygons-complete-4326.zip", "date": "YYYY-MM-DD…", "bytes": 0, "sha256": "…"}
}
```

Use a different `--tmpdir` for every concurrent compiler; Planetiler deletes
and recreates it. The compiler forces exact global Web-Mercator bounds and
rejects regional `--bounds`: Planetiler 0.10.2's GeoTools Shapefile bbox
filter returned zero features for these WGS84 polygons. For a pilot,
pre-clip the OSM input (`osmium extract --strategy=smart -p region.geojson`)
and the land polygons (`land_subset.py land_polygons.shp pilot/land_polygons.shp
--bbox W,S,E,N …`), build depth only for the pilot cells (`--cell X/Y`), and
slice with `--cell X/Y`. The overview of a pilot covers only its region. A
pilot is a local experiment only: it lacks cells, so `audit_seamap.py` rejects
it, and it is never published.

`seamap_product.py` resumes a cell only when the global input/generator identity
matches and its content-addressed object exists. It rebuilds the overview every
run, reusing an identical object without overwriting it. No additional cache is
used. Keep `--state-dir` separate from the publication output: per-layer statistics
(`mvtBytes` uncompressed, `gzipBytes` per layer for diagnosis only) and worker
`seconds` stay in that state and the generator's JSON-line stdout, not in the
published index. `audit_seamap.py` derives its own feature counts from the tiles.

Only after every selected cell succeeds does finalize publish the shards and an
immutable release record, then atomically replace `manifest.json`. Repeating an
identical release is safe; trying to reuse its name with different manifest bytes
fails without moving the pointer. A release name must match
`[A-Za-z0-9][A-Za-z0-9._-]*`, the App's rule for both products; finalize and
both audits reject any other name. Existing objects and shards may be shared
by multiple releases and are never pruned here.

### Global overview check (2026-11-03)

The z0–7 layers depend only on the land polygons and water areas, so the
global overview was compiled by the same jar from an `osmium tags-filter`
of `planet-261026` (`a/natural=water a/waterway=riverbank
a/landuse=reservoir,basin a/water`, 6.0 GB) with the complete land polygons,
`--maxzoom=7`: 274 s, then cut by `build_overview`. The file is
**17,860,905 bytes** (21,845 possible tiles, 11,604 present; z7 is 12.0 MB,
z6 4.0 MB, z5 1.3 MB, z0–4 0.75 MB).

## Output contract

```text
out/manifest.json                      schemaVersion 3, only mutable object
out/releases/<release>.json            byte-for-byte copy of that release's manifest
out/index/<sha256>.json                compact [[x,y,bytes,"sha256"],…], sorted by (x,y)
out/objects/<sha256>.pmtiles           cell or overview; filename hashes the file bytes
```

`manifest.json` keeps `id: dinghai-seamap`, `release`, `gridZoom: 8`,
`minimumZoom: 8`, `maximumZoom: 14`, `layers`, `sources` (OSM, land polygons,
GEBCO, compiled archive), attribution, depth parameters and notice, licences,
`generatorIdentity` and `depthGeneratorIdentity`. Its publication references are:

- `overview: {"sha256": "…", "bytes": N}` for the z0–7 land/water object;
- `index: {"<x4>/<y4>": "<sha256>", …}`, one shard per populated z4 parent;
- `record: "releases/<release>.json"` for immutable source tracing;
- `cellCount` and `totalBytes` (cell objects only, excluding overview/index).

For example, Hong Kong cell `8/209/111` belongs to parent `13/6`:

```text
manifest.index["13/6"] → index/<shard-sha>.json
                       → row [209,111,bytes,"<cell-sha>"]
                       → objects/<cell-sha>.pmtiles
manifest.record        → releases/20261103.json (same manifest bytes, immutable)
```

There is no global cell-inventory JSON or diagnostic layer statistics in the
publication. Even an empty selected cell gets a PMTiles object and an index row.
Object metadata retains stable schema, cell coordinates, attribution, licences,
GEBCO DOI and the illustrative-depth warning, but no release, source snapshot
URL/date/hash/bytes, generator identity or depth-generation parameters. These
belong in the manifest and its immutable release record. Every gzip member
(tiles, metadata, root and leaf directories) has mtime 0 and OS byte 255.
Index JSON uses fixed compact separators and sorted rows before hashing.

`audit_seamap.py` checks that the index lists all 65,536 cells (the App fails
a download whose release lacks a cell), object and shard hashes/sizes, parent
placement and ordering, manifest totals and release-record equality, overview
zooms/layers, cell tile placement, layers/zooms, and each object's
`dinghai:schemaVersion` and `dinghai:cell`. Extra immutable objects from other
releases are allowed. It does not establish complete or navigation-grade
geography.

## App integration boundary

The App must consume schemaVersion 3 directly: fetch only the selected cells'
z4 index shards, download objects by hash, and compare installed hashes with the
current manifest/index hashes to decide whether an update exists. Release labels
are provenance, not update identities. The server should serve `manifest.json`
with `no-cache`; objects, shards and release records are immutable. Client
integration and publication scripts belong to other lanes; this repository makes
no App change, upload or deployment and provides no legacy-format output path.

## Coastal satellite (local generation only)

`satellite_product.py` makes **dinghai-satellite**, a separate raster product.
It uses the official VersaTiles **5.7.0** CLI to remotely cut the published
`https://download.versatiles.org/satellite-raster/satellite-raster.versatiles`
by bbox. It never downloads the whole container. The publisher's release-asset
SHA-256 verifies the CLI distribution, and the executable must match that
archive. Redirects are never followed, so the recorded URL is the URL actually
read. Satellite currently has no dated URL in the publisher's satellite URL
list (`https://download.versatiles.org/satellite-raster/urllist.tsv`). That list
also gives an MD5, which cannot be checked without downloading the whole
container and is not recorded. The URL, strong HTTP ETag and full container
byte length identify the snapshot in the estimate, every cell's metadata, and
the immutable release record. Releases made before the publisher moved the
container record its former URL, `https://download.versatiles.org/satellite.versatiles`.

Selection uses the same full-resolution `depth_product.py` land index and
the App's XYZ z8 grid. Compute each cell's land fraction from polygon unions
(not a raster, so tiny islands count). Starting at the full-water central
Pacific cell `8/21/128`, flood through cells with land fraction below one,
using **eight neighbours**, wrapping x at the date line but never wrapping y.
Keep reached cells whose land fraction is above zero. This is deliberately
**cell-level connectivity**, not a test of whether individual water polygons
connect: an adjacent lagoon can be included. Enclosed inland water separated
from the ocean by wholly dry cells is excluded. The estimate records selection
results for Caspian `8/163/95`, Hong Kong `8/209/111`, and Kiel `8/135/81`.

```text
land-index.sqlite → polygon land fractions → z8 ocean flood → coastal cells
remote VersaTiles header + Brotli indexes ──────────────────┘
    → estimate.json (tile counts/lengths; 300 GB gate)
    → official CLI bbox cuts, z8–13 → transient MBTiles (TMS)
    → byte-preserving WebP PMTiles (XYZ), one object per cell
    → v3 shards + local cell release record
same pinned source → satellite_overview.py → global z0–6 WebP PMTiles
    → add manifest.overview and replace the unpublished local release record
```

Only actually stored tiles at z8–13 are copied: gaps and regional maximum
zooms remain gaps, with no generated levels, resampling or image re-encoding.
Images remain 512×512 WebP. In particular, the Hong Kong source has no z13
tiles. Metadata explicitly carries
`VersaTiles sources (https://versatiles.org/sources/)` and the source version.
The source's component attribution/licences are available at that URL.

The estimate uses **only** the coastal list and container indexes, not image
downloads. It sums all addressed tile lengths without a deduplication discount,
then reserves 40 bytes per tile and 64 KiB per cell for PMTiles and publication
overhead. If that conservative estimate exceeds **300,000,000,000 bytes**, stop;
the build command also refuses it. Cached indexes are scoped to the whole source
identity (URL, ETag and length).
The official CLI reads through a loopback range endpoint: every origin request
uses `If-Match`, verifies the returned ETag/range, and shares a four-request
semaphore. No remote write is performed. At most four cells run concurrently.

Example (the CLI archive and binary must first be downloaded from its official
GitHub v5.7.0 release; these commands do not install or execute an unverified
binary):

```sh
WORK=/tmp/dh-sat-prod
RELEASE=YYYYMMDD
PY=.venv/bin/python
$PY -m pip install --require-hashes -r requirements.txt
$PY satellite_product.py verify-cli \
  --archive /tmp/versatiles-macos-aarch64.tar.gz --cli /tmp/versatiles \
  --output "$WORK/cli-verification.json"
$PY satellite_product.py estimate --land-index land-index.sqlite \
  --cache "$WORK/index-cache" --output "$WORK/estimate.json"
# Inspect withinBudget before proceeding; a false value means stop and report.
$PY satellite_product.py build --estimate "$WORK/estimate.json" \
  --cli /tmp/versatiles --cli-verification "$WORK/cli-verification.json" \
  --release "$RELEASE" --output "$WORK/$RELEASE/out-v3" \
  --state "$WORK/$RELEASE/state" --cache "$WORK/index-cache" --jobs 4
$PY satellite_overview.py "$WORK/$RELEASE/out-v3" \
  --estimate "$WORK/estimate.json" --cli /tmp/versatiles \
  --cli-verification "$WORK/cli-verification.json" \
  --state "$WORK/$RELEASE/state" --cache "$WORK/index-cache" \
  --replace-unpublished-record
$PY audit_satellite.py "$WORK/$RELEASE/out-v3" \
  --estimate "$WORK/estimate.json" --output "$WORK/$RELEASE/audit.json"
$PY satellite_mosaics.py "$WORK/$RELEASE/out-v3" \
  --audit "$WORK/$RELEASE/audit.json" --output "$WORK"
```

The output root is the future **`satellite/`** publication prefix; it contains
`manifest.json`, `index/<sha256>.json`, `objects/<sha256>.pmtiles` and
`releases/<release>.json`. Like Seamap, the schemaVersion 3
index is compact `[[x,y,bytes,"sha256"],…]`, sorted and sharded by z4 parent.
For example, Hong Kong uses `manifest.index["13/6"]`. `cellCount` and
`totalBytes` describe cell objects; the manifest also declares `tileType: webp`,
`tileSize: 512`, `minimumZoom: 8`, and `maximumZoom: 13`.

The top-level `overview: {"sha256": "...", "bytes": N}` references the global
**z0–6** object in the same objects directory. z7 is displayed by the App using
z6; the producer does not synthesize z7. The source index for this snapshot has
**5,461 tiles**, every z0–6 XYZ tile. The resulting PMTiles is **90,528,164
bytes**. Every overview tile is copied without re-encoding from the same
ETag-pinned source using the same verified CLI. Overview index counts and
payload lengths are recorded in `state/overview-estimate.json`.

Keep the state directory outside the output. Completed cells resume only under
the same generator, estimate, CLI and source identity, and after rechecking
their object's SHA-256. A partial cell is re-extracted. Transient MBTiles are
deleted after their PMTiles object and receipt are installed. Finalization
occurs only after all cells succeed and the remote version is checked again.
For long runs, launch the build in an independent session with output appended
to a durable log, rather than tying it to an interactive agent runner.

The overview is deliberately a separate second command, run **after all cells
finish**. Do not change `satellite_product.py`, `seamap_product.py`,
`depth_product.py` or `requirements.txt` while resuming this run: their combined
hash is the cell identity. `satellite_overview.py` is outside that identity and
records its own `overviewGeneratorIdentity`. It preserves all cell objects,
shards, receipts and the cell identity. The explicit
`--replace-unpublished-record` flag authorizes replacement of the **local,
not-yet-uploaded** release record under the same release name when adding the
overview; the manifest is then replaced with those same bytes. Do not use this
command on an already published release. This local assembly step is not an
upload or a mutable-record contract for R2.

`audit_satellite.py` verifies hashes, sizes, shard order/parent placement,
release-record equality, manifest totals, metadata, tile coordinates, actual
zooms and nonzero tile counts. It decodes **every image** to verify readable
512×512 WebP. With `--estimate`, the selected cell inventory and each zoom's
tile count/payload bytes must also match the source index. It does not establish
imagery age, optical resolution, navigation suitability or App rendering.
The audit also checks the overview SHA-256 and byte size, global bounds,
attribution/source metadata, actual presence of every zoom z0–6 (and no other
zoom), and decodes every overview image.
All product files and diagnostics stay outside Git. Uploading to R2 and
switching its manifest are explicitly a separate Main-owned operation.

## Licences

All Seamap layers except depth, and the whole Seamap overview, are derived from
OpenStreetMap and OSM Data's land polygons, © OpenStreetMap contributors,
**Open Database License 1.0** (`Licenses/ODbL-1.0.txt`); this repository is the
public description of the derivation (ODbL 4.6). The depth layer is derived
from the GEBCO_2026 Grid, public domain under GEBCO's terms
(`Licenses/GEBCO-terms-of-use.txt`): GEBCO Bathymetric Compilation Group 2026
(2026). The GEBCO_2026 Grid - a continuous terrain model for oceans and land at
15 arc-second intervals. doi:10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa. It
must not be used for navigation or for any purpose involving safety at sea.
Planetiler (Apache-2.0) is a build dependency. The generator code, audit and
tests are offered under **LGPL-3.0-only** (`Licenses/LGPL-3.0.txt`,
`Licenses/GPL-3.0.txt`). See `NOTICE.txt`.
