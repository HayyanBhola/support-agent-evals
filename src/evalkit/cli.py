"""``evalkit`` command line: health, run, runs, routes, generate, rubric check, label sample,
labels status / validate / export.

Exit codes: 0 all cases ok (skipped allowed), 1 some cases failed, 2 aborted (agent down,
auth error, bad dataset, bad arguments), 3 generation stopped by a daily LLM quota
(resumable from the cache), 4 generation smoke test projects more than --max-usd,
130 interrupted with Ctrl+C (resumable).
"""

import asyncio
import json
import logging
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.table import Table

from evalkit import budget as budget_mod
from evalkit import route_report, rubrics, runner, snapshot_router
from evalkit.adapters.base import AdapterError, AgentAdapter, AuthError, ConfigError
from evalkit.adapters.glowcare import GlowCareAdapter
from evalkit.bias import run as bias_run_mod
from evalkit.cache import CacheMode
from evalkit.calibration import leakage
from evalkit.calibration import report as agreement_report_mod
from evalkit.calibration import split as split_mod
from evalkit.calibration.loader import ACCESS_LOG, FINAL_ENV, git_commit, load_labels
from evalkit.generation import llm as judge_llm
from evalkit.generation import pipeline
from evalkit.generation.generators import GENERATOR_NAMES
from evalkit.generation.llm import (
    API_KEY_ENV,
    CLIENTS,
    GenerationError,
    LLMClient,
    Provider,
    default_model_config,
)
from evalkit.generation.sources import load_sources
from evalkit.hashing import file_sha256
from evalkit.io import read_json, read_jsonl, write_jsonl
from evalkit.judges import client as judge_client
from evalkit.judges import final as judge_final_mod
from evalkit.judges import frozen as judge_frozen
from evalkit.judges import prompts as judge_prompts
from evalkit.judges import report as judge_report
from evalkit.judges import runner as judge_runner
from evalkit.judges import schema as judge_schema
from evalkit.labeling import sampling, store, subsets, workflow
from evalkit.manifest import (
    MANIFEST_NAME,
    DatasetSpec,
    case_dataset_files,
    read_manifest,
    upsert_entry,
)
from evalkit.metrics import fixreport as fixreport_mod
from evalkit.metrics import full as full_mod
from evalkit.metrics import scorecard as scorecard_mod
from evalkit.metrics import tracking as tracking_mod
from evalkit.metrics import voice as voice_mod
from evalkit.schemas import (
    AgentResponse,
    CaseResult,
    Criterion,
    Label,
    LabelSample,
    LabelValue,
    MetricResult,
    Route,
    RunMeta,
    TestCase,
)
from evalkit.synthetic import evaluate as synthetic_eval
from evalkit.synthetic import negatives as negatives_mod

EXIT_QUOTA = 3
EXIT_OVER_BUDGET = 4

app = typer.Typer(
    help="Evaluate the GlowCare support agent.", no_args_is_help=True, add_completion=False
)
console = Console()


def make_client(provider: Provider) -> LLMClient:
    """The generation LLM client (tests replace this)."""
    load_dotenv(".env")
    return CLIENTS[provider](os.environ.get(API_KEY_ENV[provider], ""))


def make_adapter(read_timeout: float | None = None, **kwargs: Any) -> AgentAdapter:
    """The adapter under test (tests replace this)."""
    return GlowCareAdapter.from_env(read_timeout=read_timeout, **kwargs)


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=Console(stderr=True), show_path=False)],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.INFO if verbose else logging.WARNING)


@app.command()
def health() -> None:
    """Print the agent's /health and check that the API key works."""
    raise typer.Exit(asyncio.run(_health()))


async def _health() -> int:
    try:
        adapter = make_adapter()
    except ConfigError as e:
        console.print(f"[red]config error:[/] {e}")
        return runner.EXIT_ABORTED
    async with adapter:
        try:
            data = await adapter.health()
        except AdapterError as e:
            console.print(f"[red]agent not reachable:[/] {e}")
            return runner.EXIT_ABORTED
        console.print_json(data=data)
        try:
            reply = await adapter.answer(runner.PROBE_CASE)
        except AuthError as e:
            console.print(f"[red]API key rejected:[/] {e}")
            return runner.EXIT_ABORTED
        except AdapterError as e:
            console.print(f"[red]probe call failed ({e.failure_type.value}):[/] {e}")
            return runner.EXIT_ABORTED
    resp = reply.response
    route = resp.route.value if resp.route else "none"
    console.print(f"[green]API key works[/]: probe 'hi' -> route={route}, ok={resp.ok}")
    healthy = data.get("status") == "ok" and resp.ok
    return runner.EXIT_OK if healthy else runner.EXIT_ABORTED


@app.command()
def run(
    dataset: Annotated[Path, typer.Option("--dataset", help="JSONL file of TestCases.")],
    limit: Annotated[int | None, typer.Option(min=1, help="Run only the first N cases.")] = None,
    concurrency: Annotated[
        int, typer.Option(min=1, help="Parallel requests.")
    ] = runner.DEFAULT_CONCURRENCY,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Don't read or write.")] = False,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Don't read the cache; overwrite it.")
    ] = False,
    resume: Annotated[str | None, typer.Option(help="Continue an unfinished run.")] = None,
    route: Annotated[Route | None, typer.Option(help="Only cases with this route.")] = None,
    ids: Annotated[
        str | None, typer.Option(help="Only these case ids (a,b,c, or @file: one id per line).")
    ] = None,
    max_rpm: Annotated[
        float | None, typer.Option(min=0.1, help="At most N agent requests per minute (paced).")
    ] = None,
    max_llm_calls: Annotated[
        int | None, typer.Option(min=1, help="Stop cleanly before more LLM-using replies.")
    ] = None,
    stop_on_failure: Annotated[
        bool, typer.Option(help="Stop cleanly at the first failed case (then --resume).")
    ] = False,
    max_retries: Annotated[
        int | None, typer.Option(min=0, help="Adapter retries per request (default 2; 0 = none).")
    ] = None,
    agent_repo: Annotated[
        Path | None, typer.Option(help="Agent repo: its commit/branch go into meta.json.")
    ] = None,
    read_timeout: Annotated[
        float | None,
        typer.Option(
            min=1,
            help="Seconds to wait for each answer (default: AGENT_READ_TIMEOUT_S, else 180).",
        ),
    ] = None,
) -> None:
    """Send a dataset to the agent and record every outcome under runs/<run_id>/."""
    if no_cache and refresh:
        raise typer.BadParameter("use --no-cache or --refresh, not both")
    mode = CacheMode.OFF if no_cache else CacheMode.REFRESH if refresh else CacheMode.NORMAL
    raw_ids = ids or ""
    ids_file = Path(raw_ids[1:]) if raw_ids.startswith("@") else None
    if raw_ids.startswith("@"):
        try:
            raw_ids = Path(raw_ids[1:]).read_text(encoding="utf-8")
        except OSError as e:
            raise typer.BadParameter(f"cannot read --ids file: {e}") from None
    config = runner.RunConfig(
        dataset=dataset,
        concurrency=concurrency,
        limit=limit,
        route=route,
        ids=tuple(i.strip() for i in raw_ids.replace(",", "\n").splitlines() if i.strip()),
        cache_mode=mode,
        resume=resume,
        max_rpm=max_rpm,
        max_llm_calls=max_llm_calls,
        stop_on_failure=stop_on_failure,
        agent_repo=agent_repo,
        ids_file=ids_file,
    )
    try:
        code = asyncio.run(_run(config, read_timeout, max_retries))
    except KeyboardInterrupt:
        console.print("[yellow]interrupted before the run started[/]")
        code = runner.EXIT_INTERRUPTED
    raise typer.Exit(code)


async def _run(
    config: runner.RunConfig, read_timeout: float | None = None, max_retries: int | None = None
) -> int:
    try:
        extra = {} if max_retries is None else {"max_retries": max_retries}
        adapter = make_adapter(read_timeout=read_timeout, **extra)
    except ConfigError as e:
        console.print(f"[red]config error:[/] {e}")
        return runner.EXIT_ABORTED
    async with adapter:
        try:
            outcome = await runner.run_eval(config, adapter, console)
        except runner.RunAborted as e:
            console.print(f"[red]aborted:[/] {e}")
            return runner.EXIT_ABORTED
    runner.print_summary(console, outcome)
    if outcome.config.max_llm_calls is not None or outcome.stopped:
        console.print(f"LLM-using replies this session: {outcome.llm_calls}")
    if outcome.stopped:
        console.print(
            f"[yellow]stopped cleanly:[/] {escape(outcome.stopped)}. Finished results are "
            "saved. Resume with:\n  "
            + runner.resume_command(config, outcome.run_id)
            + (f" --read-timeout {read_timeout:g}" if read_timeout is not None else "")
            + (f" --max-retries {max_retries}" if max_retries is not None else "")
        )
    elif outcome.interrupted:
        console.print(
            "[yellow]interrupted.[/] Finished results are saved. Resume with:\n  "
            + runner.resume_command(config, outcome.run_id)
            + (f" --read-timeout {read_timeout:g}" if read_timeout is not None else "")
        )
    return outcome.exit_code


@app.command("runs")
def list_runs(
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
) -> None:
    """List past runs, newest first."""
    metas = runner.list_runs(runs_dir)
    if not metas:
        console.print(f"no runs in {runs_dir}")
        return
    # The run id already encodes the UTC start time, so there is no separate date column.
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("run id (UTC start)", no_wrap=True)
    table.add_column("dataset", overflow="fold", min_width=12)
    for col in ["cases", "ok", "fail", "cached"]:
        table.add_column(col, justify="right", no_wrap=True)
    table.add_column("status", no_wrap=True)
    for m in metas:
        table.add_row(
            m.run_id,
            _short_path(m.dataset_path),
            str(m.counts.total),
            str(m.counts.ok),
            str(m.counts.failed),
            str(m.counts.cache_hits),
            "done" if m.finished_at else "[yellow]partial[/]",
        )
    console.print(table)


@app.command("routes")
def routes(
    run_id: Annotated[str, typer.Argument(help="A run id from `evalkit runs`.")],
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
) -> None:
    """Route and escalation report of a run: accuracy, wrong routes, latency, models."""
    try:
        _, results, cases = route_report.load_run(runs_dir, run_id)
    except runner.RunAborted as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    report = route_report.build_route_report(results, cases)
    route_report.print_route_report(console, report)
    route_report.print_run_health(console, report)


def _short_path(path: str) -> str:
    """``path`` relative to the current directory when it is inside it."""
    p = Path(path)
    try:
        return p.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path


@app.command()
def generate(
    seed: Annotated[int, typer.Option(help="Seed for every random choice.")] = (
        pipeline.DEFAULT_SEED
    ),
    only: Annotated[
        str | None, typer.Option(help=f"Only these generators ({','.join(GENERATOR_NAMES)}).")
    ] = None,
    out: Annotated[Path, typer.Option(help="Dataset to write.")] = pipeline.DEFAULT_OUT,
    provider: Annotated[
        str, typer.Option(help="LLM provider: openai (structured outputs) or groq.")
    ] = "openai",
    model: Annotated[
        str | None,
        typer.Option(help="Model for every generator. Default: gpt-5-mini (openai)."),
    ] = None,
    paraphrase_model: Annotated[
        str | None,
        typer.Option(help="Model for database paraphrasing only. Default: --model."),
    ] = None,
    reasoning_effort: Annotated[
        str, typer.Option(help="Reasoning effort for reasoning models (none = omit it).")
    ] = "low",
    concurrency: Annotated[int, typer.Option(min=1, help="Parallel LLM calls.")] = 2,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Do not use .cache/gen.")] = False,
    estimate: Annotated[
        bool, typer.Option("--estimate", help="Only estimate calls and tokens; call nothing.")
    ] = False,
    smoke: Annotated[
        int | None,
        typer.Option(
            min=1,
            help="Smoke test: keep N cases per generator (few calls each), report the cost so "
            "far and project the full run. Writes no dataset.",
        ),
    ] = None,
    max_usd: Annotated[
        float, typer.Option(help="Smoke test: exit 4 if the projected full run costs more.")
    ] = 2.0,
) -> None:
    """Generate synthetic test cases (labels by rule, wording by LLM), filtered and checked."""
    if provider not in CLIENTS:
        console.print(f"[red]cannot start:[/] unknown provider {provider!r} (openai or groq)")
        raise typer.Exit(runner.EXIT_ABORTED)
    prov: Provider = "openai" if provider == "openai" else "groq"
    effort = None if reasoning_effort == "none" else reasoning_effort
    large = default_model_config(prov, model, effort)
    config = pipeline.GenerationConfig(
        large=large,
        small=default_model_config(prov, paraphrase_model or large.name, effort),
        seed=seed,
        only=tuple(g.strip() for g in only.split(",") if g.strip()) if only else (),
        concurrency=concurrency,
        cache_dir=None if no_cache else pipeline.GenerationConfig.cache_dir,
    )
    try:
        config.selected()
        sources = load_sources()
        client = make_client(prov)
    except (ValueError, OSError, GenerationError) as e:
        console.print(f"[red]cannot start:[/] {e}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    if estimate:
        est = asyncio.run(pipeline.estimate_generation(config, client, sources))
        _print_estimate(pipeline.render_estimate(est))
        return
    existing = [
        c.question
        for p in case_dataset_files(Path("datasets"))
        if p.name != "agent_chunks.jsonl" and p.resolve() != out.resolve()
        for c in read_jsonl(p, TestCase)
    ]
    router = snapshot_router.load_router()
    run_dir = pipeline.RUNS_DIR / pipeline.new_run_id(seed)
    try:
        if smoke is not None:
            smoke_result = asyncio.run(
                pipeline.run_smoke(
                    config, client, sources, router, existing, cases_per_generator=smoke
                )
            )
        else:
            result = asyncio.run(pipeline.run_generation(config, client, sources, router, existing))
    except GenerationError as e:
        console.print(f"[red]generation failed:[/] {e}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    if smoke is not None:
        _finish_smoke(smoke_result, run_dir, max_usd)
        return
    pipeline.write_outputs(result, out, run_dir)
    summary = pipeline.summarize(result)
    console.print(
        f"kept {summary['cases']} cases; rejections by stage: "
        f"{summary['rejections_by_stage']}; report: {run_dir / 'report.md'}"
    )
    if result.quota_error is not None:
        _print_quota_stop(result, f"{out} was NOT written")
        raise typer.Exit(EXIT_QUOTA)
    console.print(f"[green]wrote[/] {out}")


def _print_quota_stop(result: pipeline.GenerationResult, what: str) -> None:
    error = result.quota_error
    assert error is not None
    finished = ", ".join(result.finished) or "none"
    console.print(
        f"[yellow]stopped: quota for {error.model}[/] ({error}).\n"
        f"Finished generators: {finished}. Every finished call is cached in .cache/gen, and "
        f"{what}.\nAfter the quota resets (or credit is added), rerun the same command to "
        "continue."
    )


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def _finish_smoke(smoke: pipeline.SmokeResult, run_dir: Path, max_usd: float) -> None:
    pipeline.write_outputs(smoke.result, None, run_dir)
    write_jsonl(run_dir / "smoke_cases.jsonl", smoke.result.cases)
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, title="Smoke test (approximate prices)")
    cols = ["generator", "model", "calls", "kept", "cost", "full-run calls", "projected"]
    for col in cols:
        table.add_column(col, justify="left" if col in ("generator", "model") else "right")
    for r in smoke.rows:
        cells = [r.smoke_calls, r.kept, _money(r.smoke_cost), r.full_calls]
        table.add_row(r.generator, r.model, *map(str, cells), _money(r.projected_cost))
    console.print(table)
    projected = smoke.projected
    console.print(
        f"spent so far: ~{_money(smoke.spent)}; projected full run: ~{_money(projected)} "
        f"(retries and rejected replies may add some); cases: {run_dir / 'smoke_cases.jsonl'}"
    )
    if smoke.result.quota_error is not None:
        _print_quota_stop(smoke.result, "no dataset is written by a smoke test")
        raise typer.Exit(EXIT_QUOTA)
    if projected is None or projected > max_usd:
        console.print(
            f"[red]over budget:[/] projected {_money(projected)} vs --max-usd {max_usd:g}; "
            "not continuing."
        )
        raise typer.Exit(EXIT_OVER_BUDGET)
    console.print(f"[green]within budget[/] (--max-usd {max_usd:g})")


def _print_estimate(rows: list[list[object]]) -> None:
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, title="Estimated LLM use (not called)")
    for col in ["generator", "model", "calls", "input tok", "output tok", "total", "cached"]:
        table.add_column(col, justify="left" if col in ("generator", "model") else "right")
    totals: dict[str, int] = {}
    for row in rows:
        table.add_row(*(str(c) for c in row))
        totals[str(row[1])] = totals.get(str(row[1]), 0) + int(str(row[5]))
    console.print(table)
    for model, total in totals.items():
        console.print(
            f"{model}: ~{total:,} visible tokens (first attempts only; retries and hidden "
            "reasoning tokens add more)"
        )


