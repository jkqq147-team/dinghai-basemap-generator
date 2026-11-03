# SPDX-License-Identifier: LGPL-3.0-only
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import mapbox_vector_tile as mvt
import shapefile
from pmtiles.tile import zxy_to_tileid
from shapely.geometry import Point, Polygon

import audit_seamap
import depth_product as dp
import seamap_product as sp

FIXTURES = Path(__file__).parent / "fixtures"


def _tile(name, geometry, properties):
    return gzip.compress(mvt.encode([{"name": name, "features": [
        {"geometry": geometry, "properties": properties}]}], default_options={"y_coord_down": True}), mtime=0)


class SeamapCellTests(unittest.TestCase):
    def test_overview_and_every_selected_cell_are_files_with_their_compiled_tiles(self):
        # Protects the App's file contract at the release boundary: the z0-7 tiles form the
        # global overview, a cell carries its z14 marine and z12 land/depth tiles unchanged and
        # declares the per-layer zoom ranges, and an empty cell still gets a file and entry.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cell, empty = (1, 2), (3, 4)
            archive = root / "compiled.pmtiles"
            sp.write_pmtiles(archive, {
                zxy_to_tileid(14, 67, 132): _tile("seamark", Point(32, 48),
                                                  {"type": "light_major", "light_color": "red", "osm_id": 123}),
                zxy_to_tileid(12, 20, 40): _tile("depth", Polygon([(0, 0), (0, 100), (100, 100), (0, 0)]),
                                                 {"min": 20}),
                zxy_to_tileid(12, 99, 99): _tile("landuse", Point(1, 1), {"class": "wood"}),  # another cell
                zxy_to_tileid(2, 0, 0): _tile("land", Polygon([(0, 0), (0, 100), (100, 100), (0, 0)]), {}),
            }, {}, (-180, -85, 180, 85), 8, 14)
            shp = root / "land_polygons.shp"
            with shapefile.Writer(str(shp.with_suffix("")), shapeType=shapefile.POLYGON) as writer:
                writer.field("FID", "N", size=11)
            dp.build_index(shp, root / "land.sqlite", log=lambda *_: None)
            dp.build(FIXTURES / "gebco-fractional.nc", "GEBCO_2026", root / "land.sqlite", [], root / "depth",
                     log=lambda *_: None)
            provenance = root / "provenance.json"
            source = {"url": "https://example.invalid/osm.pbf", "date": "2026-01-01T00:00:00Z",
                      "sha256": "a" * 64, "bytes": 1}
            provenance.write_text(json.dumps({"osm": source, "landPolygons": source}))
            manifest = sp.build(archive, provenance, root / "depth", "new", [cell, empty],
                                root / "out", root / "state")

            _, metadata, tiles = audit_seamap.read_pmtiles(sp.cell_path(root / "out", "new", *cell).read_bytes())
            decoded = {audit_seamap._zxy(tile): mvt.decode(gzip.decompress(body), default_options={"y_coord_down": True})
                       for tile, body in tiles.items()}
            self.assertEqual(set(decoded), {(12, 20, 40), (14, 67, 132)})
            self.assertEqual(decoded[(12, 20, 40)]["depth"]["features"][0]["properties"]["min"], 20)
            seamark = decoded[(14, 67, 132)]["seamark"]["features"][0]["properties"]
            self.assertEqual((seamark["type"], seamark["light_color"], seamark["osm_id"]),
                             ("light_major", "red", 123))
            layers = {layer["id"]: layer for layer in metadata["vector_layers"]}
            self.assertEqual(layers["landuse"]["fields"], {"class": "String"})
            self.assertEqual((layers["depth"]["minzoom"], layers["depth"]["maxzoom"]), (8, 12))
            self.assertEqual((layers["seamark"]["minzoom"], layers["seamark"]["maxzoom"]), (8, 14))
            self.assertIn("不可用于导航", metadata["dinghai:depthNotice"])

            overview_path = root / "out" / manifest["overview"]["url"]
            header, overview_metadata, overview = audit_seamap.read_pmtiles(overview_path.read_bytes())
            self.assertEqual([audit_seamap._zxy(tile) for tile in overview], [(2, 0, 0)])
            self.assertEqual((header["min_zoom"], header["max_zoom"]), (0, 7))
            self.assertEqual([layer["id"] for layer in overview_metadata["vector_layers"]], ["land", "water"])

            entries = json.loads((root / "out/releases/new/cells.json").read_text())
            self.assertEqual([(e["x"], e["y"]) for e in entries], [cell, empty])
            self.assertNotIn("seconds", entries[0])
            self.assertEqual(manifest["cellCount"], 2)
            for entry in entries:
                payload = sp.cell_path(root / "out", "new", entry["x"], entry["y"]).read_bytes()
                self.assertEqual(entry["bytes"], len(payload))
                self.assertEqual(entry["sha256"], hashlib.sha256(payload).hexdigest())
            _, _, none = audit_seamap.read_pmtiles(sp.cell_path(root / "out", "new", *empty).read_bytes())
            self.assertEqual(none, {})
            self.assertEqual(audit_seamap.audit(root / "out")["cellCount"], 2)


if __name__ == "__main__":
    unittest.main()
