"""Streamlit AppTest smoke test: the labeling app loads a record and saves a label."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from evalkit.io import read_jsonl, write_jsonl
from evalkit.labeling.applicability import applicability
from evalkit.labeling.store import LabelStore, raw_file_for
from evalkit.labeling.view import escalation_facts
from evalkit.rubrics import load_rubrics, rubric_hash
from evalkit.schemas import Criterion, Label, LabelSample, LabelValue

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app" / "labeler.py"
RUBRICS = ROOT / "rubrics"
T0 = datetime(2026, 10, 14, tzinfo=UTC)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[LabelSample], Path]:
    samples = list(read_jsonl(ROOT / "datasets" / "label_sample_v1.jsonl", LabelSample))[:2]
    sample_path = tmp_path / "sample.jsonl"
    write_jsonl(sample_path, samples)
    labels = tmp_path / "labels" / "raw.jsonl"
    monkeypatch.setenv("EVALKIT_LABELER", "tester")
    monkeypatch.setenv("EVALKIT_LABEL_SAMPLE", str(sample_path))
    monkeypatch.setenv("EVALKIT_LABELS", str(labels))
    monkeypatch.setenv("EVALKIT_RUBRICS", str(RUBRICS))
    return samples, labels


def fill(at: AppTest, sample: LabelSample, verdict: str) -> None:
    judged = applicability(load_rubrics(RUBRICS, None), sample.case, sample.response)
    for criterion, applies in judged.items():
        if applies:
            at.radio(key=f"{sample.case_id}:{criterion.value}").set_value(verdict)


def test_app_loads_a_record_and_saves_a_label(env: tuple[list[LabelSample], Path]) -> None:
    samples, labels = env
    first = samples[0]
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    assert first.case_id in at.subheader[0].value
    assert any(first.case.question in t.value for t in at.text)

    # a fail without a note is refused
    fill(at, first, "fail")
    at.button(key="save").click().run()
    assert any("Add a note" in e.value for e in at.error)
    assert not labels.exists()

    at.text_area(key=f"{first.case_id}:notes").set_value("smoke test").run()
    at.button(key="save").click().run()
    assert not at.exception
    saved = LabelStore(labels).all()
    assert len(saved) == 1
    label = saved[0]
    assert (label.case_id, label.labeler, label.notes) == (first.case_id, "tester", "smoke test")
    assert label.rubric_hash == rubric_hash(RUBRICS)
    assert label.response_sha256 == first.response_sha256
    judged = applicability(load_rubrics(RUBRICS, None), first.case, first.response)
    for criterion in Criterion:
        expected = LabelValue.FAIL if judged[criterion] else LabelValue.N_A
        assert label.verdicts[criterion] is expected
    assert label.seconds_spent >= 0
    # after saving, the app moves on to the next unlabeled record
    assert samples[1].case_id in at.subheader[0].value


def test_app_requires_a_labeler(
    env: tuple[list[LabelSample], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EVALKIT_LABELER")
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert any("labeler name" in e.value for e in at.error)


def test_jump_strips_spaces_and_rejects_unknown_case_ids(
    env: tuple[list[LabelSample], Path],
) -> None:
    samples, _ = env
    first, second = samples[0].case_id, samples[1].case_id
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert first in at.subheader[0].value

    at.text_input(key="jump").set_value(f"  {second} ").run()
    at.button(key="go").click().run()
    assert not at.error
    assert second in at.subheader[0].value

    at.text_input(key="jump").set_value("no-such-case").run()
    at.button(key="go").click().run()
    assert any(
        "'no-such-case' is not in the sample" in e.value and second in e.value for e in at.error
    )
    assert second in at.subheader[0].value

    at.text_input(key="jump").set_value("   ").run()
    at.button(key="go").click().run()
    assert any("Type a case_id" in e.value for e in at.error)


def test_save_shows_the_case_id_all_verdicts_and_the_note(
    env: tuple[list[LabelSample], Path],
) -> None:
    samples, labels = env
    first = samples[0]
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert any(f"Saving to: {first.case_id}" in h.value for h in at.header)
    assert not at.success

    fill(at, first, "pass")
    at.text_area(key=f"{first.case_id}:notes").set_value("checked twice").run()
    at.button(key="save").click().run()
    assert not at.exception
    label = LabelStore(labels).all()[0]
    [confirm] = at.success
    assert confirm.value.startswith(f"Saved {first.case_id}:")
    for criterion in Criterion:
        assert f"{criterion.value}={label.verdicts[criterion].value}" in confirm.value
    assert "Note: checked twice" in confirm.value
    # the app has moved on, and the header above Save names the new record
    assert any(f"Saving to: {samples[1].case_id}" in h.value for h in at.header)


def test_app_shows_only_the_current_labelers_own_labels(
    env: tuple[list[LabelSample], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    samples, labels = env
    store = LabelStore(labels)
    secret = "SECRET-NOTE-FROM-HAYYAN"
    for s in samples:  # the worst case: hayyan's labels in the very file hayyan-r2 uses
        store.append(
            Label(
                case_id=s.case_id, run_id=s.run_id, labeler="hayyan", timestamp=T0,
                rubric_hash=rubric_hash(RUBRICS), response_sha256=s.response_sha256,
                verdicts={c: LabelValue.FAIL for c in Criterion}, notes=f"{secret} {s.case_id}",
                seconds_spent=5.0,
            )
        )  # fmt: skip
    monkeypatch.setenv("EVALKIT_LABELER", "hayyan-r2")
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    first = samples[0]
    assert first.case_id in at.subheader[0].value and "labeled" not in at.subheader[0].value
    judged = applicability(load_rubrics(RUBRICS, None), first.case, first.response)
    for criterion, applies in judged.items():
        radio = at.radio(key=f"{first.case_id}:{criterion.value}")
        assert radio.value == (None if applies else LabelValue.N_A.value)
    assert at.text_area(key=f"{first.case_id}:notes").value == ""
    assert not at.success
    shown = [e.value for kind in ("text", "markdown", "caption", "subheader", "header", "info")
             for e in getattr(at, kind)] + [e.value for e in at.sidebar.markdown]  # fmt: skip
    assert not any(secret in str(v) for v in shown)
    # in the "labeled" filter hayyan-r2 has nothing yet
    at.radio(key="filter").set_value("labeled").run()
    assert any("Nothing under 'labeled'" in i.value for i in at.info)

    # hayyan-r2 saves; hayyan's rows are untouched
    at.radio(key="filter").set_value("unlabeled").run()
    fill(at, first, "pass")
    at.button(key="save").click().run()
    rows = LabelStore(labels).all()
    assert [r.labeler for r in rows] == ["hayyan", "hayyan", "hayyan-r2"]
    assert all(r.notes and secret in r.notes for r in rows[:2])
    assert secret not in at.success[0].value


def test_labels_file_defaults_to_one_per_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert raw_file_for(Path("datasets/label_sample_v1.jsonl")) == "labels_raw_v1.jsonl"
    assert raw_file_for(Path("datasets/overlap_v1.jsonl")) == "labels_raw_overlap_v1.jsonl"
    assert raw_file_for(Path("relabel_v1.jsonl")) == "labels_raw_relabel_v1.jsonl"
    samples = list(read_jsonl(ROOT / "datasets" / "overlap_v1.jsonl", LabelSample))[:1]
    write_jsonl(tmp_path / "overlap_v1.jsonl", samples)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EVALKIT_LABELER", "second")
    monkeypatch.setenv("EVALKIT_LABEL_SAMPLE", str(tmp_path / "overlap_v1.jsonl"))
    monkeypatch.delenv("EVALKIT_LABELS", raising=False)
    monkeypatch.setenv("EVALKIT_RUBRICS", str(RUBRICS))
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    assert any("overlap_v1.jsonl" in m.value for m in at.sidebar.markdown)
    fill(at, samples[0], "pass")
    at.button(key="save").click().run()
    saved = tmp_path / "datasets" / "labels" / "labels_raw_overlap_v1.jsonl"
    assert [r.labeler for r in LabelStore(saved).all()] == ["second"]


def test_n_a_is_hidden_where_never_allowed_and_escalation_facts_are_shown(
    env: tuple[list[LabelSample], Path],
) -> None:
    samples, labels = env
    first = samples[0]
    # a saved n_a on relevance (allowed in v1) comes back unset, not silently kept
    data = {c: LabelValue.PASS for c in Criterion} | {Criterion.RELEVANCE: LabelValue.N_A}
    LabelStore(labels).append(
        Label(
            case_id=first.case_id, run_id=first.run_id, labeler="tester", timestamp=T0,
            rubric_hash="a" * 64, response_sha256=first.response_sha256, verdicts=data,
            seconds_spent=1.0,
        )
    )  # fmt: skip
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    at.radio(key="filter").set_value("labeled").run()  # the labeled first record
    assert not at.exception and first.case_id in at.subheader[0].value
    for criterion in (Criterion.SAFETY, Criterion.RELEVANCE, Criterion.TONE):
        radio = at.radio(key=f"{first.case_id}:{criterion.value}")
        assert list(radio.options) == ["pass", "fail"]
    assert at.radio(key=f"{first.case_id}:relevance").value is None
    assert at.radio(key=f"{first.case_id}:tone").value == "pass"
    escalation = at.radio(key=f"{first.case_id}:escalation_appropriate")
    assert list(escalation.options) == ["pass", "fail", "n_a"]
    facts = escalation_facts(first.case.should_escalate, first.response.escalated)
    assert facts in [c.value for c in at.caption]
    assert escalation_facts(True, False) == "escalated: **no** · should_escalate: **yes**"
    assert escalation_facts(None, True) == "escalated: **yes** · should_escalate: **not set**"


def test_key_rules_box_tops_the_sidebar_and_follows_the_rubric_picked(
    env: tuple[list[LabelSample], Path],
) -> None:
    by = {r.criterion: r for r in load_rubrics(RUBRICS, None)}
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    box = at.sidebar.info[0].value
    assert box.startswith("**Key rules: safety**")
    assert all(rule in box for rule in by[Criterion.SAFETY].key_rules)
    at.selectbox(key="rubric_view").set_value("tone").run()
    box = at.sidebar.info[0].value
    assert box.startswith("**Key rules: tone**")
    assert all(rule in box for rule in by[Criterion.TONE].key_rules)


def test_saved_n_a_on_a_judged_criterion_comes_back_unset_with_a_warning(
    env: tuple[list[LabelSample], Path],
) -> None:
    from evalkit.labeling.applicability import allows_na

    samples, labels = env
    rubrics_ = load_rubrics(RUBRICS, None)
    judged = applicability(rubrics_, samples[0].case, samples[0].response)
    by = {r.criterion: r for r in rubrics_}
    # a criterion the rubric judges for this record and that offers n_a
    target = next(c for c, applies in judged.items() if applies and allows_na(by[c]))
    first = samples[0]
    data = {c: LabelValue.PASS for c in Criterion} | {target: LabelValue.N_A}
    LabelStore(labels).append(
        Label(
            case_id=first.case_id, run_id=first.run_id, labeler="tester", timestamp=T0,
            rubric_hash=rubric_hash(RUBRICS), response_sha256=first.response_sha256,
            verdicts=data, seconds_spent=1.0,
        )
    )  # fmt: skip
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    at.radio(key="filter").set_value("labeled").run()
    assert first.case_id in at.subheader[0].value
    radio = at.radio(key=f"{first.case_id}:{target.value}")
    assert radio.value is None and "n_a" in list(radio.options)
    assert any("rubric says judge this" in w.value for w in at.warning)
    # saving without choosing is refused; choosing pass saves pass
    at.button(key="save").click().run()
    assert any(f"Choose a verdict for: {target.value}" in e.value for e in at.error)
    at.radio(key=f"{first.case_id}:{target.value}").set_value("pass").run()
    at.button(key="save").click().run()
    assert LabelStore(labels).all()[-1].verdicts[target] is LabelValue.PASS
