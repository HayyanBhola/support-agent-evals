"""Overlap and relabel sets (Day 12): selection rules, no label leaks, reproducibility."""

import json
import random
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalkit import cli
from evalkit.io import read_jsonl
from evalkit.labeling import subsets
from evalkit.labeling.sampling import SEED
from evalkit.manifest import read_manifest
from evalkit.schemas import Label, LabelSample, LabelValue

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "datasets"
SAMPLES = list(read_jsonl(DATASETS / "label_sample_v1.jsonl", LabelSample))
LABELS = {lab.case_id: lab for lab in read_jsonl(DATASETS / "labels_v1.jsonl", Label)}
LABEL_KEYS = {"verdicts", "notes", "labeler", "overrides", "seconds_spent"}


def ids(records: list[LabelSample]) -> set[str]:
    return {s.case_id for s in records}


def test_overlap_rules_on_the_real_labels() -> None:
    o = subsets.select_overlap(SAMPLES, LABELS)
    assert len(o.records) == 50 and len(ids(o.records)) == 50
    assert [s.case_id for s in o.records] == sorted(ids(o.records))
    fails = subsets.fail_ids(LABELS)
    assert o.enriched == 25 == len(ids(o.records) & fails)
    rare = subsets.rare_criteria(LABELS)
    assert {c.value for c in rare} == {"safety", "correctness", "completeness", "tone"}
    rare_ids = {
        cid for cid, lab in LABELS.items() if any(lab.verdicts[c] is LabelValue.FAIL for c in rare)
    }
    assert o.rare_fail_records == len(rare_ids) == 17
    assert rare_ids <= ids(o.records)  # every rare-criterion fail record is in
    assert all(n >= subsets.ROUTE_FLOOR for n in o.by_route().values())


def test_relabel_rules_on_the_real_labels() -> None:
    o = subsets.select_overlap(SAMPLES, LABELS)
    r = subsets.select_relabel(SAMPLES, LABELS, ids(o.records))
    assert len(r.records) == 30
    assert not ids(r.records) & ids(o.records)
    assert r.from_overlap == 0
    assert r.enriched == 10 == len(ids(r.records) & subsets.fail_ids(LABELS))


def test_selection_is_deterministic_and_seed_dependent() -> None:
    a = subsets.select_overlap(SAMPLES, LABELS, seed=SEED)
    assert a == subsets.select_overlap(SAMPLES, LABELS, seed=SEED)
    assert ids(a.records) != ids(subsets.select_overlap(SAMPLES, LABELS, seed=7).records)


def test_committed_files_match_a_fresh_selection() -> None:
    o = subsets.select_overlap(SAMPLES, LABELS)
    r = subsets.select_relabel(SAMPLES, LABELS, ids(o.records))
    on_disk_o = list(read_jsonl(DATASETS / subsets.OVERLAP_FILE, LabelSample))
    on_disk_r = list(read_jsonl(DATASETS / subsets.RELABEL_FILE, LabelSample))
    assert on_disk_o == o.records and on_disk_r == r.records
    meta = read_manifest(DATASETS / "MANIFEST.json").datasets
    assert meta[subsets.OVERLAP_FILE].metadata["enriched"] == 25
    assert meta[subsets.RELABEL_FILE].metadata["from_overlap"] == 0


def test_written_files_carry_no_labels(tmp_path: Path) -> None:
    for name in (subsets.OVERLAP_FILE, subsets.RELABEL_FILE):
        text = (DATASETS / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            assert not LABEL_KEYS & json.loads(line).keys()
        notes = {lab.notes for lab in LABELS.values() if lab.notes}
        assert not [n for n in notes if n in text]
    meta = read_manifest(DATASETS / "MANIFEST.json").datasets[subsets.OVERLAP_FILE].metadata
    assert not any(isinstance(v, list) and set(v) & set(LABELS) for v in meta.values())


def test_relabel_tops_up_from_the_excluded_set_when_a_part_runs_out() -> None:
    fails = sorted(subsets.fail_ids(LABELS))
    excluded = set(fails[:45])  # leaves 5 fail records outside the overlap set
    r = subsets.select_relabel(SAMPLES, LABELS, excluded, size=30, enriched=10)
    assert r.enriched == 10 and r.from_overlap == 5
    assert len(ids(r.records) & excluded) == 5


def test_bad_inputs() -> None:
    with pytest.raises(ValueError, match="no label"):
        subsets.select_overlap(SAMPLES, {})
    with pytest.raises(ValueError, match="enriched"):
        subsets.select_overlap(SAMPLES, LABELS, size=10, enriched=20)
    with pytest.raises(ValueError, match="enriched"):
        subsets.select_relabel(SAMPLES, LABELS, [], size=500)


def test_stratified_respects_floors_and_small_pools() -> None:
    rng = random.Random(1)
    picked = subsets.stratified(SAMPLES, 16, rng, floor=2)
    routes = {subsets.route_of(s) for s in SAMPLES}
    assert len(picked) == 16 and {subsets.route_of(s) for s in picked} == routes
    assert len(subsets.stratified(SAMPLES[:3], 10, rng)) == 3  # all of a smaller pool


# --------------------------------------------------------------------------- CLI


def invoke(*args: str) -> Any:
    return CliRunner().invoke(cli.app, list(args))


def test_cli_overlap_and_relabel_set(tmp_path: Path) -> None:
    sample = tmp_path / "label_sample_v1.jsonl"
    labels = tmp_path / "labels_v1.jsonl"
    shutil.copy(DATASETS / "label_sample_v1.jsonl", sample)
    shutil.copy(DATASETS / "labels_v1.jsonl", labels)
    common = ["--sample", str(sample), "--labels", str(labels)]
    over = tmp_path / "overlap_v1.jsonl"
    res = invoke("label", "overlap", *common, "--out", str(over))
    assert res.exit_code == 0, res.output
    assert "25 with a fail" in res.output
    assert list(read_jsonl(over, LabelSample)) == list(
        read_jsonl(DATASETS / subsets.OVERLAP_FILE, LabelSample)
    )
    rel = tmp_path / "relabel_v1.jsonl"
    res = invoke("label", "relabel-set", *common, "--overlap", str(over), "--out", str(rel))
    assert res.exit_code == 0, res.output
    entry = read_manifest(tmp_path / "MANIFEST.json").datasets["relabel_v1.jsonl"]
    assert entry.metadata["excluded_overlap"] == 50 and entry.kind == "label_samples"
    # with no overlap file, the relabel set may include anything; a top-up is reported
    res = invoke(
        "label", "relabel-set", *common, "--overlap", str(tmp_path / "none.jsonl"),
        "--out", str(rel), "--enriched", "30", "--size", "30",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    res = invoke(
        "label", "relabel-set", *common, "--overlap", str(over),
        "--out", str(rel), "--enriched", "40", "--size", "40",
    )  # fmt: skip
    assert res.exit_code == 0 and "taken from the overlap set" in res.output


def test_cli_subset_errors(tmp_path: Path) -> None:
    res = invoke("label", "overlap", "--labels", str(tmp_path / "missing.jsonl"))
    assert res.exit_code == 2 and "cannot read the labels" in res.output
    res = invoke(
        "label", "overlap", "--size", "10", "--enriched", "20", "--out", str(tmp_path / "o.jsonl")
    )
    assert res.exit_code == 2 and "cannot select the overlap set" in res.output
    res = invoke("label", "relabel-set", "--size", "500", "--out", str(tmp_path / "r.jsonl"))
    assert res.exit_code == 2 and "cannot select the relabel set" in res.output
