#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Seamap z8-cell land + illustrative offshore depth producer (land-product-1).

One pmtiles per Seamap z8 cell, z8-12, layers landuse/roads/peaks/places/depth,
cut from a local z0-12 extract of one Protomaps build, a z13 peaks file of the
same build and the GEBCO_2026 15" netCDF grid. Development-side only: it reads local files, never the network.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import multiprocessing
import os
import resource
import struct
import sys
import time
from collections import OrderedDict, defaultdict
from pathlib import Path

import contourpy
import mapbox_vector_tile as mvt
import netCDF4
import numpy as np
import shapely
from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid
from PIL import Image, ImageDraw
from pmtiles.writer import optimize_directories
from pmtiles.tile import (
    Compression, Entry, TileType, deserialize_directory, deserialize_header,
    find_tile, serialize_directory, serialize_header, tileid_to_zxy, zxy_to_tileid,
)
from scipy import ndimage
from shapely import affinity
from shapely.geometry import MultiPolygon, Point, Polygon, shape
from shapely.ops import linemerge, unary_union
from shapely.validation import make_valid

VERSION = "land-product-1"
SCHEMA_VERSION = 1
GRID_ZOOM = 8
MIN_ZOOM, MAX_ZOOM = 8, 12
PEAK_ZOOM_RANGE = range(13, 15)   # zooms the peaks command may read; the release uses 13
DEFAULT_PEAK_ZOOMS = (13,)
EXTENT = 4096
LAYERS = ("landuse", "roads", "peaks", "places", "depth")

# Depth: cumulative bands, metres below GEBCO's mean-sea-level reference.
DEPTH_BANDS_M = (20, 30, 50, 100, 200)
OFFSHORE_M = 1500.0          # minimum ground distance to OSM land
MIN_FRAGMENT_M2 = 1_000_000  # pieces smaller than this are dropped (holes kept)
DEPTH_MARGIN_M = 2.0         # generator-side safety margins, see README
DISTANCE_MARGIN_PX = 3.0
DISTANCE_CAP_M = 200.0
MISSING = -1.0e6
EDT_STRIP = 512             # rows per distance-transform strip (memory bound)

# Full-resolution node lattice: z12 world pixels, 256 per z12 tile, 4096 per z8 cell.
FULL_ZOOM = 12
CELL_PX = 256 << (FULL_ZOOM - GRID_ZOOM)
WORLD_PX = 256 << FULL_ZOOM
EARTH_CIRCUMFERENCE_M = 2 * math.pi * 6378137.0
POOL_PAD = (1 << (FULL_ZOOM - MIN_ZOOM)) + 2
# Fragments are judged on a window reaching this far beyond the cell (z12 px, one z12
# tile, ~8.6 km at 28 N); the same window rule holds in every cell, see depth_tiles.
FRAGMENT_WINDOW_PX = 256
WINDOW_PAD = FRAGMENT_WINDOW_PX + POOL_PAD

LANDUSE = {
    "wood": "wood", "forest": "wood", "scrub": "wood",
    "park": "park", "garden": "park", "golf_course": "park", "grass": "park", "meadow": "park",
    "village_green": "park", "recreation_ground": "park", "grassland": "park",
    "farmland": "farmland", "orchard": "farmland", "allotments": "farmland",
    "residential": "urban", "commercial": "urban", "retail": "urban",
    "industrial": "industrial",
    "beach": "beach", "sand": "beach",
}
ROAD_DETAIL = {"trunk": "major", "trunk_link": "major", "primary": "major", "primary_link": "major",
               "secondary": "secondary", "secondary_link": "secondary",
               "tertiary": "secondary", "tertiary_link": "secondary"}
PLACE_DETAILS = {"town", "village", "locality"}
NAME_KEYS = ("name:zh-Hans", "name:zh", "name")

LAND_ATTRIBUTION = "© OpenStreetMap contributors · Protomaps"
LAND_LICENSE = "ODbL-1.0"
LAND_LICENSE_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
OSM_COPYRIGHT_URL = "https://www.openstreetmap.org/copyright"
GEBCO_DOI = "10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa"
GEBCO_ATTRIBUTION = ("GEBCO Bathymetric Compilation Group 2026 (2026). The GEBCO_2026 Grid - a continuous "
                     "terrain model for oceans and land at 15 arc-second intervals. doi:" + GEBCO_DOI)
GEBCO_TERMS_URL = "https://www.gebco.net/data-products/gridded-bathymetry/terms-of-use"
DEPTH_NOTICE = ("示意水深：由 GEBCO_2026 网格插值分层得到，非测量，不可用于导航。"
                "Illustrative depth bands interpolated from the GEBCO_2026 Grid; not a survey; "
                "not for navigation.")
SOURCE_REPOSITORY = "https://github.com/jkqq147-team/dinghai-basemap-generator"


def generator_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def params() -> dict:
    return {"bandsMeters": list(DEPTH_BANDS_M), "offshoreMeters": OFFSHORE_M,
            "minimumAreaSquareMeters": MIN_FRAGMENT_M2, "fragmentWindowPixels": FRAGMENT_WINDOW_PX,
            "depthMarginMeters": DEPTH_MARGIN_M,
            "distanceMarginPixels": DISTANCE_MARGIN_PX, "distanceCapMeters": DISTANCE_CAP_M}


# ---------------------------------------------------------------- geometry helpers

def lat_of_px(gy):
    """Latitude (deg) of z12 world-pixel row coordinate(s) (pixel corners)."""
    return np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * np.asarray(gy, dtype=np.float64) / WORLD_PX))))


def lon_of_px(gx):
    return np.asarray(gx, dtype=np.float64) / WORLD_PX * 360.0 - 180.0


def mpp_of_px(gy):
    """Ground metres per z12 world pixel at row coordinate(s) gy."""
    return EARTH_CIRCUMFERENCE_M / WORLD_PX * np.cos(np.radians(lat_of_px(gy)))


def poleward_shift_px(gy):
    """Rows within which every land pixel that can affect a capped distance lies."""
    return np.ceil(1.25 * (OFFSHORE_M + DISTANCE_CAP_M) / mpp_of_px(gy)) + 4


def conservative_mpp(gy):
    q = poleward_shift_px(gy)
    return np.minimum(mpp_of_px(gy - q), mpp_of_px(gy + q))


def cell_bounds(x, y):
    """(west, south, east, north) degrees of z8 cell x/y."""
    n = 1 << GRID_ZOOM
    west, east = x / n * 360 - 180, (x + 1) / n * 360 - 180
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


# ---------------------------------------------------------------- pmtiles

