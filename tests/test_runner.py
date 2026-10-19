import asyncio
import logging
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from rich.console import Console

from evalkit.adapters.base import AgentAdapter, AgentReply
from evalkit.cache import CacheMode
from evalkit.io import read_json, read_jsonl, write_jsonl
from evalkit.runner import (
    EXIT_FAILURES,
    EXIT_INTERRUPTED,
    EXIT_OK,
    META_FILE,
    PROBE_CASE,
    RESULTS_FILE,
    WARMUP_CASE,
    RunAborted,
    RunConfig,
    RunOutcome,
    git_info,
    list_runs,
    load_cases,
    new_run_id,
    percentile,
    print_summary,
    resume_command,
    run_eval,
    summarize,
)
from evalkit.schemas import AgentResponse, CaseResult, FailureType, Route, RunMeta, TestCase

from .agent_double import HEALTH, SENTINEL_KEY, FakeAgent, Reply, fixture, make_case, ok, raises

KQ = "How long does shipping take?"
DQ = "How much is the Hydra Balance Moisturizer?"
GQ = "hello"
CASES = [
    make_case("k1", KQ),
    make_case("d1", DQ, "database"),
    make_case("g1", GQ, "greeting"),
]
ROOT = Path(__file__).resolve().parent.parent


def write_dataset(tmp_path: Path, cases: list[TestCase], name: str = "ds.jsonl") -> Path:
    path = tmp_path / name
    write_jsonl(path, cases)
    return path


def config(tmp_path: Path, dataset: Path, **over: Any) -> RunConfig:
    return RunConfig(
        dataset=dataset, runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache", **over
    )


def agent_for_cases(delay: float = 0.0) -> FakeAgent:
    return FakeAgent(delay).on(DQ, ok("database")).on(GQ, ok("greeting"))


async def run(cfg: RunConfig, agent: FakeAgent, console: Console | None = None) -> RunOutcome:
    async with agent.adapter() as adapter:
        return await run_eval(cfg, adapter, console or Console(file=StringIO(), width=200))


def by_id(outcome: RunOutcome) -> dict[str, Any]:
    return {r.case_id: r for r in outcome.results}


# --------------------------------------------------------------------------- happy path


async def test_full_run_records_one_result_per_case(tmp_path: Path) -> None:
    agent = agent_for_cases()
    ds = write_dataset(tmp_path, CASES)
    outcome = await run(config(tmp_path, ds), agent)

    assert outcome.exit_code == EXIT_OK and not outcome.interrupted
    on_disk = list(read_jsonl(outcome.run_dir / RESULTS_FILE, CaseResult))
    assert sorted(r.case_id for r in on_disk) == ["d1", "g1", "k1"]
    assert all(r.attempts == 1 and not r.cache_hit and r.failure_type is None for r in on_disk)
    assert all((r.wall_latency_ms or 0) >= 0 and r.finished_at >= r.started_at for r in on_disk)

    meta = read_json(outcome.run_dir / META_FILE, RunMeta)
    assert meta == outcome.meta and meta.finished_at is not None
    assert meta.counts.model_dump() == {"total": 3, "ok": 3, "failed": 0, "cache_hits": 0}
    assert meta.agent_model == "gemini/test-model" and meta.agent_health == HEALTH
    assert meta.dataset_path == str(ds) and meta.concurrency == 2
    assert meta.run_id == outcome.run_id and outcome.run_dir.name == outcome.run_id

    # preflight probe + warm-up happened, but neither is recorded
    assert agent.calls("hi") == 1 and agent.calls(WARMUP_CASE.question) == 1
    assert {PROBE_CASE.id, WARMUP_CASE.id}.isdisjoint(by_id(outcome))
    assert all(body["eval_mode"] is True for body in agent.bodies)


async def test_second_run_is_all_cache_hits_without_warmup(tmp_path: Path) -> None:
    agent = agent_for_cases()
    ds = write_dataset(tmp_path, CASES)
    await run(config(tmp_path, ds), agent)
    calls_before = len(agent.messages)

    second = await run(config(tmp_path, ds), agent)
    assert all(r.cache_hit and r.attempts == 0 for r in second.results)
    assert second.meta.counts.cache_hits == 3
    assert agent.messages[calls_before:] == ["hi"]  # only the preflight probe
    assert second.summary().latency == []  # latency stats ignore cached responses


