"""Day 25 run limits for a free-tier LLM: pacing, an LLM-call cap and clean stops (rate limit /
quota, LLM unavailable, --stop-on-failure), agent provenance. Fake agent, no network."""

import dataclasses
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evalkit.cache import CacheMode
from evalkit.io import read_json, read_jsonl
from evalkit.runner import (
    EXIT_STOPPED,
    LLM_UNAVAILABLE,
    META_FILE,
    RESULTS_FILE,
    RunConfig,
    agent_repo_info,
    is_quota_error,
    resume_command,
)
from evalkit.schemas import CaseResult, RunMeta

from .agent_double import fixture, make_case, ok
from .test_runner import DQ, GQ, KQ, agent_for_cases, config, run, write_dataset

KNOWLEDGE = [make_case(f"k{i}", f"{KQ} ({i})") for i in range(5)]


def quota_reply() -> Any:
    return ok("error", error="RuntimeError: gemini returned error 429. Rate limit still hit")


def fallback_reply() -> Any:
    details = fixture("knowledge")["details"] | {"llm_error": "gemini returned error 503"}
    return ok("knowledge", details=details, prompt_tokens=None, answer_type="quoted_source")


def test_quota_markers() -> None:
    assert is_quota_error("gemini returned error 429") and is_quota_error("Quota exceeded")
    assert is_quota_error("RESOURCE_EXHAUSTED") and not is_quota_error("timeout")
    assert not is_quota_error(None)
    with pytest.raises(ValueError):
        RunConfig(dataset=Path("x"), max_rpm=0)
    with pytest.raises(ValueError):
        RunConfig(dataset=Path("x"), max_llm_calls=0)


async def test_quota_error_stops_cleanly_and_resume_reruns_that_case(tmp_path: Path) -> None:
    agent = agent_for_cases()
    for i, c in enumerate(KNOWLEDGE):
        agent.on(c.question, quota_reply() if i == 2 else ok("knowledge"))
    ds = write_dataset(tmp_path, KNOWLEDGE)
    cfg = config(tmp_path, ds, concurrency=1, warmup=False)
    outcome = await run(cfg, agent)
    assert outcome.exit_code == EXIT_STOPPED and "rate limit / quota" in (outcome.stopped or "")
    done = [r.case_id for r in read_jsonl(outcome.run_dir / RESULTS_FILE, CaseResult)]
    assert done == ["k0", "k1"]  # k2 stopped the run and is not recorded; k3, k4 never ran
    meta = read_json(outcome.run_dir / META_FILE, RunMeta)
    assert meta.finished_at is None and "k2" in meta.run_settings["stop_reason"]
    agent.on(KNOWLEDGE[2].question, ok("knowledge"))
    resumed = await run(config(tmp_path, ds, concurrency=1, warmup=False,
                               resume=outcome.run_id), agent)  # fmt: skip
    assert resumed.exit_code == 0 and len(resumed.results) == 5


async def test_llm_fallback_stops_and_is_never_cached(tmp_path: Path) -> None:
    agent = agent_for_cases().on(KNOWLEDGE[0].question, fallback_reply())
    ds = write_dataset(tmp_path, KNOWLEDGE[:2])
    outcome = await run(config(tmp_path, ds, concurrency=1, warmup=False), agent)
    assert outcome.stopped and "LLM unavailable" in outcome.stopped and outcome.results == []
    assert outcome.llm_calls == 1  # the fallback counts: the agent tried its LLM
    assert LLM_UNAVAILABLE.startswith("LLM unavailable")


async def test_llm_call_cap_counts_warmup_and_stops_before_exceeding(tmp_path: Path) -> None:
    agent = agent_for_cases()
    cases = [*KNOWLEDGE[:3], make_case("g1", GQ, "greeting"), make_case("d1", DQ, "database")]
    ds = write_dataset(tmp_path, cases)
    outcome = await run(config(tmp_path, ds, concurrency=1, max_llm_calls=3), agent)
    # warm-up (knowledge, uses the LLM) + 2 knowledge cases = 3: the cap stops the rest
    assert outcome.llm_calls == 3 and outcome.exit_code == EXIT_STOPPED
    assert "--max-llm-calls 3" in (outcome.stopped or "") and len(outcome.results) == 2


async def test_stop_on_failure_and_pacing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = agent_for_cases().on(KNOWLEDGE[1].question, ok("error", error="boom"))
    ds = write_dataset(tmp_path, KNOWLEDGE[:3])
    plain = await run(config(tmp_path, ds, concurrency=1, warmup=False), agent)
    assert plain.stopped is None and len(plain.results) == 3  # a failure alone doesn't stop
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("evalkit.runner.asyncio.sleep", fake_sleep)
    times = iter(float(t) for t in range(100))
    monkeypatch.setattr("evalkit.runner.time.monotonic", lambda: next(times))
    cfg = config(tmp_path, ds, concurrency=1, warmup=False, stop_on_failure=True, max_rpm=10,
                 cache_mode=CacheMode.OFF)  # fmt: skip
    outcome = await run(cfg, agent)
    assert outcome.stopped and "failure (agent_error)" in outcome.stopped
    assert [r.case_id for r in outcome.results] == ["k0"]
    assert sleeps and all(s <= 6.0 for s in sleeps)  # 10/min = one request every 6 s
    cmd = resume_command(cfg, outcome.run_id)
    assert "--max-rpm 10" in cmd and "--stop-on-failure" in cmd
    from_file = resume_command(dataclasses.replace(cfg, ids=("k0",), ids_file=Path("ids.txt")),
                               outcome.run_id)  # fmt: skip
    assert "--ids @ids.txt" in from_file and "k0" not in from_file.split("--ids")[1]


def test_agent_repo_info(tmp_path: Path) -> None:
    repo = tmp_path / "agent"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "eval-fixes")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "router.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "rag" / "chroma_store").mkdir(parents=True)
    (repo / "rag" / "chroma_store" / "db.bin").write_text("a", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    info = agent_repo_info(repo)
    assert info["branch"] == "eval-fixes" and len(info["commit"]) == 40 and not info["dirty_code"]
    (repo / "rag" / "chroma_store" / "db.bin").write_text("b", encoding="utf-8")
    assert not agent_repo_info(repo)["dirty_code"]  # vector-store data isn't code
    (repo / "router.py").write_text("x = 2\n", encoding="utf-8")
    dirty = agent_repo_info(repo)
    assert dirty["dirty_code"] and dirty["dirty_files"] == ["router.py"]
    assert agent_repo_info(tmp_path / "nope") == {}
