"""The labeling workflow behind the CLI: build the sample, report status, validate, export."""

import json
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from evalkit.io import read_jsonl, write_jsonl
from evalkit.labeling.applicability import applicability, mismatches
from evalkit.labeling.sampling import (
    SAMPLE_SIZE,
    SEED,
    Allocation,
    allocate,
    candidates,
    draw,
    stratum_of,
)
from evalkit.labeling.store import LabelStore
from evalkit.manifest import DatasetSpec, dataset_version, upsert_entry
from evalkit.route_report import load_run
from evalkit.rubrics import EXAMPLES_FILE, LabelState, Rubric, RubricVersions
from evalkit.schemas import Label, LabelSample, LabelValue, TestCase

SAMPLE_FILE: Final = "label_sample_v1.jsonl"
EXPORT_FILE: Final = "labels_v1.jsonl"


def route_of(sample: LabelSample) -> str:
    return stratum_of(sample)[0]


# --------------------------------------------------------------------------- sample


def build_label_sample(
    *,
    runs_dir: Path,
    run_id: str,
    dataset: Path,
    rubric_dir: Path,
    rubric_hash: str,
    out: Path,
    seed: int = SEED,
    size: int = SAMPLE_SIZE,
) -> tuple[list[LabelSample], Allocation]:
    """Draw the sample, write it, and record it in the manifest next to ``out``."""
    _, results, _ = load_run(runs_dir, run_id)
    cases = {c.id: c for c in read_jsonl(dataset, TestCase)}
    excluded = json.loads((rubric_dir / EXAMPLES_FILE).read_text(encoding="utf-8"))["case_ids"]
    pool = candidates(results, cases, excluded, run_id)
    allocation = allocate(pool, size)
    sample = draw(pool, allocation, seed)
    write_jsonl(out, sample)
    upsert_entry(
        out.parent,
        out.name,
        DatasetSpec(
            version="1.0",
            kind="label_samples",
            sources=[dataset.name],
            description="human-labeling sample (Day 9)",
            metadata={
                "run_id": run_id,
                "dataset_version": dataset_version(dataset) or dataset.name,
                "seed": seed,
                "size": size,
                "rubric_hash": rubric_hash,
                "candidates": len(pool),
                "excluded_rubric_examples": len(set(excluded)),
                "strata": {f"{r}/{t}": n for (r, t), n in sorted(allocation.by_stratum.items())},
            },
        ),
    )
    return sample, allocation


# --------------------------------------------------------------------------- status


@dataclass
class Status:
    total: int
    done_by_route: Counter[str] = field(default_factory=Counter)
    total_by_route: Counter[str] = field(default_factory=Counter)
    overrides: int = 0  # override flags across the latest labels
    stale: list[str] = field(default_factory=list)  # older rubric_hash, to re-check
    carried: list[str] = field(default_factory=list)  # older rubric_hash, carried forward
    unknown: list[str] = field(default_factory=list)  # labels whose case isn't in the sample

    @property
    def done(self) -> int:
        return sum(self.done_by_route.values())


def label_state(label: Label, current_hash: str, versions: RubricVersions | None) -> LabelState:
    """CURRENT / CARRIED / RECHECK / STALE; without ``versions`` every older label is STALE."""
    if versions is None:
        return LabelState.CURRENT if label.rubric_hash == current_hash else LabelState.STALE
    return versions.label_state(label.rubric_hash, label.case_id, current_hash)


def needs_recheck(label: Label, current_hash: str, versions: RubricVersions | None) -> bool:
    return label_state(label, current_hash, versions) in (LabelState.RECHECK, LabelState.STALE)


def status(
    samples: list[LabelSample],
    labels: dict[str, Label],
    current_hash: str,
    versions: RubricVersions | None = None,
) -> Status:
    """Progress of one labeler (``labels``: latest label per case_id)."""
    st = Status(total=len(samples), total_by_route=Counter(route_of(s) for s in samples))
    in_sample = {s.case_id: s for s in samples}
    for cid, label in sorted(labels.items()):
        if cid not in in_sample:
            st.unknown.append(cid)
            continue
        st.done_by_route[route_of(in_sample[cid])] += 1
        st.overrides += len(label.overrides)
        state = label_state(label, current_hash, versions)
        if state is LabelState.CARRIED:
            st.carried.append(cid)
        elif state is not LabelState.CURRENT:
            st.stale.append(cid)
    return st


@dataclass(frozen=True)
class Timing:
    records: int
    mean: float
    median: float
    total_minutes: float