class Archive:
    """Read-only pmtiles v3 random access with os.pread and a bounded leaf cache."""

    def __init__(self, path, leaf_cache=16):
        self.path = Path(path)
        self.fd = os.open(self.path, os.O_RDONLY)
        self.header = deserialize_header(self._read(0, 127))
        if self.header["tile_type"] != TileType.MVT:
            raise ValueError(f"{path}: not an MVT archive")
        self.root = self._directory(self.header["root_offset"], self.header["root_length"])
        self.leaves = OrderedDict()
        self.leaf_cache = leaf_cache

    def _read(self, offset, length):
        data = os.pread(self.fd, length, offset)
        if len(data) != length:
            raise ValueError(f"{self.path}: truncated read")
        return data

    def _directory(self, offset, length):
        if self.header["internal_compression"] != Compression.GZIP:
            raise ValueError("unsupported directory compression")
        return deserialize_directory(self._read(offset, length))  # gunzips itself

    def metadata(self):
        data = self._read(self.header["metadata_offset"], self.header["metadata_length"])
        if self.header["internal_compression"] == Compression.GZIP:
            data = gzip.decompress(data)
        return json.loads(data)

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
            key = entry.offset
            if key in self.leaves:
                self.leaves.move_to_end(key)
            else:
                self.leaves[key] = self._directory(self.header["leaf_directory_offset"] + entry.offset,
                                                   entry.length)
                if len(self.leaves) > self.leaf_cache:
                    self.leaves.popitem(last=False)
            directory = self.leaves[key]
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


def write_cell_pmtiles(path, tiles, cx, cy, metadata):
    write_pmtiles(path, tiles, metadata, cell_bounds(cx, cy), MIN_ZOOM, MAX_ZOOM)


# ---------------------------------------------------------------- MVT

def _varint(buf, i):
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7


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


def _layer_name(layer):
    for field, _, value in _fields(layer):
        if field == 1:
            return bytes(value).decode()
    return ""


def split_layers(tile):
    """{layer name: raw Layer message bytes} without decoding geometry."""
    return {_layer_name(v): v for f, w, v in _fields(memoryview(tile)) if f == 3 and w == 2}


def decode_layers(raw_layers, names):
    """mapbox_vector_tile decode of only the named layers (y down)."""
    wanted = [raw_layers[n] for n in names if n in raw_layers]
    if not wanted:
        return {}
    pb = b"".join(b"\x1a" + _encode_varint(len(l)) + bytes(l) for l in wanted)
    return mvt.decode(pb, default_options={"y_coord_down": True})


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


def _value(buf):
    for field, wire, value in _fields(buf):
        if field == 1:
            return bytes(value).decode()
        if field == 2:
            return struct.unpack("<f", value)[0]
        if field == 3:
            return struct.unpack("<d", value)[0]
        if field in (4, 5):
            return value - (1 << 64) if field == 4 and value >= 1 << 63 else value
        if field == 6:
            return (value >> 1) ^ -(value & 1)
        if field == 7:
            return bool(value)
    return None


def peaks_in_layer(raw):
    """[(x, y, elevation)] in layer units for pois kind=peak points with numeric elevation."""
    keys, values, features, extent = [], [], [], 4096
    for field, wire, value in _fields(raw):
        if field == 3:
            keys.append(bytes(value).decode())
        elif field == 4:
            values.append(_value(value))
        elif field == 2:
            features.append(value)
        elif field == 5:
            extent = value
    try:
        kind_key, ele_key = keys.index("kind"), keys.index("elevation")
    except ValueError:
        return [], extent
    peak = [i for i, v in enumerate(values) if v == "peak"]
    if not peak:
        return [], extent
    out = []
    for feature in features:
        tags, geometry, gtype = [], [], 0
        for field, wire, value in _fields(feature):
            if field == 2:
                tags = _packed(value)
            elif field == 3:
                gtype = value
            elif field == 4:
                geometry = _packed(value)
        if gtype != 1 or len(geometry) != 3 or geometry[0] != 9:
            continue
        props = dict(zip(tags[::2], tags[1::2]))
        if props.get(kind_key) not in peak or ele_key not in props:
            continue
        ele = values[props[ele_key]]
        if isinstance(ele, bool) or not isinstance(ele, (int, float)) or not math.isfinite(ele):
            continue
        x = (geometry[1] >> 1) ^ -(geometry[1] & 1)
        y = (geometry[2] >> 1) ^ -(geometry[2] & 1)
        out.append((x, y, ele))
    return out, extent


def _packed(buf):
    out, i, n = [], 0, len(buf)
    while i < n:
        v, i = _varint(buf, i)
        out.append(v)
    return out


def encode_layers(layers, extent):
    if not layers:
        return b""
    return mvt.encode(layers, default_options={
        "y_coord_down": True, "extents": extent, "on_invalid_geometry": on_invalid_geometry_make_valid})


# ---------------------------------------------------------------- land

def zh_name(props):
    for key in NAME_KEYS:
        value = props.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _polygonal(g):
    if g.is_empty:
        return []
    if g.geom_type == "Polygon":
        return [g]
    if g.geom_type in ("MultiPolygon", "GeometryCollection"):
        return [p for part in g.geoms for p in _polygonal(part)]
    return []


def _lines(g):
    if g.is_empty:
        return []
    if g.geom_type == "LineString":
        return [g]
    if g.geom_type in ("MultiLineString", "GeometryCollection"):
        return [p for part in g.geoms for p in _lines(part)]
    return []


def _inside(x, y, extent=EXTENT):
    return 0 <= x < extent and 0 <= y < extent


def slim_land(z, layers, peaks):
    """Land layers of one source tile, in source units (4096). Returns [{name, features}]."""
    tol = EXTENT / 512 * (0.25 if z == MAX_ZOOM else 0.5)
    floor = (2 * EXTENT / 512) ** 2
    out = []
    earth = [shape(f["geometry"]).buffer(0) for f in layers.get("earth", {}).get("features", [])
             if f["geometry"]["type"] in ("Polygon", "MultiPolygon")]
    land = unary_union(earth) if earth else None
    by_class = defaultdict(list)
    for f in layers.get("landuse", {}).get("features", []):
        c = LANDUSE.get(f["properties"].get("kind"))
        if c and f["geometry"]["type"] in ("Polygon", "MultiPolygon"):
            by_class[c].append(shape(f["geometry"]).buffer(0))
    features = []
    if land is not None and not land.is_empty:
        for c, geoms in sorted(by_class.items()):
            g = unary_union(_polygonal(unary_union(geoms).intersection(land)))
            g = g.simplify(tol, preserve_topology=True)
            parts = [q for p in _polygonal(g) for q in _polygonal(p if p.is_valid else make_valid(p))
                     if q.area >= floor]
            if parts:
                features.append({"geometry": unary_union(parts), "properties": {"class": c}})
    if features:
        out.append({"name": "landuse", "features": features})
    by_class = defaultdict(list)
    for f in layers.get("roads", {}).get("features", []):
        p = f["properties"]
        c = None
        if p.get("kind") == "highway":
            c = "highway"
        elif p.get("kind") == "major_road":
            c = ROAD_DETAIL.get(p.get("kind_detail"))
            if c == "secondary" and z < 11:
                c = None
        if c and f["geometry"]["type"] in ("LineString", "MultiLineString"):
            by_class[c].append(shape(f["geometry"]))
    features = []
    for c, geoms in sorted(by_class.items()):
        g = linemerge(_lines(unary_union(geoms))) if len(geoms) > 1 else geoms[0]
        g = g.simplify(tol)
        if not g.is_empty:
            features.append({"geometry": g, "properties": {"class": c}})
    if features:
        out.append({"name": "roads", "features": features})
    if peaks:
        out.append({"name": "peaks", "features": [
            {"geometry": Point(x, y), "properties": {"ele": int(round(ele))}} for x, y, ele in peaks]})
    places = []
    for f in layers.get("earth", {}).get("features", []):
        p, g = f["properties"], f["geometry"]
        if p.get("kind") == "island" and g["type"] == "Point" and _inside(*g["coordinates"]):
            name = zh_name(p)
            if name:
                places.append({"geometry": Point(g["coordinates"]), "properties": {"kind": "island", "name": name}})
    for f in layers.get("places", {}).get("features", []):
        p, g = f["properties"], f["geometry"]
        detail = p.get("kind_detail")
        if (p.get("kind") == "locality" and detail in PLACE_DETAILS and g["type"] == "Point"
                and _inside(*g["coordinates"]) and (p.get("min_zoom") or 0) <= z + 1):
            name = zh_name(p)
            if name:
                places.append({"geometry": Point(g["coordinates"]), "properties": {"kind": detail, "name": name}})
    if places:
        out.append({"name": "places", "features": places})
    return out


