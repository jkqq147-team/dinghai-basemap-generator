# SPDX-License-Identifier: LGPL-3.0-only
import gzip
import hashlib
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mapbox_vector_tile as mvt
import netCDF4
import numpy as np
import shapely
import shapely.affinity
from pmtiles.tile import zxy_to_tileid

import audit_land
import land_product as lp

FIXTURES = Path(__file__).parent / "fixtures"
DACHEN = (214, 106)
SEA = (100, 100)  # no source tiles, no GEBCO coverage: must not produce a file
BLOCK = [(x, y) for x in range(3424, 3428) for y in range(1697, 1701)] + \
    [(x, y) for x in range(3434, 3438) for y in range(1708, 1712)]  # fixture z12 tiles


def build_release(root, cells, release="test"):
    peaks = root / "peaks.pmtiles"
    if not peaks.exists():
        # The fixture keeps both z13 and z14 so the lift is exercised over the whole allowed range.
        lp.make_peaks([FIXTURES / "protomaps-dachen-z13-14.pmtiles"], peaks, zooms=(13, 14), log=lambda *_: None)
    return lp.build(FIXTURES / "protomaps-dachen.pmtiles", "20261025.pmtiles", peaks,
                    FIXTURES / "gebco-dachen.nc", "GEBCO_2026", release, cells, root / "land",
                    root / "state", log=lambda *_: None)


def cell_tiles(path):
    _, meta, tiles = audit_land.read_pmtiles(Path(path).read_bytes())
    return meta, {audit_land._zxy(t): audit_land.decode_tile(gzip.decompress(b)) for t, b in tiles.items()}


class LandCellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.manifest = build_release(cls.root, [DACHEN, SEA])
        cls.cell = cls.root / "land/releases/test/cells/8/214/106.pmtiles"

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_sample_cell_has_only_whitelisted_layers_zooms_and_license_metadata(self):
        # Regression: a dropped layer (buildings/boundaries/pois/earth/water) or a z13+ tile leaking,
        # or a missing licence/attribution key, fails here at the published-file boundary.
        meta, tiles = cell_tiles(self.cell)
        self.assertEqual({z for z, _, _ in tiles}, {8, 9, 10, 11, 12})
        layers = set().union(*(t.keys() for t in tiles.values()))
        self.assertEqual(layers, set(lp.LAYERS))
        self.assertEqual(meta["dinghai:landLicense"], "ODbL-1.0")
        self.assertEqual(meta["dinghai:landLicenseURL"], "https://opendatacommons.org/licenses/odbl/1-0/")
        self.assertEqual(meta["dinghai:landAttribution"], "© OpenStreetMap contributors · Protomaps")
        self.assertIn("doi:10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa", meta["dinghai:depthAttribution"])
        self.assertIn("非测量，不可用于导航", meta["dinghai:depthNotice"])
        self.assertEqual(meta["dinghai:protomapsBuild"], "20261025.pmtiles")
        self.assertEqual(meta["dinghai:gebcoGrid"], "GEBCO_2026")
        self.assertEqual(meta["dinghai:generatorSHA256"], lp.generator_sha256())

    def test_z12_peaks_are_lifted_from_z13_14(self):
        # Protomaps carries almost no peaks at z<=12; losing the z13-14 lift empties this layer.
        _, tiles = cell_tiles(self.cell)
        source = lp.Archive(FIXTURES / "protomaps-dachen.pmtiles")
        own = sum(len(lp.own_peaks(lp.split_layers(source.get(12, x, y)))) for x, y in BLOCK)
        lifted = sum(len(tiles.get((12, x, y), {}).get("peaks", (0, []))[1]) for x, y in BLOCK)
        self.assertGreater(lifted, own + 50)

    def test_depth_meets_20m_and_offshore_1500m_and_audit_rejects_a_closer_build(self):
        # The audit re-derives depth/distance from GEBCO and OSM land itself; a generator that
        # used a smaller offshore distance must be caught, not only one that crashes.
        report = audit_land.audit(self.root / "land", FIXTURES / "protomaps-dachen.pmtiles",
                                  FIXTURES / "gebco-dachen.nc")
        depth = report["cells"]["214/106"]["depth"]
        self.assertGreaterEqual(depth["min_offshore_m"], 1500)
        self.assertGreaterEqual(depth["min_depth_margin_m"], 0)
        self.assertGreater(depth["points"], 1000)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "land"
            shutil.copytree(self.root / "land", root)
            # A release that claims the 1500 m contract but whose cell was cut at 300 m:
            # same manifest/cells.json shape, consistent hashes, so only geography can fail.
            with mock.patch.object(lp, "OFFSHORE_M", 300.0):
                tiles, _ = lp.build_cell(lp.Archive(FIXTURES / "protomaps-dachen.pmtiles"),
                                         lp.Archive(self.root / "peaks.pmtiles"),
                                         lp.Gebco(FIXTURES / "gebco-dachen.nc"), *DACHEN)
            cell = root / "releases/test/cells/8/214/106.pmtiles"
            _, meta, _ = audit_land.read_pmtiles(cell.read_bytes())
            lp.write_cell_pmtiles(cell, tiles, *DACHEN, meta)
            _rehash(root, cell)
            with self.assertRaisesRegex(ValueError, "m from land"):
                audit_land.audit(root, FIXTURES / "protomaps-dachen.pmtiles", FIXTURES / "gebco-dachen.nc")

    def test_release_lists_bytes_and_sha_skips_empty_cells_resumes_and_audit_catches_tampering(self):
        cells = json.loads((self.root / "land/releases/test/cells.json").read_text())
        self.assertEqual([(c["x"], c["y"]) for c in cells], [DACHEN])
        self.assertFalse((self.root / "land/releases/test/cells/8/100").exists())
        self.assertEqual(self.manifest["cellCount"], 1)
        self.assertEqual(self.manifest["cells"], "releases/test/cells.json")
        audit_land.audit(self.root / "land")
        before = self.cell.stat().st_mtime_ns
        build_release(self.root, [DACHEN, SEA])  # resume: nothing rebuilt
        self.assertEqual(self.cell.stat().st_mtime_ns, before)
        for mutate, message in ((lambda root: _flip(root / "land/releases/test/cells/8/214/106.pmtiles"), "sha256"),
                                (lambda root: (root / "land/releases/test/cells/8/1").mkdir(parents=True)
                                 or (root / "land/releases/test/cells/8/1/1.pmtiles").write_bytes(b"x"), "differ")):
            with tempfile.TemporaryDirectory() as temporary:
                copy = Path(temporary) / "copy"
                shutil.copytree(self.root / "land", copy / "land")
                mutate(copy)
                with self.assertRaisesRegex(ValueError, message):
                    audit_land.audit(copy / "land")


def _rehash(root, cell):
    data = cell.read_bytes()
    cells_path = root / "releases/test/cells.json"
    cells = json.loads(cells_path.read_text())
    cells[0].update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    cells_bytes = json.dumps(cells).encode()
    cells_path.write_bytes(cells_bytes)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest.update(cellsBytes=len(cells_bytes), cellsSHA256=hashlib.sha256(cells_bytes).hexdigest(),
                    totalBytes=len(data))
    (root / "manifest.json").write_text(json.dumps(manifest))


def _flip(path):
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(bytes(data))


