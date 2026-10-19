"""Send a dataset to an agent and record exactly one CaseResult per case.

Flow: load + filter cases -> preflight (/health, then a "hi" probe that proves the key
works) -> open runs/<run_id>/ -> optional warm-up call (not recorded) -> run cases under a
semaphore, appending each CaseResult to results.jsonl the moment it finishes -> meta.json.

``meta.json`` is written atomically at the start (``finished_at: null``) and again at the
end, so a crash or Ctrl+C always leaves a resumable run behind.
"""

import asyncio
import logging
import math
import re
import secrets
import subprocess
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from evalkit.adapters.base import AdapterError, AgentAdapter, AuthError
from evalkit.cache import DEFAULT_CACHE_DIR, CacheMode, ResponseCache, cache_key
from evalkit.hashing import file_sha256, stable_hash
from evalkit.io import (
    JsonlError,
    append_jsonl,
    drop_partial_last_line,
    read_json,
    read_jsonl,
    write_json,
)
from evalkit.manifest import dataset_version
from evalkit.schemas import (
    AgentResponse,
    CaseResult,
    CaseSource,
    FailureType,
    Route,
    RunCounts,
    RunMeta,
    TestCase,
)

log = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 2  # benchmarked: 4 is slower than 1; see notes/day3_latency.md
RUNS_DIR = Path("runs")
RESULTS_FILE = "results.jsonl"
META_FILE = "meta.json"

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_ABORTED = 2
EXIT_INTERRUPTED = 130
EXIT_STOPPED = 3  # stopped cleanly (rate limit / quota / --max-llm-calls / failure): resume
LLM_UNAVAILABLE = "LLM unavailable, fallback answer"

_RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

PROBE_CASE = TestCase(
    id="preflight-probe",
    question="hi",
    category="preflight",
    expected_route=Route.GREETING,
    should_escalate=False,
    source=CaseSource.HUMAN,
)
WARMUP_CASE = TestCase(
    id="warmup",
    question="What is your shipping policy?",
    category="warmup",
    expected_route=Route.KNOWLEDGE,
    should_escalate=False,
    source=CaseSource.HUMAN,
)


class RunAborted(Exception):
    """The run cannot start or continue: agent down/unhealthy, auth failure, bad dataset."""


@dataclass(frozen=True)
class RunConfig:
    dataset: Path
    concurrency: int = DEFAULT_CONCURRENCY
    limit: int | None = None
    route: Route | None = None
    ids: tuple[str, ...] = ()
    cache_mode: CacheMode = CacheMode.NORMAL
    resume: str | None = None
    runs_dir: Path = RUNS_DIR
    cache_dir: Path = DEFAULT_CACHE_DIR
    warmup: bool = True
    # Day 25 (free-tier Gemini): pacing and clean stops. Not part of config_hash, so a run
    # can be resumed with other pacing.
    max_rpm: float | None = None  # at most this many agent requests per minute
    max_llm_calls: int | None = None  # stop before more replies than this used the LLM
    stop_on_failure: bool = False  # stop at the first failed case (resumable)
    agent_repo: Path | None = None  # record the agent's commit / branch in meta.json
    ids_file: Path | None = None  # where --ids came from (@file): resume_command reuses it

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if self.limit is not None and self.limit < 1:
            raise ValueError("limit must be at least 1")
        if self.max_rpm is not None and self.max_rpm <= 0:
            raise ValueError("max_rpm must be positive")
        if self.max_llm_calls is not None and self.max_llm_calls < 1:
            raise ValueError("max_llm_calls must be at least 1")

    def run_settings(self) -> dict[str, Any]:
        return {"max_rpm": self.max_rpm, "max_llm_calls": self.max_llm_calls,
                "stop_on_failure": self.stop_on_failure}  # fmt: skip

    def hashed_settings(self) -> dict[str, Any]:
        """Run settings that go into config_hash (paths and resume excluded)."""
        return {
            "concurrency": self.concurrency,
            "limit": self.limit,
            "route": self.route,
            "ids": list(self.ids),
            "cache_mode": self.cache_mode,
        }


# --------------------------------------------------------------------------- summary


@dataclass(frozen=True)
class RouteLatency:
    route: str
    n: int
    p50_ms: float
    p95_ms: float


