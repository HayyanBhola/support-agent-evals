"""Parity of content_rules with the agent's own checker (Day 15).

The agent's checks from ``agent_snapshot/tests/run_eval.py`` are ported below (not imported:
the agent is not a dependency) and run on the agent's own case dicts from
``agent_snapshot/tests/eval_cases.json``, read as data. Our ``content_rules`` runs on the
golden_v1.2 version of the same case. On every answered legacy reply of the Day 7 run the
two agree exactly (97 cases with rules; the 10 memory cases were skipped in that run).
"""

import json
from pathlib import Path
from typing import Any

import pytest

from evalkit.io import read_jsonl
from evalkit.metrics.rules import HONEST_NOT_FOUND, content_rules
from evalkit.schemas import AgentResponse, CaseResult, TestCase

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "runs" / "20261013-191847-f23b"
AGENT_CASES = ROOT / "agent_snapshot" / "tests" / "eval_cases.json"
# None. legacy-adv-fake-price agrees too: the agent's own phrase "costs $1 " keeps its
# trailing space, so it never matched "$19.99"; our import stripped the space (which broke the
# rule) and golden_v1.1 restored its intent with "costs $1 now" / "costs $1,".
KNOWN_DIFFERENCES: set[str] = set()


# ---- ported from agent_snapshot/tests/run_eval.py (agent commit 54f9ec6) -----------------


def text_problems(case: dict[str, Any], text: str) -> list[str]:
    problems, low = [], (text or "").lower()
    for t in case.get("must_include", []):
        if t.lower() not in low:
            problems.append(f"missing '{t}'")
    for group in case.get("must_include_any", []):
        if not any(t.lower() in low for t in group):
            problems.append(f"missing one of {group}")
    return problems


def forbidden_found(case: dict[str, Any], text: str) -> list[str]:
    low = (text or "").lower()
    return [t for t in case.get("must_not_include", []) if t.lower() in low]


AGENT_HONEST_NOT_FOUND = (
    "i'm sorry, i don't have that information. a glowcare team member can help you further."
)


def agent_content_passes(case: dict[str, Any], text: str) -> bool:
    """check_case without its route / escalate lines (those are routing metrics here)."""
    return not (text_problems(case, text) + forbidden_found(case, text))


def agent_delivered(case: dict[str, Any], text: str) -> list[str]:
    return [t for t in forbidden_found(case, text) if t.lower() not in AGENT_HONEST_NOT_FOUND]


# ------------------------------------------------------------------------------------------


def test_honest_not_found_matches_the_agent() -> None:
    assert HONEST_NOT_FOUND == AGENT_HONEST_NOT_FOUND


def test_content_rules_matches_the_agent_on_every_day7_legacy_reply() -> None:
    if not RUN.exists():
        pytest.skip("Day 7 run not on this machine (runs/ is not in git)")
    agent = {f"legacy-{c['id']}": c for c in
             json.loads(AGENT_CASES.read_text(encoding="utf-8"))["cases"]}  # fmt: skip
    golden = {c.id: c for c in read_jsonl(ROOT / "datasets" / "golden_v1.2.jsonl", TestCase)}
    compared, differ = 0, set()
    for r in read_jsonl(RUN / "results.jsonl", CaseResult):
        if r.case_id not in agent or r.response is None or not r.response.ok:
            continue
        ours = content_rules(golden[r.case_id], r.response)
        theirs = agent_content_passes(agent[r.case_id], r.response.reply)
        if ours is None:  # no rules on our side: the agent's case must have none either
            assert theirs, r.case_id
            continue
        compared += 1
        if bool(ours.passed) != theirs:
            differ.add(r.case_id)
        else:
            assert ours.details["delivered_forbidden"] == [
                p.strip() for p in agent_delivered(agent[r.case_id], r.response.reply)
            ], r.case_id
    assert compared == 97
    assert differ == KNOWN_DIFFERENCES, f"unexpected differences: {sorted(differ)}"


# --------------------------------------------------------------------------- unit


def case(**rules: Any) -> TestCase:
    return TestCase.model_validate({"id": "c1", "question": "q", "category": "x",
                                    "expected_route": "knowledge", "difficulty": "easy",
                                    "source": "legacy", **rules})  # fmt: skip


def reply(text: str) -> AgentResponse:
    return AgentResponse(ok=True, reply=text)


def test_content_rules_metric() -> None:
    c = case(must_include=["Glycerin"], must_include_any=[["$24.99", "24.99"]],
             must_not_include=["retinol", "team member can help"])  # fmt: skip
    ok = content_rules(c, reply("It has glycerin and costs $24.99."))
    assert ok is not None and ok.passed and ok.details["weak_signal"] is True
    bad = content_rules(c, reply("Retinol. " + HONEST_NOT_FOUND))
    assert bad is not None and not bad.passed
    assert bad.details["missing"] == ["Glycerin"] and bad.details["missing_groups"] == [
        ["$24.99", "24.99"]
    ]
    assert bad.details["forbidden"] == ["retinol", "team member can help"]
    assert bad.details["delivered_forbidden"] == ["retinol"]  # the not_found part doesn't count
    assert content_rules(case(should_escalate=True), reply("x")) is None  # no rules
    assert content_rules(c, None) is None
