#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Add the approved z0–6 overview to a completed, unpublished satellite release.

Kept outside the cell generator's identity: adding an overview never invalidates
cell receipts. This command only rewrites the local, explicitly unpublished record.
"""
from collections import defaultdict
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time

from pmtiles.tile import Compression, Entry, TileType, deserialize_directory, zxy_to_tileid
from pmtiles.writer import finalize_header

from depth_product import file_sha256
from seamap_product import _atomic_write, _gzip_header, _install_immutable
from satellite_product import ATTRIBUTION, Remote, pinned_source, verify_cli


def write_overview(mbtiles, path, source):
    tiles, counts = {}, defaultdict(lambda: {"tiles": 0, "payloadBytes": 0})
    with sqlite3.connect(f"file:{Path(mbtiles).resolve()}?mode=ro", uri=True) as db:
        for z, x, tms_y, blob in db.execute("SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles"):
            y = (1 << z) - 1 - tms_y
            if not (0 <= z <= 6 and 0 <= x < 1 << z and 0 <= y < 1 << z):
                raise ValueError("overview tile outside global z0–6")
            tile_id = zxy_to_tileid(z, x, y)
            if tile_id in tiles:
                raise ValueError("duplicate overview tile")
            if blob[:4] != b"RIFF" or blob[8:12] != b"WEBP":
                raise ValueError("overview source tile is not WebP")
            tiles[tile_id] = blob
            counts[z]["tiles"] += 1
            counts[z]["payloadBytes"] += len(blob)
    if set(counts) != set(range(7)):
        raise ValueError("overview must contain every zoom z0–6")
    entries, offsets, blobs, offset = [], {}, [], 0
    for tile_id, blob in sorted(tiles.items()):
        if blob not in offsets:
            offsets[blob] = offset
            blobs.append(blob)
            offset += len(blob)
        entries.append(Entry(tile_id, offsets[blob], len(blob), 1))
    metadata = {"name": "DingHai satellite overview", "format": "webp", "type": "baselayer",
                "tile_type": "raster", "tileSize": 512, "attribution": ATTRIBUTION,
                "minzoom": 0, "maxzoom": 6, "dinghai:schemaVersion": 3,
                "dinghai:overview": True, "dinghai:source": source}
    header = {"tile_type": TileType.WEBP, "tile_compression": Compression.NONE,
              "min_lon_e7": -1800000000, "min_lat_e7": -850511288,
              "max_lon_e7": 1800000000, "max_lat_e7": 850511288,
              "center_zoom": 0, "center_lon_e7": 0, "center_lat_e7": 0}
    header_bytes, root, meta, leaves = finalize_header(
        header, len(tiles), entries, len(blobs), metadata, True, offset)
    leaves = bytearray(leaves)
    for entry in deserialize_directory(root):
        if entry.run_length == 0:
            start, end = entry.offset, entry.offset + entry.length
            leaves[start:end] = _gzip_header(leaves[start:end])
    with Path(path).open("wb") as f:
        for chunk in (header_bytes, _gzip_header(root), _gzip_header(meta), leaves, *blobs):
            f.write(chunk)
    return dict(counts)


def publish_overview(root, manifest_bytes, overview, identity):
    root = Path(root)
    if (root / "manifest.json").read_bytes() != manifest_bytes:
        raise ValueError("manifest changed while overview was being generated")
    manifest = json.loads(manifest_bytes)
    manifest["overview"] = overview
    manifest["overviewGeneratorIdentity"] = identity
    data = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    # User-approved exception: same local release, not yet uploaded. Cell objects,
    # shards, receipts and the original cell generator identity remain unchanged.
    _atomic_write(root / manifest["record"], data)
    _atomic_write(root / "manifest.json", data)


def add_overview(root, state, cache, estimate, cli, verification):
    root, state = Path(root), Path(state)
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    plan = json.loads(Path(estimate).read_text())
    if (manifest["cellCount"] != plan["cellCount"]
            or manifest["sources"]["satellite"] != plan["source"]):
        raise ValueError("complete coastal release from the same estimate is required")
    record_path = root / manifest["record"]
    if record_path.read_bytes() != manifest_bytes:
        raise ValueError("manifest and release record differ before overview addition")
    cli_record = verify_cli(cli, verification)
    if cli_record["executableSHA256"] != manifest["cli"]["executableSHA256"]:
        raise ValueError("overview must use the same verified CLI")
    state.mkdir(parents=True, exist_ok=True)
    remote = Remote(cache, plan["source"])
    expected = defaultdict(lambda: {"tiles": 0, "payloadBytes": 0})
    for block in remote.blocks():
        if block[0] <= 6:
            for z, x, y, size in remote.tile_index(block):
                expected[z]["tiles"] += 1
                expected[z]["payloadBytes"] += size
    estimate_record = {"source": remote.identity, "levels": dict(sorted(expected.items())),
                       "tiles": sum(v["tiles"] for v in expected.values()),
                       "payloadBytes": sum(v["payloadBytes"] for v in expected.values())}
    _atomic_write(state / "overview-estimate.json", json.dumps(estimate_record, indent=2).encode())
    identity = "satellite_overview.py:sha256:" + file_sha256(__file__)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(dir=state, prefix="overview-") as tmp:
        mbtiles, pmtiles = Path(tmp) / "overview.mbtiles", Path(tmp) / "overview.pmtiles"
        with pinned_source(remote) as source, (state / "overview-cli.log").open("w") as log:
            command = [str(cli), "convert", source, str(mbtiles), "--min-zoom", "0", "--max-zoom", "6"]
            log.write(json.dumps(command) + "\n")
            log.flush()
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True,
                           env={**os.environ, "RAYON_NUM_THREADS": "1", "VERSATILES_NET_MAX_RETRIES": "3"})
        counts = write_overview(mbtiles, pmtiles, remote.identity)
        if counts != expected:
            raise ValueError("overview CLI output differs from pinned source index")
        # Independently read/decode before permitting the local record replacement.
        from audit_satellite import audit_overview
        audit_overview(pmtiles.read_bytes(), remote.identity)
        digest = file_sha256(pmtiles)
        overview = {"sha256": digest, "bytes": pmtiles.stat().st_size}
        _install_immutable(pmtiles, root / "objects" / f"{digest}.pmtiles")
    remote.fetch(0, 66)
    publish_overview(root, manifest_bytes, overview, identity)
    result = {**overview, "levels": counts, "seconds": round(time.monotonic() - started, 3),
              "generatorIdentity": identity}
    _atomic_write(state / "overview-result.json", json.dumps(result, indent=2).encode())
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--estimate", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--cli-verification", type=Path, required=True)
    parser.add_argument("--replace-unpublished-record", action="store_true", required=True)
    args = parser.parse_args()
    print(json.dumps(add_overview(args.root, args.state, args.cache, args.estimate,
                                 args.cli, args.cli_verification), indent=2))
