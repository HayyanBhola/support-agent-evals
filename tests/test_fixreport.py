"""The agent-fix before/after report (Day 25): invented runs, no agent, no network."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from evalkit import cli
from evalkit.io import write_json, write_jsonl
from evalkit.metrics import fixreport
from evalkit.runner import META_FILE, RESULTS_FILE
from evalkit.schemas import CaseResult, RunCounts, RunMeta

from .agent_double import make_case

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
CASES = [make_case("g1", "hello hello", "greeting"), make_case("e1", "I want a human", "escalate"),
         make_case("k1", "How long does shipping take?"),
         make_case("k2", "What is the refund window?")]  # fmt: skip


def result(cid: str, route: str, escalated: bool, reply: str = "r", tokens: int | None = None,
           run: str = "r") -> CaseResult:  # fmt: skip
    return CaseResult.model_validate({
        "run_id": run, "case_id": cid, "attempts": 1, "cache_hit": False,
        "started_at": T0, "finished_at": T0,
        "response": {"ok": True, "reply": reply, "route": route, "escalated": escalated,
                     "prompt_tokens": tokens,
                     "answer_type": "llm" if tokens else None}})  # fmt: skip


def meta(run_id: str, commit: str, finished: bool = True) -> RunMeta:
    return RunMeta.model_validate({
        "run_id": run_id, "git_sha": "a" * 40, "git_dirty": False, "dataset_path": "d.jsonl",
        "dataset_sha256": "b" * 64, "config_hash": "c" * 64, "agent_model": "gemini/x",
        "agent_repo": {"commit": commit * 40, "branch": "eval-fixes", "dirty_code": False},
        "run_settings": {"max_rpm": 10, "max_llm_calls": 200, "stop_on_failure": True},
        "concurrency": 1, "started_at": T0, "finished_at": T0 if finished else None,
        "counts": RunCounts(total=4, ok=4, failed=0, cache_hits=0)})  # fmt: skip


BEFORE = [result("g1", "knowledge", True), result("e1", "escalate", True),
          result("k1", "knowledge", False, tokens=900),
          result("k2", "knowledge", False)]  # fmt: skip
AFTER = [result("g1", "greeting", False), result("e1", "escalate", True),
         result("k1", "knowledge", False, "new words", 900),
         result("k2", "escalate", True)]  # fmt: skip


def runs(**over: Any) -> dict[str, fixreport.RunData]:
    data = {"before": fixreport.RunData("b1", meta("b1", "1"), BEFORE),
            "after": fixreport.RunData("a1", meta("a1", "2"), AFTER)}  # fmt: skip
    return data | over


def test_comparison_metrics_regressions_and_noise() -> None:
    cases = {c.id: c for c in CASES}
    r = runs()
    comp = fixreport.Comparison("dev", r["before"], r["after"], cases, resamples=50)
    text = "\n".join(comp.metric_lines())
    # one case fixed (g1), one broken (k2): same accuracy, and both show in the report
    assert "| Routing accuracy (n=4) | 3/4 = 75.0%" in text and "**+0.0 pts**" in text
    assert "Escalation precision | 1/2 = 50.0%" in text
    assert "TP 1 / FP 1 / FN 0 / TN 2; after TP 1 / FP 1 / FN 0 / TN 2" in text
    regs = comp.regressions()
    assert [(c, m) for c, m, _, _ in regs] == [("k2", "route_match"), ("k2", "escalation_match")]
    assert regs[0][2] == "expected knowledge, got knowledge" and "got escalate" in regs[0][3]
    routes = "\n".join(comp.route_lines())
    assert "| greeting | 1 | 0/1 | 1/1 |" in routes
    m = comp.metrics()
    assert m["dev.before.route_match"] == 0.75 and m["dev.regressions"] == 2
    assert m["dev.before.llm_calls"] == 1 and r["after"].agent_commit == "2222222"
    same = fixreport.noise(r["before"], r["before"])
    assert same["route"] == [] and same["knowledge"] == 3
    moved = fixreport.noise(r["before"], r["after"])
    assert moved["route"] == ["g1", "k2"] and moved["reply"] == ["k1"]
    empty = fixreport.paired_ci([], lambda s: 1.0)
    assert empty.low is None and fixreport.paired_ci(["a"], lambda s: None).low is None


def _write_run(runs_dir: Path, run_id: str, commit: str, rows: list[CaseResult],
               finished: bool = True) -> None:  # fmt: skip
    write_jsonl(runs_dir / run_id / RESULTS_FILE, rows)
    write_json(runs_dir / run_id / META_FILE, meta(run_id, commit, finished))


def test_cli_agent_fix_report(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    ds = tmp_path / "d.jsonl"
    write_jsonl(ds, CASES)
    for rid, commit, rows in (("db1", "1", BEFORE), ("db2", "1", BEFORE), ("da", "2", AFTER),
                              ("hb", "1", BEFORE), ("ha", "2", AFTER)):  # fmt: skip
        _write_run(runs_dir, rid, commit, rows)
    out = tmp_path / "report.md"
    args = ["agent-fix-report", "--dev-before", "db1", "--dev-before-2", "db2", "--dev-after",
            "da", "--holdout-before", "hb", "--holdout-after", "ha", "--runs-dir", str(runs_dir),
            "--dataset", str(ds), "--out", str(out), "--agent-repo", str(tmp_path / "none"),
            "--no-mlflow", "--resamples", "50"]  # fmt: skip
    res = CliRunner().invoke(cli.app, args)
    assert res.exit_code == 0, res.output
    text = out.read_text(encoding="utf-8")
    assert "## Holdout (the honest number)" in text and "**any change below is the fix" in text
    assert "| `ha` | holdout after | live | `2222222` | eval-fixes |" in text
    assert (
        "**Regressions** (right before, wrong after): 2" in text
        and "sha256 before `unknown" in text
    )
    _write_run(runs_dir, "ha", "2", AFTER, finished=False)
    partial = CliRunner().invoke(cli.app, args)
    assert partial.exit_code == 2 and "unfinished" in partial.output
    missing = CliRunner().invoke(cli.app, [*args[:-2], "--holdout-after", "nope"])
    assert missing.exit_code == 2 and "cannot build the report" in missing.output
    # the offline router for holdout-after, the unfinished live run as a cross-check
    router = Path(__file__).resolve().parent.parent / "agent_snapshot" / "agent" / "router.py"
    offline = [a for a in args if a not in ("--holdout-after", "ha")]
    off = CliRunner().invoke(cli.app, [*offline, "--holdout-after-router", str(router),
                                       "--holdout-partial", "ha",
                                       "--holdout-before-note", "abc (Day 7)",
                                       "--holdout-partial-note", "stopped for good"])  # fmt: skip
    assert off.exit_code == 0, off.output
    text = out.read_text(encoding="utf-8")
    assert "**offline** (rules only)" in text and "After = OFFLINE" in text
    assert "## Live holdout run (partial)" in text and "Cross-check against the offline" in text
    assert "live, **partial**" in text and "**Closed:** stopped for good" in text
    both = CliRunner().invoke(cli.app, [*args, "--holdout-after-router", str(router)])
    assert both.exit_code == 2 and "pass one of" in both.output
