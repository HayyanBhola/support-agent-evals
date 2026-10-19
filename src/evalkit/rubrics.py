"""Rubric files (``rubrics/<criterion>.md``): loading, validation and ``rubric_hash``.

A rubric file is Markdown with YAML frontmatter::

    ---
    id: faithfulness            # a Criterion; must match the file name
    version: 1
    verdicts: [pass, fail, n_a]
    applies_by: actual          # actual = judge the reply/route the agent gave;
                                # expected = decided by the case (expected route / label)
    applies_when: route         # route = in scope by applies_to.routes;
                                # has_gold = also any case with reference_answer/gold_db_facts
    applies_to:
      routes: [knowledge, database]
      answer_types: [llm, quoted_source, not_found, none]   # none = no answer_type
    ---

and a body with a ``**Definition:**`` line and the sections ``## Key rules`` (KEY_RULES_MIN
to KEY_RULES_MAX ``- `` bullets: the decisions most likely to be missed; the labeling app
shows them at the top of its sidebar), ``## Pass if``, ``## Fail if``, ``## Edge cases``
(each with at least one ``- `` bullet), ``## Pass examples`` and ``## Fail examples``.
Examples are ``### case_id: <id>`` headings; a section with fewer than MIN_EXAMPLES needs
an explicit ``Shortage:`` line. Every example case_id must exist in the
dataset, and ``rubrics/examples_used.json`` must list exactly the example case_ids.

``rubric_hash`` is sha256 over the sorted criterion files (newlines normalised to LF, so a
Windows checkout hashes the same); labels and judge results record it.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from evalkit.hashing import stable_hash
from evalkit.io import read_jsonl
from evalkit.schemas import Criterion, LabelValue, Route, Sha256Hex, TestCase

DEFAULT_DIR: Final = Path("rubrics")
DEFAULT_DATASET: Final = Path("datasets") / "golden_v1.2.jsonl"
EXAMPLES_FILE: Final = "examples_used.json"
MIN_EXAMPLES: Final = 3
# Day 8 decision (hybrid applicability): faithfulness, tone, safety and relevance judge the
# reply the agent actually gave; correctness and completeness are decided by the expected
# route (a misrouted question that got a fixed reply is a fail, not n_a); escalation is
# decided by the case's should_escalate label.
AGREED_APPLIES_BY: Final[dict[Criterion, str]] = {
    Criterion.FAITHFULNESS: "actual",
    Criterion.TONE: "actual",
    Criterion.SAFETY: "actual",
    Criterion.RELEVANCE: "actual",
    Criterion.CORRECTNESS: "expected",
    Criterion.COMPLETENESS: "expected",
    Criterion.ESCALATION_APPROPRIATE: "expected",
}
# Day 8 decision: correctness applies to every case with a reference_answer or
# gold_db_facts (whatever its expected route) as well as to expected knowledge/database
# cases; every other criterion is scoped by its routes alone.
AGREED_APPLIES_WHEN: Final[dict[Criterion, str]] = {
    c: ("has_gold" if c is Criterion.CORRECTNESS else "route") for c in Criterion
}
REQUIRED_SECTIONS: Final = (
    "Key rules",
    "Pass if",
    "Fail if",
    "Edge cases",
    "Pass examples",
    "Fail examples",
)
BULLET_SECTIONS: Final = ("Key rules", "Pass if", "Fail if", "Edge cases")
KEY_RULES: Final = "Key rules"
KEY_RULES_MIN: Final = 3
KEY_RULES_MAX: Final = 5
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_SECTION = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
_EXAMPLE = re.compile(r"^### case_id:\s*(\S+)\s*$", re.MULTILINE)
_DEFINITION = re.compile(r"^\*\*Definition:\*\*\s*(\S.*)$", re.MULTILINE)
_BULLET = re.compile(r"^- (\S.*)$", re.MULTILINE)


class RubricError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(
            f"{len(problems)} rubric problem(s):\n" + "\n".join(f"- {p}" for p in problems)
        )
        self.problems = problems


class AppliesTo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    routes: list[Route]
    answer_types: list[Literal["llm", "quoted_source", "not_found", "none"]]

    @field_validator("routes", "answer_types")
    @classmethod
    def _non_empty(cls, v: list[Any]) -> list[Any]:
        if not v:
            raise ValueError("must not be empty")
        return v


class RubricMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Criterion
    version: int
    verdicts: list[LabelValue]
    applies_by: Literal["actual", "expected"]
    applies_when: Literal["route", "has_gold"]
    applies_to: AppliesTo

    @field_validator("version")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("version must be at least 1")
        return v

    @field_validator("verdicts")
    @classmethod
    def _all_verdicts(cls, v: list[LabelValue]) -> list[LabelValue]:
        if sorted(v) != sorted(LabelValue):
            raise ValueError("verdicts must be exactly [pass, fail, n_a]")
        return v


@dataclass(frozen=True)
class Rubric:
    meta: RubricMeta
    definition: str
    sections: dict[str, str]
    pass_examples: list[str]
    fail_examples: list[str]
    shortages: dict[str, str] = field(default_factory=dict)  # section -> shortage note

    @property
    def criterion(self) -> Criterion:
        return self.meta.id

    @property
    def key_rules(self) -> list[str]:
        return _BULLET.findall(self.sections.get(KEY_RULES, ""))


def criterion_files(rubric_dir: Path) -> list[Path]:
    return [rubric_dir / f"{c.value}.md" for c in Criterion]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def parse_rubric(
    path: Path, known_ids: frozenset[str] | None = None
) -> tuple[Rubric | None, list[str]]:
    """One rubric file -> (rubric, problems). ``rubric`` is None if it can't be parsed."""
    name = path.name
    text = _read(path)
    m = _FRONTMATTER.match(text)
    if m is None:
        return None, [f"{name}: missing YAML frontmatter (--- ... ---) at the top"]
    try:
        raw = yaml.safe_load(m.group(1))
        meta = RubricMeta.model_validate(raw)
    except yaml.YAMLError as e:
        return None, [f"{name}: frontmatter is not valid YAML: {e}"]
    except ValidationError as e:
        errs = "; ".join(
            f"{'.'.join(map(str, x['loc'])) or 'frontmatter'}: {x['msg']}" for x in e.errors()
        )
        return None, [f"{name}: bad frontmatter: {errs}"]
    problems: list[str] = []
    if meta.id.value != path.stem:
        problems.append(f"{name}: id {meta.id.value!r} doesn't match the file name")
    for key, agreed_map in (
        ("applies_by", AGREED_APPLIES_BY),
        ("applies_when", AGREED_APPLIES_WHEN),
    ):
        value, agreed = getattr(meta, key), agreed_map[meta.id]
        if value != agreed:
            problems.append(
                f"{name}: {key} is {value!r}, but the agreed rule for {meta.id.value} is {agreed!r}"
            )
    body = text[m.end() :]
    d = _DEFINITION.search(body)
    if d is None:
        problems.append(f"{name}: missing a one-line '**Definition:** ...'")
    heads = list(_SECTION.finditer(body))
    sections = {
        h.group(1): body[h.end() : heads[i + 1].start() if i + 1 < len(heads) else len(body)]
        for i, h in enumerate(heads)
    }
    for s in REQUIRED_SECTIONS:
        if s not in sections:
            problems.append(f"{name}: missing section '## {s}'")
    for s in BULLET_SECTIONS:
        if s in sections and not re.search(r"^- \S", sections[s], re.MULTILINE):
            problems.append(f"{name}: section '## {s}' has no '- ' rule")
    n_key = len(_BULLET.findall(sections.get(KEY_RULES, "")))
    if n_key and not KEY_RULES_MIN <= n_key <= KEY_RULES_MAX:
        problems.append(
            f"{name}: '## {KEY_RULES}' has {n_key} rules; needs {KEY_RULES_MIN} to {KEY_RULES_MAX}"
        )
    examples: dict[str, list[str]] = {}
    shortages: dict[str, str] = {}
    for s in ("Pass examples", "Fail examples"):
        ids = _EXAMPLE.findall(sections.get(s, ""))
        examples[s] = ids
        note = re.search(r"^Shortage:\s*(\S.*)$", sections.get(s, ""), re.MULTILINE)
        if note:
            shortages[s] = note.group(1)
        if s in sections and len(ids) < MIN_EXAMPLES and note is None:
            problems.append(
                f"{name}: '## {s}' has {len(ids)} example(s); "
                f"needs {MIN_EXAMPLES} or a 'Shortage:' note"
            )
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            problems.append(f"{name}: '## {s}' repeats {dupes}")
        if known_ids is not None:
            problems += [
                f"{name}: example case_id {i!r} is not in the dataset"
                for i in ids
                if i not in known_ids
            ]
    rubric = Rubric(
        meta=meta,
        definition=d.group(1).strip() if d else "",
        sections=sections,
        pass_examples=examples["Pass examples"],
        fail_examples=examples["Fail examples"],
        shortages=shortages,
    )
    return rubric, problems