def land_extent(z):
    return EXTENT if z == MAX_ZOOM else 1024


def peak_zooms(peaks):
    """Source zooms recorded in a peaks archive written by make_peaks."""
    zooms = peaks.metadata().get("dinghai:peakZooms")
    if not zooms or any(z not in PEAK_ZOOM_RANGE for z in zooms):
        raise ValueError(f"{peaks.path}: no valid dinghai:peakZooms")
    return tuple(sorted(zooms))


def cell_peaks(archive, peaks, cx, cy, zooms=None):
    """Peaks of z12 tiles in the cell: the build's z12 pois.kind=peak plus those of the peaks
    archive's zooms (see `peaks`), deduplicated, highest zoom first."""
    kept = {}  # (z12 tile x, y) -> list of (x, y, ele) in z12 4096 units
    seen = set()
    for z in sorted(zooms or peak_zooms(peaks), reverse=True) + [FULL_ZOOM]:
        source = archive if z == FULL_ZOOM else peaks
        n = 1 << (z - GRID_ZOOM)
        for x in range(cx * n, (cx + 1) * n):
            for y in range(cy * n, (cy + 1) * n):
                tile = source.get(z, x, y)
                if tile is None:
                    continue
                raw = split_layers(tile).get("pois")
                if raw is None:
                    continue
                points, extent = peaks_in_layer(raw)
                k = 1 << (z - FULL_ZOOM)
                for px, py, ele in points:
                    if not _inside(px, py, extent):
                        continue
                    gx = (x + px / extent) / k * EXTENT  # z12 world in 4096 units per tile
                    gy = (y + py / extent) / k * EXTENT
                    key = (round(gx / 4), round(gy / 4), int(round(ele)))
                    if any((key[0] + i, key[1] + j, key[2]) in seen for i in (-1, 0, 1) for j in (-1, 0, 1)):
                        continue
                    seen.add(key)
                    tx, ty = int(gx // EXTENT), int(gy // EXTENT)
                    kept.setdefault((tx, ty), []).append((round(gx - tx * EXTENT), round(gy - ty * EXTENT), ele))
    for points in kept.values():
        points[:] = [(min(px, EXTENT - 1), min(py, EXTENT - 1), e) for px, py, e in points]
    return kept


def own_peaks(raw_layers):
    raw = raw_layers.get("pois")
    if raw is None:
        return []
    points, extent = peaks_in_layer(raw)
    return [(px * EXTENT / extent, py * EXTENT / extent, e) for px, py, e in points if _inside(px, py, extent)]


# ---------------------------------------------------------------- GEBCO

class Gebco:
    """GEBCO 15" netCDF (lat/lon/elevation), global or an aligned subset."""

    def __init__(self, path):
        self.path = Path(path)
        self.ds = netCDF4.Dataset(self.path)
        self.ds.set_auto_mask(False)
        lat = np.asarray(self.ds.variables["lat"][:], dtype=np.float64)
        lon = np.asarray(self.ds.variables["lon"][:], dtype=np.float64)
        for name, axis, origin in (("lat", lat, -90.0), ("lon", lon, -180.0)):
            if len(axis) < 2 or not np.allclose(np.diff(axis), 1 / 240, rtol=0, atol=1e-9):
                raise ValueError(f"{path}: {name} is not a 15 arc-second axis")
            offset = (axis[0] - origin) * 240 - 0.5
            if abs(offset - round(offset)) > 1e-6:
                raise ValueError(f"{path}: {name} not on the GEBCO 15\" cell-centre grid")
        self.lat0, self.lon0 = lat[0], lon[0]
        self.nlat, self.nlon = len(lat), len(lon)
        self.global_lon = self.nlon == 360 * 240
        self.elevation = self.ds.variables["elevation"]
        self.title = getattr(self.ds, "title", None)

    def depth(self, lats, lons):
        """Bilinear depth (m, positive below GEBCO sea level), float32 [len(lats), len(lons)].
        NaN where the grid has no coverage."""
        fi = (np.asarray(lats) - self.lat0) * 240
        fj = (np.asarray(lons) - self.lon0) * 240
        i = np.floor(fi).astype(np.int64)
        j = np.floor(fj).astype(np.int64)
        wi = (fi - i).astype(np.float32)
        wj = (fj - j).astype(np.float32)
        i0, i1 = int(i.min()), int(i.max()) + 1
        j0, j1 = int(j.min()), int(j.max()) + 1
        block = np.full((i1 - i0 + 1, j1 - j0 + 1), np.nan, dtype=np.float32)
        r0, r1 = max(i0, 0), min(i1, self.nlat - 1)
        if r0 <= r1:
            cols = np.arange(j0, j1 + 1)
            if self.global_lon:
                cols = cols % self.nlon
                runs = np.split(np.arange(len(cols)), np.flatnonzero(np.diff(cols) != 1) + 1)
            else:
                valid = (cols >= 0) & (cols < self.nlon)
                runs = [np.flatnonzero(valid)] if valid.any() else []
            for run in runs:
                if len(run):
                    a, b = int(cols[run[0]]), int(cols[run[-1]])
                    block[r0 - i0:r1 - i0 + 1, run[0]:run[-1] + 1] = np.asarray(
                        self.elevation[r0:r1 + 1, a:b + 1], dtype=np.float32)
        block = -block
        c = block[:, j - j0] * (1 - wj) + block[:, j + 1 - j0] * wj
        return c[i - i0] * (1 - wi)[:, None] + c[i + 1 - i0] * wi[:, None]

    def minimum_depth(self, lats, lons):
        """Exact bilinear minimum in each rectangle between consecutive axis values.

        Split at original GEBCO node lines: each bilinear subrectangle has its
        minimum at a corner. Longitude knots remain unwrapped; depth() reads their
        periodic columns, including the last/first-column interval at ±180°.
        """
        axes, indices = [], []
        for edges, origin in ((-np.asarray(lats), -self.lat0), (np.asarray(lons), self.lon0)):
            lo, hi = edges[0], edges[-1]
            knots = origin + np.arange(math.ceil((lo - origin) * 240),
                                       math.floor((hi - origin) * 240) + 1) / 240
            axis = np.unique(np.concatenate([edges, knots]))
            axes.append(axis)
            indices.append(np.searchsorted(axis, edges))
        values = self.depth(-axes[0], axes[1])
        for axis, ix in enumerate(indices):
            # reduceat excludes the next interval's first corner; include that shared
            # edge explicitly so the rectangles are closed on both sides.
            reduced = np.minimum.reduceat(values, ix, axis=axis)
            inner = [slice(None), slice(None)]
            inner[axis] = slice(None, -1)
            values = np.minimum(reduced[tuple(inner)], np.take(values, ix[1:], axis=axis))
        return values


# ---------------------------------------------------------------- depth

def land_raster_rings(cy):
    """z12 tile rings around the cell so every capped distance in its window is exact."""
    rows = cy * CELL_PX + np.array([-WINDOW_PAD, CELL_PX + WINDOW_PAD])
    need = WINDOW_PAD + float(poleward_shift_px(rows).max()) + 2
    return max(1, math.ceil(need / 256))


def earth_rings(raw_layers):
    layer = decode_layers(raw_layers, ["earth"]).get("earth")
    if not layer:
        return [], layer["extent"] if layer else EXTENT
    rings = []
    for f in layer["features"]:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"] if g["type"] == "MultiPolygon" else []
        rings.extend(polys)
    return rings, layer["extent"]


def land_raster(archive, cx, cy, rings_count, cell_earth):
    """Boolean land raster (row 0 north). Pixel (r, c) is the z12 world node (oy + r, ox + c)."""
    n = 1 << (FULL_ZOOM - GRID_ZOOM)
    tx0, ty0 = cx * n - rings_count, cy * n - rings_count
    side = (n + 2 * rings_count) * 256
    img = Image.new("1", (side, side), 0)
    draw = ImageDraw.Draw(img)
    exteriors, holes = [], []
    tiles_y = 1 << FULL_ZOOM
    for ty in range(ty0, ty0 + n + 2 * rings_count):
        if not 0 <= ty < tiles_y:
            continue
        for tx in range(tx0, tx0 + n + 2 * rings_count):
            if (tx, ty) in cell_earth:
                polys, extent = cell_earth[(tx, ty)]
            else:
                tile = archive.get(FULL_ZOOM, tx % tiles_y, ty)
                if tile is None:
                    continue
                polys, extent = earth_rings(split_layers(tile))
            ox, oy = (tx - tx0) * 256 + 0.5, (ty - ty0) * 256 + 0.5
            k = 256 / extent
            for poly in polys:
                for idx, ring in enumerate(poly):
                    pts = [(ox + a * k, oy + b * k) for a, b in ring]
                    if len(pts) >= 2:
                        (exteriors if idx == 0 else holes).append(pts)
    for pts in exteriors:
        if len(pts) >= 3:
            draw.polygon(pts, fill=1)
    for pts in holes:
        if len(pts) >= 3:
            draw.polygon(pts, fill=0)
    for pts in exteriors + holes:
        draw.line(pts + [pts[0]], fill=1, width=1)
    return np.asarray(img, dtype=bool), tx0 * 256, ty0 * 256


def depth_fields(archive, gebco, cx, cy, cell_earth):
    """Exact depth minima on full-resolution pixel rectangles, and distance on nodes.
    Both start at (cx*4096 - WINDOW_PAD, cy*4096 - WINDOW_PAD); the rectangle
    field has one fewer row/column than the node field."""
    rings_count = land_raster_rings(cy)
    land, ox, oy = land_raster(archive, cx, cy, rings_count, cell_earth)
    gx0, gy0 = cx * CELL_PX - WINDOW_PAD, cy * CELL_PX - WINDOW_PAD
    size = CELL_PX + 2 * WINDOW_PAD + 1
    rows = np.arange(gy0, gy0 + size)
    cols = np.arange(gx0, gx0 + size)
    depth = gebco.minimum_depth(lat_of_px(rows), lon_of_px(cols))
    depth_margin = depth - np.float32(DEPTH_MARGIN_M)
    depth_margin[~np.isfinite(depth_margin)] = MISSING
    del depth
    # Distances beyond the reach are capped anyway, so each row strip of the window
    # only needs land within the reach: exact below the cap, bounded memory.
    reach = int(poleward_shift_px(rows).max())
    r0, c0 = gy0 - oy - reach, gx0 - ox - reach
    land = land[r0:r0 + size + 2 * reach, c0:c0 + size + 2 * reach]
    dist = np.full((size, size), np.inf, dtype=np.float32)
    for s0 in range(0, size, EDT_STRIP):
        s1 = min(s0 + EDT_STRIP, size)
        strip = land[s0:s1 + 2 * reach]
        if strip.any():
            dist[s0:s1] = ndimage.distance_transform_edt(~strip)[reach:reach + s1 - s0, reach:reach + size]
    del land
    mpp = conservative_mpp(rows).astype(np.float32)[:, None]
    distance_margin = np.minimum((dist - np.float32(DISTANCE_MARGIN_PX)) * mpp - np.float32(OFFSHORE_M),
                                 np.float32(DISTANCE_CAP_M))
    return depth_margin, distance_margin


def _pool(field, s, width):
    """Minimum at the fragment-window lattice over width entries per axis.
    A radius r uses 2r pixel rectangles for continuous depth, or 2r+1 distance nodes."""
    lattice = slice(POOL_PAD, POOL_PAD + CELL_PX + 2 * FRAGMENT_WINDOW_PX + 1, s)
    a = ndimage.minimum_filter1d(field, width, axis=1, mode="nearest")[:, lattice]
    return ndimage.minimum_filter1d(a, width, axis=0, mode="nearest")[lattice, :]


def pooled_fields(depth_margin, distance_margin):
    """{z: (depth, distance)} exact rectangle depth and node distance over radius s+2."""
    return {z: (_pool(depth_margin, 1 << (FULL_ZOOM - z), 2 * ((1 << (FULL_ZOOM - z)) + 2)),
                _pool(distance_margin, 1 << (FULL_ZOOM - z), 2 * ((1 << (FULL_ZOOM - z)) + 2) + 1))
            for z in range(MIN_ZOOM, MAX_ZOOM + 1)}


def component_whole(lo, hi):
    """Whether a component's complete extent is strictly inside every cell window that
    could draw it. Continuous closed extents touch cells from ceil(lo / CELL_PX) - 1
    through floor(hi / CELL_PX), so an exact seam point belongs to both cells.
    Non-finite bounds denote an extent clipped by the observation window, not a complete
    component. This one component-level predicate is the keep/drop invariant: complete
    small components are dropped everywhere; clipped or non-fitting ones are kept."""
    lo = np.asarray(lo, dtype=float)
    hi = np.asarray(hi, dtype=float)
    known = np.isfinite(lo) & np.isfinite(hi)
    safe_lo, safe_hi = np.where(known, lo, 0.0), np.where(known, hi, 0.0)
    first = np.ceil(safe_lo / CELL_PX) - 1
    last = np.floor(safe_hi / CELL_PX)
    return (known & (lo > last * CELL_PX - FRAGMENT_WINDOW_PX)
            & (hi < (first + 1) * CELL_PX + FRAGMENT_WINDOW_PX))


def depth_tiles(pooled, cx, cy):
    """{(z, x, y): [feature]} depth features in 4096 tile units.

    Judge connected vector components after final snapping on the shared fragment
    window, before clipping tiles. A component is area-filtered only if it is
    complete in every touched cell's window."""
    out = {}
    for z in range(MIN_ZOOM, MAX_ZOOM + 1):
        s = 1 << (FULL_ZOOM - z)
        pooled_depth, pooled_distance = pooled.pop(z)
        pad = FRAGMENT_WINDOW_PX // s          # lattice nodes before the cell's first node
        xs = (np.arange(pooled_depth.shape[1]) - pad) * 16.0
        ys = (np.arange(pooled_depth.shape[0]) - pad) * 16.0
        n = 1 << (z - GRID_ZOOM)
        for band in DEPTH_BANDS_M:
            g = np.minimum(pooled_depth - np.float32(band), pooled_distance)
            if not (g > 0).any():
                continue
            region = contour_region(xs, ys, g)
            if region is None:
                continue
            kept = []
            for p in _polygonal(region):
                x0, y0, x1, y1 = p.bounds
                lo_x = -np.inf if x0 == xs[0] else cx * CELL_PX + x0 * s / 16
                hi_x = np.inf if x1 == xs[-1] else cx * CELL_PX + x1 * s / 16
                lo_y = -np.inf if y0 == ys[0] else cy * CELL_PX + y0 * s / 16
                hi_y = np.inf if y1 == ys[-1] else cy * CELL_PX + y1 * s / 16
                whole = component_whole(lo_x, hi_x) & component_whole(lo_y, hi_y)
                rows = cy * CELL_PX + np.array([y0, y1]) * s / 16
                area = p.area * (s / 16 * float(mpp_of_px(rows).min())) ** 2
                if area >= MIN_FRAGMENT_M2 or not whole:
                    kept.append(p)
            if not kept:
                continue
            region = unary_union(kept)
            for i in range(n):
                for j in range(n):
                    tile = shapely.box(i * EXTENT, j * EXTENT, (i + 1) * EXTENT, (j + 1) * EXTENT)
                    pieces = _polygonal(region.intersection(tile))
                    if pieces:
                        geom = unary_union(pieces)
                        geom = affinity.translate(geom, -i * EXTENT, -j * EXTENT)
                        out.setdefault((z, cx * n + i, cy * n + j), []).append(
                            {"geometry": geom, "properties": {"min": band}})
    return out


def contour_region(xs, ys, values):
    gen = contourpy.contour_generator(xs, ys, values.astype(np.float64), name="serial", fill_type="OuterOffset")
    points, offsets = gen.filled(0.0, 1e30)
    polygons = []
    for pts, offs in zip(points, offsets):
        rings = [np.round(pts[offs[k]:offs[k + 1]]) for k in range(len(offs) - 1)]
        if len(rings[0]) < 4:
            continue
        # Rounding can make a hole touch its shell; make_valid of such a polygon may
        # return the shell without the hole, so subtract each hole explicitly.
        region = unary_union(_polygonal(make_valid(Polygon(rings[0]))))
        holes = [p for ring in rings[1:] if len(ring) >= 4 for p in _polygonal(make_valid(Polygon(ring)))]
        if holes:
            region = region.difference(unary_union(holes))
        polygons.extend(_polygonal(region))
    # Snap-round to integers so the encoder's own rounding finds valid geometry and
    # never applies its ring-by-ring repair to depth (moves edges by < 1 unit).
    polygons = [q for p in polygons for q in _polygonal(shapely.set_precision(p, 1.0)) if q.area > 0]
    if not polygons:
        return None
    return polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)


