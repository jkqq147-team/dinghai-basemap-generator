#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Build the coastal DingHai satellite product from the published VersaTiles container."""
from __future__ import annotations

from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import argparse
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import tarfile
import tempfile
import threading
import time

import brotli
import shapely
from pmtiles.tile import Compression, Entry, TileType, deserialize_directory, zxy_to_tileid
from pmtiles.writer import finalize_header

import depth_product
from seamap_product import _atomic_write, _gzip_header, _install_immutable, cell_bounds

GRID_ZOOM = 8
MIN_ZOOM, MAX_ZOOM = 8, 13
ATTRIBUTION = "VersaTiles sources (https://versatiles.org/sources/)"
# Canonical location; the former /satellite.versatiles answers 301 here. Redirects
# are not followed, so the URL recorded in the source identity is the one read.
SOURCE_URL = "https://download.versatiles.org/satellite-raster/satellite-raster.versatiles"
# Central Pacific, west=-150.46875°, north=0°; checked to be entirely water.
OCEAN_SEED = (21, 128)
LIMIT_BYTES = 300_000_000_000
VERSION = "satellite-cell-1"


def ocean_cells(fractions, seed=OCEAN_SEED):
    """8-neighbour flood through non-land cells, wrapping longitude but not latitude."""
    if fractions[seed[0]][seed[1]] != 0:
        raise ValueError(f"ocean seed is not a full-water cell: {seed}")
    reached = {seed}
    pending = deque([seed])
    while pending:
        x, y = pending.popleft()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cell = ((x + dx) % 256, y + dy)
                if (0 <= cell[1] < 256 and cell not in reached
                        and fractions[cell[0]][cell[1]] < 1):
                    reached.add(cell)
                    pending.append(cell)
    return reached


def coastal_cells(land_index):
    """Use the existing full-resolution land index, not a rasterised island mask."""
    fractions = [[0.0] * 256 for _ in range(256)]
    with depth_product.open_index(land_index) as db:
        source = depth_product.index_source(db)
        current, pieces = None, []

        def finish():
            if current is None:
                return
            x, y = divmod(current, 256)
            size = depth_product.CELL_PX
            cell = shapely.box(x * size, y * size, (x + 1) * size, (y + 1) * size)
            land = shapely.union_all(pieces).intersection(cell)
            # covers distinguishes wholly dry cells without an arbitrary area tolerance.
            fractions[x][y] = 1.0 if land.covers(cell) else land.area / cell.area

        for cell, wkb in db.execute("SELECT cell, wkb FROM land ORDER BY cell"):
            if cell != current:
                finish()
                current, pieces = cell, []
            pieces.append(shapely.from_wkb(wkb))
        finish()
    reached = ocean_cells(fractions)
    cells = sorted((x, y) for x, y in reached if fractions[x][y] > 0)
    checks = {}
    for name, cell in (("caspian", (163, 95)), ("hongKong", (209, 111)), ("kiel", (135, 81))):
        checks[name] = {"cell": f"8/{cell[0]}/{cell[1]}", "landFraction": fractions[cell[0]][cell[1]],
                        "oceanConnected": cell in reached, "selected": cell in cells}
    return {"landPolygons": source, "oceanSeed": list(OCEAN_SEED), "cellCount": len(cells),
            "cells": cells, "landFractions": fractions, "selectionChecks": checks}


