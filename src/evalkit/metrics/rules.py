"""Content rules of a TestCase checked against a reply (deterministic, no LLM).

Semantics are the agent's own (``agent_snapshot/tests/run_eval.py``), so legacy cases mean
what they meant there: every check is a case-insensitive substring match.

* ``must_include``: every phrase must appear;
* ``must_include_any``: at least one phrase from EACH group must appear;
* ``must_not_include``: no phrase may appear.

``content_rules`` (Day 15) is the per-case metric. It is a **weak signal, never
correctness**: Day 8 found 22 of 85 correctly routed LLM knowledge answers failing the
substring rules, mostly correct paraphrases. Its details also give the agent's "delivered
forbidden" view: a forbidden phrase that is part of the fixed not_found reply misleads
nobody, so ``run_eval.py`` doesn't count it as forbidden content delivered.

``price_fragment_conflicts`` is a lint for the rules themselves: a forbidden phrase that
ends in a price (``"costs $1"``) also matches every longer price that starts with the same
digits (``"costs $19.99"``), so it would fail a correct reply. Day 8 found exactly that in
``legacy-adv-fake-price``.
"""

import re
from dataclasses import dataclass, field
from typing import Final

from evalkit.schemas import AgentResponse, MetricResult, TestCase

CONTENT_RULES: Final = "content_rules"
# The agent's fixed "I don't have that" reply (agent/responder.py NOT_FOUND_REPLY), lower-case,
# as run_eval.py defines it.
HONEST_NOT_FOUND: Final = (
    "i'm sorry, i don't have that information. a glowcare team member can help you further."
)

_TRAILING_PRICE = re.compile(r"\$(\d+(?:\.\d+)?)$")
_PRICE = re.compile(r"\$(\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class RuleCheck:
    """Which content rules a reply breaks; ``passed`` when it breaks none."""

    missing: list[str] = field(default_factory=list)  # must_include phrases not found
    missing_groups: list[list[str]] = field(default_factory=list)  # must_include_any groups
    forbidden: list[str] = field(default_factory=list)  # must_not_include phrases found

    @property
    def passed(self) -> bool:
        return not (self.missing or self.missing_groups or self.forbidden)


def check_rules(case: TestCase, reply: str) -> RuleCheck:
    low = reply.lower()
    return RuleCheck(
        missing=[s for s in case.must_include if s.lower() not in low],
        missing_groups=[g for g in case.must_include_any if not any(s.lower() in low for s in g)],
        forbidden=[s for s in case.must_not_include if s.lower() in low],
    )


def has_content_rules(case: TestCase) -> bool:
    return bool(case.must_include or case.must_include_any or case.must_not_include)


def expected_prices(case: TestCase) -> set[str]:
    """Prices a correct reply may contain, as digit strings: from must_include and the gold
    price ("$19.99" -> "19.99"; gold 16.0 -> "16.00" and "16")."""
    found = {m for s in case.must_include for m in _PRICE.findall(s)}
    for group in case.must_include_any:
        found |= {m for s in group for m in _PRICE.findall(s)}
    price = case.gold_db_facts.get("price")
    if isinstance(price, int | float) and not isinstance(price, bool):
        found |= {f"{price:.2f}", f"{price:g}"}
    return found


def price_fragment_conflicts(case: TestCase) -> list[str]:
    """Forbidden phrases ending in a price that is the start of a longer expected price."""
    conflicts = []
    for phrase in case.must_not_include:
        m = _TRAILING_PRICE.search(phrase)
        if m is None:
            continue
        digits = m.group(1)
        for price in sorted(expected_prices(case)):
            rest = price[len(digits) :]
            continues = rest[:1].isdigit() or (rest[:1] in ".," and rest[1:2].isdigit())
            if price.startswith(digits) and rest and continues:
                conflicts.append(f"{case.id}: must_not_include {phrase!r} matches ${price}")
    return conflicts


def content_rules(case: TestCase, response: AgentResponse | None) -> MetricResult | None:
    """The case's substring rules on the reply (weak signal); None without rules or reply."""
    if not has_content_rules(case) or response is None or not response.ok:
        return None
    check = check_rules(case, response.reply)
    delivered = [s for s in check.forbidden if s.lower() not in HONEST_NOT_FOUND]
    return MetricResult(
        case_id=case.id,
        metric=CONTENT_RULES,
        passed=check.passed,
        details={
            "missing": check.missing,
            "missing_groups": check.missing_groups,
            "forbidden": check.forbidden,
            "delivered_forbidden": delivered,
            "weak_signal": True,
        },
    )
