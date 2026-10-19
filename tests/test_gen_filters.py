"""Quality filters for generated candidates."""

from typing import Any

import pytest

from evalkit.generation.filters import (
    Candidate,
    Language,
    apply_filters,
    detect_language,
    language_problem,
)


def cand(
    question: str,
    *,
    slot: str = "s",
    language: Language = Language.ENGLISH,
    n: int = 0,
    **fields: Any,
) -> Candidate:
    data: dict[str, Any] = {
        "id": f"syn-test-{n:08d}",
        "question": question,
        "category": "knowledge",
        "expected_route": "knowledge",
        "should_escalate": False,
        "source": "synthetic",
        **fields,
    }
    return Candidate("gen", "sub", slot, language, data)


def run(*cands: Candidate, quota: int = 10, existing: tuple[str, ...] = ()) -> tuple[Any, Any]:
    slots = {c.slot for c in cands}
    return apply_filters(list(cands), dict.fromkeys(slots, quota), existing)


def test_keeps_a_good_candidate() -> None:
    kept, rejected = run(cand("How long does standard shipping take?"))
    assert [k.case.question for k in kept] == ["How long does standard shipping take?"]
    assert rejected == []


@pytest.mark.parametrize(
    ("candidate", "stage"),
    [
        (cand("Is this valid?", expected_route="nope"), "validation"),
        (cand("Shipping time?"), "length"),
        (cand(" ".join(["word"] * 61)), "length"),
        (cand("Is it 3-5 business days for shipping?", must_include=["3-5 business days"]),
         "answer_in_question"),
        (cand("Is it 5-7 days or 3-5 days?", must_include_any=[["3-5"], ["5-7"]]),
         "answer_in_question"),
        (cand("Is the moisturizer really 24.99 dollars?", expected_route="database",
              gold_db_facts={"price": 24.99}), "answer_in_question"),
        (cand("Do you have 42 of them in stock?", expected_route="database",
              gold_db_facts={"stock_quantity": 42}), "answer_in_question"),
    ],
)  # fmt: skip
def test_rejection_stages(candidate: Candidate, stage: str) -> None:
    kept, rejected = run(candidate)
    assert kept == []
    assert [r.stage for r in rejected] == [stage]
    assert rejected[0].generator == "gen"


def test_gold_value_inside_another_number_is_not_a_giveaway() -> None:
    c = cand("Is order 1424 the one with the cream?", expected_route="database",
             gold_db_facts={"stock_quantity": 42})  # fmt: skip
    kept, _ = run(c)
    assert len(kept) == 1


def test_near_duplicates_within_a_generator() -> None:
    kept, rejected = run(
        cand("How long does standard shipping take?", n=1),
        cand("How long does standard shipping take??", n=2),
        cand("How long does the standard shipping take?", n=3),
        cand("Can I cancel my order after two hours?", n=4),
    )
    assert len(kept) == 2
    assert [r.stage for r in rejected] == ["duplicate", "duplicate"]
    assert "exact duplicate of a kept" in rejected[0].reason
    assert "near-duplicate" in rejected[1].reason


def test_exact_duplicate_of_an_existing_dataset() -> None:
    _, rejected = run(cand("What is your return policy?"), existing=("what is your RETURN policy",))
    assert "existing dataset" in rejected[0].reason


def test_quota_per_slot_rejects_surplus() -> None:
    kept, rejected = run(
        cand("How long does standard shipping take?", n=1),
        cand("Can I cancel my order after two hours?", n=2),
        quota=1,
    )
    assert len(kept) == 1
    assert [r.stage for r in rejected] == ["surplus"]


def test_language_check_applies_to_roman_urdu_only() -> None:
    urdu = Language.ROMAN_URDU
    kept, rejected = run(
        cand("Mera order kab tak aayega?", language=urdu, n=1),
        cand("When will my order arrive please?", language=urdu, n=2),
        cand("Mera order کب آئے گا kab tak?", language=urdu, n=3),
        cand("Salam, how long does shipping take?", n=4),
    )
    assert [k.case.question for k in kept] == [
        "Mera order kab tak aayega?",
        "Salam, how long does shipping take?",
    ]
    assert [r.reason for r in rejected] == [
        "expected roman_urdu, looks like english",
        "uses a non-Latin script",
    ]


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Kya aap meri madad kar sakte hain?", Language.ROMAN_URDU),
        ("Refund kab milega?", Language.ROMAN_URDU),
        ("Do you ship to Lahore?", Language.ENGLISH),  # "do" alone is ambiguous
        ("Main question: do you ship abroad or not?", Language.ENGLISH),
        ("Salam, what is the price of your cleanser today?", Language.ENGLISH),
    ],
)
def test_detect_language(text: str, language: Language) -> None:
    assert detect_language(text) is language


def test_language_problem_accepts_typographic_punctuation() -> None:
    assert language_problem("Kya order ship ho gaya hai… “jaldi”?", Language.ROMAN_URDU) is None


def test_near_duplicates_only_count_for_the_same_gold_facts() -> None:
    def order(order_id: str, n: int) -> Candidate:
        return cand(f"What's happening with my order {order_id}?", n=n,
                    expected_route="database", gold_db_facts={"order_id": order_id})  # fmt: skip

    kept, rejected = run(order("GC10241", 1), order("GC10242", 2), order("GC10241", 3))
    assert len(kept) == 2
    assert [r.reason for r in rejected] == ["exact duplicate of a kept question"]


@pytest.mark.parametrize(
    "text",
    [
        "Gentle Daily Cleanser ka price batao?",
        "Barrier Repair Cream ke andar kaunse ingredients hain?",
        "Gentle Daily Cleanser ki keemat kya hai?",
    ],
)
def test_roman_urdu_seen_in_the_smoke_test(text: str) -> None:
    assert detect_language(text) is Language.ROMAN_URDU