async def test_refresh_and_no_cache_modes_call_the_agent(tmp_path: Path) -> None:
    agent = agent_for_cases()
    ds = write_dataset(tmp_path, CASES)
    await run(config(tmp_path, ds), agent)
    for mode in (CacheMode.REFRESH, CacheMode.OFF):
        outcome = await run(config(tmp_path, ds, cache_mode=mode), agent)
        assert not any(r.cache_hit for r in outcome.results)
    assert agent.calls(KQ) == 3


# --------------------------------------------------------------------------- failures


@pytest.mark.parametrize(
    ("step", "failure", "attempts"),
    [
        (Reply(500, text="boom"), FailureType.HTTP_ERROR, 3),
        (raises(httpx.ReadTimeout), FailureType.TIMEOUT, 1),
        (raises(httpx.ConnectError, "connection refused"), FailureType.CONNECTION, 3),
        (Reply(text="not json"), FailureType.PARSE_ERROR, 1),
    ],
)
async def test_failing_case_is_recorded_and_run_continues(
    tmp_path: Path, step: Any, failure: FailureType, attempts: int
) -> None:
    agent = agent_for_cases().on(KQ, step)
    ds = write_dataset(tmp_path, CASES)
    outcome = await run(config(tmp_path, ds), agent)
    results = by_id(outcome)
    assert results["k1"].failure_type is failure and results["k1"].attempts == attempts
    assert results["k1"].response is None and results["k1"].error_message
    assert results["d1"].failure_type is None and results["g1"].failure_type is None
    assert outcome.exit_code == EXIT_FAILURES
    assert outcome.meta.counts.failed == 1

    # failures are never cached: the next run asks the agent again
    before = agent.calls(KQ)
    await run(config(tmp_path, ds), agent)
    assert agent.calls(KQ) == before + attempts


async def test_ok_false_is_agent_error_with_response_kept(tmp_path: Path) -> None:
    agent = agent_for_cases().on(KQ, ok("error"))
    outcome = await run(config(tmp_path, write_dataset(tmp_path, CASES)), agent)
    r = by_id(outcome)["k1"]
    assert r.failure_type is FailureType.AGENT_ERROR
    assert r.response is not None and r.response.is_error
    assert r.error_message == "TimeoutError: simulated"
    assert outcome.exit_code == EXIT_FAILURES


async def test_agent_error_without_error_text_gets_a_default_message(tmp_path: Path) -> None:
    agent = agent_for_cases().on(KQ, ok("error", error=None))
    outcome = await run(config(tmp_path, write_dataset(tmp_path, CASES)), agent)
    assert by_id(outcome)["k1"].error_message == "agent returned ok=false"


async def test_history_case_is_skipped_and_does_not_fail_the_run(tmp_path: Path) -> None:
    agent = agent_for_cases()
    history_case = make_case("h1", "and the price?", history=[{"role": "user", "content": "hi"}])
    ds = write_dataset(tmp_path, [*CASES, history_case])
    outcome = await run(config(tmp_path, ds), agent)
    r = by_id(outcome)["h1"]
    assert r.failure_type is FailureType.SKIPPED and r.attempts == 0
    assert r.error_message == "history not supported by eval_mode API"
    assert agent.calls("and the price?") == 0
    assert outcome.exit_code == EXIT_OK
    assert outcome.summary().skipped == 1 and outcome.summary().failures == {}


async def test_unexpected_exception_becomes_a_recorded_failure(tmp_path: Path) -> None:
    class Buggy(AgentAdapter):
        name = "buggy"

        async def health(self) -> dict[str, Any]:
            return HEALTH

        async def answer(self, case: TestCase) -> AgentReply:
            if case.id in (PROBE_CASE.id, WARMUP_CASE.id):
                return AgentReply(AgentResponse.model_validate(fixture("greeting")), 1)
            raise RuntimeError("bug in our code")

        async def aclose(self) -> None:
            pass

    ds = write_dataset(tmp_path, CASES[:1])
    async with Buggy() as adapter:
        outcome = await run_eval(config(tmp_path, ds), adapter, Console(file=StringIO()))
    r = outcome.results[0]
    assert r.failure_type is FailureType.PARSE_ERROR
    assert r.error_message == "internal error: RuntimeError('bug in our code')"


