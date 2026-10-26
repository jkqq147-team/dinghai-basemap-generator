#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Independent audit of a land-product release: manifest, cells.json, every cell pmtiles.

Structural checks use only the Python standard library (own pmtiles/MVT decoders).
With --protomaps and --gebco the depth geography is re-checked against the sources
with NumPy/Shapely/netCDF4; nothing is imported from land_product.py.
"""
import argparse
import gzip
import hashlib
import json
import math
import struct
from collections import Counter, defaultdict
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
EDGE_TOLERANCE_PX = 0.5   # z12 px slack for MVT rounding when matching pieces across an edge
ODBL_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
LAND_ATTRIBUTION = "© OpenStreetMap contributors · Protomaps"
GEBCO_DOI = "10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa"
EARTH_M = 2 * math.pi * 6378137.0
WORLD12 = 256 << 12


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


def _tile_id(z, x, y):
    """PMTiles v3 Hilbert tile id."""
    acc = ((1 << (2 * z)) - 1) // 3
    for a in range(z - 1, -1, -1):
        s = 1 << a
        rx, ry = s & x, s & y
        acc += ((3 * rx) ^ ry) << a
        x, y = _rotate(s, x, y, rx, ry)
    return acc


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


def ring_area(ring):
    """Shoelace area in tile coordinates (y down): positive for MVT exterior rings."""
    return sum(ax * by - bx * ay for (ax, ay), (bx, by) in zip(ring, ring[1:])) / 2


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
    counts, zooms, depth = Counter(), Counter(), defaultdict(list)
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
                    depth[(z, props["min"])].append((tx, ty, parts))
    return {"tiles": len(tiles), "zooms": dict(sorted(zooms.items())), "features": dict(counts),
            "metadata": meta}, depth


# ---------------------------------------------------------------- geography

def _lat(gy):
    import numpy as np
    return np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * np.asarray(gy, dtype=float) / WORLD12))))


def _mpp(gy):
    import numpy as np
    return EARTH_M / WORLD12 * np.cos(np.radians(_lat(gy)))


def depth_polygons(features, z):
    """Shapely polygons in z12 world pixels from decoded depth features of one zoom/band."""
    from shapely.geometry import Polygon
    k = 1 << (12 - z)
    polys = []
    for tx, ty, parts in features:
        shell, holes = None, []
        for ring in parts:
            pts = [((tx * 4096 + a) / 16 * k, (ty * 4096 + b) / 16 * k) for a, b in ring]
            area = ring_area(ring)
            if area > 0:
                if shell is not None:
                    polys.append(Polygon(shell, holes))
                shell, holes = pts, []
            elif area < 0:
                if shell is None:
                    raise ValueError("depth hole before exterior ring")
                holes.append(pts)
        if shell is not None:
            polys.append(Polygon(shell, holes))
    for p in polys:
        if not p.is_valid:
            raise ValueError("invalid depth polygon")
    return polys


class Sources:
    def __init__(self, protomaps, gebco):
        import netCDF4
        import numpy as np
        self.protomaps_path = Path(protomaps)
        self.f = open(self.protomaps_path, "rb")
        head = self.f.read(127)
        if head[:7] != b"PMTiles" or head[7] != 3:
            raise ValueError("protomaps source is not pmtiles v3")
        q = struct.unpack_from("<11Q", head, 8)
        self.root = (q[0], q[1])
        self.leaf_offset, self.data_offset = q[4], q[6]
        self.directories = {}
        ds = netCDF4.Dataset(gebco)
        ds.set_auto_mask(False)
        self.lat = np.asarray(ds["lat"][:], dtype=float)
        self.lon = np.asarray(ds["lon"][:], dtype=float)
        self.elev = ds["elevation"]

    def _directory(self, offset, length):
        key = (offset, length)
        if key not in self.directories:
            if len(self.directories) > 32:
                self.directories.clear()
            self.f.seek(offset)
            buf = gzip.decompress(self.f.read(length))
            n, i = _varint(buf, 0)
            cols = []
            for _ in range(3):
                col = []
                for _ in range(n):
                    v, i = _varint(buf, i)
                    col.append(v)
                cols.append(col)
            ids, last = [], 0
            for v in cols[0]:
                last += v
                ids.append(last)
            offs = []
            for k in range(n):
                v, i = _varint(buf, i)
                offs.append(offs[k - 1] + cols[2][k - 1] if v == 0 and k > 0 else v - 1)
            self.directories[key] = (ids, cols[1], cols[2], offs)
        return self.directories[key]

    def tile(self, z, x, y):
        """Raw MVT of a source tile, by our own directory walk (no land_product code)."""
        import bisect
        want = _tile_id(z, x, y)
        offset, length = self.root
        for _ in range(4):
            ids, runs, lengths, offs = self._directory(offset, length)
            k = bisect.bisect_right(ids, want) - 1
            if k < 0:
                return None
            if runs[k] == 0:
                offset, length = self.leaf_offset + offs[k], lengths[k]
                continue
            if want >= ids[k] + runs[k]:
                return None
            self.f.seek(self.data_offset + offs[k])
            return gzip.decompress(self.f.read(lengths[k]))
        return None

    def land(self, x, y):
        """OSM land (Protomaps z12 earth polygons) around cell x/y, z12 world pixels."""
        import numpy as np
        from shapely.geometry import Polygon
        gy = np.array([y * 4096, (y + 1) * 4096])
        reach = float(np.max(1.25 * OFFSHORE_M / _mpp(gy - 4096) )) + 64
        rings = math.ceil(reach / 256)
        polys = []
        for ty in range(y * 16 - rings, y * 16 + 16 + rings):
            if not 0 <= ty < 4096:
                continue
            for tx in range(x * 16 - rings, x * 16 + 16 + rings):
                data = self.tile(12, tx % 4096, ty)
                if data is None:
                    continue
                earth = decode_tile(data).get("earth")
                if not earth:
                    continue
                extent, features = earth
                for gtype, _, parts in features:
                    if gtype != 3:
                        continue
                    shell, holes = None, []
                    for ring in parts:
                        pts = [(tx * 256 + a * 256 / extent, ty * 256 + b * 256 / extent) for a, b in ring]
                        area = ring_area(ring)
                        if area > 0:
                            if shell is not None:
                                polys.append(Polygon(shell, holes).buffer(0))
                            shell, holes = pts, []
                        elif area < 0 and shell is not None:
                            holes.append(pts)
                    if shell is not None:
                        polys.append(Polygon(shell, holes).buffer(0))
        return [p for p in polys if not p.is_empty]

    def depth(self, gx, gy):
        """Bilinear GEBCO depth (m, positive down) at z12 world pixel points; NaN without coverage."""
        import numpy as np
        lat = _lat(gy)
        lon = np.asarray(gx, dtype=float) / WORLD12 * 360 - 180
        fi = np.interp(lat, self.lat, np.arange(len(self.lat)), left=np.nan, right=np.nan)
        fj = np.interp(lon, self.lon, np.arange(len(self.lon)), left=np.nan, right=np.nan)
        ok = np.isfinite(fi) & np.isfinite(fj)
        out = np.full(len(lat), np.nan)
        if not ok.any():
            return out
        i = np.minimum(np.floor(fi[ok]).astype(int), len(self.lat) - 2)
        j = np.minimum(np.floor(fj[ok]).astype(int), len(self.lon) - 2)
        i0, i1, j0, j1 = i.min(), i.max() + 2, j.min(), j.max() + 2
        e = -np.asarray(self.elev[i0:i1, j0:j1], dtype=float)
        wi, wj = fi[ok] - i, fj[ok] - j
        a, b = i - i0, j - j0
        out[ok] = ((1 - wi) * ((1 - wj) * e[a, b] + wj * e[a, b + 1])
                   + wi * ((1 - wj) * e[a + 1, b] + wj * e[a + 1, b + 1]))
        return out

    def nodes_in(self, poly):
        """GEBCO node positions (z12 world pixels) inside poly."""
        import numpy as np
        import shapely
        minx, miny, maxx, maxy = poly.bounds
        lats = self.lat[(self.lat >= _lat(maxy)) & (self.lat <= _lat(miny))]
        lons = self.lon[(self.lon >= minx / WORLD12 * 360 - 180) & (self.lon <= maxx / WORLD12 * 360 - 180)]
        if not len(lats) or not len(lons):
            return np.empty(0), np.empty(0)
        gy = (1 - np.arcsinh(np.tan(np.radians(lats))) / np.pi) / 2 * WORLD12
        gx = (lons + 180) / 360 * WORLD12
        X, Y = np.meshgrid(gx, gy)
        X, Y = X.ravel(), Y.ravel()
        inside = shapely.contains_xy(poly, X, Y)
        return X[inside], Y[inside]


def check_depth_geography(depth, sources, x, y):
    """Every depth polygon: depth >= band floor and >= OFFSHORE_M from OSM land; interior
    pieces >= MIN_FRAGMENT_M2. Returns summary statistics."""
    import numpy as np
    import shapely
    from shapely.ops import unary_union
    land = sources.land(x, y)
    tree = shapely.STRtree(land) if land else None
    cell = shapely.box(x * 4096, y * 4096, (x + 1) * 4096, (y + 1) * 4096)
    stats = {"min_offshore_m": None, "min_depth_margin_m": None, "points": 0, "pieces": 0,
             "min_interior_piece_m2": None}
    edge_pieces = []

    def note(key, value):
        if stats[key] is None or value < stats[key]:
            stats[key] = float(value)

    by_zoom = defaultdict(dict)
    for (z, band), features in depth.items():
        by_zoom[z][band] = unary_union(depth_polygons(features, z))
    for z, bands in by_zoom.items():
        for band, region in bands.items():
            if not region.buffer(-1e-6).within(cell) and not region.within(cell.buffer(1e-9)):
                raise ValueError(f"cell {x}/{y}: z{z} depth {band} leaves the cell")
            boundary = shapely.segmentize(region.boundary, 0.25)
            pts = shapely.get_coordinates(boundary)
            nx, ny = sources.nodes_in(region)
            px = np.concatenate([pts[:, 0], nx])
            py = np.concatenate([pts[:, 1], ny])
            d = sources.depth(px, py)
            if not np.all(d >= band):
                bad = np.flatnonzero(~(d >= band))[0]
                raise ValueError(f"cell {x}/{y}: z{z} depth {band} at ({px[bad]:.1f},{py[bad]:.1f}) "
                                 f"has GEBCO depth {d[bad]}")
            note("min_depth_margin_m", float(np.min(d - band)))
            stats["points"] += len(px)
            if tree is not None:
                if tree.query(region, predicate="intersects").size:
                    raise ValueError(f"cell {x}/{y}: z{z} depth {band} touches land")
                points = shapely.points(pts)
                (pi, li), dist = tree.query_nearest(points, return_distance=True, all_matches=False)
                gy = pts[pi, 1]
                q = 1.25 * OFFSHORE_M / _mpp(gy) + 4
                mpp = np.minimum(_mpp(gy - q), _mpp(gy + q))
                ground = dist * mpp
                if ground.size and ground.min() < OFFSHORE_M:
                    k = int(np.argmin(ground))
                    raise ValueError(f"cell {x}/{y}: z{z} depth {band} {ground[k]:.1f} m from land")
                if ground.size:
                    note("min_offshore_m", ground.min())
            interior, edge = region_pieces(region, z, band, x, y)
            stats["pieces"] += len(interior) + len(edge)
            for m2 in interior:
                note("min_interior_piece_m2", m2)
            edge_pieces.extend(edge)
    return stats, edge_pieces


def region_pieces(region, z, band, x, y):
    """Splits one cell's zoom/band depth region into interior pieces (each must cover
    >= 1 km2 here; returns their m2) and pieces reaching a cell edge (records judged across
    cells by check_edge_fragments)."""
    interior, edge = [], []
    for piece in getattr(region, "geoms", [region]):
        if piece.is_empty:
            continue
        minx, miny, maxx, maxy = piece.bounds
        m2 = piece.area * float(min(_mpp(miny), _mpp(maxy))) ** 2
        edges = _edge_intervals(piece, x, y)
        if edges:
            edge.append({"z": z, "band": band, "cell": (x, y), "m2": m2,
                         "bbox": (minx, miny, maxx, maxy), "edges": edges})
        elif m2 < MIN_FRAGMENT_M2:
            raise ValueError(f"cell {x}/{y}: z{z} depth {band} piece of {m2:.0f} m2")
        else:
            interior.append(m2)
    return interior, edge


def _edge_intervals(piece, x, y):
    """{edge: [(lo, hi)]} where the piece's outline lies on the cell's W/E/N/S edge (z12 px
    along the edge), merged; empty when the piece does not reach an edge."""
    import shapely
    x0, y0, x1, y1 = x * 4096, y * 4096, (x + 1) * 4096, (y + 1) * 4096
    lines = {"W": ((x0, y0), (x0, y1), 1), "E": ((x1, y0), (x1, y1), 1),
             "N": ((x0, y0), (x1, y0), 0), "S": ((x0, y1), (x1, y1), 0)}
    out = {}
    for edge, (a, b, axis) in lines.items():
        hit = piece.boundary.intersection(shapely.LineString([a, b]).buffer(1e-6))
        if hit.is_empty:
            continue
        spans = []
        for part in getattr(hit, "geoms", [hit]):
            coords = shapely.get_coordinates(part)[:, axis]
            spans.append((float(coords.min()), float(coords.max())))
        spans.sort()
        merged = [list(spans[0])]
        for lo, hi in spans[1:]:
            if lo <= merged[-1][1] + EDGE_TOLERANCE_PX:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        out[edge] = [tuple(m) for m in merged]
    return out


def component_whole(lo, hi):
    """Independent restatement of the producer's component-level rule. Shared seam nodes
    belong to both adjacent cells, and only a strictly contained complete span can be
    judged by its area."""
    first = math.floor((lo - 1) / 4096)
    last = math.floor(hi / 4096)
    return lo > last * 4096 - FRAGMENT_WINDOW_PX and hi < (first + 1) * 4096 + FRAGMENT_WINDOW_PX


OPPOSITE = {"W": ("E", -1, 0), "E": ("W", 1, 0), "N": ("S", 0, -1), "S": ("N", 0, 1)}


def check_edge_fragments(pieces, listed):
    """Merges depth pieces that continue across cell edges (same zoom and band, overlapping
    intervals on the shared edge) and requires each merged fragment to cover >= 1 km2, unless
    it does not fit the fragment window of the cells it touches (then no cell sees all of it)
    or it continues into a cell that is not in the release (unverifiable). A positive-length
    interval on a listed shared edge must have a positive-length counterpart on the other
    side; point-only contacts are allowed. Land/MISSING is not inferred from the polygons,
    so a one-sided drawn segment is rejected rather than silently exempted."""
    parent = list(range(len(pieces)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    index = defaultdict(list)
    for i, p in enumerate(pieces):
        for edge in p["edges"]:
            index[(p["z"], p["band"], p["cell"], edge)].append(i)
    open_edge = [False] * len(pieces)
    for i, p in enumerate(pieces):
        for edge, spans in p["edges"].items():
            other, dx, dy = OPPOSITE[edge]
            nx, ny = (p["cell"][0] + dx) % 256, p["cell"][1] + dy
            if not 0 <= ny < 256:
                continue
            if (nx, ny) not in listed:
                open_edge[i] = True
                continue
            neighbours = index.get((p["z"], p["band"], (nx, ny), other), [])
            for lo, hi in spans:
                if hi - lo <= 1e-6:
                    continue  # a point touching an edge need not have a drawn continuation
                matched = False
                for j in neighbours:
                    for lo2, hi2 in pieces[j]["edges"][other]:
                        # Both sides must draw a segment, not merely touch at a point.
                        if (hi2 - lo2 > 1e-6
                                and min(hi, hi2) - max(lo, lo2) > -EDGE_TOLERANCE_PX):
                            matched = True
                            parent[find(i)] = find(j)
                if not matched:
                    raise ValueError(
                        f"one-sided shared-edge depth: fragment across cells {p['cell']} "
                        f"z{p['z']} band {p['band']} edge {edge} [{lo}, {hi}] has no "
                        f"counterpart in {(nx, ny)}")
    groups = defaultdict(list)
    for i in range(len(pieces)):
        groups[find(i)].append(i)
    summary = {"edgeFragments": len(groups), "smallestCheckedM2": None, "windowExempt": 0, "openExempt": 0}
    for members in groups.values():
        m2 = sum(pieces[i]["m2"] for i in members)
        if m2 >= MIN_FRAGMENT_M2:
            if summary["smallestCheckedM2"] is None or m2 < summary["smallestCheckedM2"]:
                summary["smallestCheckedM2"] = m2
            continue
        if any(open_edge[i] for i in members):
            summary["openExempt"] += 1
            continue
        bx0 = min(pieces[i]["bbox"][0] for i in members); by0 = min(pieces[i]["bbox"][1] for i in members)
        bx1 = max(pieces[i]["bbox"][2] for i in members); by1 = max(pieces[i]["bbox"][3] for i in members)
        t = EDGE_TOLERANCE_PX
        if not (component_whole(bx0 - t, bx1 + t) and component_whole(by0 - t, by1 + t)):
            summary["windowExempt"] += 1
            continue
        p = pieces[members[0]]
        raise ValueError(f"z{p['z']} depth {p['band']}: fragment across cells "
                         f"{sorted({pieces[i]['cell'] for i in members})} covers {m2:.0f} m2")
    return summary


# ---------------------------------------------------------------- release

def audit(root, protomaps=None, gebco=None, jobs=1):
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
    report = {"release": release, "cells": {}, "geography": bool(protomaps and gebco)}
    jobs_list = [(str(cell_root), manifest, c, protomaps and str(protomaps), gebco and str(gebco)) for c in cells]
    edge_pieces = []
    if jobs > 1:
        import multiprocessing
        with multiprocessing.get_context("spawn").Pool(jobs) as pool:
            results = pool.imap_unordered(_audit_cell, jobs_list, chunksize=4)
            for key, summary, pieces in results:
                report["cells"][key] = summary
                edge_pieces.extend(pieces)
    else:
        for job in jobs_list:
            key, summary, pieces = _audit_cell(job)
            report["cells"][key] = summary
            edge_pieces.extend(pieces)
    report["cells"] = dict(sorted(report["cells"].items()))
    if report["geography"]:
        report["edgeFragments"] = check_edge_fragments(edge_pieces, {(c["x"], c["y"]) for c in cells})
    return report


_SOURCES = {}


def _audit_cell(job):
    cell_root, manifest, c, protomaps, gebco = job
    x, y = c["x"], c["y"]
    data = (Path(cell_root) / "8" / str(x) / f"{y}.pmtiles").read_bytes()
    if len(data) != c["bytes"] or hashlib.sha256(data).hexdigest() != c["sha256"]:
        raise ValueError(f"cell {x}/{y}: bytes/sha256 disagree with cells.json")
    summary, depth = check_cell(data, manifest, x, y)
    summary.pop("metadata")
    pieces = []
    if protomaps and gebco and depth:
        if (protomaps, gebco) not in _SOURCES:
            _SOURCES[(protomaps, gebco)] = Sources(protomaps, gebco)
        summary["depth"], pieces = check_depth_geography(depth, _SOURCES[(protomaps, gebco)], x, y)
    summary["bytes"] = c["bytes"]
    return f"{x}/{y}", summary, pieces


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="resource root containing manifest.json")
    parser.add_argument("--protomaps", type=Path, help="the Protomaps build used for the release")
    parser.add_argument("--gebco", type=Path, help="the GEBCO netCDF used for the release")
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args()
    print(json.dumps(audit(args.root, args.protomaps, args.gebco, args.jobs), indent=2, sort_keys=True,
                     ensure_ascii=False))
