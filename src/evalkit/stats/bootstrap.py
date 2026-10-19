"""Percentile bootstrap confidence intervals (Day 12).

Resample the items with replacement ``resamples`` times with ``random.Random(seed)``,
compute the statistic on each resample, and take the (1 - level) / 2 and (1 + level) / 2
quantiles (linear interpolation). A statistic may be undefined on a resample (it returns
None, e.g. kappa when every resampled pair is pass/pass); those resamples are dropped and
counted, so the interval says how many resamples it rests on.
"""

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, TypeVar

RESAMPLES: Final = 2000
SEED: Final = 42
LEVEL: Final = 0.95

T = TypeVar("T")


@dataclass(frozen=True)
class Interval:
    low: float | None  # None when no resample gave a defined value
    high: float | None
    used: int  # resamples where the statistic was defined
    resamples: int
    level: float = LEVEL

    @property
    def defined(self) -> bool:
        return self.low is not None and self.high is not None


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """The q-quantile of already sorted values, with linear interpolation."""
    if not sorted_values:
        raise ValueError("quantile of no values")
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def percentile_ci(
    items: Sequence[T],
    statistic: Callable[[Sequence[T]], float | None],
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
    level: float = LEVEL,
) -> Interval:
    """Percentile bootstrap interval of ``statistic`` over ``items``; deterministic per seed."""
    if not items:
        return Interval(None, None, 0, resamples, level)
    rng = random.Random(seed)
    n = len(items)
    values = []
    for _ in range(resamples):
        value = statistic([items[rng.randrange(n)] for _ in range(n)])
        if value is not None:
            values.append(value)
    if not values:
        return Interval(None, None, 0, resamples, level)
    values.sort()
    alpha = (1 - level) / 2
    return Interval(
        quantile(values, alpha), quantile(values, 1 - alpha), len(values), resamples, level
    )
