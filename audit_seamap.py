#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Structural audit of a dinghai-seamap release; reads local files only.

Uses only the Python standard library (own pmtiles/MVT decoding); nothing is imported
from the producers.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import struct
from pathlib import Path

OSM_LAYERS = {"seamark", "land", "water", "wetland", "sea_area"}
LAYERS = OSM_LAYERS | {"landuse", "roads", "peaks", "places", "depth"}
OVERVIEW_LAYERS = {"land", "water"}


def _varint(buf, i):
    result = shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7


def _zxy(tile_id):
    z = ((3 * tile_id + 1).bit_length() - 1) // 2
    pos = tile_id - ((1 << (2 * z)) - 1) // 3
    x = y = 0
    s = 1
    while s < 1 << z:
        rx = (pos // 2) & s
        ry = (pos ^ rx) & s
        if ry == 0:
            if rx != 0:
                x, y = s - 1 - x, s - 1 - y
            x, y = y, x
        x, y, pos, s = x + rx, y + ry, pos >> 1, s << 1
    return z, x, y


def read_pmtiles(data):
    """(header, metadata, {tile_id: gzip tile}) of a whole pmtiles v3 file."""
    if data[:7] != b"PMTiles" or data[7] != 3:
        raise ValueError("not a pmtiles v3 archive")
    h = dict(zip(("root_offset", "root_length", "metadata_offset", "metadata_length", "leaf_offset",
                  "leaf_length", "data_offset", "data_length"), struct.unpack_from("<8Q", data, 8)))
    h["internal"], h["tile_compression"], h["tile_type"], h["min_zoom"], h["max_zoom"] = data[97:102]
    if h["internal"] != 2 or h["tile_compression"] != 2 or h["tile_type"] != 1:
        raise ValueError("cell must be gzip MVT with gzip directories")
    metadata = json.loads(gzip.decompress(data[h["metadata_offset"]:h["metadata_offset"] + h["metadata_length"]]))
    tiles = {}

    def directory(offset, length):
        buf = gzip.decompress(data[offset:offset + length])
        n, i = _varint(buf, 0)
        columns = []
        for _ in range(4):
            column = []
            for _ in range(n):
                v, i = _varint(buf, i)
                column.append(v)
            columns.append(column)
        ids, runs, lengths, raw_offsets = columns
        tile_id, offsets = 0, []
        for k in range(n):
            offsets.append(offsets[k - 1] + lengths[k - 1] if raw_offsets[k] == 0 and k > 0 else raw_offsets[k] - 1)
        for delta, run, length, off in zip(ids, runs, lengths, offsets):
            tile_id += delta
            if run == 0:
                directory(h["leaf_offset"] + off, length)
                continue
            if off + length > h["data_length"]:
                raise ValueError("tile beyond data section")
            for k in range(run):
                tiles[tile_id + k] = data[h["data_offset"] + off:h["data_offset"] + off + length]

    directory(h["root_offset"], h["root_length"])
    if h["data_offset"] + h["data_length"] != len(data):
        raise ValueError("file length disagrees with header")
    return h, metadata, tiles


def _fields(buf):
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = _varint(buf, i)
        elif w == 2:
            n, i = _varint(buf, i)
            v, i = buf[i:i + n], i + n
            if i > len(buf):
                raise ValueError("truncated field")
        elif w == 1:
            v, i = buf[i:i + 8], i + 8
        elif w == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise ValueError("bad wire type")
        yield f, w, v


def layer_counts(tile):
    """{layer name: feature count} of one uncompressed MVT tile."""
    counts = {}
    for f, _, layer in _fields(tile):
        if f != 3:
            raise ValueError("unknown tile field")
        fields = list(_fields(layer))
        name = next(v.decode() for lf, _, v in fields if lf == 1)
        if name in counts:
            raise ValueError(f"duplicate layer {name}")
        counts[name] = sum(1 for lf, _, _ in fields if lf == 2)
    return counts


def audit(root):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    if (manifest["id"], manifest["schemaVersion"], manifest["gridZoom"]) != ("dinghai-seamap", 2, 8):
        raise ValueError("wrong product identity or grid")
    if set(manifest["layers"]) != LAYERS or (manifest["minimumZoom"], manifest["maximumZoom"]) != (8, 14):
        raise ValueError("wrong layer or zoom contract")
    cells_path = root / manifest["cells"]
    entries = json.loads(cells_path.read_text())
    cells_bytes = cells_path.read_bytes()
    if (len(cells_bytes), hashlib.sha256(cells_bytes).hexdigest()) != (manifest["cellsBytes"], manifest["cellsSHA256"]):
        raise ValueError("cells.json digest differs")
    if len(entries) != manifest["cellCount"] or sum(e["bytes"] for e in entries) != manifest["totalBytes"]:
        raise ValueError("manifest totals differ")
    overview = manifest["overview"]
    overview_data = (root / overview["url"]).read_bytes()
    if (len(overview_data), hashlib.sha256(overview_data).hexdigest()) != (overview["bytes"], overview["sha256"]):
        raise ValueError("overview bytes or sha256 differs")
    header, _, overview_tiles = read_pmtiles(overview_data)
    if (header["min_zoom"], header["max_zoom"]) != (0, 7):
        raise ValueError("overview must be z0-7")
    for tile_id, body in overview_tiles.items():
        z = _zxy(tile_id)[0]
        names = set(layer_counts(gzip.decompress(body)))
        if z > 7 or not names <= OVERVIEW_LAYERS:
            raise ValueError(f"overview has {sorted(names)} at z{z}")
    reports = []
    expected = set()
    for entry in entries:
        path = root / f"releases/{manifest['release']}/cells/8/{entry['x']}/{entry['y']}.pmtiles"
        expected.add(path)
        data = path.read_bytes()
        if (len(data), hashlib.sha256(data).hexdigest()) != (entry["bytes"], entry["sha256"]):
            raise ValueError(f"{path}: bytes or sha256 differs")
        header, metadata, tiles = read_pmtiles(data)
        if (header["min_zoom"], header["max_zoom"]) != (8, 14) or metadata["dinghai:schemaVersion"] != 2:
            raise ValueError(f"{path}: wrong header or metadata")
        if "Open Waters" in json.dumps(metadata):
            raise ValueError(f"{path}: third-party attribution leaked")
        counts = {}
        for tile_id, body in tiles.items():
            z, x, y = _zxy(tile_id)
            if not 8 <= z <= 14 or x >> (z - 8) != entry["x"] or y >> (z - 8) != entry["y"]:
                raise ValueError(f"{path}: tile outside cell or zoom range")
            for name, count in layer_counts(gzip.decompress(body)).items():
                if name not in LAYERS or (name not in OSM_LAYERS and z > 12):
                    raise ValueError(f"{path}: unexpected layer {name} at z{z}")
                counts[name] = counts.get(name, 0) + count
        reports.append({"x": entry["x"], "y": entry["y"], "bytes": len(data), "tiles": len(tiles), "features": counts})
    root_cells = root / f"releases/{manifest['release']}/cells/8"
    actual = set(root_cells.glob("*/*.pmtiles"))
    if actual != expected:
        raise ValueError(f"disk and cells.json differ: {sorted(map(str, actual ^ expected))[:5]}")
    return {"cellCount": len(entries), "totalBytes": sum(e["bytes"] for e in entries),
            "overview": {"bytes": len(overview_data), "tiles": len(overview_tiles)}, "cells": reports}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.root), ensure_ascii=False, indent=1))