@dataclass(frozen=True)
class Summary:
    total: int
    ok: int
    failures: dict[str, int]  # by failure type, excluding skipped
    skipped: int
    cache_hits: int
    route_correct: int
    route_checked: int
    latency: list[RouteLatency]

    @property
    def route_accuracy(self) -> float | None:
        return self.route_correct / self.route_checked if self.route_checked else None


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile, ``q`` in [0, 1]."""
    if not values:
        raise ValueError("percentile of no values")
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def summarize(results: Iterable[CaseResult], cases: dict[str, TestCase]) -> Summary:
    results = list(results)
    failures: Counter[str] = Counter()
    latencies: dict[str, list[float]] = defaultdict(list)
    route_correct = route_checked = 0
    for r in results:
        if r.failure_type is not None and r.failure_type is not FailureType.SKIPPED:
            failures[r.failure_type.value] += 1
        resp = r.response
        if resp is None:
            continue
        case = cases.get(r.case_id)
        if case is not None and case.expected_route is not None and resp.ok:
            route_checked += 1
            route_correct += resp.route is case.expected_route
        if not r.cache_hit and r.wall_latency_ms is not None:
            latencies[resp.route.value if resp.route else "none"].append(r.wall_latency_ms)
    return Summary(
        total=len(results),
        ok=sum(r.failure_type is None for r in results),
        failures=dict(sorted(failures.items())),
        skipped=sum(r.failure_type is FailureType.SKIPPED for r in results),
        cache_hits=sum(r.cache_hit for r in results),
        route_correct=route_correct,
        route_checked=route_checked,
        latency=[
            RouteLatency(route, len(v), percentile(v, 0.5), percentile(v, 0.95))
            for route, v in sorted(latencies.items())
        ],
    )


def count_results(results: Iterable[CaseResult]) -> RunCounts:
    results = list(results)
    ok = sum(r.failure_type is None for r in results)
    return RunCounts(
        total=len(results),
        ok=ok,
        failed=len(results) - ok,
        cache_hits=sum(r.cache_hit for r in results),
    )


@dataclass
class RunOutcome:
    run_id: str
    run_dir: Path
    meta: RunMeta
    results: list[CaseResult]
    cases: dict[str, TestCase]
    config: RunConfig
    interrupted: bool = False
    stopped: str | None = None  # why the run stopped cleanly before the end
    llm_calls: int = 0  # replies that used (or tried) the agent's LLM, warm-up included

    @property
    def exit_code(self) -> int:
        if self.interrupted:
            return EXIT_INTERRUPTED
        if self.stopped:
            return EXIT_STOPPED
        failed = any(r.failure_type not in (None, FailureType.SKIPPED) for r in self.results)
        return EXIT_FAILURES if failed else EXIT_OK

    def summary(self) -> Summary:
        return summarize(self.results, self.cases)


def print_summary(console: Console, outcome: RunOutcome) -> None:
    s = outcome.summary()
    status = ("interrupted" if outcome.interrupted
              else "stopped, resumable" if outcome.stopped else "finished")  # fmt: skip
    console.print(f"\n[bold]Run {outcome.run_id}[/] ({status}) -> {outcome.run_dir}")
    totals = Table(show_header=False, box=None, padding=(0, 2))
    totals.add_row("cases", str(s.total))
    totals.add_row("ok", f"[green]{s.ok}[/]")
    for name, n in s.failures.items():
        totals.add_row(f"failed: {name}", f"[red]{n}[/]")
    totals.add_row("skipped", str(s.skipped))
    totals.add_row("cache hits", str(s.cache_hits))
    acc = s.route_accuracy
    totals.add_row(
        "route accuracy",
        f"{acc:.0%} ({s.route_correct}/{s.route_checked})" if acc is not None else "n/a",
    )
    console.print(totals)
    if s.latency:
        table = Table(title="Latency by actual route (non-cached)", title_justify="left")
        table.add_column("route")
        table.add_column("n", justify="right")
        table.add_column("p50", justify="right")
        table.add_column("p95", justify="right")
        for row in s.latency:
            table.add_row(row.route, str(row.n), _secs(row.p50_ms), _secs(row.p95_ms))
        console.print(table)
    else:
        console.print("latency: no non-cached responses")


def _secs(ms: float) -> str:
    return f"{ms / 1000:.2f}s"


# --------------------------------------------------------------------------- helpers


def new_run_id(now: datetime | None = None) -> str:
    """UTC timestamp plus a short random suffix, e.g. ``20261013-091500-a3f9``."""
    return f"{(now or datetime.now(UTC)):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def git_info(cwd: Path | None = None) -> tuple[str | None, bool]:
    """(HEAD sha, working tree dirty). (None, False) outside a git repo."""
    try:
        sha = _git(["rev-parse", "HEAD"], cwd)
        dirty = bool(_git(["status", "--porcelain"], cwd))
    except (OSError, subprocess.SubprocessError):
        return None, False
    return sha, dirty


# Data files of the agent repo that change without a code change (the vector store).
AGENT_DATA_PATHS: Final = ("rag/chroma_store/",)


def agent_repo_info(path: Path) -> dict[str, Any]:
    """The agent's commit, branch and whether its *code* has uncommitted changes (changes
    under AGENT_DATA_PATHS don't count). Read-only git calls; {} if it can't be read."""
    try:
        commit = _git(["rev-parse", "HEAD"], path)
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], path)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout  # not stripped: each line starts with a 2-letter status and a space
    except (OSError, subprocess.SubprocessError):
        return {}
    changed = [line[3:] for line in status.splitlines() if line.strip()]
    code = [f for f in changed if not f.startswith(AGENT_DATA_PATHS)]
    return {"commit": commit, "branch": branch, "dirty_code": bool(code),
            "dirty_files": code[:20]}  # fmt: skip


QUOTA_MARKERS: Final = ("429", "quota", "rate limit", "resource_exhausted", "resource exhausted")


def is_quota_error(text: str | None) -> bool:
    """The agent's LLM provider refused for rate limit or quota (no retry: stop the run)."""
    lowered = (text or "").lower()
    return any(m in lowered for m in QUOTA_MARKERS)


