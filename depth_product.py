#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Illustrative GEBCO depth bands, an input of the DingHai Seamap Planetiler compile.

index: OSM land polygons (land_polygons.shp) -> land index: the polygons cut into z8-cell
       pieces in z12 world pixels, read back as each cell's offshore-distance land mask.
build: GEBCO 15" netCDF + land index -> depth Shapefiles (one per z8 column): for every
       selected z8 cell, zoom 8-12 and band, one EPSG:4326 polygon record (fields min,
       zoom) clipped to the cell.
Development-side only: it reads local files, never the network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import os
import resource
import sqlite3
import sys
import time
from pathlib import Path

import contourpy
import netCDF4
import numpy as np
import shapefile
import shapely
from PIL import Image, ImageDraw
from scipy import ndimage
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union
from shapely.validation import make_valid

VERSION = "seamap-depth-1"
GRID_ZOOM = 8
MIN_ZOOM, MAX_ZOOM = 8, 12
EXTENT = 4096

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
CELLS = 1 << GRID_ZOOM
EARTH_CIRCUMFERENCE_M = 2 * math.pi * 6378137.0
LAT_LIMIT = 85.0511287798066
POOL_PAD = (1 << (FULL_ZOOM - MIN_ZOOM)) + 2
# Fragments are judged on a window reaching this far beyond the cell (z12 px, one z12
# tile, ~8.6 km at 28 N); the same window rule holds in every cell, see depth_regions.
FRAGMENT_WINDOW_PX = 256
WINDOW_PAD = FRAGMENT_WINDOW_PX + POOL_PAD

GEBCO_DOI = "10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa"
GEBCO_ATTRIBUTION = ("GEBCO Bathymetric Compilation Group 2026 (2026). The GEBCO_2026 Grid - a continuous "
                     "terrain model for oceans and land at 15 arc-second intervals. doi:" + GEBCO_DOI)
GEBCO_TERMS_URL = "https://www.gebco.net/data-products/gridded-bathymetry/terms-of-use"
DEPTH_NOTICE = ("示意水深：由 GEBCO_2026 网格插值分层得到，非测量，不可用于导航。"
                "Illustrative depth bands interpolated from the GEBCO_2026 Grid; not a survey; "
                "not for navigation.")

SHAPEFILE_PRJ = ('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137,298.257223563]],'
                 'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]]')


def generator_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def params() -> dict:
    return {"bandsMeters": list(DEPTH_BANDS_M), "offshoreMeters": OFFSHORE_M,
            "minimumAreaSquareMeters": MIN_FRAGMENT_M2, "fragmentWindowPixels": FRAGMENT_WINDOW_PX,
            "depthMarginMeters": DEPTH_MARGIN_M,
            "distanceMarginPixels": DISTANCE_MARGIN_PX, "distanceCapMeters": DISTANCE_CAP_M}


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 24):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- geometry helpers

def lat_of_px(gy):
    """Latitude (deg) of z12 world-pixel row coordinate(s) (pixel corners)."""
    return np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * np.asarray(gy, dtype=np.float64) / WORLD_PX))))


def lon_of_px(gx):
    return np.asarray(gx, dtype=np.float64) / WORLD_PX * 360.0 - 180.0


def px_of_lonlat(lon, lat):
    lat = np.radians(np.clip(np.asarray(lat, dtype=np.float64), -LAT_LIMIT, LAT_LIMIT))
    x = (np.asarray(lon, dtype=np.float64) + 180.0) / 360.0 * WORLD_PX
    return x, (1 - np.arcsinh(np.tan(lat)) / np.pi) / 2 * WORLD_PX


def mpp_of_px(gy):
    """Ground metres per z12 world pixel at row coordinate(s) gy."""
    return EARTH_CIRCUMFERENCE_M / WORLD_PX * np.cos(np.radians(lat_of_px(gy)))


def poleward_shift_px(gy):
    """Rows within which every land pixel that can affect a capped distance lies."""
    return np.ceil(1.25 * (OFFSHORE_M + DISTANCE_CAP_M) / mpp_of_px(gy)) + 4


def conservative_mpp(gy):
    q = poleward_shift_px(gy)
    return np.minimum(mpp_of_px(gy - q), mpp_of_px(gy + q))


