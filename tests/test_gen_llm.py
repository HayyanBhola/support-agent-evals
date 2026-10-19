"""Generation LLM access: OpenAI and Groq clients, retries, cache, JSON retry, usage."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from evalkit.generation.llm import (
    APPROX_PRICES,
    BadOutput,
    BudgetSkip,
    Completion,
    Estimate,
    FakeLLM,
    GenLLM,
    GroqClient,
    LLMRequestError,
    ModelConfig,
    OpenAIClient,
    QuotaExhausted,
    Usage,
    default_model_config,
    is_daily_limit,
    parse_duration,
    parse_wait,
    strict_json_schema,
)
from evalkit.generation.prompts import Prompt, load_prompt, prompt_name_of

MODEL = ModelConfig("gpt-5-mini")
GROQ = default_model_config("groq", "openai/gpt-oss-120b")
PROMPT = Prompt("demo_v1", "<!-- prompt: demo_v1 -->\nReply with JSON.", "a" * 64)


class Answer(BaseModel):
    text: str


# --------------------------------------------------------------------------- prompts


def test_load_prompt_normalizes_crlf_and_hashes(tmp_path: Path) -> None:
    (tmp_path / "x_v1.md").write_bytes(b"<!-- prompt: x_v1 -->\r\nhello\r\n")
    (tmp_path / "y_v1.md").write_bytes(b"<!-- prompt: y_v1 -->\nhello\n")
    x = load_prompt("x", prompts_dir=tmp_path)
    assert x.name == "x_v1"
    assert "\r" not in x.text
    assert len(x.sha256) == 64


def test_load_prompt_rejects_wrong_header(tmp_path: Path) -> None:
    (tmp_path / "x_v2.md").write_text("<!-- prompt: x_v1 -->\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must start with"):
        load_prompt("x", 2, prompts_dir=tmp_path)


def test_prompt_name_of() -> None:
    assert prompt_name_of("<!-- prompt: a_b_v3 -->\nrest") == "a_b_v3"
    assert prompt_name_of("no header") == ""


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("message", "seconds"),
    [
        ("Please try again in 1m2.5s. Need more tokens?", 62.5),
        ("Please try again in 750ms.", 0.75),
        ("Please try again in 7.66s.", 7.66),
        ("Please try again in 1h2m3s.", 3723.0),
        ("Rate limit reached.", None),
    ],
)
def test_parse_wait(message: str, seconds: float | None) -> None:
    if seconds is None:
        assert parse_wait(message) is None
    else:
        assert parse_wait(message) == pytest.approx(seconds)


def test_is_daily_limit() -> None:
    assert is_daily_limit("Limit 100000, Used 99000 on tokens per day (TPD)")
    assert is_daily_limit("requests per day (RPD): Limit 1000")
    assert not is_daily_limit("tokens per minute (TPM): Limit 12000")


def test_usage_and_cost() -> None:
    u = Usage()
    u.add(MODEL.name, Completion("", 1_000_000, 0), cached=False, generator="g")
    u.add(MODEL.name, Completion("", 0, 1_000_000, 400_000), cached=True, generator="g")
    u.add("unknown-model", Completion("", 5, 5), cached=False)
    m = u.by_model[MODEL.name]
    assert (m.calls, m.cached_calls, m.tokens, m.cached_tokens) == (1, 1, 1_000_000, 1_000_000)
    assert m.cached_reasoning_tokens == 400_000
    assert u.by_generator[("g", MODEL.name)].calls == 1
    price = APPROX_PRICES[MODEL.name]
    assert (price.input_per_m, price.output_per_m) == (0.125, 1.00)
    assert u.approx_cost(MODEL.name) == pytest.approx(price.input_per_m)
    assert u.approx_cost(MODEL.name, include_cached=True) == pytest.approx(
        price.input_per_m + price.output_per_m
    )
    assert u.approx_cost("unknown-model") is None
    assert u.approx_cost("never-used") is None
    assert u.total_cost() == pytest.approx(price.input_per_m)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("6m0s", 360.0), ("1.5s", 1.5), ("20ms", 0.02), ("1h", 3600.0), ("", None), ("soon", None)],
)
def test_parse_duration(text: str, seconds: float | None) -> None:
    assert parse_duration(text) == (None if seconds is None else pytest.approx(seconds))


def test_default_model_configs() -> None:
    openai = default_model_config("openai")
    assert (openai.name, openai.temperature, openai.reasoning_effort) == ("gpt-5-mini", None, "low")
    assert "temperature" not in openai.params()
    assert default_model_config("groq", "qwen/qwen3.8-27b").reasoning_effort is None
    assert GROQ.params() == {"max_tokens": 4096, "temperature": 0.8, "reasoning_effort": "low"}
    assert "reasoning_effort" not in default_model_config("openai", None, None).params()


class Nested(BaseModel):
    title: str  # a property NAMED title must survive
    tags: list[str] = []


class Outer(BaseModel):
    items: list[Nested]
    note: str | None = None


def test_strict_json_schema_closes_objects_and_requires_everything() -> None:
    schema = strict_json_schema(Outer)
    assert schema["required"] == ["items", "note"]
    assert schema["additionalProperties"] is False
    nested = schema["$defs"]["Nested"]
    assert nested["required"] == ["title", "tags"]
    assert nested["additionalProperties"] is False
    assert "title" in nested["properties"]
    assert "default" not in json.dumps(schema)
    assert {"type": "null"} in schema["properties"]["note"]["anyOf"]


# --------------------------------------------------------------------------- GroqClient


class Recorder:
    """httpx handler replaying scripted responses; records each request's JSON body."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(content: str = '{"text": "hi"}') -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        },
    )