class Remote:
    """Bounded HTTP ranges; never accept a whole-file response or a changed ETag."""

    def __init__(self, cache, identity=None):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.identity = identity
        self.slots = threading.Semaphore(4)
        header, headers = self.fetch(0, 66)
        if identity is None:
            if not headers.get("etag") or headers["etag"].startswith("W/"):
                raise ValueError("source lacks a strong ETag")
            self.identity = {"url": SOURCE_URL, "etag": headers["etag"],
                             "bytes": int(headers["content-range"].split("/")[1])}
        self.header = header
        if header[:14] != b"versatiles_v02" or header[14:16] != bytes((18, 0)):
            raise ValueError("source is not uncompressed WebP VersaTiles v02")

    def fetch(self, offset, length):
        with self.slots, tempfile.NamedTemporaryFile(dir=self.cache, suffix=".headers") as f:
            command = ["curl", "--fail", "--silent", "--show-error", "--retry", "3",
                       "--connect-timeout", "30", "--max-time", "300",
                       "--max-filesize", str(length), "--range", f"{offset}-{offset + length - 1}",
                       "--dump-header", f.name]
            if self.identity is not None:
                command += ["--header", "If-Match: " + self.identity["etag"]]
            response = subprocess.run(command + [SOURCE_URL], capture_output=True, check=True)
            raw = Path(f.name).read_text()
        headers = dict((k.lower(), v.strip()) for k, v in
                       (line.split(":", 1) for line in raw.splitlines() if ":" in line))
        expected = f"bytes {offset}-{offset + length - 1}/"
        if (not headers.get("content-range", "").startswith(expected)
                or len(response.stdout) != length):
            raise ValueError(f"source ignored range {offset}+{length}: {raw}")
        if self.identity is not None and headers.get("etag") != self.identity["etag"]:
            raise ValueError("source version changed; use a new estimate and release")
        return response.stdout, headers

    def index_range(self, offset, length):
        key = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()).hexdigest()
        path = self.cache / key / f"{offset}-{length}"
        if path.exists():
            data = path.read_bytes()
            if len(data) != length:
                raise ValueError(f"truncated index cache: {path}")
            return data
        data, _ = self.fetch(offset, length)
        _atomic_write(path, data)
        return data

    def blocks(self):
        offset, length = struct.unpack_from(">QQ", self.header, 50)
        data = brotli.decompress(self.index_range(offset, length))
        if len(data) % 33:
            raise ValueError("truncated block index")
        blocks = list(struct.iter_unpack(">BIIBBBBQQI", data))
        if len({b[:3] for b in blocks}) != len(blocks):
            raise ValueError("duplicate source blocks")
        return blocks

    def tile_index(self, block):
        z, bx, by, x0, y0, x1, y1, offset, payload, length = block
        data = brotli.decompress(self.index_range(offset + payload, length))
        if len(data) != (x1 - x0 + 1) * (y1 - y0 + 1) * 12:
            raise ValueError("tile index length disagrees with block bounds")
        for i, (off, size) in enumerate(struct.iter_unpack(">QI", data)):
            if size:
                if off + size > payload:
                    raise ValueError("tile points outside its block")
                yield z, bx * 256 + x0 + i % (x1 - x0 + 1), by * 256 + y0 + i // (x1 - x0 + 1), size


def estimate(selection, cache):
    """Read only container indexes; sum stored tile lengths, without dedup discounts."""
    started = time.monotonic()
    remote = Remote(cache)
    cells = {tuple(c): defaultdict(lambda: {"tiles": 0, "payloadBytes": 0}) for c in selection["cells"]}
    blocks = [b for b in remote.blocks() if MIN_ZOOM <= b[0] <= MAX_ZOOM]

    def one(block):
        return list(remote.tile_index(block))

    with ThreadPoolExecutor(max_workers=4) as pool:
        for tiles in pool.map(one, blocks):
            for z, x, y, size in tiles:
                cell = (x >> (z - 8), y >> (z - 8))
                if cell in cells:
                    cells[cell][z]["tiles"] += 1
                    cells[cell][z]["payloadBytes"] += size
    records, levels = [], defaultdict(lambda: {"tiles": 0, "payloadBytes": 0})
    for (x, y), counts in sorted(cells.items()):
        if not counts:
            raise ValueError(f"coastal cell has no source tiles: 8/{x}/{y}")
        for z, count in counts.items():
            for key, value in count.items():
                levels[z][key] += value
        tile_count = sum(c["tiles"] for c in counts.values())
        payload = sum(c["payloadBytes"] for c in counts.values())
        # Each PMTiles entry has four <=10-byte varints; 64 KiB covers headers,
        # metadata, gzip framing and the small index/release JSON share per cell.
        upper = payload + tile_count * 40 + 65536
        records.append({"x": x, "y": y, "levels": dict(counts), "tiles": tile_count,
                        "payloadBytes": payload, "estimatedUpperBytes": upper})
    result = {k: selection[k] for k in ("landPolygons", "oceanSeed", "cellCount", "selectionChecks")}
    result.update({"source": remote.identity, "gridZoom": 8, "minimumZoom": 8, "maximumZoom": 13,
                   "method": "Exact indexed tile lengths without deduplication, plus 40 bytes/tile and 64 KiB/cell",
                   "levels": dict(sorted(levels.items())), "cells": records,
                   "payloadBytes": sum(r["payloadBytes"] for r in records),
                   "estimatedUpperBytes": sum(r["estimatedUpperBytes"] for r in records),
                   "limitBytes": LIMIT_BYTES, "indexBlocks": len(blocks),
                   "seconds": round(time.monotonic() - started, 3)})
    result["withinBudget"] = result["estimatedUpperBytes"] <= LIMIT_BYTES
    return result


