import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evalkit.schemas import (
    AgentResponse,
    AnswerType,
    DatabaseDetails,
    KnowledgeDetails,
    ResponseSource,
    RetrievedChunk,
    Route,
)

from .conftest import RESPONSES, ROUTE_FIXTURES, load_response


def raw(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((RESPONSES / f"{name}.json").read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize("path", sorted(RESPONSES.glob("*.json")), ids=lambda p: p.stem)
def test_every_fixture_parses_without_unknown_fields(path: Path) -> None:
    resp = AgentResponse.model_validate_json(path.read_bytes())
    assert resp.unknown_fields == {}


@pytest.mark.parametrize("name", ROUTE_FIXTURES)
def test_route_fixture_has_its_route(name: str) -> None:
    resp = load_response(name)
    assert resp.route is Route(name)
    assert resp.ok and not resp.is_error
    assert resp.eval_mode is True


@pytest.mark.parametrize("name", ROUTE_FIXTURES)
def test_llm_called_only_for_knowledge(name: str) -> None:
    resp = load_response(name)
    assert resp.model  # set on every route, which is why llm_called must not use it
    assert resp.llm_called is (name == "knowledge")


def test_knowledge_fixture() -> None:
    resp = load_response("knowledge")
    details = resp.parsed_details
    assert isinstance(details, KnowledgeDetails)
    assert details.answer_type is AnswerType.LLM
    assert len(details.chunks_used) == 3
    assert resp.source is ResponseSource.KNOWLEDGE_BASE
    assert resp.retrieved_chunks and all(
        isinstance(c, RetrievedChunk) for c in resp.retrieved_chunks
    )
    assert resp.total_tokens == (resp.prompt_tokens or 0) + (resp.completion_tokens or 0) > 0
    assert resp.checker_replaced is False


def test_database_fixture() -> None:
    resp = load_response("database")
    details = resp.parsed_details
    assert isinstance(details, DatabaseDetails)
    product = details.products[0]
    assert product.name == "Hydra Balance Moisturizer"
    assert product.price == 24.99
    assert "Niacinamide" in product.ingredients
    assert resp.total_tokens == 0


def test_fixed_reply_fixtures_have_no_details() -> None:
    for name in ["greeting", "clarify", "escalate", "guardrail", "medical"]:
        resp = load_response(name)
        assert resp.source is ResponseSource.FIXED_REPLY
        assert resp.parsed_details is None


def test_escalate_fixture_uses_escalated_flag() -> None:
    resp = load_response("escalate")
    assert resp.escalated is True
    assert resp.escalation is None  # eval_mode skips the escalation record


def test_error_fixture() -> None:
    resp = load_response("error")
    assert resp.is_error
    assert resp.route is None and resp.intents is None and resp.source is None
    assert resp.error == "TimeoutError: simulated"
    assert not resp.llm_called and resp.total_tokens == 0


@pytest.mark.parametrize(
    ("answer_type", "prompt_tokens", "expected"),
    [
        ("quoted_source", 900, True),
        ("not_found", 900, True),
        ("llm", 900, False),
        ("quoted_source", 0, False),
        ("not_found", None, False),
    ],
)
def test_checker_replaced(answer_type: str, prompt_tokens: int | None, expected: bool) -> None:
    data = raw("knowledge") | {"answer_type": answer_type, "prompt_tokens": prompt_tokens}
    assert AgentResponse.model_validate(data).checker_replaced is expected


def test_misshapen_knowledge_details_stay_raw() -> None:
    data = raw("knowledge")
    data["details"]["chunks_used"] = "not a list"
    resp = AgentResponse.model_validate(data)
    assert resp.parsed_details == data["details"]


def test_misshapen_database_details_stay_raw() -> None:
    data = raw("database") | {"details": [{"unexpected": True}]}
    assert AgentResponse.model_validate(data).parsed_details == [{"unexpected": True}]


def test_details_for_unmodelled_route_stay_raw() -> None:
    data = raw("greeting") | {"details": {"anything": 1}}
    assert AgentResponse.model_validate(data).parsed_details == {"anything": 1}


def test_unknown_fields_are_kept_and_exposed() -> None:
    data = raw("greeting") | {"brand_new_field": {"x": 1}}
    resp = AgentResponse.model_validate(data)
    assert resp.unknown_fields == {"brand_new_field": {"x": 1}}
    # survives a dump/parse cycle (as inside a persisted CaseResult)
    again = AgentResponse.model_validate_json(resp.model_dump_json())
    assert again == resp and again.unknown_fields == resp.unknown_fields


def test_computed_properties_are_not_serialized() -> None:
    dumped = load_response("knowledge").model_dump()
    for name in ["llm_called", "total_tokens", "is_error", "checker_replaced", "unknown_fields"]:
        assert name not in dumped


def test_unknown_route_value_is_rejected() -> None:
    with pytest.raises(ValidationError, match="route"):
        AgentResponse.model_validate(raw("greeting") | {"route": "billing"})


def test_retrieved_chunk_documents_score_direction() -> None:
    assert RetrievedChunk.__doc__ is not None
    assert "LOWER score = MORE relevant" in RetrievedChunk.__doc__