def first_pass_timing(labels: list[Label]) -> Timing | None:
    """Labeling time per record from the earliest row per (case_id, labeler), so a later
    re-save (a correction, usually quick) doesn't replace the first-pass time."""
    first: dict[tuple[str, str], Label] = {}
    for label in labels:
        key = (label.case_id, label.labeler)
        if key not in first or label.timestamp < first[key].timestamp:
            first[key] = label
    if not first:
        return None
    secs = [lab.seconds_spent for lab in first.values()]
    return Timing(
        records=len(secs),
        mean=round(statistics.mean(secs), 1),
        median=round(statistics.median(secs), 1),
        total_minutes=round(sum(secs) / 60, 1),
    )


# --------------------------------------------------------------------------- validate


@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # information, not problems


def validate(
    samples: list[LabelSample],
    labels: list[Label],
    rubrics: list[Rubric],
    *,
    current_hash: str | None = None,
    versions: RubricVersions | None = None,
) -> Validation:
    """Errors: unknown case, response hash mismatch, a fail without notes. Warnings:
    verdicts that disagree with applicability, and (given ``current_hash``) labels to
    re-check under the current rubric; labels the current rubric carries forward get one
    note, not a warning each. (Every Label has all 7 criteria by schema.)"""
    v = Validation()
    if current_hash is not None:
        v.warnings += _hash_warnings(labels, current_hash, versions)
        v.notes += _hash_notes(labels, current_hash, versions)
    by_id = {s.case_id: s for s in samples}
    for label in labels:
        who = f"{label.case_id} ({label.labeler})"
        sample = by_id.get(label.case_id)
        if sample is None:
            v.errors.append(f"{who}: not in the labeling sample")
            continue
        if label.response_sha256 != sample.response_sha256:
            v.errors.append(f"{who}: labeled a different response (response_sha256 mismatch)")
        if label.fails and not (label.notes or "").strip():
            fails = ", ".join(c.value for c in label.fails)
            v.errors.append(f"{who}: fail on {fails} without a note")
        v.warnings += mismatches(label, applicability(rubrics, sample.case, sample.response))
    return v


def _hash_warnings(
    labels: list[Label], current_hash: str, versions: RubricVersions | None
) -> list[str]:
    out = []
    for label in labels:
        state = label_state(label, current_hash, versions)
        who = f"{label.case_id} ({label.labeler})"
        if state is LabelState.RECHECK:
            out.append(
                f"{who}: on the re-check list of the current rubric but still labeled under "
                f"{label.rubric_hash[:12]}; re-check it in the app"
            )
        elif state is LabelState.STALE:
            out.append(f"{who}: labeled under an older rubric_hash {label.rubric_hash[:12]}")
    return out


def _hash_notes(
    labels: list[Label], current_hash: str, versions: RubricVersions | None
) -> list[str]:
    states = Counter(label_state(lab, current_hash, versions) for lab in labels)
    if not states[LabelState.CARRIED]:
        return []
    note = versions.carried_note(current_hash) if versions else None
    return [
        f"{states[LabelState.CARRIED]} label(s) carry an older rubric_hash that the current "
        f"rubric carries forward, so they stay valid; {states[LabelState.CURRENT]} are under "
        f"the current rubric. {note or ''}".strip()
    ]


# --------------------------------------------------------------------------- export


def export_labels(
    store: LabelStore,
    out: Path,
    sample_file: str = SAMPLE_FILE,
    *,
    version: str = "1.0",
    changelog: str | None = None,
    current_hash: str | None = None,
    versions: RubricVersions | None = None,
) -> list[Label]:
    """Latest label per (case_id, labeler), sorted, to ``out`` plus a manifest entry (with
    each label's state against ``current_hash`` when given)."""
    labels = [lab for _, lab in sorted(store.latest().items())]
    write_jsonl(out, labels)
    verdicts = Counter(v.value for lab in labels for v in lab.verdicts.values())
    extra: dict[str, object] = {}
    if changelog:
        extra["changelog"] = changelog
    if current_hash is not None:
        states = Counter(label_state(lab, current_hash, versions).value for lab in labels)
        extra["current_rubric_hash"] = current_hash
        extra["label_states"] = {s.value: states.get(s.value, 0) for s in LabelState}
        note = versions.carried_note(current_hash) if versions else None
        if note:
            extra["carried_forward"] = note
    upsert_entry(
        out.parent,
        out.name,
        DatasetSpec(
            version=version,
            kind="labels",
            sources=[sample_file],
            description="human labels, latest per case per labeler (Day 9+)",
            metadata={
                "labelers": dict(sorted(Counter(lab.labeler for lab in labels).items())),
                "rubric_hashes": sorted({lab.rubric_hash for lab in labels}),
                "verdicts": {v.value: verdicts.get(v.value, 0) for v in LabelValue},
                "overrides": sum(len(lab.overrides) for lab in labels),
                **extra,
            },
        ),
    )
    return labels
