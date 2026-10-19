"""Route and escalation report for a finished run.

* Route accuracy overall, per expected route, per source (legacy / known_bug / synthetic)
  and per category, and every miss grouped by expected route. Same counting rule as the run
  summary: only ok responses to cases with a non-null expected_route are checked.
* Escalation precision and recall: ok responses to cases with a non-null should_escalate,
  compared with the response's ``escalated``.
* Retries, agent errors, failures, skips, latency per actual route, and the models that
  answered (from each response's ``model`` field).
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from evalkit.hashing import file_sha256
from evalkit.io import JsonlError, read_json, read_jsonl
from evalkit.runner import META_FILE, RESULTS_FILE, RunAborted, Summary, summarize
from evalkit.schemas import CaseResult, CaseSource, FailureType, RunMeta, TestCase

SOURCE_LABELS = {
    CaseSource.LEGACY: "legacy",
    CaseSource.HUMAN: "known_bug",
    CaseSource.SYNTHETIC: "synthetic",
    CaseSource.PRODUCTION: "production",
}


@dataclass
class Tally:
    correct: int = 0
    checked: int = 0

    def add(self, ok: bool) -> None:
        self.checked += 1
        self.correct += ok

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.checked if self.checked else None


@dataclass
class EscalationStats:
    """Confusion counts for should_escalate (expected) vs escalated (actual)."""

    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    missed: list[str] = field(default_factory=list)  # should escalate, didn't (case ids)
    extra: list[str] = field(default_factory=list)  # escalated, shouldn't have

    def add(self, case_id: str, expected: bool, actual: bool) -> None:
        if expected and actual:
            self.tp += 1
        elif expected:
            self.fn += 1
            self.missed.append(case_id)
        elif actual:
            self.fp += 1
            self.extra.append(case_id)
        else:
            self.tn += 1

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p is not None and r is not None and p + r else None


@dataclass(frozen=True)
class RouteMiss:
    case_id: str
    category: str
    question: str
    expected: str
    actual: str
    escalated: bool | None
    tags: tuple[str, ...]


@dataclass
class RouteReport:
    overall: Tally = field(default_factory=Tally)
    by_category: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    by_expected: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    by_source: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    escalation: EscalationStats = field(default_factory=EscalationStats)
    models: Counter[str] = field(default_factory=Counter)  # response.model of every response
    retried: dict[str, int] = field(default_factory=dict)  # case id -> attempts (> 1)
    agent_errors: list[str] = field(default_factory=list)
    summary: Summary | None = None
    misses: dict[str, list[RouteMiss]] = field(default_factory=lambda: defaultdict(list))
    unchecked: list[RouteMiss] = field(default_factory=list)  # expected_route null
    not_answered: int = 0  # no ok response (failed or skipped)
    unknown_cases: int = 0  # result whose case is not in the dataset


def build_route_report(results: Iterable[CaseResult], cases: Mapping[str, TestCase]) -> RouteReport:
    results = list(results)
    report = RouteReport(summary=summarize(results, dict(cases)))
    for r in results:
        if r.attempts > 1:
            report.retried[r.case_id] = r.attempts
        if r.failure_type is FailureType.AGENT_ERROR:
            report.agent_errors.append(r.case_id)
        if r.response is not None:
            report.models[r.response.model or "(none)"] += 1
        case = cases.get(r.case_id)
        if case is None:
            report.unknown_cases += 1
            continue
        resp = r.response
        if resp is None or not resp.ok:
            report.not_answered += 1
            continue
        actual = resp.route.value if resp.route else "none"
        miss = RouteMiss(
            case_id=case.id,
            category=case.category,
            question=case.question,
            expected=case.expected_route.value if case.expected_route else "(not checked)",
            actual=actual,
            escalated=resp.escalated,
            tags=tuple(case.tags),
        )
        if case.should_escalate is not None and resp.escalated is not None:
            report.escalation.add(case.id, case.should_escalate, resp.escalated)
        if case.expected_route is None:
            report.unchecked.append(miss)
            continue
        ok = resp.route is case.expected_route
        report.overall.add(ok)
        report.by_category[case.category].add(ok)
        report.by_expected[case.expected_route.value].add(ok)
        report.by_source[SOURCE_LABELS[case.source]].add(ok)
        if not ok:
            report.misses[case.expected_route.value].append(miss)
    return report


def load_run(runs_dir: Path, run_id: str) -> tuple[RunMeta, list[CaseResult], dict[str, TestCase]]:
    """A run's meta, results and the dataset it ran. Raises RunAborted if unreadable."""
    run_dir = runs_dir / run_id
    try:
        meta = read_json(run_dir / META_FILE, RunMeta)
        results = list(read_jsonl(run_dir / RESULTS_FILE, CaseResult))
        dataset = Path(meta.dataset_path)
        cases = {c.id: c for c in read_jsonl(dataset, TestCase)}
    except FileNotFoundError as e:
        raise RunAborted(f"cannot load run {run_id!r}: {e.filename} not found") from e
    except (ValidationError, JsonlError) as e:
        raise RunAborted(f"cannot load run {run_id!r}: {e}") from e
    if file_sha256(dataset) != meta.dataset_sha256:
        raise RunAborted(f"{dataset} changed since run {run_id}; its cases no longer match")
    return meta, results, cases


