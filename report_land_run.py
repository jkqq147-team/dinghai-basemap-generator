#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-only
"""Summarize a completed land build/audit from its manifest, state and run records."""
import argparse
import json
from collections import Counter
from pathlib import Path


def failure_type(message):
    if "one-sided shared-edge" in message:
        return "seam"
    if "piece of " in message or "fragment across cells" in message:
        return "fragment_area"
    if "has GEBCO depth" in message:
        return "depth"
    if " m from land" in message:
        return "offshore_distance"
    if "touches land" in message:
        return "land_overlap"
    if "leaves the cell" in message:
        return "cell_extent"
    return "structure_or_other"


def summarize(run_root, audit_root):
    run_root, audit_root = Path(run_root), Path(audit_root)
    manifest = json.loads((run_root / "out/land/manifest.json").read_text())
    stages = {r["stage"]: r for r in json.loads((run_root / "regeneration.json").read_text())}
    states = [json.loads(p.read_text())
              for p in (run_root / "state" / manifest["release"] / "cells").glob("*.json")]
    audit = json.loads((audit_root / "report.json").read_text())
    depths = [c["depth"] for c in audit["cells"].values() if "depth" in c]

    def minimum(key):
        return min((d[key] for d in depths if d[key] is not None), default=None)

    return {
        "release": manifest["release"],
        "cellCount": manifest["cellCount"],
        "totalBytes": manifest["totalBytes"],
        "buildSeconds": stages["build"]["seconds"],
        "buildPeakRSSBytes": max(r["peakRSSBytes"] for r in states),
        "memoryScope": "maximum recorded worker ru_maxrss",
        "auditSeconds": stages["audit"]["seconds"],
        "auditFailureCount": len(audit["failures"]),
        "auditFailuresByType": dict(sorted(Counter(failure_type(f["error"]) for f in audit["failures"]).items())),
        "failureCounting": "audit failure entries; a shared-edge failure can name both cells",
        "minOffshoreMeters": minimum("min_offshore_m"),
        "minDepthMarginMeters": minimum("min_depth_margin_m"),
        "minimaScope": "audit samples in successfully audited cells",
        "globalMinimaComplete": (audit["geography"] and not audit["failures"]
                                 and len(audit["cells"]) == manifest["cellCount"]),
        "stageExitCodes": {name: stage["returncode"] for name, stage in stages.items()},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path, help="contains out/land, state and regeneration.json")
    parser.add_argument("audit_root", type=Path, help="contains the completed audit report.json")
    args = parser.parse_args()
    print(json.dumps(summarize(args.run_root, args.audit_root), indent=2, ensure_ascii=False))
