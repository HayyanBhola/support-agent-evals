"""Routing and escalation metrics (Day 15): hand-made inputs and the Day 7 regression."""

from pathlib import Path
from typing import Any

import pytest

from evalkit.io import read_jsonl
from evalkit.metrics import routing
from evalkit.metrics.routing import EscalationSummary
from evalkit.schemas import AgentResponse, CaseResult, MetricResult, TestCase

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "runs" / "20261013-191847-f23b"


def case(cid: str = "c1", route: str | None = "knowledge", esc: bool | None = False) -> TestCase:
    return TestCase.model_validate(
        {"id": cid, "question": "q", "category": "x", "expected_route": route,
         "should_escalate": esc, "difficulty": "easy", "source": "synthetic",
         "reference_answer": "a"}
    )  # fmt: skip


def resp(
    route: str | None = "knowledge", escalated: bool | None = False, **kw: Any
) -> AgentResponse:
    return AgentResponse.model_validate({"ok": True, "reply": "r", "route": route,
                                         "escalated": escalated, **kw})  # fmt: skip


def test_route_match() -> None:
    hit = routing.route_match(case(), resp())
    assert hit is not None and hit.passed and hit.details == {"expected": "knowledge",
                                                               "actual": "knowledge"}  # fmt: skip
    miss = routing.route_match(case(route="greeting"), resp(route=None))
    assert miss is not None and not miss.passed and miss.details["actual"] == "none"
    assert routing.route_match(case(route=None), resp()) is None  # not route-checked
    assert routing.route_match(case(), None) is None
    assert routing.route_match(case(), AgentResponse(ok=False, reply="")) is None


@pytest.mark.parametrize(
    ("expected", "actual", "outcome", "passed"),
    [(True, True, "tp", True), (True, False, "fn", False), (False, True, "fp", False),
     (False, False, "tn", True)],
)  # fmt: skip
def test_escalation_match(expected: bool, actual: bool, outcome: str, passed: bool) -> None:
    r = routing.escalation_match(case(esc=expected), resp(escalated=actual))
    assert r is not None and r.passed is passed and r.details["outcome"] == outcome


def test_escalation_skips() -> None:
    assert routing.escalation_match(case(esc=None), resp()) is None
    assert routing.escalation_match(case(), resp(escalated=None)) is None
    assert routing.escalation_match(case(), None) is None
    assert not routing.answered(None) and routing.answered(resp())


def test_summary_and_confusion() -> None:
    rows = [
        routing.escalation_match(case("a", esc=True), resp(escalated=True)),
        routing.escalation_match(case("b", esc=True), resp(escalated=False)),
        routing.escalation_match(case("c", esc=False), resp(escalated=True)),
        routing.escalation_match(case("d", esc=False), resp(escalated=True)),
        routing.route_match(case("e", route="greeting"), resp(route="knowledge")),
        routing.route_match(case("f", route="greeting"), resp(route="greeting")),
    ]
    got = [r for r in rows if r is not None]
    s = EscalationSummary.from_rows(got)
    assert (s.tp, s.fn, s.fp, s.tn, s.n) == (1, 1, 2, 0, 4)
    assert s.recall == 0.5 and s.precision == pytest.approx(1 / 3)
    assert s.f1 == pytest.approx(2 * 0.5 * (1 / 3) / (0.5 + 1 / 3))
    assert routing.route_confusion(got) == {"greeting": {"knowledge": 1, "greeting": 1}}
    empty = EscalationSummary(0, 0, 0, 3)
    assert empty.recall is None and empty.precision is None and empty.f1 is None


# --------------------------------------------------------------------------- regression


def day7_rows() -> list[MetricResult]:
    if not RUN.exists():
        pytest.skip("Day 7 run not on this machine (runs/ is not in git)")
    cases = {c.id: c for c in read_jsonl(ROOT / "datasets" / "golden_v1.2.jsonl", TestCase)}
    rows = []
    for r in read_jsonl(RUN / "results.jsonl", CaseResult):
        for metric in (routing.route_match, routing.escalation_match):
            row = metric(cases[r.case_id], r.response)
            if row is not None:
                rows.append(row)
    return rows


def test_day7_numbers_on_golden_v1_2_are_reproduced_exactly() -> None:
    rows = day7_rows()
    routes = [r for r in rows if r.metric == routing.ROUTE_MATCH]
    assert (sum(bool(r.passed) for r in routes), len(routes)) == (274, 380)  # 72%
    s = EscalationSummary.from_rows(rows)
    assert (s.tp, s.fn, s.fp, s.tn) == (34, 8, 90, 153)
    assert round(100 * (s.recall or 0), 1) == 81.0
    assert round(100 * (s.precision or 0), 1) == 27.4
    assert round(s.f1 or 0, 3) == 0.410
