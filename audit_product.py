#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Independent stdlib-only schema-2 decoding and release geography audit."""
import argparse
import json
import math
import sqlite3
import struct
from collections import Counter
from pathlib import Path


def decode(blob, kind):
    offset = 0

    def count():
        nonlocal offset
        if offset + 4 > len(blob):
            raise ValueError("truncated count")
        value, = struct.unpack_from("<I", blob, offset)
        offset += 4
        return value

    def points():
        nonlocal offset
        n = count()
        if n < (3 if kind == 0 else 2) or n > (len(blob) - offset) // 4:
            raise ValueError("invalid point count")
        result = list(struct.iter_unpack("<HH", blob[offset:offset + 4 * n]))
        offset += 4 * n
        return result

    features = []
    n = count()
    if not n or n > len(blob) // 12:
        raise ValueError("invalid feature count")
    for _ in range(n):
        if kind == 0:
            rings = count()
            if not rings or rings > (len(blob) - offset) // 16:
                raise ValueError("invalid ring count")
            features.append([points() for _ in range(rings)])
        elif kind == 1:
            features.append([points()])
        else:
            raise ValueError("invalid kind")
    if offset != len(blob):
        raise ValueError("trailing bytes")
    return features


def contains(rings, x, y):
    inside = False
    for ring in rings:
        px, py = ring[-1]
        for cx, cy in ring:
            if (cy > y) != (py > y) and x < (px - cx) * (y - cy) / (py - cy) + cx:
                inside = not inside
            px, py = cx, cy
    return inside


def land_at(db, resolution, longitude, latitude):
    zoom, = db.execute("SELECT tile_zoom FROM lods WHERE resolution=?", (resolution,)).fetchone()
    tiles = 1 << zoom
    x = (longitude + 180) / 360 * tiles
    y = (1 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2 * tiles
    column, row = int(x), int(y)
    level = 0
    for hierarchy, blob in db.execute(
        "SELECT level,geometry FROM features WHERE resolution=? AND tile_column=? AND tile_row=? "
        "AND family='coast' AND kind=0 ORDER BY level", (resolution, column, row)
    ):
        if any(contains(polygon, (x-column)*65535, (y-row)*65535) for polygon in decode(blob, 0)):
            level = hierarchy
    return level % 2 == 1


CONTROLS = [
    ("Antarctic peninsula land", -65, -75, True),
    ("Antarctic cap", -60, -84, True),
    ("west date-line cap", -179.9, -84, True),
    ("east date-line cap", 179.9, -84, True),
    ("Antarctic ocean", -60, -70, False),
    ("Greenland", -40, 75, True),
    ("Arctic ocean", 0, 84, False),
    ("Europe", 15, 50, True),
    ("Australia", 133, -25, True),
    ("Pacific ocean", -140, 0, False),
]


def audit(path, geography=True):
    db = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("integrity_check failed")
        if db.execute("PRAGMA user_version").fetchone() != (2,):
            raise ValueError("wrong schema")
        metadata = dict(db.execute("SELECT * FROM metadata"))
        for key, expected in (("schema_version", "2"), ("quantization_maximum", "65535"),
                              ("geometry_encoding", "dinghai-quantized-web-mercator-v1"),
                              ("antarctica_boundary", "ice-front"),
                              ("product_scope", "marine-background"),
                              ("feature_scope", "family='coast' AND (kind=0 OR level=1)")):
            if metadata.get(key) != expected:
                raise ValueError(f"wrong {key}")
        lods = dict((r, (lo, hi, z)) for r, lo, hi, z in db.execute("SELECT * FROM lods"))
        if lods != {"c": (0, 3, 2), "l": (4, 5, 3), "i": (6, 7, 4), "h": (8, 10, 5), "f": (11, 30, 6)}:
            raise ValueError("wrong LOD routing")
        members = json.loads(metadata["source_members"])
        if metadata.get("source_scope") == "official-coast-5":
            if metadata.get("source_version") != "2.3.7" or metadata.get("source_archive_sha256") != (
                "28600e8f7a08645aab43079326df6504212ec5ccb2b4bcf3b5f4f12ed60e82bc"
            ):
                raise ValueError("wrong official input identity")
            if sorted((m["name"], m["family"], m["resolution"]) for m in members) != sorted(
                (f"gshhs_{r}.b", "coast", r) for r in lods
            ):
                raise ValueError("wrong official coast members")
        elif metadata.get("source_scope") != "explicit-file-inputs":
            raise ValueError("wrong source scope")
        if db.execute("SELECT count(*) FROM sqlite_master WHERE name='fragments'").fetchone()[0]:
            raise ValueError("unretired spool")
        groups = Counter()
        rows = points = features = fake_bottom_strokes = 0
        for r, zoom, column, row, family, level, kind, blob, count, n in db.execute("SELECT * FROM features"):
            if r not in lods or zoom != lods[r][2] or not (0 <= column < 1 << zoom and 0 <= row < 1 << zoom):
                raise ValueError("invalid tile")
            if family != "coast" or (kind == 1 and level != 1):
                raise ValueError("non-marine feature: only coast fills and level-1 strokes are allowed")
            if level not in range(1, 5):
                raise ValueError("L5/L6 leaked into hierarchy")
            decoded = decode(blob, kind)
            actual_points = sum(len(ring) for feature in decoded for ring in feature)
            if len(decoded) != count or actual_points != n:
                raise ValueError("blob counts disagree with SQL")
            if family == "coast" and kind == 1 and row == (1 << zoom) - 1:
                for line, in decoded:
                    fake_bottom_strokes += sum(a[1] == b[1] == 65535 and abs(a[0] - b[0]) > 100
                                               for a, b in zip(line, line[1:]))
            groups[f"{r}/{family}/{kind}"] += count
            rows += 1
            features += count
            points += n
        if fake_bottom_strokes:
            raise ValueError("invented polar clip-boundary coast stroke")
        if any(not groups[f"{r}/coast/{kind}"] for r in lods for kind in (0, 1)):
            raise ValueError("missing LOD fill/stroke coverage")
        controls = []
        if geography:
            for label, lon, lat, expected in CONTROLS:
                actual = land_at(db, "f", lon, lat)
                controls.append({"name": label, "longitude": lon, "latitude": lat, "land": actual})
                if actual != expected:
                    raise ValueError(f"geography control failed: {label}")
        return {"rows": rows, "features": features, "points": points, "groups": dict(sorted(groups.items())),
                "integrity": "ok",
                "fake_bottom_strokes": fake_bottom_strokes, "controls_full": controls,
                "members": members, "metadata": metadata, "lods": lods}
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.database), indent=2, sort_keys=True))
