"""LLM access for test-case generation: pluggable providers, a cache, and a fake for tests.

Providers implement ``LLMClient`` (one ``complete`` call). Two real ones share one HTTP,
retry and backoff core:

* ``OpenAIClient``: Chat Completions with strict structured outputs (a JSON schema built
  from the Pydantic output model), ``reasoning_effort`` and ``max_completion_tokens``;
  reasoning tokens are counted separately (they are billed as output);
* ``GroqClient``: Groq's OpenAI-compatible API in JSON mode.

Every ``GenLLM.ask`` call:

* sends a versioned prompt file (``prompts/<name>_v<N>.md``) as the system message and the
  call's inputs as canonical JSON in the user message;
* parses the reply into a Pydantic model. Bad JSON gets one retry (the model is shown its
  error); a second failure raises ``BadOutput`` and the caller rejects the case;
* is cached in ``.cache/gen/`` under a key covering the prompt name and file hash, the
  provider, model and its parameters, the output schema, the seed, the inputs and the
  attempt number, so a rerun replays the same outputs for free;
* adds its tokens to ``Usage`` per model and per generator (cached calls are kept apart).

429s: per-minute limits are waited out and retried (tenacity, honouring Retry-After or the
"try again in" hint); a daily limit, an exhausted OpenAI quota/billing, or a wait over
RATE_WAIT_MAX_S raises ``QuotaExhausted`` so the run stops cleanly. Everything finished so
far is already in the cache, so rerunning the same command continues from there.
"""

import asyncio
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Final, Literal, Protocol, TypeVar

import diskcache
import httpx
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from evalkit.adapters.glowcare import parse_retry_after
from evalkit.generation.prompts import Prompt, prompt_name_of
from evalkit.hashing import canonical_json, stable_hash

Provider = Literal["openai", "groq"]
PROVIDERS: Final[tuple[Provider, ...]] = ("openai", "groq")
OPENAI_URL: Final = "https://api.openai.com/v1/chat/completions"
GROQ_URL: Final = "https://api.groq.com/openai/v1/chat/completions"
API_KEY_ENV: Final[Mapping[Provider, str]] = {"openai": "OPENAI_API_KEY", "groq": "GROQ_API_KEY"}
DEFAULT_MODEL: Final[Mapping[Provider, str]] = {
    "openai": "gpt-5-mini",
    "groq": "openai/gpt-oss-120b",
}
DEFAULT_CACHE_DIR: Final = Path(".cache") / "gen"
CACHE_FORMAT: Final = 2  # bump to invalidate every cached generation call
MAX_RETRIES: Final = 8
RATE_WAIT_DEFAULT_S: Final = 10.0
RATE_WAIT_MARGIN_S: Final = 0.5
# A "please wait" longer than this is a daily limit in all but name: stop instead of waiting.
RATE_WAIT_MAX_S: Final = 300.0
BACKOFF_INITIAL_S: Final = 2.0
BACKOFF_MAX_S: Final = 30.0
BODY_PREVIEW_CHARS: Final = 300
CHARS_PER_TOKEN: Final = 4  # rough, for estimates only

M = TypeVar("M", bound=BaseModel)
Sleep = Callable[[float], Awaitable[None]]


# --------------------------------------------------------------------------- models and prices


@dataclass(frozen=True)
class ModelConfig:
    """One model on one provider and the parameters sent with every call (None = omitted)."""

    name: str
    provider: Provider = "openai"
    temperature: float | None = None  # GPT-5 models only accept the default
    max_tokens: int = 8192  # OpenAI: max_completion_tokens, which includes reasoning tokens
    reasoning_effort: str | None = "low"

    def params(self) -> dict[str, Any]:
        out: dict[str, Any] = {"max_tokens": self.max_tokens}
        if self.temperature is not None:
            out["temperature"] = self.temperature
        if self.reasoning_effort is not None:
            out["reasoning_effort"] = self.reasoning_effort
        return out