# ---------------------------------------------------------------- cell

def cell_metadata(cx, cy, release, identity):
    return {
        "name": "DingHai land", "format": "pbf", "type": "overlay",
        "dinghai:schemaVersion": SCHEMA_VERSION, "dinghai:release": release,
        "dinghai:cell": f"{GRID_ZOOM}/{cx}/{cy}",
        "attribution": f"{LAND_ATTRIBUTION}; GEBCO_2026 Grid",
        "dinghai:landAttribution": LAND_ATTRIBUTION,
        "dinghai:landLicense": LAND_LICENSE, "dinghai:landLicenseURL": LAND_LICENSE_URL,
        "dinghai:osmCopyrightURL": OSM_COPYRIGHT_URL,
        "dinghai:depthAttribution": GEBCO_ATTRIBUTION, "dinghai:depthDOI": GEBCO_DOI,
        "dinghai:depthTermsURL": GEBCO_TERMS_URL, "dinghai:depthNotice": DEPTH_NOTICE,
        "dinghai:protomapsBuild": identity["protomaps"]["build"],
        "dinghai:protomapsVersion": identity["protomaps"]["version"],
        "dinghai:protomapsSHA256": identity["protomaps"]["sha256"],
        "dinghai:gebcoGrid": identity["gebco"]["grid"],
        "dinghai:gebcoSHA256": identity["gebco"]["sha256"],
        "dinghai:generatorVersion": VERSION,
        "dinghai:generatorSHA256": identity["generatorSHA256"],
        "dinghai:sourceRepository": SOURCE_REPOSITORY,
        "dinghai:depth": params(),
        "vector_layers": [
            {"id": "landuse", "fields": {"class": "String"}, "minzoom": MIN_ZOOM, "maxzoom": MAX_ZOOM},
            {"id": "roads", "fields": {"class": "String"}, "minzoom": MIN_ZOOM, "maxzoom": MAX_ZOOM},
            {"id": "peaks", "fields": {"ele": "Number"}, "minzoom": MIN_ZOOM, "maxzoom": MAX_ZOOM},
            {"id": "places", "fields": {"kind": "String", "name": "String"}, "minzoom": MIN_ZOOM, "maxzoom": MAX_ZOOM},
            {"id": "depth", "fields": {"min": "Number"}, "minzoom": MIN_ZOOM, "maxzoom": MAX_ZOOM},
        ],
    }


