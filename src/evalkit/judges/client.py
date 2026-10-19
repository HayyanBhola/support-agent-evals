"""The judge's Groq client (Day 16): the generator's GroqClient (tenacity backoff on 429/5xx,
Retry-After, usage counting) plus what a judge needs.

* **Thinking off.** Qwen3 models can emit ``<think>...</think>``. The request first asks for
  ``reasoning_effort: "none"``; if Groq rejects that for the model, it falls back to
  ``reasoning_format: "hidden"``. ``strip_thinking`` removes any think block that still
  arrives. ``thinking`` records which mode was used.
* **Rate limiter.** Groq returns ``x-ratelimit-remaining-tokens`` / ``-requests`` and their
  reset times on every response. Before the next call, ``RateLimiter.wait`` sleeps until
  the token window resets if the remaining tokens can't cover the call, and keeps at
  least ``60 / rpm`` seconds between calls.
* **Every HTTP request is counted** (``requests``) and appended to a usage ledger
  (``reports/judges/usage.jsonl``) that enforces a daily cap across commands.
"""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

import httpx
from pydantic import BaseModel

from evalkit.generation.llm import (
    APPROX_PRICES,
    OPENAI_URL,
    Completion,
    GroqClient,
    LLMRequestError,
    Messages,
    ModelConfig,
    OpenAIClient,
    Provider,
    parse_duration,
)

JUDGE_MODEL: Final = "qwen/qwen3.8-27b"  # the primary judge (Groq)
OPENAI_JUDGE_MODEL: Final = "gpt-5-mini"  # a second-opinion judge only (OpenAI made the cases)
THINKING_OPENAI: Final = "openai reasoning hidden, reasoning_effort=minimal"
USAGE_LEDGER: Final = Path("reports") / "judges" / "usage.jsonl"
DEFAULT_RPM: Final = 30
THINKING_NONE: Final = "reasoning_effort=none"
THINKING_HIDDEN: Final = "reasoning_format=hidden"
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_OPEN_THINK = re.compile(r"^\s*<think>.*", re.DOTALL | re.IGNORECASE)

Sleep = Callable[[float], Awaitable[None]]


def judge_model_config(name: str = JUDGE_MODEL, max_tokens: int = 300) -> ModelConfig:
    return ModelConfig(name, "groq", temperature=0.0, max_tokens=max_tokens, reasoning_effort=None)


def openai_judge_model_config(
    name: str = OPENAI_JUDGE_MODEL, max_tokens: int = 2000
) -> ModelConfig:
    """GPT-5 models accept only their default temperature, so none is sent (the lowest the
    model allows); minimal reasoning keeps it cheap and as close to deterministic as it gets.
    max_completion_tokens includes the hidden reasoning tokens."""
    return ModelConfig(name, "openai", temperature=None, max_tokens=max_tokens,
                       reasoning_effort="minimal")  # fmt: skip


def model_config_for(provider: Provider, name: str | None = None) -> ModelConfig:
    if provider == "openai":
        return openai_judge_model_config(name or OPENAI_JUDGE_MODEL)
    return judge_model_config(name or JUDGE_MODEL)


