#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Structural audit of a combined dinghai-seamap release; reads local files only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import audit_land

OSM_LAYERS = {"seamark", "land", "water", "wetland", "sea_area"}
LAYERS = OSM_LAYERS | {"landuse", "roads", "peaks", "places", "depth"}


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
    reports = []
    expected = set()
    for entry in entries:
        path = root / f"releases/{manifest['release']}/cells/8/{entry['x']}/{entry['y']}.pmtiles"
        expected.add(path)
        data = path.read_bytes()
        if (len(data), hashlib.sha256(data).hexdigest()) != (entry["bytes"], entry["sha256"]):
            raise ValueError(f"{path}: bytes or sha256 differs")
        header, metadata, tiles = audit_land.read_pmtiles(data)
        if (header["min_zoom"], header["max_zoom"]) != (8, 14) or metadata["dinghai:schemaVersion"] != 2:
            raise ValueError(f"{path}: wrong header or metadata")
        if "Open Waters" in json.dumps(metadata):
            raise ValueError(f"{path}: third-party attribution leaked")
        counts = {}
        for tile_id, body in tiles.items():
            z, x, y = audit_land._zxy(tile_id)
            if not 8 <= z <= 14 or x >> (z - 8) != entry["x"] or y >> (z - 8) != entry["y"]:
                raise ValueError(f"{path}: tile outside cell or zoom range")
            for name, layer in audit_land.decode_tile(__import__("gzip").decompress(body)).items():
                if name not in LAYERS or (name not in OSM_LAYERS and z > 12):
                    raise ValueError(f"{path}: unexpected layer {name} at z{z}")
                counts[name] = counts.get(name, 0) + len(layer[1])
        reports.append({"x": entry["x"], "y": entry["y"], "bytes": len(data), "tiles": len(tiles), "features": counts})
    root_cells = root / f"releases/{manifest['release']}/cells/8"
    actual = set(root_cells.glob("*/*.pmtiles"))
    if actual != expected:
        raise ValueError(f"disk and cells.json differ: {sorted(map(str, actual ^ expected))[:5]}")
    return {"cellCount": len(entries), "totalBytes": sum(e["bytes"] for e in entries), "cells": reports}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.root), ensure_ascii=False, indent=1))