def build_cell(archive, peaks, gebco, cx, cy):
    """{tile_id: gzip MVT}, per-layer feature counts."""
    peaks12 = cell_peaks(archive, peaks, cx, cy)
    land_tiles, cell_earth = {}, {}
    for z in range(MIN_ZOOM, MAX_ZOOM + 1):
        n = 1 << (z - GRID_ZOOM)
        for x in range(cx * n, (cx + 1) * n):
            for y in range(cy * n, (cy + 1) * n):
                tile = archive.get(z, x, y)
                if tile is None:
                    continue
                raw = split_layers(tile)
                layers = decode_layers(raw, ["earth", "landuse", "roads", "places"])
                if z == FULL_ZOOM:
                    earth = layers.get("earth")
                    polys = []
                    for f in (earth or {}).get("features", []):
                        g = f["geometry"]
                        polys += [g["coordinates"]] if g["type"] == "Polygon" else \
                            g["coordinates"] if g["type"] == "MultiPolygon" else []
                    cell_earth[(x, y)] = (polys, earth["extent"] if earth else EXTENT)
                    peaks = peaks12.get((x, y), [])
                else:
                    peaks = own_peaks(raw)
                land_tiles[(z, x, y)] = slim_land(z, layers, peaks)
    for (x, y), points in peaks12.items():  # z12 tiles without a source tile but with lifted peaks
        if (FULL_ZOOM, x, y) not in land_tiles:
            land_tiles[(FULL_ZOOM, x, y)] = slim_land(FULL_ZOOM, {}, points)
    pooled = pooled_fields(*depth_fields(archive, gebco, cx, cy, cell_earth))
    depth = depth_tiles(pooled, cx, cy)
    counts = defaultdict(int)
    tiles = {}
    for key in sorted(set(land_tiles) | set(depth)):
        z = key[0]
        land = land_tiles.get(key, [])
        e = land_extent(z)
        for layer in land:
            for f in layer["features"]:
                g = f["geometry"]
                if e != EXTENT:
                    g = affinity.scale(g, e / EXTENT, e / EXTENT, origin=(0, 0))
                if layer["name"] == "landuse":
                    # Snap-round to the integer grid here: the encoder's own rounding then
                    # keeps it valid instead of repairing it ring by ring.
                    g = unary_union(_polygonal(shapely.set_precision(g, 1.0)))
                f["geometry"] = g
            layer["features"] = [f for f in layer["features"] if not f["geometry"].is_empty]
        land = [layer for layer in land if layer["features"]]
        for layer in land:
            counts[layer["name"]] += len(layer["features"])
        pb = encode_layers(land, e)
        if key in depth:
            counts["depth"] += len(depth[key])
            pb += encode_layers([{"name": "depth", "features": depth[key]}], EXTENT)
        if pb:
            tiles[zxy_to_tileid(*key)] = gzip.compress(pb, compresslevel=9, mtime=0)
    return tiles, dict(counts)


