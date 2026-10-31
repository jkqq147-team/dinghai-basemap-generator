#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Independent audit of a land-product release: manifest, cells.json, every cell pmtiles.

Structural checks use only the Python standard library (own pmtiles/MVT decoders);
nothing is imported from land_product.py.
"""
import argparse
import gzip
import hashlib
import json
import struct
from collections import Counter
from pathlib import Path

ALLOWED = {
    "landuse": {"class": {"wood", "park", "farmland", "urban", "industrial", "beach"}},
    "roads": {"class": {"highway", "major", "secondary"}},
    "peaks": {"ele": None},
    "places": {"kind": {"island", "town", "village", "locality"}, "name": None},
    "depth": {"min": {20, 30, 50, 100, 200}},
}
GEOMETRY = {"landuse": 3, "roads": 2, "peaks": 1, "places": 1, "depth": 3}
BANDS = (20, 30, 50, 100, 200)
OFFSHORE_M = 1500.0
MIN_FRAGMENT_M2 = 1_000_000
FRAGMENT_WINDOW_PX = 256  # z12 px beyond a cell that its fragment decisions may see
ODBL_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
LAND_ATTRIBUTION = "© OpenStreetMap contributors · Protomaps"
GEBCO_DOI = "10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa"


# ---------------------------------------------------------------- pmtiles (stdlib)

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


def _rotate(n, x, y, rx, ry):
    if ry == 0:
        if rx != 0:
            x, y = n - 1 - x, n - 1 - y
        x, y = y, x
    return x, y


def _zxy(tile_id):
    z = ((3 * tile_id + 1).bit_length() - 1) // 2
    pos = tile_id - ((1 << (2 * z)) - 1) // 3
    x = y = 0
    s = 1
    while s < 1 << z:
        rx = (pos // 2) & s
        ry = (pos ^ rx) & s
        x, y = _rotate(s, x, y, rx, ry)
        x, y, pos, s = x + rx, y + ry, pos >> 1, s << 1
    return z, x, y


def read_pmtiles(data):
    if data[:7] != b"PMTiles" or data[7] != 3:
        raise ValueError("not a pmtiles v3 archive")
    q = struct.unpack_from("<11Q", data, 8)
    h = dict(zip(("root_offset", "root_length", "metadata_offset", "metadata_length", "leaf_offset",
                  "leaf_length", "data_offset", "data_length", "addressed", "entries", "contents"), q))
    h["clustered"], h["internal"], h["tile_compression"], h["tile_type"], h["min_zoom"], h["max_zoom"] = data[96:102]
    h["bounds"] = struct.unpack_from("<4i", data, 102)
    if h["internal"] != 2 or h["tile_compression"] != 2 or h["tile_type"] != 1:
        raise ValueError("cell must be gzip MVT with gzip directories")
    metadata = json.loads(gzip.decompress(data[h["metadata_offset"]:h["metadata_offset"] + h["metadata_length"]]))
    tiles = {}

    def directory(offset, length):
        buf = gzip.decompress(data[offset:offset + length])
        n, i = _varint(buf, 0)
        ids, runs, lengths, offsets = [], [], [], []
        last = 0
        for _ in range(n):
            v, i = _varint(buf, i)
            last += v
            ids.append(last)
        for _ in range(n):
            v, i = _varint(buf, i)
            runs.append(v)
        for _ in range(n):
            v, i = _varint(buf, i)
            lengths.append(v)
        for k in range(n):
            v, i = _varint(buf, i)
            offsets.append(offsets[k - 1] + lengths[k - 1] if v == 0 and k > 0 else v - 1)
        if i != len(buf):
            raise ValueError("trailing directory bytes")
        for tid, run, length, off in zip(ids, runs, lengths, offsets):
            if run == 0:
                directory(h["leaf_offset"] + off, length)
            else:
                if off + length > h["data_length"]:
                    raise ValueError("tile beyond data section")
                blob = data[h["data_offset"] + off:h["data_offset"] + off + length]
                for k in range(run):
                    tiles[tid + k] = blob

    directory(h["root_offset"], h["root_length"])
    if h["data_offset"] + h["data_length"] != len(data):
        raise ValueError("file length disagrees with header")
    return h, metadata, tiles


# ---------------------------------------------------------------- MVT (stdlib)

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


def _packed(buf):
    out, i = [], 0
    while i < len(buf):
        v, i = _varint(buf, i)
        out.append(v)
    return out


def _value(buf):
    for f, _, v in _fields(buf):
        if f == 1:
            return v.decode()
        if f == 2:
            return struct.unpack("<f", v)[0]
        if f == 3:
            return struct.unpack("<d", v)[0]
        if f == 4:
            return v - (1 << 64) if v >= 1 << 63 else v
        if f == 5:
            return v
        if f == 6:
            return (v >> 1) ^ -(v & 1)
        if f == 7:
            return bool(v)
    raise ValueError("empty value")


def decode_tile(data):
    """{layer: (extent, [(type, props, parts)])}; parts are lists of (x, y) per command run."""
    layers = {}
    for f, w, layer in _fields(data):
        if f != 3:
            raise ValueError("unknown tile field")
        name, keys, values, features, extent, version = None, [], [], [], 4096, None
        for lf, _, v in _fields(layer):
            if lf == 1:
                name = v.decode()
            elif lf == 2:
                features.append(v)
            elif lf == 3:
                keys.append(v.decode())
            elif lf == 4:
                values.append(_value(v))
            elif lf == 5:
                extent = v
            elif lf == 15:
                version = v
        if name in layers:
            raise ValueError(f"duplicate layer {name}")
        if version != 2:
            raise ValueError("layer version must be 2")
        decoded = []
        for feature in features:
            tags, gtype, geometry = [], 0, []
            for ff, _, v in _fields(feature):
                if ff == 2:
                    tags = _packed(v)
                elif ff == 3:
                    gtype = v
                elif ff == 4:
                    geometry = _packed(v)
            if len(tags) % 2:
                raise ValueError("odd tag count")
            props = {keys[k]: values[v] for k, v in zip(tags[::2], tags[1::2])}
            decoded.append((gtype, props, _geometry(geometry, gtype)))
        layers[name] = (extent, decoded)
    return layers


def _geometry(commands, gtype):
    parts, cur, x, y, i = [], None, 0, 0, 0
    while i < len(commands):
        cmd, count = commands[i] & 7, commands[i] >> 3
        i += 1
        if cmd in (1, 2):
            for _ in range(count):
                x += (commands[i] >> 1) ^ -(commands[i] & 1)
                y += (commands[i + 1] >> 1) ^ -(commands[i + 1] & 1)
                i += 2
                if cmd == 1:
                    if gtype == 1:
                        parts.append([(x, y)])
                        continue
                    cur = [(x, y)]
                    parts.append(cur)
                else:
                    cur.append((x, y))
        elif cmd == 7:
            if gtype != 3:
                raise ValueError("ClosePath outside polygon")
            cur.append(cur[0])
        else:
            raise ValueError("bad geometry command")
    return parts


# ---------------------------------------------------------------- structure

def check_metadata(meta, manifest, x, y):
    expected = {
        "dinghai:landLicense": "ODbL-1.0", "dinghai:landLicenseURL": ODBL_URL,
        "dinghai:landAttribution": LAND_ATTRIBUTION, "dinghai:depthDOI": GEBCO_DOI,
        "dinghai:release": manifest["release"], "dinghai:cell": f"8/{x}/{y}",
        "dinghai:protomapsBuild": manifest["sources"]["protomaps"]["build"],
        "dinghai:protomapsSHA256": manifest["sources"]["protomaps"]["sha256"],
        "dinghai:gebcoGrid": manifest["sources"]["gebco"]["grid"],
        "dinghai:gebcoSHA256": manifest["sources"]["gebco"]["sha256"],
    }
    for key, value in expected.items():
        if meta.get(key) != value:
            raise ValueError(f"cell {x}/{y}: metadata {key} is {meta.get(key)!r}, expected {value!r}")
    if not meta.get("attribution", "").startswith(LAND_ATTRIBUTION):
        raise ValueError(f"cell {x}/{y}: attribution lacks OSM/Protomaps credit")
    if GEBCO_DOI not in meta.get("dinghai:depthAttribution", "") or "GEBCO" not in meta.get("attribution", ""):
        raise ValueError(f"cell {x}/{y}: GEBCO attribution/DOI missing")
    notice = meta.get("dinghai:depthNotice", "")
    for phrase in ("示意", "非测量", "不可用于导航", "not for navigation"):
        if phrase not in notice:
            raise ValueError(f"cell {x}/{y}: depth notice lacks {phrase!r}")
    identity = manifest["generatorIdentity"].split(":")
    if f"{meta.get('dinghai:generatorVersion')}" != identity[1] or meta.get("dinghai:generatorSHA256") != identity[3]:
        raise ValueError(f"cell {x}/{y}: generator identity disagrees with manifest")
    if sorted(l["id"] for l in meta.get("vector_layers", [])) != sorted(ALLOWED):
        raise ValueError(f"cell {x}/{y}: vector_layers metadata")


def check_cell(data, manifest, x, y):
    h, meta, tiles = read_pmtiles(data)
    if (h["min_zoom"], h["max_zoom"]) != (8, 12):
        raise ValueError(f"cell {x}/{y}: header zoom {h['min_zoom']}-{h['max_zoom']}")
    check_metadata(meta, manifest, x, y)
    counts, zooms = Counter(), Counter()
    for tid, blob in tiles.items():
        z, tx, ty = _zxy(tid)
        if not 8 <= z <= 12:
            raise ValueError(f"cell {x}/{y}: tile zoom {z}")
        if (tx >> (z - 8), ty >> (z - 8)) != (x, y):
            raise ValueError(f"cell {x}/{y}: tile {z}/{tx}/{ty} outside the cell")
        zooms[z] += 1
        for name, (extent, features) in decode_tile(gzip.decompress(blob)).items():
            if name not in ALLOWED:
                raise ValueError(f"cell {x}/{y}: layer {name!r} not allowed")
            for gtype, props, parts in features:
                if gtype != GEOMETRY[name] or set(props) != set(ALLOWED[name]):
                    raise ValueError(f"cell {x}/{y}: {name} feature schema {gtype} {sorted(props)}")
                for key, allowed in ALLOWED[name].items():
                    value = props[key]
                    if allowed is not None and value not in allowed:
                        raise ValueError(f"cell {x}/{y}: {name}.{key}={value!r}")
                if name == "peaks" and (not isinstance(props["ele"], int) or isinstance(props["ele"], bool)):
                    raise ValueError(f"cell {x}/{y}: peak without integer elevation")
                if name == "places" and not (isinstance(props["name"], str) and props["name"].strip()):
                    raise ValueError(f"cell {x}/{y}: place without name")
                counts[name] += 1
                if name == "depth":
                    if extent != 4096:
                        raise ValueError("depth extent must be 4096")
    return {"tiles": len(tiles), "zooms": dict(sorted(zooms.items())), "features": dict(counts)}


# ---------------------------------------------------------------- release

def audit(root, jobs=1):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    for key, value in (("id", "dinghai-land"), ("manifestVersion", 1), ("gridZoom", 8),
                       ("minimumZoom", 8), ("maximumZoom", 12)):
        if manifest.get(key) != value:
            raise ValueError(f"manifest {key}")
    if manifest["layers"] != list(ALLOWED):
        raise ValueError("manifest layers")
    release = manifest["release"]
    if manifest["cells"] != f"releases/{release}/cells.json" or \
            manifest["cellsURL"] != f"releases/{release}/cells/{{z}}/{{x}}/{{y}}.pmtiles":
        raise ValueError("manifest paths")
    if manifest["license"]["land"] != "ODbL-1.0" or manifest["license"]["landURL"] != ODBL_URL:
        raise ValueError("manifest license")
    if manifest["attribution"]["land"] != LAND_ATTRIBUTION or GEBCO_DOI not in manifest["attribution"]["depth"]:
        raise ValueError("manifest attribution")
    if manifest["depth"]["bandsMeters"] != list(BANDS) or manifest["depth"]["offshoreMeters"] != OFFSHORE_M \
            or manifest["depth"]["minimumAreaSquareMeters"] != MIN_FRAGMENT_M2 \
            or manifest["depth"].get("fragmentWindowPixels") != FRAGMENT_WINDOW_PX:
        raise ValueError("manifest depth parameters")
    cells_bytes = (root / manifest["cells"]).read_bytes()
    if len(cells_bytes) != manifest["cellsBytes"] or hashlib.sha256(cells_bytes).hexdigest() != manifest["cellsSHA256"]:
        raise ValueError("cells.json bytes/sha256 disagree with manifest")
    cells = json.loads(cells_bytes)
    if len(cells) != manifest["cellCount"] or sum(c["bytes"] for c in cells) != manifest["totalBytes"]:
        raise ValueError("manifest totals")
    if len({(c["x"], c["y"]) for c in cells}) != len(cells):
        raise ValueError("duplicate cell")
    cell_root = root / "releases" / release / "cells"
    listed = {(cell_root / "8" / str(c["x"]) / f"{c['y']}.pmtiles").resolve() for c in cells}
    present = {p.resolve() for p in cell_root.rglob("*") if p.is_file()} if cell_root.exists() else set()
    if present != listed:
        raise ValueError(f"cells on disk differ from cells.json: {sorted(map(str, present ^ listed))[:5]}")
    report = {"release": release, "cells": {}}
    jobs_list = [(str(cell_root), manifest, c) for c in cells]
    if jobs > 1:
        import multiprocessing
        with multiprocessing.get_context("spawn").Pool(jobs) as pool:
            results = pool.imap_unordered(_audit_cell, jobs_list, chunksize=4)
            for key, summary in results:
                report["cells"][key] = summary
    else:
        for job in jobs_list:
            key, summary = _audit_cell(job)
            report["cells"][key] = summary
    report["cells"] = dict(sorted(report["cells"].items()))
    return report


def _audit_cell(job):
    cell_root, manifest, c = job
    x, y = c["x"], c["y"]
    data = (Path(cell_root) / "8" / str(x) / f"{y}.pmtiles").read_bytes()
    if len(data) != c["bytes"] or hashlib.sha256(data).hexdigest() != c["sha256"]:
        raise ValueError(f"cell {x}/{y}: bytes/sha256 disagree with cells.json")
    summary = check_cell(data, manifest, x, y)
    summary["bytes"] = c["bytes"]
    return f"{x}/{y}", summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="resource root containing manifest.json")
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args()
    print(json.dumps(audit(args.root, args.jobs), indent=2, sort_keys=True,
                     ensure_ascii=False))
