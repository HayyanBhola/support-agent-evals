"""Agreement math (Day 12): textbook values computed by hand, degenerate cases, the CI."""

from datetime import UTC, datetime
from typing import Any

import pytest

from evalkit.calibration.agreement import (
    NO_PAIRS,
    Confusion,
    Estimate,
    agreement,
    cohen_kappa,
    criterion_agreement,
    gwet_ac1,
    match,
)
from evalkit.schemas import Criterion, Label
from evalkit.stats.bootstrap import Interval, percentile_ci, quantile

T0 = datetime(2026, 10, 14, tzinfo=UTC)


def m(pp: int, pf: int, fp: int, ff: int) -> Confusion:
    """Rows: A pass / A fail; columns: B pass / B fail."""
    return Confusion(pass_pass=pp, pass_fail=pf, fail_pass=fp, fail_fail=ff)


def pairs_of(c: Confusion) -> list[tuple[bool, bool]]:
    return (
        [(False, False)] * c.pass_pass
        + [(False, True)] * c.pass_fail
        + [(True, False)] * c.fail_pass
        + [(True, True)] * c.fail_fail
    )


def test_perfect_agreement() -> None:
    c = m(40, 0, 0, 10)
    assert c.percent_agreement() == 1.0
    assert cohen_kappa(c).value == 1.0
    assert gwet_ac1(c).value == 1.0


def test_chance_level_agreement() -> None:
    # po = 0.5; pe = 0.5*0.5 + 0.5*0.5 = 0.5 -> kappa 0; pi = 0.5, pe = 2*0.5*0.5 = 0.5 -> AC1 0
    c = m(25, 25, 25, 25)
    assert c.percent_agreement() == 0.5
    assert cohen_kappa(c).value == 0.0
    assert gwet_ac1(c).value == 0.0


def test_textbook_two_by_two() -> None:
    # n = 50, po = 35/50 = 0.7; A fails 25/50 = 0.5, B fails 20/50 = 0.4
    # kappa: pe = 0.5*0.4 + 0.5*0.6 = 0.5 -> (0.7 - 0.5) / 0.5 = 0.4
    # AC1:   pi = 0.45, pe = 2*0.45*0.55 = 0.495 -> 0.205 / 0.505 = 41/101
    c = m(20, 5, 10, 15)
    assert c.prevalence() == (0.5, 0.4)
    assert cohen_kappa(c).value == pytest.approx(0.4)
    assert gwet_ac1(c).value == pytest.approx(41 / 101)


def test_kappa_paradox_rare_fails() -> None:
    # n = 125, po = 118/125 = 0.944, but A fails 2 and B fails 5 times, never together.
    # kappa: pe = (2/125)(5/125) + (123/125)(120/125) = 14770/15625
    #        -> (14750 - 14770) / (15625 - 14770) = -20/855
    # AC1:   pi = 7/250, pe = 2*(7/250)*(243/250) = 3402/62500
    #        -> (59000 - 3402) / (62500 - 3402) = 55598/59098
    c = m(118, 5, 2, 0)
    assert c.percent_agreement() == pytest.approx(0.944)
    assert cohen_kappa(c).value == pytest.approx(-20 / 855)
    assert gwet_ac1(c).value == pytest.approx(55598 / 59098)


@pytest.mark.parametrize(("c", "only"), [(m(30, 0, 0, 0), "pass"), (m(0, 0, 0, 7), "fail")])
def test_all_one_class_kappa_is_undefined_with_a_reason(c: Confusion, only: str) -> None:
    k = cohen_kappa(c)
    assert k.value is None
    assert k.reason is not None and f"only {only} on all {c.n} pairs" in k.reason
    assert gwet_ac1(c).value == 1.0  # pi = 0 or 1 -> pe = 0, po = 1


def test_opposite_single_classes_are_defined() -> None:
    c = m(0, 9, 0, 0)  # A always pass, B always fail: po = 0, pe = 0
    assert cohen_kappa(c).value == 0.0
    assert gwet_ac1(c).value == pytest.approx(-1.0)  # pi = 0.5, pe = 0.5


def test_no_pairs() -> None:
    c = m(0, 0, 0, 0)
    assert c.percent_agreement() is None
    assert c.prevalence() == (None, None)
    assert cohen_kappa(c) == Estimate(None, NO_PAIRS)
    assert gwet_ac1(c) == Estimate(None, NO_PAIRS)


def test_estimate_has_a_value_or_a_reason() -> None:
    with pytest.raises(ValueError):
        Estimate(None)
    with pytest.raises(ValueError):
        Estimate(0.5, "both")