class FragmentTests(unittest.TestCase):
    """A depth fragment across the 214/215 seam is judged by its whole area in both cells."""

    def test_complete_fragment_one_sixteenth_pixel_inside_seam_is_dropped(self):
        # Review's GEBCO source catches fractional extents missed by integer-border tests:
        # the complete 280829 m² strip touches only 215/106, not its western neighbour.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            protomaps, peaks = root / "protomaps.pmtiles", root / "peaks.pmtiles"
            lp.write_pmtiles(protomaps, {}, {}, (-180, -85, 180, 85), 0, 12)
            lp.write_pmtiles(peaks, {}, {"dinghai:peakZooms": [13]}, (-180, -85, 180, 85), 13, 13)
            tiles, _ = lp.build_cell(lp.Archive(protomaps), lp.Archive(peaks),
                                     lp.Gebco(FIXTURES / "gebco-fractional.nc"), 215, 106)
        self.assertFalse(tiles, "a complete sub-km² component must produce no depth tiles")

    def _keep_all(self, bump):
        return self.build(bump, {(214, 106): 0, (215, 106): 0})

    def build(self, bump, patch=None):
        patch = patch or {}
        seam = 215 * 4096
        with tempfile.TemporaryDirectory() as temporary:
            protomaps, peaks, gebco = _synthetic_two_cells(Path(temporary), seam, bump)
            sources = lp.Archive(protomaps), lp.Archive(peaks), lp.Gebco(gebco)
            out = {}
            for cell in ((214, 106), (215, 106)):
                with mock.patch.object(lp, "MIN_FRAGMENT_M2", patch.get(cell, lp.MIN_FRAGMENT_M2)):
                    tiles, _ = lp.build_cell(*sources, *cell)
                out[cell] = {lp.tileid_to_zxy(t): audit_land.decode_tile(gzip.decompress(v)) for t, v in tiles.items()}
        return out

    @staticmethod
    def reviewer_window_pooled(cx):
        """The reviewer's probe: a 189 km2 lake in 214 and a 3-node channel into 215."""
        w, c, cy = lp.FRAGMENT_WINDOW_PX, lp.CELL_PX, 106
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
        w, c = lp.FRAGMENT_WINDOW_PX, lp.CELL_PX
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
                counts = [sum(1 for k in lp.depth_tiles(self.border_pooled(*cell, zoom, side), *cell)
                              if k[0] == zoom) for cell in cells]
                self.assertTrue(all(counts), (side, zoom, counts))
            with self.subTest(side=side, zoom=zoom, offset=-1):
                counts = [sum(1 for k in lp.depth_tiles(self.border_pooled(*cell, zoom, side, -1), *cell)
                              if k[0] == zoom) for cell in cells]
                self.assertEqual(counts, [0, 0], (side, zoom, counts))

    def test_window_border_component_is_kept_in_both_cells(self):
        # Regression: a thin neck from a large fragment outside 215's window reached its
        # border; treating that truncated component as whole dropped 215's 100 px part.
        tiles = {cx: lp.depth_tiles(self.reviewer_window_pooled(cx), cx, 106)
                 for cx in (214, 215)}
        for cx, tx in ((214, 3439), (215, 3440)):
            self.assertTrue(any(z == 12 and x == tx
                                for z, x, _ in tiles[cx]), (cx, sorted(tiles[cx])))

    def test_audit_rejects_one_sided_shared_edge_depth(self):
        # The reviewer's pre-fix probe kept 214's lake/channel but dropped 215's part.
        # Removing 215's records models that output: its large 214 piece used to pass
        # because the audit only joined pieces and then checked their total area.
        tiles = {cx: lp.depth_tiles(self.reviewer_window_pooled(cx), cx, 106)
                 for cx in (214, 215)}
        records = []
        # Use the generated polygons rather than a hand-made area exemption.
        for cx in (214, 215):
            geoms = [shapely.affinity.affine_transform(f["geometry"], [1 / 16, 0, 0, 1 / 16,
                                                                         x * 256, y * 256])
                     for (z, x, y), fs in tiles[cx].items() if z == 12
                     for f in fs if f["properties"]["min"] == 20]
            region = shapely.union_all(geoms) if geoms else shapely.Polygon()
            records.extend(audit_land.region_pieces(region, 12, 20, cx, 106)[1])
        records = [r for r in records if r["cell"] == (214, 106)]
        with self.assertRaisesRegex(ValueError, "one-sided shared-edge depth"):
            audit_land.check_edge_fragments(records, {(214, 106), (215, 106)})

    @staticmethod
    def pieces(decoded, z):
        """Audit piece records of band 20 at zoom z, per cell."""
        out = {}
        for cell, tiles in decoded.items():
            feats = [(x, y, parts) for (tz, x, y), layers in tiles.items() if tz == z
                     for _, props, parts in layers.get("depth", (0, []))[1] if props["min"] == 20]
            region = shapely.union_all(audit_land.depth_polygons(feats, z)) if feats else shapely.Polygon()
            out[cell] = audit_land.region_pieces(region, z, 20, *cell)[1]
        return out

    def test_small_fragment_across_the_seam_is_dropped_by_both_cells(self):
        # Regression: a piece touching a cell edge used to be kept regardless of area (a 29 m2
        # piece in the Hong Kong sample); 2x2 GEBCO nodes make 0.65 km2 in total at z12.
        decoded = self.build((2, 2))
        for cell, tiles in decoded.items():
            self.assertFalse(any("depth" in layers for layers in tiles.values()), cell)
        everything = self._keep_all((2, 2))  # fragment rule off: the fragment as drawn
        sides = self.pieces(everything, 12)
        self.assertLess(sum(p["m2"] for ps in sides.values() for p in ps), 1_000_000)
        self.assertTrue(all(sides.values()), "the fragment must reach both cells")
        # the audit rejects a release where only one side kept it (the pre-fix behaviour)
        half = {(214, 106): everything[(214, 106)], (215, 106): decoded[(215, 106)]}
        records = [p for ps in self.pieces(half, 12).values() for p in ps]
        with self.assertRaisesRegex(ValueError, "fragment across cells"):
            audit_land.check_edge_fragments(records, {(214, 106), (215, 106)})

    def test_fragment_of_1_km2_only_together_is_kept_by_both_cells(self):
        # 4x3 nodes at z10: each side < 1 km2, together >= 1 km2; a per-cell area rule drops both.
        sides = self.pieces(self.build((4, 3)), 10)
        areas = [sum(p["m2"] for p in ps) for ps in sides.values()]
        self.assertTrue(all(0 < a < 1_000_000 for a in areas), areas)
        self.assertGreaterEqual(sum(areas), 1_000_000)
        self.assertEqual(audit_land.check_edge_fragments(
            [p for ps in sides.values() for p in ps], {(214, 106), (215, 106)})["edgeFragments"], 1)


