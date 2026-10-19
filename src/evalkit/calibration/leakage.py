"""Leakage guards for Week 3 (Day 13): the test split never reaches a prompt.

* ``few_shot_examples`` draws prompt / few-shot examples **only** from the dev split and
  the rubric examples, and checks the result against the test split before returning it.
* ``scan`` (``evalkit split check``) looks through prompt and judge files for any test
  case_id, and for any test reply or question text. A text only counts if it has at least
  MIN_TEXT_CHARS characters and is **unique to test**: not also a reply or question of a
  non-test case, and not quoted in the rubric files. The fixed replies (the not_found text,
  greetings) are shared across splits and rubric examples and would otherwise flag every
  prompt that mentions them. Texts are compared case-insensitively with whitespace
  collapsed, so re-wrapping a pasted reply doesn't hide it.
"""

import random
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from evalkit.calibration.split import SplitFile
from evalkit.rubrics import criterion_files
from evalkit.schemas import LabelSample

MIN_TEXT_CHARS: Final = 40
SCAN_DIRS: Final = (Path("prompts"), Path("src") / "evalkit" / "judges")
SKIP_SUFFIXES: Final = frozenset({".pyc", ".png", ".jpg", ".xlsx", ".zip"})
Source = Literal["dev", "rubric", "both"]
TextKind = Literal["reply", "question"]


class LeakageError(AssertionError):
    """A test case_id would reach a prompt."""


def norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def assert_no_test(ids: Iterable[str], split: SplitFile) -> None:
    leaked = sorted(set(ids) & set(split.ids("test")))
    if leaked:
        raise LeakageError(f"test case_ids must never be used as examples: {leaked}")


def example_pool(
    split: SplitFile, rubric_examples: Iterable[str], source: Source = "both"
) -> list[str]:
    """The case_ids prompts may draw examples from: dev and/or the rubric examples."""
    pool: set[str] = set()
    if source in ("dev", "both"):
        pool |= set(split.ids("dev"))
    if source in ("rubric", "both"):
        pool |= set(rubric_examples)
    assert_no_test(pool, split)
    return sorted(pool)


def few_shot_examples(
    k: int,
    split: SplitFile,
    rubric_examples: Iterable[str],
    *,
    seed: int = 42,
    source: Source = "both",
    restrict_to: Iterable[str] | None = None,
) -> list[str]:
    """``k`` example case_ids (fewer if the pool is smaller) from the allowed pool, optionally
    restricted (e.g. to cases with a fail); never a test case_id."""
    pool = example_pool(split, rubric_examples, source)
    if restrict_to is not None:
        allowed = set(restrict_to)
        pool = [cid for cid in pool if cid in allowed]
    picked = random.Random(seed).sample(pool, min(k, len(pool)))
    assert_no_test(picked, split)
    return picked


# --------------------------------------------------------------------------- scan


@dataclass(frozen=True)
class Marker:
    kind: Literal["case_id", "reply", "question"]
    case_id: str
    text: str  # the case_id, or the normalised text


@dataclass(frozen=True)
class Hit:
    path: Path
    line: int | None  # None for a text found across lines
    marker: Marker

    def __str__(self) -> str:
        where = f"{self.path.as_posix()}:{self.line}" if self.line else self.path.as_posix()
        return f"{where}: test {self.marker.kind} of {self.marker.case_id}"


def markers(split: SplitFile, samples: Mapping[str, LabelSample], rubric_dir: Path) -> list[Marker]:
    """What a prompt must not contain: every test case_id, and every test reply / question
    that is long enough and unique to test."""
    test = set(split.ids("test"))
    shared = {
        norm(t)
        for cid, s in samples.items()
        if cid not in test
        for t in (s.response.reply, s.case.question)
    }
    rubric_text = " ".join(
        norm(p.read_text(encoding="utf-8")) for p in criterion_files(rubric_dir) if p.exists()
    )
    out = [Marker("case_id", cid, cid) for cid in sorted(test)]
    for cid in sorted(test):
        if cid not in samples:
            continue
        texts: tuple[tuple[TextKind, str], ...] = (
            ("reply", samples[cid].response.reply),
            ("question", samples[cid].case.question),
        )
        for kind, raw in texts:
            text = norm(raw)
            if len(text) >= MIN_TEXT_CHARS and text not in shared and text not in rubric_text:
                out.append(Marker(kind, cid, text))
    return out


def files_to_scan(roots: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if root.is_file():
            out.append(root)
        elif root.is_dir():
            out += [
                p
                for p in sorted(root.rglob("*"))
                if p.is_file() and p.suffix not in SKIP_SUFFIXES and "__pycache__" not in p.parts
            ]
    return out


def scan(paths: Iterable[Path], found: Iterable[Marker]) -> list[Hit]:
    """Every marker found in ``paths``: case_ids by line, texts in the whole file."""
    ids = [m for m in found if m.kind == "case_id"]
    texts = [m for m in found if m.kind != "case_id"]
    id_patterns = [(m, re.compile(rf"(?<![\w-]){re.escape(m.text)}(?![\w-])")) for m in ids]
    hits: list[Hit] = []
    for path in paths:
        content = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(content.splitlines(), start=1):
            hits += [Hit(path, lineno, m) for m, pattern in id_patterns if pattern.search(line)]
        whole = norm(content)
        hits += [Hit(path, None, m) for m in texts if m.text in whole]
    return hits
