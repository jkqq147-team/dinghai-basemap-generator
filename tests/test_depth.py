# SPDX-License-Identifier: LGPL-3.0-only
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import netCDF4
import numpy as np
import shapefile
import shapely

import depth_product as dp

FIXTURES = Path(__file__).parent / "fixtures"


def land_index(root, polygons):
    """OSM-style land polygons (z12 world px) written as a lon/lat Shapefile and cut by the
    real index command, so tests read land through the same boundary as a release."""
    shp = root / "land_polygons.shp"
    with shapefile.Writer(str(shp.with_suffix("")), shapeType=shapefile.POLYGON) as writer:
        writer.field("FID", "N", size=11)
        for fid, polygon in enumerate(polygons):
            ring = np.asarray(polygon.exterior.coords)
            writer.poly([list(zip(dp.lon_of_px(ring[:, 0]), dp.lat_of_px(ring[:, 1])))])
            writer.record(fid)
    dp.build_index(shp, root / "land.sqlite", log=lambda *_: None)
    return dp.open_index(root / "land.sqlite")


class FragmentTests(unittest.TestCase):
    """A depth fragment across the 214/215 seam is judged by its whole area in both cells."""

    def test_complete_fragment_one_sixteenth_pixel_inside_seam_is_dropped(self):
        # Review's GEBCO source catches fractional extents missed by integer-border tests:
        # the complete 280829 m² strip touches only 215/106, not its western neighbour.
        with tempfile.TemporaryDirectory() as temporary:
            index = land_index(Path(temporary), [])
            regions = dp.build_cell(index, dp.Gebco(FIXTURES / "gebco-fractional.nc"), 215, 106)
        self.assertFalse(regions, "a complete sub-km² component must produce no depth")

    def _keep_all(self, bump):
        return self.build(bump, {(214, 106): 0, (215, 106): 0})

    def build(self, bump, patch=None):
        patch = patch or {}
        seam = 215 * 4096
        with tempfile.TemporaryDirectory() as temporary:
            index, gebco = _synthetic_two_cells(Path(temporary), seam, bump)
            grid = dp.Gebco(gebco)
            out = {}
            for cell in ((214, 106), (215, 106)):
                with mock.patch.object(dp, "MIN_FRAGMENT_M2", patch.get(cell, dp.MIN_FRAGMENT_M2)):
                    out[cell] = dp.build_cell(index, grid, *cell)
        return out

    @staticmethod
    def reviewer_window_pooled(cx):
        """The reviewer's probe: a 189 km2 lake in 214 and a 3-node channel into 215."""
        w, c, cy = dp.FRAGMENT_WINDOW_PX, dp.CELL_PX, 106
        seam, y0 = 215 * c, cy * c + 1000

        def field(gx, gy):
            x, y = np.meshgrid(gx, gy)
            lake = ((x >= seam - 2000) & (x <= seam - 300)
                    & (y >= y0) & (y <= y0 + 100))
            channel = ((x >= seam - 300) & (x <= seam + 100)
                       & (y >= y0 + 50) & (y <= y0 + 52))
            return np.where(lake | channel, 25.0, 0.0).astype(np.float32)

        out = {}
        for z in range(8, 13):
            s = 1 << (12 - z)
            n = (c + 2 * w) // s + 1
            gx = cx * c - w + np.arange(n) * s
            gy = cy * c - w + np.arange(n) * s
            out[z] = (field(gx, gy) if z == 12 else np.zeros((n, n), np.float32),
                      np.full((n, n), 1000.0, np.float32))
        return out

    @staticmethod
    def border_pooled(cx, cy, zoom, side, offset=0):
        """A small ribbon ending exactly on a window border, or one node inside it."""
        w, c = dp.FRAGMENT_WINDOW_PX, dp.CELL_PX
        s = 1 << (12 - zoom)
        seam = (215 if side in ("E", "W") else 107) * c
        thickness = 2 * s if zoom == 12 else s
        if side in ("E", "W"):
            y0 = cy * c + 1000
            if side == "E":
                x0, x1 = seam - 6 * s, seam + w + offset * s
            else:
                x0, x1 = seam - w - offset * s, seam + 6 * s
            y1 = y0 + thickness
        else:
            x0, x1 = cx * c + 1000, cx * c + 1000 + thickness
            if side == "N":
                y0, y1 = seam - 6 * s, seam + w + offset * s
            else:
                y0, y1 = seam - w - offset * s, seam + 6 * s

        def field(gx, gy):
            x, y = np.meshgrid(gx, gy)
            ribbon = (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)
            return np.where(ribbon, 25.0, 0.0).astype(np.float32)

        out = {}
        for z in range(8, 13):
            stride = 1 << (12 - z)
            n = (c + 2 * w) // stride + 1
            gx = cx * c - w + np.arange(n) * stride
            gy = cy * c - w + np.arange(n) * stride
            out[z] = (field(gx, gy) if z == zoom else np.zeros((n, n), np.float32),
                      np.full((n, n), 1000.0, np.float32))
        return out

    def test_border_exact_fragments_have_identical_decisions_on_both_sides(self):
        # Regression for the inclusive-window boundary bug. Exact-border fragments are
        # potentially truncated and must be kept by both cells; one-node-inside controls
        # are complete and small enough to be dropped by both. Includes a z11 lattice.
        cases = [("E", 12, ((214, 106), (215, 106))),
                 ("W", 12, ((214, 106), (215, 106))),
                 ("N", 12, ((214, 106), (214, 107))),
                 ("S", 12, ((214, 106), (214, 107))),
                 ("E", 11, ((214, 106), (215, 106)))]
        for side, zoom, cells in cases:
            with self.subTest(side=side, zoom=zoom, offset=0):
                drawn = [any(z == zoom for z, _ in dp.depth_regions(self.border_pooled(*cell, zoom, side), *cell))
                         for cell in cells]
                self.assertTrue(all(drawn), (side, zoom, drawn))
            with self.subTest(side=side, zoom=zoom, offset=-1):
                drawn = [any(z == zoom for z, _ in dp.depth_regions(self.border_pooled(*cell, zoom, side, -1), *cell))
                         for cell in cells]
                self.assertEqual(drawn, [False, False], (side, zoom, drawn))

    def test_window_border_component_is_kept_in_both_cells(self):
        # Regression: a thin neck from a large fragment outside 215's window reached its
        # border; treating that truncated component as whole dropped 215's 100 px part.
        for cx, tx in ((214, 3439), (215, 3440)):
            region = dp.depth_regions(self.reviewer_window_pooled(cx), cx, 106).get((12, 20))
            column = shapely.box(tx * 256, 0, (tx + 1) * 256, dp.WORLD_PX)
            self.assertTrue(region is not None and region.intersection(column).area > 0, cx)

    @staticmethod
    def areas(regions, z):
        """Ground areas of the generated polygons, independent of the producer's area rule."""
        out = {}
        for cell, cell_regions in regions.items():
            region = cell_regions[(z, 20)]
            ymin, ymax = region.bounds[1], region.bounds[3]
            mpp = min(2 * math.pi * 6378137 / (256 << 12)
                      / math.cosh(math.pi * (1 - 2 * gy / (256 << 12))) for gy in (ymin, ymax))
            out[cell] = region.area * mpp ** 2
        return out

    def test_small_fragment_across_the_seam_is_dropped_by_both_cells(self):
        # Regression: a piece touching a cell edge used to be kept regardless of area (a 29 m2
        # piece in the Hong Kong sample); 2x2 GEBCO nodes make 0.65 km2 in total at z12.
        for cell, regions in self.build((2, 2)).items():
            self.assertFalse(regions, cell)
        areas = self.areas(self._keep_all((2, 2)), 12)  # fragment rule off: the fragment as drawn
        self.assertLess(sum(areas.values()), 1_000_000)
        self.assertTrue(all(a > 0 for a in areas.values()), "the fragment must reach both cells")

    def test_fragment_of_1_km2_only_together_is_kept_by_both_cells(self):
        # 4x3 nodes at z10: each side < 1 km2, together >= 1 km2; a per-cell area rule drops both.
        areas = list(self.areas(self.build((4, 3)), 10).values())
        self.assertTrue(all(0 < a < 1_000_000 for a in areas), areas)
        self.assertGreaterEqual(sum(areas), 1_000_000)


