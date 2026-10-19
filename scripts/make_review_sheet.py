"""Build the human review workbook for the deduplicated synthetic cases (Day 6).

Usage:
  uv run python scripts/make_review_sheet.py
Reads datasets/synthetic_dedup_v1.jsonl, runs the snapshot router offline and writes
datasets/review_queue_v1.xlsx. No LLM calls.
"""

import sys
from collections import Counter
from pathlib import Path

from evalkit.io import read_jsonl
from evalkit.review.workbook import build_rows, write_workbook
from evalkit.schemas import TestCase
from evalkit.snapshot_router import load_router

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "datasets"


def main() -> int:
    cases = list(read_jsonl(DATASETS / "synthetic_dedup_v1.jsonl", TestCase))
    rows = build_rows(cases, load_router(ROOT / "agent_snapshot"))
    out = DATASETS / "review_queue_v1.xlsx"
    write_workbook(rows, out)
    per_priority = sorted(Counter(r.priority for r in rows).items())
    print(f"wrote {len(rows)} rows to {out}; rows per priority: {dict(per_priority)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
