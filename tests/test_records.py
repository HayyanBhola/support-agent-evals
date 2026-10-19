from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from evalkit.schemas import (
    CaseResult,
    Criterion,
    FailureType,
    Label,
    LabelSample,
    MetricResult,
    RunMeta,
)

from .conftest import load_response

T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
T1 = T0 + timedelta(seconds=20)
SHA = "a" * 64


def result(**over: Any) -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "case_id": "case-1",
        "attempts": 1,
        "started_at": T0,
        "finished_at": T1,
    } | over


# --------------------------------------------------------------------------- CaseResult


def test_case_result_with_response() -> None:
    r = CaseResult.model_validate(result(response=load_response("knowledge")))
    assert r.failure_type is None and r.schema_version == 1


@pytest.mark.parametrize("failure", [f for f in FailureType if f is not FailureType.AGENT_ERROR])
def test_case_result_with_failure(failure: FailureType) -> None:
    r = CaseResult.model_validate(result(failure_type=failure, error_message="boom"))
    assert r.response is None


def test_case_result_rejects_both_set() -> None:
    with pytest.raises(ValidationError, match="exactly one of response or failure_type"):
        CaseResult.model_validate(
            result(response=load_response("knowledge"), failure_type="timeout")
        )


def test_case_result_rejects_neither_set() -> None:
    with pytest.raises(ValidationError, match="exactly one of response or failure_type"):
        CaseResult.model_validate(result())


def test_agent_error_keeps_the_error_response() -> None:
    r = CaseResult.model_validate(
        result(response=load_response("error"), failure_type="agent_error")
    )
    assert r.response is not None and r.response.is_error


@pytest.mark.parametrize("response", [None, "knowledge"])
def test_agent_error_needs_an_error_response(response: str | None) -> None:
    resp = load_response(response) if response else None
    with pytest.raises(ValidationError, match="agent_error' needs a response with ok=false"):
        CaseResult.model_validate(result(response=resp, failure_type="agent_error"))


