#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Cut the DingHai Seamap Planetiler archive into a global overview and z8 cells (seamap-cell-2).

The archive (planetiler/, one OSM snapshot + OSM land polygons + depth_product.py's
GEBCO depth Shapefiles) holds land/water at z0-14, seamark/wetland/sea_area at z8-14
and landuse/roads/peaks/places/depth at z8-12. Its z0-7 tiles become one overview
PMTiles; every selected z8 cell gets one file with its z8-14 tiles and a bytes/SHA-256
cells.json entry. Tiles are copied, never merged or re-encoded. No network or
third-party archive is read.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import threading
import time
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pmtiles.tile import (
    Compression, Entry, TileType, deserialize_directory, deserialize_header,
    find_tile, serialize_header, tileid_to_zxy, zxy_to_tileid,
)
from pmtiles.writer import optimize_directories

import depth_product

VERSION = "seamap-cell-2"
SCHEMA_VERSION = 2
GRID_ZOOM = 8
MIN_ZOOM, MAX_ZOOM = 8, 14
OSM_LAYERS = ("seamark", "land", "water", "wetland", "sea_area")
LAND_LAYERS = ("landuse", "roads", "peaks", "places", "depth")
LAYERS = OSM_LAYERS + LAND_LAYERS
LAYER_MAX_ZOOM = {name: 14 if name in OSM_LAYERS else 12 for name in LAYERS}
OVERVIEW_MAX_ZOOM = MIN_ZOOM - 1
OVERVIEW_LAYERS = ("land", "water")
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
ATTRIBUTION = "© OpenStreetMap contributors; GEBCO_2026 Grid"
OSM_ATTRIBUTION = "© OpenStreetMap contributors"
OSM_LICENSE_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
OSM_COPYRIGHT_URL = "https://www.openstreetmap.org/copyright"
SOURCE_REPOSITORY = "https://github.com/jkqq147-team/dinghai-basemap-generator"
ROOT = Path(__file__).parent
GENERATOR_FILES = ("seamap_product.py", "depth_product.py", "planetiler/pom.xml",
                   "planetiler/src/main/java/app/dinghai/SeamapCompiler.java")


def generator_sha256():
    h = hashlib.sha256()
    for name in GENERATOR_FILES:
        h.update((ROOT / name).read_bytes())
    return h.hexdigest()


def cell_bounds(x, y):
    """(west, south, east, north) degrees of z8 cell x/y."""
    n = 1 << GRID_ZOOM
    west, east = x / n * 360 - 180, (x + 1) / n * 360 - 180
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


# ---------------------------------------------------------------- pmtiles

class Archive:
    """Read-only pmtiles v3 random access with os.pread and a bounded leaf cache (one per thread)."""

    def __init__(self, path, leaf_cache=16):
        self.path = Path(path)
        self.fd = os.open(self.path, os.O_RDONLY)
        self.header = deserialize_header(self._read(0, 127))
        if self.header["tile_type"] != TileType.MVT or self.header["internal_compression"] != Compression.GZIP:
            raise ValueError(f"{path}: not a gzip-directory MVT archive")
        self.root = deserialize_directory(self._read(self.header["root_offset"], self.header["root_length"]))
        self.leaves = OrderedDict()
        self.leaf_cache = leaf_cache

    def _read(self, offset, length):
        data = os.pread(self.fd, length, offset)
        if len(data) != length:
            raise ValueError(f"{self.path}: truncated read")
        return data

    def metadata(self):
        return json.loads(gzip.decompress(self._read(self.header["metadata_offset"], self.header["metadata_length"])))

    def get(self, z, x, y):
        """Uncompressed MVT bytes of tile z/x/y, or None when the archive has no such tile."""
        tile_id = zxy_to_tileid(z, x, y)
        directory = self.root
        for _ in range(4):
            entry = find_tile(directory, tile_id)
            if entry is None:
                return None
            if entry.run_length > 0:
                data = self._read(self.header["tile_data_offset"] + entry.offset, entry.length)
                if self.header["tile_compression"] == Compression.GZIP:
                    data = gzip.decompress(data)
                elif self.header["tile_compression"] != Compression.NONE:
                    raise ValueError("unsupported tile compression")
                return data
            if entry.offset in self.leaves:
                self.leaves.move_to_end(entry.offset)
            else:
                self.leaves[entry.offset] = deserialize_directory(
                    self._read(self.header["leaf_directory_offset"] + entry.offset, entry.length))
                if len(self.leaves) > self.leaf_cache:
                    self.leaves.popitem(last=False)
            directory = self.leaves[entry.offset]
        raise ValueError("pmtiles directory too deep")


