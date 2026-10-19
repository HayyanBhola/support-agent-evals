"""HTTP adapter for the GlowCare support agent (``POST /chat``, ``GET /health``)."""

import asyncio
import math
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from pydantic import ValidationError
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from evalkit.adapters.base import (
    AdapterError,
    AgentAdapter,
    AgentConnectionError,
    AgentHTTPError,
    AgentParseError,
    AgentReply,
    AgentTimeout,
    AuthError,
    CaseSkipped,
    ConfigError,
)
from evalkit.schemas import AgentResponse, TestCase

CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 180.0  # Day 4: a knowledge call took 116.8 s, too close to the old 120 s
READ_TIMEOUT_ENV = "AGENT_READ_TIMEOUT_S"
MAX_RETRIES = 2
BACKOFF_INITIAL_S = 1.0
BACKOFF_MAX_S = 10.0
RETRY_AFTER_CAP_S = 30.0
HISTORY_UNSUPPORTED = "history not supported by eval_mode API"
BODY_PREVIEW_CHARS = 300

Sleep = Callable[[float], Awaitable[None]]


class _Retryable(Exception):
    """Internal: a failure worth retrying. ``error`` is raised if retries run out."""

    def __init__(self, error: AdapterError, retry_after: float | None = None) -> None:
        super().__init__(str(error))
        self.error = error
        self.retry_after = retry_after


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date), else None."""
    if value is None:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _read_timeout_from_env() -> float:
    raw = os.environ.get(READ_TIMEOUT_ENV, "").strip()
    if not raw:
        return READ_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value > 0):
        raise ConfigError(f"{READ_TIMEOUT_ENV} must be a positive number of seconds, got {raw!r}")
    return value


class GlowCareAdapter(AgentAdapter):
    """Calls the GlowCare agent with ``eval_mode: true`` on every request.

    The API key is only ever placed in the ``X-API-Key`` header; it is never logged, stored,
    or shown in ``repr``.
    """

    name = "glowcare"

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        read_timeout: float = READ_TIMEOUT_S,
        max_retries: int = MAX_RETRIES,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not url:
            raise ConfigError("AGENT_URL is empty")
        if not api_key:
            raise ConfigError("AGENT_API_KEY is empty")
        self.url = url
        self.health_url = str(httpx.URL(url).copy_with(path="/health", query=None))
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self._backoff = wait_exponential_jitter(initial=BACKOFF_INITIAL_S, max=BACKOFF_MAX_S)
        self._client = httpx.AsyncClient(
            headers={"X-API-Key": api_key},
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            transport=transport,
        )

    @classmethod
    def from_env(
        cls, env_file: str | Path = ".env", *, read_timeout: float | None = None, **kwargs: Any
    ) -> "GlowCareAdapter":
        """Build from AGENT_URL / AGENT_API_KEY (environment first, then ``env_file``).

        The read timeout is ``read_timeout`` if given, else AGENT_READ_TIMEOUT_S, else
        READ_TIMEOUT_S.
        """
        load_dotenv(env_file)
        missing = [k for k in ("AGENT_URL", "AGENT_API_KEY") if not os.environ.get(k)]
        if missing:
            raise ConfigError(f"missing {' and '.join(missing)}: set it in .env (see .env.example)")
        if read_timeout is None:
            read_timeout = _read_timeout_from_env()
        return cls(
            os.environ["AGENT_URL"],
            os.environ["AGENT_API_KEY"],
            read_timeout=read_timeout,
            **kwargs,
        )

    def __repr__(self) -> str:
        return f"GlowCareAdapter(url={self.url!r}, api_key=<redacted>)"

    def settings(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "eval_mode": True,
            "connect_timeout_s": self.connect_timeout,
            "read_timeout_s": self.read_timeout,
            "max_retries": self.max_retries,
        }

    async def aclose(self) -> None:
        await self._client.aclose()

    # ----------------------------------------------------------------------- health

    async def health(self) -> dict[str, Any]:
        try:
            resp = await self._client.get(self.health_url)
        except httpx.ConnectTimeout as e:
            raise AgentConnectionError(f"cannot connect to {self.health_url}: {e!r}") from e
        except httpx.TimeoutException as e:
            raise AgentTimeout(f"no /health response from {self.health_url}") from e
        except httpx.TransportError as e:
            raise AgentConnectionError(f"cannot connect to {self.health_url}: {e!r}") from e
        if resp.status_code >= 400:
            raise AgentHTTPError(
                f"/health returned HTTP {resp.status_code}", status=resp.status_code
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise AgentParseError(
                f"/health returned non-JSON: {resp.text[:BODY_PREVIEW_CHARS]!r}"
            ) from e
        if not isinstance(data, dict):
            raise AgentParseError(f"/health returned {type(data).__name__}, expected an object")
        return data

    # ----------------------------------------------------------------------- chat

    async def answer(self, case: TestCase) -> AgentReply:
        if case.history:
            raise CaseSkipped(HISTORY_UNSUPPORTED)
        return await self.send(case.question)

    async def send(self, message: str) -> AgentReply:
        """POST one message with eval_mode, retrying transient failures."""
        payload = {"message": message, "eval_mode": True}
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.max_retries + 1),
            retry=retry_if_exception_type(_Retryable),
            wait=self._wait,
            sleep=self._sleep,
            reraise=True,
        )
        attempts = 0
        try:
            async for attempt in retrying:
                with attempt:
                    attempts = attempt.retry_state.attempt_number
                    resp = await self._post_once(payload, attempts)
        except _Retryable as e:
            raise e.error from None
        return AgentReply(self._parse(resp, attempts), attempts)

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, _Retryable) and exc.retry_after is not None:
            return exc.retry_after
        return float(self._backoff(state))

    async def _post_once(self, payload: dict[str, Any], attempt: int) -> httpx.Response:
        try:
            resp = await self._client.post(self.url, json=payload)
        except httpx.ConnectTimeout as e:
            raise _Retryable(self._connection_error(e, attempt)) from e
        except httpx.TimeoutException as e:
            raise AgentTimeout(
                f"no response within {self.read_timeout:g}s", attempts=attempt
            ) from e
        except httpx.TransportError as e:
            raise _Retryable(self._connection_error(e, attempt)) from e

        status = resp.status_code
        if status in (401, 403):
            raise AuthError(f"agent rejected the API key (HTTP {status}); check AGENT_API_KEY")
        if status < 400:
            return resp
        message = f"HTTP {status}: {resp.text[:BODY_PREVIEW_CHARS]}"
        if status != 429 and status < 500:
            raise AgentHTTPError(message, status=status, attempts=attempt)
        retry_after = parse_retry_after(resp.headers.get("Retry-After"))
        if retry_after is not None and retry_after > RETRY_AFTER_CAP_S:
            raise AgentHTTPError(
                f"{message} (Retry-After {retry_after:g}s exceeds the "
                f"{RETRY_AFTER_CAP_S:g}s cap; not retried)",
                status=status,
                attempts=attempt,
            )
        raise _Retryable(AgentHTTPError(message, status=status, attempts=attempt), retry_after)

    def _connection_error(self, e: Exception, attempt: int) -> AgentConnectionError:
        return AgentConnectionError(f"cannot connect to {self.url}: {e!r}", attempts=attempt)

    @staticmethod
    def _parse(resp: httpx.Response, attempts: int) -> AgentResponse:
        try:
            return AgentResponse.model_validate_json(resp.content)
        except ValidationError as e:
            first = e.errors()[0]
            where = ".".join(str(p) for p in first["loc"]) or "body"
            raise AgentParseError(
                f"invalid agent response at {where}: {first['msg']}; "
                f"body starts {resp.text[:BODY_PREVIEW_CHARS]!r}",
                attempts=attempts,
            ) from e
