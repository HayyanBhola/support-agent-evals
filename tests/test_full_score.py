"""The full scorecard (Day 21): score-full with a fake judge, summary.json, MLflow in a temp
folder, report and compare. No network, no agent."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalkit import cli
from evalkit.io import read_jsonl, write_jsonl
from evalkit.judges import client as jc
from evalkit.metrics import full, tracking
from evalkit.schemas import MetricResult, TestCase
from tests.test_judges import DEV, FakeJudge, case, jr, ok_json, result
from tests.test_judges_final import _frozen_dir


def _fake(content: str = "") -> Any:
    def make(provider: str, key: str, **kw: Any) -> FakeJudge:
        f = FakeJudge(lambda m: content or ok_json(evidence="30 days"))
        f.last_model = "gpt-5-mini-2025-08-07"

        async def aclose() -> None:
            return None

        f.aclose = aclose  # type: ignore[attr-defined]
        return f

    return make


def _cases(ids: list[str]) -> list[TestCase]:
    out = []
    for i, cid in enumerate(ids):
        extra: dict[str, Any] = {"difficulty": ("easy", "hard")[i % 2]}
        if i % 3 == 0:
            extra |= {"source": "legacy", "expected_route": None}
        if i == 1:
            extra |= {"reference_answer": "30 days", "should_escalate": True}
        out.append(case(cid, **extra))
    return out


def _setup(tmp_path: Path, n: int = 6) -> tuple[list[str], Path]:
    ids = DEV[:n]
    runs = tmp_path / "runs"
    write_jsonl(runs / "r1" / "results.jsonl",
                [result(cid) for cid in ids[:-1]] + [result(ids[-1], ok=False)])  # fmt: skip
    write_jsonl(tmp_path / "d.jsonl", _cases(ids))
    return ids, runs


def _args(tmp_path: Path, pdir: Path, runs: Path, run: str = "r1") -> list[str]:
    return ["score-full", run, "--runs-dir", str(runs), "--dataset", str(tmp_path / "d.jsonl"),
            "--prompt-dir", str(pdir), "--ledger", str(tmp_path / "u.jsonl"),
            "--cache-dir", str(tmp_path / "c"), "--tracking-dir", str(tmp_path / "mlruns"),
            "--resamples", "50"]  # fmt: skip


def test_score_full_report_compare_and_mlflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdir = _frozen_dir(tmp_path)
    ids, runs = _setup(tmp_path)
    args = _args(tmp_path, pdir, runs)
    dry = CliRunner().invoke(cli.app, [*args, "--dry-run"])
    flat = " ".join(dry.output.split())
    assert dry.exit_code == 0, dry.output
    assert "6 cases (5 answered)" in flat and "no call made" in flat
    assert "correctness v2: 1 to judge" in flat
    pricey = CliRunner().invoke(cli.app, [*args, "--max-calls", "100"])
    assert pricey.exit_code == 2 and "--max-usd" in pricey.output
    monkeypatch.setattr(jc, "client_for", _fake())
    # MLflow runs in a subprocess test below: in-process it slows later Hypothesis tests
    res = CliRunner().invoke(cli.app, [*args, "--max-calls", "100", "--max-usd", "1",
                                       "--no-mlflow"])  # fmt: skip
    assert res.exit_code == 0, res.output
    rows = list(read_jsonl(runs / "r1" / full.SCORES_FILE, MetricResult))
    assert {"route_match", "judge_relevance", "judge_correctness"} <= {r.metric for r in rows}
    agent_rows = (runs / "r1" / "results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(agent_rows) == 6 and '"run_id"' in agent_rows[0]  # never overwritten
    summary = json.loads((runs / "r1" / full.SUMMARY_FILE).read_text(encoding="utf-8"))
    assert summary["metrics"]["judge_relevance"] == {
        "passed": 5, "n": 5, "rate": 1.0, "ci_low": 1.0, "ci_high": 1.0}  # fmt: skip
    assert set(summary["breakdowns"]) == {"route", "source", "difficulty"}
    assert set(summary["breakdowns"]["difficulty"]) == {"easy", "hard"}
    assert summary["unanswered"] == [ids[-1]] and summary["judge_errors"] == []
    assert summary["judge_usage"]["calls"] > 0 and summary["frozen_sha256"]
    rep = CliRunner().invoke(cli.app, ["report", "--run", "r1", "--runs-dir", str(runs),
                                       "--by", "difficulty"])  # fmt: skip
    assert rep.exit_code == 0 and "judge_tone" in rep.output and "hard" in rep.output
    bad_by = CliRunner().invoke(cli.app, ["report", "--run", "r1", "--runs-dir", str(runs),
                                          "--by", "x"])  # fmt: skip
    assert bad_by.exit_code == 2
    missing = CliRunner().invoke(cli.app, ["report", "--run", "none", "--runs-dir", str(runs)])
    assert missing.exit_code == 2 and "score-full none" in missing.output
    # a second run: same cases, tone now fails everywhere
    (runs / "r2").mkdir()
    worse = [r.model_copy(update={"passed": False}) if r.metric == "judge_tone" else r
             for r in rows]  # fmt: skip
    write_jsonl(runs / "r2" / full.SCORES_FILE, worse)
    (runs / "r2" / full.SUMMARY_FILE).write_text(json.dumps({**summary, "rubric_hash": "x"}),
                                                  encoding="utf-8")  # fmt: skip
    cmp = CliRunner().invoke(cli.app, ["compare", "r1", "r2", "--runs-dir", str(runs),
                                       "--resamples", "50"])  # fmt: skip
    flat_cmp = " ".join(cmp.output.split())
    assert cmp.exit_code == 0, cmp.output
    assert "differ in rubric_hash" in flat_cmp and "-100.0 pts" in flat_cmp and "worse" in flat_cmp


def test_score_full_stops_on_judge_errors_and_a_changed_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdir = _frozen_dir(tmp_path)
    _, runs = _setup(tmp_path, 13)
    monkeypatch.setattr(jc, "client_for", _fake("not json"))
    args = [*_args(tmp_path, pdir, runs), "--max-calls", "500", "--max-usd", "1", "--no-mlflow"]
    res = CliRunner().invoke(cli.app, args)
    assert res.exit_code == 2 and "more than 10 judge_error" in " ".join(res.output.split())
    gone = CliRunner().invoke(cli.app, [*_args(tmp_path, pdir, runs, "none"), "--dry-run"])
    assert gone.exit_code == 2 and "cannot score the run" in gone.output
    tone = pdir / "tone_v2.md"
    tone.write_text(tone.read_text(encoding="utf-8") + "x", encoding="utf-8")
    changed = CliRunner().invoke(cli.app, [*args, "--dry-run"])
    assert changed.exit_code == 2 and "don't match FROZEN.json" in changed.output


def test_summarize_and_compare_units() -> None:
    cases = {c.id: c for c in _cases(["a", "b", "c", "d"])}
    rows = full.judge_rows([jr("a", "pass"), jr("b", "fail"), jr("c", "judge_error")], "v3")
    assert [r.passed for r in rows] == [True, False] and rows[0].details["prompt_version"] == "v3"
    summary = full.summarize(rows, cases, {"run_id": "x"}, resamples=20)
    assert summary["metrics"]["judge_relevance"]["n"] == 2
    assert summary["latency_ms"] is None and summary["tokens"] is None
    assert summary["escalation"]["recall"]["value"] is None
    b = [r.model_copy(update={"passed": True}) for r in rows]
    (d,) = full.compare(rows, b, resamples=20)
    assert d.metric == "judge_relevance" and d.n_paired == 2 and d.delta == 0.5
    (e,) = full.compare(rows, [], resamples=20)
    assert e.delta is None and e.n_paired == 0


def test_mlflow_flattening() -> None:
    summary = {
        "run_id": "r", "dataset": "d.jsonl",
        "metrics": {"m": {"passed": 1, "n": 2, "rate": 0.5, "ci_low": None, "ci_high": 1.0}},
        "escalation": {"recall": {"value": 0.8, "ci_low": 0.5, "ci_high": 1.0}, "tp": 4},
        "breakdowns": {"route": {"knowledge": {"m": {"rate": 1.0, "n": 1}},
                                "(none)": {"m": {"rate": 0.0, "n": 1}}}},
        "latency_ms": {"p50": 10}, "tokens": None, "judge_usage": {"calls": 3},
    }  # fmt: skip
    m = tracking.metrics_of(summary)
    assert m["m.rate"] == 0.5 and "m.ci_low" not in m and m["escalation.tp"] == 4.0
    assert m["escalation.recall.ci_low"] == 0.5 and m["route.knowledge.m.rate"] == 1.0
    assert m["route.none.m.rate"] == 0.0
    assert m["latency_ms.p50"] == 10.0 and m["judge_usage.calls"] == 3.0
    params = tracking.params_of(summary)
    assert params["dataset"] == "d.jsonl" and params["git_sha"] == ""
    assert tracking.tracking_uri(Path("mlruns")) == "sqlite:///mlruns/mlflow.db"


LOG_SCRIPT = """
import json, sys
from pathlib import Path
import mlflow
from evalkit.metrics import tracking
tmp = Path(sys.argv[1])
summary = json.loads((tmp / "summary.json").read_text(encoding="utf-8"))
run_id = tracking.log_run(summary, [tmp / "summary.json", tmp / "full" / "tone_v2_openai.jsonl"],
                          tracking_dir=tmp / "mlruns")
