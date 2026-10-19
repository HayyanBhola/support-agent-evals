import dataclasses
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from evalkit import cli, runner
from evalkit.adapters.base import ConfigError
from evalkit.cache import CacheMode
from evalkit.io import write_jsonl
from evalkit.schemas import Route

from .agent_double import FakeAgent, Reply, make_case, ok, raises

CASES = [make_case("k1", "How long does shipping take?"), make_case("g1", "hello", "greeting")]


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeAgent:
    """A fake agent wired into the CLI; runs/ and .cache/ land in tmp_path."""
    fake = FakeAgent().on("hello", ok("greeting"))
    monkeypatch.setattr(cli, "make_adapter", fake.adapter)
    monkeypatch.chdir(tmp_path)
    return fake


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    path = tmp_path / "ds.jsonl"
    write_jsonl(path, CASES)
    return path


def invoke(*args: str) -> Any:
    return CliRunner().invoke(cli.app, list(args))


# --------------------------------------------------------------------------- health


def test_health_ok(agent: FakeAgent) -> None:
    result = invoke("health")
    assert result.exit_code == 0, result.output
    assert '"status": "ok"' in result.output and "API key works" in result.output


@pytest.mark.parametrize(
    ("health", "probe", "match"),
    [
        (raises(httpx.ConnectError), ok("greeting"), "agent not reachable"),
        (Reply(json={"status": "ok"}), Reply(401, text="no"), "API key rejected"),
        (Reply(json={"status": "ok"}), Reply(404, text="?"), "probe call failed"),
        (Reply(json={"status": "degraded"}), ok("greeting"), "API key works"),
    ],
)
def test_health_problems_exit_2(agent: FakeAgent, health: Any, probe: Any, match: str) -> None:
    agent.health = health
    agent.on("hi", probe)
    result = invoke("health")
    assert result.exit_code == 2 and match in result.output


@pytest.mark.parametrize("command", [["health"], ["run", "--dataset", "x.jsonl"]])
def test_config_error_exits_2(monkeypatch: pytest.MonkeyPatch, command: list[str]) -> None:
    def broken(**_: Any) -> None:
        raise ConfigError("missing AGENT_API_KEY")

    monkeypatch.setattr(cli, "make_adapter", broken)
    result = invoke(*command)
    assert result.exit_code == 2 and "missing AGENT_API_KEY" in result.output


# --------------------------------------------------------------------------- run


def test_run_passes_read_timeout_to_the_adapter(
    agent: FakeAgent, dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[float | None] = []

    def make(read_timeout: float | None = None) -> Any:
        seen.append(read_timeout)
        return agent.adapter(read_timeout=read_timeout or 99.0)

    monkeypatch.setattr(cli, "make_adapter", make)
    assert invoke("run", "--dataset", str(dataset), "--read-timeout", "240").exit_code == 0
    assert invoke("run", "--dataset", str(dataset)).exit_code == 0
    assert seen == [240.0, None]  # None: the adapter falls back to the env / default


def test_run_ok_exits_0_and_runs_lists_it(agent: FakeAgent, dataset: Path) -> None:
    result = invoke("run", "--dataset", str(dataset), "--concurrency", "1")
    assert result.exit_code == 0, result.output
    assert "route accuracy" in result.output and "100% (2/2)" in result.output

    listing = invoke("runs")
    assert listing.exit_code == 0
    assert "ds.jsonl" in listing.output and "done" in listing.output


def test_run_with_failed_case_exits_1(agent: FakeAgent, dataset: Path) -> None:
    agent.on("hello", Reply(500, text="boom"))
    result = invoke("run", "--dataset", str(dataset))
    assert result.exit_code == 1 and "failed: http_error" in result.output


def test_run_aborted_exits_2(agent: FakeAgent, dataset: Path) -> None:
    agent.on("hi", Reply(401, text="no"))
    result = invoke("run", "--dataset", str(dataset))
    assert result.exit_code == 2 and "aborted:" in result.output


def test_bad_dataset_exits_2(agent: FakeAgent, tmp_path: Path) -> None:
    result = invoke("run", "--dataset", str(tmp_path / "missing.jsonl"))
    assert result.exit_code == 2 and "dataset not found" in result.output


def test_no_cache_and_refresh_together_is_a_usage_error(agent: FakeAgent, dataset: Path) -> None:
    result = invoke("run", "--dataset", str(dataset), "--no-cache", "--refresh")
    assert result.exit_code == 2 and agent.messages == []


def test_filters_and_cache_flags_reach_the_runner(
    agent: FakeAgent, dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[runner.RunConfig] = []

    async def fake_run_eval(config: runner.RunConfig, *args: Any) -> Any:
        seen.append(config)
        raise runner.RunAborted("stop here")

    monkeypatch.setattr(runner, "run_eval", fake_run_eval)
    args = ["--dataset", str(dataset), "--ids", "k1, g1,", "--route", "greeting", "--limit", "1"]
    invoke("run", *args, "--no-cache")
    invoke("run", *args, "--refresh", "--resume", "20261013-000000-abcd")
    assert seen[0].ids == ("k1", "g1") and seen[0].route is Route.GREETING
    assert seen[0].limit == 1 and seen[0].cache_mode is CacheMode.OFF
    assert seen[1].cache_mode is CacheMode.REFRESH and seen[1].resume


def test_ctrl_c_before_the_run_starts_exits_130(
    agent: FakeAgent, dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def interrupted(*args: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "run_eval", interrupted)
    result = invoke("run", "--dataset", str(dataset))
    assert result.exit_code == 130 and "interrupted" in result.output


def test_interrupted_run_prints_resume_with_read_timeout(
    agent: FakeAgent, dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = runner.run_eval

    async def interrupted(*args: Any) -> Any:
        return dataclasses.replace(await real(*args), interrupted=True)

    monkeypatch.setattr(runner, "run_eval", interrupted)
    result = invoke("run", "--dataset", str(dataset), "--read-timeout", "240")
    assert result.exit_code == 130
    assert "--resume" in result.output and "--read-timeout 240" in result.output


def test_runs_with_no_runs(agent: FakeAgent) -> None:
    result = invoke("runs")
    assert result.exit_code == 0 and "no runs in runs" in result.output


def test_run_ids_file_limits_and_clean_stop(
    agent: FakeAgent, dataset: Path, tmp_path: Path
) -> None:
    ids = tmp_path / "ids.txt"
    ids.write_text("k1\n\ng1\n", encoding="utf-8")
    res = invoke("run", "--dataset", str(dataset), "--ids", f"@{ids}", "--max-rpm", "600",
                 "--max-retries", "0", "--concurrency", "1", "--no-cache")  # fmt: skip
    assert res.exit_code == 0, res.output
    agent.on("How long does shipping take?", ok("error", error="gemini returned error 429"))
    stopped = invoke("run", "--dataset", str(dataset), "--ids", f"@{ids}", "--no-cache",
                     "--max-llm-calls", "5", "--max-retries", "0")  # fmt: skip
    flat = " ".join(stopped.output.split())
    assert stopped.exit_code == runner.EXIT_STOPPED and "stopped cleanly" in flat
    assert "--max-llm-calls 5" in flat and "--max-retries 0" in flat
    assert "--ids @" in flat and "ids.txt" in flat and "stopped, resumable" in flat
    assert "k1,g1" not in flat  # the file, not the list of ids
    missing = invoke("run", "--dataset", str(dataset), "--ids", "@nope.txt")
    assert missing.exit_code == 2 and "cannot read --ids file" in missing.output