def rubric_hash(rubric_dir: Path = DEFAULT_DIR) -> str:
    """sha256 over the sorted criterion files' names and LF-normalised contents."""
    files = sorted(criterion_files(rubric_dir), key=lambda p: p.name)
    return stable_hash(
        [[p.name, hashlib.sha256(_read(p).encode("utf-8")).hexdigest()] for p in files]
    )


def load_rubrics(
    rubric_dir: Path = DEFAULT_DIR, dataset: Path | None = DEFAULT_DATASET
) -> list[Rubric]:
    """Every criterion's rubric, validated; raises RubricError listing every problem."""
    known = frozenset(c.id for c in read_jsonl(dataset, TestCase)) if dataset else None
    problems: list[str] = []
    rubrics: list[Rubric] = []
    for path in criterion_files(rubric_dir):
        if not path.exists():
            problems.append(f"{path.name}: missing (one file per criterion)")
            continue
        rubric, found = parse_rubric(path, known)
        problems += found
        if rubric is not None:
            rubrics.append(rubric)
    problems += _check_examples_file(rubric_dir, rubrics)
    if problems:
        raise RubricError(problems)
    return rubrics


def example_ids(rubrics: list[Rubric]) -> list[str]:
    return sorted({i for r in rubrics for i in (*r.pass_examples, *r.fail_examples)})


