"""Drop synthetic near-duplicates (Day 6). Legacy and known-bug cases are never dropped.

Usage:
  uv run python scripts/dedup_synthetic.py
Reads datasets/synthetic_raw_v1.jsonl, legacy_v1.jsonl and known_bugs_v1.jsonl; writes
datasets/synthetic_dedup_v1.jsonl and prints the drop report (markdown).
"""

import math
import sys
from pathlib import Path

from evalkit.generation.generators import ROUTE_GAP
from evalkit.io import read_jsonl, write_jsonl
from evalkit.review.dedup import find_duplicates, render_report
from evalkit.schemas import TestCase

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "datasets"


def main() -> int:
    synthetic = list(read_jsonl(DATASETS / "synthetic_raw_v1.jsonl", TestCase))
    protected = [
        c for name in ("legacy_v1.jsonl", "known_bugs_v1.jsonl")
        for c in read_jsonl(DATASETS / name, TestCase)
    ]  # fmt: skip
    kept, drops = find_duplicates(synthetic, protected)
    write_jsonl(DATASETS / "synthetic_dedup_v1.jsonl", kept)
    targets = {route: math.ceil(gap * 1.5) for route, gap in ROUTE_GAP.items()}
    print(render_report(kept, drops, targets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