# --------------------------------------------------------------------------- aborts


@pytest.mark.parametrize(
    ("health", "probe", "match"),
    [
        (raises(httpx.ConnectError), ok("greeting"), "agent is not reachable"),
        (Reply(json={"status": "degraded"}), ok("greeting"), "agent is unhealthy"),
        (Reply(json=HEALTH | {"database": "down"}), ok("greeting"), "agent is unhealthy"),
        (Reply(json=HEALTH), Reply(401, text="no"), "rejected the API key"),
        (Reply(json=HEALTH), Reply(500, text="err"), r"probe call failed \(http_error\)"),
        (Reply(json=HEALTH), ok("error"), "probe call returned ok=false"),
    ],
)
async def test_preflight_aborts_before_creating_a_run(
    tmp_path: Path, health: Any, probe: Any, match: str
) -> None:
    agent = agent_for_cases().on("hi", probe)
    agent.health = health
    with pytest.raises(RunAborted, match=match):
        await run(config(tmp_path, write_dataset(tmp_path, CASES)), agent)
    assert not (tmp_path / "runs").exists()
    assert agent.messages.count(KQ) == 0


async def test_auth_error_mid_run_aborts_and_keeps_finished_results(tmp_path: Path) -> None:
    agent = agent_for_cases().on(DQ, Reply(401, text="key revoked"))
    ds = write_dataset(tmp_path, CASES)
    cfg = config(tmp_path, ds, concurrency=1)
    with pytest.raises(RunAborted, match=r"HTTP 401.*resume with: evalkit run") as exc:
        await run(cfg, agent)
    assert SENTINEL_KEY not in str(exc.value)
    (run_dir,) = (tmp_path / "runs").iterdir()
    results = list(read_jsonl(run_dir / RESULTS_FILE, CaseResult))
    assert [r.case_id for r in results] == ["k1"]
    meta = read_json(run_dir / META_FILE, RunMeta)
    assert meta.finished_at is None and meta.counts.total == 1


async def test_auth_error_during_warmup_aborts(tmp_path: Path) -> None:
    agent = agent_for_cases().on(WARMUP_CASE.question, Reply(403, text="no"))
    with pytest.raises(RunAborted, match="HTTP 403"):
        await run(config(tmp_path, write_dataset(tmp_path, CASES)), agent)


@pytest.mark.parametrize(
    ("content", "kwargs", "match"),
    [
        (None, {}, "dataset not found"),
        (b"{broken\n", {}, r"bad dataset: .*:1:"),
        (b"", {}, "no cases selected"),
        ("dupes", {}, r"duplicate case ids \['k1'\]"),
        ("cases", {"ids": ("k1", "nope")}, r"--ids not in dataset: \['nope'\]"),
        ("cases", {"route": Route.MEDICAL}, "no cases selected"),
    ],
)
def test_bad_datasets_abort(
    tmp_path: Path, content: Any, kwargs: dict[str, Any], match: str
) -> None:
    path = tmp_path / "ds.jsonl"
    if content == "dupes":
        write_jsonl(path, [CASES[0], CASES[0]])
    elif content == "cases":
        write_jsonl(path, CASES)
    elif content is not None:
        path.write_bytes(content)
    with pytest.raises(RunAborted, match=match):
        load_cases(path, **kwargs)


def test_filters_apply_ids_then_route_then_limit(tmp_path: Path) -> None:
    path = write_dataset(tmp_path, CASES)
    assert [c.id for c in load_cases(path, ids=["g1", "k1"])] == ["k1", "g1"]
    assert [c.id for c in load_cases(path, route=Route.DATABASE)] == ["d1"]
    assert [c.id for c in load_cases(path, limit=2)] == ["k1", "d1"]
    assert [c.id for c in load_cases(path, ids=["g1", "k1"], limit=1)] == ["k1"]


