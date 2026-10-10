#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Single development-side GSHHG native-binary -> schema 2 producer."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import struct
import tempfile
import time
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Sequence, Tuple

from shapely import affinity
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.validation import make_valid

VERSION = "python-product-2"
SOURCE_VERSION = "2.3.7"
OFFICIAL_URL = "https://www.soest.hawaii.edu/pwessel/gshhg/gshhg-bin-2.3.7.zip"
ARCHIVE_BYTES = 118617033
ARCHIVE_SHA256 = "28600e8f7a08645aab43079326df6504212ec5ccb2b4bcf3b5f4f12ed60e82bc"
SCHEMA_VERSION = 2
ENCODING = "dinghai-quantized-web-mercator-v1"
QUANTIZATION_MAXIMUM = 65535
WEB_MERCATOR_MAX_LATITUDE = 85.0511287798066
LODS = {"c": (0, 3, 2), "l": (4, 5, 3), "i": (6, 7, 4), "h": (8, 10, 5), "f": (11, 30, 6)}


def polygon_parts(geometry):
    if geometry is None or geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, (MultiPolygon, GeometryCollection)):
        for child in geometry.geoms:
            yield from polygon_parts(child)


def _canonical_polygon_parts(
    points: Sequence[Tuple[float, float]],
    unwrap: bool = True,
) -> List[Polygon]:
    unwrapped = _unwrap_longitudes(points) if unwrap else list(points)
    geometry = make_valid(Polygon(unwrapped))
    if geometry.is_empty:
        return []
    result: List[Polygon] = []
    for canonical in _world_parts(geometry):
        result.extend(polygon_parts(make_valid(canonical)))
    return result


def _world_parts(geometry: BaseGeometry) -> List[BaseGeometry]:
    """Cut unwrapped geometry into its -180..180 Web Mercator copies."""
    minimum_x, _, maximum_x, _ = geometry.bounds
    first_world = math.floor((minimum_x + 180.0) / 360.0)
    last_world = math.floor((maximum_x + 180.0) / 360.0)
    latitude_clip = box(
        -180.0,
        -WEB_MERCATOR_MAX_LATITUDE,
        180.0,
        WEB_MERCATOR_MAX_LATITUDE,
    )
    result: List[BaseGeometry] = []
    for world in range(first_world, last_world + 1):
        world_clip = box(
            -180.0 + 360.0 * world,
            -WEB_MERCATOR_MAX_LATITUDE,
            180.0 + 360.0 * world,
            WEB_MERCATOR_MAX_LATITUDE,
        )
        clipped = geometry.intersection(world_clip)
        if clipped.is_empty:
            continue
        canonical = affinity.translate(clipped, xoff=-360.0 * world)
        canonical = canonical.intersection(latitude_clip)
        if not canonical.is_empty:
            result.append(canonical)
    return result


def _unwrap_longitudes(
    points: Sequence[Tuple[float, float]],
) -> List[Tuple[float, float]]:
    result: List[Tuple[float, float]] = []
    previous: float | None = None
    for raw_longitude, latitude in points:
        longitude = (raw_longitude + 180.0) % 360.0 - 180.0
        if previous is not None:
            while longitude - previous > 180.0:
                longitude -= 360.0
            while longitude - previous < -180.0:
                longitude += 360.0
        result.append((longitude, latitude))
        previous = longitude
    return result


def _tile_range(
    geometry: BaseGeometry,
    zoom: int,
) -> Tuple[range, range]:
    minimum_longitude, minimum_latitude, maximum_longitude, maximum_latitude = (
        geometry.bounds
    )
    tile_count = 1 << zoom
    minimum_x = max(
        0,
        min(tile_count - 1, int(math.floor(
            _longitude_to_tile_x(minimum_longitude, tile_count)
        ))),
    )
    maximum_x = max(
        0,
        min(tile_count - 1, int(math.floor(
            _longitude_to_tile_x(maximum_longitude, tile_count)
        ))),
    )
    minimum_y = max(
        0,
        min(tile_count - 1, int(math.floor(
            _latitude_to_tile_y(maximum_latitude, tile_count)
        ))),
    )
    maximum_y = max(
        0,
        min(tile_count - 1, int(math.floor(
            _latitude_to_tile_y(minimum_latitude, tile_count)
        ))),
    )
    return range(minimum_x, maximum_x + 1), range(minimum_y, maximum_y + 1)


def _longitude_to_tile_x(longitude: float, tile_count: int) -> float:
    return (longitude + 180.0) / 360.0 * tile_count


