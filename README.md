# GSHHG marine-background product generator — python-product-2

This is the **only DingHai basemap producer**. It runs on a development computer,
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

## Rebuild in an empty directory

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

## Output contract and release

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

**Payload/manifest hosting decision: Cloudflare only.** The existing resource root is
`https://resources.dinghai.app/gshhg/`, with the file entry point
`https://resources.dinghai.app/gshhg/manifest.json`. This new immutable version
is generated locally; its publication and App contract cutover require separate
review and authorization. The adopted corresponding-source host is one
**independent minimal GitHub repository**, planned at
`https://github.com/jkqq147-team/dinghai-basemap-generator`. It contains only
this standalone generator, audit, necessary tests/fixtures, rebuild instructions,
dependency lock and licenses, never the DingHai App or all of Marine Imagery.
For each dataset version, use the same fixed tag/release and attach the matching
minimal source ZIP. No basemap payload ZIP is uploaded to GitHub, and there is
no CF source fallback or second payload source for this new version.

The draft manifest records measured version/artifact identities and planned
GitHub source/license links for `gshhg-2.3.7-python-product-2`. The repository,
tag/release and files are created/published only after independent review;
these links are not a claim of current public availability. The final CF
payload URL remains for the separate delivery phase:

```text
dataset/tag/release: gshhg-2.3.7-python-product-2
  CF payload/manifest → gshhg-2.3.7-python-product-2.zip / manifest.json
  GitHub release      → gshhg-product-2.3.7-python-product-2-source.zip
  GitHub fixed tag    → NOTICE.txt / Licenses/
```

Corresponding source and licenses must remain accessible for the exact data
version, without relying on a resource HTML page or a source/license entry
on the offline chart page. The adopted App entry is its existing legal notices;
the offline chart page retains basemap download management. Moving its source
link is a separate App step, not work performed here.
No App support/privacy page or broader privacy/legal work is part of this release.
Do not overwrite python-product-1 artifacts or infer that version 2 is already
hosted; do not delete the existing v1 CF source. This tool neither reads
credentials nor creates repositories, pushes, uploads, deploys or creates
services. Do not publish the private repository or unrelated tools. Anonymous
resource delivery is not a promise that a host cannot see request metadata.

A fixed product can remain static. Regenerate only for an adopted official-data
version, a conversion fix or a reader-format change; keep download links and
corresponding source/licensing available. There is no scheduled rebuild/CI
framework here and no promise of permanent zero maintenance.

## License and corresponding source

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