def _polygonal(g):
    if g.is_empty:
        return []
    if g.geom_type == "Polygon":
        return [g]
    if g.geom_type in ("MultiPolygon", "GeometryCollection"):
        return [p for part in g.geoms for p in _polygonal(part)]
    return []


# ---------------------------------------------------------------- land index

def _cell_pieces(geometry):
    """(x, y, piece) for every z8 cell the geometry (z12 world px) reaches, by halving."""
    minx, miny, maxx, maxy = geometry.bounds
    x0, y0 = max(0, int(minx // CELL_PX)), max(0, int(miny // CELL_PX))
    x1 = min(CELLS - 1, max(x0, math.ceil(maxx / CELL_PX) - 1))
    y1 = min(CELLS - 1, max(y0, math.ceil(maxy / CELL_PX) - 1))
    if x0 == x1 and y0 == y1:
        yield x0, y0, geometry
        return
    if x1 - x0 >= y1 - y0:
        mid = (x0 + x1 + 1) // 2 * CELL_PX
        boxes = ((minx, miny, mid, maxy), (mid, miny, maxx, maxy))
    else:
        mid = (y0 + y1 + 1) // 2 * CELL_PX
        boxes = ((minx, miny, maxx, mid), (minx, mid, maxx, maxy))
    for box in boxes:
        part = shapely.MultiPolygon(_polygonal(shapely.clip_by_rect(geometry, *box)))
        if not part.is_empty:
            yield from _cell_pieces(part)


def build_index(land_shp, output, log=print):
    """Cuts OSM land polygons into z8-cell pieces (z12 world px) in an SQLite file."""
    output = Path(output)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.unlink(missing_ok=True)
    db = sqlite3.connect(temporary)
    db.execute("CREATE TABLE land (cell INTEGER NOT NULL, wkb BLOB NOT NULL)")
    db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    rows, polygons, pieces = [], 0, 0
    with shapefile.Reader(str(land_shp)) as reader:
        for shape in reader.iterShapes():
            points = np.asarray(shape.points, dtype=np.float64)
            x, y = px_of_lonlat(points[:, 0], points[:, 1])
            xy = np.column_stack([x, y])
            bounds = list(shape.parts) + [len(points)]
            rings = [xy[a:b] for a, b in zip(bounds, bounds[1:])]
            polygons += 1
            for cx, cy, piece in _cell_pieces(Polygon(rings[0], rings[1:])):
                rows.append((cx * CELLS + cy, shapely.to_wkb(piece)))
            if len(rows) >= 10000:
                pieces += len(rows)
                db.executemany("INSERT INTO land VALUES (?, ?)", rows)
                rows.clear()
    pieces += len(rows)
    db.executemany("INSERT INTO land VALUES (?, ?)", rows)
    db.execute("CREATE INDEX land_cell ON land (cell)")
    source = {"file": Path(land_shp).name, "bytes": Path(land_shp).stat().st_size,
              "sha256": file_sha256(land_shp), "polygons": polygons, "pieces": pieces,
              "indexGeneratorSHA256": generator_sha256()}
    db.execute("INSERT INTO metadata VALUES ('source', ?)", (json.dumps(source, sort_keys=True),))
    db.commit()
    db.close()
    os.replace(temporary, output)
    log(json.dumps(source))
    return source


def open_index(path):
    return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)


def index_source(index):
    return json.loads(index.execute("SELECT value FROM metadata WHERE key = 'source'").fetchone()[0])


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

    def _nodes(self, i0, i1, j0, j1):
        """Depth (m, positive below sea level) of grid nodes i0..i1 x j0..j1, NaN outside coverage."""
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
        return -block

    def depth(self, lats, lons):
        """Bilinear depth (m, positive below GEBCO sea level), float32 [len(lats), len(lons)].
        NaN where the grid has no coverage."""
        fi = (np.asarray(lats) - self.lat0) * 240
        fj = (np.asarray(lons) - self.lon0) * 240
        i = np.floor(fi).astype(np.int64)
        j = np.floor(fj).astype(np.int64)
        wi = (fi - i).astype(np.float32)
        wj = (fj - j).astype(np.float32)
        i0, j0 = int(i.min()), int(j.min())
        block = self._nodes(i0, int(i.max()) + 1, j0, int(j.max()) + 1)
        c = block[:, j - j0] * (1 - wj) + block[:, j + 1 - j0] * wj
        return c[i - i0] * (1 - wi)[:, None] + c[i + 1 - i0] * wi[:, None]

    def node_range(self, lats, lons):
        """(minimum, maximum) depth of the nodes enclosing the lat/lon box; every bilinear
        value inside lies between them. The minimum is NaN where coverage is missing."""
        i = np.floor((np.asarray(lats) - self.lat0) * 240).astype(np.int64)
        j = np.floor((np.asarray(lons) - self.lon0) * 240).astype(np.int64)
        block = self._nodes(int(i.min()), int(i.max()) + 1, int(j.min()), int(j.max()) + 1)
        return float(block.min()), float(np.where(np.isnan(block), -np.inf, block).max())

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


def land_raster(index, cx, cy, rings_count):
    """Boolean land raster (row 0 north). Pixel (r, c) is the z12 world node (oy + r, ox + c).
    The rings stay within one cell, so the 3x3 neighbouring cells' pieces cover it."""
    n = 1 << (FULL_ZOOM - GRID_ZOOM)
    ox, oy = (cx * n - rings_count) * 256, (cy * n - rings_count) * 256
    side = (n + 2 * rings_count) * 256
    land = np.zeros((side, side), dtype=bool)
    outlines = Image.new("1", (side, side), 0)
    draw = ImageDraw.Draw(outlines)
    for dy in (-1, 0, 1):
        y = cy + dy
        if not 0 <= y < CELLS:
            continue
        for dx in (-1, 0, 1):
            x = (cx + dx) % CELLS
            shift = np.array([(cx + dx - x) * WORLD_PX - ox + 0.5, -oy + 0.5])
            for (wkb,) in index.execute("SELECT wkb FROM land WHERE cell = ?", (x * CELLS + y,)):
                for polygon in _polygonal(shapely.from_wkb(wkb)):
                    rings = [np.asarray(polygon.exterior.coords)[:, :2] + shift] + \
                        [np.asarray(ring.coords)[:, :2] + shift for ring in polygon.interiors]
                    c0, r0 = np.maximum(np.floor(rings[0].min(axis=0)).astype(int), 0)
                    c1, r1 = np.minimum(np.floor(rings[0].max(axis=0)).astype(int) + 1, side)
                    if c0 >= c1 or r0 >= r1:
                        continue
                    # Each polygon is filled alone (holes cleared) and then added, so an
                    # island inside another polygon's hole stays land.
                    mask = Image.new("1", (int(c1 - c0), int(r1 - r0)), 0)
                    fill = ImageDraw.Draw(mask)
                    for k, ring in enumerate(rings):
                        if len(ring) >= 3:
                            fill.polygon((ring - (c0, r0)).ravel().tolist(), fill=0 if k else 1)
                        draw.line(ring.ravel().tolist(), fill=1, width=1)
                    land[r0:r1, c0:c1] |= np.asarray(mask, dtype=bool)
    return land | np.asarray(outlines, dtype=bool), ox, oy


def depth_fields(index, gebco, cx, cy):
    """Exact depth minima on full-resolution pixel rectangles, and distance on nodes.
    Both start at (cx*4096 - WINDOW_PAD, cy*4096 - WINDOW_PAD); the rectangle
    field has one fewer row/column than the node field."""
    land, ox, oy = land_raster(index, cx, cy, land_raster_rings(cy))
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


def depth_regions(pooled, cx, cy):
    """{(z, band): MultiPolygon} in z12 world px, clipped to the cell.

    Vertices sit on the zoom's 4096-per-tile grid (1/16 of a lattice step), so the
    compiler's own rounding keeps them. Judge connected vector components after final
    snapping on the shared fragment window, before clipping to the cell. A component
    is area-filtered only if it is complete in every touched cell's window."""
    out = {}
    for z in range(MIN_ZOOM, MAX_ZOOM + 1):
        s = 1 << (FULL_ZOOM - z)
        pooled_depth, pooled_distance = pooled.pop(z)
        pad = FRAGMENT_WINDOW_PX // s          # lattice nodes before the cell's first node
        xs = (np.arange(pooled_depth.shape[1]) - pad) * 16.0
        ys = (np.arange(pooled_depth.shape[0]) - pad) * 16.0
        n = 1 << (z - GRID_ZOOM)
        cell = shapely.box(0, 0, n * EXTENT, n * EXTENT)
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
            pieces = _polygonal(unary_union(kept).intersection(cell)) if kept else []
            if pieces:
                out[(z, band)] = affinity.affine_transform(
                    MultiPolygon(pieces), [s / 16, 0, 0, s / 16, cx * CELL_PX, cy * CELL_PX])
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
    # Snap-round to the integer grid so the compiler's own rounding finds valid geometry
    # and never repairs depth ring by ring (moves edges by < 1 unit).
    polygons = [q for p in polygons for q in _polygonal(shapely.set_precision(p, 1.0)) if q.area > 0]
    if not polygons:
        return None
    return polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)