class ContourTests(unittest.TestCase):
    def test_snapped_neck_does_not_leave_a_complete_sub_km2_fragment(self):
        # Existing seam/window tests have no collapsing neck: rounding must not leave
        # a small orphan after its large parent passed the shared-window area rule.
        cx, cy, zoom = 222, 135, 9
        pooled = {}
        for z in range(8, 13):
            stride = 1 << (12 - z)
            n = (dp.CELL_PX + 2 * dp.FRAGMENT_WINDOW_PX) // stride + 1
            depth = np.zeros((n, n), np.float32)
            if z == zoom:
                depth[200:220, 200:220] = 40
                depth[209:212, 230:233] = 40
                depth[210, 220:230] = 20.01
            pooled[z] = depth, np.full_like(depth, 1000)
        region = dp.depth_regions(pooled, cx, cy)[(zoom, 20)]
        origin = np.array([cx, cy]) * dp.CELL_PX - dp.FRAGMENT_WINDOW_PX
        self.assertTrue(region.covers(shapely.Point(*(origin + np.array([210, 210]) * 8))))
        self.assertFalse(region.covers(shapely.Point(*(origin + np.array([231, 210]) * 8))))

    def test_pit_whose_rounded_hole_touches_the_shell_stays_a_hole(self):
        # Regression (Hong Kong sample): after rounding, a pit's hole ring shares a stretch
        # of the shell; Polygon(shell, holes) + make_valid then returned the shell without
        # the hole, colouring the pit. The pit is 2 nodes tall, 0.01 inside the shell edge.
        xs = ys = np.arange(5) * 16.0
        values = np.ones((5, 5))
        values[:, 0] = -1
        values[1:3, 1] = 0.01
        values[1:3, 2] = -1
        shell_only = values.copy()
        shell_only[1:3, 2] = 1
        region = dp.contour_region(xs, ys, values)
        self.assertFalse(region.contains(shapely.Point(32, 24)))
        self.assertLess(region.area, dp.contour_region(xs, ys, shell_only).area)


