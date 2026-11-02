# SPDX-License-Identifier: LGPL-3.0-only
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import mapbox_vector_tile as mvt
from pmtiles.tile import zxy_to_tileid
from shapely.geometry import Point, Polygon

import audit_land
import audit_seamap
import land_product as lp
import seamap_product as sp


class SeamapCellTests(unittest.TestCase):
    def test_combined_cell_and_empty_ocean_at_release_boundary(self):
        # Protects the one-file App contract: a missing old layer, missing z14 marine layer,
        # or omitted empty-ocean file is a regression that land-product-1 tests cannot catch.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cell, ocean = (1, 2), (3, 4)
            marine = lp.encode_layers([{"name": "seamark", "features": [{
                "type": 1, "geometry": Point(32, 48),
                "properties": {"type": "light_major", "light_color": "red", "osm_id": 123},
            }]}], 4096)
            land = lp.encode_layers([{"name": "landuse", "features": [{
                "type": 3, "geometry": Polygon([(0, 0), (0, 100), (100, 100), (0, 0)]),
                "properties": {"class": "wood"},
            }]}], 4096)
            osm = root / "osm.pmtiles"
            lp.write_pmtiles(osm, {zxy_to_tileid(14, 67, 132): gzip.compress(marine, mtime=0)}, {},
                             (-180, -85, 180, 85), 8, 14)
            old_root = root / "old"
            old_path = sp._cell_path(old_root, "old", *cell)
            old_path.parent.mkdir(parents=True)
            lp.write_pmtiles(old_path, {zxy_to_tileid(12, 20, 40): gzip.compress(land, mtime=0)},
                             lp.cell_metadata(*cell, "old", {"protomaps": {"build": "b", "version": "v", "sha256": "p"},
                                                             "gebco": {"grid": "g", "sha256": "g"},
                                                             "generatorSHA256": "old"}),
                             lp.cell_bounds(*cell), 8, 12)
            old_bytes = old_path.read_bytes()
            old_entry = {"x": cell[0], "y": cell[1], "bytes": len(old_bytes),
                         "sha256": hashlib.sha256(old_bytes).hexdigest()}
            (old_root / "releases/old/cells.json").write_text(json.dumps([old_entry]))
            (old_root / "manifest.json").write_text(json.dumps({
                "release": "old", "cells": "releases/old/cells.json", "sources": {"protomaps": {}, "gebco": {}},
            }))
            provenance = root / "provenance.json"
            source = {"url": "https://example.invalid/osm.pbf", "date": "2026-01-01T00:00:00Z",
                      "sha256": "a" * 64, "bytes": 1}
            provenance.write_text(json.dumps({"osm": source, "landPolygons": source}))
            sliced = root / "sliced.pmtiles"
            sp.slice_cell(osm, sliced, *cell)
            manifest = sp.build(sliced, provenance, old_root, "new", [cell, ocean], root / "out", root / "state")

            combined_path = sp._cell_path(root / "out", "new", *cell)
            _, metadata, tiles = audit_land.read_pmtiles(combined_path.read_bytes())
            decoded = {audit_land._zxy(tile): mvt.decode(gzip.decompress(body), default_options={"y_coord_down": True})
                       for tile, body in tiles.items()}
            self.assertEqual(decoded[(12, 20, 40)]["landuse"]["features"][0]["properties"]["class"], "wood")
            seamark = decoded[(14, 67, 132)]["seamark"]["features"][0]["properties"]
            self.assertEqual((seamark["type"], seamark["light_color"], seamark["osm_id"]),
                             ("light_major", "red", 123))
            self.assertEqual({z for z, _, _ in decoded}, {12, 14})
            schemas = {layer["id"]: layer["fields"] for layer in metadata["vector_layers"]}
            self.assertEqual(schemas["landuse"], {"class": "String"})
            self.assertEqual(schemas["depth"], {"min": "Number"})

            entries = json.loads((root / "out/releases/new/cells.json").read_text())
            self.assertEqual(len(entries), 2)
            self.assertNotIn("seconds", entries[0])
            self.assertEqual(manifest["cellCount"], 2)
            for entry in entries:
                payload = sp._cell_path(root / "out", "new", entry["x"], entry["y"]).read_bytes()
                self.assertEqual(entry["bytes"], len(payload))
                self.assertEqual(entry["sha256"], hashlib.sha256(payload).hexdigest())
            _, _, empty = audit_land.read_pmtiles(sp._cell_path(root / "out", "new", *ocean).read_bytes())
            self.assertEqual(empty, {})
            self.assertEqual(audit_seamap.audit(root / "out")["cellCount"], 2)


if __name__ == "__main__":
    unittest.main()
