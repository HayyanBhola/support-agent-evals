"""Labeling: sampling, append-only store, applicability, workflow and CLI."""

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalkit import cli
from evalkit.io import read_jsonl
from evalkit.labeling import sampling, workflow
from evalkit.labeling.applicability import applicability, is_judged, mismatches
from evalkit.labeling.store import LabelStore
from evalkit.labeling.view import chunks, db_facts, facts_table
from evalkit.manifest import read_manifest
from evalkit.route_report import load_run
from evalkit.rubrics import load_rubrics, rubric_hash
from evalkit.schemas import (
    AgentResponse,
    CaseResult,
    Criterion,
    Label,
    LabelSample,
    TestCase,
)

from .conftest import load_response

ROOT = Path(__file__).resolve().parent.parent
RUBRICS = ROOT / "rubrics"
RUN = "20261013-191847-f23b"
SAMPLE_FILE = ROOT / "datasets" / "label_sample_v1.jsonl"
T0 = datetime(2026, 10, 14, tzinfo=UTC)
RUBRIC_LIST = load_rubrics(RUBRICS, None)
BY_CRITERION = {r.criterion: r for r in RUBRIC_LIST}


def case(cid: str = "c1", route: str | None = "knowledge", **over: Any) -> TestCase:
    data: dict[str, Any] = {
        "id": cid,
        "question": f"question {cid}",
        "category": "test",
        "expected_route": route,
        "source": "human",
        "should_escalate": False,
    }
    return TestCase.model_validate(data | over)


def response(
    route: str = "knowledge", answer_type: str | None = "llm", **over: Any
) -> AgentResponse:
    data = load_response("knowledge").model_dump(mode="json")
    data |= {"route": route, "answer_type": answer_type} | over
    return AgentResponse.model_validate(data)


def result(cid: str, resp: AgentResponse | None = None, failure: str | None = None) -> CaseResult:
    return CaseResult(
        run_id="run-1",
        case_id=cid,
        response=resp,
        failure_type=failure,
        attempts=1,
        started_at=T0,
        finished_at=T0,
    )


def label(cid: str = "c1", labeler: str = "ann", **over: Any) -> Label:
    data: dict[str, Any] = {
        "case_id": cid,
        "run_id": "run-1",
        "labeler": labeler,
        "timestamp": T0,
        "rubric_hash": "a" * 64,
        "response_sha256": "b" * 64,
        "verdicts": {c.value: "pass" for c in Criterion},
        "seconds_spent": 30.0,
    }
    return Label.model_validate(data | over)


# --------------------------------------------------------------------------- sampling


def pool_of(spec: dict[tuple[str | None, str | None], int]) -> list[LabelSample]:
    out = []
    for (route, atype), n in spec.items():
        for i in range(n):
            cid = f"{route or 'none'}-{atype or 'none'}-{i:03d}"
            gold: dict[str, Any] = {"gold_db_facts": {"price": 1.0}} if route == "database" else {}
            out.append(
                LabelSample(
                    case_id=cid,
                    run_id="r",
                    case=case(cid, route, **gold),
                    stratum={"expected_route": route, "answer_type": atype},
                    response=response(route or "knowledge", atype),
                    response_sha256="c" * 64,
                )
            )
    return out


def test_candidates_exclude_skipped_errored_examples_and_unknown_cases() -> None:
    cases = {c.id: c for c in (case("ok1"), case("ok2"), case("skip"), case("ex"), case("err"))}
    results = [
        result("ok1", response()),
        result("ok2", response(answer_type=None)),
        result("skip", failure="skipped"),
        result(
            "err", AgentResponse.model_validate(load_response("error").model_dump()), "agent_error"
        ),
        result("ex", response()),
        result("not-in-dataset", response()),
    ]
    out = sampling.candidates(results, cases, ["ex"], "run-1")
    assert [s.case_id for s in out] == ["ok1", "ok2"]
    assert out[1].stratum.answer_type is None
    assert out[0].response_sha256 == sampling.response_sha256(results[0])


def test_largest_remainder() -> None:
    lr = sampling.largest_remainder
    assert lr(10, {"a": 1, "b": 1}, {}, {"a": 10, "b": 10}) == {"a": 5, "b": 5}
    assert lr(10, {"a": 3, "b": 1}, {"b": 4}, {"a": 10, "b": 10}) == {"a": 5, "b": 5}
    assert lr(10, {"a": 9, "b": 1}, {}, {"a": 2, "b": 3}) == {"a": 2, "b": 3}  # caps bind
    assert lr(5, {"a": 1, "b": 1, "c": 1}, {}, {"a": 5, "b": 5, "c": 5}) == {"a": 2, "b": 2, "c": 1}


