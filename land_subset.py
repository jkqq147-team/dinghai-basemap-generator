#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Select OSM land polygons intersecting pilot bboxes into one Shapefile (no clipping).

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
    parser.add_argument("output", type=Path, help="output land_polygons.shp")
    parser.add_argument("--bbox", action="append", required=True, help="west,south,east,north; repeatable")
    args = parser.parse_args()
    boxes = [tuple(float(value) for value in text.split(",")) for text in args.bbox]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with shapefile.Reader(str(args.input)) as reader, \
            shapefile.Writer(str(args.output.with_suffix("")), shapeType=shapefile.POLYGON) as writer:
        writer.fields = reader.fields[1:]
        for item in reader.iterShapeRecords():
            left, bottom, right, top = item.shape.bbox
            if any(right >= west and left <= east and top >= south and bottom <= north
                   for west, south, east, north in boxes):
                writer.shape(item.shape)
                writer.record(*item.record)
                count += 1
    for suffix in (".prj", ".cpg"):
        source = args.input.with_suffix(suffix)
        if source.exists():
            shutil.copyfile(source, args.output.with_suffix(suffix))
    print(f"{count} polygons -> {args.output}")


if __name__ == "__main__":
    main()
