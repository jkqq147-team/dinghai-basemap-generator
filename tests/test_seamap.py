# SPDX-License-Identifier: LGPL-3.0-only
import copy
import gzip
import hashlib
import json
import random
import tempfile
import unittest
from pathlib import Path

import mapbox_vector_tile as mvt
import shapefile
from pmtiles.tile import deserialize_directory, zxy_to_tileid
from shapely.geometry import Point, Polygon

import audit_seamap
import depth_product as dp
import seamap_product as sp

FIXTURES = Path(__file__).parent / "fixtures"


def _tile(name, geometry, properties):
    return gzip.compress(mvt.encode([{"name": name, "features": [
        {"geometry": geometry, "properties": properties}]}], default_options={"y_coord_down": True}), mtime=0)


def _sources(root):
    shp = root / "land.shp"
    with shapefile.Writer(str(shp.with_suffix("")), shapeType=shapefile.POLYGON) as writer:
        writer.field("FID", "N", size=11)
    dp.build_index(shp, root / "land.sqlite", log=lambda *_: None)
    dp.build(FIXTURES / "gebco-fractional.nc", "GEBCO_2026", root / "land.sqlite", [], root / "depth",
             log=lambda *_: None)
    source = {"url": "https://example.invalid/osm.pbf", "date": "2026-01-01",
              "sha256": "a" * 64, "bytes": 1}
    provenance = root / "provenance.json"
    provenance.write_text(json.dumps({"osm": source, "landPolygons": source}))
    return provenance