class ContourTests(unittest.TestCase):
    def test_snapped_neck_does_not_leave_a_complete_sub_km2_fragment(self):
        # Existing seam/window tests have no collapsing neck: rounding must not leave
        # a small orphan after its large parent passed the shared-window area rule.
        cx, cy, zoom = 222, 135, 9
        pooled = {}
        for z in range(8, 13):
            stride = 1 << (12 - z)
            n = (lp.CELL_PX + 2 * lp.FRAGMENT_WINDOW_PX) // stride + 1
            depth = np.zeros((n, n), np.float32)
            if z == zoom:
                depth[200:220, 200:220] = 40
                depth[209:212, 230:233] = 40
                depth[210, 220:230] = 20.01
            pooled[z] = depth, np.full_like(depth, 1000)
        features = lp.depth_tiles(pooled, cx, cy)
        decoded = {key: audit_land.decode_tile(lp.encode_layers(
            [{"name": "depth", "features": fs}], lp.EXTENT)) for key, fs in features.items()}
        parts = [(x, y, rings) for (z, x, y), layers in decoded.items() if z == zoom
                 for _, props, rings in layers["depth"][1] if props["min"] == 20]
        region = shapely.union_all(audit_land.depth_polygons(parts, zoom))
        origin = np.array([cx, cy]) * lp.CELL_PX - lp.FRAGMENT_WINDOW_PX
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
        region = lp.contour_region(xs, ys, values)
        self.assertFalse(region.contains(shapely.Point(32, 24)))
        self.assertLess(region.area, lp.contour_region(xs, ys, shell_only).area)


