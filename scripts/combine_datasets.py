"""Combine datasets into one file for a run.

Usage:
  uv run python scripts/combine_datasets.py                      # builds day4_baseline.jsonl
  uv run python scripts/combine_datasets.py OUT IN1 IN2 [...]   # any combination
"""

import sys
from pathlib import Path

from evalkit.importers.combine import DuplicateCaseIds, combine_datasets

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "datasets"
DAY4_BASELINE = (
    DATASETS / "day4_baseline.jsonl",
    [DATASETS / "legacy_v1.jsonl", DATASETS / "known_bugs_v1.jsonl"],
)


def main(argv: list[str]) -> int:
    if argv:
        out, inputs = Path(argv[0]), [Path(a) for a in argv[1:]]
    else:
        out, inputs = DAY4_BASELINE
    try:
        cases = combine_datasets(inputs, out)
    except DuplicateCaseIds as e:
        print(e)
        return 1
    print(f"wrote {len(cases)} cases from {len(inputs)} files to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
