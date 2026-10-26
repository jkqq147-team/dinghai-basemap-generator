#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Rebuilds the land-product test fixtures from local copies of the sources.

  protomaps-dachen.pmtiles  z8-12 tiles over two 4x4 blocks of z12 tiles in Dachen's cell
                            (cell 8/214/106): z12 tiles whole; their z8-11 ancestors
                            reduced to earth/landuse/roads/places/pois. Protomaps
                            build 20261025; © OpenStreetMap contributors, ODbL 1.0.
  protomaps-dachen-z13-14.pmtiles
                            the hill block's z13-14 descendants reduced to pois, from build
                            20261024 (the only z13-14 copy at hand), labelled with
                            20261025's build metadata so the peaks command accepts it.
  gebco-dachen.nc           GEBCO_2026 Grid over the cell (public domain, GEBCO terms).

Usage: make_land_fixtures.py Z8_12.pmtiles Z8_14.pmtiles gebco-dachen.npz OUTDIR
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import gzip  # noqa: E402

import netCDF4  # noqa: E402
import numpy as np  # noqa: E402
from pmtiles.tile import zxy_to_tileid  # noqa: E402

import land_product as lp  # noqa: E402

CELL = (214, 106)
PEAK_BLOCK = [(x, y) for x in range(3424, 3428) for y in range(1697, 1701)]  # hills, peaks at z13-14
SEA_BLOCK = [(x, y) for x in range(3434, 3438) for y in range(1708, 1712)]  # islands beside deep water
BLOCK = PEAK_BLOCK + SEA_BLOCK  # z12 tiles


def reduce(tile, keep):
    raw = lp.split_layers(tile)
    return b"".join(b"\x1a" + lp._encode_varint(len(raw[n])) + bytes(raw[n]) for n in keep if n in raw)


def main(low, high, npz, out):
    out = Path(out)
    low, high = lp.Archive(low), lp.Archive(high)
    tiles, peaks = {}, {}
    for x, y in BLOCK:
        tiles[(12, x, y)] = low.get(12, x, y)
        for z in range(8, 12):
            k = 1 << (12 - z)
            key = (z, x // k, y // k)
            tiles[key] = reduce(low.get(*key), ("earth", "landuse", "roads", "places", "pois"))
        for z in (13, 14) if (x, y) in PEAK_BLOCK else ():
            k = 1 << (z - 12)
            for dx in range(k):
                for dy in range(k):
                    tile = high.get(z, x * k + dx, y * k + dy)
                    if tile is not None:
                        peaks[(z, x * k + dx, y * k + dy)] = reduce(tile, ("pois",))
    meta = low.metadata()
    meta["dinghai:fixture"] = "test fixture: Protomaps 20261025 z8-12, two 4x4 z12 blocks in cell 8/214/106"

    def blobs(t):
        return {zxy_to_tileid(*k): gzip.compress(v, compresslevel=9, mtime=0) for k, v in t.items() if v}

    lp.write_cell_pmtiles(out / "protomaps-dachen.pmtiles", blobs(tiles), *CELL, meta)
    meta = dict(meta, **{"dinghai:fixture": "test fixture: Protomaps 20261024 z13-14 pois at Dachen, "
                                            "labelled with build 20261025 metadata"})
    lp.write_pmtiles(out / "protomaps-dachen-z13-14.pmtiles", blobs(peaks), meta, lp.cell_bounds(*CELL), 13, 14)
    g = np.load(npz)
    ds = netCDF4.Dataset(out / "gebco-dachen.nc", "w")
    ds.createDimension("lat", len(g["lats"]))
    ds.createDimension("lon", len(g["lons"]))
    ds.createVariable("lat", "f8", ("lat",))[:] = g["lats"]
    ds.createVariable("lon", "f8", ("lon",))[:] = g["lons"]
    ds.createVariable("elevation", "i2", ("lat", "lon"), zlib=True, complevel=9)[:] = g["elev"]
    ds.title = "GEBCO_2026 Grid subset over z8 cell 214/106 (test fixture)"
    ds.close()


if __name__ == "__main__":
    main(*sys.argv[1:])