class FragmentPropertyTests(unittest.TestCase):
    """Brute-force keep/drop agreement for independent small shapes around cell seams."""

    @staticmethod
    def cases():
        w, c = lp.FRAGMENT_WINDOW_PX, lp.CELL_PX
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
        for cx in range(math.floor(minx / lp.CELL_PX) - 1, math.floor(maxx / lp.CELL_PX) + 2):
            for cy in range(math.floor(miny / lp.CELL_PX) - 1, math.floor(maxy / lp.CELL_PX) + 2):
                if shapely.box(cx * lp.CELL_PX, cy * lp.CELL_PX,
                               (cx + 1) * lp.CELL_PX, (cy + 1) * lp.CELL_PX).intersects(shape):
                    cells.append((cx, cy))
        return cells

    @staticmethod
    def build_shape(shape, zoom, cell):
        w, c = lp.FRAGMENT_WINDOW_PX, lp.CELL_PX
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
        return lp.depth_tiles(out, cx, cy)

    def test_88_shapes_have_cell_independent_keep_decisions(self):
        # Independent closed-cell geometry determines touched cells and the small/complete
        # cases. The old seam-start and corner predicates disagree on some of these cases.
        cases = self.cases()
        self.assertEqual(len(cases), 88)
        for shape, zoom, label in cases:
            cells = self.touched_cells(shape)
            self.assertTrue(cells, label)
            drawn = {cell: any(k[0] == zoom for k in self.build_shape(shape, zoom, cell))
                     for cell in cells}
            decision = next(iter(drawn.values()))
            with self.subTest(shape=label, cells=cells):
                self.assertTrue(all(drawn[cell] == decision for cell in cells),
                                (label, drawn))
            minx, miny, maxx, maxy = shape.bounds
            strictly_inside = all(
                minx > cx * lp.CELL_PX - lp.FRAGMENT_WINDOW_PX
                and miny > cy * lp.CELL_PX - lp.FRAGMENT_WINDOW_PX
                and maxx < (cx + 1) * lp.CELL_PX + lp.FRAGMENT_WINDOW_PX
                and maxy < (cy + 1) * lp.CELL_PX + lp.FRAGMENT_WINDOW_PX
                for cx, cy in cells)
            area = shape.area * float(lp.mpp_of_px(np.array([(miny + maxy) / 2]))[0]) ** 2
            if strictly_inside and area < 500_000:
                with self.subTest(shape=label, small_complete=True):
                    self.assertFalse(any(drawn.values()), (label, area, drawn))


class SeamTests(unittest.TestCase):
    def test_original_gebco_shallow_node_between_z12_nodes_is_not_coloured(self):
        # Existing smooth-grid coverage misses the resampling regression: a 4 m knot
        # surrounded by 1000 m nodes can be shallower than every integer z12 sample.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            protomaps, peaks, gebco = _synthetic_two_cells(root, 215 * lp.CELL_PX)
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
            tiles, _ = lp.build_cell(lp.Archive(protomaps), lp.Archive(peaks), lp.Gebco(gebco), 214, 106)
            decoded = {lp.tileid_to_zxy(t): audit_land.decode_tile(gzip.decompress(v))
                       for t, v in tiles.items()}
        for zoom in range(8, 13):
            with self.subTest(zoom=zoom):
                parts = [(x, y, rings) for (z, x, y), layers in decoded.items() if z == zoom
                         for _, props, rings in layers.get("depth", (0, []))[1] if props["min"] == 20]
                region = shapely.union_all(audit_land.depth_polygons(parts, zoom))
                self.assertGreater(region.area, 0)
                self.assertFalse(region.covers(point))

    def test_adjacent_cells_agree_on_depth_at_the_seam(self):
        # Each cell is generated alone; land and depth near the shared edge must come out the
        # same from both sides at every zoom (neighbour land in the offshore distance, no
        # GEBCO edge truncation, bands clipped exactly at the cell edge).
        a, b = (214, 106), (215, 106)
        seam = 215 * 4096
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            protomaps, peaks, gebco = _synthetic_two_cells(root, seam)
            archive, peak_archive, grid = lp.Archive(protomaps), lp.Archive(peaks), lp.Gebco(gebco)
            decoded = {}
            for cell in (a, b):
                tiles, _ = lp.build_cell(archive, peak_archive, grid, *cell)
                decoded[cell] = {lp.tileid_to_zxy(t): audit_land.decode_tile(gzip.decompress(v))
                                 for t, v in tiles.items()}
        line = shapely.LineString([(seam, 106 * 4096), (seam, 107 * 4096)])
        crossing = 0
        for z in range(8, 13):
            for band in lp.DEPTH_BANDS_M:
                cuts = []
                for cell in (a, b):
                    feats = [(x, y, parts) for (tz, x, y), layers in decoded[cell].items() if tz == z
                             for _, props, parts in layers.get("depth", (0, []))[1] if props["min"] == band]
                    region = shapely.union_all(audit_land.depth_polygons(feats, z)) if feats else shapely.Polygon()
                    cuts.append(region.boundary.union(region).intersection(line))
                self.assertAlmostEqual(cuts[0].symmetric_difference(cuts[1]).length, 0, places=6, msg=(z, band))
                crossing += cuts[0].length > 0
        self.assertGreaterEqual(crossing, 10, "fixture must put several bands across the seam")


