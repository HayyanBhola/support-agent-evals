"""Append-only label storage (``datasets/labels/labels_raw_v1.jsonl``).

Every save appends one Label line (flush + fsync, via ``evalkit.io.append_jsonl``); rows
are never edited in place. For each (case_id, labeler) the latest row in the file wins. A
line cut off by a crash (no trailing newline) is dropped on open, so the file always parses
and labeling resumes from the last complete save.
"""

from pathlib import Path
from typing import Final

from evalkit.io import append_jsonl, drop_partial_last_line, read_jsonl
from evalkit.schemas import Label

DEFAULT_LABELS_DIR: Final = Path("datasets") / "labels"
RAW_FILE: Final = "labels_raw_v1.jsonl"
SAMPLE_PREFIX: Final = "label_sample_"


def raw_file_for(sample: Path) -> str:
    """The raw labels file for a sample file, so each set has its own: label_sample_v1.jsonl
    -> labels_raw_v1.jsonl (Day 9), overlap_v1.jsonl -> labels_raw_overlap_v1.jsonl,
    relabel_v1.jsonl -> labels_raw_relabel_v1.jsonl."""
    return f"labels_raw_{sample.stem.removeprefix(SAMPLE_PREFIX)}.jsonl"


class LabelStore:
    def __init__(self, path: Path = DEFAULT_LABELS_DIR / RAW_FILE) -> None:
        self.path = path
        self.recovered = False  # True if a partial last line was dropped on open
        if path.exists():
            self.recovered = drop_partial_last_line(path)

    def all(self) -> list[Label]:
        """Every saved row, in save order."""
        return list(read_jsonl(self.path, Label)) if self.path.exists() else []

    def latest(self) -> dict[tuple[str, str], Label]:
        """The latest row per (case_id, labeler)."""
        out: dict[tuple[str, str], Label] = {}
        for label in self.all():
            out[(label.case_id, label.labeler)] = label
        return out

    def for_labeler(self, labeler: str) -> dict[str, Label]:
        """The latest row per case_id for one labeler."""
        return {cid: lab for (cid, who), lab in self.latest().items() if who == labeler}

    def append(self, label: Label) -> None:
        append_jsonl(self.path, label)
