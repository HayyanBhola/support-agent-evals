"""``evalkit score``: every deterministic metric on a saved run, and its scorecard (Day 15).

``score_run`` gives one MetricResult per case per metric (sorted by case_id, then metric),
from the saved results and a dataset version, with no agent, LLM or cache. ``scorecard``
renders the rows as Markdown: pass rates with 95% percentile bootstrap CIs, escalation
recall / precision / F1, the routing confusion matrix, breakdowns by expected route and by
dataset source, and the latency and token tables. The output has no timestamps, so the
same run and dataset always give byte-identical files.
"""

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from evalkit.metrics import database, ops, routing, rules, voice
from evalkit.metrics.routing import EscalationSummary
from evalkit.route_report import SOURCE_LABELS
from evalkit.schemas import CaseResult, MetricResult, TestCase
from evalkit.stats.bootstrap import RESAMPLES, SEED, Interval, percentile_ci

METRICS_FILE: Final = "metrics.jsonl"
SCORECARD_FILE: Final = "scorecard.md"
PASS_METRICS: Final = (
    routing.ROUTE_MATCH,
    routing.ESCALATION_MATCH,
    database.DB_FACTS,
    voice.VOICE_THIRD_PERSON,
    rules.CONTENT_RULES,
)
NOTES: Final = {
    routing.ROUTE_MATCH: "reply route = expected route",
    routing.ESCALATION_MATCH: "escalated = should_escalate",
    database.DB_FACTS: "reply matches gold_db_facts",
    voice.VOICE_THIRD_PERSON: "no third-person voice (pass = clean)",
    rules.CONTENT_RULES: "substring rules: **weak signal, not correctness**",
}


def score_run(
    results: Iterable[CaseResult], cases: Mapping[str, TestCase], allowlist: Sequence[str] = ()
) -> list[MetricResult]:
    """Every deterministic metric for every result whose case is in ``cases``."""
    results = [r for r in results if r.case_id in cases]
    catalog = database.catalog_from(cases.values(), [r.response for r in results])
    rows: list[MetricResult] = []
    for r in results:
        case, resp = cases[r.case_id], r.response
        candidates = (
            routing.route_match(case, resp),
            routing.escalation_match(case, resp),
            rules.content_rules(case, resp),
            voice.voice_third_person(case, resp, allowlist),
            database.db_facts(case, resp, catalog),
            ops.latency(r),
            ops.tokens(r),
        )
        rows += [row for row in candidates if row is not None]
    return sorted(rows, key=lambda row: (row.case_id, row.metric))


# --------------------------------------------------------------------------- rates


@dataclass(frozen=True)
class Rate:
    passed: int
    n: int
    ci: Interval

    @property
    def rate(self) -> float | None:
        return self.passed / self.n if self.n else None