def _latitude_to_tile_y(latitude: float, tile_count: int) -> float:
    latitude = max(
        -WEB_MERCATOR_MAX_LATITUDE,
        min(WEB_MERCATOR_MAX_LATITUDE, latitude),
    )
    radians = math.radians(latitude)
    return (
        1.0 - math.asinh(math.tan(radians)) / math.pi
    ) / 2.0 * tile_count


def _tile_bounds(x: int, y: int, zoom: int) -> Tuple[float, float, float, float]:
    tile_count = 1 << zoom
    west = x / tile_count * 360.0 - 180.0
    east = (x + 1) / tile_count * 360.0 - 180.0
    north = math.degrees(
        math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / tile_count)))
    )
    south = math.degrees(
        math.atan(
            math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / tile_count))
        )
    )
    return west, south, east, north


def _quantized_points(
    coordinates: Iterable[Tuple[float, float]],
    tile_x: int,
    tile_y: int,
    tile_zoom: int,
    closed: bool = False,
) -> List[Tuple[int, int]]:
    tile_count = 1 << tile_zoom
    result: List[Tuple[int, int]] = []
    for longitude, latitude in coordinates:
        local_x = _longitude_to_tile_x(longitude, tile_count) - tile_x
        local_y = _latitude_to_tile_y(latitude, tile_count) - tile_y
        point = (
            round(max(0.0, min(1.0, local_x)) * QUANTIZATION_MAXIMUM),
            round(max(0.0, min(1.0, local_y)) * QUANTIZATION_MAXIMUM),
        )
        if not result or result[-1] != point:
            result.append(point)
    if closed and len(result) > 1 and result[0] == result[-1]:
        result.pop()
    return result


def _encode_polygon(
    polygon: Polygon,
    tile_x: int,
    tile_y: int,
    tile_zoom: int,
) -> Tuple[bytes, int] | None:
    rings = [polygon.exterior, *polygon.interiors]
    encoded_rings: List[Tuple[List[Tuple[int, int]], bytes]] = []
    for index, ring in enumerate(rings):
        points = _quantized_points(
            ring.coords,
            tile_x,
            tile_y,
            tile_zoom,
            closed=True,
        )
        if len(points) < 3:
            if index == 0:
                return None
            continue
        encoded = bytearray(struct.pack("<I", len(points)))
        for x, y in points:
            encoded.extend(struct.pack("<HH", x, y))
        encoded_rings.append((points, bytes(encoded)))
    if not encoded_rings:
        return None
    data = bytearray(struct.pack("<I", len(encoded_rings)))
    for _, encoded in encoded_rings:
        data.extend(encoded)
    return bytes(data), sum(len(points) for points, _ in encoded_rings)


def _line_parts(geometry: BaseGeometry) -> Iterator[LineString]:
    if geometry.is_empty:
        return
    if geometry.geom_type in {"LineString", "LinearRing"}:
        yield LineString(geometry.coords)
        return
    if hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _line_parts(part)


def _encode_line(
    line: LineString,
    tile_x: int,
    tile_y: int,
    tile_zoom: int,
) -> Tuple[bytes, int] | None:
    points = _quantized_points(
        line.coords,
        tile_x,
        tile_y,
        tile_zoom,
    )
    if len(points) < 2:
        return None
    data = bytearray(struct.pack("<I", len(points)))
    for x, y in points:
        data.extend(struct.pack("<HH", x, y))
    return bytes(data), len(points)


@dataclass(frozen=True)
class Input:
    family: str
    resolution: str
    path: Path

    def __post_init__(self):
        if self.family != "coast" or self.resolution not in LODS:
            raise ValueError("only coast inputs at c/l/i/h/f resolutions are supported")


@dataclass(frozen=True)
class Record:
    level: int
    polar: bool
    points: list[tuple[float, float]]


def records(path: Path) -> Iterator[Record]:
    """44-byte BE header, then one bounded record; no global geometry array."""
    size = path.stat().st_size
    with path.open("rb") as source:
        while source.tell() < size:
            header = source.read(44)
            if len(header) != 44:
                raise ValueError("truncated binary header")
            _, n, flag, west, east, south, north, _, _, _, _ = struct.unpack(">11i", header)
            if (flag >> 8) & 255 != 15 or n < 2 or n > (size - source.tell()) // 8:
                raise ValueError("invalid binary version/point count")
            if not (-180000000 <= west <= east <= 360000000
                    and -90000000 <= south <= north <= 90000000):
                raise ValueError("invalid binary bounds")
            points = []
            remaining = n
            while remaining:
                count = min(remaining, 8192)
                data = source.read(count * 8)
                if len(data) != count * 8:
                    raise ValueError("truncated binary points")
                for x, y in struct.iter_unpack(">ii", data):
                    if not (-180000000 <= x <= 360000000 and -90000000 <= y <= 90000000):
                        raise ValueError("invalid binary coordinates")
                    points.append((x * 1e-6, y * 1e-6))
                remaining -= count
            yield Record(flag & 255, south == -90000000 and east - west == 360000000, points)