def error(status: int, message: str, **extra: Any) -> httpx.Response:
    headers = extra.pop("headers", {})
    return httpx.Response(status, json={"error": {"message": message, **extra}}, headers=headers)


def make_groq(rec: Recorder, sleeps: list[float], **kwargs: Any) -> GroqClient:
    async def sleep(s: float) -> None:
        sleeps.append(s)

    return GroqClient("key", transport=httpx.MockTransport(rec), sleep=sleep, **kwargs)


MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


async def test_groq_ok_sends_json_mode_and_seed() -> None:
    rec, sleeps = Recorder(ok()), []
    c = await make_groq(rec, sleeps).complete(GROQ, MESSAGES, 5, Answer)
    assert (c.content, c.prompt_tokens, c.completion_tokens) == ('{"text": "hi"}', 11, 7)
    body = rec.bodies[0]
    assert body["model"] == GROQ.name
    assert body["response_format"] == {"type": "json_object"}
    assert body["seed"] == 5
    assert body["temperature"] == GROQ.temperature
    assert sleeps == []


async def test_groq_waits_out_per_minute_limit_using_retry_after() -> None:
    rec, sleeps = Recorder(error(429, "TPM", headers={"retry-after": "3"}), ok()), []
    await make_groq(rec, sleeps).complete(GROQ, MESSAGES, 5, Answer)
    assert sleeps == [3.5]


async def test_groq_waits_using_message_hint_or_default() -> None:
    rec = Recorder(
        error(429, "tokens per minute (TPM). Please try again in 1.5s."),
        error(429, "slow down"),
        ok(),
    )
    sleeps: list[float] = []
    await make_groq(rec, sleeps).complete(GROQ, MESSAGES, 5, Answer)
    assert sleeps == [2.0, 10.5]


async def test_groq_daily_limit_raises_quota_without_retry() -> None:
    rec = Recorder(error(429, "tokens per day (TPD): Limit 100000. Please try again in 9m."))
    with pytest.raises(QuotaExhausted) as info:
        await make_groq(rec, []).complete(GROQ, MESSAGES, 5, Answer)
    assert info.value.model == GROQ.name
    assert info.value.wait_s == 540


async def test_groq_very_long_wait_counts_as_quota() -> None:
    rec = Recorder(error(429, "x", headers={"retry-after": "900"}))
    with pytest.raises(QuotaExhausted):
        await make_groq(rec, []).complete(GROQ, MESSAGES, 5, Answer)


async def test_groq_rate_limit_retries_run_out() -> None:
    rec = Recorder(error(429, "TPM"), error(429, "TPM"), error(429, "TPM"))
    with pytest.raises(QuotaExhausted, match="still rate limited"):
        await make_groq(rec, [], max_retries=2).complete(GROQ, MESSAGES, 5, Answer)


async def test_groq_retries_server_errors_and_connection_failures() -> None:
    rec = Recorder(error(503, "busy"), httpx.ConnectError("down"), ok())
    sleeps: list[float] = []
    await make_groq(rec, sleeps).complete(GROQ, MESSAGES, 5, Answer)
    assert len(sleeps) == 2


async def test_groq_connection_failures_run_out() -> None:
    rec = Recorder(httpx.ConnectError("down"), httpx.ConnectError("down"))
    with pytest.raises(LLMRequestError, match="cannot reach Groq"):
        await make_groq(rec, [], max_retries=1).complete(GROQ, MESSAGES, 5, Answer)


