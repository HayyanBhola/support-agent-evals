"""The dev/test split of the human labels (Day 13): a split *definition*, not label copies.

``datasets/splits/split_v1.json`` lists the case_ids of each split. It is keyed by case_id,
so the labels can be re-versioned (labels_v1.2, v2...) without re-splitting; the loader
(``evalkit.calibration.loader``) joins a split with any labels file.

Method (seed SEED, TEST_SHARE of the cases in test):

1. **Rare-fail placement.** For each rare-fail criterion (``subsets.rare_criteria``: at
   least one but fewer than RARE_FAIL_MAX fails; faithfulness joins as soon as it has
   fails), fewest fails first: if test has none of its fail cases yet, one goes to test;
   if dev has none, one goes to dev. A criterion with a single fail case can only be in
   one split: it goes to test.
2. **Stratified fill.** Strata are (expected route, has any fail). Test gets
   round(TEST_SHARE * n) places, shared over the strata as close to their proportional
   share as the rare placements allow (``bounded_shares``: a rare case already in test
   counts toward its stratum's share); the rest of each stratum is drawn with
   ``random.Random(seed)`` from its case_ids sorted.

Rubric example case_ids must not be in either split (they were excluded at sampling);
``make_split`` refuses if one is.
"""

import json
import random
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from evalkit.labeling.sampling import SEED, stratum_of
from evalkit.labeling.subsets import RARE_FAIL_MAX, rare_criteria
from evalkit.schemas import Criterion, Label, LabelSample, LabelValue

SPLIT_DIR: Final = Path("datasets") / "splits"
SPLIT_FILE: Final = "split_v1.json"
TEST_SHARE: Final = 0.4
SPLITS: Final = ("dev", "test")
SplitName = Literal["dev", "test"]
METHOD: Final = (
    "rare-fail placement (each rare-fail criterion gets a fail case in test and in dev where "
    "possible), then stratified by (expected route, has any fail), test share 0.4, each "
    "stratum as close to its proportional share as the placements allow, "
    "random.Random(seed) over sorted case_ids"
)


def has_fail(label: Label) -> bool:
    return bool(label.fails)


def stratum(sample: LabelSample, label: Label) -> str:
    return f"{stratum_of(sample)[0]}|{'fail' if has_fail(label) else 'no_fail'}"


@dataclass(frozen=True)
class Coverage:
    criterion: Criterion
    fails: int
    dev: int
    test: int
    rare: bool

    def note(self) -> str:
        if not self.fails:
            return "no fails"
        if self.fails == 1:
            return "only 1 fail: test only"
        if self.dev and self.test:
            return "in both"
        return "not in both"  # can't happen for a rare criterion with 2+ fails


@dataclass(frozen=True)
class Split:
    dev: list[str]
    test: list[str]
    seed: int

    def of(self, name: SplitName) -> list[str]:
        return self.dev if name == "dev" else self.test


def make_split(
    samples: Mapping[str, LabelSample],
    labels: Mapping[str, Label],
    *,
    exclude: Iterable[str] = (),
    test_share: float = TEST_SHARE,
    seed: int = SEED,
) -> Split:
    """Split the labeled case_ids (``labels``' keys); see the module docstring."""
    ids = sorted(labels)
    missing = [cid for cid in ids if cid not in samples]
    if missing:
        raise ValueError(f"{len(missing)} labeled case(s) not in the sample, e.g. {missing[:3]}")
    leaked = sorted(set(ids) & set(exclude))
    if leaked:
        raise ValueError(f"rubric example case_ids in the labels: {leaked}")
    rng = random.Random(seed)
    placed: dict[str, SplitName] = {}
    fails_of = {c: [cid for cid in ids if labels[cid].verdicts[c] is LabelValue.FAIL]
                for c in Criterion}  # fmt: skip
    for criterion in sorted(rare_criteria(labels), key=lambda c: (len(fails_of[c]), c.value)):
        cases = list(fails_of[criterion])
        rng.shuffle(cases)
        for target in ("test", "dev") if len(cases) > 1 else ("test",):
            if any(placed.get(cid) == target for cid in cases):
                continue
            free = next((cid for cid in cases if cid not in placed), None)
            if free is not None:
                placed[free] = target
    by_stratum: dict[str, list[str]] = {}
    for cid in ids:
        by_stratum.setdefault(stratum(samples[cid], labels[cid]), []).append(cid)
    sizes = {k: len(v) for k, v in by_stratum.items()}
    floors = {k: sum(placed.get(cid) == "test" for cid in v) for k, v in by_stratum.items()}
    caps = {k: sizes[k] - sum(placed.get(cid) == "dev" for cid in v) for k, v in by_stratum.items()}
    quota = bounded_shares(round(test_share * len(ids)), sizes, floors, caps)
    test: list[str] = []
    for key in sorted(by_stratum):
        unplaced = [cid for cid in by_stratum[key] if cid not in placed]
        test += [cid for cid in by_stratum[key] if placed.get(cid) == "test"]
        test += rng.sample(unplaced, quota[key] - floors[key])
    chosen = set(test)
    return Split(dev=[cid for cid in ids if cid not in chosen], test=sorted(test), seed=seed)


