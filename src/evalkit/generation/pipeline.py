"""Run the generators, filter, check routes against the snapshot router, write outputs.

Outputs of a run (``evalkit generate``):

* the dataset (default ``datasets/synthetic_raw_v1.jsonl``): kept cases, in generator order;
  written only when every selected generator finished;
* ``runs/gen/<run_id>/``: ``rejections.jsonl`` (every discarded candidate and why),
  ``summary.json`` and ``report.md`` (counts vs targets, rejections, router disagreements,
  tokens and approximate cost).

On a daily quota (or exhausted credit) the run stops after the current call; everything
already generated is in the LLM cache, so the same command continues from there.

``run_smoke`` is the cheap check before a full run: every generator may make only
SMOKE_CALLS calls and keeps its first few cases; the cost per call measured per generator,
times the number of calls the full run makes (from the estimate), projects the total.
"""

import json
import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from evalkit.generation.base import GenContext
from evalkit.generation.disagreements import RouterCheck, check_routes, disagreements
from evalkit.generation.disagreements import render_markdown as render_router
from evalkit.generation.filters import Kept, Rejection, apply_filters
from evalkit.generation.generators import GENERATOR_TARGET, GENERATORS, ROUTE_GAP
from evalkit.generation.generators.paraphrase import FALLBACK_TAG
from evalkit.generation.llm import (
    APPROX_PRICES,
    Estimate,
    GenLLM,
    LLMClient,
    ModelConfig,
    QuotaExhausted,
    Usage,
)
from evalkit.generation.sources import Sources
from evalkit.io import append_jsonl, write_jsonl
from evalkit.schemas import TestCase

DEFAULT_OUT: Final = Path("datasets") / "synthetic_raw_v1.jsonl"
RUNS_DIR: Final = Path("runs") / "gen"
DEFAULT_SEED: Final = 5
# Smoke-test calls per generator: enough for 5 kept cases even with some rejections.
# Knowledge needs more: a plain call is one chunk with a quota of 1-2 cases.
SMOKE_CALLS: Final = 3
SMOKE_CALLS_KNOWLEDGE: Final = 8
SMOKE_SKIP_REASON: Final = "smoke test: call budget used up"
EVO_TAG: Final = "evo-"


@dataclass(frozen=True)
class GenerationConfig:
    large: ModelConfig  # every generator ...
    small: ModelConfig  # ... except database paraphrasing (tier "small")
    seed: int = DEFAULT_SEED
    only: tuple[str, ...] = ()
    concurrency: int = 2
    cache_dir: Path | None = Path(".cache") / "gen"

    def models(self) -> dict[str, ModelConfig]:
        return {g.name: self.large if g.tier == "large" else self.small for g in GENERATORS}

    def selected(self) -> list[str]:
        unknown = set(self.only) - {g.name for g in GENERATORS}
        if unknown:
            raise ValueError(f"unknown generator(s): {', '.join(sorted(unknown))}")
        return [g.name for g in GENERATORS if not self.only or g.name in self.only]


@dataclass
class GeneratorStats:
    candidates: int = 0
    kept: int = 0
    rejected: Counter[str] = field(default_factory=Counter)


@dataclass
class GenerationResult:
    config: GenerationConfig
    kept: list[Kept] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    stats: dict[str, GeneratorStats] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    checks: list[RouterCheck] = field(default_factory=list)
    quota_error: QuotaExhausted | None = None
    finished: list[str] = field(default_factory=list)

    @property
    def cases(self) -> list[TestCase]:
        return [k.case for k in self.kept]

    @property
    def complete(self) -> bool:
        return self.quota_error is None


async def run_generation(
    config: GenerationConfig,
    client: LLMClient,
    sources: Sources,
    predict: Callable[[str], str],
    existing_questions: Iterable[str] = (),
    *,
    call_budget: Mapping[str, int] | None = None,
    keep_per_generator: int | None = None,
) -> GenerationResult:
    existing = tuple(existing_questions)
    llm = GenLLM(
        client,
        seed=config.seed,
        cache_dir=config.cache_dir,
        concurrency=config.concurrency,
        call_budget=call_budget,
    )
    ctx = GenContext(llm=llm, models=config.models(), sources=sources, seed=config.seed)
    result = GenerationResult(config=config, usage=llm.usage)
    try:
        for spec in GENERATORS:
            if spec.name not in config.selected():
                continue
            try:
                out = await spec.run(ctx)
            except QuotaExhausted as e:
                result.quota_error = e
                break
            kept, rejected = apply_filters(out.candidates, out.quotas, existing)
            all_rejected = out.rejections + rejected
            if keep_per_generator is not None:
                kept = kept[:keep_per_generator]
                all_rejected = [r for r in all_rejected if r.reason != SMOKE_SKIP_REASON]
            stats = result.stats.setdefault(spec.name, GeneratorStats())
            stats.candidates = len(out.candidates)
            stats.kept = len(kept)
            stats.rejected.update(r.stage for r in all_rejected)
            result.kept += kept
            result.rejections += all_rejected
            result.finished.append(spec.name)
    finally:
        llm.close()
    result.checks = check_routes(result.cases, predict)
    return result


