"""Subsets of the labeled sample for agreement checks (Day 12).

* **Overlap set** (``overlap_v1.jsonl``, 50 records) for a second labeler. Fails are rare,
  and agreement on them matters most, so about half the records are ones where the first
  labeler gave at least one fail: first every record with a fail on a *rare* criterion (one
  with fewer than RARE_FAIL_MAX fails in the first labeler's labels), then other fail
  records stratified by expected route; the other half has no fail, stratified by route
  with at least ROUTE_FLOOR per route where possible.
* **Relabel set** (``relabel_v1.jsonl``, 30 records) for a blind self-relabel later: drawn
  from the records not in the overlap set (records from it are used only if a part runs
  out, and counted), lightly enriched: about 10 fail records stratified by route, the rest
  no-fail stratified with the same floor.

Selection reads the first labeler's verdicts, but the output is plain ``LabelSample``
records (case + saved response; the schema has no verdict or note fields), sorted by
case_id so the enriched records aren't grouped. The MANIFEST entry records counts only,
never which records were enriched. Deterministic: one ``random.Random(seed)``, pools
sorted by case_id.
"""

import random
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from evalkit.hashing import file_sha256
from evalkit.io import write_jsonl
from evalkit.labeling.sampling import SEED, largest_remainder, stratum_of
from evalkit.manifest import DatasetSpec, upsert_entry
from evalkit.schemas import Criterion, Label, LabelSample, LabelValue

OVERLAP_FILE: Final = "overlap_v1.jsonl"
RELABEL_FILE: Final = "relabel_v1.jsonl"
OVERLAP_SIZE: Final = 50
OVERLAP_ENRICHED: Final = 25
RELABEL_SIZE: Final = 30
RELABEL_ENRICHED: Final = 10
RARE_FAIL_MAX: Final = 20  # a criterion with fewer fails than this (but some) is rare
ROUTE_FLOOR: Final = 2  # per route in the no-fail part, where the route has that many


def route_of(sample: LabelSample) -> str:
    return stratum_of(sample)[0]


def fail_ids(labels: Mapping[str, Label]) -> set[str]:
    """case_ids where the labeler gave at least one fail."""
    return {cid for cid, lab in labels.items() if lab.fails}


def rare_criteria(labels: Mapping[str, Label]) -> list[Criterion]:
    """Criteria with at least one but fewer than RARE_FAIL_MAX fails."""
    fails = Counter(c for lab in labels.values() for c in lab.fails)
    return [c for c in Criterion if 0 < fails[c] < RARE_FAIL_MAX]


def stratified(
    pool: Sequence[LabelSample], k: int, rng: random.Random, floor: int = 0
) -> list[LabelSample]:
    """``k`` records (or all of ``pool`` if smaller) split over expected routes in proportion
    to their size, at least ``floor`` per route where it has that many."""
    by: dict[str, list[LabelSample]] = {}
    for s in sorted(pool, key=lambda s: s.case_id):
        by.setdefault(route_of(s), []).append(s)
    sizes = {r: len(v) for r, v in by.items()}
    quota = largest_remainder(min(k, len(pool)), sizes, {r: floor for r in by}, sizes)
    picked: list[LabelSample] = []
    for route in sorted(by):
        picked += rng.sample(by[route], quota[route])
    return picked


@dataclass(frozen=True)
class Subset:
    records: list[LabelSample]  # sorted by case_id
    enriched: int  # records where the first labeler gave at least one fail
    rare_fail_records: int  # of those, records with a fail on a rare criterion
    rare: list[Criterion]
    from_overlap: int = 0  # relabel set only: records taken from the overlap set

    def by_route(self) -> dict[str, int]:
        return dict(sorted(Counter(route_of(s) for s in self.records).items()))


def _split(
    samples: Sequence[LabelSample], labels: Mapping[str, Label]
) -> tuple[list[LabelSample], list[LabelSample]]:
    missing = sorted(s.case_id for s in samples if s.case_id not in labels)
    if missing:
        raise ValueError(f"{len(missing)} record(s) have no label, e.g. {missing[:3]}")
    fails = fail_ids(labels)
    failing = [s for s in samples if s.case_id in fails]
    return failing, [s for s in samples if s.case_id not in fails]


