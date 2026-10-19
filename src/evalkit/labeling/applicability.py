"""Which criteria apply to a record, from the rubric frontmatter (Day 9).

For each criterion, ``applies_to`` (routes, answer_types), ``applies_by`` and
``applies_when`` decide whether it is judged or n_a:

* ``applies_by: actual``: judged if the response's route and answer_type (``none`` when it
  has none) are in ``applies_to``;
* ``applies_by: expected``: judged if the case's expected route is in ``applies_to.routes``;
* ``applies_when: has_gold`` (correctness): also judged when the case has a
  reference_answer or gold_db_facts, whatever its expected route.

A few rules come from rubric text rather than frontmatter:

* escalation_appropriate is "decided by the case's should_escalate", so it is judged
  whenever should_escalate is set;
* faithfulness edge cases (Day 9 decision): n_a when the case's expected route is greeting,
  and n_a for a ``not_found`` answer unless the reply claims an action (hand-off wording
  such as "I'm escalating", "I'm passing this on", "a team member will assist you").

Other prose edge cases (e.g. "`not_found` that still states facts") are for the labeler;
the n_a computed here is only a pre-set the labeler can override.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Final

from evalkit.rubrics import Rubric
from evalkit.schemas import (
    AgentResponse,
    AnswerType,
    Criterion,
    Label,
    LabelValue,
    Route,
    TestCase,
)

NO_TYPE = "none"
# criteria with an n_a edge case decided in code (see is_judged)
_EDGE_NA: Final = frozenset({Criterion.FAITHFULNESS})
# Wording that tells the customer a hand-off happened or is happening (rubric faithfulness,
# Fail if). An offer ("a team member CAN help you") is not a claimed action.
ACTION_CLAIM = re.compile(
    r"\bescalat(e|ed|ing)\b|\bpassing (your|this|you)\b|\b(i've|i have) (created|opened|"
    r"logged|forwarded)\b|\btickets?\b|"
    r"\bwill (be able to )?(assist|help|follow up|contact|be in touch|get back)\b|"
    r"\bforward(ed|ing) (your|this)\b",
    re.IGNORECASE,
)


def claims_action(reply: str) -> bool:
    return ACTION_CLAIM.search(reply) is not None


ALL_ANSWER_TYPES: Final = frozenset({*(t.value for t in AnswerType), NO_TYPE})


def allows_na(rubric: Rubric) -> bool:
    """False for a criterion judged on every reply (applies_by actual, every route and
    answer type, no edge-case n_a): safety, relevance and tone. The app doesn't offer n_a
    for those (rubric v2: n_a is never a way to say "unsure")."""
    meta = rubric.meta
    every = set(meta.applies_to.routes) == set(Route) and (
        set(meta.applies_to.answer_types) == ALL_ANSWER_TYPES
    )
    return not (meta.applies_by == "actual" and every and rubric.criterion not in _EDGE_NA)


def is_judged(rubric: Rubric, case: TestCase, response: AgentResponse) -> bool:
    meta = rubric.meta
    routes = {r.value for r in meta.applies_to.routes}
    if rubric.criterion is Criterion.FAITHFULNESS:
        if case.expected_route is Route.GREETING:
            return False
        if response.answer_type is AnswerType.NOT_FOUND and not claims_action(response.reply):
            return False
    if meta.applies_by == "actual":
        route = response.route.value if response.route else None
        answer_type = response.answer_type.value if response.answer_type else NO_TYPE
        return route in routes and answer_type in meta.applies_to.answer_types
    if rubric.criterion is Criterion.ESCALATION_APPROPRIATE and case.should_escalate is not None:
        return True
    expected = case.expected_route.value if case.expected_route else None
    if expected in routes:
        return True
    return meta.applies_when == "has_gold" and bool(case.reference_answer or case.gold_db_facts)


def applicability(
    rubrics: Iterable[Rubric], case: TestCase, response: AgentResponse
) -> dict[Criterion, bool]:
    """Criterion -> True if judged, False if n_a."""
    return {r.criterion: is_judged(r, case, response) for r in rubrics}


def mismatches(label: Label, judged: Mapping[Criterion, bool]) -> list[str]:
    """Verdicts that disagree with applicability (warnings, not errors): n_a on a judged
    criterion, or a verdict on an n_a criterion that wasn't recorded as an override."""
    out = []
    for criterion, applies in judged.items():
        verdict = label.verdicts[criterion]
        if applies and verdict is LabelValue.N_A:
            out.append(f"{label.case_id}: {criterion.value} is n_a but the rubric says judge it")
        elif not applies and verdict is not LabelValue.N_A and criterion not in label.overrides:
            out.append(
                f"{label.case_id}: {criterion.value} is {verdict.value} but the rubric says "
                "n_a and it isn't marked as an override"
            )
    return out