def _check_examples_file(rubric_dir: Path, rubrics: list[Rubric]) -> list[str]:
    path = rubric_dir / EXAMPLES_FILE
    if not path.exists():
        return [f"{EXAMPLES_FILE}: missing"]
    try:
        listed = json.loads(path.read_text(encoding="utf-8"))["case_ids"]
    except (ValueError, KeyError, TypeError) as e:
        return [f"{EXAMPLES_FILE}: unreadable ({e!r}); needs a 'case_ids' list"]
    expected = example_ids(rubrics)
    if sorted(listed) != expected:
        missing = sorted(set(expected) - set(listed))
        extra = sorted(set(listed) - set(expected))
        return [f"{EXAMPLES_FILE}: case_ids out of date (missing {missing}, extra {extra})"]
    return []


_ORDER_ITEM = re.compile(r"^\d+\.\s+\*\*([a-z_]+)\*\*", re.MULTILINE)


def judging_order(rubric_dir: Path = DEFAULT_DIR) -> list[Criterion]:
    """The criteria in the order of rubrics/README.md's "(c) Judging order" list."""
    text = _read(rubric_dir / "README.md")
    m = re.search(r"^## \(c\) Judging order\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if m is None:
        raise RubricError(["README.md: missing section '## (c) Judging order'"])
    names = _ORDER_ITEM.findall(m.group(1))
    try:
        order = [Criterion(n) for n in names]
    except ValueError as e:
        raise RubricError([f"README.md: judging order names an unknown criterion ({e})"]) from e
    if sorted(order) != sorted(Criterion):
        raise RubricError([f"README.md: judging order must list every criterion once, got {names}"])
    return order


# --------------------------------------------------------------------------- versions

VERSIONS_FILE: Final = "versions.json"


class LabelState(StrEnum):
    """How a label's rubric_hash relates to the current rubric."""

    CURRENT = "current"  # made under the current rubric
    CARRIED = "carried"  # an older rubric the current one carries forward (still valid)
    RECHECK = "recheck"  # an older rubric, and the case is on a carry-forward re-check list
    STALE = "stale"  # any other older rubric: re-check it


class CarryForward(BaseModel):
    """A version that keeps labels made under ``from_hash`` valid, except ``recheck``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_hash: Sha256Hex
    changed_criteria: list[Criterion]
    recheck: list[str]
    note: str


class RubricVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    day: int
    rubric_hash: Sha256Hex
    carry_forward: CarryForward | None = None


class RubricVersions(BaseModel):
    """``rubrics/versions.json``: every rubric version and what each carries forward."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    versions: list[RubricVersion]

    def get(self, rubric_hash: str) -> RubricVersion | None:
        return next((v for v in self.versions if v.rubric_hash == rubric_hash), None)

    def label_state(self, label_hash: str, case_id: str, current_hash: str) -> LabelState:
        """Walk back from the current version along carry_forward links: a label made under
        a version reached this way is still valid unless its case is on a re-check list of a
        version in between."""
        if label_hash == current_hash:
            return LabelState.CURRENT
        recheck: set[str] = set()
        version = self.get(current_hash)
        while version is not None and version.carry_forward is not None:
            recheck |= set(version.carry_forward.recheck)
            if version.carry_forward.from_hash == label_hash:
                return LabelState.RECHECK if case_id in recheck else LabelState.CARRIED
            version = self.get(version.carry_forward.from_hash)
        return LabelState.STALE

    def carried_note(self, current_hash: str) -> str | None:
        """The current version's carry-forward note, for validate/export output."""
        version = self.get(current_hash)
        if version is None or version.carry_forward is None:
            return None
        cf = version.carry_forward
        changed = ", ".join(c.value for c in cf.changed_criteria)
        return (
            f"rubric v{version.version} carries forward labels made under "
            f"{cf.from_hash[:12]} (changed: {changed}; {len(cf.recheck)} case(s) to re-check). "
            f"{cf.note}"
        )


def load_versions(rubric_dir: Path = DEFAULT_DIR) -> RubricVersions | None:
    """``versions.json`` if present (without it every older label is STALE)."""
    path = rubric_dir / VERSIONS_FILE
    if not path.exists():
        return None
    return RubricVersions.model_validate_json(path.read_text(encoding="utf-8"))
