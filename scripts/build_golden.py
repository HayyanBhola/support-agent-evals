"""Assemble the golden set: legacy + known bugs + reviewed synthetic cases, then patches.

Usage:
  uv run python scripts/build_golden.py
Writes datasets/golden_v1.jsonl (Day 7, tagged data-v1.0: never changed) ordered by source
(legacy, human, synthetic) then id, and datasets/golden_v1.1.jsonl = golden_v1 with
PATCHES_V1_1 applied (same order), and datasets/golden_v1.2.jsonl = golden_v1.1 with
PATCHES_V1_2 applied. Deterministic: rebuilding gives byte-identical files.
Refuses duplicate ids; every patched case is re-validated as a TestCase.
"""

import sys
from pathlib import Path
from typing import Any

from evalkit.importers.combine import DuplicateCaseIds, build_golden
from evalkit.io import read_jsonl, write_jsonl
from evalkit.schemas import TestCase

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "datasets"
GOLDEN = DATASETS / "golden_v1.jsonl"
GOLDEN_V1_1 = DATASETS / "golden_v1.1.jsonl"
GOLDEN_V1_2 = DATASETS / "golden_v1.2.jsonl"
SOURCES = [
    DATASETS / "legacy_v1.jsonl",
    DATASETS / "known_bugs_v1.jsonl",
    DATASETS / "synthetic_reviewed_v1.jsonl",
]

# case id -> fields to replace. Each patch is documented in the datasets/README.md changelog.
PATCHES_V1_1: dict[str, dict[str, Any]] = {
    # Day 8: the sheet's "costs $1 " lost its trailing space on import and "costs $1" also
    # matches the correct "costs $19.99". Replaced by phrases no longer price can contain.
    "legacy-adv-fake-price": {
        "must_not_include": ["$1.00", "costs $1 now", "costs $1,", "costs $1.", "yes, it costs"],
    },
}


# Day 8 decision: simple out-of-scope questions should not escalate (rubric
# escalation_appropriate). None of these contains a complaint, a request for a human or a
# safety issue (those would keep should_escalate null).
OUT_OF_SCOPE_IDS = (
    "legacy-oos-competitor",
    "legacy-oos-poem",
    "legacy-oos-shoes",
    "legacy-oos-weather",
    "syn-out-of-scope-0199f408",
    "syn-out-of-scope-1d1db350",
    "syn-out-of-scope-1e1ee390",
    "syn-out-of-scope-268b798c",
    "syn-out-of-scope-28e2c9a4",
    "syn-out-of-scope-3c9e8bed",
    "syn-out-of-scope-3d5569c1",
    "syn-out-of-scope-535f83df",
    "syn-out-of-scope-6b06ea9a",
    "syn-out-of-scope-6cdd20dd",
    "syn-out-of-scope-8870b07e",
    "syn-out-of-scope-ae0fa0ba",
    "syn-out-of-scope-bb22494d",
    "syn-out-of-scope-d3e389ac",
    "syn-out-of-scope-fbbd3086",
)
PATCHES_V1_2: dict[str, dict[str, Any]] = {
    case_id: {"should_escalate": False} for case_id in OUT_OF_SCOPE_IDS
}


def apply_patches(cases: list[TestCase], patches: dict[str, dict[str, Any]]) -> list[TestCase]:
    known = {c.id for c in cases}
    unknown = sorted(set(patches) - known)
    if unknown:
        raise KeyError(f"patched case(s) not in the golden set: {unknown}")
    return [
        TestCase.model_validate(c.model_dump(mode="json") | patches[c.id]) if c.id in patches else c
        for c in cases
    ]


def main(out: Path = GOLDEN, out_v1_1: Path = GOLDEN_V1_1, out_v1_2: Path = GOLDEN_V1_2) -> int:
    try:
        cases = build_golden(SOURCES, out)
    except DuplicateCaseIds as e:
        print(e)
        return 1
    print(f"wrote {len(cases)} cases to {out}")
    patched = apply_patches(list(read_jsonl(out, TestCase)), PATCHES_V1_1)
    write_jsonl(out_v1_1, patched)
    print(f"wrote {len(patched)} cases to {out_v1_1} ({len(PATCHES_V1_1)} patched)")
    patched_v1_2 = apply_patches(patched, PATCHES_V1_2)
    write_jsonl(out_v1_2, patched_v1_2)
    print(f"wrote {len(patched_v1_2)} cases to {out_v1_2} ({len(PATCHES_V1_2)} patched)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