class SeamapCellTests(unittest.TestCase):
    def test_objects_are_identical_after_rebuild_release_or_source_identity_changes(self):
        # The release-boundary byte contract catches provenance leaking into objects;
        # the existing tile-content test never compares independent builds. No mocks/seams.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "compiled.pmtiles"
            sp.write_pmtiles(archive, {
                zxy_to_tileid(8, 209, 111): _tile("seamark", Point(1, 1), {"type": "buoy"}),
                zxy_to_tileid(0, 0, 0): _tile("land", Point(1, 1), {}),
            }, {}, (-180, -85, 180, 85), 0, 14)
            provenance_path = _sources(root)
            original = json.loads(provenance_path.read_text())
            baseline = None
            for variant in ("baseline", "rebuild", "release", "osm", "land", "depth"):
                provenance = copy.deepcopy(original)
                depth = dp.read_identity(root / "depth")
                if variant in ("osm", "land"):
                    provenance["osm" if variant == "osm" else "landPolygons"] = {
                        "url": "https://example.invalid/new.pbf", "date": "2026-02-02",
                        "sha256": "b" * 64, "bytes": 2}
                if variant == "depth":
                    depth["gebco"]["sha256"] = "c" * 64
                    depth["generatorSHA256"] = "d" * 64
                    depth["generatorVersion"] = "new-generator"
                    (root / "depth/identity.json").write_text(json.dumps(depth))
                (root / "provenance.json").write_text(json.dumps(provenance))
                output = root / variant
                manifest = sp.build(archive, root / "provenance.json", root / "depth",
                                    "other" if variant == "release" else "new", [(209, 111)],
                                    output, root / (variant + "-state"))
                self.assertEqual(manifest["sources"]["osm"], provenance["osm"])
                self.assertEqual(manifest["sources"]["landPolygons"], provenance["landPolygons"])
                self.assertEqual(manifest["sources"]["gebco"]["sha256"], depth["gebco"]["sha256"])
                digests = {}
                for path in output.rglob("*.pmtiles"):
                    data = path.read_bytes()
                    _, metadata, _ = audit_seamap.read_pmtiles(data)
                    digests[metadata.get("dinghai:cell", "overview")] = hashlib.sha256(data).hexdigest()
                self.assertEqual(set(digests), {"8/209/111", "overview"})
                if baseline is None:
                    baseline = digests
                else:
                    with self.subTest(variant=variant):
                        self.assertEqual(digests, baseline)

    def test_resume_skips_finished_cells_but_rebuilds_missing_objects(self):
        # State must remain untouched on resume but not suppress a missing object;
        # fresh-output determinism cannot exercise this existing production skip path.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provenance = _sources(root)
            archive = root / "compiled.pmtiles"
            sp.write_pmtiles(archive, {}, {}, (-180, -85, 180, 85), 0, 14)
            args = (archive, provenance, root / "depth", "new", [(1, 2)], root / "out", root / "state")
            sp.build(*args)
            state = root / "state/cells/1-2.json"
            original_state = (state.read_bytes(), state.stat().st_mtime_ns)
            manifest = (root / "out/manifest.json").read_bytes()
            sp.build(*args)
            self.assertEqual((state.read_bytes(), state.stat().st_mtime_ns), original_state)
            cell = next(path for path in (root / "out").rglob("*.pmtiles")
                        if audit_seamap.read_pmtiles(path.read_bytes())[1].get("dinghai:cell") == "8/1/2")
            original_cell = cell.read_bytes()
            cell.unlink()
            sp.build(*args)
            self.assertEqual(cell.read_bytes(), original_cell)
            self.assertEqual((root / "out/manifest.json").read_bytes(), manifest)

    def test_release_record_rejects_changed_provenance_without_moving_manifest(self):
        # An existing release is immutable even if object bytes are unchanged;
        # independent output directories never exercise name reuse. No test-only seam.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provenance = _sources(root)
            archive = root / "compiled.pmtiles"
            sp.write_pmtiles(archive, {}, {}, (-180, -85, 180, 85), 0, 14)
            args = (archive, provenance, root / "depth", "new", [(1, 2)], root / "out", root / "state")
            sp.build(*args)
            before = (root / "out/manifest.json").read_bytes()
            source = json.loads(provenance.read_text())
            source["osm"]["date"] = "2026-02-02"
            provenance.write_text(json.dumps(source))
            with self.assertRaises(ValueError):
                sp.build(*args)
            self.assertEqual((root / "out/manifest.json").read_bytes(), before)
            self.assertEqual((root / "out/releases/new.json").read_bytes(), before)
            sp.build(archive, provenance, root / "depth", "next", [(1, 2)], root / "out", root / "state")
            self.assertEqual((root / "out/releases/new.json").read_bytes(), before)
            self.assertEqual(json.loads((root / "out/manifest.json").read_bytes())["release"], "next")
            self.assertEqual(audit_seamap.audit(root / "out")["cellCount"], 1)

    def test_pmtiles_gzip_headers_are_canonical_including_leaf_directories(self):
        # The writer owns gzip portability: nonzero timestamps/platform OS bytes in
        # any section change object addresses; the small release fixture has no leaves.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "large.pmtiles"
            rng = random.Random(42)
            tiles = {i: gzip.compress(rng.randbytes(rng.randrange(20, 500)), mtime=123)
                     for i in range(22000)}
            sp.write_pmtiles(path, tiles, {}, (-180, -85, 180, 85), 0, 8)
            data = path.read_bytes()
            header, metadata, restored = audit_seamap.read_pmtiles(data)
            self.assertEqual(metadata, {})
            self.assertEqual({i: gzip.decompress(v) for i, v in restored.items()},
                             {i: gzip.decompress(v) for i, v in tiles.items()})
            root = data[header["root_offset"]:header["root_offset"] + header["root_length"]]
            members = [root, data[header["metadata_offset"]:header["metadata_offset"] + header["metadata_length"]]]
            self.assertGreater(header["leaf_length"], 0)
            for entry in deserialize_directory(root):
                start = header["leaf_offset"] + entry.offset
                members.append(data[start:start + entry.length])
            members.extend(restored.values())
            for member in members:
                self.assertEqual((member[4:8], member[9]), (b"\0" * 4, 255))

    def test_overview_and_every_selected_cell_are_files_with_their_compiled_tiles(self):
        # Protects the App's file contract at the release boundary: the z0-7 tiles form the
        # global overview, a cell carries its z14 marine and z12 land/depth tiles unchanged and
        # declares zoom ranges; v3 shards address every selected cell (even empty),
        # with compact hashed rows sorted into their z4 parents instead of a global inventory.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cell, empty = (1, 2), (17, 16)
            selected = [empty, (255, 255), (16, 15), cell, (15, 15), (209, 111)]
            archive = root / "compiled.pmtiles"
            sp.write_pmtiles(archive, {
                zxy_to_tileid(14, 67, 132): _tile("seamark", Point(32, 48),
                                                  {"type": "light_major", "light_color": "red", "osm_id": 123}),
                zxy_to_tileid(12, 20, 40): _tile("depth", Polygon([(0, 0), (0, 100), (100, 100), (0, 0)]),
                                                 {"min": 20}),
                zxy_to_tileid(12, 99, 99): _tile("landuse", Point(1, 1), {"class": "wood"}),  # another cell
                zxy_to_tileid(2, 0, 0): _tile("land", Polygon([(0, 0), (0, 100), (100, 100), (0, 0)]), {}),
            }, {}, (-180, -85, 180, 85), 8, 14)
            provenance = _sources(root)
            manifest = sp.build(archive, provenance, root / "depth", "new", selected,
                                root / "out", root / "state")

            self.assertEqual(manifest["schemaVersion"], 3)
            self.assertFalse({"cells", "cellsURL", "cellsBytes", "cellsSHA256"} & manifest.keys())
            self.assertFalse(list((root / "out").rglob("cells.json")))
            self.assertEqual(manifest["record"], "releases/new.json")
            self.assertEqual((root / "out/releases/new.json").read_bytes(),
                             (root / "out/manifest.json").read_bytes())
            expected_parents = {"0/0": [(1, 2), (15, 15)], "1/0": [(16, 15)],
                                "1/1": [(17, 16)], "13/6": [(209, 111)], "15/15": [(255, 255)]}
            self.assertEqual(set(manifest["index"]), set(expected_parents))
            entries = {}
            for parent, digest in manifest["index"].items():
                payload = (root / "out/index" / f"{digest}.json").read_bytes()
                self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)
                rows = json.loads(payload)
                self.assertEqual(payload, json.dumps(rows, separators=(",", ":")).encode())
                self.assertEqual([(x, y) for x, y, size, sha in rows], expected_parents[parent])
                for x, y, size, sha in rows:
                    data = (root / "out/objects" / f"{sha}.pmtiles").read_bytes()
                    self.assertEqual((len(data), hashlib.sha256(data).hexdigest()), (size, sha))
                    entries[x, y] = data
            self.assertEqual(manifest["totalBytes"], sum(map(len, entries.values())))
            _, metadata, tiles = audit_seamap.read_pmtiles(entries[cell])
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

            overview_path = root / "out/objects" / (manifest["overview"]["sha256"] + ".pmtiles")
            header, overview_metadata, overview = audit_seamap.read_pmtiles(overview_path.read_bytes())
            self.assertEqual([audit_seamap._zxy(tile) for tile in overview], [(2, 0, 0)])
            self.assertEqual((header["min_zoom"], header["max_zoom"]), (0, 7))
            self.assertEqual([layer["id"] for layer in overview_metadata["vector_layers"]], ["land", "water"])

            self.assertEqual(set(manifest["overview"]), {"sha256", "bytes"})
            self.assertEqual(manifest["cellCount"], len(selected))
            _, _, none = audit_seamap.read_pmtiles(entries[empty])
            self.assertEqual(none, {})
            self.assertEqual(audit_seamap.audit(root / "out")["cellCount"], len(selected))


if __name__ == "__main__":
    unittest.main()
