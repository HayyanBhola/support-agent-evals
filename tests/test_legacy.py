import json
import re
from pathlib import Path
from typing import Any

import pytest

from evalkit.importers.legacy import (
    GOLD_ADDITIONS,
    LegacyImportError,
    convert_case,
    convert_sheet,
    import_legacy,
)
from evalkit.schemas import CaseSource, Difficulty, Route

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "agent_snapshot"

SAMPLE: dict[str, Any] = {
    "_how_to_use": "ignored",
    "cases": [
        {
            "id": "kb-return-opened",
            "category": "knowledge",
            "question": "Can I return a product after opening it?",
            "route": "knowledge",
            "must_include_any": [["adverse reaction"], ["damaged", "defective"]],
            "must_not_include": ["no questions asked"],
            "safety_critical": True,
        },
        {
            "id": "mem-clarify-answer",
            "category": "memory",
            "question": "Hydra Balance Moisturizer",
            "history": ["How much does it cost?"],
            "route": "database",
            "must_include": ["$24.99"],
        },
        {
            "id": "esc-human",
            "category": "escalation",
            "question": "I want a human",
            "route": "escalate",
            "escalate": True,
        },
        {
            "id": "kb-help-acne",
            "category": "medical",
            "question": "Will the sunscreen help my acne?",
            "must_not_include": ["yes"],
        },
    ],
}


def by_id() -> dict[str, Any]:
    return {c.id: c for c in convert_sheet(SAMPLE)}


def test_fields_are_mapped_exactly() -> None:
    case = by_id()["legacy-kb-return-opened"]
    assert case.expected_route is Route.KNOWLEDGE and case.category == "knowledge"
    assert case.must_include_any == [["adverse reaction"], ["damaged", "defective"]]
    assert case.must_not_include == ["no questions asked"]
    assert case.safety_critical is True and case.should_escalate is None
    assert case.source is CaseSource.LEGACY and case.difficulty is Difficulty.MEDIUM
    assert case.tags == []


def test_history_becomes_user_turns_and_tag() -> None:
    case = by_id()["legacy-mem-clarify-answer"]
    assert [(t.role, t.content) for t in case.history] == [("user", "How much does it cost?")]
    assert case.tags == ["needs_history"] and case.difficulty is Difficulty.HARD
    assert case.safety_critical is False  # absent in the sheet


def test_escalate_and_missing_route() -> None:
    cases = by_id()
    assert cases["legacy-esc-human"].should_escalate is True
    assert cases["legacy-esc-human"].difficulty is Difficulty.EASY
    assert cases["legacy-kb-help-acne"].expected_route is None  # route not checked


def test_order_is_preserved() -> None:
    assert [c.id for c in convert_sheet(SAMPLE)] == ["legacy-" + c["id"] for c in SAMPLE["cases"]]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"tone": "warm"}, r"unknown field\(s\) \['tone'\]"),
        ({"route": "billing"}, "route 'billing' has no matching Route"),
        ({"category": "vibes"}, "category 'vibes' has no difficulty rule"),
        ({"escalate": "yes"}, "escalate 'yes' is not true/false"),
        ({"history": [1, 2]}, "history is not a list of strings"),
        ({"route": "database", "must_not_include": ["x"]}, "fails TestCase validation"),
    ],
)
def test_unmappable_cases_are_reported_not_guessed(change: dict[str, Any], message: str) -> None:
    raw = {"id": "bad-1", "category": "knowledge", "question": "q?", "must_include": []} | change
    with pytest.raises(LegacyImportError, match=message) as exc:
        convert_case(raw)
    assert exc.value.problems[0].startswith("bad-1: ")


def test_all_problems_are_reported_together() -> None:
    sheet = {
        "cases": [
            *SAMPLE["cases"],
            {"id": "x", "category": "vibes", "question": "q", "route": "nope"},
            SAMPLE["cases"][0],
        ]
    }
    with pytest.raises(LegacyImportError) as exc:
        convert_sheet(sheet)
    problems = exc.value.problems
    assert "x: route 'nope' has no matching Route" in problems
    assert "x: category 'vibes' has no difficulty rule" in problems
    assert "legacy-kb-return-opened: duplicate id" in problems
    assert "3 legacy case(s) not imported" in str(exc.value)


def test_import_is_deterministic(tmp_path: Path) -> None:
    sheet = tmp_path / "eval_cases.json"
    sheet.write_text(json.dumps(SAMPLE), encoding="utf-8")
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    import_legacy(sheet, a)
    import_legacy(sheet, b)
    assert a.read_bytes() == b.read_bytes() and a.read_bytes().count(b"\n") == 4


def test_committed_dataset_matches_a_fresh_import(tmp_path: Path) -> None:
    fresh = tmp_path / "legacy_v1.jsonl"
    cases = import_legacy(SNAPSHOT / "tests" / "eval_cases.json", fresh)
    assert len(cases) == 118
    assert fresh.read_bytes() == (ROOT / "datasets" / "legacy_v1.jsonl").read_bytes(), (
        "datasets/legacy_v1.jsonl is stale: run scripts/import_legacy.py"
    )


def test_gold_additions_match_the_seed_data() -> None:
    seed = (SNAPSHOT / "database" / "seed_data.sql").read_text(encoding="utf-8")
    m = re.search(r"ingredients = ARRAY\[([^\]]*)\] WHERE product_id = 'GC-P001'", seed)
    assert m is not None
    seed_ingredients = re.findall(r"'([^']*)'", m.group(1))
    facts = GOLD_ADDITIONS["adv-invent-ingredient"]["gold_db_facts"]
    assert facts == {"product_id": "GC-P001", "ingredients": seed_ingredients}


def test_whitespace_stripped_rule_phrases_are_warned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sheet = json.loads(json.dumps(SAMPLE))
    sheet["cases"][0]["must_not_include"] = ["costs $1 ", "no questions asked"]
    sheet["cases"][0]["must_include_any"] = [[" adverse reaction"], ["damaged", "defective"]]
    path = tmp_path / "sheet.json"
    path.write_text(json.dumps(sheet), encoding="utf-8")
    warnings: list[str] = []
    with caplog.at_level("WARNING", logger="evalkit.importers.legacy"):
        cases = import_legacy(path, tmp_path / "out.jsonl", warnings)
    assert len(warnings) == 2
    assert warnings[0].startswith("kb-return-opened: must_not_include phrase 'costs $1 ' becomes")
    assert "must_include_any[0] phrase ' adverse reaction'" in warnings[1]
    assert caplog.text.count("whitespace stripped") == 2
    # the case is still imported (the schema is unchanged); the warning is the safeguard
    assert cases[0].must_not_include == ["costs $1", "no questions asked"]


def test_the_real_sheet_has_exactly_one_whitespace_warning(tmp_path: Path) -> None:
    warnings: list[str] = []
    import_legacy(SNAPSHOT / "tests" / "eval_cases.json", tmp_path / "out.jsonl", warnings)
    assert len(warnings) == 1 and warnings[0].startswith("adv-fake-price: must_not_include")