def uniform_regions(index, gebco, cx, cy):
    """The cell's regions when no band needs contouring, else None.

    Nodes no deeper than the first band (+ margin) colour nothing. Nodes all deeper than
    the last band (+ margin), with no land piece in the 3x3 cells around (distance at its
    cap), fill every band at every zoom: exactly what depth_regions draws for those fields."""
    edges = np.array([-WINDOW_PAD, CELL_PX + WINDOW_PAD])
    shallowest, deepest = gebco.node_range(lat_of_px(cy * CELL_PX + edges), lon_of_px(cx * CELL_PX + edges))
    if deepest <= DEPTH_BANDS_M[0] + DEPTH_MARGIN_M:
        return {}
    near = [((cx + dx) % CELLS) * CELLS + cy + dy for dx in (-1, 0, 1) for dy in (-1, 0, 1) if 0 <= cy + dy < CELLS]
    if shallowest > DEPTH_BANDS_M[-1] + DEPTH_MARGIN_M and not index.execute(
            f"SELECT 1 FROM land WHERE cell IN ({','.join('?' * len(near))}) LIMIT 1", near).fetchone():
        cell = MultiPolygon([shapely.box(cx * CELL_PX, cy * CELL_PX, (cx + 1) * CELL_PX, (cy + 1) * CELL_PX)])
        return {(z, band): cell for z in range(MIN_ZOOM, MAX_ZOOM + 1) for band in DEPTH_BANDS_M}
    return None


