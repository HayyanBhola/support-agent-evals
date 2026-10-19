"""What a judge sees for one case (Day 16): the common fields plus only what its criterion
needs. Serialised deterministically, so its sha256 can key the cache."""

import hashlib
import json
from collections.abc import Callable
from typing import Any, Final

from evalkit.labeling.view import chunks, db_facts
from evalkit.schemas import AgentResponse, Criterion, TestCase

# Extra context per criterion, beyond question / history / reply / route / answer_type /
# escalated (Day 17): faithfulness gets what the reply could rest on (retrieved chunks, the
# database rows in details); correctness the reference answer and gold facts; completeness
# only whether an answer exists; relevance, safety and tone nothing more.
EXTRA: Final[dict[Criterion, tuple[str, ...]]] = {
    Criterion.RELEVANCE: (),
    Criterion.TONE: (),
    Criterion.SAFETY: (),
    Criterion.ESCALATION_APPROPRIATE: ("should_escalate",),
    Criterion.FAITHFULNESS: ("retrieved_chunks", "db_facts"),
    Criterion.CORRECTNESS: ("reference_answer", "gold_db_facts"),
    Criterion.COMPLETENESS: ("answer_available",),
}


COMMON_FIELDS: Final = ("question", "history", "reply", "route", "answer_type", "escalated")

# Fields a prompt version adds for its criterion.
EXTRA_BY_VERSION: Final[dict[tuple[Criterion, str], tuple[str, ...]]] = {
    (Criterion.RELEVANCE, "v2"): ("answer_available",),
    (Criterion.SAFETY, "v2"): ("db_facts",),
    (Criterion.RELEVANCE, "v3"): ("answer_status",),
    (Criterion.SAFETY, "v3"): ("db_facts", "retrieved_chunks"),
}
# completeness v3 swaps its two-valued flag for the three-valued one
REPLACED_BY_VERSION: Final[dict[tuple[Criterion, str], dict[str, str]]] = {
    (Criterion.COMPLETENESS, "v3"): {"answer_available": "answer_status"},
}


def has_gold(case: TestCase) -> bool:
    return bool(case.reference_answer or case.gold_db_facts)


def input_fields(criterion: Criterion, version: str) -> list[str]:
    """The input fields a criterion's prompt version sends (for FROZEN.json)."""
    swap = REPLACED_BY_VERSION.get((criterion, version), {})
    extra = [swap.get(f, f) for f in EXTRA[criterion]]
    extra += list(EXTRA_BY_VERSION.get((criterion, version), ()))
    named = ["answer_available (3-valued)" if f == "answer_status" else f for f in extra]
    return [*COMMON_FIELDS, *named]


# Cases a judge version never sees although the rubric judges them (Day 17): correctness v2
# has no basis without a reference answer or gold facts, so those cases are n_a for the judge.
JUDGE_SKIPS: Final[dict[tuple[Criterion, str], Callable[[TestCase], bool]]] = {
    (Criterion.CORRECTNESS, "v2"): lambda case: not has_gold(case),
}


def answer_available(case: TestCase) -> bool:
    """The knowledge base or database holds an answer: the case has gold database facts, or a
    reference answer and is neither out-of-scope nor tagged unanswerable (their reference
    answers describe how to decline, not an answer the bot could give). Gold facts that say
    the order or product doesn't exist (``exists`` / ``product_exists`` false) mean the right
    reply is "not found": there is no answer to give (Day 17 fix)."""
    facts = case.gold_db_facts
    if facts.get("exists") is False or facts.get("product_exists") is False:
        return False
    if facts:
        return True
    declining = case.category == "out_of_scope" or "unanswerable" in case.tags
    return bool(case.reference_answer) and not declining


def answer_status(case: TestCase) -> bool | None:
    """Three-valued ``answer_available`` (Day 17, v3 prompts): True when the knowledge base or
    database holds an answer (gold facts, or a reference answer on a case that isn't
    out-of-scope or unanswerable); False when the gold facts say the order or product doesn't
    exist; None when it isn't known (no gold facts and no usable reference answer)."""
    facts = case.gold_db_facts
    if facts.get("exists") is False or facts.get("product_exists") is False:
        return False
    declining = case.category == "out_of_scope" or "unanswerable" in case.tags
    if facts or (case.reference_answer and not declining):
        return True
    return None


def judge_input(
    criterion: Criterion, case: TestCase, response: AgentResponse, version: str = "v1"
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "question": case.question,
        "history": [{"role": t.role, "content": t.content} for t in case.history],
        "reply": response.reply,
        "route": response.route.value if response.route else None,
        "answer_type": response.answer_type.value if response.answer_type else None,
        "escalated": response.escalated,
    }
    swap = REPLACED_BY_VERSION.get((criterion, version), {})
    fields = [swap.get(f, f) for f in EXTRA[criterion]]
    for field in (*fields, *EXTRA_BY_VERSION.get((criterion, version), ())):
        if field == "answer_status":  # sent under the same name, three-valued
            data["answer_available"] = answer_status(case)
        elif field == "answer_available":
            data[field] = answer_available(case)
        elif field == "should_escalate":
            data[field] = case.should_escalate
        elif field == "retrieved_chunks":
            data[field] = [{"id": c.id, "text": c.text} for c in chunks(response)]
        elif field == "db_facts":
            data[field] = db_facts(response)
        elif field == "reference_answer":
            data[field] = case.reference_answer
        elif field == "gold_db_facts":
            data[field] = dict(case.gold_db_facts)
    return data


def to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1)


def input_sha256(data: dict[str, Any]) -> str:
    return hashlib.sha256(to_json(data).encode("utf-8")).hexdigest()
