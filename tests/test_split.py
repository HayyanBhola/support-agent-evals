"""The dev/test split (Day 13): sizes, stratification, rare-fail coverage, no example leak."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalkit import cli
from evalkit.calibration import split as sp
from evalkit.hashing import file_sha256
from evalkit.io import read_jsonl
from evalkit.manifest import DatasetSpec, read_manifest, upsert_entry
from evalkit.schemas import Criterion, Label, LabelSample, LabelValue

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "datasets"
SPLIT = DATASETS / "splits" / "split_v1.json"
SAMPLES = {s.case_id: s for s in read_jsonl(DATASETS / "label_sample_v1.jsonl", LabelSample)}
LABELS = {lab.case_id: lab for lab in read_jsonl(DATASETS / "labels_v1.1.jsonl", Label)}
EXAMPLES = json.loads((ROOT / "rubrics" / "examples_used.json").read_text("utf-8"))["case_ids"]
T0 = datetime(2026, 10, 14, tzinfo=UTC)


def test_real_split_sizes_and_disjointness() -> None:
    s = sp.make_split(SAMPLES, LABELS, exclude=EXAMPLES)
    assert (len(s.dev), len(s.test)) == (96, 64)
    assert not set(s.dev) & set(s.test)
    assert set(s.dev) | set(s.test) == set(LABELS)
    assert s.dev == sorted(s.dev) and s.test == sorted(s.test)


def test_split_is_deterministic_and_seed_dependent() -> None:
    a = sp.make_split(SAMPLES, LABELS, seed=42)
    assert a == sp.make_split(SAMPLES, LABELS, seed=42)
    assert a.test != sp.make_split(SAMPLES, LABELS, seed=7).test


def test_strata_stay_near_their_share() -> None:
    s = sp.make_split(SAMPLES, LABELS)
    sizes = sp.stratum_counts(LABELS, SAMPLES, LABELS)
    test = sp.stratum_counts(s.test, SAMPLES, LABELS)
    for key, n in sizes.items():
        assert abs(test.get(key, 0) - 0.4 * n) < 2, key  # within one case of 40%, rare aside


def test_every_rare_fail_criterion_has_a_fail_in_both_splits() -> None:
    s = sp.make_split(SAMPLES, LABELS)
    cov = {c.criterion: c for c in sp.coverage(s, LABELS)}
    for c in (Criterion.SAFETY, Criterion.CORRECTNESS, Criterion.COMPLETENESS, Criterion.TONE):
        assert cov[c].rare and cov[c].dev >= 1 and cov[c].test >= 1, c
        assert cov[c].note() == "in both"
    assert cov[Criterion.FAITHFULNESS].note() == "no fails" and not cov[Criterion.FAITHFULNESS].rare
    assert not cov[Criterion.RELEVANCE].rare  # 30 fails: not rare


def test_no_rubric_example_is_in_either_split() -> None:
    doc = sp.read_split(SPLIT)
    assert not set(EXAMPLES) & (set(doc.ids("dev")) | set(doc.ids("test")))
    with pytest.raises(ValueError, match="rubric example"):
        sp.make_split(SAMPLES, LABELS, exclude=[next(iter(LABELS))])


def test_committed_split_matches_a_fresh_one() -> None:
    doc = sp.read_split(SPLIT)
    fresh = sp.make_split(SAMPLES, LABELS, exclude=EXAMPLES, seed=doc.seed)
    assert (doc.ids("dev"), doc.ids("test")) == (fresh.dev, fresh.test)
    assert doc.labels_sha256 == file_sha256(DATASETS / doc.labels_file)
    assert doc.labels_version == "1.1"
    entry = read_manifest(DATASETS / "MANIFEST.json").datasets["splits/split_v1.json"]
    assert entry.kind == "splits" and entry.records == 160
    assert entry.metadata["dev"] == 96 and entry.metadata["test"] == 64


def test_agreement_metadata() -> None:
    raw = json.loads(SPLIT.read_text(encoding="utf-8"))
    meta = raw["agreement_metadata"]
    overlap = {s.case_id for s in read_jsonl(DATASETS / "overlap_v1.jsonl", LabelSample)}
    assert set(meta["overlap"]["dev"]) | set(meta["overlap"]["test"]) == overlap
    assert set(meta["overlap"]["test"]) <= set(raw["splits"]["test"])
    round1, current = meta["disagreements_round1"]["cases"], meta["disagreements_current"]["cases"]
    assert sum(map(len, round1.values())) == 51  # the official round-1 report
    assert sum(map(len, current.values())) == 45  # the post-hoc v2 report
    assert set(current) <= overlap and "tone" in round1["legacy-kb-fragrance"]


# --------------------------------------------------------------------------- synthetic


def label(cid: str, **verdicts: str) -> Label:
    data: dict[str, Any] = {
        "case_id": cid, "run_id": "r", "labeler": "a", "timestamp": T0,
        "rubric_hash": "a" * 64, "response_sha256": "b" * 64,
        "verdicts": {c.value: "pass" for c in Criterion} | verdicts, "seconds_spent": 1.0,
    }  # fmt: skip
    return Label.model_validate(data)


def test_single_fail_goes_to_test_and_missing_cases_are_refused() -> None:
    ids = sorted(LABELS)[:20]
    labels = {cid: label(cid) for cid in ids}
    labels[ids[3]] = label(ids[3], safety="fail")
    s = sp.make_split({c: SAMPLES[c] for c in ids}, labels)
    assert ids[3] in s.test
    cov = {c.criterion: c for c in sp.coverage(s, labels)}[Criterion.SAFETY]
    assert cov.note() == "only 1 fail: test only"
    assert sp.Coverage(Criterion.TONE, 2, 2, 0, True).note() == "not in both"
    with pytest.raises(ValueError, match="not in the sample"):
        sp.make_split({}, labels)


def test_bounded_shares() -> None:
    sizes = {"a": 3, "b": 5, "c": 2}
    assert sp.bounded_shares(4, sizes, {}, sizes) == {"a": 1, "b": 2, "c": 1}
    assert sp.bounded_shares(4, sizes, {"a": 3}, sizes) == {"a": 3, "b": 1, "c": 0}
    assert sp.bounded_shares(4, sizes, {}, {"a": 0, "b": 5, "c": 2}) == {"a": 0, "b": 3, "c": 1}
    assert sp.bounded_shares(20, sizes, {}, sizes) == sizes  # more than there is: all


def test_disagreements() -> None:
    a = {"x": label("x", tone="fail"), "y": label("y"), "only-a": label("only-a")}
    b = {"x": label("x"), "y": label("y")}
    assert sp.disagreements(a, b) == {"x": ["tone"]}
    assert sp.verdict_table(["x"], a)["tone"] == {"pass": 0, "fail": 1, "n_a": 0}
    assert LabelValue.FAIL.value == "fail"


# --------------------------------------------------------------------------- CLI


def test_cli_split_make(tmp_path: Path) -> None:
    for name in ("label_sample_v1.jsonl", "labels_v1.1.jsonl", "labels_v1.jsonl",
                 "overlap_v1.jsonl"):  # fmt: skip
        shutil.copy(DATASETS / name, tmp_path / name)
    upsert_entry(tmp_path, "labels_v1.1.jsonl", DatasetSpec(version="1.1", kind="labels"))
    out = tmp_path / "splits" / "split_v1.json"
    args = [
        "split", "make", "--labels", str(tmp_path / "labels_v1.1.jsonl"),
        "--sample", str(tmp_path / "label_sample_v1.jsonl"),
        "--round1", str(tmp_path / "labels_v1.jsonl"),
        "--overlap", str(tmp_path / "overlap_v1.jsonl"), "--out", str(out),
    ]  # fmt: skip
    res = CliRunner().invoke(cli.app, args)
    assert res.exit_code == 0, res.output
    assert "dev 96 / test 64" in res.output and "safety: 2 fail(s), dev 1, test 1" in res.output
    fresh, committed = (json.loads(f.read_text("utf-8")) for f in (out, SPLIT))
    rubric_dependent = ("rubric_hash", "label_states")  # the rubric at the time of the run
    assert {k: v for k, v in fresh.items() if k not in rubric_dependent} == {
        k: v for k, v in committed.items() if k not in rubric_dependent
    }
    assert "splits/split_v1.json" in read_manifest(tmp_path / "MANIFEST.json").datasets
    elsewhere = tmp_path.parent / f"{tmp_path.name}-split.json"
    res = CliRunner().invoke(cli.app, [*args[:-1], str(elsewhere)])
    assert res.exit_code == 0 and "MANIFEST" not in res.output.splitlines()[-1]
    elsewhere.unlink()
    bad = CliRunner().invoke(cli.app, [*args[:3], str(tmp_path / "missing.jsonl"), *args[4:]])
    assert bad.exit_code == 2 and "cannot make the split" in bad.output