class FragmentPropertyTests(unittest.TestCase):
    """Brute-force keep/drop agreement for independent small shapes around cell seams."""

    @staticmethod
    def cases():
        w, c = dp.FRAGMENT_WINDOW_PX, dp.CELL_PX
        sx, sy = 215 * c, 107 * c
        result = []
        for zoom in (12, 11):
            s = 1 << (12 - zoom)
            thickness = 2 * s if zoom == 12 else s
            for axis in ("x", "y"):
                for offset in (-w - 1, -w, -1, 0, 1, w - 1, w, w + 1):
                    for length in (4, 256):
                        if axis == "x":
                            x0, x1 = sx + offset * s, sx + (offset + length) * s
                            y0, y1 = 106 * c + 1000, 106 * c + 1000 + thickness
                        else:
                            x0, x1 = 214 * c + 1000, 214 * c + 1000 + thickness
                            y0, y1 = sy + offset * s, sy + (offset + length) * s
                        result.append((shapely.box(x0, y0, x1, y1), zoom,
                                       f"{axis}-offset{offset}-length{length}-z{zoom}"))
            for slope in (1, -1):
                for shift in (-w, 0, w):
                    for length in (16, 64):
                        cx, cy = sx + shift * s, sy
                        if slope == 1:
                            line = shapely.LineString([(cx - length * s, cy - length * s),
                                                       (cx + length * s, cy + length * s)])
                        else:
                            line = shapely.LineString([(cx - length * s, cy + length * s),
                                                       (cx + length * s, cy - length * s)])
                        result.append((line.buffer(0.75 * s, cap_style=2), zoom,
                                       f"corner-slope{slope}-shift{shift}-length{length}-z{zoom}"))
        return result

    @staticmethod
    def touched_cells(shape):
        """Closed cell extents, independently of the producer's component predicate."""
        minx, miny, maxx, maxy = shape.bounds
        cells = []
        for cx in range(math.floor(minx / dp.CELL_PX) - 1, math.floor(maxx / dp.CELL_PX) + 2):
            for cy in range(math.floor(miny / dp.CELL_PX) - 1, math.floor(maxy / dp.CELL_PX) + 2):
                if shapely.box(cx * dp.CELL_PX, cy * dp.CELL_PX,
                               (cx + 1) * dp.CELL_PX, (cy + 1) * dp.CELL_PX).intersects(shape):
                    cells.append((cx, cy))
        return cells

    @staticmethod
    def build_shape(shape, zoom, cell):
        w, c = dp.FRAGMENT_WINDOW_PX, dp.CELL_PX
        cx, cy = cell
        out = {}
        for z in range(8, 13):
            stride = 1 << (12 - z)
            n = (c + 2 * w) // stride + 1
            gx = cx * c - w + np.arange(n) * stride
            gy = cy * c - w + np.arange(n) * stride
            x, y = np.meshgrid(gx, gy)
            values = (shapely.intersects_xy(shape, x, y).astype(np.float32) * 25
                      if z == zoom else np.zeros((n, n), np.float32))
            out[z] = (values, np.full((n, n), 1000.0, np.float32))
        return dp.depth_regions(out, cx, cy)

    def test_88_shapes_have_cell_independent_keep_decisions(self):
        # Independent closed-cell geometry determines touched cells and the small/complete
        # cases. The old seam-start and corner predicates disagree on some of these cases.
        cases = self.cases()
        self.assertEqual(len(cases), 88)
        for shape, zoom, label in cases:
            cells = self.touched_cells(shape)
            self.assertTrue(cells, label)
            drawn = {cell: any(z == zoom for z, _ in self.build_shape(shape, zoom, cell))
                     for cell in cells}
            decision = next(iter(drawn.values()))
            with self.subTest(shape=label, cells=cells):
                self.assertTrue(all(drawn[cell] == decision for cell in cells),
                                (label, drawn))
            minx, miny, maxx, maxy = shape.bounds
            strictly_inside = all(
                minx > cx * dp.CELL_PX - dp.FRAGMENT_WINDOW_PX
                and miny > cy * dp.CELL_PX - dp.FRAGMENT_WINDOW_PX
                and maxx < (cx + 1) * dp.CELL_PX + dp.FRAGMENT_WINDOW_PX
                and maxy < (cy + 1) * dp.CELL_PX + dp.FRAGMENT_WINDOW_PX
                for cx, cy in cells)
            area = shape.area * float(dp.mpp_of_px(np.array([(miny + maxy) / 2]))[0]) ** 2
            if strictly_inside and area < 500_000:
                with self.subTest(shape=label, small_complete=True):
                    self.assertFalse(any(drawn.values()), (label, area, drawn))