def _check_size(samples: Sequence[LabelSample], size: int, enriched: int) -> None:
    if not 0 <= enriched <= size <= len(samples):
        raise ValueError(f"need 0 <= enriched ({enriched}) <= size ({size}) <= {len(samples)}")


def select_overlap(
    samples: Sequence[LabelSample],
    labels: Mapping[str, Label],
    *,
    size: int = OVERLAP_SIZE,
    enriched: int = OVERLAP_ENRICHED,
    seed: int = SEED,
) -> Subset:
    _check_size(samples, size, enriched)
    rng = random.Random(seed)
    failing, clean = _split(samples, labels)
    rare = rare_criteria(labels)
    rare_pool = [s for s in failing if any(labels[s.case_id].verdicts[c] is LabelValue.FAIL
                                           for c in rare)]  # fmt: skip
    rare_picked = stratified(rare_pool, enriched, rng)
    taken = {s.case_id for s in rare_picked}
    other_fail = stratified(
        [s for s in failing if s.case_id not in taken], enriched - len(rare_picked), rng
    )
    fail_part = rare_picked + other_fail
    no_fail = stratified(clean, size - len(fail_part), rng, ROUTE_FLOOR)
    records = sorted(fail_part + no_fail, key=lambda s: s.case_id)
    return Subset(records, len(fail_part), len(rare_picked), rare)


def select_relabel(
    samples: Sequence[LabelSample],
    labels: Mapping[str, Label],
    exclude: Iterable[str],
    *,
    size: int = RELABEL_SIZE,
    enriched: int = RELABEL_ENRICHED,
    seed: int = SEED,
) -> Subset:
    _check_size(samples, size, enriched)
    rng = random.Random(seed)
    excluded = set(exclude)
    failing, clean = _split(samples, labels)

    def part(pool: list[LabelSample], k: int, floor: int) -> tuple[list[LabelSample], int]:
        fresh = [s for s in pool if s.case_id not in excluded]
        picked = stratified(fresh, k, rng, floor)
        short = k - len(picked)  # only if the fresh pool ran out: top up from the excluded
        if short > 0:
            picked += stratified([s for s in pool if s.case_id in excluded], short, rng, floor)
        return picked, max(short, 0)

    fail_part, from_fail = part(failing, enriched, 0)
    no_fail, from_clean = part(clean, size - len(fail_part), ROUTE_FLOOR)
    rare = rare_criteria(labels)
    rare_n = sum(
        1 for s in fail_part if any(labels[s.case_id].verdicts[c] is LabelValue.FAIL for c in rare)
    )
    records = sorted(fail_part + no_fail, key=lambda s: s.case_id)
    return Subset(records, len(fail_part), rare_n, rare, from_overlap=from_fail + from_clean)


def write_subset(
    subset: Subset,
    out: Path,
    *,
    sample_file: Path,
    labels_file: Path,
    labeler: str,
    seed: int,
    rubric_hash: str,
    description: str,
    extra: Mapping[str, object] | None = None,
) -> None:
    """Write the records and a MANIFEST entry next to ``out`` (counts only)."""
    write_jsonl(out, subset.records)
    upsert_entry(
        out.parent,
        out.name,
        DatasetSpec(
            version="1.0",
            kind="label_samples",
            sources=[sample_file.name],
            description=description,
            metadata={
                "seed": seed,
                "size": len(subset.records),
                "enriched": subset.enriched,
                "rare_fail_records": subset.rare_fail_records,
                "rare_criteria": [c.value for c in subset.rare],
                "no_fail": len(subset.records) - subset.enriched,
                "by_route": subset.by_route(),
                "rubric_hash": rubric_hash,
                "selected_with_labels_of": labeler,
                "labels_file": labels_file.name,
                "labels_sha256": file_sha256(labels_file),
                **(extra or {}),
            },
        ),
    )
