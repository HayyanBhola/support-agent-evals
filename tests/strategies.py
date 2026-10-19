"""Hypothesis strategies for evalkit models."""

import string
from typing import Any

from hypothesis import reject
from hypothesis import strategies as st
from pydantic import ValidationError

from evalkit.schemas import CaseSource, Difficulty, Route, TestCase

# Roman Urdu, Urdu script and emoji show up in real messages; lone surrogates cannot be UTF-8.
_chars = st.characters(blacklist_categories=("Cs",))
text = st.one_of(
    st.text(_chars, min_size=1, max_size=40),
    st.sampled_from(["mera order kahan hai?", "آپ کا آرڈر", "price kya hai 💧", "  padded  "]),
).filter(lambda s: s.strip() != "")
_alnum = string.ascii_lowercase + string.digits
slugs = st.builds(
    lambda head, tail: head + tail,
    st.sampled_from(_alnum),
    st.text(_alnum + "_-", max_size=20),
)
fact_values = st.one_of(
    text,
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.lists(text, max_size=4),
)


def _norm(s: str) -> str:
    return s.strip().casefold()


@st.composite
def valid_cases(draw: st.DrawFn) -> TestCase:
    """Arbitrary TestCases, nudged after drawing so they (almost) always pass the rules."""
    route = draw(st.sampled_from(Route))
    data: dict[str, Any] = {
        "id": draw(slugs),
        "question": draw(text),
        "category": draw(slugs),
        "expected_route": route,
        "should_escalate": draw(st.none() | st.booleans()),
        "history": draw(
            st.lists(
                st.fixed_dictionaries(
                    {"role": st.sampled_from(["user", "assistant"]), "content": text}
                ),
                max_size=2,
            )
        ),
        "reference_answer": draw(st.none() | text),
        "gold_context_ids": draw(st.lists(text, max_size=2)),
        "gold_db_facts": draw(st.dictionaries(text, fact_values, max_size=2)),
        "must_include": draw(st.lists(text, max_size=2)),
        "must_include_any": draw(st.lists(st.lists(text, min_size=1, max_size=2), max_size=2)),
        "must_not_include": draw(st.lists(text, max_size=2)),
        "safety_critical": draw(st.booleans()),
        "difficulty": draw(st.sampled_from(Difficulty)),
        "source": draw(st.sampled_from(CaseSource)),
        "tags": draw(st.lists(slugs, max_size=2)),
    }
    if route is Route.ESCALATE and data["should_escalate"] is False:
        data["should_escalate"] = True
    if route is Route.DATABASE and not (data["gold_db_facts"] or data["must_include"]):
        data["must_include"] = [draw(text)]
    included = {_norm(s) for s in data["must_include"]}
    data["must_not_include"] = [s for s in data["must_not_include"] if _norm(s) not in included]
    if data["should_escalate"] is None and data["reference_answer"] is None:
        data["reference_answer"] = draw(text)
    try:
        return TestCase.model_validate(data)
    except ValidationError:  # safety net for rare edge cases
        reject()
