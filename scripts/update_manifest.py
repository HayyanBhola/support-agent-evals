# ruff: noqa: E501  (the spec table is data: one line per dataset reads best)
"""Write datasets/MANIFEST.json: sha256, record count, version and sources of every dataset.

Usage:
  uv run python scripts/update_manifest.py
Run it after rebuilding any dataset. A version is a human decision: bump it here when a
file's content changes on purpose (tests fail while a hash is stale).
"""

import sys
from pathlib import Path

from evalkit.manifest import (
    MANIFEST_NAME,
    DatasetSpec,
    build_manifest,
    read_manifest,
    write_manifest,
)

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "datasets"

SPECS = {
    "agent_chunks.jsonl": DatasetSpec(version="1.0", kind="chunks", description="knowledge-base chunks exported from the agent snapshot (54f9ec6)"),
    "legacy_v1.jsonl": DatasetSpec(version="1.0", description="the agent's own eval sheet, imported (Day 4)"),
    "known_bugs_v1.jsonl": DatasetSpec(version="1.0", description="hand-written known bugs and controls (Day 4)"),
    "day4_baseline.jsonl": DatasetSpec(version="1.0", sources=["legacy_v1.jsonl", "known_bugs_v1.jsonl"], description="Day 4 baseline run set"),
    "sample_5.jsonl": DatasetSpec(version="1.0", description="smoke test"),
    "bench_knowledge_10.jsonl": DatasetSpec(version="1.0", description="Day 3 concurrency benchmark"),
    "synthetic_raw_v1.jsonl": DatasetSpec(version="1.0", sources=["agent_chunks.jsonl"], description="gpt-5-mini generation, labels by rule (Day 5)"),
    "synthetic_dedup_v1.jsonl": DatasetSpec(version="1.0", sources=["synthetic_raw_v1.jsonl", "legacy_v1.jsonl", "known_bugs_v1.jsonl"], description="near-duplicates removed (Day 6)"),
    "synthetic_reviewed_v1.jsonl": DatasetSpec(version="1.0", sources=["synthetic_dedup_v1.jsonl"], description="AI first pass + human audit applied (Day 6)"),
    "golden_v1.jsonl": DatasetSpec(version="1.0", sources=["legacy_v1.jsonl", "known_bugs_v1.jsonl", "synthetic_reviewed_v1.jsonl"], description="the golden set (Day 7)"),
    "golden_v1.1.jsonl": DatasetSpec(version="1.1", sources=["golden_v1.jsonl"], description="golden_v1 with legacy-adv-fake-price's must_not_include fixed (Day 8)"),
    "golden_v1.2.jsonl": DatasetSpec(version="1.2", sources=["golden_v1.1.jsonl"], description="golden_v1.1 with should_escalate=false on the 19 out-of-scope cases (Day 8 decision)"),
}  # fmt: skip


def main() -> int:
    path = DATASETS / MANIFEST_NAME
    # entries written by commands (label sample, label export) are kept and re-hashed
    manifest = build_manifest(DATASETS, SPECS, read_manifest(path) if path.exists() else None)
    write_manifest(manifest, DATASETS / "MANIFEST.json")
    print(f"wrote {len(manifest.datasets)} entries to {DATASETS / 'MANIFEST.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