def build_cell(index, gebco, cx, cy):
    """{(z, band): MultiPolygon in z12 world px} of one z8 cell."""
    uniform = uniform_regions(index, gebco, cx, cy)
    if uniform is not None:
        return uniform
    return depth_regions(pooled_fields(*depth_fields(index, gebco, cx, cy)), cx, cy)


# ---------------------------------------------------------------- Shapefile shards
# One Shapefile per z8 column keeps each file far below the format's 2 GB limit and
# makes a finished column the unit of resumption. Shapefiles, not a GeoPackage: the
# pinned Planetiler reads EPSG:4326 GeoPackages latitude-first.

def _column_name(x):
    return f"x{x:03d}"


def _write_column(output, x, rows, cells):
    """rows: [(lon/lat Polygon parts, band, zoom)]; the sidecar JSON marks the column done.
    A column without depth gets only the sidecar (and loses a Shapefile of an earlier selection)."""
    output = Path(output)
    for suffix in (".json", ".shp", ".shx", ".dbf", ".prj"):
        (output / f"{_column_name(x)}{suffix}").unlink(missing_ok=True)
    if rows:
        temporary = output / f".{_column_name(x)}"
        with shapefile.Writer(str(temporary), shapeType=shapefile.POLYGON) as writer:
            writer.field("min", "N", size=4)
            writer.field("zoom", "N", size=2)
            for polygons, band, zoom in rows:
                parts = []
                for polygon in polygons:
                    # Shapefile rings: exteriors clockwise, holes counter-clockwise.
                    polygon = orient(polygon, -1.0)
                    parts += [list(polygon.exterior.coords)] + [list(ring.coords) for ring in polygon.interiors]
                writer.poly(parts)
                writer.record(band, zoom)
        (output / f"{_column_name(x)}.prj").write_text(SHAPEFILE_PRJ)
        for suffix in (".shp", ".shx", ".dbf"):
            os.replace(f"{temporary}{suffix}", output / f"{_column_name(x)}{suffix}")
    (output / f"{_column_name(x)}.json").write_text(json.dumps(sorted(cells)))


def read_identity(output):
    return json.loads((Path(output) / "identity.json").read_text())