def test_allocation_oversamples_llm_knowledge_and_keeps_route_minimums() -> None:
    pool = pool_of(
        {
            ("knowledge", "llm"): 80,
            ("knowledge", "not_found"): 20,
            ("database", None): 70,
            ("greeting", "not_found"): 12,
            ("medical", None): 4,
            (None, None): 9,
        }
    )
    a = sampling.allocate(pool, size=100, llm_quota=40, route_min=10, unchecked_min=6)
    assert a.total == 100
    assert a.by_stratum[("knowledge", "llm")] == 40
    routes = a.by_route()
    assert routes["greeting"] >= 10 and routes["(none)"] >= 6
    assert routes["medical"] == 4  # capped at what exists
    assert set(routes) == {"knowledge", "database", "greeting", "medical", "(none)"}
    with pytest.raises(ValueError, match="exceeds"):
        sampling.allocate(pool, size=1000)


def test_draw_is_deterministic_and_seed_dependent() -> None:
    pool = pool_of({("knowledge", "llm"): 30, ("database", None): 30, ("clarify", None): 12})
    a = sampling.allocate(pool, size=30, llm_quota=10, route_min=5)
    first = [s.case_id for s in sampling.draw(pool, a, 42)]
    assert first == [s.case_id for s in sampling.draw(pool, a, 42)]
    assert first == sorted(first) and len(first) == 30
    assert first != [s.case_id for s in sampling.draw(pool, a, 7)]


def test_real_sample_matches_the_rules() -> None:
    samples = list(read_jsonl(SAMPLE_FILE, LabelSample))
    assert len(samples) == 160 and len({s.case_id for s in samples}) == 160
    examples = set(
        json.loads((RUBRICS / "examples_used.json").read_text(encoding="utf-8"))["case_ids"]
    )
    assert not examples & {s.case_id for s in samples}
    routes = Counter(sampling.stratum_of(s)[0] for s in samples)
    assert all(n >= sampling.ROUTE_MIN for r, n in routes.items() if r != sampling.NO_ROUTE)
    assert routes[sampling.NO_ROUTE] >= sampling.UNCHECKED_MIN
    assert Counter(sampling.stratum_of(s) for s in samples)[sampling.LLM_KNOWLEDGE] == 60
    golden = {c.id: c for c in read_jsonl(ROOT / "datasets" / "golden_v1.2.jsonl", TestCase)}
    assert all(s.case == golden[s.case_id] for s in samples)  # v1.2 gold fields
    entry = read_manifest(ROOT / "datasets" / "MANIFEST.json").datasets["label_sample_v1.jsonl"]
    assert entry.metadata["run_id"] == RUN and entry.metadata["seed"] == 42
    assert entry.metadata["dataset_version"] == "golden_v1.2@1.2"
    assert sum(entry.metadata["strata"].values()) == 160


def test_build_label_sample_is_reproducible(tmp_path: Path) -> None:
    if not (ROOT / "runs" / RUN).exists():
        pytest.skip("Day 7 run not on this machine (runs/ is not in git)")
    out = tmp_path / workflow.SAMPLE_FILE
    dataset = tmp_path / "golden_v1.2.jsonl"
    dataset.write_bytes((ROOT / "datasets" / "golden_v1.2.jsonl").read_bytes())
    sample, allocation = workflow.build_label_sample(
        runs_dir=ROOT / "runs",
        run_id=RUN,
        dataset=dataset,
        rubric_dir=RUBRICS,
        rubric_hash="d" * 64,
        out=out,
    )
    assert out.read_bytes() == SAMPLE_FILE.read_bytes()
    assert allocation.total == 160 == len(sample)
    meta = read_manifest(tmp_path / "MANIFEST.json").datasets[out.name].metadata
    assert meta["rubric_hash"] == "d" * 64 and meta["candidates"] == 362


# --------------------------------------------------------------------------- store


def test_store_appends_and_latest_wins(tmp_path: Path) -> None:
    store = LabelStore(tmp_path / "labels" / "raw.jsonl")
    assert store.all() == [] and store.latest() == {}
    store.append(label("c1", "ann", notes="first"))
    store.append(label("c1", "bob"))
    store.append(label("c1", "ann", notes="second"))
    assert len(store.all()) == 3  # never edited in place
    assert store.for_labeler("ann")["c1"].notes == "second"
    assert set(store.latest()) == {("c1", "ann"), ("c1", "bob")}
    lines = store.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and all(json.loads(line)["case_id"] == "c1" for line in lines)