async def test_groq_json_validate_failed_returns_the_bad_output() -> None:
    rec = Recorder(error(400, "bad json", code="json_validate_failed", failed_generation="{oops"))
    c = await make_groq(rec, []).complete(GROQ, MESSAGES, 5, Answer)
    assert c.content == "{oops"


async def test_groq_other_client_errors_are_not_retried() -> None:
    rec = Recorder(httpx.Response(401, text="unauthorized"))
    with pytest.raises(LLMRequestError, match="HTTP 401"):
        await make_groq(rec, []).complete(GROQ, MESSAGES, 5, Answer)


async def test_groq_unexpected_body() -> None:
    rec = Recorder(httpx.Response(200, json={"choices": []}))
    with pytest.raises(LLMRequestError, match="unexpected response"):
        await make_groq(rec, []).complete(GROQ, MESSAGES, 5, Answer)


async def test_groq_needs_key_and_hides_it() -> None:
    with pytest.raises(LLMRequestError, match="GROQ_API_KEY"):
        GroqClient("")
    t = GroqClient("secret-key")
    assert "secret-key" not in repr(t)
    await t.aclose()


# --------------------------------------------------------------------------- GenLLM


async def test_genllm_parses_and_caches(tmp_path: Path) -> None:
    fake = FakeLLM(lambda name, inputs: {"text": inputs["q"]})
    llm = GenLLM(fake, seed=1, cache_dir=tmp_path)
    a = await llm.ask(PROMPT, {"q": "x"}, Answer, MODEL, generator="g", est_output_tokens=10)
    b = await llm.ask(PROMPT, {"q": "x"}, Answer, MODEL, generator="g", est_output_tokens=10)
    assert a == b == Answer(text="x")
    assert len(fake.calls) == 1
    u = llm.usage.by_model[MODEL.name]
    assert (u.calls, u.cached_calls, u.prompt_tokens, u.cached_prompt_tokens) == (1, 1, 100, 100)
    llm.close()


async def test_genllm_cache_key_covers_prompt_hash_model_and_inputs(tmp_path: Path) -> None:
    fake = FakeLLM(lambda name, inputs: {"text": "ok"})
    llm = GenLLM(fake, seed=1, cache_dir=tmp_path)
    other_prompt = Prompt(PROMPT.name, PROMPT.text, "b" * 64)
    other_model = ModelConfig(MODEL.name, temperature=0.2)
    for prompt, model, inputs in [
        (PROMPT, MODEL, {"q": 1}),
        (other_prompt, MODEL, {"q": 1}),
        (PROMPT, other_model, {"q": 1}),
        (PROMPT, MODEL, {"q": 2}),
    ]:
        await llm.ask(prompt, inputs, Answer, model, generator="g", est_output_tokens=1)
    assert len(fake.calls) == 4
    llm.close()
    reseeded = GenLLM(fake, seed=2, cache_dir=tmp_path)
    await reseeded.ask(PROMPT, {"q": 1}, Answer, MODEL, generator="g", est_output_tokens=1)
    assert len(fake.calls) == 5
    reseeded.close()


async def test_genllm_retries_bad_json_once_showing_the_error() -> None:
    def responder(name: str, inputs: dict[str, Any]) -> Any:
        return "not json" if inputs["_attempt"] == 1 else {"text": "fixed"}

    fake = FakeLLM(responder)
    llm = GenLLM(fake, seed=1, cache_dir=None)
    out = await llm.ask(PROMPT, {}, Answer, MODEL, generator="g", est_output_tokens=1)
    assert out.text == "fixed"
    assert [c[2] for c in fake.calls] == [1, 2]


async def test_genllm_bad_output_after_retry() -> None:
    fake = FakeLLM(lambda name, inputs: {"wrong": 1})
    llm = GenLLM(fake, seed=1, cache_dir=None)
    with pytest.raises(BadOutput, match="text: Field required"):
        await llm.ask(PROMPT, {}, Answer, MODEL, generator="g", est_output_tokens=1)
    assert len(fake.calls) == 2


async def test_genllm_estimate_mode_calls_nothing(tmp_path: Path) -> None:
    fake = FakeLLM(lambda name, inputs: {"text": "ok"})
    warm = GenLLM(fake, seed=1, cache_dir=tmp_path)
    await warm.ask(PROMPT, {"q": 1}, Answer, MODEL, generator="g", est_output_tokens=1)
    warm.close()
    est = Estimate()
    llm = GenLLM(fake, seed=1, cache_dir=tmp_path, estimate=est)
    for q in (1, 2):
        with pytest.raises(BadOutput, match="estimate only"):
            await llm.ask(PROMPT, {"q": q}, Answer, MODEL, generator="g", est_output_tokens=30)
    llm.close()
    key = ("g", MODEL.name)
    assert len(fake.calls) == 1
    assert est.cached_calls[key] == 1
    assert est.calls[key] == 1
    assert est.output_tokens[key] == 30
    assert est.input_tokens[key] > 0