def test_error_response_must_be_classified_as_agent_error() -> None:
    with pytest.raises(ValidationError, match="must have failure_type 'agent_error'"):
        CaseResult.model_validate(result(response=load_response("error")))


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"finished_at": T0 - timedelta(seconds=1)}, "finished_at is before started_at"),
        ({"started_at": datetime(2026, 10, 13)}, "timezone"),
        ({"attempts": -1}, "attempts"),
        ({"attempts": "1"}, "attempts"),
        ({"wall_latency_ms": -5.0}, "wall_latency_ms"),
    ],
)
def test_case_result_field_rules(override: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        CaseResult.model_validate(result(failure_type="timeout") | override)


# --------------------------------------------------------------------------- Label / Metric


def label(**over: Any) -> dict[str, Any]:
    return {
        "case_id": "case-1",
        "run_id": "run-1",
        "labeler": "human:hayyan",
        "timestamp": T0,
        "rubric_hash": SHA,
        "response_sha256": "b" * 64,
        "verdicts": {c.value: "pass" for c in Criterion},
        "seconds_spent": 42.5,
    } | over


@pytest.mark.parametrize("field", ["rubric_hash", "response_sha256", "verdicts", "seconds_spent"])
def test_label_requires(field: str) -> None:
    data = label()
    del data[field]
    with pytest.raises(ValidationError, match=field):
        Label.model_validate(data)


def test_label_is_valid_and_frozen() -> None:
    lab = Label.model_validate(
        label(
            verdicts={c.value: "fail" if c is Criterion.TONE else "n_a" for c in Criterion},
            overrides=["safety"],
            notes="third person",
        )  # fmt: skip
    )
    assert lab.fails == [Criterion.TONE]
    assert lab.overrides == [Criterion.SAFETY]
    with pytest.raises(ValidationError, match="frozen"):
        lab.notes = "x"


def test_label_needs_every_criterion() -> None:
    verdicts = {c.value: "pass" for c in Criterion if c is not Criterion.TONE}
    with pytest.raises(ValidationError, match=r"no verdict for \['tone'\]"):
        Label.model_validate(label(verdicts=verdicts))


@pytest.mark.parametrize(
    "override",
    [
        {"verdicts": {c.value: "maybe" for c in Criterion}},
        {"verdicts": {**{c.value: "pass" for c in Criterion}, "vibes": "pass"}},
        {"labeler": " "},
        {"timestamp": "2026-10-13"},
        {"rubric_hash": "not-a-hash"},
        {"response_sha256": None},
        {"seconds_spent": -1.0},
        {"overrides": ["safety", "safety"]},
        {"overrides": ["vibes"]},
    ],
)
def test_label_rejects_bad_values(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Label.model_validate(label(**override))


def sample(**over: Any) -> dict[str, Any]:
    return {
        "case_id": "shipping-policy-01",
        "run_id": "run-1",
        "stratum": {"expected_route": "knowledge", "answer_type": "llm"},
        "case": {"id": "shipping-policy-01", "question": "What is your shipping policy?",
                 "category": "policy", "expected_route": "knowledge",
                 "must_include": ["shipping"], "source": "human"},
        "response": load_response("knowledge"),
        "response_sha256": SHA,
    } | over  # fmt: skip


def test_label_sample_is_valid() -> None:
    s = LabelSample.model_validate(sample())
    assert s.stratum.answer_type is not None and s.case.id == s.case_id
    assert (
        LabelSample.model_validate(
            sample(stratum={"expected_route": None, "answer_type": None})
        ).stratum.expected_route
        is None
    )


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"case_id": "other-id"}, "case_id 'other-id' != case.id"),
        ({"stratum": {"expected_route": "nowhere", "answer_type": None}}, "expected_route"),
        ({"response_sha256": "short"}, "response_sha256"),
        ({"extra": 1}, "Extra inputs"),
    ],
)
def test_label_sample_rejects(override: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        LabelSample.model_validate(sample(**override))


@pytest.mark.parametrize(
    "fields", [{"value": 0.75}, {"value": 3}, {"passed": False}, {"value": 1.0, "passed": True}]
)
def test_metric_result_valid(fields: dict[str, Any]) -> None:
    MetricResult.model_validate({"case_id": "case-1", "metric": "route_accuracy"} | fields)


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({}, "set value, passed, or both"),
        ({"passed": "true"}, "passed"),
        ({"value": True}, "value"),
    ],
)
def test_metric_result_invalid(fields: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        MetricResult.model_validate({"case_id": "case-1", "metric": "route_accuracy"} | fields)


# --------------------------------------------------------------------------- RunMeta


def run_meta(**over: Any) -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "git_sha": "dcd8107",
        "git_dirty": False,
        "dataset_path": "datasets/smoke.jsonl",
        "dataset_sha256": SHA,
        "config_hash": SHA,
        "agent_health": {"status": "ok", "llm": "gemini/gemini-3.1-flash-lite"},
        "concurrency": 2,
        "started_at": T0,
        "finished_at": T1,
        "counts": {"total": 10, "ok": 9, "failed": 1, "cache_hits": 3},
    } | over


def test_run_meta_valid() -> None:
    meta = RunMeta.model_validate(run_meta())
    assert meta.counts.total == 10 and meta.judge_versions == {}


def test_run_meta_may_be_unfinished_and_outside_git() -> None:
    RunMeta.model_validate(run_meta(finished_at=None, git_sha=None))


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"counts": {"total": 10, "ok": 9, "failed": 2, "cache_hits": 0}}, r"!= total \(10\)"),
        ({"counts": {"total": 1, "ok": 1, "failed": 0, "cache_hits": 2}}, r"cache_hits \(2\)"),
        ({"finished_at": T0 - timedelta(seconds=1)}, "finished_at is before started_at"),
        ({"dataset_sha256": "abc"}, "dataset_sha256"),
        ({"git_sha": "not-a-sha"}, "git_sha"),
        ({"concurrency": 0}, "concurrency"),
    ],
)
def test_run_meta_rules(override: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        RunMeta.model_validate(run_meta(**override))