def test_store_survives_a_crash_mid_write_and_resumes(tmp_path: Path) -> None:
    path = tmp_path / "raw.jsonl"
    LabelStore(path).append(label("c1"))
    with path.open("a", encoding="utf-8") as f:
        f.write('{"case_id": "c2", "run_id": "run-1", "lab')  # cut off by a crash
    store = LabelStore(path)
    assert store.recovered
    assert [lab.case_id for lab in store.all()] == ["c1"]
    store.append(label("c2"))
    assert sorted(LabelStore(path).for_labeler("ann")) == ["c1", "c2"]
    assert not LabelStore(path).recovered


# --------------------------------------------------------------------------- applicability


def judged(criterion: Criterion, c: TestCase, r: AgentResponse) -> bool:
    return is_judged(BY_CRITERION[criterion], c, r)


def test_applies_by_actual_uses_the_reply_route_and_answer_type() -> None:
    c = case(route="clarify")  # not greeting: greeting cases are n_a for faithfulness
    assert judged(Criterion.FAITHFULNESS, c, response("knowledge", "llm"))
    assert judged(Criterion.FAITHFULNESS, c, response("database", None))  # none = no type
    assert not judged(Criterion.FAITHFULNESS, case(route="knowledge"), response("greeting", None))
    assert judged(Criterion.TONE, c, response("greeting", None))


def test_applies_by_expected_uses_the_case_route() -> None:
    misrouted = response("greeting", None)
    assert judged(Criterion.COMPLETENESS, case(route="knowledge"), misrouted)
    assert not judged(Criterion.COMPLETENESS, case(route="greeting"), response("knowledge", "llm"))


def test_applies_when_has_gold_extends_correctness() -> None:
    no_route = case(route=None, gold_db_facts={"product_exists": False}, should_escalate=None)
    assert judged(Criterion.CORRECTNESS, no_route, response("database", None))
    assert not judged(Criterion.COMPLETENESS, no_route, response("database", None))
    plain = case(route=None, should_escalate=None, must_not_include=["x"])
    assert not judged(Criterion.CORRECTNESS, plain, response())
    with_ref = case(route="escalate", should_escalate=True, reference_answer="hand off")
    assert judged(Criterion.CORRECTNESS, with_ref, response("escalate", None))


def test_escalation_is_decided_by_should_escalate() -> None:
    assert judged(
        Criterion.ESCALATION_APPROPRIATE, case(route=None, should_escalate=False), response()
    )
    unset = case(route=None, should_escalate=None, must_not_include=["x"])
    assert not judged(Criterion.ESCALATION_APPROPRIATE, unset, response())
    assert judged(
        Criterion.ESCALATION_APPROPRIATE,
        case(route="greeting", should_escalate=None, must_not_include=["x"]),
        response(),
    )


def test_mismatches_are_warnings() -> None:
    applies = applicability(RUBRIC_LIST, case(route="greeting"), response("greeting", None))
    assert applies[Criterion.CORRECTNESS] is False and applies[Criterion.TONE] is True
    verdicts = {c.value: ("n_a" if not applies[c] else "pass") for c in Criterion}
    assert mismatches(label(verdicts=verdicts), applies) == []
    bad = verdicts | {"tone": "n_a", "correctness": "fail"}
    found = mismatches(label(verdicts=bad, notes="x"), applies)
    assert any("tone is n_a but the rubric says judge it" in m for m in found)
    assert any("correctness is fail but the rubric says n_a" in m for m in found)
    assert mismatches(label(verdicts=bad, notes="x", overrides=["correctness"]), applies) == [
        "c1: tone is n_a but the rubric says judge it"
    ]


# --------------------------------------------------------------------------- view


def test_view_extracts_chunks_db_facts_and_facts_table() -> None:
    samples = {s.case_id: s for s in read_jsonl(SAMPLE_FILE, LabelSample)}
    kn = next(
        s
        for s in samples.values()
        if s.response.answer_type and s.response.answer_type.value == "llm"
    )
    assert chunks(kn.response) and db_facts(kn.response) == []
    db = next(
        s
        for s in samples.values()
        if s.response.route and s.response.route.value == "database" and s.response.details
    )
    assert db_facts(db.response)
    order = samples["syn-database-83b7e883"]  # an order lookup for an unknown id
    assert db_facts(order.response) == [{"order_id": "GC10250", "found": False}]
    product = samples["legacy-db-price-hydra"]
    assert db_facts(product.response)[0]["product_id"] == "GC-P001"
    no_details = response("knowledge", "not_found", details=None, retrieved_chunks=None)
    assert chunks(no_details) == []
    assert facts_table(kn)["actual route"] == "knowledge"
    escalated = LabelSample.model_validate(
        kn.model_dump()
        | {"response": response().model_copy(update={"route": None, "escalated": None})}
    )
    table = facts_table(escalated)
    assert table["actual route"] == "-" and table["escalated"] == "-"