async def estimate_generation(
    config: GenerationConfig, client: LLMClient, sources: Sources
) -> Estimate:
    """Every call the run would make (from the real inputs), without making any."""
    estimate = Estimate()
    llm = GenLLM(client, seed=config.seed, cache_dir=config.cache_dir, estimate=estimate)
    ctx = GenContext(llm=llm, models=config.models(), sources=sources, seed=config.seed)
    try:
        for spec in GENERATORS:
            if spec.name in config.selected():
                await spec.run(ctx)
    finally:
        llm.close()
    return estimate


@dataclass(frozen=True)
class SmokeRow:
    generator: str
    model: str
    smoke_calls: int  # calls made in the smoke run (fresh + cached)
    smoke_cost: float | None  # approximate USD of those calls
    kept: int
    full_calls: int  # calls the full run makes
    projected_cost: float | None  # smoke cost per call x full_calls


@dataclass
class SmokeResult:
    result: GenerationResult
    rows: list[SmokeRow]

    @property
    def spent(self) -> float:
        """Approximate USD actually spent by this smoke run (fresh calls only)."""
        return self.result.usage.total_cost()

    @property
    def projected(self) -> float | None:
        """Projected USD of the full run; None if a generator's cost can't be projected."""
        costs = [r.projected_cost for r in self.rows]
        return None if any(c is None for c in costs) else sum(c or 0.0 for c in costs)


async def run_smoke(
    config: GenerationConfig,
    client: LLMClient,
    sources: Sources,
    predict: Callable[[str], str],
    existing_questions: Iterable[str] = (),
    *,
    cases_per_generator: int = 5,
) -> SmokeResult:
    """A few real cases per generator plus a projection of the full run's cost."""
    estimate = await estimate_generation(config, client, sources)
    result = await run_generation(
        config,
        client,
        sources,
        predict,
        existing_questions,
        call_budget={
            g: SMOKE_CALLS_KNOWLEDGE if g == "knowledge" else SMOKE_CALLS for g in config.selected()
        },
        keep_per_generator=cases_per_generator,
    )
    models = config.models()
    rows = []
    for name in result.finished:
        model = models[name].name
        u = result.usage.by_generator.get((name, model))
        calls = 0 if u is None else u.calls + u.cached_calls
        cost = None if u is None else u.cost(model, include_cached=True)
        full = estimate.total_calls(name)
        projected = None if cost is None or not calls else cost / calls * full
        rows.append(SmokeRow(name, model, calls, cost, result.stats[name].kept, full, projected))
    return SmokeResult(result, rows)


# --------------------------------------------------------------------------- outputs


def new_run_id(seed: int, now: datetime | None = None) -> str:
    return f"{(now or datetime.now(UTC)):%Y%m%d-%H%M%S}-seed{seed}"