run = mlflow.get_run(run_id)
arts = sorted(a.path for a in mlflow.MlflowClient().list_artifacts(run_id))
kind = mlflow.get_experiment(run.info.experiment_id).tags.get("mlflow.experimentKind")
print(json.dumps({"params": run.data.params, "metrics": run.data.metrics, "artifacts": arts,
                  "kind": kind}))
"""


def test_mlflow_log_run_in_a_subprocess(tmp_path: Path) -> None:
    summary = {"run_id": "r1", "dataset": "golden_v1.2.jsonl", "git_sha": "abc",
               "metrics": {"judge_tone": {"passed": 3, "n": 4, "rate": 0.75, "ci_low": 0.25,
                                          "ci_high": 1.0}}}  # fmt: skip
    (tmp_path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "tone_v2_openai.jsonl").write_text("{}\n", encoding="utf-8")
    done = subprocess.run([sys.executable, "-c", LOG_SCRIPT, str(tmp_path)], capture_output=True,
                          text=True, timeout=300, check=False)  # fmt: skip
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["params"]["dataset"] == "golden_v1.2.jsonl" and out["params"]["git_sha"] == "abc"
    assert out["metrics"]["judge_tone.rate"] == 0.75
    assert out["artifacts"] == ["judges", "summary.json"]
    assert out["kind"] == "custom_model_development"  # the UI's "Machine learning" view
    assert (tmp_path / "mlruns" / "mlflow.db").exists()
    # a second run reuses the experiment
    again = subprocess.run([sys.executable, "-c", LOG_SCRIPT, str(tmp_path)],
                           capture_output=True, text=True, timeout=300, check=False)  # fmt: skip
    assert again.returncode == 0, again.stderr[-2000:]


def test_judge_sample_is_stratified_and_seeded() -> None:
    ids = [f"c{i:02d}" for i in range(30)]
    cases = {cid: c for cid, c in zip(ids, _cases(ids), strict=True)}  # 10 legacy, 20 synthetic
    got = full.judge_sample(cases, ids, 9)
    assert got == full.judge_sample(cases, ids, 9) and got != full.judge_sample(cases, ids, 9, 7)
    legacy = [c for c in got if cases[c].source.value == "legacy"]
    assert len(got) == 9 and len(legacy) == 3
    assert full.judge_sample(cases, ids[:5], 5) == ids[:5]
    with pytest.raises(ValueError, match="only 5 answered"):
        full.judge_sample(cases, ids[:5], 6)


def test_cli_sample_cases_and_score_full_on_the_sample(tmp_path: Path) -> None:
    pdir = _frozen_dir(tmp_path)
    ids, runs = _setup(tmp_path)
    out = tmp_path / "sample.json"
    base = ["judge", "sample-cases", "--run", "r1", "--runs-dir", str(runs), "--dataset",
            str(tmp_path / "d.jsonl"), "--out", str(out)]  # fmt: skip
    res = CliRunner().invoke(cli.app, [*base, "--n", "3"])
    assert res.exit_code == 0, res.output
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["n"] == 3 and ids[-1] not in doc["case_ids"] and sum(doc["strata"].values()) == 3
    again = CliRunner().invoke(cli.app, [*base, "--n", "3"])
    assert again.exit_code == 2 and "exists" in again.output
    big = CliRunner().invoke(cli.app, [*base[:-1], str(tmp_path / "s2.json"), "--n", "50"])
    assert big.exit_code == 2 and "cannot sample" in big.output
    dry = CliRunner().invoke(cli.app, [*_args(tmp_path, pdir, runs), "--dry-run",
                                       "--judge-sample", str(out)])  # fmt: skip
    flat = " ".join(dry.output.split())
    assert dry.exit_code == 0 and "6 frozen judges on 3 sampled cases" in flat
    assert "relevance v3: 3 to judge" in flat
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"case_ids": ["nope"]}), encoding="utf-8")
    wrong = CliRunner().invoke(cli.app, [*_args(tmp_path, pdir, runs), "--dry-run",
                                         "--judge-sample", str(bad)])  # fmt: skip
    assert wrong.exit_code == 2 and "not in" in wrong.output
