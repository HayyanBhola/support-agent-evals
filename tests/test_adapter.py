from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from evalkit.adapters import (
    AgentConnectionError,
    AgentHTTPError,
    AgentParseError,
    AgentTimeout,
    AuthError,
    CaseSkipped,
    ConfigError,
    GlowCareAdapter,
)
from evalkit.adapters.glowcare import (
    HISTORY_UNSUPPORTED,
    READ_TIMEOUT_ENV,
    READ_TIMEOUT_S,
    parse_retry_after,
)
from evalkit.schemas import FailureType, Route

from .agent_double import HEALTH, SENTINEL_KEY, FakeAgent, Reply, fixture, make_case, ok, raises

Q = "What is your shipping policy?"
CASE = make_case("k1", Q)


async def answer(agent: FakeAgent) -> tuple[int, Route | None]:
    async with agent.adapter() as adapter:
        reply = await adapter.answer(CASE)
    return reply.attempts, reply.response.route


# --------------------------------------------------------------------------- success


async def test_success_sends_eval_mode_and_key() -> None:
    agent = FakeAgent().on(Q, ok("knowledge"))
    assert await answer(agent) == (1, Route.KNOWLEDGE)
    assert agent.bodies == [{"message": Q, "eval_mode": True}]
    assert agent.api_keys == [SENTINEL_KEY]


async def test_ok_false_is_returned_not_raised() -> None:
    agent = FakeAgent().on(Q, ok("error"))
    async with agent.adapter() as adapter:
        reply = await adapter.answer(CASE)
    assert reply.response.is_error and reply.response.error == "TimeoutError: simulated"


# --------------------------------------------------------------------------- retries


async def test_429_then_success_honours_retry_after() -> None:
    agent = FakeAgent().on(Q, Reply(429, text="slow down", headers={"Retry-After": "3"}), ok())
    assert await answer(agent) == (2, Route.KNOWLEDGE)
    assert agent.sleeps == [3.0]


async def test_429_without_retry_after_uses_backoff() -> None:
    agent = FakeAgent().on(Q, Reply(429, text="slow down"), ok())
    assert (await answer(agent))[0] == 2
    assert 1.0 <= agent.sleeps[0] <= 2.0


async def test_429_with_retry_after_over_cap_fails_without_waiting() -> None:
    agent = FakeAgent().on(Q, Reply(429, text="", headers={"Retry-After": "120"}), ok())
    with pytest.raises(AgentHTTPError, match="exceeds the 30s cap") as exc:
        await answer(agent)
    assert exc.value.attempts == 1 and exc.value.status == 429
    assert agent.sleeps == [] and agent.calls(Q) == 1


async def test_500_three_times_is_http_error_after_3_attempts() -> None:
    agent = FakeAgent().on(Q, Reply(500, text="boom"))
    with pytest.raises(AgentHTTPError, match="HTTP 500: boom") as exc:
        await answer(agent)
    assert exc.value.attempts == 3 and exc.value.failure_type is FailureType.HTTP_ERROR
    assert agent.calls(Q) == 3
    first, second = agent.sleeps  # exponential with jitter: 1+[0,1], then 2+[0,1]
    assert 1.0 <= first <= 2.0 and 2.0 <= second <= 3.0


async def test_read_timeout_is_not_retried() -> None:
    agent = FakeAgent().on(Q, raises(httpx.ReadTimeout))
    with pytest.raises(AgentTimeout, match="no response within 180s") as exc:
        await answer(agent)
    assert exc.value.attempts == 1 and agent.calls(Q) == 1 and agent.sleeps == []


async def test_connection_refused_is_retried_then_connection_error() -> None:
    agent = FakeAgent().on(Q, raises(httpx.ConnectError, "connection refused"))
    with pytest.raises(AgentConnectionError, match="connection refused") as exc:
        await answer(agent)
    assert exc.value.attempts == 3 and agent.calls(Q) == 3


