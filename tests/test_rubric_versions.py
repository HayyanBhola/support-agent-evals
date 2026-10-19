"""rubrics/versions.json (Day 12): carried-forward labels in status, validate, export, app."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalkit import cli
from evalkit.io import read_jsonl, write_jsonl
from evalkit.labeling import workflow
from evalkit.labeling.store import LabelStore
from evalkit.manifest import read_manifest
from evalkit.rubrics import (
    LabelState,
    RubricVersions,
    load_rubrics,
    load_versions,
    rubric_hash,
)
from evalkit.schemas import Criterion, Label, LabelSample, LabelValue

ROOT = Path(__file__).resolve().parent.parent
RUBRICS = ROOT / "rubrics"
V1, V2, V3 = "1" * 64, "2" * 64, "3" * 64
T0 = datetime(2026, 10, 14, tzinfo=UTC)


def versions(recheck2: list[str], recheck3: list[str] | None = None) -> RubricVersions:
    data: list[dict[str, Any]] = [
        {"version": 1, "day": 8, "rubric_hash": V1},
        {"version": 2, "day": 12, "rubric_hash": V2, "carry_forward": {
            "from_hash": V1, "changed_criteria": ["tone"], "recheck": recheck2,
            "note": "tone lost a rule"}},
    ]  # fmt: skip
    if recheck3 is not None:
        data.append({"version": 3, "day": 20, "rubric_hash": V3, "carry_forward": {
            "from_hash": V2, "changed_criteria": ["safety"], "recheck": recheck3,
            "note": "safety"}})  # fmt: skip
    return RubricVersions.model_validate({"versions": data})


def test_label_state_walks_the_carry_forward_chain() -> None:
    v = versions(["a"], ["b"])
    assert v.label_state(V3, "x", V3) is LabelState.CURRENT
    assert v.label_state(V2, "x", V3) is LabelState.CARRIED
    assert v.label_state(V1, "x", V3) is LabelState.CARRIED
    assert v.label_state(V2, "b", V3) is LabelState.RECHECK
    assert v.label_state(V1, "a", V3) is LabelState.RECHECK  # v2's list still applies
    assert v.label_state(V2, "a", V3) is LabelState.CARRIED  # a was re-checked under v2
    assert v.label_state("9" * 64, "x", V3) is LabelState.STALE
    assert v.label_state(V1, "x", "9" * 64) is LabelState.STALE  # current not recorded
    no_link = RubricVersions.model_validate(
        {"versions": [{"version": 1, "day": 8, "rubric_hash": V1},
                      {"version": 2, "day": 9, "rubric_hash": V2}]}
    )  # fmt: skip
    assert no_link.label_state(V1, "x", V2) is LabelState.STALE


def test_carried_note() -> None:
    v = versions(["a", "b"])
    note = v.carried_note(V2)
    assert note is not None and "v2 carries forward" in note and "tone" in note
    assert "2 case(s) to re-check" in note
    assert v.carried_note(V1) is None and v.carried_note("9" * 64) is None


def test_real_versions_file_records_the_current_rubric() -> None:
    v = load_versions(RUBRICS)
    assert v is not None
    current = v.get(rubric_hash(RUBRICS))
    assert current is not None and current.version == 4
    # walk the carry-forward chain: v4 relevance -> v3 correctness -> v2 tone -> v1
    expected = [(4, [Criterion.RELEVANCE], 0), (3, [Criterion.CORRECTNESS], 0),
                (2, [Criterion.TONE], 8)]  # fmt: skip
    version = current
    for number, changed, rechecks in expected:
        assert version is not None and version.version == number
        assert version.carry_forward is not None
        assert version.carry_forward.changed_criteria == changed
        assert len(version.carry_forward.recheck) == rechecks
        version = v.get(version.carry_forward.from_hash)
    assert version is not None and version.version == 1 and version.carry_forward is None
    # a file's version counts its own changes (the rubric version counts the whole set)
    by = {r.criterion: r.meta.version for r in load_rubrics(RUBRICS, None)}
    assert by[Criterion.CORRECTNESS] == 3 and by[Criterion.RELEVANCE] == 3
    assert {c for c, n in by.items() if n == 2} == set(Criterion) - {
        Criterion.CORRECTNESS, Criterion.RELEVANCE}  # fmt: skip


def test_rubric_check_reports_the_versions_record(tmp_path: Path) -> None:
    d = tmp_path / "rubrics"
    shutil.copytree(RUBRICS, d)
    args = ["rubric", "check", "--dir", str(d)]
    assert "recorded in versions.json as v4" in CliRunner().invoke(cli.app, args).output
    (d / "versions.json").unlink()
    assert "not in versions.json" in CliRunner().invoke(cli.app, args).output
    (d / "versions.json").write_text('{"versions": "nope"}', encoding="utf-8")
    res = CliRunner().invoke(cli.app, args)
    assert res.exit_code == 2 and "versions.json is invalid" in res.output
    assert load_versions(tmp_path) is None


# --------------------------------------------------------------------------- workflow


def labels_for(samples: list[LabelSample], hashes: list[str]) -> list[Label]:
    return [
        Label(
            case_id=s.case_id, run_id=s.run_id, labeler="ann", timestamp=T0, rubric_hash=h,
            response_sha256=s.response_sha256, verdicts=dict.fromkeys(Criterion, LabelValue.PASS),
            seconds_spent=5.0,
        )
        for s, h in zip(samples, hashes, strict=True)
    ]  # fmt: skip


SAMPLES = list(read_jsonl(ROOT / "datasets" / "label_sample_v1.jsonl", LabelSample))[:4]


def test_status_validate_export_with_carried_labels(tmp_path: Path) -> None:
    v = versions([SAMPLES[1].case_id])
    labels = labels_for(SAMPLES, [V2, V1, V1, "9" * 64])  # current, recheck, carried, stale
    st = workflow.status(SAMPLES, {lab.case_id: lab for lab in labels}, V2, v)
    assert st.carried == [SAMPLES[2].case_id]
    assert sorted(st.stale) == sorted([SAMPLES[1].case_id, SAMPLES[3].case_id])
    assert workflow.status(SAMPLES, {lab.case_id: lab for lab in labels}, V2).carried == []

    rubrics = load_rubrics(RUBRICS, None)
    result = workflow.validate(SAMPLES, labels, rubrics, current_hash=V2, versions=v)
    hash_warnings = [w for w in result.warnings if "rubric" in w and "says" not in w]
    assert len(hash_warnings) == 2
    assert any("re-check list" in w and SAMPLES[1].case_id in w for w in hash_warnings)
    assert any("older rubric_hash 999999999999" in w for w in hash_warnings)
    assert len(result.notes) == 1 and "1 label(s) carry an older rubric_hash" in result.notes[0]
    assert workflow.validate(SAMPLES, labels[:1], rubrics, current_hash=V2, versions=v).notes == []
    assert workflow.validate(SAMPLES, labels, rubrics).notes == []  # no hash given: as before

    store = LabelStore(tmp_path / "raw.jsonl")
    for lab in labels:
        store.append(lab)
    write_jsonl(tmp_path / workflow.SAMPLE_FILE, SAMPLES)  # the export's source
    out = tmp_path / "labels_v1.1.jsonl"
    workflow.export_labels(store, out, version="1.1", changelog="re-checked", current_hash=V2,
                           versions=v)  # fmt: skip
    meta = read_manifest(tmp_path / "MANIFEST.json").datasets[out.name]
    assert meta.version == "1.1"
    assert meta.metadata["changelog"] == "re-checked"
    assert meta.metadata["label_states"] == {"current": 1, "carried": 1, "recheck": 1, "stale": 1}
    assert "tone lost a rule" in meta.metadata["carried_forward"]


def test_cli_validate_status_export_print_the_carry_forward(tmp_path: Path) -> None:
    current = rubric_hash(RUBRICS)
    v1 = load_versions(RUBRICS)
    assert v1 is not None
    old = v1.versions[0].rubric_hash
    sample = tmp_path / "sample.jsonl"
    write_jsonl(sample, SAMPLES)
    raw = tmp_path / "labels" / "raw.jsonl"
    store = LabelStore(raw)
    for lab in labels_for(SAMPLES, [current, old, old, old]):
        store.append(lab)
    common = ["--sample", str(sample), "--labels", str(raw)]
    res = CliRunner().invoke(cli.app, ["labels", "validate", *common])
    assert res.exit_code == 0 and "note" in res.output and "3 label(s) carry" in res.output
    res = CliRunner().invoke(cli.app, ["labels", "status", *common])
    assert "carried forward from an older rubric_hash (still valid): 3" in res.output
    out = tmp_path / "labels_v1.1.jsonl"
    res = CliRunner().invoke(
        cli.app, ["labels", "export", *common, "--out", str(out), "--version", "1.1",
                  "--changelog", "x"],
    )  # fmt: skip
    assert res.exit_code == 0 and "carried: 3, current: 1" in res.output
    assert json.loads(out.read_text(encoding="utf-8").splitlines()[0])["labeler"] == "ann"


# --------------------------------------------------------------------------- app

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402


def test_app_recheck_filter_skips_carried_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = load_versions(RUBRICS)
    assert v is not None
    old = v.versions[0].rubric_hash
    recheck = v.versions[1].carry_forward
    assert recheck is not None
    every = list(read_jsonl(ROOT / "datasets" / "label_sample_v1.jsonl", LabelSample))
    on_list = next(s for s in every if s.case_id in recheck.recheck)
    other = next(s for s in every if s.case_id not in recheck.recheck)
    sample = tmp_path / "sample.jsonl"
    write_jsonl(sample, [other, on_list])
    raw = tmp_path / "raw.jsonl"
    store = LabelStore(raw)
    for lab in labels_for([other, on_list], [old, old]):
        store.append(lab)
    for key, value in {
        "EVALKIT_LABELER": "ann",
        "EVALKIT_LABEL_SAMPLE": str(sample),
        "EVALKIT_LABELS": str(raw),
        "EVALKIT_RUBRICS": str(RUBRICS),
    }.items():
        monkeypatch.setenv(key, value)  # fmt: skip
    at = AppTest.from_file(str(ROOT / "app" / "labeler.py"), default_timeout=60).run()
    at.radio(key="filter").set_value("to re-check under the current rubric").run()
    assert on_list.case_id in at.subheader[0].value
    assert "(re-check under the current rubric)" in at.subheader[0].value
    at.radio(key="filter").set_value("labeled").run()
    at.button(key="back").click().run()
    assert other.case_id in at.subheader[0].value and "re-check" not in at.subheader[0].value
