"""The free-tier run planner (Day 25): offline route prediction, LLM-call counts, day plan."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalkit import budget, cli
from evalkit.io import read_jsonl
from evalkit.schemas import CaseResult, TestCase

ROOT = Path(__file__).resolve().parent.parent
ROUTER = ROOT / "agent_snapshot" / "agent" / "router.py"
PRODUCTS = ROOT / "agent_snapshot" / "data" / "products" / "products.json"


def test_offline_router_reproduces_the_day7_routes() -> None:
    """The snapshot router is the Day 7 agent's: predicted routes must match every reply."""
    run = ROOT / "runs" / "20261013-191847-f23b" / "results.jsonl"
    if not run.exists():
        pytest.skip("the Day 7 run is not on this machine (runs/ is gitignored)")
    cases = list(read_jsonl(ROOT / "datasets" / "golden_v1.2.jsonl", TestCase))
    day7 = {r.case_id: r for r in read_jsonl(run, CaseResult)}
    pred = budget.predict_routes(budget.load_router(ROUTER), budget.load_products(PRODUCTS),
                                 cases)  # fmt: skip
    actual = {c: r.response.route.value for c, r in day7.items() if r.response and r.response.route}
    assert pred == actual


def _resp(cid: str, route: str, tokens: int | None, details: Any = None) -> CaseResult:
    return CaseResult.model_validate({
        "run_id": "r", "case_id": cid, "attempts": 1, "cache_hit": False,
        "started_at": "2026-10-13T00:00:00Z", "finished_at": "2026-10-13T00:00:01Z",
        "response": {"ok": True, "reply": "x", "route": route, "prompt_tokens": tokens,
                     "details": details}})  # fmt: skip


def test_llm_calls_subsets_and_day_plan() -> None:
    pred = {"a": "knowledge", "b": "knowledge", "c": "greeting", "d": "knowledge", "e": "knowledge"}
    earlier = {"a": _resp("a", "knowledge", 900), "b": _resp("b", "knowledge", None),
               "c": _resp("c", "greeting", None),
               "e": _resp("e", "knowledge", None, {"llm_error": "503"})}  # fmt: skip
    # b answered without the LLM before (no chunks): 0; d unseen: 1; e was a fallback: 1
    assert budget.llm_calls(pred, earlier) == {"a": 1, "b": 0, "c": 0, "d": 1, "e": 1}
    cases = [TestCase.model_validate({"id": i, "question": "q", "category": "c",
                                      "expected_route": "knowledge", "should_escalate": False,
                                      "source": "synthetic"}) for i in "abcde"]  # fmt: skip
    split = {"dev": ["a", "b"], "test": ["c"]}
    assert budget.subset_ids(cases, split, "dev") == ["a", "b"]
    assert budget.subset_ids(cases, split, "holdout") == ["d", "e"]
    with pytest.raises(ValueError):
        budget.subset_ids(cases, split, "test")
    runs = [budget.PlannedRun(n, 10, c, Counter()) for n, c in
            (("r1", 150), ("r2", 80), ("r3", 100), ("r4", 300))]  # fmt: skip
    days = budget.plan_days(runs, 240)
    assert [[r.name for r in d] for d in days] == [["r1", "r2"], ["r3"], ["r4"]]
    assert runs[0].minutes(10) == pytest.approx(1.2)
    with pytest.raises(ImportError):
        budget.load_router(Path("nope.txt"))


def test_cli_agent_budget(tmp_path: Path) -> None:
    if not (ROOT / "runs" / "20261013-191847-f23b" / "results.jsonl").exists():
        pytest.skip("the Day 7 run is not on this machine")
    args = ["agent-budget", "--after-router", str(ROUTER), "--ids-dir", str(tmp_path),
            "--daily-budget", "200"]  # fmt: skip
    res = CliRunner().invoke(cli.app, args)
    flat = " ".join(res.output.split())
    assert res.exit_code == 0, res.output
    assert "predicted LLM calls: 354" in flat and "day 1:" in flat and "day 2:" in flat
    assert len((tmp_path / "agent_dev_ids.txt").read_text(encoding="utf-8").split()) == 96
    pending = CliRunner().invoke(cli.app, ["agent-budget", "--ids-dir", str(tmp_path)])
    assert "pending" in pending.output
    bad = CliRunner().invoke(cli.app, ["agent-budget", "--products", str(tmp_path / "x.json")])
    assert bad.exit_code == 2 and "cannot plan" in bad.output
    assert json.loads((ROOT / "datasets" / "splits" / "split_v1.json").read_text("utf-8"))