def _synthetic_two_cells(root, seam, bump=None):
    """One island straddling the seam of cells 214/106 and 215/106, and a GEBCO grid whose
    depth rises northward with a wobble, so bands and the offshore hole cross the seam.
    With bump=(columns, rows): a 10 m sea instead, with one 40 m block of GEBCO nodes at
    29.3 N whose columns are split evenly around the first node east of the seam."""
    island = shapely.box(seam - 70, 106 * 4096 + 2000, seam + 50, 106 * 4096 + 2110)
    tiles = {}
    for tx in range(214 * 16 - 2, 216 * 16 + 2):
        for ty in range(106 * 16 - 2, 107 * 16 + 2):
            local = shapely.affinity.affine_transform(island, [16, 0, 0, 16, -tx * 4096, -ty * 4096])
            if local.intersects(shapely.box(-64, -64, 4160, 4160)):
                layer = [{"name": "earth", "features": [{"geometry": local, "properties": {"kind": "earth"}}]}]
                tiles[zxy_to_tileid(12, tx, ty)] = gzip.compress(
                    mvt.encode(layer, default_options={"y_coord_down": True}), mtime=0)
    meta = {"version": "4.15.2", "planetiler:osm:osmosisreplicationtime": "synthetic",
            "planetiler:githash": "synthetic"}
    protomaps = root / "protomaps.pmtiles"
    lp.write_pmtiles(protomaps, tiles, meta, lp.cell_bounds(214, 106), 12, 12)
    peak = mvt.encode([{"name": "pois", "features": [
        {"geometry": shapely.Point(10, 10), "properties": {"kind": "peak", "elevation": 5}}]}],
        default_options={"y_coord_down": True})
    raw = root / "raw-peaks.pmtiles"
    lp.write_pmtiles(raw, {zxy_to_tileid(13, 214 * 32, 106 * 32): gzip.compress(peak, mtime=0)}, meta,
                     lp.cell_bounds(214, 106), 13, 13)
    peaks = root / "peaks.pmtiles"
    lp.make_peaks([raw], peaks, log=lambda *_: None)
    lat = (np.arange(int((28.2 + 90) * 240), int((29.7 + 90) * 240)) + 0.5) / 240 - 90
    lon = (np.arange(int((120.7 + 180) * 240), int((122.5 + 180) * 240)) + 0.5) / 240 - 180
    la, lo = np.meshgrid(lat, lon, indexing="ij")
    depth = 10 + 160 * (la - 28.3) + 25 * np.sin((lo - 121) * 9) * np.cos((la - 28.3) * 7)
    if bump:
        columns, rows = bump
        depth = np.full(la.shape, 10.0)
        j, i = np.searchsorted(lon, float(lp.lon_of_px(seam))), np.searchsorted(lat, 29.3)
        depth[i:i + rows, j - columns // 2:j + columns // 2] = 40.0
    gebco = root / "gebco.nc"
    ds = netCDF4.Dataset(gebco, "w")
    ds.createDimension("lat", len(lat))
    ds.createDimension("lon", len(lon))
    ds.createVariable("lat", "f8", ("lat",))[:] = lat
    ds.createVariable("lon", "f8", ("lon",))[:] = lon
    ds.createVariable("elevation", "i2", ("lat", "lon"))[:] = np.round(-depth).astype(np.int16)
    ds.close()
    return protomaps, peaks, gebco


if __name__ == "__main__":
    unittest.main()
