"""Agreement between two label sets, per criterion (Day 12).

Two labelers' labels are matched by case_id. Per criterion:

* **applicability agreement**: how often both chose n_a or both judged (pass/fail);
* on the pairs where **both judged**: n, percent agreement, a 2x2 confusion matrix (rows:
  labeler A pass/fail, columns: labeler B pass/fail), each labeler's fail prevalence,
  **Cohen's kappa** and **Gwet's AC1**, each with a 95% percentile bootstrap CI over the
  pairs (seed 42, 2000 resamples).

Cohen's kappa = (po - pe) / (1 - pe), pe = pA_pass * pB_pass + pA_fail * pB_fail. It is
undefined when pe = 1, i.e. both labelers used one and the same verdict on every pair;
that is reported as undefined with the reason, never as 0 or NaN. With rare fails kappa
also misbehaves when defined (the "kappa paradox": 94% agreement can give kappa < 0),
which is why AC1 is reported next to it.

Gwet's AC1 = (po - pe) / (1 - pe), pe = 2 * pi * (1 - pi), pi = the mean of the two
labelers' fail prevalences. pe is at most 0.5, so AC1 is defined whenever n >= 1.

Arithmetic is exact (fractions) so the textbook values come out exactly.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

from evalkit.schemas import Criterion, Label, LabelValue
from evalkit.stats.bootstrap import RESAMPLES, SEED, Interval, percentile_ci

THRESHOLD: Final = 0.6  # flag kappa (or AC1 when kappa is undefined) below this
MIN_FAIL_PAIRS: Final = 10  # flag fewer pairs than this where at least one labeler said fail

Pair = tuple[bool, bool]  # (A said fail, B said fail) on a pair where both judged


@dataclass(frozen=True)
class Estimate:
    value: float | None
    reason: str | None = None  # why it is undefined

    def __post_init__(self) -> None:
        if (self.value is None) == (self.reason is None):
            raise ValueError("an Estimate has a value or a reason, not both")


@dataclass(frozen=True)
class Confusion:
    """2x2 counts on the pairs where both judged; first letter A's verdict, second B's."""

    pass_pass: int = 0
    pass_fail: int = 0
    fail_pass: int = 0
    fail_fail: int = 0

    @classmethod
    def from_pairs(cls, pairs: Iterable[Pair]) -> "Confusion":
        counts = {(False, False): 0, (False, True): 0, (True, False): 0, (True, True): 0}
        for pair in pairs:
            counts[pair] += 1
        return cls(
            counts[(False, False)],
            counts[(False, True)],
            counts[(True, False)],
            counts[(True, True)],
        )

    @property
    def n(self) -> int:
        return self.pass_pass + self.pass_fail + self.fail_pass + self.fail_fail

    @property
    def agreed(self) -> int:
        return self.pass_pass + self.fail_fail

    @property
    def a_fails(self) -> int:
        return self.fail_pass + self.fail_fail

    @property
    def b_fails(self) -> int:
        return self.pass_fail + self.fail_fail

    @property
    def pairs_with_a_fail(self) -> int:
        """Pairs where at least one labeler said fail."""
        return self.n - self.pass_pass

    def percent_agreement(self) -> float | None:
        return self.agreed / self.n if self.n else None

    def prevalence(self) -> tuple[float | None, float | None]:
        """Fail prevalence of A and of B."""
        if not self.n:
            return None, None
        return self.a_fails / self.n, self.b_fails / self.n


NO_PAIRS: Final = "no pairs where both judged"


def cohen_kappa(m: Confusion) -> Estimate:
    if not m.n:
        return Estimate(None, NO_PAIRS)
    if m.a_fails == m.b_fails and m.a_fails in (0, m.n):
        only = "fail" if m.a_fails else "pass"
        return Estimate(
            None, f"undefined: both labelers gave only {only} on all {m.n} pairs (pe = 1)"
        )
    n = m.n
    po = Fraction(m.agreed, n)
    a_f, b_f = Fraction(m.a_fails, n), Fraction(m.b_fails, n)
    pe = a_f * b_f + (1 - a_f) * (1 - b_f)
    return Estimate(float((po - pe) / (1 - pe)))


def gwet_ac1(m: Confusion) -> Estimate:
    if not m.n:
        return Estimate(None, NO_PAIRS)
    n = m.n
    po = Fraction(m.agreed, n)
    pi = Fraction(m.a_fails + m.b_fails, 2 * n)
    pe = 2 * pi * (1 - pi)
    return Estimate(float((po - pe) / (1 - pe)))


def _kappa_of(pairs: Sequence[Pair]) -> float | None:
    return cohen_kappa(Confusion.from_pairs(pairs)).value


def _ac1_of(pairs: Sequence[Pair]) -> float | None:
    return gwet_ac1(Confusion.from_pairs(pairs)).value


@dataclass(frozen=True)
class Applicability:
    both_na: int = 0
    both_judged: int = 0
    a_na_b_judged: int = 0
    a_judged_b_na: int = 0

    @property
    def n(self) -> int:
        return self.both_na + self.both_judged + self.a_na_b_judged + self.a_judged_b_na

    def agreement(self) -> float | None:
        return (self.both_na + self.both_judged) / self.n if self.n else None


@dataclass(frozen=True)
class CriterionAgreement:
    criterion: Criterion
    applicability: Applicability
    confusion: Confusion
    kappa: Estimate
    ac1: Estimate
    kappa_ci: Interval
    ac1_ci: Interval

    def headline(self) -> tuple[str, float | None]:
        """Kappa, or AC1 when kappa is undefined. (With two verdicts kappa is undefined only
        when every pair agrees on one verdict, and AC1 is then 1.)"""
        if self.kappa.value is not None:
            return "kappa", self.kappa.value
        return "AC1", self.ac1.value

    def flags(self) -> list[str]:
        """Why this criterion's agreement can't be taken at face value (empty: fine)."""
        m = self.confusion
        if not m.n:
            return [NO_PAIRS]
        out = []
        name, value = self.headline()
        if value is not None and value < THRESHOLD:
            out.append(f"{name} below {THRESHOLD}")
        if m.pairs_with_a_fail < MIN_FAIL_PAIRS:
            out.append(
                f"too few fails to judge ({m.pairs_with_a_fail} pair(s) with a fail, "
                f"fewer than {MIN_FAIL_PAIRS})"
            )
        return out


def criterion_agreement(
    criterion: Criterion,
    pairs_of_labels: Sequence[tuple[Label, Label]],
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> CriterionAgreement:
    counts = {"both_na": 0, "both_judged": 0, "a_na_b_judged": 0, "a_judged_b_na": 0}
    pairs: list[Pair] = []
    for a, b in pairs_of_labels:
        va, vb = a.verdicts[criterion], b.verdicts[criterion]
        a_na, b_na = va is LabelValue.N_A, vb is LabelValue.N_A
        if a_na and b_na:
            counts["both_na"] += 1
        elif a_na:
            counts["a_na_b_judged"] += 1
        elif b_na:
            counts["a_judged_b_na"] += 1
        else:
            counts["both_judged"] += 1
            pairs.append((va is LabelValue.FAIL, vb is LabelValue.FAIL))
    m = Confusion.from_pairs(pairs)
    kappa, ac1 = cohen_kappa(m), gwet_ac1(m)
    none = Interval(None, None, 0, resamples)
    return CriterionAgreement(
        criterion=criterion,
        applicability=Applicability(**counts),
        confusion=m,
        kappa=kappa,
        ac1=ac1,
        kappa_ci=percentile_ci(pairs, _kappa_of, resamples=resamples, seed=seed)
        if kappa.value is not None
        else none,
        ac1_ci=percentile_ci(pairs, _ac1_of, resamples=resamples, seed=seed)
        if ac1.value is not None
        else none,
    )


def match(a: Mapping[str, Label], b: Mapping[str, Label]) -> list[tuple[Label, Label]]:
    """(A's label, B's label) for every case_id both labeled, sorted by case_id."""
    return [(a[cid], b[cid]) for cid in sorted(a.keys() & b.keys())]


def agreement(
    a: Mapping[str, Label],
    b: Mapping[str, Label],
    criteria: Iterable[Criterion] = tuple(Criterion),
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> list[CriterionAgreement]:
    """Per-criterion agreement over the cases both labeled (``a``/``b``: case_id -> label)."""
    pairs = match(a, b)
    return [criterion_agreement(c, pairs, resamples=resamples, seed=seed) for c in criteria]