def default_model_config(
    provider: Provider, name: str | None = None, reasoning_effort: str | None = "low"
) -> ModelConfig:
    """Sensible parameters per provider: OpenAI reasoning models take no temperature; on Groq
    only the gpt-oss models accept reasoning_effort."""
    name = name or DEFAULT_MODEL[provider]
    if provider == "openai":
        return ModelConfig(name, "openai", reasoning_effort=reasoning_effort)
    effort = reasoning_effort if name.startswith("openai/gpt-oss") else None
    return ModelConfig(name, "groq", temperature=0.8, max_tokens=4096, reasoning_effort=effort)


@dataclass(frozen=True)
class Price:
    """USD per million tokens. Reasoning tokens are billed as output."""

    input_per_m: float
    output_per_m: float


# APPROXIMATE, from public price lists; not checked against a bill. gpt-5-mini: the figures
# agreed on Day 5. The Groq free tier is not billed at all.
APPROX_PRICES: Final[Mapping[str, Price]] = {
    "gpt-5-mini": Price(0.125, 1.00),
    "openai/gpt-oss-120b": Price(0.15, 0.60),
    "openai/gpt-oss-20b": Price(0.075, 0.30),
}


# --------------------------------------------------------------------------- errors


class GenerationError(Exception):
    """Base class for generation-side failures."""


class BadOutput(GenerationError):
    """The model's reply could not be parsed into the expected model (after the retry)."""


class BudgetSkip(BadOutput):
    """Smoke test: this generator's call budget is used up; the call was not made."""


class QuotaExhausted(GenerationError):
    """A daily (or very long) rate limit, or no quota left: stop the run and resume later."""

    def __init__(self, model: str, message: str, wait_s: float | None = None) -> None:
        super().__init__(message)
        self.model = model
        self.wait_s = wait_s


class LLMRequestError(GenerationError):
    """A non-retryable API error (bad request, auth, unknown model, ...)."""


# --------------------------------------------------------------------------- usage


@dataclass
class ModelUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0  # includes reasoning tokens
    reasoning_tokens: int = 0
    cached_calls: int = 0
    cached_prompt_tokens: int = 0
    cached_completion_tokens: int = 0
    cached_reasoning_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cached_tokens(self) -> int:
        return self.cached_prompt_tokens + self.cached_completion_tokens

    def add(self, c: "Completion", *, cached: bool) -> None:
        if cached:
            self.cached_calls += 1
            self.cached_prompt_tokens += c.prompt_tokens
            self.cached_completion_tokens += c.completion_tokens
            self.cached_reasoning_tokens += c.reasoning_tokens
        else:
            self.calls += 1
            self.prompt_tokens += c.prompt_tokens
            self.completion_tokens += c.completion_tokens
            self.reasoning_tokens += c.reasoning_tokens

    def cost(self, model: str, *, include_cached: bool = False) -> float | None:
        """Approximate USD; None if the model's price is unknown."""
        price = APPROX_PRICES.get(model)
        if price is None:
            return None
        p, c = self.prompt_tokens, self.completion_tokens
        if include_cached:
            p, c = p + self.cached_prompt_tokens, c + self.cached_completion_tokens
        return (p * price.input_per_m + c * price.output_per_m) / 1_000_000


@dataclass
class Usage:
    """Token totals per model and per (generator, model) for one run."""

    by_model: dict[str, ModelUsage] = field(default_factory=dict)
    by_generator: dict[tuple[str, str], ModelUsage] = field(default_factory=dict)

    def add(self, model: str, c: "Completion", *, cached: bool, generator: str = "?") -> None:
        self.by_model.setdefault(model, ModelUsage()).add(c, cached=cached)
        self.by_generator.setdefault((generator, model), ModelUsage()).add(c, cached=cached)

    def approx_cost(self, model: str, *, include_cached: bool = False) -> float | None:
        u = self.by_model.get(model)
        return None if u is None else u.cost(model, include_cached=include_cached)

    def total_cost(self) -> float:
        """Approximate USD of the fresh calls whose price is known."""
        return sum(u.cost(m) or 0.0 for m, u in self.by_model.items())


