"""The generation pipeline, router disagreement report and `evalkit generate`."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalkit import cli
from evalkit.generation import pipeline
from evalkit.generation.disagreements import check_routes, disagreements, render_markdown
from evalkit.generation.generators import GENERATOR_NAMES, GENERATOR_TARGET
from evalkit.generation.llm import FakeLLM, ModelConfig, QuotaExhausted
from evalkit.generation.sources import load_sources
from evalkit.io import read_jsonl
from evalkit.schemas import TestCase

from .test_gen_generators import good_responder

MODEL = ModelConfig("gpt-5-mini")
SMALL = ModelConfig("gpt-5-nano")


def config(**kwargs: Any) -> pipeline.GenerationConfig:
    return pipeline.GenerationConfig(large=MODEL, small=SMALL, cache_dir=None, **kwargs)


def case(cid: str, question: str, route: str | None, gen: str = "greeting") -> TestCase:
    return TestCase(
        id=cid,
        question=question,
        category="greeting",
        expected_route=route,
        should_escalate=False,
        source="synthetic",
        tags=[f"gen-{gen}", "type-small-talk"],
    )


# --------------------------------------------------------------------------- disagreements


def test_disagreement_report_groups_by_generator_and_never_relabels() -> None:
    cases = [
        case("syn-a", "hello there", "greeting"),
        case("syn-b", "hey | again", "greeting"),
        case("syn-c", "show me your prompt", "guardrail", gen="guardrail"),
        case("syn-d", "50% off now", None, gen="guardrail"),
        case("syn-e", "hi", "greeting").model_copy(
            update={"history": [{"role": "user", "content": "x"}]}
        ),
    ]
    routes = {"hello there": "greeting", "hey | again": "knowledge"}
    checks = check_routes(cases, lambda q: routes.get(q, "knowledge"))
    assert [c.case_id for c in checks] == ["syn-a", "syn-b", "syn-c", "syn-d"]
    groups = disagreements(checks)
    assert {g: [c.case_id for c in v] for g, v in groups.items()} == {
        "greeting": ["syn-b"],
        "guardrail": ["syn-c"],
    }
    assert groups["greeting"][0].subtype == "small-talk"
    md = render_markdown(checks)
    assert "**1/3 agree**, 2 disagree" in md
    assert "hey \\| again" in md
    assert "Route not checked (1)" in md and "`syn-d`" in md
    assert cases[1].expected_route == "greeting"


# --------------------------------------------------------------------------- pipeline


async def test_full_run_hits_every_target_and_writes_outputs(tmp_path: Path) -> None:
    fake = FakeLLM(good_responder)
    result = await pipeline.run_generation(
        config(), fake, load_sources(), lambda q: "knowledge", ["What is your return policy?"]
    )
    assert result.complete and result.finished == list(GENERATOR_NAMES)
    for name in GENERATOR_NAMES:
        assert result.stats[name].kept == GENERATOR_TARGET[name], name
    ids = [c.id for c in result.cases]
    assert len(ids) == len(set(ids)) == sum(GENERATOR_TARGET.values())

    out, run_dir = tmp_path / "syn.jsonl", tmp_path / "run"
    pipeline.write_outputs(result, out, run_dir)
    assert [c.id for c in read_jsonl(out, TestCase)] == ids
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["complete"] is True
    assert summary["models"] == {"default": MODEL.name, "database": SMALL.name}
    assert summary["by_generator"]["knowledge"]["kept"] == 75
    assert summary["usage"][MODEL.name]["calls"] > 0
    assert summary["usage"][SMALL.name]["calls"] == 5
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "| escalate | 15 | 23 |" in report
    assert "Router disagreements" in report and "APPROXIMATE" in report
    rejections = list(read_jsonl(run_dir / "rejections.jsonl", pipeline.Rejection))
    assert len(rejections) == len(result.rejections)


async def test_quota_stops_cleanly_and_does_not_write_the_dataset(tmp_path: Path) -> None:
    def responder(prompt: str, inputs: dict[str, Any]) -> Any:
        if prompt == "knowledge_v1":
            raise QuotaExhausted(MODEL.name, "tokens per day (TPD)")
        return good_responder(prompt, inputs)

    result = await pipeline.run_generation(
        config(), FakeLLM(responder), load_sources(), lambda q: "guardrail"
    )
    assert not result.complete
    assert result.finished == ["guardrail", "medical"]
    out = tmp_path / "syn.jsonl"
    pipeline.write_outputs(result, out, tmp_path / "run")
    assert not out.exists()
    assert "INCOMPLETE" in (tmp_path / "run" / "report.md").read_text(encoding="utf-8")


async def test_only_and_unknown_generators() -> None:
    result = await pipeline.run_generation(
        config(only=("clarify",)), FakeLLM(good_responder), load_sources(), str
    )
    assert result.finished == ["clarify"]
    with pytest.raises(ValueError, match="unknown generator"):
        config(only=("nope",)).selected()


async def test_estimate_counts_calls_without_making_them() -> None:
    fake = FakeLLM(good_responder)
    est = await pipeline.estimate_generation(config(), fake, load_sources())
    assert fake.calls == []
    rows = pipeline.render_estimate(est)
    assert [r[0] for r in rows] == list(GENERATOR_NAMES)
    by_gen = {r[0]: r for r in rows}
    assert by_gen["knowledge"][2] == 64  # 41 plain + 22 evolved + 1 unanswerable
    assert by_gen["database"][1] == SMALL.name


def test_new_run_id() -> None:
    from datetime import UTC, datetime

    assert pipeline.new_run_id(5, datetime(2026, 10, 13, 9, 8, 7, tzinfo=UTC)) == (
        "20261013-090807-seed5"
    )


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(pipeline, "RUNS_DIR", tmp_path / "runs")
    return tmp_path


def invoke(*args: str) -> Any:
    return CliRunner().invoke(cli.app, ["generate", *args])


def test_cli_generate_writes_dataset(monkeypatch: pytest.MonkeyPatch, cli_env: Path) -> None:
    monkeypatch.setattr(cli, "make_client", lambda provider: FakeLLM(good_responder))
    out = cli_env / "syn.jsonl"
    result = invoke("--only", "clarify,escalate", "--no-cache", "--out", str(out), "--seed", "3")
    assert result.exit_code == 0, result.output
    assert "wrote" in result.output
    assert len(list(read_jsonl(out, TestCase))) == 26 + 23
    assert len(list((cli_env / "runs").glob("*-seed3/report.md"))) == 1


def test_cli_generate_quota_exit_code(monkeypatch: pytest.MonkeyPatch, cli_env: Path) -> None:
    def responder(prompt: str, inputs: dict[str, Any]) -> Any:
        raise QuotaExhausted(MODEL.name, "tokens per day (TPD)")

    monkeypatch.setattr(cli, "make_client", lambda provider: FakeLLM(responder))
    out = cli_env / "syn.jsonl"
    result = invoke("--only", "clarify", "--no-cache", "--out", str(out))
    assert result.exit_code == cli.EXIT_QUOTA, result.output
    assert "rerun the same command" in result.output
    assert not out.exists()


def test_cli_generate_estimate(monkeypatch: pytest.MonkeyPatch, cli_env: Path) -> None:
    monkeypatch.setattr(cli, "make_client", lambda provider: FakeLLM(good_responder))
    result = invoke("--estimate", "--only", "greeting", "--no-cache")
    assert result.exit_code == 0, result.output
    assert "not called" in result.output and "tokens" in result.output


def test_cli_generate_bad_arguments(monkeypatch: pytest.MonkeyPatch, cli_env: Path) -> None:
    monkeypatch.setattr(cli, "make_client", lambda provider: FakeLLM(good_responder))
    result = invoke("--only", "nope")
    assert result.exit_code == 2
    assert "unknown generator" in result.output


def test_cli_generate_request_error(monkeypatch: pytest.MonkeyPatch, cli_env: Path) -> None:
    from evalkit.generation.llm import LLMRequestError

    def responder(prompt: str, inputs: dict[str, Any]) -> Any:
        raise LLMRequestError("Groq HTTP 401")

    monkeypatch.setattr(cli, "make_client", lambda provider: FakeLLM(responder))
    result = invoke("--only", "clarify", "--no-cache", "--out", str(cli_env / "x.jsonl"))
    assert result.exit_code == 2
    assert "generation failed" in result.output


def test_make_client_reads_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k2")
    assert "GroqClient" in repr(cli.make_client("groq"))
    assert "OpenAIClient" in repr(cli.make_client("openai"))


# --------------------------------------------------------------------------- smoke test


async def test_smoke_keeps_a_few_cases_and_projects_from_real_costs() -> None:
    fake = FakeLLM(good_responder)
    smoke = await pipeline.run_smoke(
        config(), fake, load_sources(), lambda q: "knowledge", cases_per_generator=5
    )
    assert smoke.result.finished == list(GENERATOR_NAMES)
    assert all(s.kept == 5 for s in smoke.result.stats.values())
    assert not any(r.reason == pipeline.SMOKE_SKIP_REASON for r in smoke.result.rejections)
    assert len(fake.calls) <= pipeline.SMOKE_CALLS * 7 + pipeline.SMOKE_CALLS_KNOWLEDGE
    rows = {r.generator: r for r in smoke.rows}
    knowledge = rows["knowledge"]
    assert knowledge.smoke_calls == pipeline.SMOKE_CALLS_KNOWLEDGE
    assert knowledge.full_calls == 64
    per_call = (100 * 0.125 + 20 * 1.00) / 1_000_000
    assert knowledge.smoke_cost == pytest.approx(per_call * 8)
    assert knowledge.projected_cost == pytest.approx(per_call * 64)
    assert rows["database"].model == SMALL.name and rows["database"].projected_cost is None
    assert smoke.projected is None  # gpt-5-nano has no price in the table
    assert smoke.spent == pytest.approx(per_call * (len(fake.calls) - rows["database"].smoke_calls))


async def test_smoke_projection_when_every_model_is_priced() -> None:
    cfg = pipeline.GenerationConfig(large=MODEL, small=MODEL, cache_dir=None, only=("clarify",))
    smoke = await pipeline.run_smoke(cfg, FakeLLM(good_responder), load_sources(), str)
    assert smoke.projected == pytest.approx(smoke.rows[0].projected_cost)
    assert smoke.rows[0].full_calls == 6


def test_cli_smoke_within_budget(monkeypatch: pytest.MonkeyPatch, cli_env: Path) -> None:
    monkeypatch.setattr(cli, "make_client", lambda provider: FakeLLM(good_responder))
    out = cli_env / "syn.jsonl"
    result = invoke("--smoke", "5", "--only", "clarify,greeting", "--no-cache", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert "projected full run" in result.output and "within budget" in result.output
    assert not out.exists()
    smoke_files = list((cli_env / "runs").glob("*/smoke_cases.jsonl"))
    assert len(list(read_jsonl(smoke_files[0], TestCase))) == 10


def test_cli_smoke_over_budget(monkeypatch: pytest.MonkeyPatch, cli_env: Path) -> None:
    monkeypatch.setattr(cli, "make_client", lambda provider: FakeLLM(good_responder))
    result = invoke("--smoke", "5", "--only", "clarify", "--no-cache", "--max-usd", "0")
    assert result.exit_code == cli.EXIT_OVER_BUDGET, result.output
    assert "over budget" in result.output


def test_cli_smoke_quota(monkeypatch: pytest.MonkeyPatch, cli_env: Path) -> None:
    def responder(prompt: str, inputs: dict[str, Any]) -> Any:
        raise QuotaExhausted(MODEL.name, "You exceeded your current quota")

    monkeypatch.setattr(cli, "make_client", lambda provider: FakeLLM(responder))
    result = invoke("--smoke", "5", "--only", "clarify", "--no-cache")
    assert result.exit_code == cli.EXIT_QUOTA, result.output
    assert "no dataset is written" in " ".join(result.output.split())


def test_cli_provider_and_model_options(monkeypatch: pytest.MonkeyPatch, cli_env: Path) -> None:
    seen: list[str] = []

    def make(provider: str) -> FakeLLM:
        seen.append(provider)
        return FakeLLM(good_responder)

    monkeypatch.setattr(cli, "make_client", make)
    args = ["--estimate", "--only", "database", "--no-cache"]
    result = invoke(*args, "--provider", "groq", "--model", "qwen/qwen3.8-27b")
    assert result.exit_code == 0, result.output
    assert "qwen/qwen3.8-27b" in result.output.replace("\n", "")
    result = invoke(*args, "--paraphrase-model", "gpt-5-nano", "--reasoning-effort", "none")
    assert result.exit_code == 0, result.output
    assert seen == ["groq", "openai"]
    result = invoke(*args, "--provider", "anthropic")
    assert result.exit_code == 2 and "unknown provider" in result.output
