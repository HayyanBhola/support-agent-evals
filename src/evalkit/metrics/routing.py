"""Routing and escalation metrics (Day 15), deterministic.

* ``route_match``: the reply's route equals the case's expected_route (cases without an
  expected route are skipped: they are not route-checked);
* ``escalation_match``: ``escalated`` equals the case's ``should_escalate`` (null skipped).
  Each row's details carry the outcome (tp / fp / fn / tn), so the run's precision,
  recall and F1 come from the rows. **Recall is reported on its own first**: a missed
  escalation (a customer who needed a human and didn't get one) is worse than an extra one.

Only answered cases (an ok response) are scored, as in ``route_report``.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from evalkit.schemas import AgentResponse, MetricResult, TestCase

ROUTE_MATCH: Final = "route_match"
ESCALATION_MATCH: Final = "escalation_match"
NO_ROUTE: Final = "none"


def answered(response: AgentResponse | None) -> bool:
    return response is not None and response.ok


def route_match(case: TestCase, response: AgentResponse | None) -> MetricResult | None:
    if case.expected_route is None or response is None or not response.ok:
        return None
    actual = response.route.value if response.route else NO_ROUTE
    return MetricResult(
        case_id=case.id,
        metric=ROUTE_MATCH,
        passed=response.route is case.expected_route,
        details={"expected": case.expected_route.value, "actual": actual},
    )


def outcome(expected: bool, actual: bool) -> str:
    if expected:
        return "tp" if actual else "fn"
    return "fp" if actual else "tn"


def escalation_match(case: TestCase, response: AgentResponse | None) -> MetricResult | None:
    if case.should_escalate is None or response is None or not response.ok:
        return None
    if response.escalated is None:
        return None
    return MetricResult(
        case_id=case.id,
        metric=ESCALATION_MATCH,
        passed=response.escalated == case.should_escalate,
        details={
            "expected": case.should_escalate,
            "actual": response.escalated,
            "outcome": outcome(case.should_escalate, response.escalated),
        },
    )


def route_confusion(rows: Iterable[MetricResult]) -> dict[str, Counter[str]]:
    """expected route -> Counter of actual routes, from route_match rows."""
    out: dict[str, Counter[str]] = {}
    for r in rows:
        if r.metric == ROUTE_MATCH:
            out.setdefault(r.details["expected"], Counter())[r.details["actual"]] += 1
    return dict(sorted(out.items()))


@dataclass(frozen=True)
class EscalationSummary:
    tp: int
    fp: int
    fn: int
    tn: int

    @classmethod
    def from_rows(cls, rows: Iterable[MetricResult]) -> "EscalationSummary":
        c = Counter(r.details["outcome"] for r in rows if r.metric == ESCALATION_MATCH)
        return cls(c["tp"], c["fp"], c["fn"], c["tn"])

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p is not None and r is not None and p + r else None