def identity(path: Path) -> dict:
    sha = hashlib.sha256()
    crc = 0
    size = 0
    with path.open("rb") as source:
        while data := source.read(1024 * 1024):
            sha.update(data)
            crc = zlib.crc32(data, crc)
            size += len(data)
    return {"bytes": size, "crc32": crc, "sha256": sha.hexdigest()}


def _geometry(record: Record):
    raw = record.points if record.polar else _unwrap_longitudes(record.points)
    # Only sea/land outlines survive, always from the original source path.
    # L2–4 inland hierarchy retains fills but produces no stroke fragments.
    strokes = _world_parts(LineString(raw)) if record.level in (1, 5) else []
    fill = raw
    if record.polar:
        if record.level != 5 or raw[0][0] != 180 or raw[-1][0] != -180:
            raise ValueError("invalid ice-front polar endpoints")
        fill = raw + [(-180, -90), (180, -90)]
    return _canonical_polygon_parts(fill, unwrap=False), strokes


def _fragments(geometry, resolution, level, kind):
    """Cut a source part by row before tiles; no repeated whole-continent clip."""
    zoom = LODS[resolution][2]
    _, rows = _tile_range(geometry, zoom)
    for row in rows:
        west, south, _, north = _tile_bounds(0, row, zoom)
        strip = geometry.intersection(box(west, south, 180, north))
        if strip.is_empty:
            continue
        columns, _ = _tile_range(strip, zoom)
        for column in columns:
            clipped = strip.intersection(box(*_tile_bounds(column, row, zoom)))
            if kind == 0:
                parts = polygon_parts(make_valid(clipped))
                encoder = _encode_polygon
            else:
                parts = _line_parts(clipped)
                encoder = _encode_line
            for part in parts:
                encoded = encoder(part, column, row, zoom)
                if encoded is not None:
                    yield (resolution, zoom, column, row, "coast", level, kind, *encoded)


