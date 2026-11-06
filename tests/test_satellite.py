# SPDX-License-Identifier: LGPL-3.0-only
import json
import hashlib
from io import BytesIO
from pathlib import Path
import sqlite3
import tempfile
import unittest

import shapely
from PIL import Image
from pmtiles.tile import zxy_to_tileid

import audit_satellite
import satellite_product as satellite
import satellite_overview


class CoastalSelectionTests(unittest.TestCase):
    def test_ocean_flood_wraps_longitude_and_crosses_diagonals_but_not_land(self):
        # Seamap has no ocean flood: four-neighbour or non-wrapping traversal loses this coast.
        fractions = [[1.0] * 256 for _ in range(256)]
        for x in range(22):
            fractions[x][128] = 0.0
        fractions[255][129] = 0.25
        fractions[253][129] = 0.25
        fractions[21][0] = fractions[21][255] = 0.0
        reached = satellite.ocean_cells(fractions)
        self.assertEqual(reached, {(x, 128) for x in range(22)} | {(255, 129)})

    def test_selection_keeps_tiny_islands_and_excludes_enclosed_inland_water(self):
        # A raster/area cutoff loses islands; treating all non-land as sea admits the enclosed island.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "land.sqlite"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE land(cell INTEGER, wkb BLOB)")
                db.execute("CREATE TABLE metadata(key TEXT, value TEXT)")
                db.execute("INSERT INTO metadata VALUES ('source', ?)", (json.dumps({"fixture": True}),))
                for x in range(99, 102):
                    for y in range(99, 102):
                        if (x, y) != (100, 100):
                            geom = shapely.box(x * 4096, y * 4096, (x + 1) * 4096, (y + 1) * 4096)
                            db.execute("INSERT INTO land VALUES (?, ?)", (x * 256 + y, shapely.to_wkb(geom)))
                for x, y in ((100, 100), (50, 128)):
                    geom = shapely.box(x * 4096 + 10, y * 4096 + 10, x * 4096 + 11, y * 4096 + 11)
                    db.execute("INSERT INTO land VALUES (?, ?)", (x * 256 + y, shapely.to_wkb(geom)))
            result = satellite.coastal_cells(path)
            self.assertEqual(result["cells"], [(50, 128)])
            self.assertEqual(result["landFractions"][50][128], 1 / 4096**2)


class PublicationTests(unittest.TestCase):
    def test_raster_release_preserves_source_blobs_and_missing_levels_at_public_boundary(self):
        # Existing Seamap tests only cover MVT: re-encoding, filled gaps or lost overview zooms breaks this copy contract.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mb = root / "input.mbtiles"
            source = {"url": satellite.SOURCE_URL, "etag": '"fixture-version"', "bytes": 1000}
            expected = {}
            with sqlite3.connect(mb) as db:
                db.execute("CREATE TABLE tiles(zoom_level INTEGER,tile_column INTEGER,tile_row INTEGER,tile_data BLOB)")
                for z, x, y, color in ((8, 209, 111, "blue"), (10, 836, 444, "green")):
                    stream = BytesIO()
                    Image.new("RGB", (512, 512), color).save(stream, format="WEBP", lossless=True)
                    blob = stream.getvalue()
                    expected[zxy_to_tileid(z, x, y)] = blob
                    db.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, x, (1 << z) - 1 - y, blob))
            path = root / "object.pmtiles"
            counts = satellite.write_cell(mb, path, 209, 111, source)
            self.assertEqual(set(counts), {8, 10})
            data = path.read_bytes()
            _, _, tiles = audit_satellite.read_pmtiles(data)
            self.assertEqual(tiles, expected)
            sha = hashlib.sha256(data).hexdigest()
            (root / "objects").mkdir()
            path.rename(root / "objects" / f"{sha}.pmtiles")
            identity = {"sources": {"satellite": source}, "generatorIdentity": "test",
                        "cli": {"version": "5.7.0"}, "coastalSelection": {}}
            satellite.finalize(root, "test", [{"x": 209, "y": 111, "bytes": len(data), "sha256": sha}], identity)
            overview_mb = root / "overview.mbtiles"
            overview_expected = {}
            with sqlite3.connect(overview_mb) as db:
                db.execute("CREATE TABLE tiles(zoom_level INTEGER,tile_column INTEGER,tile_row INTEGER,tile_data BLOB)")
                for z in range(7):
                    blob = next(iter(expected.values()))
                    db.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, 0, (1 << z) - 1, blob))
                    overview_expected[zxy_to_tileid(z, 0, 0)] = blob
            overview_path = root / "overview.pmtiles"
            satellite_overview.write_overview(overview_mb, overview_path, source)
            overview_bytes = overview_path.read_bytes()
            overview_sha = hashlib.sha256(overview_bytes).hexdigest()
            overview_path.rename(root / "objects" / f"{overview_sha}.pmtiles")
            overview = {"sha256": overview_sha, "bytes": len(overview_bytes)}
            satellite_overview.publish_overview(root, (root / "manifest.json").read_bytes(), overview, "overview-test")
            manifest = json.loads((root / "manifest.json").read_text())
            self.assertEqual(set(manifest["index"]), {"13/6"})
            self.assertEqual(manifest["overview"], overview)
            self.assertEqual(manifest["generatorIdentity"], "test")
            self.assertEqual(audit_satellite.read_pmtiles(overview_bytes)[2], overview_expected)
            report = audit_satellite.audit(root)
            self.assertEqual((report["cellCount"], report["levels"]), (1, {8: 1, 10: 1}))
            self.assertEqual(report["overview"]["levels"], {z: 1 for z in range(7)})
            # Changing immutable bytes must fail at the release audit, not pass on file existence.
            (root / "objects" / f"{sha}.pmtiles").write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                audit_satellite.audit(root)


if __name__ == "__main__":
    unittest.main()
