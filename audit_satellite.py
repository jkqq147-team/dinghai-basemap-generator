#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Audit local satellite v3 publication, including every stored WebP image; no network."""
from __future__ import annotations

import argparse
from collections import Counter
import gzip
import hashlib
from io import BytesIO
import json
from pathlib import Path
import statistics
import struct
import time

from PIL import Image
from audit_seamap import _varint, _zxy

ATTRIBUTION = "VersaTiles sources (https://versatiles.org/sources/)"


def read_pmtiles(data):
    if data[:8] != b"PMTiles\x03":
        raise ValueError("not PMTiles v3")
    names = ("root_offset", "root_length", "metadata_offset", "metadata_length", "leaf_offset",
             "leaf_length", "data_offset", "data_length", "addressed", "entries", "contents")
    h = dict(zip(names, struct.unpack_from("<11Q", data, 8)))
    h["internal"], h["compression"], h["type"], h["minzoom"], h["maxzoom"] = data[97:102]
    if (h["internal"], h["compression"], h["type"]) != (2, 1, 4):
        raise ValueError("expected WebP tiles, no tile compression, gzip directories")
    if not 0 <= h["minzoom"] <= h["maxzoom"] <= 13:
        raise ValueError("zooms outside satellite range")
    if h["data_offset"] + h["data_length"] != len(data):
        raise ValueError("file size disagrees with PMTiles header")
    metadata = json.loads(gzip.decompress(data[h["metadata_offset"]:h["metadata_offset"] + h["metadata_length"]]))
    tiles, visited = {}, set()

    def directory(offset, length, depth=0):
        if depth > 3 or (offset, length) in visited:
            raise ValueError("cyclic or overly deep directory")
        visited.add((offset, length))
        buf = gzip.decompress(data[offset:offset + length])
        n, i = _varint(buf, 0)
        columns = []
        for _ in range(4):
            column = []
            for _ in range(n):
                value, i = _varint(buf, i)
                column.append(value)
            columns.append(column)
        ids, runs, lengths, raw_offsets = columns
        tile_id, previous_end = 0, 0
        for k, (delta, run, size, raw_offset) in enumerate(zip(ids, runs, lengths, raw_offsets)):
            off = previous_end if raw_offset == 0 and k else raw_offset - 1
            previous_end = off + size
            tile_id += delta
            if off < 0 or size <= 0:
                raise ValueError("invalid directory range")
            if run == 0:
                if off + size > h["leaf_length"]:
                    raise ValueError("directory outside leaf section")
                directory(h["leaf_offset"] + off, size, depth + 1)
            else:
                if off + size > h["data_length"]:
                    raise ValueError("tile outside data section")
                for tid in range(tile_id, tile_id + run):
                    if tid in tiles:
                        raise ValueError("duplicate tile address")
                    tiles[tid] = data[h["data_offset"] + off:h["data_offset"] + off + size]

    directory(h["root_offset"], h["root_length"])
    if not tiles or len(tiles) != h["addressed"]:
        raise ValueError("empty cell or addressed tile count mismatch")
    return h, metadata, tiles


def check_image(blob):
    with Image.open(BytesIO(blob)) as image:
        if image.format != "WEBP" or image.size != (512, 512):
            raise ValueError("tile must be 512×512 WebP")
        image.load()


def audit_overview(data, source):
    h, meta, tiles = read_pmtiles(data)
    if (h["minzoom"], h["maxzoom"]) != (0, 6):
        raise ValueError("overview header must declare z0–6")
    if struct.unpack_from("<4i", data, 102) != (-1800000000, -850511288, 1800000000, 850511288):
        raise ValueError("overview must declare global Web-Mercator bounds")
    expected = {"format": "webp", "tile_type": "raster", "tileSize": 512,
                "attribution": ATTRIBUTION, "dinghai:schemaVersion": 3, "dinghai:source": source,
                "dinghai:overview": True, "minzoom": 0, "maxzoom": 6}
    if any(meta.get(k) != v for k, v in expected.items()):
        raise ValueError("incorrect overview metadata")
    counts, payload = Counter(), Counter()
    for tid, blob in tiles.items():
        z, x, y = _zxy(tid)
        if not (0 <= z <= 6 and 0 <= x < 1 << z and 0 <= y < 1 << z):
            raise ValueError("overview tile outside z0–6")
        check_image(blob)
        counts[z] += 1
        payload[z] += len(blob)
    if set(counts) != set(range(7)):
        raise ValueError("overview must contain every zoom z0–6")
    return {"tiles": len(tiles), "levels": dict(sorted(counts.items())),
            "payloadBytes": dict(sorted(payload.items()))}


def audit(root, estimate=None):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as images:
        return _audit(root, estimate, images)


