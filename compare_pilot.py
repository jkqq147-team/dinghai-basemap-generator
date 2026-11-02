#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Aggregate-only z14 seamark counts for pilot cells, never a generation input.

The third-party PMTiles is read through bounded HTTP Range requests in memory.
No response body, tile, feature, geometry or attribute is written to disk or
printed. Only aggregate counts and transfer totals leave this process.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import ssl
import urllib.request
from collections import Counter
from pathlib import Path

import certifi
import mapbox_vector_tile as mvt
from pmtiles.tile import Compression, TileType, deserialize_directory, deserialize_header, find_tile, zxy_to_tileid

import land_product as lp
import seamap_product as sp

MAX_GAP = 64 * 1024
MAX_RUN = 4 * 1024 * 1024
TLS = ssl.create_default_context(cafile=certifi.where())
USER_AGENT = "DingHai-Seamap-Pilot/1.0 (aggregate-count-verification)"


class RemoteArchive:
    def __init__(self, url):
        self.url = url
        self.requests = self.bytes = 0
        self.header = deserialize_header(self.read(0, 127))
        if self.header["tile_type"] != TileType.MVT or self.header["internal_compression"] not in (Compression.NONE, Compression.GZIP):
            raise ValueError("unsupported remote PMTiles")
        self.root = self.directory(self.header["root_offset"], self.header["root_length"])

    def read(self, offset, length):
        request = urllib.request.Request(self.url, headers={"Range": f"bytes={offset}-{offset + length - 1}",
                                                             "User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60, context=TLS) as response:
            if response.status != 206:
                raise ValueError("server did not honour bounded Range request")
            data = response.read()
        if len(data) != length:
            raise ValueError("short Range response")
        self.requests += 1
        self.bytes += len(data)
        return data

    def directory(self, offset, length):
        return deserialize_directory(self.read(offset, length))

    def entries(self, cx, cy, zoom=14):
        wanted = {zxy_to_tileid(zoom, x, y) for x in range(cx * 64, (cx + 1) * 64)
                  for y in range(cy * 64, (cy + 1) * 64)}
        found = {}
        pending = [self.root]
        for _ in range(4):
            leaves = {}
            for directory in pending:
                for tile_id in wanted - found.keys():
                    entry = find_tile(directory, tile_id)
                    if entry is None:
                        continue
                    if entry.run_length:
                        found[tile_id] = entry
                    else:
                        leaves.setdefault((entry.offset, entry.length), []).append(tile_id)
            pending = []
            for (offset, length), ids in leaves.items():
                directory = self.directory(self.header["leaf_directory_offset"] + offset, length)
                pending.append(directory)
                for tile_id in ids:
                    entry = find_tile(directory, tile_id)
                    if entry and entry.run_length:
                        found[tile_id] = entry
        return found

    def payloads(self, entries):
        ranges = sorted({(entry.offset, entry.length) for entry in entries.values()})
        groups = []
        for offset, length in ranges:
            if length > MAX_RUN:
                raise ValueError("remote tile exceeds bounded run")
            if groups and offset + length - groups[-1][0] <= MAX_RUN and offset - groups[-1][1] <= MAX_GAP:
                groups[-1][1] = offset + length
            else:
                groups.append([offset, offset + length])
        bodies = {}
        for start, end in groups:
            block = self.read(self.header["tile_data_offset"] + start, end - start)
            for offset, length in ranges:
                if start <= offset and offset + length <= end:
                    bodies[(offset, length)] = block[offset - start:offset - start + length]
        for tile_id, entry in entries.items():
            body = bodies[(entry.offset, entry.length)]
            if self.header["tile_compression"] == Compression.GZIP:
                body = gzip.decompress(body)
            elif self.header["tile_compression"] != Compression.NONE:
                raise ValueError("unsupported tile compression")
            yield tile_id, body


def _group(layer, properties):
    kind = properties.get("type", "")
    if layer == "light" or kind in {"light_major", "light_minor"}:
        return "lights"
    if kind.startswith(("buoy_", "beacon_")) or kind in {"light_float", "light_vessel"}:
        return "aids"
    if kind in {"rock", "wreck", "obstruction"}:
        return "hazards"
    if kind in {"shoreline_construction", "pontoon"}:
        return "structures"
    return "other"


def _add_tile(raw, groups, types, seen):
    for layer_name, layer in mvt.decode(raw, default_options={"y_coord_down": True}).items():
        if layer_name not in {"seamark", "light"}:
            continue
        for feature in layer["features"]:
            if feature["geometry"]["type"] not in {"Point", "MultiPoint"}:
                continue
            properties = feature["properties"]
            kind = properties.get("type", "")
            identity = (layer_name, properties.get("osm_id", feature.get("id")), kind,
                        json.dumps(feature["geometry"], sort_keys=True) if properties.get("osm_id") is None else "")
            if identity in seen:
                continue
            seen.add(identity)
            groups[_group(layer_name, properties)] += 1
            types[kind or "(untyped)"] += 1


def own_counts(path, cx, cy):
    archive = lp.Archive(path)
    groups, types, seen = Counter(), Counter(), set()
    for x in range(cx * 64, (cx + 1) * 64):
        for y in range(cy * 64, (cy + 1) * 64):
            raw = archive.get(14, x, y)
            if raw:
                _add_tile(raw, groups, types, seen)
    return groups, types


def remote_counts(tilejson_url, cx, cy):
    request = urllib.request.Request(tilejson_url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60, context=TLS) as response:
        tilejson = json.load(response)
    match = re.search(r"Seamap ([0-9-]+)", tilejson["description"])
    if not match:
        raise ValueError("TileJSON description has no archive date")
    url = tilejson_url.rsplit("/", 1)[0] + f"/{match.group(1)}.pmtiles"
    archive = RemoteArchive(url)
    groups, types, seen = Counter(), Counter(), set()
    for _, raw in archive.payloads(archive.entries(cx, cy)):
        _add_tile(raw, groups, types, seen)
    return groups, types, {"requests": archive.requests, "rangeBytes": archive.bytes,
                           "thirdPartyOSMDate": re.search(r"OpenStreetMap data ([0-9-]+)", tilejson["description"]).group(1)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--cell", type=sp._cell, action="append", required=True)
    parser.add_argument("--tilejson-url", default="https://tiles.openwaters.io/seamap/tiles.json")
    args = parser.parse_args(argv)
    for x, y in args.cell:
        own_groups, own_types = own_counts(sp._cell_path(args.release_root, args.release, x, y), x, y)
        other_groups, other_types, transfer = remote_counts(args.tilejson_url, x, y)
        print(json.dumps({"cell": f"8/{x}/{y}", "own": {"groups": own_groups, "byType": own_types},
                          "thirdParty": {"groups": other_groups, "byType": other_types}, **transfer},
                         ensure_ascii=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
