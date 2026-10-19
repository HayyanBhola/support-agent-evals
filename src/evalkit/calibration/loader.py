"""Load the human labels of one split (Day 13), with the test split locked.

``load_labels("dev")`` returns case_id -> Label for the dev split. ``load_labels("test")``
raises ``TestSplitLocked`` unless the call passes ``final=True`` and a non-empty
``reason`` **and** the environment has ``EVALKIT_FINAL_TEST=1``: two independent switches,
one in the code and one in the shell, so a test load can't happen by accident. Every
successful test load appends a line to ``reports/test_access.log`` (UTC time, git commit,
caller, reason), so any peek at the test labels is visible in git.

The split is keyed by case_id: by default the labels come from the file the split
recorded, but any labels file (a later version) can be passed; every case_id of the split
must be in it.
"""

import inspect
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from evalkit.calibration.split import SPLIT_DIR, SPLIT_FILE, SplitName, read_split
from evalkit.io import read_jsonl
from evalkit.schemas import Label

FINAL_ENV: Final = "EVALKIT_FINAL_TEST"
ACCESS_LOG: Final = Path("reports") / "test_access.log"
DEFAULT_SPLIT: Final = SPLIT_DIR / SPLIT_FILE


class TestSplitLocked(PermissionError):
    """The test split was requested without final=True, a reason and EVALKIT_FINAL_TEST=1."""

    __test__ = False  # not a pytest test class


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def _caller() -> str:
    """module:function:line of the code that called load_labels."""
    frame = inspect.stack()[2]
    module = inspect.getmodule(frame.frame)
    name = module.__name__ if module else Path(frame.filename).name
    return f"{name}:{frame.function}:{frame.lineno}"


def load_labels(
    split: SplitName,
    *,
    final: bool = False,
    reason: str | None = None,
    split_path: Path = DEFAULT_SPLIT,
    labels_path: Path | None = None,
    labeler: str | None = None,
    access_log: Path = ACCESS_LOG,
) -> dict[str, Label]:
    """case_id -> Label for every case of ``split`` (see the module docstring)."""
    if split not in ("dev", "test"):
        raise ValueError(f"unknown split {split!r}: dev or test")
    if split == "test":
        missing = [
            what
            for what, ok in (
                ("final=True", final),
                ("a reason", bool(reason and reason.strip())),
                (f"{FINAL_ENV}=1 in the environment", os.environ.get(FINAL_ENV) == "1"),
            )
            if not ok
        ]
        if missing:
            raise TestSplitLocked(
                f"the test split is locked; it needs {', '.join(missing)}. Use dev for "
                "everything except the final, reported evaluation."
            )
    doc = read_split(split_path)
    path = labels_path or split_path.parent.parent / doc.labels_file
    labels = {lab.case_id: lab for lab in read_jsonl(path, Label)
              if labeler is None or lab.labeler == labeler}  # fmt: skip
    ids = doc.ids(split)
    absent = [cid for cid in ids if cid not in labels]
    if absent:
        raise ValueError(f"{len(absent)} {split} case(s) have no label in {path}: {absent[:3]}")
    if split == "test":
        access_log.parent.mkdir(parents=True, exist_ok=True)
        line = "\t".join(
            [
                datetime.now(UTC).isoformat(timespec="seconds"),
                git_commit(),
                _caller(),
                " ".join((reason or "").split()),
            ]
        )
        with access_log.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    return {cid: labels[cid] for cid in ids}
