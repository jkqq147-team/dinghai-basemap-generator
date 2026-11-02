#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Split a DingHai OSM Planetiler archive into z8 cells and merge land-product-1.

The OSM archive contains seamark/land/water/wetland/sea_area at z8-14. Existing
land cells contribute landuse/roads/peaks/places/depth at z8-12. No network or
third-party archive is read by this producer. Every selected z8 cell gets one
PMTiles file, including an empty ocean cell, and a bytes/SHA-256 cells.json entry.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pmtiles.tile import tileid_to_zxy, zxy_to_tileid

import land_product as lp

VERSION = "seamap-cell-1"
SCHEMA_VERSION = 2
OSM_LAYERS = ("seamark", "land", "water", "wetland", "sea_area")
LAYERS = OSM_LAYERS + lp.LAYERS
FIELDS = {
    "seamark": {"type": "String", "category": "String", "shape": "String", "color": "String",
                "color_pattern": "String", "topmark_shape": "String", "topmark_color": "String",
                "light_color": "String", "family": "String", "osm_id": "Number", "zoom": "Number",
                "name": "String", "name:zh": "String", "name:zh-Hans": "String", "seamark:*": "String"},
    "land": {},
    "water": {"name": "String", "water": "String"},
    "wetland": {"type": "String", "category": "String", "name": "String", "surface": "String"},
    "sea_area": {"category": "String", "name": "String", "name:zh": "String", "name:zh-Hans": "String"},
    "landuse": {"class": "String"}, "roads": {"class": "String"}, "peaks": {"ele": "Number"},
    "places": {"kind": "String", "name": "String"}, "depth": {"min": "Number"},
}


def generator_sha256():
    h = hashlib.sha256()
    for path in (Path(__file__), Path(__file__).parent / "land_subset.py", Path(__file__).parent / "seamap_slice.py",
                 Path(__file__).parent / "planetiler/src/main/java/app/dinghai/SeamapCompiler.java"):
        h.update(path.read_bytes())
    return h.hexdigest()


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _layer_message(raw):
    return b"\x1a" + lp._encode_varint(len(raw)) + raw


def _stats(tiles):
    layers = defaultdict(lambda: {"features": 0, "maxzoomFeatures": 0, "mvtBytes": 0, "gzipBytes": 0})
    for tile_id, raw in tiles.items():
        z = tileid_to_zxy(tile_id)[0]
        for name, layer in lp.split_layers(raw).items():
            stat = layers[name]
            count = sum(1 for field, _, _ in lp._fields(layer) if field == 2)
            stat["features"] += count
            if z == (14 if name in OSM_LAYERS else 12):
                stat["maxzoomFeatures"] += count
            stat["mvtBytes"] += len(layer)
            # Layer sizes are individually compressed MVT Layer messages, not additive PMTiles spans.
            stat["gzipBytes"] += len(gzip.compress(_layer_message(layer), compresslevel=9, mtime=0))
    return dict(sorted(layers.items()))


def _metadata(cx, cy, release, identity, old_meta):
    vector_layers = [{"id": name, "fields": FIELDS.get(name, {}), "minzoom": 8,
                      "maxzoom": 14 if name in OSM_LAYERS else 12} for name in LAYERS]
    metadata = {
        "name": "DingHai Seamap cell", "format": "pbf", "type": "overlay",
        "attribution": "© OpenStreetMap contributors · Protomaps; GEBCO_2026 Grid",
        "dinghai:schemaVersion": SCHEMA_VERSION, "dinghai:release": release,
        "dinghai:cell": f"8/{cx}/{cy}", "dinghai:generatorVersion": VERSION,
        "dinghai:generatorSHA256": identity["generatorSHA256"],
        "dinghai:sourceRepository": lp.SOURCE_REPOSITORY,
        "dinghai:osmAttribution": "© OpenStreetMap contributors",
        "dinghai:osmLicense": "ODbL-1.0", "dinghai:osmLicenseURL": lp.LAND_LICENSE_URL,
        "dinghai:osmCopyrightURL": lp.OSM_COPYRIGHT_URL,
        "dinghai:osmPbf": identity["provenance"]["osm"],
        "dinghai:landPolygons": identity["provenance"]["landPolygons"],
        "vector_layers": vector_layers,
    }
    # Preserve the public land-product provenance and illustrative-depth notices verbatim.
    if old_meta:
        metadata["dinghai:landGeneratorVersion"] = old_meta.get("dinghai:generatorVersion")
        metadata["dinghai:landGeneratorSHA256"] = old_meta.get("dinghai:generatorSHA256")
    for key, value in (old_meta or {}).items():
        if key.startswith("dinghai:") and key not in {"dinghai:schemaVersion", "dinghai:release", "dinghai:cell",
                                                       "dinghai:generatorVersion", "dinghai:generatorSHA256"}:
            metadata[key] = value
    return metadata