@contextmanager
def pinned_source(remote):
    """The official CLI reads this loopback range endpoint; all origin reads use If-Match.

    The semaphore in Remote caps aggregate community-service concurrency at four,
    including requests made concurrently inside each CLI process.
    """
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", str(remote.identity["bytes"]))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

        def do_GET(self):
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
            if not match:
                self.send_error(416, "a bounded byte range is required")
                return
            start, end = map(int, match.groups())
            length = end - start + 1
            if length <= 0 or end >= remote.identity["bytes"]:
                self.send_error(416)
                return
            try:
                key = hashlib.sha256(json.dumps(remote.identity, sort_keys=True).encode()).hexdigest()
                cached = remote.cache / key / f"{start}-{length}"
                data = cached.read_bytes() if cached.exists() else remote.fetch(start, length)[0]
                if len(data) != length:
                    raise ValueError("truncated cached range")
                self.send_response(206)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Range", f"bytes {start}-{end}/{remote.identity['bytes']}")
                self.send_header("ETag", remote.identity["etag"])
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                self.send_error(502, str(exc))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/satellite.versatiles"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def write_cell(mbtiles, path, x, y, source):
    """Copy source WebP blobs without decoding/encoding; SQLite rows are TMS."""
    tiles, counts = {}, defaultdict(lambda: {"tiles": 0, "payloadBytes": 0})
    with sqlite3.connect(f"file:{Path(mbtiles).resolve()}?mode=ro", uri=True) as db:
        for z, tx, tms_y, data in db.execute("SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles"):
            ty = (1 << z) - 1 - tms_y
            if not (8 <= z <= 13 and tx >> (z - 8) == x and ty >> (z - 8) == y):
                raise ValueError(f"CLI returned a tile outside 8/{x}/{y}: {z}/{tx}/{ty}")
            tile_id = zxy_to_tileid(z, tx, ty)
            if tile_id in tiles:
                raise ValueError("duplicate source tile")
            if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
                raise ValueError("source tile is not WebP")
            tiles[tile_id] = data
            counts[z]["tiles"] += 1
            counts[z]["payloadBytes"] += len(data)
    if not tiles:
        raise ValueError(f"empty coastal cell: 8/{x}/{y}")
    entries, offsets, blobs, offset = [], {}, [], 0
    for tile_id, data in sorted(tiles.items()):
        if data not in offsets:
            offsets[data] = offset
            blobs.append(data)
            offset += len(data)
        entries.append(Entry(tile_id, offsets[data], len(data), 1))
    west, south, east, north = cell_bounds(x, y)
    metadata = {"name": f"DingHai satellite 8/{x}/{y}", "format": "webp", "type": "baselayer",
                "tile_type": "raster", "tileSize": 512, "attribution": ATTRIBUTION,
                "minzoom": min(counts), "maxzoom": max(counts), "dinghai:schemaVersion": 3,
                "dinghai:cell": f"8/{x}/{y}", "dinghai:source": source}
    header = {"tile_type": TileType.WEBP, "tile_compression": Compression.NONE,
              "min_lon_e7": round(west * 1e7), "min_lat_e7": round(south * 1e7),
              "max_lon_e7": round(east * 1e7), "max_lat_e7": round(north * 1e7),
              "center_zoom": 8, "center_lon_e7": round((west + east) / 2 * 1e7),
              "center_lat_e7": round((south + north) / 2 * 1e7)}
    header_bytes, root, meta, leaves = finalize_header(
        header, len(tiles), entries, len(blobs), metadata, True, offset)
    leaves = bytearray(leaves)
    for entry in deserialize_directory(root):
        if entry.run_length == 0:
            a, b = entry.offset, entry.offset + entry.length
            leaves[a:b] = _gzip_header(leaves[a:b])
    with Path(path).open("wb") as f:
        for chunk in (header_bytes, _gzip_header(root), _gzip_header(meta), leaves, *blobs):
            f.write(chunk)
    return dict(counts)