# --------------------------------------------------------------------------- clients


@dataclass(frozen=True)
class Completion:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0


Messages = list[dict[str, str]]


class LLMClient(Protocol):
    """One chat completion whose content should be JSON matching ``schema``."""

    async def complete(
        self, model: ModelConfig, messages: Messages, seed: int, schema: type[BaseModel]
    ) -> Completion: ...


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """``model``'s JSON schema in the form OpenAI's strict structured outputs accept: every
    object closed and all its properties required (optional fields stay nullable), without
    keywords strict mode rejects (defaults, string lengths). Pydantic still validates the
    reply, so dropped constraints are checked there."""
    schema = model.model_json_schema()

    def fix(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                fix(item)
            return
        if not isinstance(node, dict):
            return
        for key in ("default", "title", "minLength", "maxLength"):
            node.pop(key, None)
        if node.get("type") == "object":
            props = node.setdefault("properties", {})
            node["required"] = list(props)
            node["additionalProperties"] = False
            for prop in props.values():  # property NAMES are data, never popped
                fix(prop)
        for key, value in node.items():
            if key != "properties":
                fix(value)

    fix(schema)
    return schema


_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?$")


def parse_duration(text: str | None) -> float | None:
    """Seconds from "1h2m3.5s", "6m0s", "20ms" or "1.5s" (OpenAI rate-limit headers)."""
    if not text:
        return None
    text = text.strip()
    if text.endswith("ms") and text[:-2].replace(".", "", 1).isdigit():
        return float(text[:-2]) / 1000
    m = _DURATION.fullmatch(text)
    if not m or not any(m.groups()):
        return None
    h, mins, s = m.groups()
    return int(h or 0) * 3600 + int(mins or 0) * 60 + float(s or 0)


def parse_wait(message: str) -> float | None:
    """Seconds from a "Please try again in 1m2.5s" / "in 750ms" message; None if absent."""
    # "ms" first: the h/m/s branch can match empty, which would hide a "750ms" hint.
    m = re.search(r"try again in ([\d.]+ms|(?:\d+h)?(?:\d+m(?!s))?(?:[\d.]+s)?)\b", message)
    if not m or not m.group(1):
        return None
    return parse_duration(m.group(1))


_BILLING = ("no credits remaining", "add credits", "billing", "exceeded your current quota")


def is_billing_error(message: str) -> bool:
    """No credits / quota left on the account (OpenAI): adding credits fixes it, waiting
    doesn't. OpenAI's "no credits remaining" 429 carries no insufficient_quota code (Day 22)."""
    lowered = message.lower()
    return any(marker in lowered for marker in _BILLING)


def is_daily_limit(message: str) -> bool:
    lowered = message.lower()
    return "per day" in lowered or "(tpd)" in lowered or "(rpd)" in lowered


class _Retryable(Exception):
    """Internal: a failure worth retrying. ``error`` is raised if retries run out."""

    def __init__(self, error: GenerationError, wait_s: float | None = None) -> None:
        super().__init__(str(error))
        self.error = error
        self.wait_s = wait_s


class _ChatClient:
    """The HTTP, retry and backoff core shared by the OpenAI-compatible providers."""

    provider: ClassVar[Provider]
    default_url: ClassVar[str]

    def __init__(
        self,
        api_key: str,
        *,
        url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        max_retries: int = MAX_RETRIES,
        timeout_s: float = 180.0,
    ) -> None:
        if not api_key:
            env = API_KEY_ENV[self.provider]
            raise LLMRequestError(f"{env} is empty: set it in .env (see .env.example)")
        self.url = url or self.default_url
        self._sleep = sleep
        self.max_retries = max_retries
        self._backoff = wait_exponential_jitter(multiplier=BACKOFF_INITIAL_S, max=BACKOFF_MAX_S)
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(url={self.url!r}, api_key=<redacted>)"

    async def aclose(self) -> None:
        await self._client.aclose()

    def payload(
        self, model: ModelConfig, messages: Messages, seed: int, schema: type[BaseModel]
    ) -> dict[str, Any]:
        raise NotImplementedError  # pragma: no cover - every provider overrides it

    def rate_limit_wait(self, resp: httpx.Response, message: str) -> float | None:
        """Seconds the provider asks us to wait after a 429 (None = unknown)."""
        wait = parse_retry_after(resp.headers.get("retry-after"))
        return wait if wait is not None else parse_wait(message)

    def is_quota_error(self, resp: httpx.Response, message: str) -> bool:
        """A 429 that waiting a minute won't fix."""
        return is_daily_limit(message)

    def bad_request_completion(self, resp: httpx.Response) -> Completion | None:
        """A 400 that is really the model's bad output (retried as bad JSON), else None."""
        return None

    async def complete(
        self, model: ModelConfig, messages: Messages, seed: int, schema: type[BaseModel]
    ) -> Completion:
        payload = self.payload(model, messages, seed, schema)
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.max_retries + 1),
            retry=retry_if_exception_type(_Retryable),
            wait=self._wait,
            sleep=self._sleep,
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    completion = await self._post_once(model.name, payload)
        except _Retryable as e:
            raise e.error from None
        return completion

    def _wait(self, state: RetryCallState) -> float:
        """A rate limit's own hint (plus a margin) if it gave one, else jittered backoff."""
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, _Retryable) and exc.wait_s is not None:
            return exc.wait_s + RATE_WAIT_MARGIN_S
        return float(self._backoff(state))

    async def _post_once(self, model: str, payload: dict[str, Any]) -> Completion:
        name = type(self).__name__.removesuffix("Client")
        try:
            resp = await self._client.post(self.url, json=payload)
        except httpx.TransportError as e:
            raise _Retryable(LLMRequestError(f"cannot reach {name}: {e!r}")) from e
        status = resp.status_code
        if status == 200:
            return self._parse_ok(resp)
        message = _error_message(resp)
        if status == 429:
            wait = self.rate_limit_wait(resp, message)
            if self.is_quota_error(resp, message) or (wait is not None and wait > RATE_WAIT_MAX_S):
                billing = (
                    "quota/billing exhausted (add credits): " if is_billing_error(message) else ""
                )
                raise QuotaExhausted(model, billing + message, wait)
            raise _Retryable(
                QuotaExhausted(model, f"still rate limited after retries: {message}", wait),
                RATE_WAIT_DEFAULT_S if wait is None else wait,
            )
        if status >= 500:
            raise _Retryable(LLMRequestError(f"{name} HTTP {status} for {model}: {message}"))
        if status == 400 and (bad := self.bad_request_completion(resp)) is not None:
            return bad
        raise LLMRequestError(f"{name} HTTP {status} for {model}: {message}")

    @staticmethod
    def _parse_ok(resp: httpx.Response) -> Completion:
        try:
            data = resp.json()
            content = data["choices"][0]["message"].get("content") or ""
            usage = data.get("usage") or {}
            details = usage.get("completion_tokens_details") or {}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            raise LLMRequestError(f"unexpected response: {resp.text[:BODY_PREVIEW_CHARS]!r}") from e
        return Completion(
            content=content,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
        )


