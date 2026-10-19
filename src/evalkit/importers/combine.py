"""Concatenate TestCase datasets into one file (the runner takes a single --dataset)."""

from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from evalkit.io import read_jsonl, write_jsonl
from evalkit.schemas import TestCase


class DuplicateCaseIds(ValueError):
    pass


def _load_unique(inputs: Sequence[Path]) -> list[TestCase]:
    cases = [case for path in inputs for case in read_jsonl(path, TestCase)]
    dupes = sorted(i for i, n in Counter(c.id for c in cases).items() if n > 1)
    if dupes:
        raise DuplicateCaseIds(f"case ids appear more than once across {list(inputs)}: {dupes}")
    return cases


def combine_datasets(inputs: Sequence[Path], out_path: Path) -> list[TestCase]:
    """Write every case of ``inputs``, in order, to ``out_path``. Ids must be unique overall."""
    cases = _load_unique(inputs)
    write_jsonl(out_path, cases)
    return cases


SOURCE_ORDER = ("legacy", "human", "synthetic", "production")


def build_golden(inputs: Sequence[Path], out_path: Path) -> list[TestCase]:
    """Like ``combine_datasets``, but in a fixed order: by source (legacy, human, synthetic,
    production), then by id, so the file is identical whatever order the inputs come in."""
    cases = _load_unique(inputs)
    cases.sort(key=lambda c: (SOURCE_ORDER.index(c.source.value), c.id))
    write_jsonl(out_path, cases)
    return cases