def verify_cli(cli, verification):
    """Require both the publisher-verified distribution record and its exact executable."""
    record = json.loads(Path(verification).read_text())
    if record["asset"]["digest"] != "sha256:" + record["verifiedSHA256"]:
        raise ValueError("CLI publisher checksum disagrees with verified distribution")
    if depth_product.file_sha256(cli) != record["executableSHA256"]:
        raise ValueError("CLI executable differs from verified distribution")
    if subprocess.check_output([str(cli), "--version"], text=True).strip() != "versatiles 5.7.0":
        raise ValueError("requires official versatiles 5.7.0")
    return record


def verify_distribution(cli, archive, output):
    url = "https://api.github.com/repos/versatiles-org/versatiles-rs/releases/tags/v5.7.0"
    release = json.loads(subprocess.check_output(["curl", "--fail", "--silent", "--show-error", url]))
    asset = next(a for a in release["assets"] if a["name"] == Path(archive).name)
    digest = depth_product.file_sha256(archive)
    if asset["digest"] != "sha256:" + digest:
        raise ValueError("distribution differs from the publisher SHA-256")
    with tarfile.open(archive) as tar:
        member = next(m for m in tar.getmembers() if m.name.rsplit("/", 1)[-1] == "versatiles")
        executable = tar.extractfile(member).read()
    executable_hash = hashlib.sha256(executable).hexdigest()
    if executable_hash != depth_product.file_sha256(cli):
        raise ValueError("executable differs from verified distribution")
    record = {"release": url, "asset": asset, "verifiedSHA256": digest,
              "executableSHA256": executable_hash}
    _atomic_write(output, json.dumps(record, indent=2).encode())
    verify_cli(cli, output)
    return record


def finalize(output, release, entries, identity):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", release):
        raise ValueError("release must be a filename component")
    output = Path(output)
    shards = defaultdict(list)
    for entry in sorted(entries, key=lambda e: (e["x"], e["y"])):
        x, y = entry["x"], entry["y"]
        shards[f"{x >> 4}/{y >> 4}"].append([x, y, entry["bytes"], entry["sha256"]])
    index = {}
    for parent, rows in sorted(shards.items()):
        data = json.dumps(rows, separators=(",", ":")).encode()
        digest = hashlib.sha256(data).hexdigest()
        _atomic_write(output / "index" / f"{digest}.json", data, immutable=True)
        index[parent] = digest
    manifest = {"id": "dinghai-satellite", "manifestVersion": 1, "schemaVersion": 3,
                "release": release, "gridZoom": 8, "minimumZoom": 8, "maximumZoom": 13,
                "tileType": "webp", "tileSize": 512, "attribution": ATTRIBUTION,
                "sources": identity["sources"], "generatorIdentity": identity["generatorIdentity"],
                "cli": identity["cli"], "coastalSelection": identity["coastalSelection"],
                "index": index, "record": f"releases/{release}.json", "cellCount": len(entries),
                "totalBytes": sum(e["bytes"] for e in entries)}
    data = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    _atomic_write(output / manifest["record"], data, immutable=True)
    _atomic_write(output / "manifest.json", data)
    return manifest