class OpenAIClient(_ChatClient):
    """OpenAI Chat Completions with strict structured outputs.

    No ``seed``: reasoning models aren't reproducible anyway; reruns replay from the cache.
    """

    provider = "openai"
    default_url = OPENAI_URL

    def payload(
        self, model: ModelConfig, messages: Messages, seed: int, schema: type[BaseModel]
    ) -> dict[str, Any]:
        params = model.params()
        out: dict[str, Any] = {
            "model": model.name,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": strict_json_schema(schema),
                    "strict": True,
                },
            },
            "max_completion_tokens": params.pop("max_tokens"),
        }
        return out | params

    def rate_limit_wait(self, resp: httpx.Response, message: str) -> float | None:
        ms = resp.headers.get("retry-after-ms")
        if ms is not None:
            try:
                return max(0.0, float(ms) / 1000)
            except ValueError:
                pass
        # explicit base call, not super(): OpenAIJudgeClient (a GroqClient) borrows these two
        # methods unbound, and super() fails for an object that isn't an OpenAIClient (Day 22)
        return _ChatClient.rate_limit_wait(self, resp, message)

    def is_quota_error(self, resp: httpx.Response, message: str) -> bool:
        return (
            _error_code(resp) == "insufficient_quota"
            or is_billing_error(message)
            or _ChatClient.is_quota_error(self, resp, message)
        )