# ---------------------------------------------------------------- identity / state

def file_sha256(path, cache_dir):
    path = Path(path).resolve()
    st = path.stat()
    cache = Path(cache_dir) / "input-hashes.json"
    known = json.loads(cache.read_text()) if cache.exists() else {}
    key = f"{path}|{st.st_size}|{st.st_mtime_ns}"
    if key not in known:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(1 << 24):
                h.update(chunk)
        known[key] = h.hexdigest()
        _atomic_write(cache, json.dumps(known, indent=1, sort_keys=True).encode())
    return known[key]


def input_identity(protomaps_path, protomaps_build, peaks_path, gebco_path, gebco_grid, state_dir):
    archive = Archive(protomaps_path)
    meta = archive.metadata()
    peaks_meta = Archive(peaks_path).metadata()
    build = (meta.get("version"), meta.get("planetiler:osm:osmosisreplicationtime"), meta.get("planetiler:githash"))
    for source in peaks_meta.get("dinghai:sources", []):
        if (source["version"], source["osmReplicationTime"], source["planetilerGithash"]) != build:
            raise ValueError(f"{peaks_path}: peaks come from another Protomaps build than {protomaps_path}")
    if peaks_meta.get("dinghai:peaksVersion") is None:
        raise ValueError(f"{peaks_path}: not written by the peaks command")
    gebco = Gebco(gebco_path)
    return {
        "generatorVersion": VERSION, "generatorSHA256": generator_sha256(), "params": params(),
        "protomaps": {
            "build": protomaps_build, "version": meta.get("version"),
            "bytes": Path(protomaps_path).stat().st_size,
            "sha256": file_sha256(protomaps_path, state_dir),
            "planetilerGithash": meta.get("planetiler:githash"),
            "osmReplicationTime": meta.get("planetiler:osm:osmosisreplicationtime"),
            "osmReplicationSequence": meta.get("planetiler:osm:osmosisreplicationseq"),
            "zooms": [archive.header["min_zoom"], archive.header["max_zoom"]],
        },
        "peaks": {"bytes": Path(peaks_path).stat().st_size, "sha256": file_sha256(peaks_path, state_dir),
                  "zooms": list(peak_zooms(Archive(peaks_path))), "sources": peaks_meta["dinghai:sources"]},
        "gebco": {
            "grid": gebco_grid, "doi": GEBCO_DOI, "title": gebco.title,
            "bytes": Path(gebco_path).stat().st_size, "sha256": file_sha256(gebco_path, state_dir),
            "coverage": "global" if gebco.global_lon and gebco.nlat == 180 * 240 else "subset",
        },
    }


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def cell_path(output, release, x, y):
    return Path(output) / "releases" / release / "cells" / str(GRID_ZOOM) / str(x) / f"{y}.pmtiles"


def _peak_rss():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform == "darwin" else rss * 1024


_WORKER = {}


def _init_worker(protomaps, peaks, gebco):
    _WORKER["archive"] = Archive(protomaps)
    _WORKER["peaks"] = Archive(peaks)
    _WORKER["gebco"] = Gebco(gebco)