def bounded_shares(
    total: int, sizes: Mapping[str, int], floors: Mapping[str, int], caps: Mapping[str, int]
) -> dict[str, int]:
    """Integer shares summing to ``total``, as close as possible to each key's proportional
    target (total * size / sum of sizes) while staying within [floor, cap]: a floor counts
    toward the target instead of being added on top of it. Start from the rounded-down
    target clipped to the bounds, then add to the keys furthest below their target (or take
    from those furthest above it) one at a time; ties go by key, so it is deterministic."""
    whole = sum(sizes.values())
    target = {k: total * n / whole for k, n in sizes.items()}
    alloc = {k: min(max(int(target[k]), floors.get(k, 0)), caps[k]) for k in sizes}
    while sum(alloc.values()) < total:
        open_keys = [k for k in sorted(sizes) if alloc[k] < caps[k]]
        if not open_keys:
            break
        k = max(open_keys, key=lambda k: (target[k] - alloc[k], -sorted(sizes).index(k)))
        alloc[k] += 1
    while sum(alloc.values()) > total:
        over = [k for k in sorted(sizes) if alloc[k] > floors.get(k, 0)]
        k = max(over, key=lambda k: (alloc[k] - target[k], -sorted(sizes).index(k)))
        alloc[k] -= 1
    return alloc


def coverage(split: Split, labels: Mapping[str, Label]) -> list[Coverage]:
    """Fail cases per criterion in each split (rare: the criteria step 1 placed)."""
    rare = set(rare_criteria(labels))
    out = []
    for c in Criterion:
        dev = sum(labels[cid].verdicts[c] is LabelValue.FAIL for cid in split.dev)
        test = sum(labels[cid].verdicts[c] is LabelValue.FAIL for cid in split.test)
        out.append(Coverage(c, dev + test, dev, test, c in rare))
    return out


def verdict_table(ids: Iterable[str], labels: Mapping[str, Label]) -> dict[str, dict[str, int]]:
    """criterion -> {pass, fail, n_a} over ``ids``."""
    counts = {c: Counter(labels[cid].verdicts[c].value for cid in ids) for c in Criterion}
    return {c.value: {v.value: counts[c].get(v.value, 0) for v in LabelValue} for c in Criterion}


def stratum_counts(
    ids: Iterable[str], samples: Mapping[str, LabelSample], labels: Mapping[str, Label]
) -> dict[str, int]:
    return dict(sorted(Counter(stratum(samples[cid], labels[cid]) for cid in ids).items()))


def disagreements(a: Mapping[str, Label], b: Mapping[str, Label]) -> dict[str, list[str]]:
    """case_id -> criteria where the two labelers' verdicts differ (cases both labeled)."""
    out = {}
    for cid in sorted(a.keys() & b.keys()):
        diff = [c.value for c in Criterion if a[cid].verdicts[c] is not b[cid].verdicts[c]]
        if diff:
            out[cid] = diff
    return out


class SplitFile(BaseModel):
    """``split_v1.json`` as read back."""

    model_config = ConfigDict(extra="allow", frozen=True)

    version: str
    seed: int
    method: str
    labels_file: str
    labels_sha256: str
    labels_version: str | None
    rubric_hash: str
    splits: dict[str, list[str]]

    def ids(self, name: SplitName) -> list[str]:
        return self.splits[name]


def read_split(path: Path) -> SplitFile:
    return SplitFile.model_validate_json(path.read_text(encoding="utf-8"))


def split_document(
    split: Split,
    samples: Mapping[str, LabelSample],
    labels: Mapping[str, Label],
    *,
    labels_file: str,
    labels_sha256: str,
    labels_version: str | None,
    rubric_hash: str,
    label_states: Mapping[str, int],
    overlap: Sequence[str],
    round1: Mapping[str, list[str]],
    current: Mapping[str, list[str]],
    second_labeler: str,
) -> dict[str, object]:
    """The JSON written to split_v1.json (deterministic: sorted keys and ids)."""
    in_overlap = set(overlap)
    return {
        "version": "1",
        "seed": split.seed,
        "method": METHOD,
        "test_share": TEST_SHARE,
        "rare_fail_max": RARE_FAIL_MAX,
        "labels_file": labels_file,
        "labels_sha256": labels_sha256,
        "labels_version": labels_version,
        "rubric_hash": rubric_hash,
        "label_states": dict(label_states),
        "splits": {"dev": split.dev, "test": split.test},
        "counts": {
            name: {
                "cases": len(split.of(name)),
                "by_stratum": stratum_counts(split.of(name), samples, labels),
                "verdicts": verdict_table(split.of(name), labels),
            }
            for name in SPLITS
        },
        "rare_fail_coverage": {
            cov.criterion.value: {
                "fails": cov.fails,
                "dev": cov.dev,
                "test": cov.test,
                "rare": cov.rare,
                "note": cov.note(),
            }
            for cov in coverage(split, labels)
        },  # fmt: skip
        "agreement_metadata": {
            "note": (
                f"metadata only: overlap with {second_labeler} and the criteria where the "
                "two disagree, for reporting judge scores on agreed cases separately"
            ),
            "second_labeler": second_labeler,
            "overlap": {n: [c for c in split.of(n) if c in in_overlap] for n in SPLITS},
            "disagreements_round1": {"labels": "labels_v1.jsonl", "cases": dict(round1)},
            "disagreements_current": {"labels": labels_file, "cases": dict(current)},
        },
    }


def write_split(doc: Mapping[str, object], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