def write_pmtiles(path, tiles, metadata, bounds, min_zoom, max_zoom):
    """Writes {tile_id: gzip MVT} as a clustered pmtiles v3, deduplicating identical bytes.
    bounds = (west, south, east, north) degrees."""
    entries, offsets, blobs, offset = [], {}, [], 0
    for tile_id in sorted(tiles):
        data = tiles[tile_id]
        if data in offsets:
            last = entries[-1]
            if last.offset == offsets[data] and last.tile_id + last.run_length == tile_id:
                last.run_length += 1
                continue
            entries.append(Entry(tile_id, offsets[data], len(data), 1))
        else:
            offsets[data] = offset
            entries.append(Entry(tile_id, offset, len(data), 1))
            blobs.append(data)
            offset += len(data)
    root, leaves, _ = optimize_directories(entries, 16384 - 127)  # gzip-compressed by pmtiles
    meta = gzip.compress(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode(), mtime=0)
    west, south, east, north = bounds
    header = {
        "version": 3, "root_offset": 127, "root_length": len(root),
        "metadata_offset": 127 + len(root), "metadata_length": len(meta),
        "leaf_directory_offset": 127 + len(root) + len(meta), "leaf_directory_length": len(leaves),
        "tile_data_offset": 127 + len(root) + len(meta) + len(leaves), "tile_data_length": offset,
        "addressed_tiles_count": len(tiles), "tile_entries_count": len(entries),
        "tile_contents_count": len(blobs), "clustered": True,
        "internal_compression": Compression.GZIP, "tile_compression": Compression.GZIP,
        "tile_type": TileType.MVT, "min_zoom": min_zoom, "max_zoom": max_zoom,
        "min_lon_e7": round(west * 1e7), "min_lat_e7": round(south * 1e7),
        "max_lon_e7": round(east * 1e7), "max_lat_e7": round(north * 1e7),
        "center_zoom": min_zoom, "center_lon_e7": round((west + east) / 2 * 1e7),
        "center_lat_e7": round((south + north) / 2 * 1e7),
    }
    with open(path, "wb") as f:
        f.write(serialize_header(header))
        f.write(root)
        f.write(meta)
        f.write(leaves)
        for blob in blobs:
            f.write(blob)


# ---------------------------------------------------------------- MVT layer statistics

def _varint(buf, i):
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7