def _pct(t: Tally) -> str:
    acc = t.accuracy
    return f"{acc:.0%} ({t.correct}/{t.checked})" if acc is not None else "n/a"


def _ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def print_route_report(console: Console, report: RouteReport) -> None:
    console.print(f"[bold]Route accuracy[/]: {_pct(report.overall)}")
    if report.not_answered:
        console.print(f"not checked (no ok response): {report.not_answered}")
    for title, tallies in (
        ("expected route", report.by_expected),
        ("source", report.by_source),
        ("category", report.by_category),
    ):
        table = Table(title=f"By {title}", title_justify="left")
        table.add_column(title)
        table.add_column("accuracy", justify="right")
        for name, tally in sorted(tallies.items()):
            table.add_row(name, _pct(tally))
        console.print(table)
    if report.misses:
        table = Table(title="Wrong routes, by expected route", title_justify="left")
        for col in ("expected", "actual", "case", "category", "escalated", "question"):
            table.add_column(col, overflow="fold")
        for expected in sorted(report.misses):
            for m in report.misses[expected]:
                table.add_row(
                    m.expected,
                    m.actual,
                    m.case_id,
                    m.category,
                    str(m.escalated),
                    escape(m.question),
                )
        console.print(table)
    else:
        console.print("no wrong routes")
    if report.unchecked:
        console.print("Route not checked (expected_route null):")
        for m in report.unchecked:
            console.print(f"  {m.case_id}: {m.actual} (escalated={m.escalated})")


def print_run_health(console: Console, report: RouteReport) -> None:
    """Escalation, retries, errors, failures, latency and models of the run."""
    e = report.escalation
    console.print(
        f"[bold]Escalation[/] (should_escalate vs escalated): precision {_ratio(e.precision)} "
        f"({e.tp}/{e.tp + e.fp}); TP {e.tp}, FP {e.fp}, FN {e.fn}, TN {e.tn}"
    )
    console.print(f"[bold]Escalation recall[/]: {_ratio(e.recall)} ({e.tp}/{e.tp + e.fn})")
    console.print(f"Escalation F1: {_ratio(e.f1)}")
    if e.missed:
        console.print(f"  should escalate, didn't: {', '.join(e.missed)}")
    if e.extra:
        console.print(f"  escalated, shouldn't have: {', '.join(e.extra)}")
    s = report.summary
    if s is not None:
        console.print(
            f"cases {s.total}: ok {s.ok}, skipped {s.skipped}, failed {sum(s.failures.values())}"
            + (f" {dict(s.failures)}" if s.failures else "")
            + f", cache hits {s.cache_hits}"
        )
        table = Table(title="Latency by actual route (non-cached)", title_justify="left")
        for col in ("route", "n", "p50", "p95"):
            table.add_column(col, justify="left" if col == "route" else "right")
        for row in s.latency:
            table.add_row(
                row.route, str(row.n), f"{row.p50_ms / 1000:.2f} s", f"{row.p95_ms / 1000:.2f} s"
            )
        console.print(table)
    extra_attempts = sum(n - 1 for n in report.retried.values())
    console.print(f"retries: {len(report.retried)} cases, {extra_attempts} extra attempts")
    console.print(f"agent_error cases: {', '.join(report.agent_errors) or 'none'}")
    models = ", ".join(f"{m} {n}" for m, n in report.models.most_common())
    console.print(f"models in responses: {models or 'none'}")
