#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Select OSM land polygons intersecting named pilot bboxes (no geometry clipping).

This avoids Planetiler 0.10.2/GeoTools' empty bounded Shapefile filter. The
compiler still performs all actual tile clipping. Global builds use the full
Shapefile directly and do not need this helper.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import shapefile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="extracted global land_polygons.shp")
    parser.add_argument("output", type=Path)
    parser.add_argument("--bbox", action="append", required=True,
                        help="NAME=west,south,east,north; repeat for independent output directories")
    args = parser.parse_args()
    selections = []
    for text in args.bbox:
        name, values = text.split("=", 1)
        west, south, east, north = (float(value) for value in values.split(","))
        if not name or not west < east and south < north:
            raise ValueError(f"invalid bbox {text}")
        directory = args.output / name
        directory.mkdir(parents=True, exist_ok=True)
        writer = shapefile.Writer(str(directory / "land_polygons"), shapeType=shapefile.POLYGON)
        selections.append((name, (west, south, east, north), directory, writer))

    with shapefile.Reader(str(args.input)) as reader:
        for _, _, _, writer in selections:
            writer.fields = reader.fields[1:]
        counts = {name: 0 for name, *_ in selections}
        for item in reader.iterShapeRecords():
            left, bottom, right, top = item.shape.bbox
            for name, (west, south, east, north), _, writer in selections:
                if right >= west and left <= east and top >= south and bottom <= north:
                    writer.shape(item.shape)
                    writer.record(*item.record)
                    counts[name] += 1
    for name, (west, south, east, north), directory, writer in selections:
        writer.close()
        for suffix in (".prj", ".cpg"):
            source = args.input.with_suffix(suffix)
            if source.exists():
                shutil.copyfile(source, directory / f"land_polygons{suffix}")
        print(f"{name}: {counts[name]} polygons -> {directory / 'land_polygons.shp'}")


if __name__ == "__main__":
    main()
