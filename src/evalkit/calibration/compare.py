"""A metric's per-case pass/fail against human labels on one criterion (Day 15).

Pairs are the cases that have both a metric row and a judged label (pass/fail; n_a is
skipped). "Fail" on both sides is the positive class, as in ``agreement``: the result has
the confusion matrix (rows: human, columns: metric), percent agreement, Cohen's kappa and
Gwet's AC1 with bootstrap CIs, and every disagreeing case. Used on the dev split only.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from evalkit.calibration.agreement import Confusion, Estimate, Pair, cohen_kappa, gwet_ac1
from evalkit.schemas import Criterion, Label, LabelValue, MetricResult
from evalkit.stats.bootstrap import RESAMPLES, SEED, Interval, percentile_ci


@dataclass(frozen=True)
class MetricVsLabels:
    metric: str
    criterion: Criterion
    confusion: Confusion  # first letter: human; second: metric
    kappa: Estimate
    ac1: Estimate
    kappa_ci: Interval
    ac1_ci: Interval
    disagreements: list[tuple[str, str, str]]  # (case_id, human verdict, metric verdict)
    unpaired: int  # cases in scope without a metric row or with an n_a label


def metric_vs_labels(
    rows: Iterable[MetricResult],
    labels: Mapping[str, Label],
    metric: str,
    criterion: Criterion,
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> MetricVsLabels:
    by_case = {r.case_id: r for r in rows if r.metric == metric}
    pairs: list[Pair] = []
    disagreements = []
    unpaired = 0
    for cid in sorted(labels):
        verdict = labels[cid].verdicts[criterion]
        row = by_case.get(cid)
        if row is None or row.passed is None or verdict is LabelValue.N_A:
            unpaired += 1
            continue
        human_fail, metric_fail = verdict is LabelValue.FAIL, not row.passed
        pairs.append((human_fail, metric_fail))
        if human_fail != metric_fail:
            disagreements.append(
                (
                    cid,
                    verdict.value,
                    LabelValue.FAIL.value if metric_fail else LabelValue.PASS.value,
                )
            )
    m = Confusion.from_pairs(pairs)

    def k(sample: Sequence[Pair]) -> float | None:
        return cohen_kappa(Confusion.from_pairs(sample)).value

    def a(sample: Sequence[Pair]) -> float | None:
        return gwet_ac1(Confusion.from_pairs(sample)).value

    return MetricVsLabels(
        metric=metric,
        criterion=criterion,
        confusion=m,
        kappa=cohen_kappa(m),
        ac1=gwet_ac1(m),
        kappa_ci=percentile_ci(pairs, k, resamples=resamples, seed=seed),
        ac1_ci=percentile_ci(pairs, a, resamples=resamples, seed=seed),
        disagreements=disagreements,
        unpaired=unpaired,
    )