def _audit(root, estimate, images):
    started = time.monotonic()
    root = Path(root)
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    for key, expected in (("id", "dinghai-satellite"), ("schemaVersion", 3), ("gridZoom", 8),
                          ("minimumZoom", 8), ("maximumZoom", 13), ("tileType", "webp"), ("tileSize", 512),
                          ("attribution", ATTRIBUTION)):
        if manifest[key] != expected:
            raise ValueError(f"unexpected manifest {key}")
    if manifest["record"] != f"releases/{manifest['release']}.json":
        raise ValueError("invalid release record path")
    if (root / manifest["record"]).read_bytes() != manifest_bytes:
        raise ValueError("release record differs from manifest")
    source = manifest["sources"]["satellite"]
    if not source.get("etag"):
        raise ValueError("source version missing")
    planned = None
    if estimate is not None:
        plan = json.loads(Path(estimate).read_text())
        if source != plan["source"] or not plan["withinBudget"]:
            raise ValueError("source or budget differs from estimate")
        planned = {(c["x"], c["y"]): c for c in plan["cells"]}
    cells, levels, seen, hashes = [], Counter(), set(), set()

    def hashed(kind, sha, suffix):
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("invalid content address")
        data = (root / kind / f"{sha}.{suffix}").read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"{kind} SHA-256 mismatch: {sha}")
        return data

    overview = manifest["overview"]
    overview_data = hashed("objects", overview["sha256"], "pmtiles")
    if len(overview_data) != overview["bytes"]:
        raise ValueError("overview size differs from manifest")
    overview_report = {**overview, **audit_overview(overview_data, source)}
    del overview_data
    for parent, shard in sorted(manifest["index"].items()):
        rows = json.loads(hashed("index", shard, "json"))
        if not rows or rows != sorted(rows, key=lambda row: (row[0], row[1])):
            raise ValueError("empty or unsorted index shard")
        for x, y, size, sha in rows:
            if not (0 <= x < 256 and 0 <= y < 256) or (x, y) in seen:
                raise ValueError("duplicate or invalid cell")
            if f"{x >> 4}/{y >> 4}" != parent:
                raise ValueError("wrong z4 index parent")
            seen.add((x, y))
            hashes.add(sha)
            data = hashed("objects", sha, "pmtiles")
            if len(data) != size:
                raise ValueError("object size differs from index")
            h, meta, tiles = read_pmtiles(data)
            expected_meta = {"format": "webp", "tile_type": "raster", "tileSize": 512,
                             "attribution": ATTRIBUTION, "dinghai:schemaVersion": 3,
                             "dinghai:cell": f"8/{x}/{y}", "dinghai:source": source,
                             "minzoom": h["minzoom"], "maxzoom": h["maxzoom"]}
            if any(meta.get(k) != v for k, v in expected_meta.items()):
                raise ValueError(f"incorrect cell metadata: {x}/{y}")
            counts, payload = Counter(), Counter()
            for tid, blob in tiles.items():
                z, tx, ty = _zxy(tid)
                if not (8 <= z <= 13 and tx >> (z - 8) == x and ty >> (z - 8) == y):
                    raise ValueError(f"tile outside its cell: {z}/{tx}/{ty}")
                counts[z] += 1
                payload[z] += len(blob)
            for _ in images.map(check_image, tiles.values()):
                pass
            if (min(counts), max(counts)) != (h["minzoom"], h["maxzoom"]):
                raise ValueError("header zooms disagree with actual tiles")
            if planned is not None:
                expected = planned.get((x, y))
                actual = {str(z): {"tiles": counts[z], "payloadBytes": payload[z]} for z in counts}
                if expected is None or actual != expected["levels"]:
                    raise ValueError(f"source-index inventory differs: {x}/{y}")
            levels.update(counts)
            cells.append({"x": x, "y": y, "bytes": size, "sha256": sha,
                          "tiles": len(tiles), "levels": dict(sorted(counts.items()))})
    if planned is not None and seen != set(planned):
        raise ValueError("selected coastal cells differ from estimate")
    sizes = [c["bytes"] for c in cells]
    if len(cells) != manifest["cellCount"] or sum(sizes) != manifest["totalBytes"]:
        raise ValueError("manifest cell/byte totals differ")
    if len(hashes) != len(cells):
        raise ValueError("multiple cells share an object despite cell-specific metadata")
    return {"cellCount": len(cells), "objectCount": len(hashes) + 1, "indexCount": len(manifest["index"]),
            "totalBytes": sum(sizes), "levels": dict(sorted(levels.items())),
            "maximumCellBytes": max(sizes), "medianCellBytes": statistics.median(sizes),
            "overview": overview_report,
            "seconds": round(time.monotonic() - started, 3), "cells": cells}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--estimate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.root, args.estimate)
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "cells"}))
