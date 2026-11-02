#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Extract one z8 cell's z8-14 MVT layers from a DingHai OSM PMTiles archive."""
import argparse
from pathlib import Path

import seamap_product as sp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cell", type=sp._cell, required=True)
    args = parser.parse_args()
    sp.slice_cell(args.input, args.output, *args.cell)


if __name__ == "__main__":
    main()