# --------------------------------------------------------------------------- warm-up


async def test_warmup_failure_is_logged_and_run_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    agent = agent_for_cases().on(WARMUP_CASE.question, Reply(500, text="cold"))
    outcome = await run(config(tmp_path, write_dataset(tmp_path, CASES)), agent)
    assert outcome.exit_code == EXIT_OK
    assert "warm-up call failed" in caplog.text


async def test_warmup_can_be_disabled_and_is_skipped_for_skip_only_runs(tmp_path: Path) -> None:
    agent = agent_for_cases()
    await run(config(tmp_path, write_dataset(tmp_path, CASES), warmup=False), agent)
    history_only = [make_case("h1", "x?", history=[{"role": "user", "content": "y"}])]
    await run(config(tmp_path, write_dataset(tmp_path, history_only, "h.jsonl")), agent)
    assert agent.calls(WARMUP_CASE.question) == 0


async def test_unknown_agent_fields_are_warned_about_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    agent = agent_for_cases()
    agent.default = ok("knowledge", shiny_new_field=1)
    two = [make_case("k1", KQ), make_case("k2", "Another question?")]
    await run(config(tmp_path, write_dataset(tmp_path, two)), agent)
    warnings = [r for r in caplog.records if "shiny_new_field" in r.getMessage()]
    assert len(warnings) == 1


# --------------------------------------------------------------------------- resume


async def test_resume_skips_finished_ids_and_drops_a_partial_line(tmp_path: Path) -> None:
    agent = agent_for_cases()
    ds = write_dataset(tmp_path, CASES)
    first = await run(config(tmp_path, ds, cache_mode=CacheMode.OFF), agent)
    results_path = first.run_dir / RESULTS_FILE
    lines = results_path.read_bytes().splitlines(keepends=True)
    kept_ids = [r.case_id for r in first.results[:2]]
    results_path.write_bytes(b"".join(lines[:2]) + b'{"run_id": "trunc')  # crash mid-write
    calls_before = {q: agent.calls(q) for q in (KQ, DQ, GQ)}

    resumed = await run(config(tmp_path, ds, cache_mode=CacheMode.OFF, resume=first.run_id), agent)
    assert resumed.run_id == first.run_id and resumed.meta.started_at == first.meta.started_at
    assert sorted(r.case_id for r in resumed.results) == ["d1", "g1", "k1"]
    rerun = {q for q in (KQ, DQ, GQ) if agent.calls(q) > calls_before[q]}
    missing = ({"k1": KQ, "d1": DQ, "g1": GQ}[i] for i in {"k1", "d1", "g1"} - set(kept_ids))
    assert rerun == set(missing)
    assert len(list(read_jsonl(results_path, CaseResult))) == 3
    assert resumed.meta.finished_at is not None and resumed.meta.counts.total == 3


async def test_resume_with_no_results_file_runs_everything(tmp_path: Path) -> None:
    agent = agent_for_cases()
    ds = write_dataset(tmp_path, CASES)
    first = await run(config(tmp_path, ds), agent)
    (first.run_dir / RESULTS_FILE).unlink()
    resumed = await run(config(tmp_path, ds, resume=first.run_id), agent)
    assert len(resumed.results) == 3