def llm_error(resp: AgentResponse) -> str | None:
    """The agent's fallback marker: the LLM was unavailable (e.g. 503 after its own retry)
    and the reply was quoted from the documents instead. Not the agent's real answer."""
    details = resp.details
    if isinstance(details, dict) and details.get("llm_error"):
        return str(details["llm_error"])
    return None


def used_llm(resp: AgentResponse | None) -> bool:
    """The reply called the agent's LLM (it reports tokens), or tried to (fallback)."""
    return resp is not None and (resp.prompt_tokens is not None or llm_error(resp) is not None)


def _git(args: list[str], cwd: Path | None) -> str:
    out = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=10
    )
    return out.stdout.strip()


def load_cases(
    path: Path,
    *,
    route: Route | None = None,
    ids: Sequence[str] = (),
    limit: int | None = None,
) -> list[TestCase]:
    """Read the dataset and apply filters in order: --ids, --route, then --limit."""
    try:
        cases = list(read_jsonl(path, TestCase))
    except FileNotFoundError as e:
        raise RunAborted(f"dataset not found: {path}") from e
    except JsonlError as e:
        raise RunAborted(f"bad dataset: {e}") from e
    dupes = sorted(i for i, n in Counter(c.id for c in cases).items() if n > 1)
    if dupes:
        raise RunAborted(f"bad dataset: duplicate case ids {dupes}")
    if ids:
        unknown = sorted(set(ids) - {c.id for c in cases})
        if unknown:
            raise RunAborted(f"--ids not in dataset: {unknown}")
        cases = [c for c in cases if c.id in set(ids)]
    if route is not None:
        cases = [c for c in cases if c.expected_route is route]
    if limit is not None:
        cases = cases[:limit]
    if not cases:
        raise RunAborted(f"no cases selected from {path}")
    return cases


def _quote(arg: str) -> str:
    """Quote for both PowerShell and POSIX shells (double quotes only when needed)."""
    return arg if re.fullmatch(r"[\w@%+=:,./-]+", arg) else f'"{arg}"'


def resume_command(config: RunConfig, run_id: str) -> str:
    """The command that continues ``run_id`` with the same settings."""
    parts = ["evalkit", "run", "--dataset", config.dataset.as_posix(), "--resume", run_id]
    if config.concurrency != DEFAULT_CONCURRENCY:
        parts += ["--concurrency", str(config.concurrency)]
    if config.limit is not None:
        parts += ["--limit", str(config.limit)]
    if config.route is not None:
        parts += ["--route", config.route.value]
    if config.ids_file is not None:
        parts += ["--ids", "@" + config.ids_file.as_posix()]
    elif config.ids:
        parts += ["--ids", ",".join(config.ids)]
    if config.max_rpm is not None:
        parts += ["--max-rpm", f"{config.max_rpm:g}"]
    if config.max_llm_calls is not None:
        parts += ["--max-llm-calls", str(config.max_llm_calls)]
    if config.stop_on_failure:
        parts.append("--stop-on-failure")
    if config.agent_repo is not None:
        parts += ["--agent-repo", _quote(config.agent_repo.as_posix())]
    if config.cache_mode is CacheMode.OFF:
        parts.append("--no-cache")
    elif config.cache_mode is CacheMode.REFRESH:
        parts.append("--refresh")
    return " ".join(_quote(p) for p in parts)


