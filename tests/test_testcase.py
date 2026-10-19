from typing import Any

import pytest
from pydantic import ValidationError

from evalkit.schemas import Route, TestCase


def test_minimal_case_is_valid(case_data: dict[str, Any]) -> None:
    case = TestCase.model_validate(case_data)
    assert case.schema_version == 1
    assert case.expected_route is Route.KNOWLEDGE


def test_question_is_stripped(case_data: dict[str, Any]) -> None:
    case = TestCase.model_validate(case_data | {"question": "  hi there \n"})
    assert case.question == "hi there"


def test_route_only_greeting_case_is_valid_via_should_escalate() -> None:
    case = TestCase.model_validate(
        {
            "id": "hi-again",
            "question": "hi again",
            "category": "greeting",
            "expected_route": "greeting",
            "should_escalate": False,
            "source": "legacy",
        }
    )
    assert case.should_escalate is False


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (
            {"must_include": []},
            "asserts nothing: set at least one of reference_answer",
        ),
        (
            {"expected_route": "escalate", "should_escalate": False},
            "expected_route is 'escalate' but should_escalate is False",
        ),
        (
            {"expected_route": "database", "must_include": [], "reference_answer": "24.99"},
            "database cases need gold_db_facts or must_include",
        ),
        (
            {"must_include": ["Free Shipping"], "must_not_include": ["free shipping"]},
            r"must_include and must_not_include overlap \(case-insensitive\): \['free shipping'\]",
        ),
    ],
    ids=["asserts-nothing", "escalate-not-escalating", "database-without-facts", "overlap"],
)
def test_rule_violations_are_rejected_with_clear_message(
    case_data: dict[str, Any], override: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message) as exc:
        TestCase.model_validate(case_data | override)
    assert "'shipping-policy-01'" in str(exc.value)  # names the case


def test_database_case_with_gold_facts_is_valid(case_data: dict[str, Any]) -> None:
    case = TestCase.model_validate(
        case_data
        | {
            "expected_route": "database",
            "must_include": [],
            "gold_db_facts": {"price": 24.99, "in_stock": True, "ingredients": ["Glycerin"]},
        }
    )
    assert case.gold_db_facts["ingredients"] == ["Glycerin"]


def test_escalate_case_may_leave_should_escalate_unset(case_data: dict[str, Any]) -> None:
    case = TestCase.model_validate(case_data | {"expected_route": "escalate"})
    assert case.should_escalate is None


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"expected_route": "billing"}, "expected_route"),
        ({"difficulty": "extreme"}, "difficulty"),
        ({"source": "scraped"}, "source"),
        ({"id": "Bad ID"}, "id"),
        ({"id": ""}, "id"),
        ({"question": "   "}, "question"),
        ({"category": "Policy"}, "category"),
        ({"should_escalate": "yes"}, "should_escalate"),
        ({"safety_critical": 1}, "safety_critical"),
        ({"must_include_any": [[]]}, "must_include_any"),
        ({"must_include": [" "]}, "must_include"),
        ({"gold_db_facts": {"price": {"amount": 1}}}, "gold_db_facts"),
        ({"history": [{"role": "system", "content": "x"}]}, "history"),
        ({"schema_version": 2}, "schema_version"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_bad_field_values_are_rejected(
    case_data: dict[str, Any], override: dict[str, Any], field: str
) -> None:
    with pytest.raises(ValidationError, match=field):
        TestCase.model_validate(case_data | override)


def test_case_is_frozen(case_data: dict[str, Any]) -> None:
    case = TestCase.model_validate(case_data)
    with pytest.raises(ValidationError, match="frozen"):
        case.question = "changed"


def test_expected_route_may_be_null_but_not_missing(case_data: dict[str, Any]) -> None:
    case = TestCase.model_validate(case_data | {"expected_route": None})
    assert case.expected_route is None
    without = {k: v for k, v in case_data.items() if k != "expected_route"}
    with pytest.raises(ValidationError, match="expected_route"):
        TestCase.model_validate(without)
