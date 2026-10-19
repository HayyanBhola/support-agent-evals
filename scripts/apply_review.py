"""Apply the filled-in review workbook to the synthetic cases (Day 6).

Usage:
  uv run python scripts/apply_review.py --decisions review/ai_review_decisions.csv \
      --audit review/human_audit.md          # Claude's first pass + the human audit
  uv run python scripts/apply_review.py [WORKBOOK] [DATASET] [--bulk-accepted 2,3,4]
In the --decisions mode the audit is required: every audited verdict must be filled in,
and the log records the review method (AI first pass, human audit, unaudited keeps).
Defaults: datasets/review_queue_v1.xlsx and datasets/synthetic_dedup_v1.jsonl.
--bulk-accepted lists the review priorities that were accepted in bulk without a detailed
look: their kept cases are tagged review_bulk_accepted and the log says so. Writes
datasets/synthetic_reviewed_v1.jsonl and datasets/review_log_v1.md, or nothing at all (exit
1) if any row has no decision, a label change has no note, or a fixed case is invalid.
"""

import argparse
import sys
from pathlib import Path

from evalkit.importers.chunks import ChunkRecord
from evalkit.io import read_jsonl, write_jsonl
from evalkit.review.apply import (
    ReviewError,
    apply_audit,
    apply_review,
    read_decisions_csv,
    read_rows,
    render_log,
    render_method,
    review_tags,
)
from evalkit.review.audit import AuditError, parse_audit
from evalkit.schemas import TestCase

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "datasets"
AUDIT_SEED_NOTE = "random.Random(6), see scripts/build_ai_review.py"


def chunk_ids() -> frozenset[str]:
    return frozenset(c.id for c in read_jsonl(DATASETS / "agent_chunks.jsonl", ChunkRecord))


def main(argv: list[str], out_dir: Path = DATASETS) -> int:
    parser = argparse.ArgumentParser(description="Apply the review workbook.")
    parser.add_argument("workbook", nargs="?", default=DATASETS / "review_queue_v1.xlsx")
    parser.add_argument("dataset", nargs="?", default=DATASETS / "synthetic_dedup_v1.jsonl")
    parser.add_argument("--bulk-accepted", default="", help="priorities accepted in bulk")
    parser.add_argument("--decisions", type=Path, help="Claude's first-pass decisions (CSV)")
    parser.add_argument("--audit", type=Path, help="the filled-in human audit (Markdown)")
    args = parser.parse_args(argv)
    bulk = frozenset(int(p) for p in args.bulk_accepted.split(",") if p.strip())
    cases = list(read_jsonl(Path(args.dataset), TestCase))
    method: list[str] = []
    try:
        if args.decisions is not None:
            if args.audit is None:
                print("not applied: --decisions needs --audit (the human audit file)")
                return 1
            ai_rows = read_decisions_csv(args.decisions)
            outcome = apply_audit(ai_rows, parse_audit(args.audit.read_text(encoding="utf-8")))
            result = apply_review(
                cases, outcome.rows, extra_tags=review_tags(outcome), chunk_ids=chunk_ids()
            )
            method = render_method(ai_rows, outcome, AUDIT_SEED_NOTE)
        else:
            result = apply_review(cases, read_rows(Path(args.workbook)), bulk)
    except (ReviewError, AuditError) as e:
        print(f"not applied: {e}")
        return 1
    out = out_dir / "synthetic_reviewed_v1.jsonl"
    write_jsonl(out, result.cases)
    log = out_dir / "review_log_v1.md"
    log.write_text(
        "# Review log v1\n\n" + render_log(result, method), encoding="utf-8", newline="\n"
    )
    print(f"wrote {len(result.cases)} cases to {out} and the log to {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
