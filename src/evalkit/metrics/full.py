"""``evalkit score-full`` (Day 21): the full scorecard of a saved run, and ``evalkit compare``.

Deterministic metrics (Day 15) plus the six frozen OpenAI judges on **every** case of the
dataset (applicability decides n_a; cases the run skipped, e.g. chat history, stay skipped).
This scores the agent; it doesn't calibrate the judges, so no human label is read.

Outputs, next to the run's own ``results.jsonl`` (the agent's replies, never overwritten):

- ``scores.jsonl``: one MetricResult per case per metric or judge. A judge row is
  ``judge_<criterion>`` with ``passed`` and the verdict's reason, evidence, prompt version and
  model in ``details``; judge_error and n_a cases have no row (they are counted in summary).
- ``summary.json``: pass rates with 95% percentile bootstrap CIs per metric, escalation
  recall / precision / F1, the same rates by expected_route, source and difficulty, latency
  and tokens, and the provenance (dataset sha, rubric hash, FROZEN.json hash, models, git).

``compare`` takes two runs' score rows: per metric the two rates and the difference (b - a)
with a paired bootstrap CI over the cases both runs scored.
"""

import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from evalkit.judges.frozen import FINAL_VERSIONS
from evalkit.judges.schema import JudgeResult
from evalkit.metrics import ops, routing
from evalkit.metrics.routing import EscalationSummary
from evalkit.metrics.scorecard import PASS_METRICS, escalation_ci, group_of, rate
from evalkit.schemas import MetricResult, TestCase
from evalkit.stats.bootstrap import RESAMPLES, SEED, Interval, percentile_ci

SCORES_FILE: Final = "scores.jsonl"
SUMMARY_FILE: Final = "summary.json"
JUDGES_DIR: Final = "full"  # runs/<run_id>/judges/full/<criterion>_<version>_openai.jsonl
MAX_JUDGE_ERRORS: Final = 10
JUDGE_METRICS: Final = tuple(f"judge_{c.value}" for c in FINAL_VERSIONS)
ALL_METRICS: Final = (*PASS_METRICS, *JUDGE_METRICS)
GROUPS: Final = ("route", "source", "difficulty")
JUDGE_SAMPLE_FILE: Final = "judge_sample_v1.json"  # datasets/judge_sample_v1.json


def judge_rows(results: Iterable[JudgeResult], version: str) -> list[MetricResult]:
    """A judge's verdicts as score rows (judge_error left out)."""
    return [
        MetricResult(
            case_id=r.case_id,
            metric=f"judge_{r.criterion}",
            passed=r.verdict == "pass",
            details={
                "prompt_version": version,
                "model": r.model,
                "reason": r.reason,
                "evidence": r.evidence,
            },  # fmt: skip
        )
        for r in results
        if r.verdict != "judge_error"
    ]


def judge_sample(
    cases: Mapping[str, TestCase], answered: Iterable[str], n: int, seed: int = SEED
) -> list[str]:
    """``n`` answered case ids, stratified by expected route x source: each stratum gets its
    proportional share (largest remainder), drawn with ``random.Random(seed)``."""
    strata: dict[tuple[str, str], list[str]] = {}
    for cid in sorted(set(answered) & set(cases)):
        key = (group(cases[cid], "route"), group(cases[cid], "source"))
        strata.setdefault(key, []).append(cid)
    total = sum(len(v) for v in strata.values())
    if n > total:
        raise ValueError(f"asked for {n} cases, only {total} answered")
    shares = {k: n * len(v) / total for k, v in strata.items()}
    take = {k: int(x) for k, x in shares.items()}
    by_remainder = sorted(strata, key=lambda k: (-(shares[k] - take[k]), k))
    for k in by_remainder[: n - sum(take.values())]:
        take[k] += 1
    rng = random.Random(seed)
    return sorted(cid for k in sorted(strata) for cid in rng.sample(strata[k], take[k]))


def group(case: TestCase, by: str) -> str:
    return case.difficulty.value if by == "difficulty" else group_of(case, by)