def _run_cell(job):
    x, y, output, release, identity, run_id, state_dir = job
    started = time.monotonic()
    tiles, counts = build_cell(_WORKER["archive"], _WORKER["peaks"], _WORKER["gebco"], x, y)
    path = cell_path(output, release, x, y)
    land = sum(counts.get(name, 0) for name in ("landuse", "roads", "peaks", "places"))
    record = {"x": x, "y": y, "run": run_id, "counts": counts}
    if land == 0 and counts.get("depth", 0) == 0:
        record["empty"] = True
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        write_cell_pmtiles(tmp, tiles, x, y, cell_metadata(x, y, release, identity))
        data = tmp.read_bytes()
        os.replace(tmp, path)
        record.update(empty=False, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    record["seconds"] = round(time.monotonic() - started, 3)
    record["peakRSSBytes"] = _peak_rss()
    _atomic_write(Path(state_dir) / "cells" / f"{x}-{y}.json", json.dumps(record, sort_keys=True).encode())
    return record


def _done(state_dir, output, release, run_id, x, y):
    state = Path(state_dir) / "cells" / f"{x}-{y}.json"
    if not state.exists():
        return None
    record = json.loads(state.read_text())
    if record.get("run") != run_id:
        return None
    path = cell_path(output, release, x, y)
    if record["empty"]:
        return None if path.exists() else record
    return record if path.exists() and path.stat().st_size == record["bytes"] else None


def build(protomaps, protomaps_build, peaks, gebco, gebco_grid, release, cells, output, state_dir,
          jobs=1, max_rss_bytes=None, log=print):
    """Builds every selected cell (resumable), then writes cells.json and manifest.json."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    identity = input_identity(protomaps, protomaps_build, peaks, gebco, gebco_grid, state_dir)
    run_id = hashlib.sha256(json.dumps({"identity": identity, "release": release},
                                       sort_keys=True).encode()).hexdigest()
    cells = sorted(set(cells))
    todo = [(x, y) for x, y in cells if _done(state_dir, output, release, run_id, x, y) is None]
    log(f"{len(cells)} cells selected, {len(cells) - len(todo)} already done, {len(todo)} to build")
    jobs_list = [(x, y, str(output), release, identity, run_id, str(state_dir)) for x, y in todo]
    finished = 0
    if jobs_list:
        context = multiprocessing.get_context("spawn")
        with context.Pool(jobs, initializer=_init_worker, initargs=(str(protomaps), str(peaks), str(gebco)),
                          maxtasksperchild=50) as pool:
            for record in pool.imap_unordered(_run_cell, jobs_list):
                finished += 1
                log(json.dumps({k: record.get(k) for k in ("x", "y", "empty", "bytes", "seconds", "peakRSSBytes")}))
                if max_rss_bytes and record["peakRSSBytes"] > max_rss_bytes:
                    pool.terminate()
                    raise MemoryError(f"worker peak RSS {record['peakRSSBytes']} exceeds {max_rss_bytes}; "
                                      "finished cells are kept, rerun with fewer jobs to resume")
    return finalize(output, release, cells, identity, run_id, state_dir)


def finalize(output, release, cells, identity, run_id, state_dir):
    output = Path(output)
    entries, expected = [], set()
    for x, y in cells:
        record = _done(state_dir, output, release, run_id, x, y)
        if record is None:
            raise ValueError(f"cell {x}/{y} is not built for this run")
        if record["empty"]:
            continue
        path = cell_path(output, release, x, y)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != record["bytes"] or digest != record["sha256"]:
            raise ValueError(f"cell {x}/{y} changed after it was built")
        entries.append({"x": x, "y": y, "bytes": len(data), "sha256": digest})
        expected.add(path.resolve())
    cells_root = output / "releases" / release / "cells"
    for tmp in cells_root.rglob(".*.tmp") if cells_root.exists() else []:
        tmp.unlink()  # this producer's own interrupted writes
    stray = sorted(str(p) for p in cells_root.rglob("*") if p.is_file() and p.resolve() not in expected) \
        if cells_root.exists() else []
    if stray:
        raise ValueError(f"unlisted files under {cells_root}: {stray[:5]}")
    cells_json = json.dumps(entries, separators=(",", ":")).encode()
    _atomic_write(output / "releases" / release / "cells.json", cells_json)
    manifest = {
        "id": "dinghai-land", "manifestVersion": 1, "release": release, "schemaVersion": SCHEMA_VERSION,
        "gridZoom": GRID_ZOOM, "minimumZoom": MIN_ZOOM, "maximumZoom": MAX_ZOOM, "layers": list(LAYERS),
        "sources": {
            "protomaps": {k: identity["protomaps"][k] for k in (
                "build", "version", "bytes", "sha256", "planetilerGithash", "osmReplicationTime",
                "osmReplicationSequence", "zooms")},
            "peaks": identity["peaks"],
            "gebco": {k: identity["gebco"][k] for k in ("grid", "doi", "bytes", "sha256", "coverage")},
        },
        "depth": {"thresholdMeters": DEPTH_BANDS_M[0], "offshoreMeters": OFFSHORE_M,
                  "minimumAreaSquareMeters": MIN_FRAGMENT_M2, "fragmentWindowPixels": FRAGMENT_WINDOW_PX,
                  "bandsMeters": list(DEPTH_BANDS_M),
                  "property": "min", "notice": DEPTH_NOTICE},
        "attribution": {"land": LAND_ATTRIBUTION, "depth": GEBCO_ATTRIBUTION},
        "license": {"land": LAND_LICENSE, "landURL": LAND_LICENSE_URL, "osmCopyrightURL": OSM_COPYRIGHT_URL,
                    "depth": "GEBCO Grid terms of use (public domain)", "depthURL": GEBCO_TERMS_URL},
        "generatorIdentity": f"land_product.py:{VERSION}:sha256:{identity['generatorSHA256']}",
        "sourceRepository": SOURCE_REPOSITORY,
        "cellsURL": f"releases/{release}/cells/{{z}}/{{x}}/{{y}}.pmtiles",
        "cells": f"releases/{release}/cells.json",
        "cellsBytes": len(cells_json), "cellsSHA256": hashlib.sha256(cells_json).hexdigest(),
        "cellCount": len(entries), "totalBytes": sum(e["bytes"] for e in entries),
    }
    _atomic_write(output / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1).encode())
    return manifest


# ---------------------------------------------------------------- peaks archive

def make_peaks(inputs, output, zooms=DEFAULT_PEAK_ZOOMS, log=print):
    """Reduces Protomaps extracts to pois.kind=peak points with numeric elevation at the given
    zooms (each input must cover them). A tile present in several inputs must reduce to
    identical bytes."""
    zooms = tuple(sorted(set(zooms)))
    if not zooms or any(z not in PEAK_ZOOM_RANGE for z in zooms):
        raise ValueError(f"peak zooms must be within {PEAK_ZOOM_RANGE.start}-{PEAK_ZOOM_RANGE.stop - 1}")
    tiles, sources = {}, []
    for path in inputs:
        archive = Archive(path)
        if archive.header["min_zoom"] > zooms[0] or archive.header["max_zoom"] < zooms[-1]:
            raise ValueError(f"{path}: covers z{archive.header['min_zoom']}-{archive.header['max_zoom']}, "
                             f"not z{zooms[0]}-{zooms[-1]}")
        meta = archive.metadata()
        sources.append({"path": Path(path).name, "bytes": Path(path).stat().st_size,
                        "version": meta.get("version"),
                        "osmReplicationTime": meta.get("planetiler:osm:osmosisreplicationtime"),
                        "planetilerGithash": meta.get("planetiler:githash")})
        scanned = 0
        for z, x, y, data in _all_tiles(archive):
            if z not in zooms:
                continue
            scanned += 1
            raw = split_layers(data).get("pois")
            points, extent = peaks_in_layer(raw) if raw is not None else ([], EXTENT)
            if not points:
                continue
            blob = gzip.compress(encode_layers([{"name": "pois", "features": [
                {"geometry": Point(px, py), "properties": {"kind": "peak", "elevation": ele}}
                for px, py, ele in points]}], extent), compresslevel=9, mtime=0)
            tile_id = zxy_to_tileid(z, x, y)
            if tiles.setdefault(tile_id, blob) != blob:
                raise ValueError(f"{path}: tile {z}/{x}/{y} disagrees with an earlier input")
        log(json.dumps({"input": Path(path).name, "tilesScanned": scanned, "peakTiles": len(tiles)}))
    if not tiles:
        raise ValueError("no peaks found")
    builds = {(s["version"], s["osmReplicationTime"], s["planetilerGithash"]) for s in sources}
    if len(builds) != 1:
        raise ValueError(f"inputs come from different builds: {sorted(map(str, builds))}")
    metadata = {"name": f"DingHai peaks (Protomaps pois kind=peak, z{'-'.join(map(str, zooms))})",
                "dinghai:peaksVersion": VERSION, "dinghai:peakZooms": list(zooms),
                "dinghai:sources": sources, "attribution": LAND_ATTRIBUTION,
                "vector_layers": [{"id": "pois", "fields": {"kind": "String", "elevation": "Number"}}]}
    tmp = Path(output).with_name(f".{Path(output).name}.tmp")
    write_pmtiles(tmp, tiles, metadata, (-180.0, -85.0511287798066, 180.0, 85.0511287798066),
                  zooms[0], zooms[-1])
    os.replace(tmp, output)
    return {"tiles": len(tiles), "bytes": Path(output).stat().st_size}


# ---------------------------------------------------------------- coastal cell list

def coastal_cells(seamap_z6):
    """Seamap z8 cells whose z6 land raster (16x16 px per cell) is partly land, or that
    contain a z6 seamark/light point."""
    archive = Archive(seamap_z6)
    state, marks = {}, set()
    p = 64
    for z, x, y, data in _all_tiles(archive):
        if z != 6:
            continue
        layers = mvt.decode(data, default_options={"y_coord_down": True})
        img = Image.new("1", (p, p), 0)
        draw = ImageDraw.Draw(img)
        land = layers.get("land", {})
        ext = land.get("extent", 4096)
        for f in land.get("features", []):
            g = f["geometry"]
            polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"] if g["type"] == "MultiPolygon" else []
            for poly in polys:
                for i, ring in enumerate(poly):
                    pts = [(a / ext * p, b / ext * p) for a, b in ring]
                    if len(pts) >= 3:
                        draw.polygon(pts, fill=1 if i == 0 else 0)
        a = np.asarray(img, dtype=bool)
        for i in range(4):
            for j in range(4):
                state[(x * 4 + i, y * 4 + j)] = int(a[j * 16:(j + 1) * 16, i * 16:(i + 1) * 16].sum())
        for name in ("seamark", "light"):
            layer = layers.get(name, {})
            e = layer.get("extent", 4096)
            for f in layer.get("features", []):
                if f["geometry"]["type"] != "Point":
                    continue
                px, py = f["geometry"]["coordinates"]
                if 0 <= px < e and 0 <= py < e:
                    marks.add((x * 4 + int(px / e * 4), y * 4 + int(py / e * 4)))
    coastal = {k for k, v in state.items() if 0 < v < 256}
    cells = sorted(coastal | marks)
    meta = archive.metadata()
    return {"source": {"path": Path(seamap_z6).name, "bytes": Path(seamap_z6).stat().st_size,
                       "name": meta.get("name"),
                       "osmReplicationTime": meta.get("planetiler:osm:osmosisreplicationtime")},
            "method": "z6 land raster 16x16 px per z8 cell: partly land; union z6 seamark/light points",
            "counts": {"coastal": len(coastal), "seamarkCells": len(marks), "selected": len(cells)},
            "cells": [list(c) for c in cells]}


def _all_tiles(archive):
    def walk(entries):
        for e in entries:
            if e.run_length:
                data = archive._read(archive.header["tile_data_offset"] + e.offset, e.length)
                if archive.header["tile_compression"] == Compression.GZIP:
                    data = gzip.decompress(data)
                for k in range(e.run_length):
                    yield (*tileid_to_zxy(e.tile_id + k), data)
            else:
                yield from walk(archive._directory(archive.header["leaf_directory_offset"] + e.offset, e.length))
    yield from walk(archive.root)


def cells_region(cells, inset=1e-6):
    """GeoJSON MultiPolygon of the cells, each inset so a region extract takes only its tiles."""
    polygons = []
    for x, y in cells:
        w, s, e, n = cell_bounds(x, y)
        w, s, e, n = w + inset, s + inset, e - inset, n - inset
        polygons.append([[[w, s], [e, s], [e, n], [w, n], [w, s]]])
    return {"type": "Feature", "properties": {},
            "geometry": {"type": "MultiPolygon", "coordinates": polygons}}


# ---------------------------------------------------------------- CLI

def _cell_arg(text):
    x, y = (int(v) for v in text.split("/"))
    if not (0 <= x < 256 and 0 <= y < 256):
        raise argparse.ArgumentTypeError("cell must be X/Y within the z8 grid")
    return x, y


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("cells", help="coastal z8 cell list from a Seamap z6 extract")
    c.add_argument("--seamap-z6", type=Path, required=True)
    c.add_argument("--output", type=Path, required=True)
    c.add_argument("--region-output", type=Path,
                   help="also write the cells as a GeoJSON MultiPolygon for pmtiles extract --region")
    p = sub.add_parser("peaks", help="reduce z13 (or z13-14) Protomaps extracts to peak points")
    p.add_argument("inputs", type=Path, nargs="+")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--zooms", default="13", help="comma-separated source zooms, default 13")
    b = sub.add_parser("build", help="build cells, then cells.json and manifest.json")
    b.add_argument("--protomaps", type=Path, required=True, help="local z0-12 extract of one Protomaps build")
    b.add_argument("--protomaps-build", required=True, help="build key, e.g. 20261025.pmtiles")
    b.add_argument("--peaks", type=Path, required=True, help="output of the peaks command, same build")
    b.add_argument("--gebco", type=Path, required=True, help="GEBCO 15\" netCDF")
    b.add_argument("--gebco-grid", required=True, help="grid name, e.g. GEBCO_2026")
    b.add_argument("--release", required=True)
    b.add_argument("--output", type=Path, required=True, help="resource root (contains manifest.json)")
    b.add_argument("--state-dir", type=Path, required=True, help="resume state, outside --output")
    sel = b.add_mutually_exclusive_group(required=True)
    sel.add_argument("--cell", type=_cell_arg, action="append", metavar="X/Y")
    sel.add_argument("--cells-file", type=Path, help="output of the cells command")
    b.add_argument("--jobs", type=int, default=1)
    b.add_argument("--max-rss-mb", type=int, help="stop when a worker's peak RSS exceeds this")
    a = parser.parse_args(argv)
    if a.command == "cells":
        result = coastal_cells(a.seamap_z6)
        _atomic_write(a.output, json.dumps(result, separators=(",", ":")).encode())
        if a.region_output:
            _atomic_write(a.region_output, json.dumps(cells_region(result["cells"]), separators=(",", ":")).encode())
        print(json.dumps({"counts": result["counts"], "source": result["source"]}))
        return
    if a.command == "peaks":
        print(json.dumps(make_peaks(a.inputs, a.output, tuple(int(z) for z in a.zooms.split(",")))))
        return
    if a.state_dir.resolve().is_relative_to(a.output.resolve()):
        parser.error("--state-dir must be outside --output")
    cells = [tuple(c) for c in json.loads(a.cells_file.read_text())["cells"]] if a.cells_file else a.cell
    manifest = build(a.protomaps, a.protomaps_build, a.peaks, a.gebco, a.gebco_grid, a.release, cells, a.output,
                     a.state_dir, a.jobs, a.max_rss_mb * 1048576 if a.max_rss_mb else None)
    print(json.dumps({k: manifest[k] for k in ("release", "cellCount", "totalBytes", "generatorIdentity")}))


if __name__ == "__main__":
    main()