class GroqClient(_ChatClient):
    """Groq's OpenAI-compatible chat API in JSON mode (``json_object``)."""

    provider = "groq"
    default_url = GROQ_URL

    def payload(
        self, model: ModelConfig, messages: Messages, seed: int, schema: type[BaseModel]
    ) -> dict[str, Any]:
        return {
            "model": model.name,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "seed": seed,
            **model.params(),
        }

    def bad_request_completion(self, resp: httpx.Response) -> Completion | None:
        if _error_code(resp) == "json_validate_failed":
            # Groq's JSON mode refused the model's output: treat it as bad JSON.
            return Completion(content=str(_error_body(resp).get("failed_generation") or ""))
        return None


CLIENTS: Final[Mapping[Provider, type[_ChatClient]]] = {
    "openai": OpenAIClient,
    "groq": GroqClient,
}


def _error_body(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    err = data.get("error") if isinstance(data, dict) else None
    return err if isinstance(err, dict) else {}


def _error_message(resp: httpx.Response) -> str:
    return str(_error_body(resp).get("message") or resp.text[:BODY_PREVIEW_CHARS])


def _error_code(resp: httpx.Response) -> str | None:
    code = _error_body(resp).get("code")
    return str(code) if code is not None else None


Responder = Callable[[str, dict[str, Any]], Any]


class FakeLLM:
    """Scripted replies for tests: ``responder(prompt_name, inputs)`` returns a dict (sent as
    JSON), a str (sent as is) or raises. Every call is recorded in ``calls``."""

    def __init__(self, responder: Responder, *, tokens: tuple[int, int, int] = (100, 20, 0)):
        self.responder = responder
        self.tokens = tokens
        self.calls: list[tuple[str, dict[str, Any], int]] = []
        self.schemas: list[str] = []

    async def complete(
        self, model: ModelConfig, messages: Messages, seed: int, schema: type[BaseModel]
    ) -> Completion:
        prompt_name = prompt_name_of(messages[0]["content"])
        inputs = json.loads(messages[1]["content"])
        attempt = len(messages) // 2  # 2 messages on the first try, 4 on the retry
        self.calls.append((prompt_name, inputs, attempt))
        self.schemas.append(schema.__name__)
        reply = self.responder(prompt_name, inputs | {"_attempt": attempt})
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return Completion(content, *self.tokens)


# --------------------------------------------------------------------------- client


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@dataclass
class Estimate:
    """Token estimate per (generator, model), filled by ``GenLLM`` in estimate mode."""

    calls: dict[tuple[str, str], int] = field(default_factory=dict)
    input_tokens: dict[tuple[str, str], int] = field(default_factory=dict)
    output_tokens: dict[tuple[str, str], int] = field(default_factory=dict)
    cached_calls: dict[tuple[str, str], int] = field(default_factory=dict)

    def add(self, key: tuple[str, str], inp: int, out: int, *, cached: bool) -> None:
        if cached:
            self.cached_calls[key] = self.cached_calls.get(key, 0) + 1
            return
        self.calls[key] = self.calls.get(key, 0) + 1
        self.input_tokens[key] = self.input_tokens.get(key, 0) + inp
        self.output_tokens[key] = self.output_tokens.get(key, 0) + out

    def total_calls(self, generator: str) -> int:
        """Calls the full run makes for ``generator`` (fresh + already cached)."""
        return sum(
            n
            for counts in (self.calls, self.cached_calls)
            for (g, _), n in counts.items()
            if g == generator
        )


class GenLLM:
    """Cached, JSON-parsing, concurrency-limited access to an ``LLMClient``."""

    def __init__(
        self,
        client: LLMClient,
        *,
        seed: int,
        cache_dir: Path | None = DEFAULT_CACHE_DIR,
        concurrency: int = 2,
        estimate: Estimate | None = None,
        call_budget: Mapping[str, int] | None = None,
    ) -> None:
        self.client = client
        self.seed = seed
        self.usage = Usage()
        self.estimate = estimate
        self.call_budget = call_budget  # smoke test: max asks per generator (missing = 0)
        self._asks: dict[str, int] = {}
        self._sem = asyncio.Semaphore(concurrency)
        self._cache = diskcache.Cache(str(cache_dir)) if cache_dir is not None else None

    def close(self) -> None:
        if self._cache is not None:
            self._cache.close()

    def _key(
        self,
        prompt: Prompt,
        model: ModelConfig,
        schema: type[BaseModel],
        messages: Messages,
        attempt: int,
    ) -> str:
        return stable_hash(
            {
                "format": CACHE_FORMAT,
                "prompt": prompt.name,
                "prompt_sha256": prompt.sha256,
                "provider": model.provider,
                "model": model.name,
                "params": model.params(),
                "schema": schema.model_json_schema(),
                "seed": self.seed,
                "messages": messages,
                "attempt": attempt,
            }
        )

    async def ask(
        self,
        prompt: Prompt,
        inputs: Mapping[str, Any],
        out: type[M],
        model: ModelConfig,
        *,
        generator: str,
        est_output_tokens: int,
    ) -> M:
        """One JSON answer parsed into ``out``; raises BadOutput after one failed retry."""
        if self.call_budget is not None:
            if self._asks.get(generator, 0) >= self.call_budget.get(generator, 0):
                raise BudgetSkip("smoke test: call budget used up")
            self._asks[generator] = self._asks.get(generator, 0) + 1
        messages: Messages = [
            {"role": "system", "content": prompt.text},
            {"role": "user", "content": canonical_json(dict(inputs))},
        ]
        error = bad_content = ""
        for attempt in (0, 1):
            if attempt:
                messages = [
                    *messages[:2],
                    {"role": "assistant", "content": bad_content[:4000]},
                    {
                        "role": "user",
                        "content": "Your reply was not valid: "
                        f"{error}. Reply again with only the corrected JSON object.",
                    },
                ]
            key = self._key(prompt, model, out, messages, attempt)
            if self.estimate is not None:
                cached = self._cache is not None and key in self._cache
                inp = sum(estimate_tokens(m["content"]) for m in messages)
                self.estimate.add((generator, model.name), inp, est_output_tokens, cached=cached)
                raise BadOutput("estimate only: no call made")
            completion = await self._complete(key, model, out, messages, generator)
            try:
                return out.model_validate_json(completion.content)
            except ValidationError as e:
                first = e.errors()[0]
                where = ".".join(str(p) for p in first["loc"]) or "reply"
                error = f"{where}: {first['msg']}"
                bad_content = completion.content
        raise BadOutput(f"invalid JSON after one retry ({error})")

    async def _complete(
        self,
        key: str,
        model: ModelConfig,
        schema: type[BaseModel],
        messages: Messages,
        generator: str,
    ) -> Completion:
        if self._cache is not None:
            hit = self._cache.get(key)
            if isinstance(hit, dict):
                c = Completion(**hit)
                self.usage.add(model.name, c, cached=True, generator=generator)
                return c
        async with self._sem:
            c = await self.client.complete(model, messages, self.seed, schema)
        self.usage.add(model.name, c, cached=False, generator=generator)
        if self._cache is not None:
            self._cache.set(
                key,
                {
                    "content": c.content,
                    "prompt_tokens": c.prompt_tokens,
                    "completion_tokens": c.completion_tokens,
                    "reasoning_tokens": c.reasoning_tokens,
                },
            )
        return c