def _encode_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _fields(buf):
    """Yields (field, wire, value-or-bytes) of one protobuf message."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            length, i = _varint(buf, i)
            value, i = buf[i:i + length], i + length
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        else:
            raise ValueError("unsupported protobuf wire type")
        yield field, wire, value


def split_layers(tile):
    """{layer name: raw Layer message bytes} without decoding geometry."""
    layers = {}
    for field, wire, layer in _fields(memoryview(tile)):
        if field == 3 and wire == 2:
            name = next(bytes(v).decode() for f, _, v in _fields(layer) if f == 1)
            layers[name] = layer
    return layers


def _stats(tiles):
    layers = defaultdict(lambda: {"features": 0, "maxzoomFeatures": 0, "mvtBytes": 0, "gzipBytes": 0})
    for tile_id, raw in tiles.items():
        z = tileid_to_zxy(tile_id)[0]
        for name, layer in split_layers(raw).items():
            stat = layers[name]
            count = sum(1 for field, _, _ in _fields(layer) if field == 2)
            stat["features"] += count
            if z == LAYER_MAX_ZOOM[name]:
                stat["maxzoomFeatures"] += count
            stat["mvtBytes"] += len(layer)
            # Layer sizes are individually compressed MVT Layer messages, not additive PMTiles spans.
            message = b"\x1a" + _encode_varint(len(layer)) + layer
            stat["gzipBytes"] += len(gzip.compress(message, compresslevel=9, mtime=0))
    return dict(sorted(layers.items()))


# ---------------------------------------------------------------- cells

def _metadata(cx, cy, release, identity):
    depth = identity["depth"]
    return {
        "name": "DingHai Seamap cell", "format": "pbf", "type": "overlay", "attribution": ATTRIBUTION,
        "dinghai:schemaVersion": SCHEMA_VERSION, "dinghai:release": release,
        "dinghai:cell": f"{GRID_ZOOM}/{cx}/{cy}", "dinghai:generatorVersion": VERSION,
        "dinghai:generatorSHA256": identity["generatorSHA256"],
        "dinghai:sourceRepository": SOURCE_REPOSITORY,
        "dinghai:osmAttribution": OSM_ATTRIBUTION,
        "dinghai:osmLicense": "ODbL-1.0", "dinghai:osmLicenseURL": OSM_LICENSE_URL,
        "dinghai:osmCopyrightURL": OSM_COPYRIGHT_URL,
        "dinghai:osmPbf": identity["provenance"]["osm"],
        "dinghai:landPolygons": identity["provenance"]["landPolygons"],
        "dinghai:depthAttribution": depth["gebco"]["attribution"], "dinghai:depthDOI": depth["gebco"]["doi"],
        "dinghai:depthTermsURL": depth["gebco"]["termsURL"], "dinghai:depthNotice": depth["notice"],
        "dinghai:gebcoGrid": depth["gebco"]["grid"], "dinghai:gebcoSHA256": depth["gebco"]["sha256"],
        "dinghai:depth": depth["params"],
        "vector_layers": [{"id": name, "fields": FIELDS[name], "minzoom": MIN_ZOOM,
                           "maxzoom": LAYER_MAX_ZOOM[name]} for name in LAYERS],
    }


def build_cell(archive, output_path, cx, cy, release, identity):
    started = time.monotonic()
    raw_tiles = {}
    for z in range(MIN_ZOOM, MAX_ZOOM + 1):
        n = 1 << (z - GRID_ZOOM)
        for x in range(cx * n, (cx + 1) * n):
            for y in range(cy * n, (cy + 1) * n):
                raw = archive.get(z, x, y)
                if raw is not None:
                    raw_tiles[zxy_to_tileid(z, x, y)] = raw
    tiles = {tile_id: gzip.compress(raw, compresslevel=9, mtime=0) for tile_id, raw in raw_tiles.items()}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    write_pmtiles(temporary, tiles, _metadata(cx, cy, release, identity), cell_bounds(cx, cy), MIN_ZOOM, MAX_ZOOM)
    os.replace(temporary, output_path)
    data = output_path.read_bytes()
    return {"x": cx, "y": cy, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "seconds": round(time.monotonic() - started, 3), "layers": _stats(raw_tiles)}


def build_overview(archive, output, release, identity):
    """The archive's z0-7 tiles (land and inland water) as one global PMTiles."""
    tiles = {}
    for z in range(OVERVIEW_MAX_ZOOM + 1):
        for x in range(1 << z):
            for y in range(1 << z):
                raw = archive.get(z, x, y)
                if raw is not None:
                    tiles[zxy_to_tileid(z, x, y)] = gzip.compress(raw, compresslevel=9, mtime=0)
    metadata = {
        "name": "DingHai Seamap overview", "format": "pbf", "type": "baselayer", "attribution": OSM_ATTRIBUTION,
        "dinghai:schemaVersion": SCHEMA_VERSION, "dinghai:release": release, "dinghai:generatorVersion": VERSION,
        "dinghai:generatorSHA256": identity["generatorSHA256"], "dinghai:sourceRepository": SOURCE_REPOSITORY,
        "dinghai:osmLicense": "ODbL-1.0", "dinghai:osmLicenseURL": OSM_LICENSE_URL,
        "dinghai:osmCopyrightURL": OSM_COPYRIGHT_URL,
        "dinghai:osmPbf": identity["provenance"]["osm"], "dinghai:landPolygons": identity["provenance"]["landPolygons"],
        "vector_layers": [{"id": name, "fields": {}, "minzoom": 0, "maxzoom": OVERVIEW_MAX_ZOOM}
                          for name in OVERVIEW_LAYERS],
    }
    path = Path(output) / "releases" / release / "overview.pmtiles"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    write_pmtiles(temporary, tiles, metadata, (-180.0, -85.0511287798066, 180.0, 85.0511287798066),
                  0, OVERVIEW_MAX_ZOOM)
    os.replace(temporary, path)
    data = path.read_bytes()
    return {"url": f"releases/{release}/overview.pmtiles", "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "minimumZoom": 0, "maximumZoom": OVERVIEW_MAX_ZOOM,
            "layers": list(OVERVIEW_LAYERS)}


def cell_path(output, release, x, y):
    return Path(output) / "releases" / release / "cells" / str(GRID_ZOOM) / str(x) / f"{y}.pmtiles"


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _identity(archive_path, provenance_path, depth_dir):
    provenance = json.loads(Path(provenance_path).read_text())
    if set(provenance) != {"osm", "landPolygons"}:
        raise ValueError("provenance must contain exactly osm and landPolygons")
    records = list(provenance["osm"]) if isinstance(provenance["osm"], list) else [provenance["osm"]]
    for source in records + [provenance["landPolygons"]]:
        for key in ("url", "date", "sha256", "bytes"):
            if key not in source:
                raise ValueError(f"source provenance lacks {key}")
    return {"generatorSHA256": generator_sha256(), "archiveSHA256": depth_product.file_sha256(archive_path),
            "provenance": provenance, "depth": depth_product.read_identity(depth_dir)}


