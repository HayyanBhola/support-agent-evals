"""Content-rule checks (evalkit.metrics.rules) and the price-fragment lint."""

from typing import Any

import pytest

from evalkit.metrics.rules import (
    check_rules,
    expected_prices,
    has_content_rules,
    price_fragment_conflicts,
)
from evalkit.schemas import TestCase


def case(**fields: Any) -> TestCase:
    data: dict[str, Any] = {
        "id": "c1",
        "question": "How much is the serum?",
        "category": "database",
        "expected_route": "database",
        "source": "human",
        "must_include": ["$19.99"],
    }
    return TestCase.model_validate(data | fields)


def test_check_rules_semantics_match_run_eval() -> None:
    c = case(
        must_include=["$19.99", "Serum"],
        must_include_any=[["in stock", "available"], ["today"]],
        must_not_include=["Out of Stock", "costs $1."],
    )
    r = check_rules(c, "The SERUM costs $19.99 and is available. It is out of stock today?")
    assert r.missing == [] and r.missing_groups == []
    assert r.forbidden == ["Out of Stock"]  # case-insensitive substring
    assert not r.passed
    r2 = check_rules(c, "It costs $19.99.")
    assert r2.missing == ["Serum"]
    assert r2.missing_groups == [["in stock", "available"], ["today"]]
    assert check_rules(case(), "The Niacinamide Serum costs $19.99.").passed


def test_has_content_rules() -> None:
    assert has_content_rules(case())
    assert not has_content_rules(
        case(must_include=[], gold_db_facts={"price": 19.99}, expected_route="database")
    )


def test_expected_prices() -> None:
    c = case(must_include=["$19.99"], must_include_any=[["$4.99", "free"]],
             gold_db_facts={"price": 16.0})  # fmt: skip
    assert expected_prices(c) == {"19.99", "4.99", "16.00", "16"}
    assert expected_prices(case(gold_db_facts={"price": True})) == {"19.99"}


# --------------------------------------------------------------------------- regression


def test_price_fragment_that_matches_a_longer_valid_price_is_flagged() -> None:
    """Day 8 regression: "costs $1" forbade the fake $1 but also matched the real $19.99."""
    bad = case(must_not_include=["$1.00", "costs $1", "costs $1.", "yes, it costs"])
    assert price_fragment_conflicts(bad) == ["c1: must_not_include 'costs $1' matches $19.99"]
    assert not check_rules(bad, "The Niacinamide Serum costs $19.99.").passed


@pytest.mark.parametrize(
    ("expected", "phrase"),
    [
        ("$19.99", p)
        for p in ("costs $1 now", "costs $1,", "costs $1.", "$1.00", "yes, it costs", "costs $2")
    ]
    + [("$24.99", "$19.99")],
)
def test_safe_phrases_are_not_flagged(expected: str, phrase: str) -> None:
    assert price_fragment_conflicts(case(must_include=[expected], must_not_include=[phrase])) == []


@pytest.mark.parametrize(
    ("price", "phrase"),
    [(16.0, "is $1"), (32.5, "only $3"), (24.99, "$24.9"), (21.0, "$2")],
)
def test_conflicts_against_gold_prices(price: float, phrase: str) -> None:
    c = case(must_include=[], gold_db_facts={"price": price}, must_not_include=[phrase])
    assert len(price_fragment_conflicts(c)) >= 1
