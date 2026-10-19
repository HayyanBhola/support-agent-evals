"""Draw the human-labeling sample from a run (Day 9).

Candidates: the run's answered responses (ok, not skipped or errored) joined by case_id to
a dataset version (its gold fields), minus every rubric example case_id (labeling those
would leak the rubric's own examples into the evaluation of the judges).

Allocation (SAMPLE_SIZE records, seed SEED):

1. knowledge / ``llm`` answers, where hallucinations happen, are oversampled: a fixed quota
   of LLM_KNOWLEDGE_QUOTA (capped at what exists);
2. every other expected route gets at least ROUTE_MIN records (UNCHECKED_MIN for cases
   whose route is not checked), capped at what exists;
3. the rest is shared out in proportion to what is left in each route;
4. inside a route, its quota is split over the answer_type strata in proportion to their
   size, at least one per stratum present (largest-remainder rounding).

Within a stratum, records are drawn with ``random.Random(seed)`` from the candidates sorted
by case_id, so the same inputs always give the same sample.
"""

import random
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from evalkit.hashing import stable_hash
from evalkit.schemas import CaseResult, LabelSample, SampleStratum, TestCase

SAMPLE_SIZE: Final = 160
SEED: Final = 42
LLM_KNOWLEDGE_QUOTA: Final = 60
ROUTE_MIN: Final = 10
UNCHECKED_MIN: Final = 6
NO_ROUTE: Final = "(none)"  # expected_route null (route not checked)
NO_TYPE: Final = "none"  # response without answer_type (fixed replies, database)
LLM_KNOWLEDGE: Final = ("knowledge", "llm")

Stratum = tuple[str, str]  # (expected route, answer type)


def response_sha256(result: CaseResult) -> str:
    assert result.response is not None
    return stable_hash(result.response.model_dump(mode="json"))


def stratum_of(sample: LabelSample) -> Stratum:
    s = sample.stratum
    return (
        s.expected_route.value if s.expected_route else NO_ROUTE,
        s.answer_type.value if s.answer_type else NO_TYPE,
    )


def candidates(
    results: Iterable[CaseResult],
    cases: Mapping[str, TestCase],
    exclude: Iterable[str],
    run_id: str,
) -> list[LabelSample]:
    """Answered responses with a case in ``cases``, minus ``exclude``, sorted by case_id."""
    excluded = set(exclude)
    out = []
    for r in results:
        resp = r.response
        if resp is None or not resp.ok or r.failure_type is not None:
            continue  # skipped, failed or agent_error
        if r.case_id in excluded or r.case_id not in cases:
            continue
        case = cases[r.case_id]
        out.append(
            LabelSample(
                case_id=r.case_id,
                run_id=run_id,
                stratum=SampleStratum(
                    expected_route=case.expected_route, answer_type=resp.answer_type
                ),
                case=case,
                response=resp,
                response_sha256=response_sha256(r),
            )
        )
    return sorted(out, key=lambda s: s.case_id)


def largest_remainder(
    total: int, weights: Mapping[str, int], floors: Mapping[str, int], caps: Mapping[str, int]
) -> dict[str, int]:
    """Split ``total`` over the keys of ``weights`` (positive sizes): each key first gets its
    floor, the rest goes in proportion to ``weights``, and no key goes above its cap. The
    remainder of each round goes to the largest fractional shares, ties broken by key, so
    the result is deterministic. Stops early if every key is at its cap."""
    alloc = {k: min(floors.get(k, 0), caps[k]) for k in weights}
    left = total - sum(alloc.values())
    while left > 0:
        open_keys = [k for k in sorted(weights) if alloc[k] < caps[k]]
        if not open_keys:
            break
        weight = sum(weights[k] for k in open_keys)
        shares = {k: left * weights[k] / weight for k in open_keys}
        for k in open_keys:
            alloc[k] += min(int(shares[k]), caps[k] - alloc[k])
        left = total - sum(alloc.values())
        for k in sorted(open_keys, key=lambda k: (-(shares[k] % 1), k)):
            if left and alloc[k] < caps[k]:
                alloc[k] += 1
                left -= 1
    return alloc


@dataclass(frozen=True)
class Allocation:
    by_stratum: dict[Stratum, int]
    available: dict[Stratum, int]

    @property
    def total(self) -> int:
        return sum(self.by_stratum.values())

    def by_route(self) -> dict[str, int]:
        out: Counter[str] = Counter()
        for (route, _), n in self.by_stratum.items():
            out[route] += n
        return dict(out)


def allocate(
    pool: Sequence[LabelSample],
    size: int = SAMPLE_SIZE,
    llm_quota: int = LLM_KNOWLEDGE_QUOTA,
    route_min: int = ROUTE_MIN,
    unchecked_min: int = UNCHECKED_MIN,
) -> Allocation:
    available = Counter(stratum_of(s) for s in pool)
    if size > sum(available.values()):
        raise ValueError(f"sample size {size} exceeds the {sum(available.values())} candidates")
    fixed = {LLM_KNOWLEDGE: min(llm_quota, available.get(LLM_KNOWLEDGE, 0))}
    rest = {k: v for k, v in available.items() if k not in fixed}
    routes = sorted({r for r, _ in rest})
    route_size = {r: sum(v for (rr, _), v in rest.items() if rr == r) for r in routes}
    already = {r: sum(v for (rr, _), v in fixed.items() if rr == r) for r in routes}
    floors = {
        r: max(0, (unchecked_min if r == NO_ROUTE else route_min) - already[r]) for r in routes
    }
    route_quota = largest_remainder(size - sum(fixed.values()), route_size, floors, route_size)
    by_stratum: dict[Stratum, int] = dict(fixed)
    for r in routes:
        strata = {t: v for (rr, t), v in rest.items() if rr == r}
        quota = route_quota[r]
        ones = {t: 1 for t in strata} if quota >= len(strata) else {}
        split = largest_remainder(quota, strata, ones, strata)
        by_stratum.update({(r, t): n for t, n in split.items()})
    return Allocation({k: v for k, v in by_stratum.items() if v}, dict(available))


def draw(
    pool: Sequence[LabelSample], allocation: Allocation, seed: int = SEED
) -> list[LabelSample]:
    """The sample, sorted by case_id. Deterministic for the same pool, allocation and seed."""
    rng = random.Random(seed)
    by: dict[Stratum, list[LabelSample]] = {}
    for s in sorted(pool, key=lambda s: s.case_id):
        by.setdefault(stratum_of(s), []).append(s)
    picked: list[LabelSample] = []
    for stratum in sorted(allocation.by_stratum):
        picked += rng.sample(by[stratum], allocation.by_stratum[stratum])
    return sorted(picked, key=lambda s: s.case_id)