# --------------------------------------------------------------------------- workflow + CLI


def sample_labels(samples: list[LabelSample], who: str, h: str) -> list[Label]:
    out = []
    for s in samples:
        applies = applicability(RUBRIC_LIST, s.case, s.response)
        verdicts = {c.value: ("pass" if applies[c] else "n_a") for c in Criterion}
        out.append(
            label(
                s.case_id, who, rubric_hash=h, response_sha256=s.response_sha256, verdicts=verdicts
            )
        )
    return out


def test_status_validate_export_workflow(tmp_path: Path) -> None:
    samples = list(read_jsonl(SAMPLE_FILE, LabelSample))[:5]
    current = rubric_hash(RUBRICS)
    store = LabelStore(tmp_path / "labels" / "raw.jsonl")
    good = sample_labels(samples[:3], "ann", current)
    for lab in good:
        store.append(lab)
    store.append(
        good[0].model_copy(update={"rubric_hash": "e" * 64, "overrides": [Criterion.TONE]})
    )
    st = workflow.status(samples, store.for_labeler("ann"), current)
    assert st.done == 3 and st.total == 5 and st.stale == [samples[0].case_id]
    assert st.overrides == 1
    unknown = workflow.status(samples, {"zzz": label("zzz")}, current)
    assert unknown.unknown == ["zzz"] and unknown.done == 0

    verdicts = {c.value: "fail" for c in Criterion}
    bad = [
        label(samples[3].case_id, "ann", verdicts=verdicts),  # fail, no note, wrong sha
        label("not-in-sample", "ann"),
    ]
    all_na = label(
        samples[4].case_id,
        "ann",
        response_sha256=samples[4].response_sha256,
        verdicts={c.value: "n_a" for c in Criterion},
    )
    v = workflow.validate(samples, [*good, *bad, all_na], RUBRIC_LIST)
    assert any("not in the labeling sample" in e for e in v.errors)
    assert any("response_sha256 mismatch" in e for e in v.errors)
    assert any("without a note" in e for e in v.errors)
    assert not any(e.startswith(good[1].case_id) for e in v.errors)
    assert any("tone is n_a but the rubric says judge it" in w for w in v.warnings)

    from evalkit.io import write_jsonl

    write_jsonl(tmp_path / workflow.SAMPLE_FILE, samples)
    out = tmp_path / workflow.EXPORT_FILE
    exported = workflow.export_labels(store, out)
    assert [(lab.case_id, lab.labeler) for lab in exported] == sorted(
        (lab.case_id, lab.labeler) for lab in exported
    )
    assert len(list(read_jsonl(out, Label))) == 3
    meta = read_manifest(tmp_path / "MANIFEST.json").datasets[out.name].metadata
    assert meta["labelers"] == {"ann": 3} and meta["overrides"] == 1


def invoke(*args: str) -> Any:
    return CliRunner().invoke(cli.app, list(args))


def test_cli_label_commands(tmp_path: Path) -> None:
    samples = list(read_jsonl(SAMPLE_FILE, LabelSample))[:4]
    sample_path = tmp_path / "sample.jsonl"
    from evalkit.io import write_jsonl

    write_jsonl(sample_path, samples)
    raw = tmp_path / "labels" / "raw.jsonl"
    common = ["--sample", str(sample_path), "--labels", str(raw), "--rubric-dir", str(RUBRICS)]
    empty = invoke("labels", "status", *common)
    assert empty.exit_code == 0 and "no labels yet (0/4)" in empty.output
    store = LabelStore(raw)
    for lab in sample_labels(samples[:2], "ann", rubric_hash(RUBRICS)):
        store.append(lab)
    st = invoke("labels", "status", *common)
    assert st.exit_code == 0, st.output
    assert "ann: 2/4 labeled" in st.output and "the current rubric: 0" in st.output
    assert "first-pass time per record" in st.output
    assert invoke("labels", "status", *common, "--labeler", "bob").exit_code == 0
    ok = invoke("labels", "validate", *common)
    assert ok.exit_code == 0 and "0 error(s)" in ok.output
    store.append(label(samples[2].case_id, "ann", verdicts={c.value: "fail" for c in Criterion}))
    bad = invoke("labels", "validate", *common)
    assert bad.exit_code == 1 and "without a note" in bad.output
    out = tmp_path / "labels_v1.jsonl"
    ex = invoke(
        "labels", "export", "--labels", str(raw), "--out", str(out), "--sample", str(sample_path)
    )
    assert ex.exit_code == 0 and "3 labels" in ex.output
    assert invoke("labels", "status", "--sample", str(tmp_path / "nope.jsonl")).exit_code == 2


