# SPDX-License-Identifier: LGPL-3.0-only
import math
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

import gshhg_product as producer
from audit_product import audit, decode, land_at

FIXTURES = Path(__file__).parent / "fixtures"


class ProductTests(unittest.TestCase):
    def test_official_records_round_trip_marine_five_lods_fills_and_polar_coast_without_inland_strokes(self):
        # Real binary->SQLite boundary: inland strokes or lost fills/LOD/polar closure fail;
        # previous coverage required those unwanted strokes; no test-only production seam.
        inputs = [producer.Input("coast", r, FIXTURES / ("gshhs_f-controls.b" if r == "f" else "gshhs_c.b"))
                  for r in ("c", "l", "i", "h", "f")]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output.sqlite"
            producer.build(inputs, output, "2.3.7-fixture")
            with sqlite3.connect(output) as db:
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM features WHERE family!='coast' OR (kind=1 AND level>1)"
                ).fetchone()[0], 0, "marine product must not download inland outlines or other families")
                for r in ("c", "l", "i", "h"):
                    self.assertEqual([level for level, in db.execute(
                        "SELECT DISTINCT level FROM features WHERE resolution=? AND kind=0 ORDER BY level",
                        (r,)
                    )], [1, 2, 3], "official c fixture has L1–3 fills and no L4 source record")
            result = audit(output, geography=False)
            self.assertGreater(result["points"], 10000)
            self.assertEqual(result["fake_bottom_strokes"], 0)
            self.assertTrue(all(result["groups"].get(f"{r}/coast/{k}", 0) > 0
                                for r in ("c", "l", "i", "h", "f") for k in (0, 1)))
            members = result["members"]
            self.assertEqual(members[0]["records"], 1781)
            self.assertEqual(members[-1]["records"], 2)
            self.assertEqual(members[-1]["levels"], {"5": 1, "6": 1})
            with sqlite3.connect(output) as db:
                for r in ("c", "l", "i", "h", "f"):
                    for lon, lat, expected in ((-60, -84, True), (-179.9, -84, True),
                                                (179.9, -84, True), (-60, -70, False)):
                        self.assertEqual(land_at(db, r, lon, lat), expected, (r, lon, lat))
                self.assertTrue(land_at(db, "c", -40, 75))
                self.assertFalse(land_at(db, "c", 0, 84))
                self.assertTrue(land_at(db, "c", 15, 50))
                self.assertTrue(land_at(db, "c", 133, -25))

    def test_closed_source_stroke_keeps_last_segment_and_l5_island_but_excludes_l6(self):
        # Regression: old quantization dropped a line's closing duplicate, losing its last source edge.
        # Existing polar test used an open path; no new production seam is needed.
        ring = [(10, 10), (11, 10), (11, 11), (10, 11), (10, 10)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.b"
            source.write_bytes(self.binary(ring, 1) + self.binary([(x+10, y) for x, y in ring], 5)
                               + self.binary([(x+20, y) for x, y in ring], 6))
            output = root / "output.sqlite"
            producer.build([producer.Input("coast", "c", source)], output, "synthetic")
            with sqlite3.connect(output) as db:
                self.assertTrue(land_at(db, "c", 20.5, 10.5))
                self.assertFalse(land_at(db, "c", 30.5, 10.5))
                from shapely.geometry import LineString, Point
                lines = []
                for zoom, col, row, blob in db.execute(
                        "SELECT tile_zoom,tile_column,tile_row,geometry FROM features WHERE kind=1"):
                    for line, in decode(blob, 1):
                        points = []
                        for x, y in line:
                            lon = (col + x/65535) / (1 << zoom) * 360 - 180
                            lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2*(row+y/65535)/(1 << zoom)))))
                            points.append((lon, lat))
                        lines.append(LineString(points))
                self.assertLess(min(line.distance(Point(10, 10.5)) for line in lines), 0.01)

    def test_invalid_binary_never_replaces_previous_product(self):
        # Corrupt native counts/version/truncation must abort real staging, not damage published output.
        valid = self.binary([(10, 10), (11, 10), (11, 11), (10, 10)], 1)
        bad_version = bytearray(valid)
        struct.pack_into(">i", bad_version, 8, 1 | (14 << 8))
        for data in (valid[:20], valid[:-1], bytes(bad_version)):
            with self.subTest(size=len(data)), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "bad.b"
                source.write_bytes(data)
                output = root / "output.sqlite"
                output.write_bytes(b"previous product")
                with self.assertRaises(ValueError):
                    producer.build([producer.Input("coast", "c", source)], output, "synthetic")
                self.assertEqual(output.read_bytes(), b"previous product")
                self.assertEqual(list(root.glob(".gshhg-*")), [])

    @staticmethod
    def binary(points, level):
        xs = [round(x*1000000) for x, _ in points]
        ys = [round(y*1000000) for _, y in points]
        return struct.pack(">11i", 1, len(points), level | (15 << 8), min(xs), max(xs), min(ys), max(ys),
                           0, 0, -1, -1) + b"".join(struct.pack(">ii", x, y) for x, y in zip(xs, ys))


if __name__ == "__main__":
    unittest.main()