def _ci_dict(ci: Interval) -> dict[str, float | None]:
    return {"ci_low": ci.low, "ci_high": ci.high}


def rate_dict(rows: Sequence[MetricResult], resamples: int, seed: int) -> dict[str, Any]:
    r = rate(rows, resamples=resamples, seed=seed)
    return {"passed": r.passed, "n": r.n, "rate": r.rate, **_ci_dict(r.ci)}


def summarize(
    rows: Sequence[MetricResult],
    cases: Mapping[str, TestCase],
    meta: Mapping[str, Any],
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> dict[str, Any]:
    by_metric: dict[str, list[MetricResult]] = {}
    for r in rows:
        by_metric.setdefault(r.metric, []).append(r)
    metrics = [m for m in ALL_METRICS if m in by_metric]
    esc = by_metric.get(routing.ESCALATION_MATCH, [])
    s = EscalationSummary.from_rows(esc)
    stats: dict[str, Callable[[EscalationSummary], float | None]] = {
        "recall": lambda x: x.recall, "precision": lambda x: x.precision, "f1": lambda x: x.f1,
    }  # fmt: skip
    escalation = {name: {"value": f(s), **_ci_dict(escalation_ci(esc, f, resamples=resamples,
                                                                 seed=seed))}
                  for name, f in stats.items()}  # fmt: skip
    breakdowns: dict[str, dict[str, dict[str, Any]]] = {}
    for by in GROUPS:
        groups = sorted({group(c, by) for c in cases.values()})
        breakdowns[by] = {
            g: {m: rate_dict([r for r in by_metric[m] if group(cases[r.case_id], by) == g],
                             resamples, seed)
                for m in metrics}
            for g in groups
        }  # fmt: skip
    lat = ops.latency_by_route(by_metric.get(ops.LATENCY, [])).get("all")
    tok = ops.tokens_by_route(by_metric.get(ops.TOKENS, [])).get("all")
    return {
        **meta,
        "n_cases": len(cases),
        "resamples": resamples,
        "seed": seed,
        "metrics": {m: rate_dict(by_metric[m], resamples, seed) for m in metrics},
        "escalation": escalation | {"tp": s.tp, "fp": s.fp, "fn": s.fn, "tn": s.tn},
        "breakdowns": breakdowns,
        "latency_ms": {"n": lat.n, "p50": lat.p50, "p95": lat.p95, "max": lat.max} if lat else None,
        "tokens": {"llm_replies": tok.n, "prompt": tok.prompt, "completion": tok.completion}
        if tok
        else None,
    }


# --------------------------------------------------------------------------- compare


@dataclass(frozen=True)
class Delta:
    metric: str
    a: dict[str, Any]  # rate_dict of run a (all its rows)
    b: dict[str, Any]
    n_paired: int  # cases both runs scored on this metric
    delta: float | None  # b - a on the paired cases
    ci: Interval


def _passes(rows: Iterable[MetricResult], metric: str) -> dict[str, bool]:
    return {r.case_id: bool(r.passed) for r in rows if r.metric == metric and r.passed is not None}


def compare(
    rows_a: Sequence[MetricResult],
    rows_b: Sequence[MetricResult],
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> list[Delta]:
    """Per metric present in either run: both rates and b - a with a paired bootstrap CI."""
    present = {r.metric for r in [*rows_a, *rows_b]}
    out = []
    for m in [m for m in ALL_METRICS if m in present]:
        pa, pb = _passes(rows_a, m), _passes(rows_b, m)
        pairs = [(pa[c], pb[c]) for c in sorted(pa.keys() & pb.keys())]

        def diff(sample: Sequence[tuple[bool, bool]]) -> float | None:
            return sum(b - a for a, b in sample) / len(sample) if sample else None

        out.append(Delta(
            m,
            rate_dict([r for r in rows_a if r.metric == m], resamples, seed),
            rate_dict([r for r in rows_b if r.metric == m], resamples, seed),
            len(pairs),
            diff(pairs),
            percentile_ci(pairs, diff, resamples=resamples, seed=seed),
        ))  # fmt: skip
    return out
