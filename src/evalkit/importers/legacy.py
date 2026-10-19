"""Convert the agent's legacy test sheet (tests/eval_cases.json) to TestCases.

Field meanings come from the snapshot's tests/run_eval.py:

* ``route`` / ``escalate`` are checked only when present -> ``expected_route`` null /
  ``should_escalate`` null when absent.
* ``must_include`` (all), ``must_include_any`` (one from EACH group), ``must_not_include``:
  case-insensitive substring checks, identical to ours, so they are copied verbatim.
* ``history``: earlier customer messages, replayed before the question -> user Turns, plus
  the ``needs_history`` tag (eval_mode can't replay history yet, so the runner skips them).
* ``safety_critical``: present only as true -> absent means False.

Nothing is guessed: unknown fields, routes or categories, and cases that fail the TestCase
validators, are collected and raised together as a LegacyImportError.

Whitespace: TestCase strips leading/trailing whitespace from every string, which can change
what a substring rule matches (Day 8: the sheet's ``"costs $1 "`` became ``"costs $1"``,
which also matches ``"costs $19.99"``). Every rule phrase that stripping changes is logged
as a warning and returned in the import report, so it is never silent.
"""

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evalkit.io import write_jsonl
from evalkit.schemas import CaseSource, Difficulty, Route, TestCase

log = logging.getLogger(__name__)

ID_PREFIX = "legacy-"
NEEDS_HISTORY_TAG = "needs_history"

KNOWN_FIELDS = frozenset(
    {
        "id",
        "category",
        "question",
        "route",
        "escalate",
        "history",
        "must_include",
        "must_include_any",
        "must_not_include",
        "safety_critical",
    }
)

# Legacy category -> difficulty (documented in datasets/README.md).
DIFFICULTY_BY_CATEGORY: dict[str, Difficulty] = {
    "greeting": Difficulty.EASY,
    "database": Difficulty.EASY,
    "clarify": Difficulty.EASY,
    "escalation": Difficulty.EASY,
    "knowledge": Difficulty.MEDIUM,
    "medical": Difficulty.MEDIUM,
    "out_of_scope": Difficulty.MEDIUM,
    "robustness": Difficulty.MEDIUM,
    "adversarial": Difficulty.HARD,
    "memory": Difficulty.HARD,
}

# Additions agreed on Day 4, applied on top of the legacy data (and tagged, so they're
# visible). adv-invent-ingredient is a database case with only must_not_include, which our
# validator rejects; its gold facts come from agent_snapshot/database/seed_data.sql.
GOLD_ADDITIONS: dict[str, dict[str, Any]] = {
    "adv-invent-ingredient": {
        "gold_db_facts": {
            "product_id": "GC-P001",
            "ingredients": [
                "Water",
                "Glycerin",
                "Hyaluronic Acid",
                "Niacinamide",
                "Dimethicone",
                "Phenoxyethanol",
            ],
        },
        "tag": "gold_added_day4",
    },
}


class LegacyImportError(ValueError):
    """One or more legacy cases could not be mapped. ``problems`` lists them all."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__(
            f"{len(problems)} legacy case(s) not imported:\n  " + "\n  ".join(problems)
        )
        self.problems = list(problems)


def _problems_with_fields(raw: Mapping[str, Any]) -> list[str]:
    problems = []
    unknown = sorted(set(raw) - KNOWN_FIELDS)
    if unknown:
        problems.append(f"unknown field(s) {unknown}")
    route = raw.get("route")
    if route is not None and route not in {r.value for r in Route}:
        problems.append(f"route {route!r} has no matching Route")
    if raw.get("category") not in DIFFICULTY_BY_CATEGORY:
        problems.append(f"category {raw.get('category')!r} has no difficulty rule")
    if "escalate" in raw and not isinstance(raw["escalate"], bool):
        problems.append(f"escalate {raw['escalate']!r} is not true/false")
    history = raw.get("history", [])
    if not isinstance(history, list) or not all(isinstance(h, str) for h in history):
        problems.append("history is not a list of strings")
    return problems


def whitespace_warnings(raw: Mapping[str, Any]) -> list[str]:
    """Rule phrases whose leading/trailing whitespace TestCase will strip (changing the rule)."""
    case_id = str(raw.get("id"))
    phrases: list[tuple[str, Any]] = [
        (field, s) for field in ("must_include", "must_not_include") for s in raw.get(field, [])
    ]
    phrases += [
        (f"must_include_any[{i}]", s)
        for i, group in enumerate(raw.get("must_include_any", []))
        for s in group
    ]
    return [
        f"{case_id}: {field} phrase {s!r} becomes {s.strip()!r} (whitespace stripped; "
        "the substring rule now matches more or less than intended)"
        for field, s in phrases
        if isinstance(s, str) and s != s.strip()
    ]


def convert_case(raw: Mapping[str, Any]) -> TestCase:
    """One legacy case -> TestCase. Raises LegacyImportError (with the case id) on problems."""
    case_id = str(raw.get("id"))
    problems = _problems_with_fields(raw)
    if problems:
        raise LegacyImportError([f"{case_id}: {p}" for p in problems])
    history = raw.get("history", [])
    tags = [NEEDS_HISTORY_TAG] if history else []
    data: dict[str, Any] = {
        "id": ID_PREFIX + case_id,
        "question": raw["question"],
        "category": raw["category"],
        "expected_route": raw.get("route"),
        "should_escalate": raw.get("escalate"),
        "history": [{"role": "user", "content": h} for h in history],
        "must_include": raw.get("must_include", []),
        "must_include_any": raw.get("must_include_any", []),
        "must_not_include": raw.get("must_not_include", []),
        "safety_critical": bool(raw.get("safety_critical", False)),
        "difficulty": DIFFICULTY_BY_CATEGORY[raw["category"]],
        "source": CaseSource.LEGACY,
    }
    addition = GOLD_ADDITIONS.get(case_id)
    if addition is not None:
        data["gold_db_facts"] = addition["gold_db_facts"]
        tags.append(addition["tag"])
    data["tags"] = tags
    try:
        return TestCase.model_validate(data)
    except ValidationError as e:
        reasons = "; ".join(err["msg"] for err in e.errors())
        raise LegacyImportError([f"{case_id}: fails TestCase validation: {reasons}"]) from e


def convert_sheet(sheet: Mapping[str, Any], warnings: list[str] | None = None) -> list[TestCase]:
    """All cases of a legacy sheet, in their original order. Reports every problem at once.
    Whitespace warnings are logged and, if ``warnings`` is given, appended to it."""
    cases, problems = [], []
    for raw in sheet["cases"]:
        for warning in whitespace_warnings(raw):
            log.warning("%s", warning)
            if warnings is not None:
                warnings.append(warning)
        try:
            cases.append(convert_case(raw))
        except LegacyImportError as e:
            problems.extend(e.problems)
    ids = [c.id for c in cases]
    problems += [f"{i}: duplicate id" for i in sorted({i for i in ids if ids.count(i) > 1})]
    if problems:
        raise LegacyImportError(problems)
    return cases


def import_legacy(
    sheet_path: Path, out_path: Path, warnings: list[str] | None = None
) -> list[TestCase]:
    """Read ``sheet_path`` (eval_cases.json), write ``out_path`` (JSONL), return the cases.
    Import warnings (see ``whitespace_warnings``) are appended to ``warnings`` if given."""
    sheet = json.loads(sheet_path.read_text(encoding="utf-8-sig"))
    cases = convert_sheet(sheet, warnings)
    write_jsonl(out_path, cases)
    return cases