# --------------------------------------------------------------------------- OpenAIClient


def make_openai(rec: Recorder, sleeps: list[float], **kwargs: Any) -> OpenAIClient:
    async def sleep(s: float) -> None:
        sleeps.append(s)

    return OpenAIClient("key", transport=httpx.MockTransport(rec), sleep=sleep, **kwargs)


def openai_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": '{"text": "hi"}', "refusal": None}}],
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 300,
                "completion_tokens_details": {"reasoning_tokens": 256},
            },
        },
    )


async def test_openai_sends_strict_schema_and_reasoning_effort() -> None:
    rec = Recorder(openai_ok())
    c = await make_openai(rec, []).complete(MODEL, MESSAGES, 5, Answer)
    assert (c.prompt_tokens, c.completion_tokens, c.reasoning_tokens) == (50, 300, 256)
    body = rec.bodies[0]
    assert body["model"] == "gpt-5-mini"
    assert body["reasoning_effort"] == "low"
    assert body["max_completion_tokens"] == MODEL.max_tokens
    assert "max_tokens" not in body and "temperature" not in body and "seed" not in body
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "Answer" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["required"] == ["text"]


async def test_openai_rate_limit_uses_retry_after_ms() -> None:
    rec = Recorder(
        error(429, "Rate limit reached for requests", headers={"retry-after-ms": "1200"}),
        error(429, "x", headers={"retry-after-ms": "soon", "retry-after": "2"}),
        openai_ok(),
    )
    sleeps: list[float] = []
    await make_openai(rec, sleeps).complete(MODEL, MESSAGES, 5, Answer)
    assert sleeps == [pytest.approx(1.7), 2.5]


async def test_openai_insufficient_quota_stops_the_run() -> None:
    rec = Recorder(error(429, "You exceeded your current quota", code="insufficient_quota"))
    with pytest.raises(QuotaExhausted, match="current quota"):
        await make_openai(rec, []).complete(MODEL, MESSAGES, 5, Answer)


async def test_openai_bad_request_and_key() -> None:
    rec = Recorder(error(400, "Unsupported parameter: 'seed'"))
    with pytest.raises(LLMRequestError, match="OpenAI HTTP 400 for gpt-5-mini"):
        await make_openai(rec, []).complete(MODEL, MESSAGES, 5, Answer)
    with pytest.raises(LLMRequestError, match="OPENAI_API_KEY"):
        OpenAIClient("")


async def test_genllm_passes_the_schema_and_counts_reasoning() -> None:
    fake = FakeLLM(lambda name, inputs: {"text": "x"}, tokens=(10, 30, 25))
    llm = GenLLM(fake, seed=1, cache_dir=None)
    await llm.ask(PROMPT, {}, Answer, MODEL, generator="g", est_output_tokens=1)
    assert fake.schemas == ["Answer"]
    assert llm.usage.by_model[MODEL.name].reasoning_tokens == 25


async def test_genllm_call_budget_per_generator() -> None:
    fake = FakeLLM(lambda name, inputs: {"text": "x"})
    llm = GenLLM(fake, seed=1, cache_dir=None, call_budget={"a": 1, "b": 1})
    await llm.ask(PROMPT, {"q": 1}, Answer, MODEL, generator="a", est_output_tokens=1)
    with pytest.raises(BudgetSkip):
        await llm.ask(PROMPT, {"q": 2}, Answer, MODEL, generator="a", est_output_tokens=1)
    await llm.ask(PROMPT, {"q": 3}, Answer, MODEL, generator="b", est_output_tokens=1)
    assert len(fake.calls) == 2


async def test_genllm_cache_keeps_reasoning_tokens(tmp_path: Path) -> None:
    fake = FakeLLM(lambda name, inputs: {"text": "x"}, tokens=(10, 30, 25))
    for _ in range(2):
        llm = GenLLM(fake, seed=1, cache_dir=tmp_path)
        await llm.ask(PROMPT, {}, Answer, MODEL, generator="g", est_output_tokens=1)
        llm.close()
    assert len(fake.calls) == 1
    assert llm.usage.by_model[MODEL.name].cached_reasoning_tokens == 25