def write_outputs(result: GenerationResult, out_path: Path | None, run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    rejections_path = run_dir / "rejections.jsonl"
    rejections_path.unlink(missing_ok=True)
    for r in result.rejections:
        append_jsonl(rejections_path, r)
    (run_dir / "summary.json").write_text(
        json.dumps(summarize(result), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (run_dir / "report.md").write_text(render_report(result), encoding="utf-8", newline="\n")
    if out_path is not None and result.complete:
        write_jsonl(out_path, result.cases)


def _evolution(case: TestCase) -> str:
    return next((t.removeprefix(EVO_TAG) for t in case.tags if t.startswith(EVO_TAG)), "?")


def _counts(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def summarize(result: GenerationResult) -> dict[str, Any]:
    cases = result.cases
    usage = {
        model: {
            "calls": u.calls,
            "prompt_tokens": u.prompt_tokens,
            "completion_tokens": u.completion_tokens,
            "reasoning_tokens": u.reasoning_tokens,
            "cached_calls": u.cached_calls,
            "cached_tokens": u.cached_tokens,
            "approx_cost_usd": result.usage.approx_cost(model),
            "approx_cost_usd_incl_cached": result.usage.approx_cost(model, include_cached=True),
        }
        for model, u in result.usage.by_model.items()
    }
    return {
        "seed": result.config.seed,
        "models": {"default": result.config.large.name, "database": result.config.small.name},
        "complete": result.complete,
        "finished_generators": result.finished,
        "quota_error": str(result.quota_error) if result.quota_error else None,
        "cases": len(cases),
        "by_generator": {
            name: {
                "target": GENERATOR_TARGET[name],
                "kept": s.kept,
                "candidates": s.candidates,
                "rejected": dict(sorted(s.rejected.items())),
            }
            for name, s in result.stats.items()
        },
        "by_route": _counts(
            c.expected_route.value if c.expected_route else "unchecked" for c in cases
        ),
        "by_category": _counts(c.category for c in cases),
        "by_difficulty": _counts(c.difficulty.value for c in cases),
        "by_evolution": _counts(_evolution(c) for c in cases),
        "safety_critical": sum(c.safety_critical for c in cases),
        "paraphrase_fallbacks": sum(FALLBACK_TAG in c.tags for c in cases),
        "rejections_by_stage": _counts(r.stage for r in result.rejections),
        "router_disagreements": {g: len(v) for g, v in disagreements(result.checks).items()},
        "usage": usage,
    }


def _table(header: Sequence[str], rows: Iterable[Sequence[object]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out


def _route_rows(by_route: Mapping[str, int]) -> list[list[object]]:
    rows: list[list[object]] = []
    for route, gap in ROUTE_GAP.items():
        target = math.ceil(gap * 1.5)
        rows.append([route, gap, target, by_route.get(route, 0)])
    rows.append(["route not checked", "-", "-", by_route.get("unchecked", 0)])
    return rows


def render_report(result: GenerationResult) -> str:
    s = summarize(result)
    lines = [
        f"Seed {s['seed']}; model `{s['models']['default']}` (database paraphrasing: "
        f"`{s['models']['database']}`). {s['cases']} cases kept"
        + ("" if s["complete"] else f" (INCOMPLETE: {s['quota_error']})")
        + ".",
        "",
        "### Per generator",
        "",
    ]
    lines += _table(
        ["Generator", "Target", "Kept", "Candidates", "Rejected (by stage)"],
        [
            [g, v["target"], v["kept"], v["candidates"],
             ", ".join(f"{k} {n}" for k, n in v["rejected"].items()) or "-"]
            for g, v in s["by_generator"].items()
        ],
    )  # fmt: skip
    lines += ["", "### Per expected route vs the Day 4 gap", ""]
    lines += _table(["Route", "Gap", "Target (1.5x)", "Kept"], _route_rows(s["by_route"]))
    for title, key in [
        ("Per category", "by_category"),
        ("Per difficulty", "by_difficulty"),
        ("Per evolution type", "by_evolution"),
    ]:
        lines += ["", f"### {title}", ""]
        lines += _table(["Value", "Cases"], list(s[key].items()))
    lines += [
        "",
        f"Safety-critical: {s['safety_critical']}. Paraphrase fallbacks (template wording "
        f"kept because no reworded variant passed): {s['paraphrase_fallbacks']}.",
        "",
        "### Rejections",
        "",
    ]
    lines += _table(["Stage", "Count"], list(s["rejections_by_stage"].items()))
    examples = [r for r in result.rejections if r.stage not in ("surplus",)]
    if examples:
        lines += ["", "<details><summary>Every rejection except surplus</summary>", ""]
        lines += _table(
            ["Generator", "Type", "Stage", "Reason", "Question"],
            [[r.generator, r.subtype, r.stage, _md(r.reason), _md(r.question or "")]
             for r in examples],
        )  # fmt: skip
        lines += ["", "</details>"]
    lines += ["", "### Router disagreements (review aid; labels unchanged)", ""]
    lines.append(render_router(result.checks))
    lines += ["### Tokens and cost", ""]
    lines += _table(
        ["Model", "Calls", "Prompt tok", "Completion tok", "of which reasoning", "Cached calls",
         "Approx. cost"],
        [
            [m, u["calls"], u["prompt_tokens"], u["completion_tokens"], u["reasoning_tokens"],
             u["cached_calls"],
             "n/a" if u["approx_cost_usd"] is None else f"~${u['approx_cost_usd']:.4f}"]
            for m, u in s["usage"].items()
        ],
    )  # fmt: skip
    lines += [
        "",
        "Costs are APPROXIMATE (public list prices, not checked against a bill: "
        + ", ".join(
            f"`{m}` ${p.input_per_m:g}/${p.output_per_m:g} per 1M in/out"
            for m, p in APPROX_PRICES.items()
        )
        + "). Reasoning tokens are billed as output and included in completion tokens; "
        "the Groq free tier is not billed.",
    ]
    return "\n".join(lines) + "\n"


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")[:300]


def render_estimate(estimate: Estimate) -> list[list[object]]:
    """Rows: generator, model, calls, input, output, total tokens, cached calls."""
    keys = sorted(
        set(estimate.calls) | set(estimate.cached_calls),
        key=lambda k: ([g.name for g in GENERATORS].index(k[0]), k[1]),
    )
    rows: list[list[object]] = []
    for key in keys:
        inp, out = estimate.input_tokens.get(key, 0), estimate.output_tokens.get(key, 0)
        rows.append([key[0], key[1], estimate.calls.get(key, 0), inp, out, inp + out,
                     estimate.cached_calls.get(key, 0)])  # fmt: skip
    return rows
