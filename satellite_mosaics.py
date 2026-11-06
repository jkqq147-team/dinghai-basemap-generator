#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Choose three random published cells (at least one Asian) and render native-tile mosaics."""
import argparse
from io import BytesIO
import json
from pathlib import Path
import random

from PIL import Image, ImageDraw

from audit_satellite import read_pmtiles
from audit_seamap import _zxy
from seamap_product import cell_bounds


def render(root, audit_path, output, seed=None):
    root, output = Path(root), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    cells = json.loads(Path(audit_path).read_text())["cells"]
    if seed is None:
        seed = random.SystemRandom().getrandbits(64)
    rng = random.Random(seed)
    # East/South/Southeast Asian mainland and islands, deliberately away from
    # the Europe/Asia boundary; these bounds only choose a visual audit sample.
    asian = [c for c in cells if 60 <= cell_bounds(c["x"], c["y"])[0] < 150
             and -10 <= cell_bounds(c["x"], c["y"])[1] < 60]
    first = rng.choice(asian)
    selected = [first, *rng.sample([c for c in cells if c != first], 2)]
    samples = []
    for cell in selected:
        x, y = cell["x"], cell["y"]
        data = (root / "objects" / f"{cell['sha256']}.pmtiles").read_bytes()
        _, _, tiles = read_pmtiles(data)
        coordinates = {_zxy(tid): blob for tid, blob in tiles.items()}
        z = max(c[0] for c in coordinates)
        positions = sorted(c for c in coordinates if c[0] == z)
        # Centre a 4x4 native-tile window on a randomly chosen stored tile.
        _, tx, ty = rng.choice(positions)
        side = 1 << (z - 8)
        width = min(4, side)
        left = max(x * side, min(tx - width // 2, (x + 1) * side - width))
        top = max(y * side, min(ty - width // 2, (y + 1) * side - width))
        panel = Image.new("RGB", (width * 512, width * 512 + 64), "#606060")
        draw = ImageDraw.Draw(panel)
        draw.rectangle((0, 0, panel.width, 63), fill="#102030")
        draw.text((12, 10), f"8/{x}/{y} | native z{z}, 512 px WebP | grey = absent source tile", fill="white")
        draw.text((12, 32), "VersaTiles sources (https://versatiles.org/sources/)", fill="white")
        missing = []
        for py in range(top, top + width):
            for px in range(left, left + width):
                blob = coordinates.get((z, px, py))
                if blob is None:
                    missing.append([z, px, py])
                else:
                    with Image.open(BytesIO(blob)) as image:
                        panel.paste(image.convert("RGB"), ((px - left) * 512, 64 + (py - top) * 512))
        name = f"mosaic-8-{x}-{y}-z{z}.png"
        panel.save(output / name)
        samples.append({"cell": f"8/{x}/{y}", "sha256": cell["sha256"], "file": name,
                        "zoom": z, "tileWindow": [left, top, width, width], "missingTiles": missing,
                        "asianSample": cell == first})
    report = {"seed": seed, "samples": samples}
    (output / "mosaics.json").write_text(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    print(json.dumps(render(args.root, args.audit, args.output, args.seed), indent=2))