def test_quantile_interpolates() -> None:
    assert quantile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    assert quantile([0.0, 10.0], 0.25) == 2.5
    assert quantile([7.0], 0.975) == 7.0
    with pytest.raises(ValueError):
        quantile([], 0.5)


def test_bootstrap_ci_is_deterministic_per_seed() -> None:
    pairs = pairs_of(m(20, 5, 10, 15))

    def kappa(p: list[tuple[bool, bool]]) -> float | None:
        return cohen_kappa(Confusion.from_pairs(p)).value

    a = percentile_ci(pairs, kappa, resamples=500, seed=42)
    b = percentile_ci(pairs, kappa, resamples=500, seed=42)
    c = percentile_ci(pairs, kappa, resamples=500, seed=7)
    assert a == b
    assert a != c
    assert a.defined and a.low is not None and a.high is not None
    assert a.low < 0.4 < a.high
    assert a.used == 500


def test_bootstrap_drops_and_counts_undefined_resamples() -> None:
    pairs = [(False, False)] * 19 + [(True, True)]  # resamples without the fail: undefined
    ci = percentile_ci(pairs, lambda p: cohen_kappa(Confusion.from_pairs(p)).value, seed=42)
    assert 0 < ci.used < ci.resamples
    assert percentile_ci([], lambda p: 1.0) == Interval(None, None, 0, 2000)
    always_undefined = percentile_ci(pairs, lambda p: None, resamples=10)
    assert not always_undefined.defined and always_undefined.used == 0


# --------------------------------------------------------------------------- labels


def label(cid: str, labeler: str, **verdicts: str) -> Label:
    data: dict[str, Any] = {
        "case_id": cid,
        "run_id": "run-1",
        "labeler": labeler,
        "timestamp": T0,
        "rubric_hash": "a" * 64,
        "response_sha256": "b" * 64,
        "verdicts": {c.value: "pass" for c in Criterion} | verdicts,
        "seconds_spent": 10.0,
    }
    return Label.model_validate(data)


def test_criterion_agreement_from_labels() -> None:
    a = {
        "c1": label("c1", "a", safety="fail", tone="n_a"),
        "c2": label("c2", "a", safety="pass", tone="pass"),
        "c3": label("c3", "a", safety="n_a"),
        "c4": label("c4", "a", safety="fail", tone="n_a"),
        "only-a": label("only-a", "a"),
    }
    b = {
        "c1": label("c1", "b", safety="fail", tone="n_a"),
        "c2": label("c2", "b", safety="fail", tone="n_a"),
        "c3": label("c3", "b", safety="pass"),
        "c4": label("c4", "b", safety="pass"),
        "only-b": label("only-b", "b"),
    }
    assert [x.case_id for x, _ in match(a, b)] == ["c1", "c2", "c3", "c4"]
    by = {r.criterion: r for r in agreement(a, b, resamples=200)}

    safety = by[Criterion.SAFETY]
    assert safety.applicability.a_na_b_judged == 1 and safety.applicability.both_judged == 3
    assert safety.applicability.agreement() == 0.75
    assert safety.confusion == m(0, 1, 1, 1)
    assert safety.confusion.pairs_with_a_fail == 3

    tone = by[Criterion.TONE]
    ta = tone.applicability
    assert (ta.both_na, ta.a_judged_b_na, ta.a_na_b_judged, ta.both_judged) == (1, 1, 1, 1)
    assert tone.confusion == m(1, 0, 0, 0)
    assert tone.kappa.value is None and tone.kappa_ci == Interval(None, None, 0, 200)
    assert tone.ac1.value == 1.0 and tone.ac1_ci.defined
    assert any("too few fails" in f for f in tone.flags())


def test_flags() -> None:
    lab = label("c", "x")

    def flags_for(c: Confusion) -> list[str]:
        pairs = [(lab, lab)]
        r = criterion_agreement(Criterion.TONE, pairs, resamples=10)
        return type(r)(
            r.criterion, r.applicability, c, cohen_kappa(c), gwet_ac1(c), r.kappa_ci, r.ac1_ci
        ).flags()

    assert flags_for(m(30, 0, 0, 20)) == []
    assert flags_for(m(20, 5, 10, 15)) == ["kappa below 0.6"]
    assert flags_for(m(0, 0, 0, 0)) == [NO_PAIRS]
    few = flags_for(m(40, 0, 0, 0))
    assert len(few) == 1 and "too few fails" in few[0]  # AC1 = 1, kappa undefined
    assert flags_for(m(0, 0, 0, 12)) == []  # all fail on both sides: AC1 = 1, 12 fail pairs
    assert any("kappa below" in f for f in flags_for(m(118, 5, 2, 0)))