rubric_app = typer.Typer(help="Rubric files (rubrics/): validate and hash.", no_args_is_help=True)
app.add_typer(rubric_app, name="rubric")


@rubric_app.command("check")
def rubric_check(
    rubric_dir: Annotated[Path, typer.Option("--dir", help="Rubric folder.")] = rubrics.DEFAULT_DIR,
    dataset: Annotated[
        Path, typer.Option(help="Dataset the example case_ids must exist in.")
    ] = rubrics.DEFAULT_DATASET,
) -> None:
    """Validate every rubric file and print rubric_hash (exit 2 on any problem)."""
    try:
        loaded = rubrics.load_rubrics(rubric_dir, dataset)
    except (rubrics.RubricError, OSError) as e:
        console.print(f"[red]rubric check failed:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    for col in ("criterion", "version", "routes", "pass ex.", "fail ex.", "shortage"):
        table.add_column(col)
    for r in loaded:
        table.add_row(
            r.criterion.value,
            str(r.meta.version),
            str(len(r.meta.applies_to.routes)),
            str(len(r.pass_examples)),
            str(len(r.fail_examples)),
            "yes" if r.shortages else "",
        )
    console.print(table)
    console.print(f"{len(rubrics.example_ids(loaded))} example case_ids; all valid")
    current = rubrics.rubric_hash(rubric_dir)
    console.print(f"rubric_hash: {current}")
    try:
        versions = rubrics.load_versions(rubric_dir)
    except ValueError as e:
        console.print(f"[red]{rubrics.VERSIONS_FILE} is invalid:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    recorded = versions.get(current) if versions else None
    if recorded is None:
        console.print(
            f"[yellow]warning[/] this rubric_hash is not in {rubrics.VERSIONS_FILE}: record the "
            "new version (and what it carries forward) before labeling under it"
        )
    else:
        console.print(f"recorded in {rubrics.VERSIONS_FILE} as v{recorded.version}")


# --------------------------------------------------------------------------- labeling

label_app = typer.Typer(
    help="Human labeling: draw the sample, the overlap and relabel sets.", no_args_is_help=True
)
labels_app = typer.Typer(help="Human labels: status, validate, export.", no_args_is_help=True)
app.add_typer(label_app, name="label")
app.add_typer(labels_app, name="labels")
DAY7_RUN = "20261013-191847-f23b"
LABEL_DATASET = Path("datasets") / "golden_v1.2.jsonl"
SAMPLE_PATH = Path("datasets") / workflow.SAMPLE_FILE
LABELS_RAW = store.DEFAULT_LABELS_DIR / store.RAW_FILE


@label_app.command("sample")
def label_sample(
    run_id: Annotated[str, typer.Option(help="Run whose responses are labeled.")] = DAY7_RUN,
    dataset: Annotated[Path, typer.Option(help="Gold fields from this dataset.")] = LABEL_DATASET,
    out: Annotated[Path, typer.Option(help="Sample file to write.")] = SAMPLE_PATH,
    seed: Annotated[int, typer.Option(help="Random seed.")] = sampling.SEED,
    size: Annotated[int, typer.Option(min=1, help="Records to draw.")] = sampling.SAMPLE_SIZE,
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
) -> None:
    """Draw the stratified labeling sample and record it in MANIFEST.json."""
    try:
        sample, allocation = workflow.build_label_sample(
            runs_dir=runs_dir, run_id=run_id, dataset=dataset, rubric_dir=rubric_dir,
            rubric_hash=rubrics.rubric_hash(rubric_dir), out=out, seed=seed, size=size,
        )  # fmt: skip
    except (runner.RunAborted, ValueError, OSError) as e:
        console.print(f"[red]cannot draw the sample:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, title=f"{len(sample)} records, seed {seed}")
    for col in ("expected_route", "answer_type", "sampled", "available"):
        table.add_column(
            col, justify="left" if col in ("expected_route", "answer_type") else "right"
        )
    for (route, atype), n in sorted(allocation.by_stratum.items()):
        table.add_row(route, atype, str(n), str(allocation.available[(route, atype)]))
    console.print(table)
    console.print(
        "by expected route: "
        + ", ".join(f"{r} {n}" for r, n in sorted(allocation.by_route().items()))
    )
    console.print(f"[green]wrote[/] {out} (and its MANIFEST.json entry)")


LABELS_EXPORT = Path("datasets") / workflow.EXPORT_FILE
OVERLAP_PATH = Path("datasets") / subsets.OVERLAP_FILE
RELABEL_PATH = Path("datasets") / subsets.RELABEL_FILE


def _subset_inputs(
    sample_path: Path, labels_path: Path, labeler: str
) -> tuple[list[LabelSample], dict[str, Label]]:
    samples = _load_labeling(sample_path)
    try:
        labels = {
            lab.case_id: lab for lab in read_jsonl(labels_path, Label) if lab.labeler == labeler
        }
    except (OSError, ValueError) as e:
        console.print(f"[red]cannot read the labels:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    return samples, labels


def _report_subset(subset: subsets.Subset, out: Path) -> None:
    console.print(
        f"{len(subset.records)} records: {subset.enriched} with a fail "
        f"({subset.rare_fail_records} with a fail on a rare criterion: "
        f"{', '.join(c.value for c in subset.rare) or 'none'}), "
        f"{len(subset.records) - subset.enriched} without"
    )
    console.print(
        "by expected route: " + ", ".join(f"{r} {n}" for r, n in subset.by_route().items())
    )
    console.print(f"[green]wrote[/] {out} (no verdicts or notes; MANIFEST entry has counts only)")


@label_app.command("overlap")
def label_overlap(
    labeler: Annotated[
        str, typer.Option(help="Whose labels pick the enriched records.")
    ] = "hayyan",
    sample_path: Annotated[Path, typer.Option("--sample", help="Labeled sample.")] = SAMPLE_PATH,
    labels_path: Annotated[Path, typer.Option("--labels", help="Exported labels.")] = LABELS_EXPORT,
    out: Annotated[Path, typer.Option(help="Overlap file to write.")] = OVERLAP_PATH,
    size: Annotated[int, typer.Option(min=1, help="Records.")] = subsets.OVERLAP_SIZE,
    enriched: Annotated[int, typer.Option(min=0, help="Records with a fail.")] = (
        subsets.OVERLAP_ENRICHED
    ),
    seed: Annotated[int, typer.Option(help="Random seed.")] = sampling.SEED,
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
) -> None:
    """The overlap set for a second labeler: stratified by route, enriched for fails."""
    samples, labels = _subset_inputs(sample_path, labels_path, labeler)
    try:
        subset = subsets.select_overlap(samples, labels, size=size, enriched=enriched, seed=seed)
    except ValueError as e:
        console.print(f"[red]cannot select the overlap set:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    subsets.write_subset(
        subset, out, sample_file=sample_path, labels_file=labels_path, labeler=labeler,
        seed=seed, rubric_hash=rubrics.rubric_hash(rubric_dir),
        description="overlap set for a second labeler (Day 12), enriched for fails",
    )  # fmt: skip
    _report_subset(subset, out)


@label_app.command("relabel-set")
def label_relabel_set(
    labeler: Annotated[
        str, typer.Option(help="Whose labels pick the enriched records.")
    ] = "hayyan",
    sample_path: Annotated[Path, typer.Option("--sample", help="Labeled sample.")] = SAMPLE_PATH,
    labels_path: Annotated[Path, typer.Option("--labels", help="Exported labels.")] = LABELS_EXPORT,
    overlap: Annotated[Path, typer.Option(help="Overlap set to avoid.")] = OVERLAP_PATH,
    out: Annotated[Path, typer.Option(help="Relabel file to write.")] = RELABEL_PATH,
    size: Annotated[int, typer.Option(min=1, help="Records.")] = subsets.RELABEL_SIZE,
    enriched: Annotated[int, typer.Option(min=0, help="Records with a fail.")] = (
        subsets.RELABEL_ENRICHED
    ),
    seed: Annotated[int, typer.Option(help="Random seed.")] = sampling.SEED,
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
) -> None:
    """The blind self-relabel set: not in the overlap set, lightly enriched for fails."""
    samples, labels = _subset_inputs(sample_path, labels_path, labeler)
    excluded = [s.case_id for s in _load_labeling(overlap)] if overlap.exists() else []
    try:
        subset = subsets.select_relabel(
            samples, labels, excluded, size=size, enriched=enriched, seed=seed
        )
    except ValueError as e:
        console.print(f"[red]cannot select the relabel set:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    subsets.write_subset(
        subset, out, sample_file=sample_path, labels_file=labels_path, labeler=labeler,
        seed=seed, rubric_hash=rubrics.rubric_hash(rubric_dir),
        description="blind self-relabel set (Day 12), lightly enriched for fails",
        extra={"excluded_overlap": len(excluded), "from_overlap": subset.from_overlap},
    )  # fmt: skip
    _report_subset(subset, out)
    if subset.from_overlap:
        console.print(f"[yellow]{subset.from_overlap} record(s) taken from the overlap set[/]")


def _load_labeling(sample_path: Path) -> list[LabelSample]:
    try:
        return list(read_jsonl(sample_path, LabelSample))
    except (OSError, ValueError) as e:
        console.print(f"[red]cannot read the sample:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None


@labels_app.command("status")
def labels_status(
    labeler: Annotated[str | None, typer.Option(help="Only this labeler (default: each).")] = None,
    sample_path: Annotated[Path, typer.Option("--sample", help="Sample file.")] = SAMPLE_PATH,
    labels_path: Annotated[Path, typer.Option("--labels", help="Raw labels file.")] = LABELS_RAW,
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
) -> None:
    """Done / remaining per expected route, overrides, labels under an older rubric_hash."""
    samples = _load_labeling(sample_path)
    label_store = store.LabelStore(labels_path)
    latest = label_store.latest()
    who = sorted({w for _, w in latest}) if labeler is None else [labeler]
    current = rubrics.rubric_hash(rubric_dir)
    versions = rubrics.load_versions(rubric_dir)
    if not who:
        console.print(f"no labels yet (0/{len(samples)})")
        return
    for name in who:
        mine = {cid: lab for (cid, w), lab in latest.items() if w == name}
        st = workflow.status(samples, mine, current, versions)
        table = Table(
            box=box.SIMPLE_HEAD, pad_edge=False, title=f"{name}: {st.done}/{st.total} labeled"
        )
        for col in ("expected_route", "done", "remaining"):
            table.add_column(col, justify="left" if col == "expected_route" else "right")
        for route, n in sorted(st.total_by_route.items()):
            done = st.done_by_route.get(route, 0)
            table.add_row(route, str(done), str(n - done))
        console.print(table)
        stale = f" ({', '.join(st.stale)})" if st.stale else ""
        console.print(f"overrides: {st.overrides}; to re-check under the current rubric: "
                      f"{len(st.stale)}{stale}")  # fmt: skip
        if st.carried:
            console.print(
                f"carried forward from an older rubric_hash (still valid): {len(st.carried)}"
            )
        if st.unknown:
            console.print(f"[yellow]labels for cases not in the sample:[/] {', '.join(st.unknown)}")
        timing = workflow.first_pass_timing(
            [lab for lab in label_store.all() if lab.labeler == name]
        )
        if timing:
            console.print(
                f"first-pass time per record (earliest row each): mean {timing.mean} s, "
                f"median {timing.median} s, total {timing.total_minutes} min"
            )


@labels_app.command("validate")
def labels_validate(
    sample_path: Annotated[Path, typer.Option("--sample", help="Sample file.")] = SAMPLE_PATH,
    labels_path: Annotated[Path, typer.Option("--labels", help="Raw labels file.")] = LABELS_RAW,
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
) -> None:
    """Check the latest labels: notes on fails, response hashes; applicability warnings."""
    samples = _load_labeling(sample_path)
    latest = list(store.LabelStore(labels_path).latest().values())
    loaded = rubrics.load_rubrics(rubric_dir, None)
    result = workflow.validate(
        samples, latest, loaded, current_hash=rubrics.rubric_hash(rubric_dir),
        versions=rubrics.load_versions(rubric_dir),
    )  # fmt: skip
    for n in result.notes:
        console.print(f"[cyan]note[/] {escape(n)}")
    for w in result.warnings:
        console.print(f"[yellow]warning[/] {escape(w)}")
    for err in result.errors:
        console.print(f"[red]error[/] {escape(err)}")
    console.print(
        f"{len(latest)} labels: {len(result.errors)} error(s), {len(result.warnings)} warning(s)"
    )
    if result.errors:
        raise typer.Exit(1)


@labels_app.command("export")
def labels_export(
    labels_path: Annotated[Path, typer.Option("--labels", help="Raw labels file.")] = LABELS_RAW,
    sample_path: Annotated[Path, typer.Option("--sample", help="Sample file.")] = SAMPLE_PATH,
    out: Annotated[Path, typer.Option(help="Export file.")] = Path("datasets")
    / workflow.EXPORT_FILE,
    version: Annotated[str, typer.Option(help="Version recorded in MANIFEST.")] = "1.0",
    changelog: Annotated[str | None, typer.Option(help="What changed (MANIFEST).")] = None,
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
) -> None:
    """Latest label per case per labeler -> datasets/labels_v1.jsonl (+ MANIFEST entry)."""
    current = rubrics.rubric_hash(rubric_dir)
    versions = rubrics.load_versions(rubric_dir)
    exported = workflow.export_labels(
        store.LabelStore(labels_path), out, sample_path.name, version=version,
        changelog=changelog, current_hash=current, versions=versions,
    )  # fmt: skip
    states = Counter(workflow.label_state(lab, current, versions).value for lab in exported)
    console.print(f"[green]wrote[/] {len(exported)} labels to {out}")
    console.print(", ".join(f"{s}: {n}" for s, n in sorted(states.items())))
    note = versions.carried_note(current) if versions else None
    if states.get("carried") and note:
        console.print(f"[cyan]note[/] {escape(note)}")


# --------------------------------------------------------------------------- agreement

agreement_app = typer.Typer(help="Agreement between labelers.", no_args_is_help=True)
app.add_typer(agreement_app, name="agreement")


@agreement_app.command("report")
def agreement_report(
    a: Annotated[str, typer.Option("--a", help="First labeler (e.g. hayyan).")],
    b: Annotated[str, typer.Option("--b", help="Second labeler (or hayyan-r2).")],
    labels: Annotated[
        list[Path] | None,
        typer.Option("--labels", help="Raw labels file(s); default: every labels_raw*.jsonl."),
    ] = None,
    sample: Annotated[
        Path | None, typer.Option(help="The set compared (default: found in MANIFEST).")
    ] = None,
    out: Annotated[Path, typer.Option(help="Report to write.")] = agreement_report_mod.DEFAULT_OUT,
    labels_dir: Annotated[Path, typer.Option(help="Folder of raw labels.")] = (
        store.DEFAULT_LABELS_DIR
    ),
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
    resamples: Annotated[int, typer.Option(min=1, help="Bootstrap resamples.")] = 2000,
    seed: Annotated[int, typer.Option(help="Bootstrap seed.")] = 42,
    title: Annotated[str | None, typer.Option(help="Report title.")] = None,
    note: Annotated[str | None, typer.Option(help="A note shown under the title.")] = None,
) -> None:
    """Per-criterion agreement (kappa, AC1, CIs, confusion) and every disagreement."""
    files = labels or sorted(labels_dir.glob(agreement_report_mod.RAW_GLOB))
    try:
        latest = agreement_report_mod.latest_labels(files)
        text, results = agreement_report_mod.build_report(
            a, b, latest, order=rubrics.judging_order(rubric_dir),
            sources=[f.as_posix() for f in files], datasets_dir=labels_dir.parent,
            sample=sample, resamples=resamples, seed=seed, title=title, note=note,
        )  # fmt: skip
    except (OSError, ValueError) as e:
        console.print(f"[red]cannot build the agreement report:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    for r in results:
        name, value = r.headline()
        flags = "; ".join(r.flags())
        line = f"{r.criterion.value}: n={r.confusion.n}, {name} "
        line += f"{value:.2f}" if value is not None else "undefined"
        console.print(line + (f"  [yellow]{escape(flags)}[/]" if flags else ""))
    console.print(f"[green]wrote[/] {out}")


# --------------------------------------------------------------------------- split

split_app = typer.Typer(help="Dev/test split of the human labels.", no_args_is_help=True)
app.add_typer(split_app, name="split")
SPLIT_LABELS = Path("datasets") / "labels_v1.1.jsonl"
SPLIT_OUT = split_mod.SPLIT_DIR / split_mod.SPLIT_FILE
OVERLAP_RAW = store.DEFAULT_LABELS_DIR / store.raw_file_for(OVERLAP_PATH)


@split_app.command("make")
def split_make(
    labels_path: Annotated[Path, typer.Option("--labels", help="Labels to split.")] = SPLIT_LABELS,
    sample_path: Annotated[Path, typer.Option("--sample", help="Labeled sample.")] = SAMPLE_PATH,
    labeler: Annotated[str, typer.Option(help="Whose labels.")] = "hayyan",
    round1: Annotated[Path, typer.Option(help="Round-1 labels.")] = LABELS_EXPORT,
    second: Annotated[str, typer.Option(help="The overlap labeler.")] = "labeller_b",
    second_labels: Annotated[Path, typer.Option(help="Their raw labels.")] = OVERLAP_RAW,
    overlap: Annotated[Path, typer.Option(help="The overlap set.")] = OVERLAP_PATH,
    out: Annotated[Path, typer.Option(help="Split file to write.")] = SPLIT_OUT,
    seed: Annotated[int, typer.Option(help="Random seed.")] = sampling.SEED,
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
) -> None:
    """Split the labeled case_ids into dev (60%) and test (40%); write the split file."""
    try:
        samples = {s.case_id: s for s in _load_labeling(sample_path)}
        labels = {lab.case_id: lab for lab in read_jsonl(labels_path, Label)
                  if lab.labeler == labeler}  # fmt: skip
        examples = json.loads((rubric_dir / rubrics.EXAMPLES_FILE).read_text("utf-8"))
        split = split_mod.make_split(samples, labels, exclude=examples["case_ids"], seed=seed)
        old = {lab.case_id: lab for lab in read_jsonl(round1, Label) if lab.labeler == labeler}
        theirs = agreement_report_mod.latest_labels([second_labels]).get(second, {})
        overlap_ids = [s.case_id for s in read_jsonl(overlap, LabelSample)]
    except (OSError, ValueError, KeyError) as e:
        console.print(f"[red]cannot make the split:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    current = rubrics.rubric_hash(rubric_dir)
    versions = rubrics.load_versions(rubric_dir)
    states = Counter(workflow.label_state(lab, current, versions).value for lab in labels.values())
    datasets_dir = labels_path.parent
    entry = (
        read_manifest(datasets_dir / MANIFEST_NAME).datasets.get(labels_path.name)
        if (datasets_dir / MANIFEST_NAME).exists()
        else None
    )
    doc = split_mod.split_document(
        split, samples, labels, labels_file=labels_path.name,
        labels_sha256=file_sha256(labels_path), labels_version=entry.version if entry else None,
        rubric_hash=current, label_states=dict(sorted(states.items())), overlap=overlap_ids,
        round1=split_mod.disagreements(old, theirs),
        current=split_mod.disagreements(labels, theirs), second_labeler=second,
    )  # fmt: skip
    split_mod.write_split(doc, out)
    name = out.relative_to(datasets_dir).as_posix() if out.is_relative_to(datasets_dir) else None
    if name is not None:
        upsert_entry(
            datasets_dir, name,
            DatasetSpec(
                version="1", kind="splits", sources=[labels_path.name, sample_path.name],
                description="dev/test split of the human labels by case_id (Day 13)",
                metadata={
                    "seed": seed, "dev": len(split.dev), "test": len(split.test),
                    "labels_version": entry.version if entry else None,
                    "labels_sha256": file_sha256(labels_path), "rubric_hash": current,
                },
            ),
        )  # fmt: skip
    _print_split(split, labels)
    console.print(f"[green]wrote[/] {out}" + (" (and its MANIFEST.json entry)" if name else ""))


def _print_split(split: split_mod.Split, labels: dict[str, Label]) -> None:
    title = f"dev {len(split.dev)} / test {len(split.test)} (seed {split.seed})"
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, title=title)
    table.add_column("criterion")
    for name in split_mod.SPLITS:
        table.add_column(f"{name} pass / fail / n_a", justify="right")
    tables = {n: split_mod.verdict_table(split.of(n), labels) for n in split_mod.SPLITS}
    for c in Criterion:
        row = [" / ".join(str(tables[n][c.value][v.value]) for v in LabelValue)
               for n in split_mod.SPLITS]  # fmt: skip
        table.add_row(c.value, *row)
    console.print(table)
    for cov in split_mod.coverage(split, labels):
        if cov.fails < subsets.RARE_FAIL_MAX:
            console.print(
                f"{cov.criterion.value}: {cov.fails} fail(s), dev {cov.dev}, test {cov.test} "
                f"({cov.note()})"
            )


@split_app.command("check")
def split_check(
    paths: Annotated[
        list[Path] | None,
        typer.Option("--path", help="File or folder to scan (default: prompts/, judges/)."),
    ] = None,
    split_path: Annotated[Path, typer.Option("--split", help="Split file.")] = SPLIT_OUT,
    sample_path: Annotated[Path, typer.Option("--sample", help="Labeled sample.")] = SAMPLE_PATH,
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
) -> None:
    """Fail if a prompt or judge file contains a test case_id, reply or question."""
    try:
        split = split_mod.read_split(split_path)
        samples = {s.case_id: s for s in _load_labeling(sample_path)}
    except (OSError, ValueError) as e:
        console.print(f"[red]cannot check the split:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    found = leakage.markers(split, samples, rubric_dir)
    files = leakage.files_to_scan(paths or leakage.SCAN_DIRS)
    hits = leakage.scan(files, found)
    for hit in hits:
        console.print(f"[red]leak[/] {escape(str(hit))}")
    kinds = Counter(m.kind for m in found)
    summary = (
        f"{len(files)} file(s) scanned for {kinds['case_id']} test case_ids, "
        f"{kinds['reply']} replies and {kinds['question']} questions "
        f"(texts: at least {leakage.MIN_TEXT_CHARS} characters and unique to test)"
    )
    if hits:
        console.print(f"[red]{len(hits)} leak(s)[/]; {summary}")
        raise typer.Exit(1)
    console.print(f"[green]no test data found[/]; {summary}")


# --------------------------------------------------------------------------- score


@app.command("score")
def score(
    run_id: Annotated[str, typer.Argument(help="Saved run to score.")],
    dataset: Annotated[Path, typer.Option(help="Dataset version to score against.")] = (
        LABEL_DATASET
    ),
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
    allowlist: Annotated[Path, typer.Option(help="voice_third_person allowlist.")] = (
        voice_mod.ALLOWLIST_FILE
    ),
    price_in: Annotated[
        float | None, typer.Option(help="USD per 1M prompt tokens (no default).")
    ] = None,
    price_out: Annotated[
        float | None, typer.Option(help="USD per 1M completion tokens (no default).")
    ] = None,
    resamples: Annotated[int, typer.Option(min=1, help="Bootstrap resamples.")] = 2000,
    seed: Annotated[int, typer.Option(help="Bootstrap seed.")] = 42,
) -> None:
    """Every deterministic metric on a saved run -> metrics.jsonl and scorecard.md (no LLM)."""
    run_dir = runs_dir / run_id
    try:
        results = list(read_jsonl(run_dir / runner.RESULTS_FILE, CaseResult))
        cases = {c.id: c for c in read_jsonl(dataset, TestCase)}
    except (OSError, ValueError) as e:
        console.print(f"[red]cannot score the run:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    rows = scorecard_mod.score_run(results, cases, voice_mod.load_allowlist(allowlist))
    write_jsonl(run_dir / scorecard_mod.METRICS_FILE, rows)
    text = scorecard_mod.scorecard(
        run_id, dataset.name, file_sha256(dataset), rows, cases,
        results_count=sum(r.case_id in cases for r in results), price_in=price_in,
        price_out=price_out, resamples=resamples, seed=seed,
    )  # fmt: skip
    (run_dir / scorecard_mod.SCORECARD_FILE).write_text(text, encoding="utf-8", newline="\n")
    for m in scorecard_mod.PASS_METRICS:
        r = scorecard_mod.rate([x for x in rows if x.metric == m], resamples=resamples, seed=seed)
        console.print(f"{m}: {r.passed}/{r.n}" + (f" ({100 * r.rate:.1f}%)" if r.rate else ""))
    console.print(f"[green]wrote[/] {run_dir / scorecard_mod.METRICS_FILE} ({len(rows)} rows) "
                  f"and {run_dir / scorecard_mod.SCORECARD_FILE}")  # fmt: skip


# --------------------------------------------------------------------------- judges

judge_app = typer.Typer(
    help="LLM judges (dev split; `judge final` runs the one test evaluation).", no_args_is_help=True
)
app.add_typer(judge_app, name="judge")
JUDGE_LABELS = Path("datasets") / "labels_v1.2.jsonl"
OPENAI_OUTPUT_ESTIMATE = judge_client.OPENAI_OUTPUT_ESTIMATE


def _judge_setup(criterion: str, rubric_dir: Path) -> tuple[Criterion, rubrics.Rubric, str]:
    try:
        crit = Criterion(criterion)
        loaded = {r.criterion: r for r in rubrics.load_rubrics(rubric_dir, None)}
    except (ValueError, rubrics.RubricError) as e:
        console.print(f"[red]cannot load the rubric:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    return crit, loaded[crit], rubrics.rubric_hash(rubric_dir)


@judge_app.command("prompt")
def judge_prompt(
    criterion: Annotated[str, typer.Option(help="Criterion, e.g. relevance.")],
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
    split_path: Annotated[Path, typer.Option("--split-file", help="Split file.")] = SPLIT_OUT,
    prompt_dir: Annotated[Path, typer.Option(help="Prompt folder.")] = judge_prompts.PROMPT_DIR,
    prompt_version: Annotated[str, typer.Option(help="Judge prompt version.")] = (
        judge_prompts.PROMPT_VERSION
    ),
) -> None:
    """Build prompts/judges/<criterion>_<version>.md from the rubric file."""
    crit, rubric, rhash = _judge_setup(criterion, rubric_dir)
    try:
        text = judge_prompts.build_prompt(
            rubric, rhash, split_mod.read_split(split_path), prompt_version
        )
    except ValueError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    path = judge_prompts.prompt_path(crit, prompt_dir, prompt_version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    console.print(f"[green]wrote[/] {path} (prompt sha256 {judge_prompts.prompt_sha256(text)})")


JudgeInputs = tuple[
    judge_runner.JudgeConfig,
    rubrics.Rubric,
    split_mod.SplitFile,
    dict[str, CaseResult],
    dict[str, TestCase],
]


def _judge_inputs(
    criterion: str,
    run_id: str,
    runs_dir: Path,
    dataset: Path,
    rubric_dir: Path,
    split_path: Path,
    prompt_dir: Path,
    model: str | None,
    version: str = judge_prompts.PROMPT_VERSION,
    provider: str = "groq",
) -> JudgeInputs:
    crit, rubric, rhash = _judge_setup(criterion, rubric_dir)
    try:
        prompt = judge_prompts.load_prompt(crit, prompt_dir, version)
        results_file = runs_dir / run_id / runner.RESULTS_FILE
        results = {r.case_id: r for r in read_jsonl(results_file, CaseResult)}
        cases = {c.id: c for c in read_jsonl(dataset, TestCase)}
        split = split_mod.read_split(split_path)
    except (OSError, ValueError) as e:
        console.print(f"[red]cannot prepare the judge:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    if prompt != judge_prompts.build_prompt(rubric, rhash, split, version):
        path = judge_prompts.prompt_path(crit, prompt_dir, version)
        console.print(
            f"[red]{path} is out of date with the rubric:[/] run "
            f"`evalkit judge prompt --criterion {criterion} --prompt-version {version}`"
        )
        raise typer.Exit(runner.EXIT_ABORTED)
    if provider not in judge_llm.PROVIDERS:
        console.print(f"[red]unknown provider {provider!r}: groq or openai[/]")
        raise typer.Exit(runner.EXIT_ABORTED)
    model_cfg = judge_client.model_config_for(provider, model)
    cfg = judge_runner.JudgeConfig(crit, model_cfg, prompt, rhash, version)
    return cfg, rubric, split, results, cases


def _provider_tag(provider: str) -> str:
    """Results and reports of a second-opinion judge are named apart from the primary's."""
    return "" if provider == "groq" else f"_{provider}"


@judge_app.command("run")
def judge_run(
    criterion: Annotated[str, typer.Option(help="Criterion, e.g. relevance.")],
    run_id: Annotated[str, typer.Option("--run", help="Saved agent run.")] = DAY7_RUN,
    split_name: Annotated[str, typer.Option("--split", help="dev (test is locked).")] = "dev",
    dry_run: Annotated[bool, typer.Option(help="Count calls and tokens; call nothing.")] = False,
    max_calls: Annotated[int, typer.Option(min=0, help="Hard cap on API calls.")] = 0,
    daily_cap: Annotated[int, typer.Option(min=0, help="Cap on today's calls (ledger).")] = 200,
    consistency: Annotated[
        int, typer.Option(min=0, help="Instead: re-judge N dev cases with the cache bypassed.")
    ] = 0,
    limit: Annotated[
        int, typer.Option(min=0, help="Smoke test: only the first N dev cases (sorted).")
    ] = 0,
    only: Annotated[
        str | None, typer.Option(help="Only these dev case_ids (comma-separated, or @file).")
    ] = None,
    allow_unfrozen: Annotated[
        bool, typer.Option(help="Run a prompt version other than the frozen one.")
    ] = False,
    provider: Annotated[
        str, typer.Option(help="groq (primary judge) or openai (second opinion only).")
    ] = "groq",
    model: Annotated[
        str | None, typer.Option(help="Model id (default: qwen/qwen3.8-27b, gpt-5-mini).")
    ] = None,
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
    dataset: Annotated[Path, typer.Option(help="Dataset version.")] = LABEL_DATASET,
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
    split_path: Annotated[Path, typer.Option("--split-file", help="Split file.")] = SPLIT_OUT,
    prompt_dir: Annotated[Path, typer.Option(help="Prompt folder.")] = judge_prompts.PROMPT_DIR,
    ledger_path: Annotated[Path, typer.Option("--ledger", help="Usage ledger.")] = (
        judge_client.USAGE_LEDGER
    ),
    cache_dir: Annotated[Path, typer.Option(help="Judge cache.")] = judge_runner.CACHE_DIR,
    stop_on_refusal: Annotated[
        bool, typer.Option(help="Stop at the first refused request (no automatic retry).")
    ] = True,
    prompt_version: Annotated[str, typer.Option(help="Judge prompt version.")] = (
        judge_prompts.PROMPT_VERSION
    ),
) -> None:
    """Judge one criterion on the dev split of a saved run (Groq; --dry-run first)."""
    frozen = judge_frozen.frozen_version(Criterion(criterion), prompt_dir)
    if frozen is not None and prompt_version != frozen and not allow_unfrozen:
        console.print(
            f"[red]{criterion}'s judge prompt is frozen at {frozen}[/] (prompts/judges/"
            f"{judge_frozen.FROZEN_FILE}); run --prompt-version {frozen}, or pass "
            "--allow-unfrozen to re-run an older version on purpose"
        )
        raise typer.Exit(runner.EXIT_ABORTED)
    cfg, rubric, split, results, cases = _judge_inputs(
        criterion, run_id, runs_dir, dataset, rubric_dir, split_path, prompt_dir, model,
        prompt_version, provider,
    )  # fmt: skip
    cache = judge_runner.JudgeCache(cache_dir)
    ledger = judge_client.Ledger(ledger_path)
    try:
        plan = judge_runner.build_plan(cfg, rubric, split, split_name, results, cases, cache)
    except PermissionError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    use_cache = consistency == 0
    if only:
        raw = Path(only[1:]).read_text(encoding="utf-8") if only.startswith("@") else only
        wanted = {c.strip() for c in raw.replace(",", " ").split() if c.strip()}
        chosen = sorted(i.case_id for i in plan.items if i.case_id in wanted)
        plan = judge_runner.build_plan(
            cfg, rubric, split, split_name, results, cases, cache, only=chosen
        )
    if limit:
        first = sorted(i.case_id for i in plan.items)[:limit]
        plan = judge_runner.build_plan(
            cfg, rubric, split, split_name, results, cases, cache, only=first
        )
    if consistency:
        sample = judge_runner.consistency_sample(plan, consistency)
        plan = judge_runner.build_plan(
            cfg, rubric, split, split_name, results, cases, cache, use_cache=False, only=sample
        )
    calls = plan.calls if use_cache else len(plan.items)
    worst = calls * judge_runner.CALLS_PER_CASE_MAX
    console.print(
        f"{cfg.criterion.value} on {split_name} with {cfg.model.name}: {len(plan.items)} to "
        f"judge, {len(plan.skipped_na)} n_a (not sent), {len(plan.unanswered)} unanswered; "
        f"{plan.cached if use_cache else 0} cached -> {calls} API call(s) (up to {worst} with "
        f"repair retries), about {plan.est_tokens} tokens; calls made today: "
        f"{ledger.calls_today()} of {daily_cap}"
    )
    out_est = judge_runner.OUTPUT_TOKENS_ESTIMATE
    if provider == "openai":
        out_est = OPENAI_OUTPUT_ESTIMATE
    in_est = plan.est_tokens - calls * judge_runner.OUTPUT_TOKENS_ESTIMATE
    cost = judge_client.estimate_cost_usd(cfg.model.name, in_est, calls * out_est)
    worst_cost = judge_client.estimate_cost_usd(
        cfg.model.name, 2 * in_est, worst * cfg.model.max_tokens
    )
    if cost is not None and worst_cost is not None:
        console.print(
            f"estimated cost: about ${cost:.3f} ({in_est} input + {calls * out_est} output "
            f"tokens at approximate list prices); worst case with every repair and the full "
            f"output budget: ${worst_cost:.2f}"
        )
    elif provider == "groq":
        console.print("cost: none (Groq free tier, not billed)")
    if dry_run:
        console.print("[cyan]dry run[/]: no call made")
        return
    try:  # before any client exists: a refused budget never touches the provider
        judge_runner.check_budget(calls, max_calls, ledger, daily_cap)
    except judge_runner.BudgetExceeded as e:
        console.print(f"[red]stopped:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    load_dotenv(".env")  # lazily, as make_client does
    key = os.environ.get(judge_llm.API_KEY_ENV[cfg.model.provider], "")
    if not key and cfg.model.provider == "openai":
        key = os.environ.get("OPENAI_KEY", "")  # an alternative name some .env files use
    suffix = _provider_tag(provider) + (
        "_consistency" if consistency else ("_smoke" if limit else ("_subset" if only else ""))
    )
    name = f"{cfg.criterion.value}_{cfg.version}{suffix}.jsonl"
    out = runs_dir / run_id / "judges" / name
    replies = {cid: r.response for cid, r in results.items() if r.response is not None}

    async def go() -> tuple[list[judge_schema.JudgeResult], int, judge_client.JudgeClient]:
        client = judge_client.client_for(
            cfg.model.provider, key, ledger=ledger, purpose=f"{criterion}_{cfg.version}{suffix}",
            max_retries=0 if stop_on_refusal else judge_llm.MAX_RETRIES,
        )  # fmt: skip
        try:
            got, made = await judge_runner.run_judge(
                client, cfg, plan, replies, cache, max_calls=max_calls, daily_cap=daily_cap,
                ledger=ledger, use_cache=use_cache, write_cache=use_cache,
                stop_on_refusal=stop_on_refusal,
            )  # fmt: skip
        finally:
            await client.aclose()
        return got, made, client

    try:
        got, made, client = asyncio.run(go())
    except (judge_runner.BudgetExceeded, judge_runner.CallRefused,
            judge_llm.GenerationError) as e:  # fmt: skip
        console.print(f"[red]stopped:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    write_jsonl(out, got)
    errors = sum(r.verdict == "judge_error" for r in got)
    console.print(
        f"[green]wrote[/] {out}: {len(got)} results, {made} API call(s), {errors} "
        f"judge_error; thinking: {client.thinking}"
    )
    if client.limiter.limits:
        limits = ", ".join(f"{k}={v}" for k, v in sorted(client.limiter.limits.items()))
        console.print(f"rate limits: {limits}")


@judge_app.command("agree")
def judge_agree(
    criterion: Annotated[str, typer.Option(help="Criterion, e.g. relevance.")],
    run_id: Annotated[str, typer.Option("--run", help="Saved agent run.")] = DAY7_RUN,
    split_name: Annotated[str, typer.Option("--split", help="dev (test is locked).")] = "dev",
    labels_path: Annotated[Path, typer.Option("--labels", help="Human labels.")] = JUDGE_LABELS,
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
    split_path: Annotated[Path, typer.Option("--split-file", help="Split file.")] = SPLIT_OUT,
    out_dir: Annotated[Path, typer.Option(help="Report folder.")] = judge_report.REPORT_DIR,
    prompt_version: Annotated[str, typer.Option(help="Judge prompt version.")] = (
        judge_prompts.PROMPT_VERSION
    ),
    compare_with: Annotated[
        str | None, typer.Option(help="An earlier prompt version to compare side by side.")
    ] = None,
    provider: Annotated[str, typer.Option(help="Whose results: groq or openai.")] = "groq",
    vs_provider: Annotated[
        str | None, typer.Option(help="Add a judge-vs-judge section against this provider.")
    ] = None,
    vs_version: Annotated[
        str | None, typer.Option(help="The other judge's prompt version (default: the same).")
    ] = None,
    dataset: Annotated[Path, typer.Option(help="Dataset version.")] = LABEL_DATASET,
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
    prompt_dir: Annotated[Path, typer.Option(help="Prompt folder.")] = judge_prompts.PROMPT_DIR,
    cache_dir: Annotated[Path, typer.Option(help="Judge cache.")] = judge_runner.CACHE_DIR,
) -> None:
    """The judge vs the human labels on dev -> reports/judges/<criterion>_dev_<version>.md."""
    if split_name != "dev":
        console.print("[red]judges are compared on the dev split only; test is locked[/]")
        raise typer.Exit(runner.EXIT_ABORTED)
    crit = Criterion(criterion)
    tag = _provider_tag(provider)
    base = runs_dir / run_id / "judges" / f"{crit.value}_{prompt_version}{tag}"
    again_path = Path(f"{base}_consistency.jsonl")
    try:
        got = list(read_jsonl(Path(f"{base}.jsonl"), judge_schema.JudgeResult))
        again = (
            list(read_jsonl(again_path, judge_schema.JudgeResult)) if again_path.exists() else []
        )
        labels = load_labels("dev", split_path=split_path, labels_path=labels_path)
        old_path = runs_dir / run_id / "judges" / f"{crit.value}_{compare_with}{tag}.jsonl"
        old = list(read_jsonl(old_path, judge_schema.JudgeResult)) if compare_with else []
    except (OSError, ValueError) as e:
        console.print(f"[red]cannot build the report:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    compare = (compare_with, old, prompt_version) if compare_with else None
    versus = None
    if vs_provider is not None:
        other_version = vs_version or prompt_version
        other = _other_judge_results(
            criterion, run_id, runs_dir, dataset, rubric_dir, split_path, prompt_dir,
            cache_dir, other_version, vs_provider,
        )  # fmt: skip
        this_name = f"{got[0].model if got else provider} {prompt_version}"
        other_name = f"{other[0].model if other else vs_provider} {other_version}"
        versus = (other_name, other, this_name)
    try:
        cases_for_source = read_jsonl(dataset, TestCase)
        sources = {c.id: route_report.SOURCE_LABELS[c.source] for c in cases_for_source}
    except (OSError, ValueError):
        sources = None
    text = judge_report.build_report(
        crit, got, labels, labels_file=labels_path.name, consistency=again, compare=compare,
        versus=versus, sources=sources,
    )  # fmt: skip
    out = out_dir / f"{crit.value}_dev_{prompt_version}{tag}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    a = judge_report.agreement(got, labels, crit)
    kappa = "undefined" if a.kappa.value is None else f"{a.kappa.value:.2f}"
    console.print(
        f"{crit.value}: n={a.confusion.n}, agreement "
        f"{100 * (a.confusion.percent_agreement() or 0):.1f}%, kappa {kappa}"
    )
    console.print(f"[green]wrote[/] {out}")


def _other_judge_results(
    criterion: str,
    run_id: str,
    runs_dir: Path,
    dataset: Path,
    rubric_dir: Path,
    split_path: Path,
    prompt_dir: Path,
    cache_dir: Path,
    version: str,
    provider: str,
) -> list[judge_schema.JudgeResult]:
    """Another judge's dev results: its results file if the run finished, else whatever its
    cache holds (a run that stopped early). Never calls a model."""
    path = runs_dir / run_id / "judges" / f"{criterion}_{version}{_provider_tag(provider)}.jsonl"
    if path.exists():
        return list(read_jsonl(path, judge_schema.JudgeResult))
    cfg, rubric, split, results, cases = _judge_inputs(
        criterion, run_id, runs_dir, dataset, rubric_dir, split_path, prompt_dir, None,
        version, provider,
    )  # fmt: skip
    cache = judge_runner.JudgeCache(cache_dir)
    plan = judge_runner.build_plan(cfg, rubric, split, "dev", results, cases, cache)
    return judge_runner.cached_results(plan, cache)


@judge_app.command("freeze")
def judge_freeze(
    prompt_dir: Annotated[Path, typer.Option(help="Prompt folder.")] = judge_prompts.PROMPT_DIR,
) -> None:
    """Write prompts/judges/FROZEN.json: the final prompt version per criterion and its hashes."""
    path = judge_frozen.write_frozen(prompt_dir)
    data = judge_frozen.read_frozen(prompt_dir) or {}
    for name, item in data.get("criteria", {}).items():
        console.print(f"{name}: {item['version']} ({item['prompt_sha256'][:12]})")
    console.print(f"[green]wrote[/] {path}")


# --------------------------------------------------------------------------- negatives

negatives_app = typer.Typer(help="Synthetic negatives for the frozen judges.", no_args_is_help=True)
app.add_typer(negatives_app, name="negatives")
NEGATIVES_PATH = Path("datasets") / negatives_mod.DATASET_FILE
NEGATIVES_LABELS = Path("datasets") / "labels_v1.3.jsonl"

NegativesInputs = tuple[
    dict[str, LabelSample], dict[str, Label], list[str], dict[Criterion, rubrics.Rubric]
]


def _negatives_inputs(
    sample_path: Path, labels_path: Path, split_path: Path, rubric_dir: Path
) -> NegativesInputs:
    try:
        samples = {s.case_id: s for s in _load_labeling(sample_path)}
        labels = load_labels("dev", split_path=split_path, labels_path=labels_path)
        dev = split_mod.read_split(split_path).ids("dev")
        loaded = {r.criterion: r for r in rubrics.load_rubrics(rubric_dir, None)}
    except (OSError, ValueError, rubrics.RubricError) as e:
        console.print(f"[red]cannot read the inputs:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    return samples, labels, dev, loaded


@negatives_app.command("make")
def negatives_make(
    out: Annotated[Path, typer.Option(help="Synthetic negatives file.")] = NEGATIVES_PATH,
    sample_path: Annotated[Path, typer.Option("--sample", help="Labeled sample.")] = SAMPLE_PATH,
    labels_path: Annotated[Path, typer.Option("--labels", help="Human labels.")] = NEGATIVES_LABELS,
    split_path: Annotated[Path, typer.Option("--split-file", help="Split file.")] = SPLIT_OUT,
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
    seed: Annotated[int, typer.Option(help="Random seed.")] = negatives_mod.SEED,
) -> None:
    """Template edits of dev replies (and their controls) -> datasets/synthetic_negatives_v1."""
    samples, labels, dev, loaded = _negatives_inputs(
        sample_path, labels_path, split_path, rubric_dir
    )
    records = negatives_mod.make_negatives(
        samples, labels, dev, loaded, judge_frozen.FINAL_VERSIONS, seed=seed
    )
    write_jsonl(out, records)
    negs = [r for r in records if r.kind == "negative"]
    by = Counter(r.criterion.value for r in negs)
    spec = DatasetSpec(
        version="1",
        kind="synthetic_negatives",
        sources=[sample_path.name, labels_path.name],
        description="SYNTHETIC: template edits of dev replies (negatives) and the unedited "
        "replies (controls), to test the frozen judges; never human labels (Day 18)",
        metadata={
            "seed": seed,
            "negatives": len(negs),
            "controls": len(records) - len(negs),
            "by_criterion": dict(sorted(by.items())),
            "split": "dev only",
            "synthetic": True,
        },
    )
    upsert_entry(out.parent, out.name, spec)
    counts = ", ".join(f"{c} {n}" for c, n in sorted(by.items()))
    console.print(f"{len(negs)} negatives + {len(records) - len(negs)} controls: {counts}")
    console.print(f"[green]wrote[/] {out} (and its MANIFEST.json entry)")


def _negatives_results_path(runs_dir: Path, run_id: str, provider: str) -> Path:
    name = f"negatives_v1{_provider_tag(provider)}.jsonl"
    return runs_dir / run_id / "judges" / "synthetic" / name


@negatives_app.command("judge")
def negatives_judge(
    provider: Annotated[str, typer.Option(help="groq (primary) or openai (second opinion).")],
    data: Annotated[Path, typer.Option(help="Synthetic negatives file.")] = NEGATIVES_PATH,
    run_id: Annotated[str, typer.Option("--run", help="Saved agent run.")] = DAY7_RUN,
    dry_run: Annotated[bool, typer.Option(help="Count calls and cost; call nothing.")] = False,
    max_calls: Annotated[
        int, typer.Option(min=0, help="Hard cap on API calls (all criteria).")
    ] = 0,
    daily_cap: Annotated[int, typer.Option(min=0, help="Cap on today's calls (ledger).")] = 200,
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
    dataset: Annotated[Path, typer.Option(help="Dataset version.")] = LABEL_DATASET,
    sample_path: Annotated[Path, typer.Option("--sample", help="Labeled sample.")] = SAMPLE_PATH,
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
    split_path: Annotated[Path, typer.Option("--split-file", help="Split file.")] = SPLIT_OUT,
    prompt_dir: Annotated[Path, typer.Option(help="Prompt folder.")] = judge_prompts.PROMPT_DIR,
    ledger_path: Annotated[Path, typer.Option("--ledger", help="Usage ledger.")] = (
        judge_client.USAGE_LEDGER
    ),
    cache_dir: Annotated[Path, typer.Option(help="Judge cache.")] = judge_runner.CACHE_DIR,
) -> None:
    """Run the FROZEN judges on the synthetic negatives (--dry-run first)."""
    try:
        records = list(read_jsonl(data, negatives_mod.SyntheticRecord))
        samples = {s.case_id: s for s in _load_labeling(sample_path)}
    except (OSError, ValueError) as e:
        console.print(f"[red]cannot read the synthetic negatives:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    cache = judge_runner.JudgeCache(cache_dir)
    ledger = judge_client.Ledger(ledger_path)
    plans: list[tuple[judge_runner.JudgeConfig, judge_runner.Plan]] = []
    for crit in sorted({r.criterion for r in records}, key=lambda c: c.value):
        version = judge_frozen.frozen_version(crit, prompt_dir)
        if version is None:
            console.print(f"[red]{crit.value} has no frozen judge prompt[/]")
            raise typer.Exit(runner.EXIT_ABORTED)
        cfg = _judge_inputs(
            crit.value, run_id, runs_dir, dataset, rubric_dir, split_path, prompt_dir, None,
            version, provider,
        )[0]  # fmt: skip
        plans.append((cfg, synthetic_eval.build_items(cfg, records, samples, cache)))
    calls = sum(p.calls for _, p in plans)
    model = plans[0][0].model.name if plans else "-"
    est_in = sum(p.est_tokens for _, p in plans) - calls * judge_runner.OUTPUT_TOKENS_ESTIMATE
    per_call = judge_runner.OUTPUT_TOKENS_ESTIMATE
    if provider == "openai":
        per_call = OPENAI_OUTPUT_ESTIMATE
    cost = judge_client.estimate_cost_usd(model, est_in, calls * per_call)
    for cfg, plan in plans:
        console.print(
            f"{cfg.criterion.value} ({cfg.version}): {len(plan.items)} records, "
            f"{plan.cached} cached, {plan.calls} call(s)"
        )
    priced = f", estimated cost about ${cost:.3f}" if cost is not None else ""
    console.print(
        f"total: {calls} API call(s) with {model}{priced}; calls made today: "
        f"{ledger.calls_today()} of {daily_cap}"
    )
    if dry_run:
        console.print("[cyan]dry run[/]: no call made")
        return
    try:
        judge_runner.check_budget(calls, max_calls, ledger, daily_cap)
    except judge_runner.BudgetExceeded as e:
        console.print(f"[red]stopped:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    load_dotenv(".env")
    key = os.environ.get(judge_llm.API_KEY_ENV[plans[0][0].model.provider], "")
    if not key and provider == "openai":
        key = os.environ.get("OPENAI_KEY", "")
    replies = {r.id: synthetic_eval.edited_response(r, samples[r.source_case_id]) for r in records}

    async def go() -> list[judge_schema.JudgeResult]:
        got: list[judge_schema.JudgeResult] = []
        left = max_calls
        for cfg, plan in plans:
            purpose = f"negatives_{cfg.criterion.value}_{cfg.version}{_provider_tag(provider)}"
            client = judge_client.client_for(
                cfg.model.provider, key, ledger=ledger, purpose=purpose, max_retries=0
            )
            try:
                res, made = await judge_runner.run_judge(
                    client, cfg, plan, replies, cache, max_calls=left, daily_cap=daily_cap,
                    ledger=ledger, stop_on_refusal=True,
                )  # fmt: skip
            finally:
                await client.aclose()
            got += res
            left -= made
        return got

    try:
        got = asyncio.run(go())
    except (
        judge_runner.BudgetExceeded,
        judge_runner.CallRefused,
        judge_llm.GenerationError,
    ) as e:
        console.print(f"[red]stopped:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    out = _negatives_results_path(runs_dir, run_id, provider)
    ordered: list[judge_schema.JudgeResult] = sorted(got, key=lambda r: r.case_id)
    write_jsonl(out, ordered)
    errors = Counter(r.criterion for r in got if r.verdict == "judge_error")
    console.print(
        f"[green]wrote[/] {out}: {len(got)} results; judge_error by criterion: {dict(errors) or 0}"
    )


@negatives_app.command("report")
def negatives_report(
    provider: Annotated[str, typer.Option(help="Whose results: groq or openai.")],
    data: Annotated[Path, typer.Option(help="Synthetic negatives file.")] = NEGATIVES_PATH,
    run_id: Annotated[str, typer.Option("--run", help="Saved agent run.")] = DAY7_RUN,
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
    out: Annotated[Path, typer.Option(help="Report.")] = (
        judge_report.REPORT_DIR / "synthetic_negatives_v1.md"
    ),
    prompt_dir: Annotated[Path, typer.Option(help="Prompt folder.")] = judge_prompts.PROMPT_DIR,
) -> None:
    """Catch rate and false alarms per criterion and edit type -> reports/judges/."""
    path = _negatives_results_path(runs_dir, run_id, provider)
    try:
        records = list(read_jsonl(data, negatives_mod.SyntheticRecord))
        results = {r.case_id: r for r in read_jsonl(path, judge_schema.JudgeResult)}
    except (OSError, ValueError) as e:
        console.print(f"[red]cannot build the report:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    versions = {
        c: v
        for c, v in judge_frozen.FINAL_VERSIONS.items()
        if judge_frozen.frozen_version(c, prompt_dir) == v
    }
    model = next(iter(results.values())).model if results else provider
    text = synthetic_eval.report(records, results, versions, model=model)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    console.print(f"[green]wrote[/] {out}")


@judge_app.command("bias")
def judge_bias(
    provider: Annotated[str, typer.Option(help="Whose results: openai or groq.")] = "openai",
    run_id: Annotated[str, typer.Option("--run", help="Saved agent run.")] = DAY7_RUN,
    labels_path: Annotated[Path, typer.Option("--labels", help="Human labels.")] = (
        Path("datasets") / "labels_v1.3.jsonl"
    ),
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
    split_path: Annotated[Path, typer.Option("--split-file", help="Split file.")] = SPLIT_OUT,
    dataset: Annotated[Path, typer.Option(help="Dataset version.")] = LABEL_DATASET,
    prompt_dir: Annotated[Path, typer.Option(help="Prompt folder.")] = judge_prompts.PROMPT_DIR,
    out: Annotated[Path, typer.Option(help="Report.")] = (
        judge_report.REPORT_DIR / "bias_by_source_dev.md"
    ),
) -> None:
    """Judge vs human per criterion on dev, split by case source (saved results only)."""
    tag = _provider_tag(provider)
    folder = runs_dir / run_id / "judges"
    try:
        labels = load_labels("dev", split_path=split_path, labels_path=labels_path)
        cases = list(read_jsonl(dataset, TestCase))
    except (OSError, ValueError) as e:
        console.print(f"[red]cannot build the bias report:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    sources = {c.id: route_report.SOURCE_LABELS[c.source] for c in cases}
    entries = []
    for crit in judge_frozen.FINAL_VERSIONS:
        version = judge_frozen.frozen_version(crit, prompt_dir) or judge_frozen.FINAL_VERSIONS[crit]
        full = folder / f"{crit.value}_{version}{tag}.jsonl"
        subset = folder / f"{crit.value}_{version}{tag}_subset.jsonl"
        path, note = (full, "") if full.exists() else (subset, "subset")
        if not path.exists():
            console.print(f"[yellow]{crit.value} {version}: no saved results; skipped[/]")
            continue
        results = [r for r in read_jsonl(path, judge_schema.JudgeResult) if r.case_id in labels]
        n_note = f"{note}, {len(results)} of 96" if note else ""
        entries.append((crit, version, results, n_note))
    model = next((r.model for _, _, rs, _ in entries for r in rs), provider)
    lines = [
        "# Judge bias check: agreement by case source (dev split)",
        "",
        f"- **Judge:** `{model}` on the frozen prompts; saved results only, no new judge calls.",
        f"- **Human labels:** `{labels_path.name}` (hayyan), dev split.",
        "- **Why:** OpenAI generated the synthetic cases (Day 5) and is also the judge, so it "
        "might score its own family's cases differently. **non-OpenAI** = legacy + known_bugs "
        "cases; **synthetic** = OpenAI-generated.",
        f"- **Flag:** an AC1 gap above {judge_report.GAP_AC1} or an agreement gap above "
        f"{int(100 * judge_report.GAP_AGREEMENT)} points (gap = non-OpenAI minus synthetic). "
        "With few cases or few fails per side, a gap can be noise: read it with n.",
        "",
        *judge_report.bias_lines(entries, labels, sources),
        "",
        "## Human fails by source (the confound)",
        "",
        "Where the human fails sit decides what each side can show: a side with no fails can "
        "only show agreement on passes.",
        "",
        "| Criterion | non-OpenAI: human fails / judged | synthetic: human fails / judged |",
        "|---|---|---|",
        *judge_report.human_fail_lines(list(judge_frozen.FINAL_VERSIONS), labels, sources),
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    console.print(f"[green]wrote[/] {out}")


JudgePlan = tuple[judge_runner.JudgeConfig, judge_runner.Plan, dict[str, CaseResult]]


def _print_plans(plans: list[JudgePlan]) -> tuple[int, float]:
    """One line per judge plan (OpenAI cost estimate); returns the total calls and cost."""
    calls, total = 0, 0.0
    for cfg, plan, _ in plans:
        in_est = plan.est_tokens - plan.calls * judge_runner.OUTPUT_TOKENS_ESTIMATE
        out_est = plan.calls * OPENAI_OUTPUT_ESTIMATE
        cost = judge_client.estimate_cost_usd(cfg.model.name, in_est, out_est) or 0.0
        calls, total = calls + plan.calls, total + cost
        console.print(
            f"{cfg.criterion.value} {cfg.version}: {len(plan.items)} to judge, "
            f"{len(plan.skipped_na)} n_a, {len(plan.unanswered)} unanswered, {plan.cached} "
            f"cached -> {plan.calls} call(s), about ${cost:.3f}"
        )
    return calls, total


def _check_spend(
    calls: int, cost: float, max_calls: int, max_usd: float, ledger: judge_client.Ledger,
    daily_cap: int,
) -> None:  # fmt: skip
    if cost > max_usd:
        console.print(f"[red]stopped:[/] about ${cost:.3f} is above --max-usd {max_usd}")
        raise typer.Exit(runner.EXIT_ABORTED)
    try:
        judge_runner.check_budget(calls, max_calls, ledger, daily_cap)
    except judge_runner.BudgetExceeded as e:
        console.print(f"[red]stopped:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None


def _run_judge_plans(
    plans: list[JudgePlan],
    *,
    cache: judge_runner.JudgeCache,
    ledger: judge_client.Ledger,
    max_calls: int,
    daily_cap: int,
    folder: Path,
    max_errors: int,
    purpose: str,
    stop_note: str = "",
) -> tuple[dict[Criterion, list[judge_schema.JudgeResult]], int, float]:
    """Run the OpenAI judge plans in order, saving each to ``folder``; stop at a refusal,
    the budget or more than ``max_errors`` judge_errors. Returns the results, the calls
    made and the approximate cost of the fresh (non-cached) verdicts."""
    load_dotenv(".env")
    key = os.environ.get(judge_llm.API_KEY_ENV["openai"], "") or os.environ.get("OPENAI_KEY", "")
    got_all: dict[Criterion, list[judge_schema.JudgeResult]] = {}
    made_total = 0
    for cfg, plan, results in plans:
        replies = {cid: r.response for cid, r in results.items() if r.response is not None}
        tag = f"{purpose}_{cfg.criterion.value}_{cfg.version}"

        async def go(
            cfg: judge_runner.JudgeConfig = cfg,
            plan: judge_runner.Plan = plan,
            replies: dict[str, AgentResponse] = replies,
            tag: str = tag,
            left: int = max_calls - made_total,
        ) -> tuple[list[judge_schema.JudgeResult], int]:
            client = judge_client.client_for("openai", key, ledger=ledger, purpose=tag,
                                             max_retries=0)  # fmt: skip
            try:
                return await judge_runner.run_judge(
                    client, cfg, plan, replies, cache, max_calls=left,
                    daily_cap=daily_cap, ledger=ledger, stop_on_refusal=True,
                )  # fmt: skip
            finally:
                await client.aclose()

        try:
            got, made = asyncio.run(go())
        except (judge_runner.BudgetExceeded, judge_runner.CallRefused,
                judge_llm.GenerationError) as e:  # fmt: skip
            console.print(f"[red]stopped:[/] {escape(str(e))}{stop_note}")
            raise typer.Exit(runner.EXIT_ABORTED) from None
        made_total += made
        write_jsonl(folder / f"{cfg.criterion.value}_{cfg.version}_openai.jsonl", got)
        got_all[cfg.criterion] = got
        errors = sum(r.verdict == "judge_error" for g in got_all.values() for r in g)
        console.print(f"{cfg.criterion.value}: {len(got)} results, {made} call(s), "
                      f"{errors} judge_error so far")  # fmt: skip
        if errors > max_errors:
            console.print(f"[red]stopped:[/] more than {max_errors} judge_error{stop_note}")
            raise typer.Exit(runner.EXIT_ABORTED)
    fresh = [r for g in got_all.values() for r in g if not r.cache_hit]
    cost = judge_client.estimate_cost_usd(
        judge_client.OPENAI_JUDGE_MODEL,
        sum(r.input_tokens for r in fresh),
        sum(r.output_tokens + r.reasoning_tokens for r in fresh),
    )
    return got_all, made_total, cost or 0.0


@judge_app.command("sample-cases")
def judge_sample_cases(
    run_id: Annotated[str, typer.Option("--run", help="Saved agent run.")] = DAY7_RUN,
    n: Annotated[int, typer.Option(min=1, help="Cases to sample.")] = 120,
    seed: Annotated[int, typer.Option(help="Sampling seed.")] = 42,
    dataset: Annotated[Path, typer.Option(help="Dataset version.")] = LABEL_DATASET,
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
    out: Annotated[Path, typer.Option(help="Sample file (never overwritten).")] = (
        Path("datasets") / full_mod.JUDGE_SAMPLE_FILE
    ),
) -> None:
    """A seeded judge sample of answered cases, stratified by expected route x source."""
    if out.exists():
        console.print(f"[red]{out} exists[/]: a judge sample is versioned; write a new file")
        raise typer.Exit(runner.EXIT_ABORTED)
    try:
        results = list(read_jsonl(runs_dir / run_id / runner.RESULTS_FILE, CaseResult))
        cases = {c.id: c for c in read_jsonl(dataset, TestCase)}
        answered = [r.case_id for r in results if r.response is not None and r.response.ok]
        ids = full_mod.judge_sample(cases, answered, n, seed)
    except (OSError, ValueError) as e:
        console.print(f"[red]cannot sample:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    strata = Counter(f"{full_mod.group(cases[c], 'route')}/{full_mod.group(cases[c], 'source')}"
                     for c in ids)  # fmt: skip
    doc = {
        "name": out.stem, "n": len(ids), "seed": seed, "stratified_by": "expected_route x source",
        "from": f"answered cases of run {run_id}", "dataset": dataset.name,
        "dataset_sha256": file_sha256(dataset), "strata": dict(sorted(strata.items())),
        "case_ids": ids,
    }  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    console.print(f"[green]wrote[/] {out}: {len(ids)} of {len(answered)} answered cases")


@judge_app.command("final")
def judge_final(
    run_id: Annotated[str, typer.Option("--run", help="Saved agent run.")] = DAY7_RUN,
    dry_run: Annotated[bool, typer.Option(help="Count calls and cost; call nothing.")] = False,
    max_calls: Annotated[int, typer.Option(min=0, help="Hard cap on API calls.")] = 0,
    max_usd: Annotated[float, typer.Option(min=0, help="Refuse if the estimate is above.")] = 0.0,
    daily_cap: Annotated[int, typer.Option(min=0, help="Cap on today's calls (ledger).")] = 200,
    reason: Annotated[str, typer.Option(help="Logged with the test access.")] = (
        judge_final_mod.FINAL_REASON
    ),
    labels_path: Annotated[Path, typer.Option("--labels", help="Human labels.")] = (
        Path("datasets") / "labels_v1.3.jsonl"
    ),
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
    dataset: Annotated[Path, typer.Option(help="Dataset version.")] = LABEL_DATASET,
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
    split_path: Annotated[Path, typer.Option("--split-file", help="Split file.")] = SPLIT_OUT,
    sample_path: Annotated[Path, typer.Option("--sample", help="Labeled sample.")] = SAMPLE_PATH,
    prompt_dir: Annotated[Path, typer.Option(help="Prompt folder.")] = judge_prompts.PROMPT_DIR,
    ledger_path: Annotated[Path, typer.Option("--ledger", help="Usage ledger.")] = (
        judge_client.USAGE_LEDGER
    ),
    cache_dir: Annotated[Path, typer.Option(help="Judge cache.")] = judge_runner.CACHE_DIR,
    access_log: Annotated[Path, typer.Option(help="Test access log.")] = ACCESS_LOG,
    out: Annotated[Path, typer.Option(help="Report.")] = (
        judge_report.REPORT_DIR / judge_final_mod.REPORT_FILE
    ),
) -> None:
    """The ONE frozen-judge evaluation on the locked test split (OpenAI; --dry-run first)."""
    used = judge_frozen.used_record(prompt_dir)
    if used:
        console.print(
            f"[red]the test split was already used[/] ({used.get('date')}, "
            f"{used.get('reason')}): a new evaluation needs a new test set"
        )
        raise typer.Exit(runner.EXIT_ABORTED)
    problems = judge_frozen.frozen_problems(prompt_dir)
    try:
        split = split_mod.read_split(split_path)
        samples = {s.case_id: s for s in _load_labeling(sample_path)}
    except (OSError, ValueError) as e:
        console.print(f"[red]cannot check the split:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    found = leakage.markers(split, samples, rubric_dir)
    leaks = leakage.scan(leakage.files_to_scan([*leakage.SCAN_DIRS, prompt_dir]), found)
    problems += [f"leak: {hit}" for hit in leaks]
    for p in problems:
        console.print(f"[red]{escape(p)}[/]")
    if problems:
        console.print("[red]aborted:[/] FROZEN.json or the leakage check failed")
        raise typer.Exit(runner.EXIT_ABORTED)
    console.print("[green]FROZEN.json matches the prompt files; no test data in prompts[/]")
    if not dry_run and os.environ.get(FINAL_ENV) != "1":
        console.print(f"[red]set {FINAL_ENV}=1[/]: the test labels are loaded after judging")
        raise typer.Exit(runner.EXIT_ABORTED)
    cache = judge_runner.JudgeCache(cache_dir)
    ledger = judge_client.Ledger(ledger_path)
    plans: list[JudgePlan] = []
    for crit, version in judge_frozen.FINAL_VERSIONS.items():
        cfg, rubric, split, results, cases = _judge_inputs(
            crit.value, run_id, runs_dir, dataset, rubric_dir, split_path, prompt_dir, None,
            version, "openai",
        )  # fmt: skip
        plan = judge_runner.build_plan(cfg, rubric, split, "test", results, cases, cache,
                                       final=True)  # fmt: skip
        plans.append((cfg, plan, results))
    calls, total_cost = _print_plans(plans)
    console.print(
        f"test split ({len(split.ids('test'))} cases), 6 frozen judges: {calls} API call(s) "
        f"(up to {calls * judge_runner.CALLS_PER_CASE_MAX} with repairs), about "
        f"${total_cost:.3f}; calls made today: {ledger.calls_today()} of {daily_cap}"
    )
    if dry_run:
        console.print("[cyan]dry run[/]: no call made, no test label loaded")
        return
    _check_spend(calls, total_cost, max_calls, max_usd, ledger, daily_cap)
    folder = runs_dir / run_id / "judges" / judge_final_mod.RESULTS_DIR
    got_all, made_total, cost = _run_judge_plans(
        plans, cache=cache, ledger=ledger, max_calls=max_calls, daily_cap=daily_cap,
        folder=folder, max_errors=judge_final_mod.MAX_JUDGE_ERRORS, purpose="final_test",
        stop_note="; no test label loaded",
    )  # fmt: skip
    try:
        labels = load_labels("test", final=True, reason=reason, split_path=split_path,
                             labels_path=labels_path, access_log=access_log)  # fmt: skip
        dev_labels = load_labels("dev", split_path=split_path, labels_path=labels_path)
        sources = {c.id: route_report.SOURCE_LABELS[c.source]
                   for c in read_jsonl(dataset, TestCase)}  # fmt: skip
    except (OSError, ValueError) as e:
        console.print(f"[red]cannot load the labels:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    entries = []
    for cfg, plan, _ in plans:
        dev_path = (
            runs_dir / run_id / "judges" / f"{cfg.criterion.value}_{cfg.version}_openai.jsonl"
        )
        dev = list(read_jsonl(dev_path, judge_schema.JudgeResult)) if dev_path.exists() else []
        entries.append(judge_final_mod.FinalEntry(
            cfg.criterion, cfg.version, got_all[cfg.criterion], dev, len(plan.skipped_na)
        ))  # fmt: skip
    date = datetime.now(UTC).isoformat(timespec="seconds")
    commit = git_commit()
    meta = {
        "run_id": run_id, "labels_file": labels_path.name, "n_test": len(labels),
        "reason": reason, "calls": made_total, "cached": sum(r.cache_hit for r in
        (x for g in got_all.values() for x in g)), "cost_usd": cost, "max_usd": max_usd,
        "max_calls": max_calls, "date": date, "commit": commit[:12],
    }  # fmt: skip
    text = judge_final_mod.build_final_report(entries, labels, dev_labels, sources, meta)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    judge_frozen.mark_test_used(
        {
            "date": date,
            "commit": commit,
            "reason": reason,
            "run_id": run_id,
            "judge": judge_client.OPENAI_JUDGE_MODEL,
            "report": out.as_posix(),
            "calls": made_total,
            "approx_cost_usd": round(cost, 4),
            "note": "The test split is USED by the one final frozen-judge evaluation: any "
            "future judge prompt change needs a new test set.",
        },
        prompt_dir,
    )
    for entry in entries:
        a = judge_report.agreement(entry.test, labels, entry.criterion)
        console.print(
            f"{entry.criterion.value} {entry.version}: n={a.confusion.n}, agreement "
            f"{100 * (a.confusion.percent_agreement() or 0):.1f}%, AC1 "
            f"{'–' if a.ac1.value is None else f'{a.ac1.value:.2f}'}"
        )
    console.print(f"[green]wrote[/] {out}; {made_total} call(s), about ${cost:.3f}")
    console.print(
        f"[yellow]the test split is now USED[/] (recorded in {prompt_dir / 'FROZEN.json'})"
    )


# --------------------------------------------------------------------------- full scorecard


@app.command("score-full")
def score_full(
    run_id: Annotated[str, typer.Argument(help="Saved run to score.")],
    dry_run: Annotated[bool, typer.Option(help="Count judge calls and cost; call nothing.")] = (
        False
    ),
    max_calls: Annotated[int, typer.Option(min=0, help="Hard cap on judge API calls.")] = 0,
    max_usd: Annotated[float, typer.Option(min=0, help="Refuse if the estimate is above.")] = 0.0,
    daily_cap: Annotated[int, typer.Option(min=0, help="Cap on today's calls (ledger).")] = 200,
    mlflow_log: Annotated[bool, typer.Option("--mlflow/--no-mlflow", help="Log to MLflow.")] = (
        True
    ),
    dataset: Annotated[Path, typer.Option(help="Dataset version to score against.")] = (
        LABEL_DATASET
    ),
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
    allowlist: Annotated[Path, typer.Option(help="voice_third_person allowlist.")] = (
        voice_mod.ALLOWLIST_FILE
    ),
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
    split_path: Annotated[Path, typer.Option("--split-file", help="Split file.")] = SPLIT_OUT,
    prompt_dir: Annotated[Path, typer.Option(help="Prompt folder.")] = judge_prompts.PROMPT_DIR,
    ledger_path: Annotated[Path, typer.Option("--ledger", help="Usage ledger.")] = (
        judge_client.USAGE_LEDGER
    ),
    cache_dir: Annotated[Path, typer.Option(help="Judge cache.")] = judge_runner.CACHE_DIR,
    tracking_dir: Annotated[Path, typer.Option(help="MLflow folder.")] = tracking_mod.TRACKING_DIR,
    judge_sample: Annotated[
        Path | None, typer.Option(help="Judge only these cases (a judge sample file).")
    ] = None,
    resamples: Annotated[int, typer.Option(min=1, help="Bootstrap resamples.")] = 2000,
    seed: Annotated[int, typer.Option(help="Bootstrap seed.")] = 42,
) -> None:
    """Deterministic metrics on every case + the six frozen OpenAI judges (every case, or a
    --judge-sample) -> scores.jsonl, summary.json and an MLflow run (--dry-run first)."""
    problems = judge_frozen.frozen_problems(prompt_dir)
    for p in problems:
        console.print(f"[red]{escape(p)}[/]")
    if problems:
        console.print("[red]aborted:[/] the judge prompts don't match FROZEN.json")
        raise typer.Exit(runner.EXIT_ABORTED)
    run_dir = runs_dir / run_id
    try:
        results = list(read_jsonl(run_dir / runner.RESULTS_FILE, CaseResult))
        cases = {c.id: c for c in read_jsonl(dataset, TestCase)}
        meta_path = run_dir / runner.META_FILE
        meta = read_json(meta_path, RunMeta) if meta_path.exists() else None
        sample = json.loads(judge_sample.read_text(encoding="utf-8")) if judge_sample else None
        judged_ids = list(sample["case_ids"]) if sample else list(cases)
        if unknown := sorted(set(judged_ids) - set(cases)):
            raise ValueError(f"{len(unknown)} judge sample case(s) not in {dataset}: {unknown[:3]}")
    except (OSError, ValueError, KeyError) as e:
        console.print(f"[red]cannot score the run:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    cache = judge_runner.JudgeCache(cache_dir)
    ledger = judge_client.Ledger(ledger_path)
    plans: list[JudgePlan] = []
    for crit, version in judge_frozen.FINAL_VERSIONS.items():
        cfg, rubric, _, by_id, _ = _judge_inputs(
            crit.value, run_id, runs_dir, dataset, rubric_dir, split_path, prompt_dir, None,
            version, "openai",
        )  # fmt: skip
        plan = judge_runner.plan_cases(cfg, rubric, judged_ids, by_id, cases, cache)
        plans.append((cfg, plan, by_id))
    calls, cost_est = _print_plans(plans)
    console.print(
        f"{len(cases)} cases ({sum(r.response is not None and r.response.ok for r in results)} "
        f"answered); 6 frozen judges on {len(judged_ids)} "
        f"{'sampled ' if sample else ''}cases: {calls} API call(s) (up to "
        f"{calls * judge_runner.CALLS_PER_CASE_MAX} with repairs), about ${cost_est:.3f}; "
        f"calls made today: {ledger.calls_today()} of {daily_cap}"
    )
    if dry_run:
        console.print("[cyan]dry run[/]: no call made")
        return
    _check_spend(calls, cost_est, max_calls, max_usd, ledger, daily_cap)
    folder = run_dir / "judges" / full_mod.JUDGES_DIR
    got_all, made, cost = _run_judge_plans(
        plans, cache=cache, ledger=ledger, max_calls=max_calls, daily_cap=daily_cap,
        folder=folder, max_errors=full_mod.MAX_JUDGE_ERRORS, purpose="score_full",
    )  # fmt: skip
    rows = scorecard_mod.score_run(results, cases, voice_mod.load_allowlist(allowlist))
    for cfg, _, _ in plans:
        rows += full_mod.judge_rows(got_all[cfg.criterion], cfg.version)
    rows.sort(key=lambda r: (r.case_id, r.metric))
    frozen_file = prompt_dir / judge_frozen.FROZEN_FILE
    provenance = {
        "run_id": run_id,
        "dataset": dataset.name,
        "dataset_sha256": file_sha256(dataset),
        "rubric_hash": rubrics.rubric_hash(rubric_dir),
        "frozen_sha256": file_sha256(frozen_file),
        "judge_model": judge_client.OPENAI_JUDGE_MODEL,
        "judge_prompts": {c.value: v for c, v in judge_frozen.FINAL_VERSIONS.items()},
        "agent_model": meta.agent_model if meta else None,
        "agent_run_git_sha": meta.git_sha if meta else None,
        "git_sha": git_commit(),
        "judge_usage": {"calls": made, "approx_cost_usd": round(cost, 4),
                        "cached": sum(r.cache_hit for g in got_all.values() for r in g)},
        "judge_sample": {"file": judge_sample.as_posix(), "sha256": file_sha256(judge_sample),
                         "n": len(judged_ids)} if judge_sample else None,
        "judge_n_a": {cfg.criterion.value: len(plan.skipped_na) for cfg, plan, _ in plans},
        "unanswered": sorted({c for _, plan, _ in plans for c in plan.unanswered}),
        "judge_errors": sorted(f"{r.criterion}:{r.case_id}" for g in got_all.values()
                               for r in g if r.verdict == "judge_error"),
    }  # fmt: skip
    summary = full_mod.summarize(rows, cases, provenance, resamples=resamples, seed=seed)
    write_jsonl(run_dir / full_mod.SCORES_FILE, rows)
    summary_path = run_dir / full_mod.SUMMARY_FILE
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8",
                            newline="\n")  # fmt: skip
    console.print(f"[green]wrote[/] {run_dir / full_mod.SCORES_FILE} ({len(rows)} rows) and "
                  f"{summary_path}; {made} judge call(s), about ${cost:.3f}")  # fmt: skip
    if mlflow_log:
        artifacts = [run_dir / full_mod.SCORES_FILE, summary_path, *sorted(folder.glob("*.jsonl"))]
        mlflow_id = tracking_mod.log_run(summary, artifacts, tracking_dir=tracking_dir)
        console.print(f"[green]MLflow[/] run {mlflow_id} in {tracking_dir} "
                      f"(view: uv run mlflow ui --backend-store-uri "
                      f"{tracking_mod.tracking_uri(tracking_dir)})")  # fmt: skip
    _print_summary(summary, "route")


def _rate_text(r: dict[str, object]) -> str:
    rate, low, high = r.get("rate"), r.get("ci_low"), r.get("ci_high")
    if not isinstance(rate, float | int):
        return "–"
    ci = (f" [{100 * low:.1f}, {100 * high:.1f}]" if isinstance(low, float | int)
          and isinstance(high, float | int) else "")  # fmt: skip
    return f"{100 * rate:.1f}%{ci}"


def _print_summary(summary: dict[str, object], by: str) -> None:
    metrics = summary.get("metrics")
    assert isinstance(metrics, dict)
    table = Table(title=f"Run {summary.get('run_id')} ({summary.get('dataset')})", box=box.SIMPLE)
    for col in ("Metric", "Passed / n", "Rate [95% CI]"):
        table.add_column(col)
    for m, r in metrics.items():
        table.add_row(m, f"{r['passed']}/{r['n']}", _rate_text(r))
    console.print(table)
    esc = summary.get("escalation")
    if isinstance(esc, dict):
        console.print(
            "escalation: "
            + ", ".join(
                f"{k} {_rate_text({**v, 'rate': v['value']})}"
                for k, v in esc.items()
                if isinstance(v, dict)
            )  # fmt: skip
            + f" (TP {esc.get('tp')}, FN {esc.get('fn')}, FP {esc.get('fp')})"
        )
    breakdowns = summary.get("breakdowns")
    if not isinstance(breakdowns, dict) or by not in breakdowns:
        return
    groups = breakdowns[by]
    # compact cells (rate and n) so the table fits a terminal; the CIs are in summary.json
    per = Table(title=f"Pass rate by {by} (n); CIs in summary.json", box=box.SIMPLE)
    per.add_column("Metric", no_wrap=True)
    for g in groups:
        per.add_column(g, no_wrap=True)
    for m in metrics:
        per.add_row(m, *(f"{100 * groups[g][m]['rate']:.0f}% ({groups[g][m]['n']})"
                         if groups[g][m]["n"] else "–" for g in groups))  # fmt: skip
    console.print(per)


def _read_summary(runs_dir: Path, run_id: str) -> dict[str, object]:
    path = runs_dir / run_id / full_mod.SUMMARY_FILE
    try:
        data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        console.print(f"[red]no full scorecard for {run_id}:[/] {escape(str(e))} (run "
                      f"`evalkit score-full {run_id}` first)")  # fmt: skip
        raise typer.Exit(runner.EXIT_ABORTED) from None
    return data


@app.command("report")
def report(
    run_id: Annotated[str, typer.Option("--run", help="A run scored with score-full.")],
    by: Annotated[str, typer.Option(help="Breakdown: route, source or difficulty.")] = "route",
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
) -> None:
    """The full scorecard of a run (summary.json) as tables."""
    if by not in full_mod.GROUPS:
        console.print(f"[red]--by must be one of {', '.join(full_mod.GROUPS)}[/]")
        raise typer.Exit(runner.EXIT_ABORTED)
    _print_summary(_read_summary(runs_dir, run_id), by)


@app.command("compare")
def compare(
    run_a: Annotated[str, typer.Argument(help="Baseline run (before).")],
    run_b: Annotated[str, typer.Argument(help="New run (after).")],
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
    resamples: Annotated[int, typer.Option(min=1, help="Bootstrap resamples.")] = 2000,
    seed: Annotated[int, typer.Option(help="Bootstrap seed.")] = 42,
) -> None:
    """Per metric: run a, run b, and b - a with a paired bootstrap 95% CI."""
    summaries = [_read_summary(runs_dir, r) for r in (run_a, run_b)]
    for key in ("dataset_sha256", "frozen_sha256", "rubric_hash"):
        if summaries[0].get(key) != summaries[1].get(key):
            console.print(f"[yellow]warning:[/] the runs differ in {key}: deltas mix changes")
    rows_a, rows_b = (list(read_jsonl(runs_dir / r / full_mod.SCORES_FILE, MetricResult))
                      for r in (run_a, run_b))  # fmt: skip
    table = Table(title=f"{run_b} vs {run_a} (b - a, paired over common cases)", box=box.SIMPLE)
    for col in ("Metric", "a", "b", "paired n", "delta [95% CI]", ""):
        table.add_column(col)
    for d in full_mod.compare(rows_a, rows_b, resamples=resamples, seed=seed):
        if d.delta is None:
            delta, mark = "–", ""
        else:
            low, high = d.ci.low, d.ci.high
            ci = (
                f" [{100 * low:+.1f}, {100 * high:+.1f}]"
                if low is not None and high is not None
                else ""
            )
            delta = f"{100 * d.delta:+.1f} pts{ci}"
            sure = low is not None and high is not None and (low > 0 or high < 0)
            mark = ("better" if d.delta > 0 else "worse") if sure else ""
        table.add_row(d.metric, _rate_text(d.a), _rate_text(d.b), str(d.n_paired), delta, mark)
    console.print(table)


# --------------------------------------------------------------------------- bias tests

bias_app = typer.Typer(help="Judge bias tests (meaning-preserving perturbations, dev only).",
                       no_args_is_help=True)  # fmt: skip
app.add_typer(bias_app, name="bias")


@bias_app.command("run")
def bias_run(
    dry_run: Annotated[bool, typer.Option(help="Count calls and cost; call nothing.")] = False,
    go: Annotated[bool, typer.Option("--go", help="Run the judges (after a dry run).")] = False,
    from_cache: Annotated[
        bool, typer.Option(help="No calls: report from cached verdicts; the rest is not run.")
    ] = False,
    missing_note: Annotated[str, typer.Option(help="Label for cells without verdicts.")] = (
        "not run"
    ),
    run_id: Annotated[str, typer.Option("--run", help="Saved agent run.")] = DAY7_RUN,
    n: Annotated[
        int, typer.Option(min=2, help="Dev cases in the sample.")
    ] = bias_run_mod.SAMPLE_SIZE,
    max_calls: Annotated[int, typer.Option(min=0, help="Hard cap on API calls.")] = 0,
    max_usd: Annotated[float, typer.Option(min=0, help="Refuse if the estimate is above.")] = 0.5,
    daily_cap: Annotated[int, typer.Option(min=0, help="Cap on today's calls (ledger).")] = 200,
    mlflow_log: Annotated[bool, typer.Option("--mlflow/--no-mlflow", help="Log to MLflow.")] = (
        True
    ),
    labels_path: Annotated[Path, typer.Option("--labels", help="Human labels.")] = (
        Path("datasets") / "labels_v1.3.jsonl"
    ),
    judge_sample: Annotated[Path, typer.Option(help="Cases to prefer (cached verdicts).")] = (
        Path("datasets") / full_mod.JUDGE_SAMPLE_FILE
    ),
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
    dataset: Annotated[Path, typer.Option(help="Dataset version.")] = LABEL_DATASET,
    rubric_dir: Annotated[Path, typer.Option(help="Rubric folder.")] = rubrics.DEFAULT_DIR,
    split_path: Annotated[Path, typer.Option("--split-file", help="Split file.")] = SPLIT_OUT,
    prompt_dir: Annotated[Path, typer.Option(help="Prompt folder.")] = judge_prompts.PROMPT_DIR,
    ledger_path: Annotated[Path, typer.Option("--ledger", help="Usage ledger.")] = (
        judge_client.USAGE_LEDGER
    ),
    cache_dir: Annotated[Path, typer.Option(help="Judge cache.")] = judge_runner.CACHE_DIR,
    dataset_out: Annotated[Path, typer.Option(help="Perturbations file.")] = (
        Path("datasets") / bias_run_mod.DATASET_FILE
    ),
    out: Annotated[Path, typer.Option(help="Report.")] = (
        judge_report.REPORT_DIR / bias_run_mod.REPORT_FILE
    ),
    tracking_dir: Annotated[Path, typer.Option(help="MLflow folder.")] = tracking_mod.TRACKING_DIR,
) -> None:
    """Frozen OpenAI judges on original vs perturbed replies of 30 dev cases: flip rates."""
    if dry_run + go + from_cache != 1:
        console.print("[red]pass exactly one of --dry-run, --go or --from-cache[/]")
        raise typer.Exit(runner.EXIT_ABORTED)
    problems = judge_frozen.frozen_problems(prompt_dir)
    for p in problems:
        console.print(f"[red]{escape(p)}[/]")
    if problems:
        console.print("[red]aborted:[/] the judge prompts don't match FROZEN.json")
        raise typer.Exit(runner.EXIT_ABORTED)
    try:
        labels = load_labels("dev", split_path=split_path, labels_path=labels_path)
        dev_ids = set(split_mod.read_split(split_path).ids("dev"))
        preferred = set(json.loads(judge_sample.read_text(encoding="utf-8"))["case_ids"])
    except (OSError, ValueError, KeyError) as e:
        console.print(f"[red]cannot prepare the bias tests:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    sample = bias_run_mod.choose_sample(labels, preferred & dev_ids, n)
    assert set(sample) <= dev_ids  # dev only: the test split is locked and used
    cache = judge_runner.JudgeCache(cache_dir)
    ledger = judge_client.Ledger(ledger_path)
    jobs: list[bias_run_mod.Job] = []
    versions: dict[Criterion, str] = {}
    replies: dict[str, AgentResponse] = {}
    chunk_ids: dict[str, list[str]] = {}
    for crit in bias_run_mod.CRITERIA:
        version = judge_frozen.FINAL_VERSIONS[crit]
        versions[crit] = version
        cfg, rubric, _, results, cases = _judge_inputs(
            crit.value, run_id, runs_dir, dataset, rubric_dir, split_path, prompt_dir, None,
            version, "openai",
        )  # fmt: skip
        plan = judge_runner.plan_cases(cfg, rubric, sample, results, cases, cache)
        replies = {c: r.response for c, r in results.items() if r.response is not None}
        for item in plan.items:
            if "retrieved_chunks" in item.data:
                chunk_ids[item.case_id] = [c["id"] for c in item.data["retrieved_chunks"]]
        jobs += bias_run_mod.perturbed_jobs(cfg, plan, replies, cache)
    total_calls, total_cost = 0, 0.0
    for job in jobs:
        cost = bias_run_mod.job_cost(job)
        total_calls, total_cost = total_calls + job.plan.calls, total_cost + cost
        console.print(f"{job.cfg.criterion.value} {job.cfg.version} / {job.perturbation}: "
                      f"{len(job.plan.items)} case(s), {job.plan.cached} cached -> "
                      f"{job.plan.calls} call(s), about ${cost:.3f}")  # fmt: skip
    n_fail = sum(bias_run_mod.human_fail(labels[c]) for c in sample)
    console.print(
        f"{len(sample)} dev cases ({n_fail} with a human fail, {len(sample) - n_fail} without; "
        f"{len(set(sample) & preferred)} from {judge_sample.name}): {total_calls} API call(s) "
        f"(up to {total_calls * judge_runner.CALLS_PER_CASE_MAX} with repairs), about "
        f"${total_cost:.3f}; calls made today: {ledger.calls_today()} of {daily_cap}"
    )
    bias_run_mod.write_rows(dataset_out, bias_run_mod.dataset_rows(sample, replies, chunk_ids))
    rows = bias_run_mod.dataset_rows(sample, replies, chunk_ids)
    upsert_entry(dataset_out.parent, dataset_out.name, DatasetSpec(
        version="1", kind="bias_perturbations", sources=[labels_path.name, judge_sample.name],
        description="Template perturbations (verbosity, authority, format, injection, chunk "
        "order) of dev replies, to test the frozen judges for bias; no LLM (Day 22)",
        metadata={"seed": 42, "cases": len(sample), "rows": len(rows), "split": "dev only"},
    ))  # fmt: skip
    console.print(f"wrote {dataset_out} (perturbations, deterministic) and its MANIFEST entry")
    if dry_run:
        console.print("[cyan]dry run[/]: no call made")
        return
    if from_cache:
        results_by, made = bias_run_mod.cached_results(jobs, cache), 0
    else:
        _check_spend(total_calls, total_cost, max_calls, max_usd, ledger, daily_cap)
        load_dotenv(".env")
        key = os.environ.get(judge_llm.API_KEY_ENV["openai"], "") or os.environ.get(
            "OPENAI_KEY", ""
        )

        def make_client(tag: str) -> LLMClient:
            return judge_client.client_for("openai", key, ledger=ledger, purpose=tag,
                                           max_retries=0)  # fmt: skip

        try:
            results_by, made = bias_run_mod.run_jobs(
                jobs, make_client, cache, ledger=ledger, max_calls=max_calls,
                daily_cap=daily_cap,
            )  # fmt: skip
        except (judge_runner.BudgetExceeded, judge_runner.CallRefused,
                judge_llm.GenerationError) as e:  # fmt: skip
            console.print(f"[red]stopped:[/] {escape(str(e))} (finished verdicts are cached: "
                          "`evalkit bias run --from-cache` reports them)")  # fmt: skip
            raise typer.Exit(runner.EXIT_ABORTED) from None
    folder = runs_dir / run_id / "judges" / bias_run_mod.RESULTS_DIR
    for (crit, name), got in results_by.items():
        if got:
            write_jsonl(folder / f"{crit.value}_{versions[crit]}_{name}.jsonl", got)
    # what the perturbed verdicts cost when they were made (this run or earlier ones)
    perturbed = [r for (_, name), got in results_by.items() if name != "original" for r in got]
    cost = judge_client.estimate_cost_usd(
        judge_client.OPENAI_JUDGE_MODEL, sum(r.input_tokens for r in perturbed),
        sum(r.output_tokens + r.reasoning_tokens for r in perturbed),
    ) or 0.0  # fmt: skip
    stats = bias_run_mod.flip_stats(results_by, planned=bias_run_mod.planned_counts(jobs))
    model = next((r.model for got in results_by.values() for r in got), "gpt-5-mini")
    meta = {"model": model, "versions": versions, "n_cases": len(sample), "n_fail": n_fail,
            "n_pass": len(sample) - n_fail, "n_preferred": len(set(sample) & preferred),
            "calls": sum(r.attempts for r in perturbed), "cost_usd": cost,
            "missing_note": missing_note}  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(bias_run_mod.build_report(stats, results_by, meta), encoding="utf-8",
                   newline="\n")  # fmt: skip
    for s in stats:
        console.print(f"{s.perturbation} x {s.criterion.value}: {s.flips}/{s.n} flips "
                      f"({s.fail_to_pass} fail->pass){' FLAG' if s.flagged else ''}")  # fmt: skip
    console.print(f"[green]wrote[/] {out}; {made} call(s), about ${cost:.3f}")
    if mlflow_log:
        params = {"judge_model": model, "n_cases": str(len(sample)), "seed": "42",
                  "split": "dev", "frozen_sha256": file_sha256(prompt_dir / "FROZEN.json"),
                  "git_sha": git_commit()}  # fmt: skip
        params |= {f"prompt_{c.value}": v for c, v in versions.items()}
        metrics = bias_run_mod.mlflow_metrics(stats) | {
            "calls": float(sum(r.attempts for r in perturbed)), "approx_cost_usd": cost,
            "calls_this_run": float(made),
        }  # fmt: skip
        mlflow_id = tracking_mod.log_metrics("day22-bias", params, metrics, [out, dataset_out],
                                             tracking_dir=tracking_dir)  # fmt: skip
        console.print(f"[green]MLflow[/] run day22-bias ({mlflow_id})")


# --------------------------------------------------------------------------- agent run budget


@app.command("agent-budget")
def agent_budget(
    before_router: Annotated[Path, typer.Option(help="Router file of the unchanged agent.")] = (
        Path("agent_snapshot") / "agent" / "router.py"
    ),
    after_router: Annotated[
        Path | None, typer.Option(help="Router file of the fixed agent (omit: not built yet).")
    ] = None,
    products: Annotated[Path, typer.Option(help="The agent's product list.")] = (
        Path("agent_snapshot") / "data" / "products" / "products.json"
    ),
    earlier_run: Annotated[str, typer.Option(help="Run whose replies show LLM use.")] = DAY7_RUN,
    dataset: Annotated[Path, typer.Option(help="Dataset version.")] = LABEL_DATASET,
    split_path: Annotated[Path, typer.Option("--split-file", help="Split file.")] = SPLIT_OUT,
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
    max_rpm: Annotated[float, typer.Option(min=0.1, help="Pacing (requests per minute).")] = 10,
    daily_budget: Annotated[int, typer.Option(min=1, help="LLM calls allowed per day.")] = 240,
    max_calls_per_run: Annotated[int, typer.Option(min=1, help="Cap per run.")] = 200,
    ids_dir: Annotated[Path, typer.Option(help="Where to write the id lists.")] = (
        split_mod.SPLIT_DIR
    ),
) -> None:
    """Dry run for agent runs on a free-tier LLM: predicted LLM calls per run (router rules run
    offline), time at the pacing, and a day-by-day plan under the daily budget. No call."""
    try:
        cases = list(read_jsonl(dataset, TestCase))
        split = json.loads(split_path.read_text(encoding="utf-8"))["splits"]
        earlier = {r.case_id: r for r in read_jsonl(runs_dir / earlier_run / runner.RESULTS_FILE,
                                                    CaseResult)}  # fmt: skip
        prods = budget_mod.load_products(products)
        routers = {"before": budget_mod.load_router(before_router)}
        if after_router is not None:
            routers["after"] = budget_mod.load_router(after_router)
    except (OSError, ValueError, KeyError, ImportError) as e:
        console.print(f"[red]cannot plan:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    subsets = {name: budget_mod.subset_ids(cases, split, name) for name in ("dev", "holdout")}
    ids_dir.mkdir(parents=True, exist_ok=True)
    for name, ids in subsets.items():
        (ids_dir / f"agent_{name}_ids.txt").write_text("\n".join(ids) + "\n", encoding="utf-8",
                                                      newline="\n")  # fmt: skip
    by_id = {c.id: c for c in cases}
    planned: list[budget_mod.PlannedRun] = []
    table = Table(title="Planned agent runs (predicted LLM calls)", box=box.SIMPLE)
    for col in ("Run", "Cases", "Knowledge route", "LLM calls (+1 warm-up)", "Minutes", "Cap ok"):
        table.add_column(col)
    for phase in ("before", "after"):
        for name, ids in subsets.items():
            label = f"{phase} / {name}"
            if phase not in routers:
                runnable = sum(not by_id[i].history for i in ids)
                table.add_row(label, str(runnable), "–", "pending: fixed router not built", "–",
                              "–")  # fmt: skip
                continue
            pred = budget_mod.predict_routes(routers[phase], prods, [by_id[i] for i in ids])
            calls = budget_mod.llm_calls(pred, earlier)
            run = budget_mod.PlannedRun(label, len(pred),
                                        sum(calls.values()) + budget_mod.WARMUP_CALLS,
                                        Counter(pred.values()))  # fmt: skip
            planned.append(run)
            table.add_row(label, str(run.cases), str(run.routes["knowledge"]), str(run.calls),
                          f"{run.minutes(max_rpm):.0f}",
                          "yes" if run.calls <= max_calls_per_run else "[red]split[/]")  # fmt: skip
    console.print(table)
    total = sum(r.calls for r in planned)
    console.print(f"predicted LLM calls: {total} (a 503 can add up to 1 retry per call); daily "
                  f"budget {daily_budget} (80% of the assumed RPD); pacing {max_rpm:g}/min, "
                  "concurrency 1")  # fmt: skip
    for n, day in enumerate(budget_mod.plan_days(planned, daily_budget), 1):
        names = ", ".join(r.name for r in day)
        console.print(f"day {n}: {names} -> {sum(r.calls for r in day)} calls")
    console.print(f"wrote {ids_dir / 'agent_dev_ids.txt'} and {ids_dir / 'agent_holdout_ids.txt'}"
                  " (dry run: no call made)")  # fmt: skip


# --------------------------------------------------------------------------- agent fix report


def _load_run(runs_dir: Path, run_id: str) -> fixreport_mod.RunData:
    meta = read_json(runs_dir / run_id / runner.META_FILE, RunMeta)
    results = list(read_jsonl(runs_dir / run_id / runner.RESULTS_FILE, CaseResult))
    return fixreport_mod.RunData(run_id, meta, results)


def _agent_file_sha(agent_repo: Path, commit: str, path: str) -> str:
    """sha256 of one file of the agent repo at a commit (read-only `git show`)."""
    import hashlib
    import subprocess

    try:
        blob = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=agent_repo,
                              capture_output=True, check=True, timeout=10).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return hashlib.sha256(blob).hexdigest()


def _router_at_commit(agent_repo: Path, commit: str, router: Path) -> Path:
    """The router file as it was at ``commit`` (read-only `git show` into a temp file), so a
    report never depends on what is checked out; the given file if git can't provide it."""
    import subprocess
    import tempfile

    try:
        rel = router.resolve().relative_to(agent_repo.resolve()).as_posix()
        blob = subprocess.run(["git", "show", f"{commit}:{rel}"], cwd=agent_repo,
                              capture_output=True, check=True, timeout=10).stdout  # fmt: skip
    except (ValueError, OSError, subprocess.SubprocessError):
        return router
    out = Path(tempfile.mkdtemp()) / f"router_{commit[:7]}.py"
    out.write_bytes(blob)
    return out


def _holdout_notes(
    runs: dict[str, fixreport_mod.RunData], partial: fixreport_mod.RunData | None
) -> list[str]:
    before, after = runs["holdout_before"], runs["holdout_after"]
    lines = []
    if before.meta.agent_repo.get("commit") is None:
        lines.append(
            f"- **Before** = run `{before.run_id}` restricted to the holdout cases (agent "
            f"{before.agent_commit}, default temperature). On dev, that run and the "
            "temperature-0 baseline had identical routes and escalations (see Noise), so it "
            "stands in for the unchanged agent here."
        )
    if after.mode == "offline":
        lines.append(
            "- **After = OFFLINE**: the fixed router's rules run on every holdout case, no agent "
            'call. After the fix, routing and escalation don\'t depend on the LLM ("not found" '
            "no longer escalates), so these numbers are exact for routing and escalation; they "
            "say nothing about the wording of knowledge answers."
        )
    if partial is not None:
        lines.append(
            f"- **The live holdout run is PARTIAL** (`{partial.run_id}`, stopped by Gemini 503 "
            '"overloaded", resumable): see the cross-check at the end.'
        )
    return [*lines, ""] if lines else []


def _partial_lines(
    partial: fixreport_mod.RunData, agree: dict[str, Any] | None, note: str = ""
) -> list[str]:
    answered = [r for r in partial.results if r.response is not None]
    lines = [
        "## Live holdout run (partial)",
        "",
        f"Run `{partial.run_id}` (agent `{partial.agent_commit}`): {len(answered)} cases "
        f"answered of {partial.meta.counts.total} recorded so far (the rest skipped: chat "
        f"history); stopped cleanly: {partial.meta.run_settings.get('stop_reason', '–')}. "
        "It is not used for the numbers above.",
    ]
    if agree is not None:
        same = not (agree["route_differs"] or agree["escalation_differs"])
        lines.append(
            f"\nCross-check against the offline rules on those {agree['n']} cases: route differs "
            f"on **{len(agree['route_differs'])}**, escalation on "
            f"**{len(agree['escalation_differs'])}**"
            + (
                " (the offline numbers match the live agent)."
                if same
                else f": {agree['route_differs']} {agree['escalation_differs']}."
            )  # fmt: skip
        )
    if note:
        lines.append(f"\n**Closed:** {note}")
    return [*lines, ""]


@app.command("agent-fix-report")
def agent_fix_report(
    dev_before: Annotated[str, typer.Option(help="Run: unchanged agent on dev.")],
    dev_before_2: Annotated[str, typer.Option(help="Run: unchanged agent on dev, again.")],
    dev_after: Annotated[str, typer.Option(help="Run: fixed agent on dev.")],
    holdout_before: Annotated[str, typer.Option(help="Run: unchanged agent on holdout.")],
    holdout_after: Annotated[
        str | None, typer.Option(help="Run: fixed agent on holdout (finished).")
    ] = None,
    holdout_after_router: Annotated[
        Path | None, typer.Option(help="Instead: the fixed router's rules, run offline.")
    ] = None,
    holdout_partial: Annotated[
        str | None, typer.Option(help="An unfinished live holdout run: cross-check only.")
    ] = None,
    holdout_partial_note: Annotated[
        str, typer.Option(help="Why the partial live run was closed (shown in the report).")
    ] = "",
    holdout_before_note: Annotated[
        str, typer.Option(help="Agent commit to show when the run didn't record one.")
    ] = "",
    products: Annotated[Path, typer.Option(help="The agent's product list (offline).")] = (
        Path("agent_snapshot") / "data" / "products" / "products.json"
    ),
    agent_repo: Annotated[Path, typer.Option(help="Agent repo (read-only).")] = Path(
        "../rag-whatsapp-agent"
    ),
    dataset: Annotated[Path, typer.Option(help="Dataset version.")] = LABEL_DATASET,
    runs_dir: Annotated[Path, typer.Option(help="Where runs are stored.")] = runner.RUNS_DIR,
    out: Annotated[Path, typer.Option(help="Report.")] = Path("reports")
    / fixreport_mod.REPORT_FILE,
    mlflow_log: Annotated[bool, typer.Option("--mlflow/--no-mlflow", help="Log to MLflow.")] = (
        True
    ),
    tracking_dir: Annotated[Path, typer.Option(help="MLflow folder.")] = tracking_mod.TRACKING_DIR,
    resamples: Annotated[int, typer.Option(min=1, help="Bootstrap resamples.")] = 2000,
) -> None:
    """Before/after report of the agent fix on dev and holdout (deterministic metrics, no
    judges, no calls): CIs, paired differences, regressions, noise, provenance."""
    if (holdout_after is None) == (holdout_after_router is None):
        console.print("[red]pass one of --holdout-after or --holdout-after-router[/]")
        raise typer.Exit(runner.EXIT_ABORTED)
    try:
        named = [("dev_before", dev_before), ("dev_before_2", dev_before_2),
                 ("dev_after", dev_after), ("holdout_before", holdout_before)]  # fmt: skip
        if holdout_after is not None:
            named.append(("holdout_after", holdout_after))
        runs = {name: _load_run(runs_dir, rid) for name, rid in named}
        cases = {c.id: c for c in read_jsonl(dataset, TestCase)}
        split = json.loads(SPLIT_OUT.read_text(encoding="utf-8"))["splits"]
        holdout_ids = set(budget_mod.subset_ids(list(cases.values()), split, "holdout"))
        if holdout_before_note:
            hb = runs["holdout_before"]
            runs["holdout_before"] = fixreport_mod.RunData(hb.run_id, hb.meta, hb.results,
                                                           holdout_before_note)  # fmt: skip
        runs["holdout_before"] = runs["holdout_before"].only(holdout_ids)
        after_commit = str(runs["dev_after"].meta.agent_repo.get("commit", ""))
        if holdout_after_router is not None:
            router = budget_mod.load_router(
                _router_at_commit(agent_repo, after_commit, holdout_after_router)
            )
            prods = budget_mod.load_products(products)
            routes = budget_mod.predict_routes(router, prods,
                                               [cases[i] for i in sorted(holdout_ids)])  # fmt: skip
            runs["holdout_after"] = fixreport_mod.offline_run(
                "offline-holdout-after", routes, runs["dev_after"].meta, after_commit
            )
        partial = _load_run(runs_dir, holdout_partial) if holdout_partial else None
    except (OSError, ValueError, KeyError, ImportError) as e:
        console.print(f"[red]cannot build the report:[/] {escape(str(e))}")
        raise typer.Exit(runner.EXIT_ABORTED) from None
    unfinished = [n for n, r in runs.items() if r.meta.finished_at is None]
    if unfinished:
        console.print(f"[red]unfinished run(s):[/] {', '.join(unfinished)} (resume them first)")
        raise typer.Exit(runner.EXIT_ABORTED)
    comps = [
        fixreport_mod.Comparison("dev", runs["dev_before"], runs["dev_after"], cases, resamples),
        fixreport_mod.Comparison(
            "holdout", runs["holdout_before"], runs["holdout_after"], cases, resamples
        ),  # fmt: skip
    ]
    noise = fixreport_mod.noise(runs["dev_before"], runs["dev_before_2"])
    before_commit = str(runs["dev_before"].meta.agent_repo.get("commit", ""))
    rules = {tag: _agent_file_sha(agent_repo, c, "SYSTEM_RULES.md") if c else "unknown"
             for tag, c in (("before", before_commit), ("after", after_commit))}  # fmt: skip
    lines = [
        "# Agent fix v1: before vs after (routing and escalation)",
        "",
        "Deterministic metrics only (no judges, no OpenAI). Expected labels: "
        f"`{dataset.name}` fields `expected_route` and `should_escalate`. **Holdout** = the "
        "golden cases in neither dev nor the locked test split: the fixes were written from dev "
        "errors, so **holdout is the honest number**. 95% percentile bootstrap CIs over cases "
        f"({resamples} resamples, seed 42); differences are paired (same cases).",
        "",
        "## Runs and versions",
        "",
        "| Run | Subset | Kind | Agent commit | Branch | Code dirty | evalkit | Cases | "
        "LLM replies (*) | Started (UTC) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    shown = dict(runs)
    if partial is not None:
        shown["holdout_after_live"] = fixreport_mod.RunData(
            partial.run_id, partial.meta, partial.results, mode="partial"
        )
    for name, r in shown.items():
        m = r.meta
        kind = {"live": "live", "offline": "**offline** (rules only)",
                "partial": "live, **partial**"}[r.mode]  # fmt: skip
        if r.mode == "live" and r.meta.finished_at is None:
            kind = "live, **partial**"
        lines.append(
            f"| `{r.run_id}` | {name.replace('_', ' ')} | {kind} | `{r.agent_commit}` | "
            f"{m.agent_repo.get('branch', '–')} | {m.agent_repo.get('dirty_code', '–')} | "
            f"`{(m.git_sha or '–')[:7]}` | {len(r.results)} | {r.llm_calls} | "
            + ("– (no agent call) |" if r.mode == "offline" else f"{m.started_at:%Y-%m-%d %H:%M} |")
        )
    settings = runs["dev_after"].meta.run_settings
    lines += [
        "",
        "(*) Saved replies that used the LLM; each live run also made one warm-up call. Cases "
        "include the 10 chat-history cases that are always skipped (holdout).",
        "",
        f"- **Agent before:** `{before_commit[:7]}` (unchanged routing + temperature 0 + "
        f"configurable retries); **after:** `{after_commit[:7]}` (the 9 fixes). Agent LLM: "
        f"`{runs['dev_after'].meta.agent_model}`, temperature 0. `SYSTEM_RULES.md` (the LLM's "
        f"system prompt) sha256 before `{rules['before'][:12]}`, after `{rules['after'][:12]}`.",
        f"- **Run settings:** concurrency 1, {settings.get('max_rpm')} requests/min, at most "
        f"{settings.get('max_llm_calls')} LLM calls per run, stop on the first failure, no "
        "retries in evalkit; agent started with LLM_RATE_LIMIT_RETRIES=0, "
        "LLM_OVERLOAD_RETRIES=1. Cache off (every reply fresh).",
        f"- **Dataset:** `{dataset.name}` sha256 `{file_sha256(dataset)[:12]}`.",
        "",
        "## Noise: the unchanged agent twice on dev",
        "",
        f"Two runs of `{before_commit[:7]}` on the same {noise['n']} dev cases: route changed "
        f"on **{len(noise['route'])}**, escalation on **{len(noise['escalated'])}**, answer type "
        f"on **{len(noise['answer_type'])}**, reply text on **{len(noise['reply'])}** of "
        f"{noise['knowledge']} knowledge replies. "
        + (
            "At temperature 0 the agent was fully repeatable, so **any change below is the fix, "
            "not noise** (for dev; holdout was run once per side)."
            if not (noise["route"] or noise["escalated"] or noise["answer_type"])
            else "Changes up to this size can be noise."
        ),  # fmt: skip
        "",
    ]
    for comp in comps:
        regs = comp.regressions()
        lines += [f"## {comp.name.capitalize()}" + (" (the honest number)" if comp.name ==
                  "holdout" else " (optimistic: the fixes were written from these errors)"),
                  ""]  # fmt: skip
        if comp.name == "holdout":
            lines += _holdout_notes(runs, partial)
        lines += [*comp.metric_lines(), "", "Routing by expected route:", "",
                  *comp.route_lines(), "", f"**Regressions** (right before, wrong after): "
                  f"{len(regs)}", ""]  # fmt: skip
        if regs:
            lines += ["| case_id | metric | before | after | question |", "|---|---|---|---|---|"]
            lines += [f"| {cid} | {metric} | {b} | {a} | "
                      f"{cases[cid].question[:90].replace('|', '/')} |"
                      for cid, metric, b, a in regs]  # fmt: skip
        lines.append("")
    offline_after = runs["holdout_after"].mode == "offline"
    agree = (fixreport_mod.agreement(partial, runs["holdout_after"])
             if partial is not None and offline_after else None)  # fmt: skip
    if partial is not None:
        lines += _partial_lines(partial, agree, holdout_partial_note)
    text = "\n".join(lines)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    console.print(f"[green]wrote[/] {out}")
    for comp in comps:
        console.print(f"{comp.name}: {len(comp.regressions())} regression(s)")
    if mlflow_log:
        params = {
            "agent_commit_before": before_commit, "agent_commit_after": after_commit,
            "agent_branch": str(runs["dev_after"].meta.agent_repo.get("branch", "")),
            "agent_model": str(runs["dev_after"].meta.agent_model), "temperature": "0",
            "system_rules_sha_before": rules["before"], "system_rules_sha_after": rules["after"],
            "dataset": dataset.name, "dataset_sha256": file_sha256(dataset),
            "evalkit_git_sha": str(runs["dev_after"].meta.git_sha),
            **{f"run_{n}": r.run_id for n, r in runs.items()},
            "holdout_after_kind": runs["holdout_after"].mode,
            "holdout_live_partial_run": partial.run_id if partial else "",
        }  # fmt: skip
        metrics: dict[str, float] = {}
        for comp in comps:
            metrics |= comp.metrics()
        if agree is not None:
            metrics |= {"holdout.live_partial.cases": agree["n"],
                        "holdout.live_partial.route_differs": len(agree["route_differs"]),
                        "holdout.live_partial.escalation_differs":
                            len(agree["escalation_differs"])}  # fmt: skip
        metrics |= {"noise.route_changes": len(noise["route"]),
                    "noise.escalation_changes": len(noise["escalated"]),
                    "noise.reply_changes": len(noise["reply"])}  # fmt: skip
        live = [r for r in [*runs.values(), *([partial] if partial else [])] if r.mode == "live"]
        artifacts = [out, *(runs_dir / r.run_id / runner.META_FILE for r in live)]
        mlflow_id = tracking_mod.log_metrics("agent-fix-v1", params, metrics, artifacts,
                                             tracking_dir=tracking_dir)  # fmt: skip
        console.print(f"[green]MLflow[/] run agent-fix-v1 ({mlflow_id})")