def occupied_cells(osm_pmtiles):
    """z8 cells with any OSM tile, from directories only (never tile bodies)."""
    archive = lp.Archive(osm_pmtiles)
    cells = set()

    def visit(directory, depth):
        if depth > 4:
            raise ValueError("OSM PMTiles directory too deep")
        for entry in directory:
            if not entry.run_length:
                leaf = archive._directory(archive.header["leaf_directory_offset"] + entry.offset, entry.length)
                visit(leaf, depth + 1)
                continue
            for offset in range(entry.run_length):
                z, x, y = tileid_to_zxy(entry.tile_id + offset)
                cells.add((x >> (z - 8), y >> (z - 8)))

    visit(archive.root, 1)
    return cells


def build_cell(osm_path, old_path, output_path, cx, cy, release, identity, has_marine=True):
    started = time.monotonic()
    osm = lp.Archive(osm_path)
    old = lp.Archive(old_path) if old_path else None
    old_meta = old.metadata() if old else None
    raw_tiles = {}
    maximum = 14 if has_marine else 12 if old else 7
    for z in range(8, maximum + 1):
        n = 1 << (z - 8)
        for x in range(cx * n, (cx + 1) * n):
            for y in range(cy * n, (cy + 1) * n):
                marine = osm.get(z, x, y) if has_marine else None
                base = old.get(z, x, y) if old and z <= 12 else None
                if marine is not None or base is not None:
                    raw_tiles[zxy_to_tileid(z, x, y)] = (marine or b"") + (base or b"")
    stats = _stats(raw_tiles)
    tiles = {tile_id: gzip.compress(raw, compresslevel=9, mtime=0) for tile_id, raw in raw_tiles.items()}
    metadata = _metadata(cx, cy, release, identity, old_meta)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    lp.write_pmtiles(temporary, tiles, metadata, lp.cell_bounds(cx, cy), 8, 14)
    os.replace(temporary, output_path)
    data = output_path.read_bytes()
    return {"x": cx, "y": cy, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "seconds": round(time.monotonic() - started, 3), "layers": stats}


