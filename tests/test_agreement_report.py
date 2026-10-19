"""The agreement report (Day 12): header warnings, enrichment line, disagreements, CLI."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalkit import cli
from evalkit.calibration import report
from evalkit.io import read_jsonl, write_jsonl
from evalkit.labeling.store import LabelStore
from evalkit.schemas import Criterion, Label, LabelSample, LabelValue

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "datasets"
OVERLAP = list(read_jsonl(DATASETS / "overlap_v1.jsonl", LabelSample))
T0 = datetime(2026, 10, 14, tzinfo=UTC)
H1, H2 = "a" * 64, "c" * 64


def label(s: LabelSample, who: str, when: datetime = T0, **over: Any) -> Label:
    verdicts = {c.value: "pass" for c in Criterion} | over.pop("verdicts", {})
    data: dict[str, Any] = {
        "case_id": s.case_id,
        "run_id": s.run_id,
        "labeler": who,
        "timestamp": when,
        "rubric_hash": H1,
        "response_sha256": s.response_sha256,
        "verdicts": verdicts,
        "seconds_spent": 10.0,
    }
    return Label.model_validate(data | over)


def two_labelers(b_name: str = "bob", b_when: datetime = T0 + timedelta(days=1)) -> list[Label]:
    cases = OVERLAP[:12]
    curt = [label(s, "hayyan", verdicts={"tone": "fail"}, notes="curt") for s in cases[:3]]
    a = curt + [label(s, "hayyan") for s in cases[3:]]
    b = [label(s, b_name, b_when) for s in cases]
    b[0] = label(cases[0], b_name, b_when, verdicts={"tone": "fail"}, notes="too | short\nreply")
    b[5] = label(cases[5], b_name, b_when, verdicts={"faithfulness": "n_a"})
    return a + b


def build(labels: list[Label], a: str = "hayyan", b: str = "bob", **kw: Any) -> str:
    by: dict[str, dict[str, Label]] = {}
    for lab in labels:
        by.setdefault(lab.labeler, {})[lab.case_id] = lab
    text, _ = report.build_report(a, b, by, datasets_dir=DATASETS, resamples=100, **kw)
    return text


def test_report_header_table_and_disagreements() -> None:
    text = build(two_labelers())
    assert "# Human agreement: hayyan vs bob" in text
    assert "**Matched cases:** 12" in text
    assert "`overlap_v1.jsonl` (50 records), **enriched for fails**: 25 of 50" in text
    assert "not population fail rates" in text
    assert report.AC1_NOTE in text
    assert "every matched case was labeled on the same response" in text
    assert "different rubric versions" not in text
    # tone: A fails 3, B fails 1 (together on case 0) -> 2 disagreements; faithfulness: 1
    assert f"| {OVERLAP[1].case_id} | tone | fail | pass | curt | – |" in text
    assert f"| {OVERLAP[0].case_id} | tone |" not in text
    assert f"| {OVERLAP[5].case_id} | faithfulness | pass | n_a | – | – |" in text
    assert "too \\| short reply" not in text  # case 0 agrees, so its notes aren't listed
    assert "Flagged:" in text and "too few fails to judge" in text
    assert "### tone" in text


def test_report_warns_about_rubric_versions_and_changed_responses() -> None:
    labels = two_labelers()
    labels[-1] = labels[-1].model_copy(update={"rubric_hash": H2, "response_sha256": "d" * 64})
    fail_safety = labels[-2].verdicts | {Criterion.SAFETY: LabelValue.FAIL}
    labels[-2] = labels[-2].model_copy(update={"notes": "a | b", "verdicts": fail_safety})
    text = build(labels)
    assert "⚠ **The labelers used different rubric versions.**" in text
    assert f"`{H1[:12]}`, `{H2[:12]}`" in text
    assert "1 case(s) were labeled on different responses" in text
    assert "| safety | pass | fail | – | a \\| b |" in text


def test_self_relabel_gap_warning() -> None:
    soon = build(two_labelers("hayyan-r2", T0 + timedelta(days=1)), b="hayyan-r2")
    assert "Self-relabel less than 3 days" in soon
    later = build(two_labelers("hayyan-r2", T0 + timedelta(days=4)), b="hayyan-r2")
    assert "Self-relabel" not in later and "started 4.0 days after" in later
    other = build(two_labelers("bob", T0 + timedelta(days=1)))
    assert "Self-relabel" not in other


def test_no_disagreements() -> None:
    cases = OVERLAP[:3]
    labels = [label(s, "hayyan") for s in cases] + [label(s, "bob") for s in cases]
    assert "No disagreements." in build(labels)


def test_errors() -> None:
    with pytest.raises(ValueError, match="no labels by 'bob'"):
        build([label(OVERLAP[0], "hayyan")])
    with pytest.raises(ValueError, match="no case in common"):
        build([label(OVERLAP[0], "hayyan"), label(OVERLAP[1], "bob")])


def test_latest_labels_by_timestamp_across_files(tmp_path: Path) -> None:
    s = OVERLAP[0]
    old = label(s, "bob", T0, notes="old")
    new = label(s, "bob", T0 + timedelta(hours=1), notes="new")
    write_jsonl(tmp_path / "labels_raw_1.jsonl", [new])
    write_jsonl(tmp_path / "labels_raw_2.jsonl", [old])  # later file, earlier label
    latest = report.latest_labels(sorted(tmp_path.glob(report.RAW_GLOB)))
    assert latest["bob"][s.case_id].notes == "new"


def test_identify_set(tmp_path: Path) -> None:
    overlap_ids = {s.case_id for s in OVERLAP[:5]}
    found = report.identify_set(overlap_ids, DATASETS)
    assert found == report.SetInfo("overlap_v1.jsonl", 50, 25)
    whole = report.identify_set({"no-such-case"}, DATASETS)
    assert whole is None
    given = report.identify_set(overlap_ids, DATASETS, DATASETS / "label_sample_v1.jsonl")
    assert given is not None and given.name == "label_sample_v1.jsonl" and not given.enriched
    assert report.identify_set(overlap_ids, tmp_path) is None
    text = build(two_labelers(), sample=DATASETS / "label_sample_v1.jsonl")
    assert "(160 records), not enriched." in text


def test_unidentified_set_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report, "identify_set", lambda *a, **k: None)
    assert "**Set:** not identified" in build(two_labelers())


# --------------------------------------------------------------------------- CLI


def test_cli_agreement_report(tmp_path: Path) -> None:
    labels_dir = tmp_path / "labels"
    store = LabelStore(labels_dir / "labels_raw_overlap_v1.jsonl")
    for lab in two_labelers():
        store.append(lab)
    out = tmp_path / "reports" / "human_agreement.md"
    args = ["agreement", "report", "--a", "hayyan", "--b", "bob", "--labels-dir", str(labels_dir)]
    res = CliRunner().invoke(
        cli.app, [*args, "--out", str(out), "--resamples", "50",
                  "--sample", str(DATASETS / "overlap_v1.jsonl")],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert "tone: n=12, kappa" in res.output and "too few fails" in res.output
    text = out.read_text(encoding="utf-8")
    assert "enriched for fails" in text and "labels_raw_overlap_v1.jsonl" in text
    missing = CliRunner().invoke(cli.app, [*args[:4], "--b", "nobody", "--labels-dir",
                                           str(labels_dir), "--out", str(out)])  # fmt: skip
    assert missing.exit_code == 2 and "no labels by 'nobody'" in missing.output


def test_title_and_note() -> None:
    text = build(two_labelers(), title="Post-hoc agreement", note="Not the official result.")
    lines = text.splitlines()
    assert lines[:3] == ["# Post-hoc agreement: hayyan vs bob", "", "> Not the official result."]
