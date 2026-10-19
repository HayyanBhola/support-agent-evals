"""agent_snapshot/tests/eval_cases.json -> datasets/legacy_v1.jsonl.

Usage: uv run python scripts/import_legacy.py
Deterministic: re-running it on the same snapshot gives a byte-identical file.
"""

import sys
from collections import Counter
from pathlib import Path

from evalkit.importers.legacy import LegacyImportError, import_legacy

ROOT = Path(__file__).resolve().parent.parent
SHEET = ROOT / "agent_snapshot" / "tests" / "eval_cases.json"
OUT = ROOT / "datasets" / "legacy_v1.jsonl"


def main() -> int:
    warnings: list[str] = []
    try:
        cases = import_legacy(SHEET, OUT, warnings)
    except LegacyImportError as e:
        print(e)
        return 1
    print(f"wrote {len(cases)} cases to {OUT.relative_to(ROOT)}")
    routes = Counter(c.expected_route.value if c.expected_route else "(unchecked)" for c in cases)
    print("by expected_route:", dict(sorted(routes.items())))
    print("by category:", dict(sorted(Counter(c.category for c in cases).items())))
    tags = Counter(t for c in cases for t in c.tags)
    print("tags:", dict(sorted(tags.items())))
    print(f"warnings: {len(warnings)}")
    for warning in warnings:
        print(f"  WARNING {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