def slice_cell(input_path, output_path, cx, cy):
    """Write only one cell's z8-14 OSM tiles, without decoding or re-encoding MVT layers."""
    archive = lp.Archive(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tiles = {}
    for z in range(8, 15):
        n = 1 << (z - 8)
        for x in range(cx * n, (cx + 1) * n):
            for y in range(cy * n, (cy + 1) * n):
                raw = archive.get(z, x, y)
                if raw is not None:
                    tiles[zxy_to_tileid(z, x, y)] = gzip.compress(raw, compresslevel=9, mtime=0)
    lp.write_pmtiles(output_path, tiles, {"name": "DingHai OSM cell slice", "format": "pbf"},
                     lp.cell_bounds(cx, cy), 8, 14)


def _cell_path(output, release, x, y):
    return Path(output) / "releases" / release / "cells" / "8" / str(x) / f"{y}.pmtiles"


def _old_cell(root, release, x, y):
    path = _cell_path(root, release, x, y)
    return path if path.is_file() else None


def _identity(osm_pmtiles, provenance_path, land_root, state_dir):
    provenance = json.loads(Path(provenance_path).read_text())
    if set(provenance) != {"osm", "landPolygons"}:
        raise ValueError("provenance must contain exactly osm and landPolygons")
    records = list(provenance["osm"]) if isinstance(provenance["osm"], list) else [provenance["osm"]]
    records.append(provenance["landPolygons"])
    for source in records:
        for key in ("url", "date", "sha256", "bytes"):
            if key not in source:
                raise ValueError(f"source provenance lacks {key}")
    old_manifest = json.loads((Path(land_root) / "manifest.json").read_text())
    return {"generatorSHA256": generator_sha256(), "osmPMTilesSHA256": lp.file_sha256(osm_pmtiles, state_dir),
            "provenance": provenance, "landManifest": old_manifest}


def build(osm_pmtiles, provenance, land_root, release, cells, output, state_dir, jobs=1):
    output, state_dir = Path(output), Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    identity = _identity(osm_pmtiles, provenance, land_root, state_dir)
    identity_text = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    identity_hash = hashlib.sha256(identity_text.encode()).hexdigest()
    old_release = identity["landManifest"]["release"]
    marine_cells = occupied_cells(osm_pmtiles)
    old_cells = {(c["x"], c["y"]): c for c in json.loads(
        (Path(land_root) / identity["landManifest"]["cells"]).read_text())}
    results = []

    def one(cell):
        x, y = cell
        state = state_dir / "cells" / f"{x}-{y}.json"
        if state.exists():
            record = json.loads(state.read_text())
            if record["identity"] == identity_hash and _cell_path(output, release, x, y).is_file():
                return record["entry"], record.get("seconds")
        result = build_cell(osm_pmtiles, _old_cell(land_root, old_release, x, y) if (x, y) in old_cells else None,
                            _cell_path(output, release, x, y), x, y, release, identity, cell in marine_cells)
        seconds = result.pop("seconds")
        _atomic_write(state, json.dumps({"identity": identity_hash, "entry": result, "seconds": seconds},
                                        separators=(",", ":")).encode())
        return result, seconds

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for entry, seconds in pool.map(one, cells):
            results.append(entry)
            print(json.dumps({**entry, "seconds": seconds}, ensure_ascii=False, separators=(",", ":")), flush=True)
    return finalize(output, release, cells, results, identity)


def finalize(output, release, cells, entries, identity):
    output = Path(output)
    entries.sort(key=lambda entry: (entry["x"], entry["y"]))
    cells_json = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode()
    release_root = output / "releases" / release
    _atomic_write(release_root / "cells.json", cells_json)
    expected = {Path(str(entry["x"])) / f"{entry['y']}.pmtiles" for entry in entries}
    actual = {path.relative_to(release_root / "cells" / "8") for path in (release_root / "cells" / "8").glob("*/*.pmtiles")}
    if actual != expected:
        raise ValueError(f"cell files and selection differ: {sorted(map(str, actual ^ expected))[:5]}")
    old = identity["landManifest"]
    sources = {key: old["sources"][key] for key in ("protomaps", "peaks", "gebco") if key in old.get("sources", {})}
    sources.update(identity["provenance"])
    sources["seamapPMTiles"] = {"sha256": identity["osmPMTilesSHA256"], "generator": "Planetiler 0.10.2 seamap-osm-1"}
    manifest = {
        "id": "dinghai-seamap", "manifestVersion": 1, "release": release, "schemaVersion": SCHEMA_VERSION,
        "gridZoom": 8, "minimumZoom": 8, "maximumZoom": 14, "layers": list(LAYERS), "sources": sources,
        "landRelease": old["release"], "attribution": "© OpenStreetMap contributors · Protomaps; GEBCO_2026 Grid",
        "landGeneratorIdentity": old.get("generatorIdentity"),
        "depth": old.get("depth"),
        "licences": {"osm": "ODbL-1.0", "gebco": "GEBCO terms of use"},
        "generatorIdentity": f"seamap_product.py:{VERSION}:sha256:{identity['generatorSHA256']}",
        "cellsURL": f"releases/{release}/cells/{{z}}/{{x}}/{{y}}.pmtiles",
        "cells": f"releases/{release}/cells.json", "cellsBytes": len(cells_json),
        "cellsSHA256": hashlib.sha256(cells_json).hexdigest(), "cellCount": len(entries),
        "totalBytes": sum(entry["bytes"] for entry in entries),
    }
    _atomic_write(output / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1).encode())
    return manifest


def _cell(text):
    x, y = (int(value) for value in text.split("/"))
    if not (0 <= x < 256 and 0 <= y < 256):
        raise argparse.ArgumentTypeError("cell must be X/Y in the z8 grid")
    return x, y


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osm-pmtiles", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--land-release", type=Path, required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    selected = parser.add_mutually_exclusive_group(required=True)
    selected.add_argument("--cell", type=_cell, action="append")
    selected.add_argument("--cells-file", type=Path)
    selected.add_argument("--all-cells", action="store_true")
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args(argv)
    cells = ([(x, y) for x in range(256) for y in range(256)] if args.all_cells else
             [tuple(cell) for cell in json.loads(args.cells_file.read_text())["cells"]] if args.cells_file else args.cell)
    manifest = build(args.osm_pmtiles, args.provenance, args.land_release, args.release, cells,
                     args.output, args.state_dir, args.jobs)
    print(json.dumps({key: manifest[key] for key in ("release", "cellCount", "totalBytes", "generatorIdentity")}))


if __name__ == "__main__":
    main()