def _mean(values: Sequence[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def rate(rows: Iterable[MetricResult], *, resamples: int = RESAMPLES, seed: int = SEED) -> Rate:
    flags = [bool(r.passed) for r in rows]
    return Rate(sum(flags), len(flags), percentile_ci(flags, _mean, resamples=resamples, seed=seed))


def escalation_ci(
    rows: Sequence[MetricResult],
    stat: Callable[[EscalationSummary], float | None],
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> Interval:
    def of(sample: Sequence[MetricResult]) -> float | None:
        return stat(EscalationSummary.from_rows(sample))

    return percentile_ci(list(rows), of, resamples=resamples, seed=seed)


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def _ci(ci: Interval) -> str:
    if ci.low is None or ci.high is None:
        return "–"
    return f"[{100 * ci.low:.1f}, {100 * ci.high:.1f}]"


def _rate_cells(r: Rate) -> str:
    return f"{r.passed}/{r.n} | {_pct(r.rate)} | {_ci(r.ci)}"


def group_of(case: TestCase, by: str) -> str:
    if by == "route":
        return case.expected_route.value if case.expected_route else "(none)"
    return SOURCE_LABELS[case.source]


# --------------------------------------------------------------------------- markdown


def scorecard(
    run_id: str,
    dataset: str,
    dataset_sha256: str,
    rows: Sequence[MetricResult],
    cases: Mapping[str, TestCase],
    *,
    results_count: int,
    price_in: float | None = None,
    price_out: float | None = None,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> str:
    by_metric: dict[str, list[MetricResult]] = {}
    for r in rows:
        by_metric.setdefault(r.metric, []).append(r)
    kw = {"resamples": resamples, "seed": seed}
    lines = [
        f"# Scorecard: run {run_id}",
        "",
        f"- **Dataset:** `{dataset}` (sha256 `{dataset_sha256[:12]}`); {results_count} results "
        f"scored, {len({r.case_id for r in rows})} cases with at least one metric.",
        "- **Deterministic metrics only** (no LLM, no agent, no cache). 95% percentile "
        f"bootstrap CIs over cases ({resamples} resamples, seed {seed}).",
        "- `content_rules` is the agent's substring check: a **weak signal, never correctness** "
        "(correct paraphrases fail it).",
        "",
        "## Headline",
        "",
        "| Metric | What it checks | Passed | Rate | 95% CI |",
        "|---|---|---|---|---|",
    ]
    for m in PASS_METRICS:
        lines.append(f"| {m} | {NOTES[m]} | {_rate_cells(rate(by_metric.get(m, []), **kw))} |")
    esc = by_metric.get(routing.ESCALATION_MATCH, [])
    s = EscalationSummary.from_rows(esc)
    lines += [
        "",
        "## Escalation (should_escalate set)",
        "",
        "A missed escalation is worse than an extra one, so recall comes first.",
        "",
        "| | Value | 95% CI |",
        "|---|---|---|",
        f"| **Recall** (needed a human and got one) | {_pct(s.recall)} ({s.tp}/{s.tp + s.fn}) "
        f"| {_ci(escalation_ci(esc, lambda x: x.recall, **kw))} |",
        f"| Precision (escalated and needed it) | {_pct(s.precision)} ({s.tp}/{s.tp + s.fp}) "
        f"| {_ci(escalation_ci(esc, lambda x: x.precision, **kw))} |",
        f"| F1 | {'–' if s.f1 is None else f'{s.f1:.3f}'} | "
        f"{_f1_ci(escalation_ci(esc, lambda x: x.f1, **kw))} |",
        "",
        f"TP {s.tp}, FN {s.fn} (missed), FP {s.fp} (extra), TN {s.tn}.",
        "",
        *_confusion(by_metric.get(routing.ROUTE_MATCH, [])),
    ]
    for by, title in (("route", "expected route"), ("source", "dataset source")):
        lines += _breakdown(by_metric, cases, by, title, kw)
    lines += _db_section(by_metric.get(database.DB_FACTS, []))
    lines += _voice_section(by_metric.get(voice.VOICE_THIRD_PERSON, []))
    lines += _ops_section(by_metric, price_in, price_out)
    return "\n".join(lines) + "\n"


def _f1_ci(ci: Interval) -> str:
    if ci.low is None or ci.high is None:
        return "–"
    return f"[{ci.low:.3f}, {ci.high:.3f}]"


def _confusion(route_rows: Sequence[MetricResult]) -> list[str]:
    conf = routing.route_confusion(route_rows)
    actuals = sorted({a for c in conf.values() for a in c})
    lines = [
        "## Routing confusion (rows: expected, columns: actual)",
        "",
        "| expected \\ actual | " + " | ".join(actuals) + " | accuracy |",
        "|---|" + "---|" * (len(actuals) + 1),
    ]
    for expected, counts in conf.items():
        n = sum(counts.values())
        cells = " | ".join(str(counts.get(a, 0) or "") for a in actuals)
        lines.append(f"| {expected} | {cells} | {_pct(counts.get(expected, 0) / n)} ({n}) |")
    return [*lines, ""]


def _breakdown(
    by_metric: Mapping[str, list[MetricResult]],
    cases: Mapping[str, TestCase],
    by: str,
    title: str,
    kw: Mapping[str, int],
) -> list[str]:
    groups = sorted({group_of(c, by) for c in cases.values()})
    lines = [
        f"## By {title}",
        "",
        "Pass rate (passed/n) per metric; 95% CI in brackets.",
        "",
        f"| {title} | " + " | ".join(PASS_METRICS) + " |",
        "|---|" + "---|" * len(PASS_METRICS),
    ]
    for g in groups:
        cells = []
        for m in PASS_METRICS:
            sub = [r for r in by_metric.get(m, []) if group_of(cases[r.case_id], by) == g]
            r = rate(sub, resamples=kw["resamples"], seed=kw["seed"])
            cells.append("–" if not r.n else f"{_pct(r.rate)} ({r.passed}/{r.n}) {_ci(r.ci)}")
        lines.append(f"| {g} | " + " | ".join(cells) + " |")
    return [*lines, ""]


def _db_section(rows: Sequence[MetricResult]) -> list[str]:
    failed = Counter(c for r in rows for c in r.details["failed"])
    lines = ["## db_facts: failed checks", ""]
    if not failed:
        return [*lines, "None.", ""]
    lines += ["| Check | Failed |", "|---|---|"]
    lines += [f"| {c} | {n} |" for c, n in sorted(failed.items())]
    wrong = [f"{r.case_id} ({r.details['different_product']})" for r in rows
             if r.details["different_product"]]  # fmt: skip
    if wrong:
        lines += ["", "Answered a different product: " + ", ".join(wrong)]
    return [*lines, ""]


def _voice_section(rows: Sequence[MetricResult]) -> list[str]:
    flagged = [r for r in rows if not r.passed]
    phrases = Counter(p for r in flagged for p in r.details["phrases"])
    lines = [
        "## voice_third_person",
        "",
        f"{len(flagged)} of {len(rows)} replies flagged. Phrases: "
        + (", ".join(f"{p} ({n})" for p, n in phrases.most_common()) or "none")
        + ".",
        "",
    ]
    return lines


def _ops_section(
    by_metric: Mapping[str, list[MetricResult]], price_in: float | None, price_out: float | None
) -> list[str]:
    lat = ops.latency_by_route(by_metric.get(ops.LATENCY, []))
    tok = ops.tokens_by_route(by_metric.get(ops.TOKENS, []))
    lines = [
        "## Latency by actual route (agent latency_ms)",
        "",
        "| Route | n | p50 ms | p95 ms | max ms |",
        "|---|---|---|---|---|",
    ]
    lines += [f"| {k} | {v.n} | {v.p50:.0f} | {v.p95:.0f} | {v.max:.0f} |" for k, v in lat.items()]
    lines += [
        "",
        "## Tokens by actual route (replies that called the LLM)",
        "",
        "| Route | LLM replies | prompt | completion | total | mean per reply |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {k} | {v.n} | {v.prompt} | {v.completion} | {v.total} | {v.total / v.n:.0f} |"
        for k, v in tok.items()
    ]
    cost = tok["all"].cost_usd(price_in, price_out) if "all" in tok else None
    lines += [
        "",
        f"**Cost:** ${cost:.4f} at ${price_in} / ${price_out} per 1M prompt / completion tokens."
        if cost is not None
        else "**Cost:** not computed: no price configured (pass `--price-in` / `--price-out`).",
    ]
    return lines
