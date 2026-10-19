"""Operational metrics (Day 15): latency and tokens from the response metadata.

Per case: ``latency_ms`` (the agent's own ``latency_ms``; the client-side
``wall_latency_ms`` in details) and ``tokens`` (prompt + completion, only when the reply
called the LLM). Summaries per actual route: latency p50 / p95 / max, token totals and
means. **Cost is computed only when prices are given** (USD per 1M prompt / completion
tokens); there is no default price.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from evalkit.schemas import CaseResult, MetricResult
from evalkit.stats.bootstrap import quantile

LATENCY: Final = "latency_ms"
TOKENS: Final = "tokens"
NO_ROUTE: Final = "none"


def _route(result: CaseResult) -> str:
    resp = result.response
    return resp.route.value if resp is not None and resp.route else NO_ROUTE


def latency(result: CaseResult) -> MetricResult | None:
    resp = result.response
    if resp is None or not resp.ok or resp.latency_ms is None:
        return None
    return MetricResult(
        case_id=result.case_id,
        metric=LATENCY,
        value=float(resp.latency_ms),
        details={"route": _route(result), "wall_latency_ms": result.wall_latency_ms},
    )


def tokens(result: CaseResult) -> MetricResult | None:
    resp = result.response
    if resp is None or not resp.ok or resp.prompt_tokens is None:
        return None
    completion = resp.completion_tokens or 0
    return MetricResult(
        case_id=result.case_id,
        metric=TOKENS,
        value=resp.prompt_tokens + completion,
        details={"route": _route(result), "prompt": resp.prompt_tokens, "completion": completion},
    )


@dataclass(frozen=True)
class LatencySummary:
    n: int
    p50: float
    p95: float
    max: float


@dataclass(frozen=True)
class TokenSummary:
    n: int  # replies that called the LLM
    prompt: int
    completion: int

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def cost_usd(self, price_in: float | None, price_out: float | None) -> float | None:
        """USD for these tokens at the given prices per 1M tokens; None without prices."""
        if price_in is None or price_out is None:
            return None
        return (self.prompt * price_in + self.completion * price_out) / 1_000_000


def latency_by_route(rows: Iterable[MetricResult]) -> dict[str, LatencySummary]:
    by: dict[str, list[float]] = {}
    for r in rows:
        if r.metric == LATENCY and r.value is not None:
            by.setdefault(r.details["route"], []).append(float(r.value))
            by.setdefault("all", []).append(float(r.value))
    out = {}
    for route, values in sorted(by.items()):
        values.sort()
        out[route] = LatencySummary(
            len(values), quantile(values, 0.5), quantile(values, 0.95), values[-1]
        )
    return out


def tokens_by_route(rows: Iterable[MetricResult]) -> dict[str, TokenSummary]:
    acc: dict[str, list[int]] = {}
    for r in rows:
        if r.metric != TOKENS:
            continue
        for route in (r.details["route"], "all"):
            n, p, c = acc.get(route, [0, 0, 0])
            acc[route] = [n + 1, p + r.details["prompt"], c + r.details["completion"]]
    return {route: TokenSummary(*v) for route, v in sorted(acc.items())}
