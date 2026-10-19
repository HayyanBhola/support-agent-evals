"""evalkit score (Day 15): scoring a tiny hand-made run, the scorecard, metric vs labels."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalkit import cli
from evalkit.calibration.compare import metric_vs_labels
from evalkit.io import read_jsonl, write_jsonl
from evalkit.metrics import scorecard
from evalkit.schemas import CaseResult, Criterion, Label, MetricResult, TestCase

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "runs" / "20261013-191847-f23b"


def case(cid: str, **over: Any) -> TestCase:
    data: dict[str, Any] = {"id": cid, "question": "q", "category": "x",
                            "expected_route": "knowledge", "should_escalate": False,
                            "difficulty": "easy", "source": "synthetic"}  # fmt: skip
    return TestCase.model_validate(data | over)


def result(cid: str, reply: str, route: str = "knowledge", escalated: bool = False,
           prompt: int | None = 100) -> CaseResult:  # fmt: skip
    return CaseResult.model_validate({
        "run_id": "tiny", "case_id": cid, "attempts": 1, "cache_hit": False,
        "wall_latency_ms": 20.0, "started_at": "2026-10-13T00:00:00Z",
        "finished_at": "2026-10-13T00:00:01Z",
        "response": {"ok": True, "reply": reply, "route": route, "escalated": escalated,
                     "latency_ms": 10.0, "prompt_tokens": prompt, "completion_tokens": 5},
    })  # fmt: skip


CASES = {
    "a": case("a", must_include=["glycerin"]),
    "b": case("b", expected_route="greeting", source="legacy"),
    "c": case("c", expected_route="database", should_escalate=None,
              gold_db_facts={"product_name": "Niacinamide Serum", "price": 19.99}),
    "d": case("d", should_escalate=True, source="human"),
}  # fmt: skip
RESULTS = [
    result("a", "It has glycerin."),
    result("b", "Hello", route="knowledge", escalated=True, prompt=None),
    result("c", "The Niacinamide Serum costs $19.99.", route="database", prompt=None),
    result("d", "Customers should contact support.", escalated=True),
    result("not-in-dataset", "x"),
]


def test_score_run_rows() -> None:
    rows = scorecard.score_run(RESULTS, CASES, ["customer support"])
    assert rows == sorted(rows, key=lambda r: (r.case_id, r.metric))
    by = {(r.case_id, r.metric): r for r in rows}
    assert by[("a", "content_rules")].passed and by[("a", "route_match")].passed
    assert not by[("b", "route_match")].passed and not by[("b", "escalation_match")].passed
    assert by[("c", "db_facts")].passed and ("c", "escalation_match") not in by
    assert not by[("d", "voice_third_person")].passed and by[("d", "escalation_match")].passed
    assert ("b", "tokens") not in by and by[("a", "tokens")].value == 105
    assert not any(r.case_id == "not-in-dataset" for r in rows)


def test_scorecard_markdown() -> None:
    rows = scorecard.score_run(RESULTS, CASES)
    text = scorecard.scorecard("tiny", "d.jsonl", "f" * 64, rows, CASES, results_count=4,
                               resamples=200)  # fmt: skip
    assert text.startswith("# Scorecard: run tiny")
    assert "| route_match | reply route = expected route | 3/4 | 75.0% |" in text
    assert "weak signal" in text and "**Recall**" in text and "100.0% (1/1)" in text
    assert "| greeting |" in text and "0.0% (1)" in text  # 1 greeting, routed to knowledge
    assert "## By dataset source" in text and "| known_bug |" in text
    assert "voice_third_person" in text and "customers (1)" in text
    assert "not computed: no price configured" in text
    priced = scorecard.scorecard("tiny", "d.jsonl", "f" * 64, rows, CASES, results_count=4,
                                 price_in=1.0, price_out=2.0, resamples=50)  # fmt: skip
    assert "**Cost:** $0.0002" in priced
    assert text == scorecard.scorecard("tiny", "d.jsonl", "f" * 64, rows, CASES,
                                       results_count=4, resamples=200)  # fmt: skip


def test_scorecard_edges() -> None:
    assert scorecard.rate([]).rate is None
    no_rows = scorecard.scorecard("e", "d", "f" * 64, [], {}, results_count=0, resamples=10)
    assert "| route_match | reply route = expected route | 0/0 | – | – |" in no_rows
    assert "None." in no_rows  # no db_facts failures
    details = {"failed": ["same_product"], "different_product": "x"}
    wrong = MetricResult(case_id="c", metric="db_facts", passed=False, details=details)
    assert "Answered a different product: c (x)" in "\n".join(scorecard._db_section([wrong]))


# --------------------------------------------------------------------------- vs labels

T0 = datetime(2026, 10, 14, tzinfo=UTC)


def label(cid: str, verdict: str) -> Label:
    return Label.model_validate({
        "case_id": cid, "run_id": "r", "labeler": "h", "timestamp": T0, "rubric_hash": "a" * 64,
        "response_sha256": "b" * 64, "seconds_spent": 1.0,
        "verdicts": {c.value: "pass" for c in Criterion} | {"escalation_appropriate": verdict},
    })  # fmt: skip


def test_metric_vs_labels() -> None:
    rows = [MetricResult(case_id=c, metric="escalation_match", passed=p)
            for c, p in [("a", True), ("b", False), ("c", True), ("d", True)]]  # fmt: skip
    labels = {"a": label("a", "pass"), "b": label("b", "fail"), "c": label("c", "fail"),
              "d": label("d", "n_a"), "e": label("e", "pass")}  # fmt: skip
    r = metric_vs_labels(rows, labels, "escalation_match", Criterion.ESCALATION_APPROPRIATE,
                         resamples=100)  # fmt: skip
    assert r.confusion.n == 3 and r.confusion.fail_fail == 1 and r.confusion.fail_pass == 1
    assert r.disagreements == [("c", "fail", "pass")] and r.unpaired == 2


def test_dev_escalation_sanity_on_the_real_run() -> None:
    metrics = RUN / scorecard.METRICS_FILE
    if not metrics.exists():
        pytest.skip("Day 7 run (and its metrics.jsonl) not on this machine")
    from evalkit.calibration.loader import load_labels

    rows = list(read_jsonl(metrics, MetricResult))
    r = metric_vs_labels(rows, load_labels("dev"), "escalation_match",
                         Criterion.ESCALATION_APPROPRIATE)  # fmt: skip
    assert (r.confusion.n, r.confusion.agreed, r.disagreements) == (73, 73, [])


# --------------------------------------------------------------------------- CLI


def test_cli_score(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "tiny"
    write_jsonl(run_dir / "results.jsonl", RESULTS)
    dataset = tmp_path / "d.jsonl"
    write_jsonl(dataset, list(CASES.values()))
    args = ["score", "tiny", "--dataset", str(dataset), "--runs-dir", str(tmp_path / "runs"),
            "--resamples", "50"]  # fmt: skip
    res = CliRunner().invoke(cli.app, args)
    assert res.exit_code == 0, res.output
    assert "route_match: 3/4 (75.0%)" in res.output
    rows = list(read_jsonl(run_dir / "metrics.jsonl", MetricResult))
    first = (run_dir / "scorecard.md").read_bytes()
    assert rows and CliRunner().invoke(cli.app, args).exit_code == 0
    assert (run_dir / "scorecard.md").read_bytes() == first  # deterministic
    missing = CliRunner().invoke(cli.app, ["score", "nope", "--runs-dir", str(tmp_path)])
    assert missing.exit_code == 2 and "cannot score the run" in missing.output
