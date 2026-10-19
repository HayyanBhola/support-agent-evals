"""Leakage guards (Day 13): examples only from dev or the rubric, and `evalkit split check`."""

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from evalkit import cli
from evalkit.calibration import leakage
from evalkit.calibration.split import SplitFile, read_split
from evalkit.io import read_jsonl
from evalkit.schemas import LabelSample

ROOT = Path(__file__).resolve().parent.parent
SPLIT = read_split(ROOT / "datasets" / "splits" / "split_v1.json")
EXAMPLES = json.loads((ROOT / "rubrics" / "examples_used.json").read_text("utf-8"))["case_ids"]
SAMPLES = {s.case_id: s for s in read_jsonl(ROOT / "datasets" / "label_sample_v1.jsonl",
                                             LabelSample)}  # fmt: skip
TEST = set(SPLIT.ids("test"))


@settings(max_examples=200, deadline=None)
@given(
    k=st.integers(min_value=0, max_value=200),
    seed=st.integers(min_value=0, max_value=10**6),
    source=st.sampled_from(["dev", "rubric", "both"]),
)
def test_few_shot_examples_never_return_a_test_case(k: int, seed: int, source: Any) -> None:
    picked = leakage.few_shot_examples(k, SPLIT, EXAMPLES, seed=seed, source=source)
    assert not set(picked) & TEST
    assert len(picked) == min(k, len(leakage.example_pool(SPLIT, EXAMPLES, source)))


def test_pool_is_dev_plus_rubric_examples() -> None:
    assert set(leakage.example_pool(SPLIT, EXAMPLES, "dev")) == set(SPLIT.ids("dev"))
    assert set(leakage.example_pool(SPLIT, EXAMPLES, "rubric")) == set(EXAMPLES)
    both = leakage.example_pool(SPLIT, EXAMPLES)
    assert set(both) == set(SPLIT.ids("dev")) | set(EXAMPLES) and not set(both) & TEST
    # restricting to test cases (e.g. "cases with a fail") still yields none of them
    assert leakage.few_shot_examples(10, SPLIT, EXAMPLES, restrict_to=TEST) == []
    dev_fail = SPLIT.ids("dev")[:5]
    assert set(leakage.few_shot_examples(10, SPLIT, EXAMPLES, restrict_to=dev_fail)) == set(
        dev_fail
    )


def test_a_test_id_in_the_pool_raises() -> None:
    a_test_case = SPLIT.ids("test")[0]
    with pytest.raises(leakage.LeakageError, match=a_test_case):
        leakage.example_pool(SPLIT, [a_test_case])
    bad = SplitFile.model_validate(SPLIT.model_dump() | {"splits": {
        "dev": [a_test_case], "test": [a_test_case]}})  # fmt: skip
    with pytest.raises(leakage.LeakageError):
        leakage.few_shot_examples(1, bad, [], source="dev")


# --------------------------------------------------------------------------- scan

MARKERS = leakage.markers(SPLIT, SAMPLES, ROOT / "rubrics")


def unique_test_reply() -> tuple[str, str]:
    m = next(m for m in MARKERS if m.kind == "reply")
    return m.case_id, SAMPLES[m.case_id].response.reply


def test_markers_skip_short_and_shared_texts() -> None:
    texts = [m for m in MARKERS if m.kind != "case_id"]
    assert texts and all(len(m.text) >= leakage.MIN_TEXT_CHARS for m in texts)
    not_found = (
        "I'm sorry, I don't have that information. A GlowCare team member can help you further."
    )
    assert leakage.norm(not_found) not in {m.text for m in texts}  # a shared fixed reply
    assert {m.case_id for m in MARKERS if m.kind == "case_id"} == TEST


def test_scan_finds_ids_replies_and_questions(tmp_path: Path) -> None:
    cid, reply = unique_test_reply()
    q = next(m for m in MARKERS if m.kind == "question")
    (tmp_path / "judge.md").write_text(f"Example:\nsee {cid} here\n", encoding="utf-8")
    (tmp_path / "few_shot.md").write_text(
        "Reply:\n" + reply.upper().replace(" ", "\n  "), encoding="utf-8"
    )  # re-wrapped and upper-cased: still found
    (tmp_path / "q.txt").write_text(SAMPLES[q.case_id].case.question, encoding="utf-8")
    (tmp_path / "clean.md").write_text(f"{cid}-extra and x{cid} are other ids", encoding="utf-8")
    hits = leakage.scan(leakage.files_to_scan([tmp_path]), MARKERS)
    found = {(h.path.name, h.marker.kind) for h in hits}
    assert found == {("judge.md", "case_id"), ("few_shot.md", "reply"), ("q.txt", "question")}
    assert str(next(h for h in hits if h.path.name == "judge.md")).endswith(
        f"judge.md:2: test case_id of {cid}"
    )
    assert "few_shot.md: test reply" in str(next(h for h in hits if h.marker.kind == "reply"))


def test_files_to_scan(tmp_path: Path) -> None:
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "img.png").write_bytes(b"\0")
    files = leakage.files_to_scan([tmp_path, tmp_path / "a.md", tmp_path / "missing"])
    assert [p.name for p in files] == ["a.md", "a.md"]  # the folder's, then the file given


def test_cli_split_check(tmp_path: Path) -> None:
    res = CliRunner().invoke(cli.app, ["split", "check"])
    assert res.exit_code == 0, res.output
    assert "no test data found" in res.output  # the real prompts/ and judges/ today
    cid, _ = unique_test_reply()
    (tmp_path / "prompt.md").write_text(f"use {cid} as an example", encoding="utf-8")
    res = CliRunner().invoke(cli.app, ["split", "check", "--path", str(tmp_path)])
    assert res.exit_code == 1 and "1 leak(s)" in res.output and cid in res.output
    res = CliRunner().invoke(cli.app, ["split", "check", "--split", str(tmp_path / "x.json")])
    assert res.exit_code == 2 and "cannot check the split" in res.output