# gpt-5-mini output per verdict, hidden minimal reasoning included (an estimate for dry runs)
OPENAI_OUTPUT_ESTIMATE: Final = 400


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Approximate USD (APPROX_PRICES; reasoning tokens billed as output); None if unpriced."""
    price = APPROX_PRICES.get(model)
    if price is None:
        return None
    return (input_tokens * price.input_per_m + output_tokens * price.output_per_m) / 1_000_000


def strip_thinking(text: str) -> tuple[str, bool]:
    """``text`` without ``<think>...</think>`` blocks (and an unclosed leading one)."""
    stripped = _THINK.sub("", text)
    if "</think>" in stripped.lower():  # a closing tag without its opening one
        stripped = stripped[stripped.lower().rindex("</think>") + len("</think>") :]
    elif _OPEN_THINK.match(stripped):
        stripped = ""
    return stripped.strip(), stripped.strip() != text.strip()


@dataclass
class RateLimiter:
    rpm: int = DEFAULT_RPM
    clock: Callable[[], float] = time.monotonic
    remaining_tokens: int | None = None
    remaining_requests: int | None = None
    tokens_reset_s: float | None = None
    limit_tokens: int | None = None  # tokens per minute, from x-ratelimit-limit-tokens
    window: list[tuple[float, int]] = field(default_factory=list)  # (sent at, tokens reserved)
    limits: dict[str, str] = field(default_factory=dict)  # last seen x-ratelimit-* headers
    _last: float | None = None
    _seen_at: float = 0.0

    def observe(self, headers: httpx.Headers) -> None:
        self.limits = {k: v for k, v in headers.items() if k.lower().startswith("x-ratelimit")}

        def num(name: str) -> int | None:
            raw = headers.get(name)
            try:
                return int(float(raw)) if raw is not None else None
            except ValueError:
                return None

        self.remaining_tokens = num("x-ratelimit-remaining-tokens")
        self.limit_tokens = num("x-ratelimit-limit-tokens") or self.limit_tokens
        self.remaining_requests = num("x-ratelimit-remaining-requests")
        self.tokens_reset_s = parse_duration(headers.get("x-ratelimit-reset-tokens"))
        self._seen_at = self.clock()

    def delay(self, tokens_needed: int) -> float:
        """Seconds to wait before a call that needs about ``tokens_needed`` tokens."""
        now = self.clock()
        wait = 0.0
        if self._last is not None:
            wait = max(wait, 60.0 / self.rpm - (now - self._last))
        if (
            self.remaining_tokens is not None
            and self.remaining_tokens < tokens_needed
            and self.tokens_reset_s is not None
        ):
            wait = max(wait, self.tokens_reset_s - (now - self._seen_at))
        if self.limit_tokens is not None:
            # keep the tokens reserved in the last 60 s within the per-minute limit
            recent = [(t, n) for t, n in self.window if now - t < 60.0]
            used = sum(n for _, n in recent)
            for t, n in recent:  # oldest first: wait until enough has expired
                if used + tokens_needed <= self.limit_tokens:
                    break
                wait = max(wait, 60.0 - (now - t))
                used -= n
        return max(wait, 0.0)

    def mark(self, tokens: int = 0) -> None:
        self._last = self.clock()
        if tokens:
            self.window = [(t, n) for t, n in self.window if self._last - t < 60.0]
            self.window.append((self._last, tokens))


class Ledger:
    """One JSON line per HTTP request to the judge provider; counts today's calls."""

    def __init__(self, path: Path = USAGE_LEDGER, today: Callable[[], date] | None = None):
        self.path = path
        self._today = today or (lambda: datetime.now(UTC).date())

    def calls_today(self) -> int:
        if not self.path.exists():
            return 0
        day = self._today().isoformat()
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return sum(1 for line in lines if line.strip() and json.loads(line)["time"][:10] == day)

    def record(self, **fields: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = {"time": datetime.now(UTC).isoformat(timespec="seconds"), **fields}
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")


class JudgeClient(GroqClient):
    """GroqClient with thinking disabled, rate-limit pacing and request counting."""

    def __init__(
        self,
        api_key: str,
        *,
        limiter: RateLimiter | None = None,
        ledger: Ledger | None = None,
        purpose: str = "judge",
        sleep: Sleep = asyncio.sleep,
        **kw: Any,
    ) -> None:
        super().__init__(api_key, sleep=sleep, **kw)
        self.limiter = limiter or RateLimiter()
        self.ledger = ledger
        self.purpose = purpose
        self.thinking = THINKING_NONE
        self.requests = 0  # every HTTP request to the provider, retries included
        self.refused = 0  # requests that didn't come back 200
        self.last_model: str | None = None
        self._judge_sleep = sleep
        self._last_status: int | None = None
        self._reserve = 0
        self._client.event_hooks["response"].append(self._on_response)

    async def _on_response(self, response: httpx.Response) -> None:
        self._last_status = response.status_code

    def payload(
        self, model: ModelConfig, messages: Messages, seed: int, schema: type[BaseModel]
    ) -> dict[str, Any]:
        out = super().payload(model, messages, seed, schema)
        if self.thinking == THINKING_NONE:
            out["reasoning_effort"] = "none"
        else:
            out["reasoning_format"] = "hidden"
        return out

    async def complete(
        self, model: ModelConfig, messages: Messages, seed: int, schema: type[BaseModel]
    ) -> Completion:
        needed = sum(len(m["content"]) for m in messages) // 4 + model.max_tokens
        self._reserve = needed  # paced in _post_once, before every request and retry
        try:
            return await super().complete(model, messages, seed, schema)
        except LLMRequestError as e:
            if self.thinking == THINKING_NONE and "reasoning" in str(e).lower():
                self.thinking = THINKING_HIDDEN  # the model doesn't take reasoning_effort
                return await super().complete(model, messages, seed, schema)
            raise

    async def _post_once(self, model: str, payload: dict[str, Any]) -> Completion:
        wait = self.limiter.delay(self._reserve)  # a retry waits its turn too
        if wait > 0:
            await self._judge_sleep(wait)
        self.limiter.mark(self._reserve)
        self.requests += 1
        self._last_status = None
        completion: Completion | None = None
        error: str | None = None
        try:
            completion = await super()._post_once(model, payload)
            return completion
        except Exception as e:
            error = str(getattr(e, "error", e))[:300]
            raise
        finally:
            if self._last_status != 200:
                self.refused += 1
            if self.ledger is not None:
                self.ledger.record(
                    model=model, purpose=self.purpose, ok=completion is not None,
                    status=self._last_status, error=error,
                    prompt_tokens=completion.prompt_tokens if completion else 0,
                    completion_tokens=completion.completion_tokens if completion else 0,
                )  # fmt: skip

    def _parse_ok(self, resp: httpx.Response) -> Completion:  # type: ignore[override]
        self.limiter.observe(resp.headers)
        try:
            self.last_model = resp.json().get("model")
        except ValueError:
            self.last_model = None
        return GroqClient._parse_ok(resp)


class OpenAIJudgeClient(JudgeClient):
    """The same judge client on OpenAI: strict JSON-schema output (``OpenAIClient``), hidden
    reasoning (nothing to strip), OpenAI's rate-limit headers; counting, ledger and pacing as
    for Groq. A second-opinion judge only."""

    provider = "openai"  # type: ignore[assignment]  # GroqClient narrowed it to "groq"
    default_url = OPENAI_URL

    def __init__(self, api_key: str, **kw: Any) -> None:
        super().__init__(api_key, **kw)
        self.thinking = THINKING_OPENAI

    def payload(
        self, model: ModelConfig, messages: Messages, seed: int, schema: type[BaseModel]
    ) -> dict[str, Any]:
        return OpenAIClient.payload(self, model, messages, seed, schema)  # type: ignore[arg-type]

    def rate_limit_wait(self, resp: httpx.Response, message: str) -> float | None:
        return OpenAIClient.rate_limit_wait(self, resp, message)  # type: ignore[arg-type]

    def is_quota_error(self, resp: httpx.Response, message: str) -> bool:
        return OpenAIClient.is_quota_error(self, resp, message)  # type: ignore[arg-type]


def client_for(provider: Provider, api_key: str, **kw: Any) -> JudgeClient:
    return OpenAIJudgeClient(api_key, **kw) if provider == "openai" else JudgeClient(api_key, **kw)