def build(estimate_path, cli, verification, release, output, state_dir, cache, jobs=4):
    if not 1 <= jobs <= 4:
        raise ValueError("jobs must be between 1 and 4")
    plan = json.loads(Path(estimate_path).read_text())
    if plan["estimatedUpperBytes"] > LIMIT_BYTES or not plan["withinBudget"]:
        raise ValueError("estimate exceeds the 300 GB gate; extraction is not authorized")
    record = verify_cli(cli, verification)
    output, state = Path(output), Path(state_dir)
    output.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    generator = hashlib.sha256()
    for name in ("satellite_product.py", "seamap_product.py", "depth_product.py", "requirements.txt"):
        generator.update((Path(__file__).parent / name).read_bytes())
    identity = {"sources": {"satellite": plan["source"], "landPolygons": plan["landPolygons"]},
                "generatorIdentity": f"satellite_product.py:{VERSION}:sha256:{generator.hexdigest()}",
                "cli": {"version": "5.7.0", "distribution": record["asset"]["browser_download_url"],
                        "sha256": record["verifiedSHA256"], "executableSHA256": record["executableSHA256"]},
                "coastalSelection": {"rule": "z8 non-land 8-neighbour ocean flood; longitude wraps",
                                     "oceanSeed": plan["oceanSeed"], "checks": plan["selectionChecks"]},
                "estimateSHA256": depth_product.file_sha256(estimate_path)}
    identity_hash = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    identity_path = state / "identity.json"
    encoded = json.dumps(identity, sort_keys=True).encode()
    _atomic_write(identity_path, encoded, immutable=True)
    # Budget enough for the unfinished PMTiles plus four transient MBTiles copies.
    completed = sum(p.stat().st_size for p in (output / "objects").glob("*.pmtiles"))
    needed = max(0, plan["estimatedUpperBytes"] - completed) + jobs * max(
        c["estimatedUpperBytes"] for c in plan["cells"]) * 2
    if shutil.disk_usage(output).free < needed:
        raise ValueError(f"insufficient free disk: need {needed} additional bytes")
    remote = Remote(cache, plan["source"])
    started = time.monotonic()
    with pinned_source(remote) as source:
        def one(cell):
            x, y = cell["x"], cell["y"]
            receipt = state / "cells" / f"{x}-{y}.json"
            if receipt.exists():
                saved = json.loads(receipt.read_text())
                obj = output / "objects" / f"{saved['sha256']}.pmtiles"
                if (saved["identity"] == identity_hash and obj.exists()
                        and depth_product.file_sha256(obj) == saved["sha256"]):
                    return saved
            t = time.monotonic()
            with tempfile.TemporaryDirectory(dir=state, prefix=f"{x}-{y}-") as tmp:
                mbtiles = Path(tmp) / "cell.mbtiles"
                pmtiles = Path(tmp) / "cell.pmtiles"
                w, s, e, n = cell_bounds(x, y)
                bbox = ",".join(map(str, (w + 1e-8, s + 1e-8, e - 1e-8, n - 1e-8)))
                command = [str(cli), "convert", source, str(mbtiles), "--bbox=" + bbox,
                           "--min-zoom", "8", "--max-zoom", "13"]
                log = state / "logs" / f"{x}-{y}.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open("w") as f:
                    f.write(json.dumps(command) + "\n")
                    f.flush()
                    subprocess.run(command, stdout=f, stderr=subprocess.STDOUT, check=True,
                                   env={**os.environ, "RAYON_NUM_THREADS": "1",
                                        "VERSATILES_NET_MAX_RETRIES": "3"})
                counts = write_cell(mbtiles, pmtiles, x, y, plan["source"])
                if {str(z): v for z, v in counts.items()} != cell["levels"]:
                    raise ValueError(f"CLI tile counts/lengths disagree with source index: {x}/{y}")
                digest = depth_product.file_sha256(pmtiles)
                obj = output / "objects" / f"{digest}.pmtiles"
                obj.parent.mkdir(parents=True, exist_ok=True)
                _install_immutable(pmtiles, obj)
                saved = {"x": x, "y": y, "sha256": digest, "bytes": obj.stat().st_size,
                         "levels": counts, "seconds": round(time.monotonic() - t, 3), "identity": identity_hash}
                _atomic_write(receipt, json.dumps(saved, sort_keys=True).encode())
                print(json.dumps(saved), flush=True)
                return saved

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(one, plan["cells"]))
    remote.fetch(0, 66)  # Do not move the publication pointer if the source changed.
    manifest = finalize(output, release, results, identity)
    _atomic_write(state / "timing.json", json.dumps({"seconds": time.monotonic() - started}).encode())
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("verify-cli")
    p.add_argument("--cli", type=Path, required=True)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p = commands.add_parser("estimate")
    p.add_argument("--land-index", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--cache", type=Path, required=True)
    p = commands.add_parser("build")
    p.add_argument("--estimate", type=Path, required=True)
    p.add_argument("--cli", type=Path, required=True)
    p.add_argument("--cli-verification", type=Path, required=True)
    p.add_argument("--release", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if args.command == "verify-cli":
        result = verify_distribution(args.cli, args.archive, args.output)
        print(json.dumps({"verifiedSHA256": result["verifiedSHA256"]}))
    elif args.command == "estimate":
        selection = coastal_cells(args.land_index)
        result = estimate(selection, args.cache)
        _atomic_write(args.output, json.dumps(result, sort_keys=True, indent=2).encode())
        print(json.dumps({k: v for k, v in result.items() if k != "cells"}), flush=True)
    else:
        result = build(args.estimate, args.cli, args.cli_verification, args.release,
                       args.output, args.state, args.cache, args.jobs)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