def input_identity(gebco_path, gebco_grid, index_path):
    gebco = Gebco(gebco_path)
    return {
        "generatorVersion": VERSION, "generatorSHA256": generator_sha256(), "params": params(),
        "notice": DEPTH_NOTICE,
        "gebco": {"grid": gebco_grid, "doi": GEBCO_DOI, "title": gebco.title, "attribution": GEBCO_ATTRIBUTION,
                  "termsURL": GEBCO_TERMS_URL, "bytes": Path(gebco_path).stat().st_size,
                  "sha256": file_sha256(gebco_path),
                  "coverage": "global" if gebco.global_lon and gebco.nlat == 180 * 240 else "subset"},
        "landPolygons": index_source(open_index(index_path)),
    }


# ---------------------------------------------------------------- build

_WORKER = {}


def _init_worker(index, gebco):
    _WORKER["index"] = open_index(index)
    _WORKER["gebco"] = Gebco(gebco)


def _lonlat(region):
    return shapely.transform(region, lambda xy: np.column_stack([lon_of_px(xy[:, 0]), lat_of_px(xy[:, 1])]))


def _run_column(job):
    x, ys = job
    started = time.monotonic()
    rows = []
    for y in ys:
        for (z, band), region in sorted(build_cell(_WORKER["index"], _WORKER["gebco"], x, y).items()):
            rows.append((list(_lonlat(region).geoms), band, z))
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return x, ys, rows, round(time.monotonic() - started, 3), rss if sys.platform == "darwin" else rss * 1024


def build(gebco, gebco_grid, index, cells, output, jobs=1, log=print):
    """Writes every selected cell's depth into per-column Shapefiles; finished columns are skipped."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    identity = input_identity(gebco, gebco_grid, index)
    if (output / "identity.json").exists() and read_identity(output) != identity:
        raise ValueError(f"{output} was built from other inputs or code; remove it to rebuild")
    (output / "identity.json").write_text(json.dumps(identity, ensure_ascii=False, sort_keys=True))
    columns = {}
    for x, y in cells:
        columns.setdefault(x, set()).add(y)
    todo = []
    for x, ys in sorted(columns.items()):
        done = output / f"{_column_name(x)}.json"
        if not done.exists() or set(json.loads(done.read_text())) != ys:
            todo.append((x, sorted(ys)))
    log(json.dumps({"columns": len(columns), "todo": len(todo)}))
    if todo:
        context = multiprocessing.get_context("spawn")
        with context.Pool(jobs, initializer=_init_worker, initargs=(str(index), str(gebco)),
                          maxtasksperchild=4) as pool:
            for x, ys, rows, seconds, rss in pool.imap_unordered(_run_column, todo):
                _write_column(output, x, rows, ys)
                log(json.dumps({"x": x, "cells": len(ys), "features": len(rows), "seconds": seconds,
                                "peakRSSBytes": rss}))
    return {"columns": len(columns), "cells": len(cells)}


def _cell_arg(text):
    x, y = (int(v) for v in text.split("/"))
    if not (0 <= x < CELLS and 0 <= y < CELLS):
        raise argparse.ArgumentTypeError("cell must be X/Y within the z8 grid")
    return x, y


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    i = sub.add_parser("index", help="cut OSM land polygons into z8-cell pieces")
    i.add_argument("--land", type=Path, required=True, help="extracted land_polygons.shp (complete, EPSG:4326)")
    i.add_argument("--output", type=Path, required=True)
    b = sub.add_parser("build", help="write the selected cells' depth bands as per-column Shapefiles")
    b.add_argument("--gebco", type=Path, required=True, help="GEBCO 15\" netCDF")
    b.add_argument("--gebco-grid", required=True, help="grid name, e.g. GEBCO_2026")
    b.add_argument("--land-index", type=Path, required=True, help="output of the index command")
    b.add_argument("--output", type=Path, required=True, help="depth directory, resumed when it exists")
    selected = b.add_mutually_exclusive_group(required=True)
    selected.add_argument("--cell", type=_cell_arg, action="append", metavar="X/Y")
    selected.add_argument("--all-cells", action="store_true")
    b.add_argument("--jobs", type=int, default=1)
    a = parser.parse_args(argv)
    if a.command == "index":
        build_index(a.land, a.output)
        return
    cells = [(x, y) for x in range(CELLS) for y in range(CELLS)] if a.all_cells else a.cell
    print(json.dumps(build(a.gebco, a.gebco_grid, a.land_index, cells, a.output, a.jobs)))


if __name__ == "__main__":
    main()