def test_cli_label_sample(tmp_path: Path) -> None:
    if not (ROOT / "runs" / RUN).exists():
        pytest.skip("Day 7 run not on this machine (runs/ is not in git)")
    out = tmp_path / "s.jsonl"
    (tmp_path / "golden_v1.2.jsonl").write_bytes(
        (ROOT / "datasets" / "golden_v1.2.jsonl").read_bytes()
    )
    r = invoke(
        "label",
        "sample",
        "--out",
        str(out),
        "--runs-dir",
        str(ROOT / "runs"),
        "--rubric-dir",
        str(RUBRICS),
        "--dataset",
        str(tmp_path / "golden_v1.2.jsonl"),
    )
    assert r.exit_code == 0, r.output
    assert "160 records, seed 42" in r.output and "knowledge 64" in r.output
    assert out.read_bytes() == SAMPLE_FILE.read_bytes()
    bad = invoke("label", "sample", "--run-id", "no-such-run", "--out", str(out))
    assert bad.exit_code == 2 and "cannot draw the sample" in bad.output


def test_load_run_of_day7_is_consistent_with_the_sample() -> None:
    if not (ROOT / "runs" / RUN).exists():
        pytest.skip("Day 7 run not on this machine (runs/ is not in git)")
    _, results, _ = load_run(ROOT / "runs", RUN)
    by_id = {r.case_id: r for r in results}
    for s in read_jsonl(SAMPLE_FILE, LabelSample):
        assert sampling.response_sha256(by_id[s.case_id]) == s.response_sha256


# --------------------------------------------------------------------------- faithfulness


NOT_FOUND_REPLY = (
    "I'm sorry, I don't have that information. A GlowCare team member can help you further."
)


@pytest.mark.parametrize(
    ("reply", "claims"),
    [
        (
            NOT_FOUND_REPLY,
            False,
        ),
        ("I am escalating this matter to a human team member.", True),
        ("This case should be escalated to a human team member.", True),
        ("A GlowCare team member will be able to assist you further.", True),
        ("I'm passing your message to a GlowCare team member right away.", True),
        ("I've created a ticket for you.", True),
        ("We have an escalation policy for complaints.", False),
    ],
)
def test_claims_action(reply: str, claims: bool) -> None:
    from evalkit.labeling.applicability import claims_action

    assert claims_action(reply) is claims


def test_faithfulness_is_n_a_for_not_found_unless_it_claims_an_action() -> None:
    c = case(route="knowledge")
    plain = response(
        "knowledge",
        "not_found",
        reply=NOT_FOUND_REPLY,
    )
    claiming = response(
        "knowledge", "not_found", reply="I don't know, but I am escalating this now."
    )
    assert not judged(Criterion.FAITHFULNESS, c, plain)
    assert judged(Criterion.FAITHFULNESS, c, claiming)
    assert judged(Criterion.FAITHFULNESS, c, response("knowledge", "llm"))
    # only faithfulness is affected: tone is still judged on the same reply
    assert judged(Criterion.TONE, c, plain)


def test_faithfulness_is_n_a_for_greeting_cases() -> None:
    greeting = case(route="greeting")
    assert not judged(Criterion.FAITHFULNESS, greeting, response("knowledge", "llm"))
    assert not judged(Criterion.FAITHFULNESS, greeting, response("escalate", None))
    assert judged(Criterion.FAITHFULNESS, case(route="clarify"), response("escalate", None))


def test_first_pass_timing_uses_the_earliest_row_per_record() -> None:
    from datetime import timedelta

    later = T0 + timedelta(hours=1)
    labels = [
        label("c2", seconds_spent=90.0, timestamp=later),  # re-save of c2: ignored
        label("c1", seconds_spent=30.0),
        label("c2", seconds_spent=60.0),
        label("c1", "bob", seconds_spent=10.0),  # another labeler counts on its own
    ]
    t = workflow.first_pass_timing(labels)
    assert t == workflow.Timing(records=3, mean=33.3, median=30.0, total_minutes=1.7)
    assert workflow.first_pass_timing([]) is None


def test_n_a_is_never_allowed_for_criteria_judged_on_every_reply() -> None:
    from evalkit.labeling.applicability import allows_na

    never = {r.criterion for r in RUBRIC_LIST if not allows_na(r)}
    assert never == {Criterion.SAFETY, Criterion.RELEVANCE, Criterion.TONE}
