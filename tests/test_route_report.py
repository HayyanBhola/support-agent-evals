from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from evalkit import cli
from evalkit.hashing import file_sha256
from evalkit.io import write_json, write_jsonl
from evalkit.route_report import build_route_report, load_run, print_route_report
from evalkit.runner import META_FILE, RESULTS_FILE, RunAborted, count_results
from evalkit.schemas import AgentResponse, CaseResult, FailureType, RunMeta

from .agent_double import fixture, make_case

NOW = datetime(2026, 10, 13, tzinfo=UTC)
CASES = [
    make_case("k1", "Shipping?", category="knowledge"),
    make_case("k2", "Returns?", category="knowledge"),
    make_case("g1", "hi again", "greeting", category="greeting", tags=["known_bug"]),
    make_case("d1", "Price?", "database", category="database"),
    make_case("u1", "Acne?", expected_route=None, category="medical"),
    make_case("s1", "Skipped?", category="memory"),
]


def result(case_id: str, route: str, **resp: Any) -> CaseResult:
    response = AgentResponse.model_validate(fixture("knowledge", route=route, **resp))
    return CaseResult(
        run_id="run-1",
        case_id=case_id,
        response=response,
        attempts=1,
        started_at=NOW,
        finished_at=NOW,
    )


def results() -> list[CaseResult]:
    common: dict[str, Any] = {
        "run_id": "run-1",
        "attempts": 0,
        "started_at": NOW,
        "finished_at": NOW,
    }
    return [
        result("k1", "knowledge"),
        result("k2", "knowledge"),
        result("g1", "knowledge", escalated=True),
        result("d1", "clarify"),
        result("u1", "knowledge"),
        CaseResult(case_id="s1", failure_type=FailureType.SKIPPED, **common),
        result("gone", "knowledge"),  # case not in the dataset
    ]


def test_report_counts_and_groups_misses() -> None:
    report = build_route_report(results(), {c.id: c for c in CASES})
    assert (report.overall.correct, report.overall.checked) == (2, 4)
    assert report.by_category["knowledge"].accuracy == 1.0
    assert report.by_category["greeting"].accuracy == 0.0
    assert report.by_expected["database"].checked == 1
    assert sorted(report.misses) == ["database", "greeting"]
    miss = report.misses["greeting"][0]
    assert (miss.case_id, miss.actual, miss.escalated, miss.tags) == (
        "g1",
        "knowledge",
        True,
        ("known_bug",),
    )
    assert [m.case_id for m in report.unchecked] == ["u1"]
    assert report.not_answered == 1 and report.unknown_cases == 1


def test_print_report() -> None:
    out = Console(file=StringIO(), width=200)
    print_route_report(out, build_route_report(results(), {c.id: c for c in CASES}))
    text = out.file.getvalue()  # type: ignore[attr-defined]
    assert "Route accuracy: 50% (2/4)" in text and "Wrong routes, by expected route" in text
    assert "not checked (no ok response): 1" in text and "u1: knowledge" in text

    out = Console(file=StringIO(), width=200)
    print_route_report(out, build_route_report([], {}))
    text = out.file.getvalue()  # type: ignore[attr-defined]
    assert "n/a" in text and "no wrong routes" in text


def write_run(tmp_path: Path) -> Path:
    dataset = tmp_path / "ds.jsonl"
    write_jsonl(dataset, CASES)
    run_dir = tmp_path / "runs" / "run-1"
    rs = results()
    write_jsonl(run_dir / RESULTS_FILE, rs)
    meta = RunMeta(
        run_id="run-1",
        git_sha=None,
        git_dirty=False,
        dataset_path=str(dataset),
        dataset_sha256=file_sha256(dataset),
        config_hash="0" * 64,
        concurrency=2,
        started_at=NOW,
        finished_at=NOW,
        counts=count_results(rs),
    )
    write_json(run_dir / META_FILE, meta)
    return dataset