def build(inputs: Sequence[Input], output: Path, source_version: str, archive: dict | None = None) -> dict:
    """Single producer for both full releases and explicitly selected fixture inputs."""
    if not inputs or not source_version:
        raise ValueError("source version and file inputs required")
    keys = [(i.resolution, i.family) for i in inputs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate family/resolution input")
    inputs = sorted(inputs, key=lambda i: list(LODS).index(i.resolution))
    members = [
        {"name": i.path.name, "family": i.family, "resolution": i.resolution, **identity(i.path)}
        for i in inputs
    ]
    generator_sha = identity(Path(__file__))["sha256"]
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    # Isolated staging; failure never replaces an already published output.
    with tempfile.TemporaryDirectory(prefix=".gshhg-", dir=output.parent) as staging:
        temporary = Path(staging) / "DingHaiCoastlines.sqlite"
        db = sqlite3.connect(temporary)
        try:
            db.executescript("""
                PRAGMA page_size=4096;
                PRAGMA cache_size=-4096;
                PRAGMA temp_store=FILE;
                PRAGMA user_version=2;
                CREATE TABLE metadata(name TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
                CREATE TABLE lods(resolution TEXT PRIMARY KEY, minimum_zoom INTEGER, maximum_zoom INTEGER, tile_zoom INTEGER) WITHOUT ROWID;
                CREATE TABLE features(resolution TEXT, tile_zoom INTEGER, tile_column INTEGER, tile_row INTEGER,
                    family TEXT, level INTEGER, kind INTEGER, geometry BLOB, feature_count INTEGER, point_count INTEGER,
                    PRIMARY KEY(resolution,tile_column,tile_row,family,level,kind)) WITHOUT ROWID;
                CREATE TABLE fragments(resolution TEXT, tile_zoom INTEGER, tile_column INTEGER, tile_row INTEGER,
                    family TEXT, level INTEGER, kind INTEGER, geometry BLOB, points INTEGER);
                BEGIN IMMEDIATE;
            """)
            for inp, member in zip(inputs, members):
                member["records"] = 0
                member["levels"] = {}
                member_started = time.monotonic()
                for record in records(inp.path):
                    member["records"] += 1
                    level_key = str(record.level)
                    member["levels"][level_key] = member["levels"].get(level_key, 0) + 1
                    level = record.level
                    if level not in range(1, 7):
                        raise ValueError("invalid coastline hierarchy level")
                    if level == 6:
                        continue
                    if level == 5:
                        level = 1
                    fills, strokes = _geometry(record)
                    for kind, parts in ((0, fills), (1, strokes)):
                        for part in parts:
                            db.executemany("INSERT INTO fragments VALUES (?,?,?,?,?,?,?,?,?)",
                                           _fragments(part, inp.resolution, level, kind))
                print(f"Converted {inp.family}/{inp.resolution}: {member['records']} records "
                      f"in {time.monotonic() - member_started:.3f}s", flush=True)

            metadata = {
                "name": "DingHai Global Coastlines",
                "schema_version": "2",
                "source": "GSHHG",
                "source_version": source_version,
                "source_url": OFFICIAL_URL if archive else "explicit-file-inputs",
                "source_archive_sha256": archive["sha256"] if archive else "",
                "source_members": json.dumps(members, sort_keys=True, separators=(",", ":")),
                "source_scope": "official-coast-5" if archive else "explicit-file-inputs",
                "product_scope": "marine-background",
                "feature_scope": "family='coast' AND (kind=0 OR level=1)",
                "generator_identity": f"{VERSION}:sha256:{generator_sha}",
                "geometry_encoding": ENCODING,
                "quantization_maximum": "65535",
                "antarctica_boundary": "ice-front",
                "license": "LGPL-3.0-only",
            }
            metadata["source_identity"] = hashlib.sha256(
                json.dumps({"archive": archive, "members": members}, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            db.executemany("INSERT INTO metadata VALUES (?,?)", sorted(metadata.items()))
            db.executemany("INSERT INTO lods VALUES (?,?,?,?)",
                           [(resolution, *lod) for resolution, lod in LODS.items()])
            db.execute("CREATE INDEX fragment_order ON fragments(resolution,tile_column,tile_row,family,level,kind)")
            scan = db.execute(
                "SELECT * FROM fragments ORDER BY resolution,tile_column,tile_row,family,level,kind,rowid")
            key = None
            data = bytearray()
            count = points = 0

            def flush():
                if count:
                    db.execute("INSERT INTO features VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (*key, struct.pack("<I", count) + data, count, points))

            for row in scan:
                next_key, blob, n = row[:7], row[7], row[8]
                if next_key != key:
                    flush()
                    key, data, count, points = next_key, bytearray(), 0, 0
                data.extend(blob)
                count += 1
                points += n
            flush()
            if key is None:
                raise ValueError("empty dataset")
            db.execute("DROP TABLE fragments")
            db.commit()
            db.execute("VACUUM")
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("SQLite quick_check failed")
            counts = db.execute("SELECT count(*),sum(feature_count),sum(point_count) FROM features").fetchone()
        finally:
            db.close()
        os.replace(temporary, output)
    return {"database": identity(output), "members": members, "rows": counts[0],
            "features": counts[1], "points": counts[2], "seconds": time.monotonic() - started,
            "generator_identity": metadata["generator_identity"]}


def build_official(archive_path: Path, output: Path) -> dict:
    actual = identity(archive_path)
    if actual["bytes"] != ARCHIVE_BYTES or actual["sha256"] != ARCHIVE_SHA256:
        raise ValueError("official archive bytes/SHA256 mismatch")
    with tempfile.TemporaryDirectory(prefix="gshhg-input-") as staging:
        inputs = []
        with zipfile.ZipFile(archive_path) as archive:
            for resolution in LODS:
                name = f"gshhs_{resolution}.b"
                matches = [i for i in archive.infolist() if i.filename == name]
                if len(matches) != 1:
                    raise ValueError(f"missing or duplicate official member: {name}")
                info = matches[0]
                path = Path(staging) / name
                with archive.open(info) as source, path.open("wb") as target:
                    while data := source.read(1024 * 1024):
                        target.write(data)
                extracted = identity(path)
                if extracted["bytes"] != info.file_size or extracted["crc32"] != info.CRC:
                    raise ValueError(f"member bytes/CRC mismatch: {name}")
                inputs.append(Input("coast", resolution, path))
        return build(inputs, output, SOURCE_VERSION, archive=actual)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--official-archive", type=Path)
    source.add_argument("--input", action="append", metavar="FAMILY:LOD:FILE")
    parser.add_argument("--source-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.official_archive:
        if args.source_version != SOURCE_VERSION:
            parser.error("official archive is fixed to source version 2.3.7")
        result = build_official(args.official_archive, args.output)
    else:
        inputs = []
        for argument in args.input:
            family, resolution, path = argument.split(":", 2)
            inputs.append(Input(family, resolution, Path(path)))
        result = build(inputs, args.output, args.source_version)
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()