def list_runs(runs_dir: Path = RUNS_DIR) -> list[RunMeta]:
    """Every readable run under ``runs_dir``, newest first."""
    metas = []
    for path in sorted(runs_dir.glob(f"*/{META_FILE}")):
        try:
            metas.append(read_json(path, RunMeta))
        except (OSError, ValidationError) as e:
            log.warning("skipping unreadable %s: %s", path, e)
    return sorted(metas, key=lambda m: m.started_at, reverse=True)


async def preflight(adapter: AgentAdapter) -> dict[str, Any]:
    """Check /health, then prove the key works with one probe call. Raises RunAborted."""
    try:
        health = await adapter.health()
    except AdapterError as e:
        raise RunAborted(f"agent is not reachable ({e}); is it running?") from e
    if health.get("status") != "ok" or health.get("database", "ok") != "ok":
        raise RunAborted(f"agent is unhealthy: {health}")
    try:
        reply = await adapter.answer(PROBE_CASE)
    except AuthError as e:
        raise RunAborted(str(e)) from e
    except AdapterError as e:
        raise RunAborted(f"probe call failed ({e.failure_type.value}): {e}") from e
    if reply.response.is_error:
        raise RunAborted(f"probe call returned ok=false: {reply.response.error}")
    return health


# --------------------------------------------------------------------------- the run