class SeamTests(unittest.TestCase):
    def test_uniform_cells_skip_contouring_with_the_regions_contouring_draws(self):
        # Most of the 65,536 cells are open ocean or dry land: the shortcut must give exactly
        # what the full field/contour path gives, and must not apply near land.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index, gebco = _synthetic_two_cells(root, 215 * dp.CELL_PX)
            empty = land_index(root / "empty", [])
            for elevation, land, uniform in ((-1000, empty, True), (100, index, True), (-1000, index, False)):
                flat = root / f"gebco{elevation}.nc"
                if not flat.exists():
                    shutil.copyfile(gebco, flat)
                    with netCDF4.Dataset(flat, "a") as ds:
                        ds["elevation"][:] = elevation
                grid = dp.Gebco(flat)
                with self.subTest(elevation=elevation, land=land is index):
                    shortcut = dp.uniform_regions(land, grid, 214, 106)
                    self.assertEqual(shortcut is not None, uniform)
                    if uniform:
                        full = dp.depth_regions(dp.pooled_fields(*dp.depth_fields(land, grid, 214, 106)), 214, 106)
                        self.assertEqual(set(shortcut), set(full))
                        self.assertTrue(all(shortcut[key].equals(full[key]) for key in full))

    def test_bands_stay_offshore_of_osm_land(self):
        # The offshore mask now comes from OSM land polygons through the land index: a lost,
        # shifted or flipped land piece would colour water within 1.5 km of the island.
        seam = 215 * dp.CELL_PX
        with tempfile.TemporaryDirectory() as temporary:
            index, gebco = _synthetic_two_cells(Path(temporary), seam)
            regions = dp.build_cell(index, dp.Gebco(gebco), 214, 106)
        island = _island(seam)
        mpp = float(dp.mpp_of_px(island.centroid.y))
        for zoom in range(8, 13):
            with self.subTest(zoom=zoom):
                region = regions[(zoom, 20)]
                self.assertGreaterEqual(region.distance(island) * mpp, dp.OFFSHORE_M)

    def test_original_gebco_shallow_node_between_z12_nodes_is_not_coloured(self):
        # Existing smooth-grid coverage misses the resampling regression: a 4 m knot
        # surrounded by 1000 m nodes can be shallower than every integer z12 sample.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index, gebco = _synthetic_two_cells(root, 215 * dp.CELL_PX)
            with netCDF4.Dataset(gebco, "a") as ds:
                lat, lon = ds["lat"][:], ds["lon"][:]
                xs = (lon + 180) / 360 * (256 << 12)
                ys = (1 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2 * (256 << 12)
                cols = np.flatnonzero((lon > 121) & (lon < 121.5))
                rows = np.flatnonzero((lat > 29) & (lat < 29.3))
                j = cols[np.argmax(np.abs(xs[cols] - np.round(xs[cols])))]
                i = rows[np.argmax(np.abs(ys[rows] - np.round(ys[rows])))]
                ds["elevation"][:] = -1000
                ds["elevation"][i, j] = -4
                point = shapely.Point(float(xs[j]), float(ys[i]))
            regions = dp.build_cell(index, dp.Gebco(gebco), 214, 106)
        for zoom in range(8, 13):
            with self.subTest(zoom=zoom):
                region = regions[(zoom, 20)]
                self.assertGreater(region.area, 0)
                self.assertFalse(region.covers(point))

    def test_adjacent_cells_agree_on_depth_at_the_seam(self):
        # Each cell is generated alone; land and depth near the shared edge must come out the
        # same from both sides at every zoom (neighbour land in the offshore distance, no
        # GEBCO edge truncation, bands clipped exactly at the cell edge).
        a, b = (214, 106), (215, 106)
        seam = 215 * 4096
        with tempfile.TemporaryDirectory() as temporary:
            index, gebco = _synthetic_two_cells(Path(temporary), seam)
            grid = dp.Gebco(gebco)
            regions = {cell: dp.build_cell(index, grid, *cell) for cell in (a, b)}
        line = shapely.LineString([(seam, 106 * 4096), (seam, 107 * 4096)])
        crossing = 0
        for z in range(8, 13):
            for band in dp.DEPTH_BANDS_M:
                cuts = []
                for cell in (a, b):
                    region = regions[cell].get((z, band), shapely.MultiPolygon())
                    cuts.append(region.boundary.union(region).intersection(line))
                self.assertAlmostEqual(cuts[0].symmetric_difference(cuts[1]).length, 0, places=6, msg=(z, band))
                crossing += cuts[0].length > 0
        self.assertGreaterEqual(crossing, 10, "fixture must put several bands across the seam")


def _island(seam):
    return shapely.box(seam - 70, 106 * 4096 + 2000, seam + 50, 106 * 4096 + 2110)


def _synthetic_two_cells(root, seam, bump=None):
    """One island straddling the seam of cells 214/106 and 215/106, and a GEBCO grid whose
    depth rises northward with a wobble, so bands and the offshore hole cross the seam.
    With bump=(columns, rows): a 10 m sea instead, with one 40 m block of GEBCO nodes at
    29.3 N whose columns are split evenly around the first node east of the seam."""
    index = land_index(root, [_island(seam)])
    lat = (np.arange(int((28.2 + 90) * 240), int((29.7 + 90) * 240)) + 0.5) / 240 - 90
    lon = (np.arange(int((120.7 + 180) * 240), int((122.5 + 180) * 240)) + 0.5) / 240 - 180
    la, lo = np.meshgrid(lat, lon, indexing="ij")
    depth = 10 + 160 * (la - 28.3) + 25 * np.sin((lo - 121) * 9) * np.cos((la - 28.3) * 7)
    if bump:
        columns, rows = bump
        depth = np.full(la.shape, 10.0)
        j, i = np.searchsorted(lon, float(dp.lon_of_px(seam))), np.searchsorted(lat, 29.3)
        depth[i:i + rows, j - columns // 2:j + columns // 2] = 40.0
    gebco = root / "gebco.nc"
    ds = netCDF4.Dataset(gebco, "w")
    ds.createDimension("lat", len(lat))
    ds.createDimension("lon", len(lon))
    ds.createVariable("lat", "f8", ("lat",))[:] = lat
    ds.createVariable("lon", "f8", ("lon",))[:] = lon
    ds.createVariable("elevation", "i2", ("lat", "lon"))[:] = np.round(-depth).astype(np.int16)
    ds.close()
    return index, gebco


if __name__ == "__main__":
    unittest.main()