@pytest.mark.parametrize("exc_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError])
async def test_connection_problem_then_success(exc_type: type[Exception]) -> None:
    agent = FakeAgent().on(Q, raises(exc_type), ok())
    assert (await answer(agent))[0] == 2


@pytest.mark.parametrize("status", [400, 404, 422])
async def test_other_4xx_is_not_retried(status: int) -> None:
    agent = FakeAgent().on(Q, Reply(status, text="nope"))
    with pytest.raises(AgentHTTPError, match=f"HTTP {status}") as exc:
        await answer(agent)
    assert exc.value.attempts == 1 and agent.calls(Q) == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_raises_auth_error(status: int) -> None:
    agent = FakeAgent().on(Q, Reply(status, text="bad key"))
    with pytest.raises(AuthError, match=f"HTTP {status}") as exc:
        await answer(agent)
    assert SENTINEL_KEY not in str(exc.value) and agent.calls(Q) == 1


# --------------------------------------------------------------------------- parsing


async def test_malformed_json_is_parse_error() -> None:
    agent = FakeAgent().on(Q, Reply(text="<html>oops</html>"))
    with pytest.raises(AgentParseError, match="oops") as exc:
        await answer(agent)
    assert exc.value.failure_type is FailureType.PARSE_ERROR


async def test_wrong_shape_is_parse_error_naming_the_field() -> None:
    agent = FakeAgent().on(Q, Reply(json=fixture("knowledge", route="billing")))
    with pytest.raises(AgentParseError, match="at route"):
        await answer(agent)


async def test_history_case_is_skipped_without_a_request() -> None:
    agent = FakeAgent()
    case = make_case("h1", Q, history=[{"role": "user", "content": "earlier"}])
    async with agent.adapter() as adapter:
        with pytest.raises(CaseSkipped, match=HISTORY_UNSUPPORTED) as exc:
            await adapter.answer(case)
    assert exc.value.attempts == 0 and agent.messages == []


# --------------------------------------------------------------------------- health


async def test_health_ok() -> None:
    agent = FakeAgent()
    async with agent.adapter() as adapter:
        assert await adapter.health() == HEALTH
        assert adapter.health_url == "http://agent.test/health"


@pytest.mark.parametrize(
    ("step", "error", "match"),
    [
        (raises(httpx.ConnectError), AgentConnectionError, "cannot connect"),
        (raises(httpx.ConnectTimeout), AgentConnectionError, "cannot connect"),
        (raises(httpx.ReadTimeout), AgentTimeout, "no /health response"),
        (Reply(500, text="down"), AgentHTTPError, "HTTP 500"),
        (Reply(text="not json"), AgentParseError, "non-JSON"),
        (Reply(json=["a"]), AgentParseError, "expected an object"),
    ],
)
async def test_health_failures(step: object, error: type[Exception], match: str) -> None:
    agent = FakeAgent()
    agent.health = step  # type: ignore[assignment]
    async with agent.adapter() as adapter:
        with pytest.raises(error, match=match):
            await adapter.health()


# --------------------------------------------------------------------------- config


def test_from_env_reports_missing_vars(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_URL", raising=False)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="missing AGENT_URL and AGENT_API_KEY"):
        GlowCareAdapter.from_env(tmp_path / "no.env")


async def test_from_env_reads_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_URL", raising=False)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        f"AGENT_URL=http://x.test/chat\nAGENT_API_KEY={SENTINEL_KEY}\n", encoding="utf-8"
    )
    adapter = GlowCareAdapter.from_env(env)
    try:
        assert adapter.url == "http://x.test/chat"
    finally:
        await adapter.aclose()
        monkeypatch.delenv("AGENT_URL", raising=False)  # load_dotenv wrote them to os.environ
        monkeypatch.delenv("AGENT_API_KEY", raising=False)


@pytest.fixture
def agent_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("AGENT_URL", "http://x.test/chat")
    monkeypatch.setenv("AGENT_API_KEY", SENTINEL_KEY)
    monkeypatch.delenv(READ_TIMEOUT_ENV, raising=False)
    return monkeypatch


@pytest.mark.parametrize(
    ("env_value", "arg", "expected"),
    [
        (None, None, READ_TIMEOUT_S),
        ("", None, READ_TIMEOUT_S),
        (" 240 ", None, 240.0),
        ("240", 30.0, 30.0),
        (None, 45.0, 45.0),
    ],
)
async def test_read_timeout_argument_then_env_then_default(
    agent_env: pytest.MonkeyPatch,
    tmp_path: Path,
    env_value: str | None,
    arg: float | None,
    expected: float,
) -> None:
    if env_value is not None:
        agent_env.setenv(READ_TIMEOUT_ENV, env_value)
    adapter = GlowCareAdapter.from_env(tmp_path / "no.env", read_timeout=arg)
    try:
        assert adapter.read_timeout == expected
        assert adapter.settings()["read_timeout_s"] == expected
    finally:
        await adapter.aclose()


def test_default_read_timeout_is_180() -> None:
    assert READ_TIMEOUT_S == 180.0


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "nan", "inf"])
def test_bad_read_timeout_env_is_a_config_error(
    agent_env: pytest.MonkeyPatch, tmp_path: Path, bad: str
) -> None:
    agent_env.setenv(READ_TIMEOUT_ENV, bad)
    with pytest.raises(ConfigError, match=READ_TIMEOUT_ENV):
        GlowCareAdapter.from_env(tmp_path / "no.env")


@pytest.mark.parametrize(("url", "key", "match"), [("", "k", "AGENT_URL"), ("http://x", "", "KEY")])
def test_empty_settings_are_rejected(url: str, key: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        GlowCareAdapter(url, key)


async def test_key_is_hidden_from_repr_and_settings() -> None:
    adapter = FakeAgent().adapter()
    assert SENTINEL_KEY not in repr(adapter) and "<redacted>" in repr(adapter)
    assert SENTINEL_KEY not in str(adapter.settings())
    assert adapter.settings()["eval_mode"] is True
    await adapter.aclose()


# --------------------------------------------------------------------------- Retry-After


NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("3", 3.0),
        (" 2.5 ", 2.5),
        ("-4", 0.0),
        ("soon", None),
        ("inf", None),
        (format_datetime(NOW + timedelta(seconds=7), usegmt=True), 7.0),
        (format_datetime(NOW - timedelta(seconds=7), usegmt=True), 0.0),
    ],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value, now=NOW) == expected


def test_parse_retry_after_defaults_to_current_time() -> None:
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=60), usegmt=True)
    assert 50 < (parse_retry_after(future) or 0) <= 60