async def test_resume_warns_when_settings_differ(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    agent = agent_for_cases()
    ds = write_dataset(tmp_path, CASES)
    first = await run(config(tmp_path, ds), agent)
    await run(config(tmp_path, ds, concurrency=3, resume=first.run_id), agent)
    assert "run settings differ" in caplog.text


async def test_resume_refusals(tmp_path: Path) -> None:
    agent = agent_for_cases()
    ds = write_dataset(tmp_path, CASES)
    first = await run(config(tmp_path, ds), agent)

    for bad_id, match in [("../etc", "invalid run id"), ("20990101-000000-ffff", "no run")]:
        with pytest.raises(RunAborted, match=match):
            await run(config(tmp_path, ds, resume=bad_id), agent)

    results = first.run_dir / RESULTS_FILE
    good = results.read_bytes()
    results.write_bytes(b"garbage\n" + good)
    with pytest.raises(RunAborted, match="cannot resume"):
        await run(config(tmp_path, ds, resume=first.run_id), agent)
    results.write_bytes(good)

    changed = write_dataset(tmp_path, [*CASES, make_case("new", "New?")])
    assert changed == ds
    with pytest.raises(RunAborted, match="changed since run"):
        await run(config(tmp_path, ds, resume=first.run_id), agent)

    (first.run_dir / META_FILE).write_text("{}", encoding="utf-8")
    with pytest.raises(RunAborted, match="unreadable"):
        await run(config(tmp_path, ds, resume=first.run_id), agent)


# --------------------------------------------------------------------------- concurrency


async def test_semaphore_never_exceeds_the_concurrency_limit(tmp_path: Path) -> None:
    agent = FakeAgent(delay=0.02)
    cases = [make_case(f"k{i}", f"Question number {i}?") for i in range(12)]
    outcome = await run(config(tmp_path, write_dataset(tmp_path, cases), concurrency=3), agent)
    assert len(outcome.results) == 12
    assert agent.max_in_flight == 3


async def test_ctrl_c_stops_cleanly_and_is_resumable(tmp_path: Path) -> None:
    agent = FakeAgent(delay=0.05)
    cases = [make_case(f"k{i}", f"Question number {i}?") for i in range(6)]
    cfg = config(tmp_path, write_dataset(tmp_path, cases), concurrency=1, warmup=False)
    console = Console(file=StringIO(), width=200)
    async with agent.adapter() as adapter:
        task = asyncio.create_task(run_eval(cfg, adapter, console))
        while not list((tmp_path / "runs").glob(f"*/{RESULTS_FILE}")) or (
            next((tmp_path / "runs").glob(f"*/{RESULTS_FILE}")).read_bytes().count(b"\n") < 2
        ):
            await asyncio.sleep(0.01)
        task.cancel()
        outcome = await task

    assert outcome.interrupted and outcome.exit_code == EXIT_INTERRUPTED
    on_disk = list(read_jsonl(outcome.run_dir / RESULTS_FILE, CaseResult))
    assert 2 <= len(on_disk) < 6 and len(on_disk) == len(outcome.results)
    meta = read_json(outcome.run_dir / META_FILE, RunMeta)
    assert meta.finished_at is None and meta.counts.total == len(on_disk)

    resumed = await run(config(tmp_path, cfg.dataset, concurrency=1, resume=outcome.run_id), agent)
    assert len(resumed.results) == 6 and resumed.exit_code == EXIT_OK


# --------------------------------------------------------------------------- secrets


async def test_api_key_never_appears_in_logs_output_or_files(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    agent = agent_for_cases().on(KQ, Reply(500, text="boom")).on(GQ, Reply(text="not json"))
    agent.default = ok("knowledge", extra_field="x")
    cases = [*CASES, make_case("k2", "Something else?")]
    console = Console(file=StringIO(), width=200)
    await run(config(tmp_path, write_dataset(tmp_path, cases)), agent, console)
    with pytest.raises(RunAborted):
        await run(config(tmp_path, write_dataset(tmp_path, CASES)), agent.on("hi", Reply(401)))

    assert agent.api_keys and all(k == SENTINEL_KEY for k in agent.api_keys)  # it was sent...
    assert SENTINEL_KEY not in caplog.text  # ...but never logged,
    assert SENTINEL_KEY not in console.file.getvalue()  # type: ignore[attr-defined]
    for path in tmp_path.rglob("*"):  # ...nor written to runs/ or the cache
        if path.is_file():
            assert SENTINEL_KEY.encode() not in path.read_bytes(), path


# --------------------------------------------------------------------------- summary & misc


async def test_summary_route_accuracy_latency_and_printing(tmp_path: Path) -> None:
    agent = agent_for_cases().on(GQ, ok("knowledge")).on(DQ, Reply(500, text="x"))
    cases = [*CASES, make_case("m1", "hm", route="greeting")]
    outcome = await run(config(tmp_path, write_dataset(tmp_path, cases)), agent)
    s = outcome.summary()
    assert (s.route_correct, s.route_checked) == (1, 3)
    assert s.failures == {"http_error": 1} and s.ok == 3
    assert [row.route for row in s.latency] == ["knowledge"] and s.latency[0].n == 3

    out = Console(file=StringIO(), width=200)
    print_summary(out, outcome)
    text = out.file.getvalue()  # type: ignore[attr-defined]
    assert "route accuracy" in text and "33% (1/3)" in text and "failed: http_error" in text
    assert "Latency by actual route" in text

    cached = await run(config(tmp_path, write_dataset(tmp_path, CASES[:1], "one.jsonl")), agent)
    out = Console(file=StringIO(), width=200)
    print_summary(out, cached)
    assert "no non-cached responses" in out.file.getvalue()  # type: ignore[attr-defined]


def test_summary_of_errors_only() -> None:
    s = summarize([], {})
    assert s.route_accuracy is None and s.total == 0


async def test_print_summary_without_route_checks(tmp_path: Path) -> None:
    agent = agent_for_cases().on(KQ, ok("error"))
    outcome = await run(config(tmp_path, write_dataset(tmp_path, CASES[:1])), agent)
    out = Console(file=StringIO(), width=200)
    print_summary(out, outcome)
    assert "n/a" in out.file.getvalue()  # type: ignore[attr-defined]
    assert outcome.summary().latency[0].route == "none"  # ok=false has route null


def test_percentile() -> None:
    assert percentile([5.0], 0.95) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert percentile([4.0, 1.0, 3.0, 2.0], 0.95) == pytest.approx(3.85)
    with pytest.raises(ValueError):
        percentile([], 0.5)


def test_run_config_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        RunConfig(dataset=tmp_path, concurrency=0)
    with pytest.raises(ValueError, match="limit"):
        RunConfig(dataset=tmp_path, limit=0)


def test_resume_command_repeats_the_flags() -> None:
    base = RunConfig(dataset=Path("datasets/sample_5.jsonl"))
    assert resume_command(base, "r1") == "evalkit run --dataset datasets/sample_5.jsonl --resume r1"
    full = RunConfig(
        dataset=Path("my data.jsonl"),
        concurrency=4,
        limit=3,
        route=Route.KNOWLEDGE,
        ids=("a", "b"),
        cache_mode=CacheMode.OFF,
    )
    assert resume_command(full, "r1") == (
        'evalkit run --dataset "my data.jsonl" --resume r1 --concurrency 4 --limit 3 '
        "--route knowledge --ids a,b --no-cache"
    )
    refresh = RunConfig(dataset=Path("d.jsonl"), cache_mode=CacheMode.REFRESH)
    assert resume_command(refresh, "r1").endswith("--refresh")


def test_new_run_id_is_a_slug() -> None:
    from datetime import UTC, datetime

    run_id = new_run_id(datetime(2026, 10, 13, 9, 15, tzinfo=UTC))
    assert run_id.startswith("20261013-091500-") and len(run_id) == 20
    assert new_run_id() != new_run_id()


def test_git_info(tmp_path: Path) -> None:
    assert git_info(tmp_path) == (None, False)
    sha, dirty = git_info(ROOT)
    assert sha is not None and len(sha) == 40 and isinstance(dirty, bool)


async def test_list_runs_newest_first_and_skips_unreadable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    agent = agent_for_cases()
    ds = write_dataset(tmp_path, CASES)
    a = await run(config(tmp_path, ds), agent)
    b = await run(config(tmp_path, ds), agent)
    broken = tmp_path / "runs" / "broken"
    broken.mkdir()
    (broken / META_FILE).write_text("{}", encoding="utf-8")
    assert [m.run_id for m in list_runs(tmp_path / "runs")] == [b.run_id, a.run_id]
    assert "skipping unreadable" in caplog.text


async def test_null_expected_route_is_left_out_of_route_accuracy(tmp_path: Path) -> None:
    agent = agent_for_cases()
    unchecked = make_case("u1", "Will the sunscreen help my acne?", expected_route=None)
    outcome = await run(config(tmp_path, write_dataset(tmp_path, [*CASES, unchecked])), agent)
    s = outcome.summary()
    assert s.ok == 4 and (s.route_correct, s.route_checked) == (3, 3)