@dataclass
class _Run:
    config: RunConfig
    adapter: AgentAdapter
    cache: ResponseCache
    console: Console
    run_id: str
    results_path: Path
    keys: dict[str, str]
    results: list[CaseResult]
    total: int
    warned_fields: set[str] = field(default_factory=set)
    stopping: bool = False
    stop_reason: str | None = None
    llm_calls: int = 0
    _next_start: float = 0.0
    _pace_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def stop(self, reason: str) -> None:
        if not self.stopping:
            self.stopping, self.stop_reason = True, reason
            self.console.print(f"[yellow]stopping:[/] {escape(reason)}")

    async def pace(self) -> None:
        """At most config.max_rpm agent requests per minute (evenly spaced)."""
        if self.config.max_rpm is None:
            return
        async with self._pace_lock:
            now = time.monotonic()
            if self._next_start > now:
                await asyncio.sleep(self._next_start - now)
            self._next_start = max(now, self._next_start) + 60.0 / self.config.max_rpm

    async def warm_up(self) -> None:
        t0 = time.perf_counter()
        await self.pace()
        try:
            reply = await self.adapter.answer(WARMUP_CASE)
        except AdapterError as e:
            log.warning("warm-up call failed (%s); continuing", e)
            return
        self.llm_calls += used_llm(reply.response)
        route = reply.response.route.value if reply.response.route else "none"
        self.console.print(f"warm-up: {route} in {time.perf_counter() - t0:.1f}s (not recorded)")

    async def run_all(self, todo: list[TestCase]) -> None:
        sem = asyncio.Semaphore(self.config.concurrency)

        async def worker(case: TestCase) -> None:
            async with sem:
                if self.stopping:  # an AuthError or a clean stop: don't start anything new
                    return
                cap = self.config.max_llm_calls
                if cap is not None and self.llm_calls >= cap:
                    self.stop(f"--max-llm-calls {cap} reached ({self.llm_calls} LLM calls)")
                    return
                try:
                    result = await self.run_case(case)
                except AuthError:
                    self.stopping = True
                    raise
                self.llm_calls += used_llm(result.response)
                reason = self._stop_reason(result)
                if reason is not None:
                    # not recorded: a resumed run tries this case again
                    self._report(case, result)
                    self.stop(f"{reason} on {case.id}; it will be re-run on resume")
                    return
            append_jsonl(self.results_path, result)
            self.results.append(result)
            self._report(case, result)

        async with asyncio.TaskGroup() as tg:
            for case in todo:
                tg.create_task(worker(case))

    def _stop_reason(self, r: CaseResult) -> str | None:
        """Why this result stops the run: the LLM's quota or rate limit (always), the LLM
        unavailable after the agent's own retry (always), or any failure with
        --stop-on-failure."""
        if r.failure_type is None or r.failure_type is FailureType.SKIPPED:
            return None
        if is_quota_error(r.error_message):
            return "rate limit / quota (no retry)"
        if (r.error_message or "").startswith(LLM_UNAVAILABLE):
            return "LLM unavailable (no retry)"
        if self.config.stop_on_failure:
            return f"failure ({r.failure_type.value})"
        return None

    async def run_case(self, case: TestCase) -> CaseResult:
        key = self.keys[case.id]
        cached = self.cache.get(key)
        if cached is None:
            await self.pace()  # before the clock starts: latency never includes pacing
        started_at = datetime.now(UTC)
        t0 = time.perf_counter()

        def finish(**fields: Any) -> CaseResult:
            return CaseResult(
                run_id=self.run_id,
                case_id=case.id,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                wall_latency_ms=(time.perf_counter() - t0) * 1000,
                **fields,
            )

        if cached is not None:
            return finish(response=cached.response, attempts=0, cache_hit=True)
        try:
            reply = await self.adapter.answer(case)
        except AdapterError as e:
            return finish(failure_type=e.failure_type, error_message=str(e), attempts=e.attempts)
        except AuthError:
            raise
        except Exception as e:  # a bug in our code must not stop the run
            log.exception("unexpected error on case %s", case.id)
            return finish(
                failure_type=FailureType.PARSE_ERROR,
                error_message=f"internal error: {e!r}",
                attempts=1,
            )
        resp = reply.response
        self._warn_unknown_fields(resp)
        if resp.is_error:
            return finish(
                response=resp,
                failure_type=FailureType.AGENT_ERROR,
                error_message=resp.error or "agent returned ok=false",
                attempts=reply.attempts,
            )
        if (fallback := llm_error(resp)) is not None:  # never cached
            # marked ok=false so the record is a consistent agent_error (it stops the run
            # and is not written: a resumed run asks again)
            return finish(
                response=resp.model_copy(update={"ok": False, "error": f"{LLM_UNAVAILABLE}"}),
                failure_type=FailureType.AGENT_ERROR,
                error_message=f"{LLM_UNAVAILABLE}: {fallback}",
                attempts=reply.attempts,
            )
        self.cache.put(key, resp)
        return finish(response=resp, attempts=reply.attempts)

    def _warn_unknown_fields(self, resp: AgentResponse) -> None:
        new = sorted(set(resp.unknown_fields) - self.warned_fields)
        if new:
            self.warned_fields.update(new)
            log.warning("agent sent fields the schema does not know: %s", ", ".join(new))

    def _report(self, case: TestCase, r: CaseResult) -> None:
        prefix = f"[{len(self.results)}/{self.total}] {escape(case.id)}"
        if r.failure_type is FailureType.SKIPPED:
            self.console.print(f"{prefix}: [yellow]skipped[/] ({escape(r.error_message or '')})")
        elif r.failure_type is not None:
            msg = escape((r.error_message or "")[:100])
            self.console.print(f"{prefix}: [red]{r.failure_type.value}[/] {msg}")
        else:
            assert r.response is not None
            route = r.response.route.value if r.response.route else "none"
            where = "cached" if r.cache_hit else _secs(r.wall_latency_ms or 0.0)
            self.console.print(f"{prefix}: [green]ok[/] {route} ({where})")


def _validate_resume(config: RunConfig, dataset_sha: str, config_hash: str) -> RunMeta:
    run_id = config.resume or ""
    if not _RUN_ID_RE.match(run_id):
        raise RunAborted(f"invalid run id {run_id!r}")
    meta_path = config.runs_dir / run_id / META_FILE
    if not meta_path.exists():
        raise RunAborted(f"no run {run_id!r} in {config.runs_dir}")
    try:
        meta = read_json(meta_path, RunMeta)
    except ValidationError as e:
        raise RunAborted(f"unreadable {meta_path}: {e}") from e
    if meta.dataset_sha256 != dataset_sha:
        raise RunAborted(
            f"dataset {config.dataset} changed since run {run_id} started; start a new run"
        )
    if meta.config_hash != config_hash:
        log.warning("run settings differ from the original run %s; continuing", run_id)
    return meta