def test_load_run_and_cli(tmp_path: Path) -> None:
    write_run(tmp_path)
    meta, rs, cases = load_run(tmp_path / "runs", "run-1")
    assert meta.run_id == "run-1" and len(rs) == 7 and set(cases) == {c.id for c in CASES}
    out = CliRunner().invoke(cli.app, ["routes", "run-1", "--runs-dir", str(tmp_path / "runs")])
    assert out.exit_code == 0 and "Route accuracy" in out.output


def test_load_run_refusals(tmp_path: Path) -> None:
    with pytest.raises(RunAborted, match="not found"):
        load_run(tmp_path / "runs", "nope")
    dataset = write_run(tmp_path)
    write_jsonl(dataset, CASES[:2])  # dataset edited after the run
    with pytest.raises(RunAborted, match="changed since run"):
        load_run(tmp_path / "runs", "run-1")
    (tmp_path / "runs" / "run-1" / META_FILE).write_text("{}", encoding="utf-8")
    with pytest.raises(RunAborted, match="cannot load run"):
        load_run(tmp_path / "runs", "run-1")
    out = CliRunner().invoke(cli.app, ["routes", "run-1", "--runs-dir", str(tmp_path / "runs")])
    assert out.exit_code == 2


def test_report_by_source_escalation_retries_errors_and_models() -> None:
    from evalkit.route_report import print_run_health
    from evalkit.schemas import CaseSource

    cases = {c.id: c for c in CASES}
    cases["k2"] = cases["k2"].model_copy(update={"source": CaseSource.SYNTHETIC})
    cases["e1"] = make_case("e1", "I want a human", "escalate", source=CaseSource.LEGACY)
    cases["e2"] = make_case("e2", "Talk to a person", "escalate", source=CaseSource.LEGACY)
    cases["x1"] = make_case("x1", "Broken?", category="knowledge")
    rs = results()
    rs.append(result("e1", "escalate", escalated=True))
    rs.append(result("e2", "knowledge", escalated=False).model_copy(update={"attempts": 3}))
    err = AgentResponse.model_validate(fixture("error"))
    rs.append(
        CaseResult(
            run_id="run-1",
            case_id="x1",
            response=err,
            failure_type=FailureType.AGENT_ERROR,
            error_message="boom",
            attempts=1,
            started_at=NOW,
            finished_at=NOW,
        )
    )

    report = build_route_report(rs, cases)
    assert {k: (t.correct, t.checked) for k, t in report.by_source.items()} == {
        "known_bug": (1, 3),
        "synthetic": (1, 1),
        "legacy": (1, 2),
    }
    e = report.escalation
    assert (e.tp, e.fp, e.fn, e.tn) == (1, 1, 1, 4)
    assert e.precision == 0.5 and e.recall == 0.5 and e.f1 == 0.5
    assert e.missed == ["e2"] and e.extra == ["g1"]
    assert report.retried == {"e2": 3}
    assert report.agent_errors == ["x1"]
    assert report.models["gemini/gemini-3.1-flash-lite"] == 9
    assert report.summary is not None and report.summary.skipped == 1

    out = StringIO()
    print_run_health(Console(file=out, width=200), report)
    text = out.getvalue()
    assert "precision 50% (1/2)" in text and "Escalation recall: 50% (1/2)" in text
    assert "Escalation F1: 50%" in text
    assert "should escalate, didn't: e2" in text and "escalated, shouldn't have: g1" in text
    assert "retries: 1 cases, 2 extra attempts" in text
    assert "agent_error cases: x1" in text
    assert "models in responses: gemini/gemini-3.1-flash-lite 9" in text
    assert "Latency by actual route" in text and "skipped 1" in text


def test_escalation_stats_without_data() -> None:
    from evalkit.route_report import EscalationStats

    e = EscalationStats()
    assert e.precision is None and e.recall is None and e.f1 is None
    e.add("a", expected=True, actual=False)
    e.add("b", expected=False, actual=True)
    assert e.precision == 0.0 and e.recall == 0.0 and e.f1 is None