def build(archive_path, provenance, depth_dir, release, cells, output, state_dir, jobs=1):
    output, state_dir = Path(output), Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    identity = _identity(archive_path, provenance, depth_dir)
    identity_hash = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    local = threading.local()

    def one(cell):
        x, y = cell
        state = state_dir / "cells" / f"{x}-{y}.json"
        if state.exists():
            record = json.loads(state.read_text())
            if record["identity"] == identity_hash and cell_path(output, release, x, y).is_file():
                return record["entry"], record["seconds"]
        if not hasattr(local, "archive"):
            local.archive = Archive(archive_path)
        result = build_cell(local.archive, cell_path(output, release, x, y), x, y, release, identity)
        seconds = result.pop("seconds")
        _atomic_write(state, json.dumps({"identity": identity_hash, "entry": result, "seconds": seconds},
                                        separators=(",", ":")).encode())
        return result, seconds

    overview = build_overview(Archive(archive_path), output, release, identity)
    print(json.dumps({"overview": overview}), flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for entry, seconds in pool.map(one, cells):
            results.append(entry)
            print(json.dumps({**entry, "seconds": seconds}, ensure_ascii=False, separators=(",", ":")), flush=True)
    return finalize(output, release, results, identity, overview)


def finalize(output, release, entries, identity, overview):
    output = Path(output)
    entries.sort(key=lambda entry: (entry["x"], entry["y"]))
    cells_json = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode()
    release_root = output / "releases" / release
    _atomic_write(release_root / "cells.json", cells_json)
    expected = {Path(str(entry["x"])) / f"{entry['y']}.pmtiles" for entry in entries}
    actual = {path.relative_to(release_root / "cells" / "8") for path in (release_root / "cells" / "8").glob("*/*.pmtiles")}
    if actual != expected:
        raise ValueError(f"cell files and selection differ: {sorted(map(str, actual ^ expected))[:5]}")
    depth = identity["depth"]
    sources = dict(identity["provenance"])
    sources["gebco"] = {key: depth["gebco"][key] for key in ("grid", "doi", "bytes", "sha256", "coverage")}
    sources["seamapPMTiles"] = {"sha256": identity["archiveSHA256"], "generator": "Planetiler 0.10.2 seamap-osm-2"}
    manifest = {
        "id": "dinghai-seamap", "manifestVersion": 1, "release": release, "schemaVersion": SCHEMA_VERSION,
        "gridZoom": GRID_ZOOM, "minimumZoom": MIN_ZOOM, "maximumZoom": MAX_ZOOM, "layers": list(LAYERS),
        "sources": sources, "attribution": ATTRIBUTION,
        "depth": {"thresholdMeters": depth["params"]["bandsMeters"][0], **depth["params"],
                  "property": "min", "notice": depth["notice"]},
        "licences": {"osm": "ODbL-1.0", "gebco": "GEBCO terms of use"},
        "generatorIdentity": f"seamap_product.py:{VERSION}:sha256:{identity['generatorSHA256']}",
        "depthGeneratorIdentity": f"depth_product.py:{depth['generatorVersion']}:sha256:{depth['generatorSHA256']}",
        "cellsURL": f"releases/{release}/cells/{{z}}/{{x}}/{{y}}.pmtiles",
        "cells": f"releases/{release}/cells.json", "cellsBytes": len(cells_json),
        "cellsSHA256": hashlib.sha256(cells_json).hexdigest(), "cellCount": len(entries),
        "totalBytes": sum(entry["bytes"] for entry in entries), "overview": overview,
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
    parser.add_argument("--archive", type=Path, required=True, help="the compiler's PMTiles output")
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--depth", type=Path, required=True, help="depth_product.py output that compile read")
    parser.add_argument("--release", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    selected = parser.add_mutually_exclusive_group(required=True)
    selected.add_argument("--cell", type=_cell, action="append")
    selected.add_argument("--all-cells", action="store_true")
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args(argv)
    cells = [(x, y) for x in range(256) for y in range(256)] if args.all_cells else args.cell
    manifest = build(args.archive, args.provenance, args.depth, args.release, cells,
                     args.output, args.state_dir, args.jobs)
    print(json.dumps({key: manifest[key] for key in ("release", "cellCount", "totalBytes", "generatorIdentity")}))


if __name__ == "__main__":
    main()