def _load_prior(results_path: Path) -> list[CaseResult]:
    if not results_path.exists():
        return []
    if drop_partial_last_line(results_path):
        log.warning("dropped a half-written last line in %s", results_path)
    try:
        return list(read_jsonl(results_path, CaseResult))
    except JsonlError as e:
        raise RunAborted(f"cannot resume: {e}") from e


async def run_eval(
    config: RunConfig, adapter: AgentAdapter, console: Console | None = None
) -> RunOutcome:
    """Run (or resume) an eval. Raises RunAborted; returns an outcome otherwise.

    Cancellation (Ctrl+C) is caught: results written so far stay on disk, meta.json is saved
    with ``finished_at: null`` and the outcome is marked ``interrupted``.
    """
    console = console or Console()
    cases = load_cases(config.dataset, route=config.route, ids=config.ids, limit=config.limit)
    dataset_sha = file_sha256(config.dataset)
    version = dataset_version(config.dataset)
    config_hash = stable_hash(
        {
            "adapter": adapter.name,
            "adapter_settings": adapter.settings(),
            "dataset_sha256": dataset_sha,
            **config.hashed_settings(),
        }
    )
    resumed = _validate_resume(config, dataset_sha, config_hash) if config.resume else None

    health = await preflight(adapter)
    agent_llm = health.get("llm") if isinstance(health.get("llm"), str) else None

    if resumed is not None:
        run_id, started_at = resumed.run_id, resumed.started_at
        run_dir = config.runs_dir / run_id
        prior = _load_prior(run_dir / RESULTS_FILE)
    else:
        run_id, started_at = new_run_id(), datetime.now(UTC)
        run_dir = config.runs_dir / run_id
        run_dir.mkdir(parents=True)
        prior = []
    done = {r.case_id for r in prior}
    todo = [c for c in cases if c.id not in done]
    git_sha, git_dirty = git_info()
    agent_repo = agent_repo_info(config.agent_repo) if config.agent_repo is not None else {}

    def make_meta(
        results: list[CaseResult], finished_at: datetime | None, stop_reason: str | None = None
    ) -> RunMeta:
        return RunMeta(
            run_id=run_id,
            git_sha=git_sha,
            git_dirty=git_dirty,
            dataset_path=str(config.dataset),
            dataset_sha256=dataset_sha,
            dataset_version=version,
            config_hash=config_hash,
            agent_model=agent_llm,
            agent_health=health,
            agent_repo=agent_repo,
            run_settings=config.run_settings()
            | ({"stop_reason": stop_reason} if stop_reason else {}),
            concurrency=config.concurrency,
            started_at=started_at,
            finished_at=finished_at,
            counts=count_results(results),
        )

    results = list(prior)
    write_json(run_dir / META_FILE, make_meta(results, None))
    console.print(
        f"run {run_id}: {len(todo)} to run"
        + (f", {len(done)} already done" if done else "")
        + f", concurrency {config.concurrency}, cache {config.cache_mode.value}"
    )

    cache = ResponseCache(config.cache_dir, config.cache_mode)
    run = _Run(
        config=config,
        adapter=adapter,
        cache=cache,
        console=console,
        run_id=run_id,
        results_path=run_dir / RESULTS_FILE,
        keys={c.id: cache_key(adapter.name, c, agent_llm) for c in todo},
        results=results,
        total=len(results) + len(todo),
    )
    abort: str | None = None
    interrupted = False
    try:
        try:
            needs_agent = [c for c in todo if not c.history and not cache.contains(run.keys[c.id])]
            if config.warmup and needs_agent:
                await run.warm_up()
            await run.run_all(todo)
        except* AuthError as eg:
            abort = str(eg.exceptions[0])
    except asyncio.CancelledError:
        interrupted = True
    finally:
        cache.close()

    finished_at = None if (interrupted or abort or run.stop_reason) else datetime.now(UTC)
    meta = make_meta(results, finished_at, run.stop_reason)
    write_json(run_dir / META_FILE, meta)
    if abort is not None:
        raise RunAborted(
            f"{abort}. {len(results)} results kept; after fixing it, resume with: "
            f"{resume_command(config, run_id)}"
        )
    return RunOutcome(
        run_id=run_id,
        run_dir=run_dir,
        meta=meta,
        results=results,
        cases={c.id: c for c in cases},
        config=config,
        interrupted=interrupted,
        stopped=run.stop_reason,
        llm_calls=run.llm_calls,
    )
