"""The judge framework (Day 16), with a fake provider and a mock transport: no network."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from evalkit import cli
from evalkit.calibration.split import read_split
from evalkit.generation.llm import Completion, LLMRequestError, ModelConfig
from evalkit.io import read_jsonl, write_jsonl
from evalkit.judges import client as jc
from evalkit.judges import prompts, report, runner
from evalkit.judges.inputs import input_sha256, judge_input
from evalkit.judges.schema import JudgeResult, JudgeVerdict
from evalkit.rubrics import load_rubrics, rubric_hash
from evalkit.schemas import AgentResponse, CaseResult, Criterion, Label, TestCase

ROOT = Path(__file__).resolve().parent.parent
RUBRICS = ROOT / "rubrics"
SPLIT_FILE = ROOT / "datasets" / "splits" / "split_v1.json"
SPLIT = read_split(SPLIT_FILE)
RUBRIC = {r.criterion: r for r in load_rubrics(RUBRICS, None)}
RHASH = rubric_hash(RUBRICS)
DEV, TEST = SPLIT.ids("dev"), SPLIT.ids("test")
REPLY = "Our return window is 30 days from delivery."
RELEVANCE_V1_SHA = "f57930f2127b2aa615326ca93163f6b4259c125fce414cb2fb3de5a61c916b05"


def cfg(criterion: Criterion = Criterion.RELEVANCE) -> runner.JudgeConfig:
    prompt = prompts.build_prompt(RUBRIC[criterion], RHASH, SPLIT)
    return runner.JudgeConfig(criterion, jc.judge_model_config(), prompt, RHASH)


def case(cid: str, **over: Any) -> TestCase:
    data: dict[str, Any] = {"id": cid, "question": f"How long do I have to return? ({cid})",
                            "category": "x", "expected_route": "knowledge",
                            "should_escalate": False, "difficulty": "easy",
                            "source": "synthetic"}  # fmt: skip
    return TestCase.model_validate(data | over)


def result(cid: str, reply: str = REPLY, ok: bool = True) -> CaseResult:
    base: dict[str, Any] = {"run_id": "r", "case_id": cid, "attempts": 1, "cache_hit": False,
                            "wall_latency_ms": 1.0, "started_at": "2026-10-13T00:00:00Z",
                            "finished_at": "2026-10-13T00:00:01Z"}  # fmt: skip
    if not ok:
        return CaseResult.model_validate({**base, "failure_type": "skipped"})
    resp = {"ok": True, "reply": reply, "route": "knowledge", "answer_type": "llm",
            "escalated": False}  # fmt: skip
    return CaseResult.model_validate({**base, "response": resp})


class FakeJudge:
    """Scripted contents; records every call."""

    def __init__(self, contents: list[str] | Callable[[list[dict[str, str]]], str]) -> None:
        self.contents = contents
        self.calls: list[list[dict[str, str]]] = []
        self.thinking = "fake"
        self.last_model = "qwen/qwen3.8-27b"

    async def complete(
        self, model: ModelConfig, messages: list[dict[str, str]], seed: int, schema: Any
    ) -> Completion:
        self.calls.append(messages)
        content = (self.contents(messages) if callable(self.contents)
                   else self.contents[len(self.calls) - 1])  # fmt: skip
        return Completion(content=content, prompt_tokens=1000, completion_tokens=40)


def ok_json(verdict: str = "pass", evidence: str = "return window is 30 days") -> str:
    return json.dumps({"verdict": verdict, "reason": "It answers the question.",
                       "evidence": evidence})  # fmt: skip


# --------------------------------------------------------------------------- prompts


def test_prompt_is_built_from_the_rubric_and_the_committed_file_is_current() -> None:
    text = prompts.build_prompt(RUBRIC[Criterion.RELEVANCE], RHASH, SPLIT)
    rubric = RUBRIC[Criterion.RELEVANCE]
    assert rubric.definition in text and RHASH in text
    for section in ("Key rules", "Pass if", "Fail if", "Edge cases", "Pass examples"):
        assert f"## {section}" in text and rubric.sections[section].strip() in text
    assert '"verdict": "pass" | "fail"' in text
    pdir = ROOT / "prompts" / "judges"
    # v1 is frozen once committed (its results are cached under its sha256) ...
    v1 = prompts.load_prompt(Criterion.RELEVANCE, pdir, "v1")
    assert prompts.prompt_sha256(v1) == RELEVANCE_V1_SHA
    # ... and the newest committed version must match a fresh build from the current rubric
    latest = max(v for v in prompts.VERSIONS if prompts.prompt_path(
        Criterion.RELEVANCE, pdir, v).exists())  # fmt: skip
    fresh = prompts.build_prompt(rubric, RHASH, SPLIT, latest)
    assert prompts.load_prompt(Criterion.RELEVANCE, pdir, latest) == fresh, (
        f"run `evalkit judge prompt --criterion relevance --prompt-version {latest}`"
    )
    assert prompts.prompt_sha256(text) == prompts.prompt_sha256(text.replace("\n", "\r\n"))
    system = prompts.system_text(text)
    assert "<!--" not in system and system.startswith("# Judge: relevance")


def test_prompt_never_contains_a_test_case() -> None:
    for c in Criterion:
        text = prompts.build_prompt(RUBRIC[c], RHASH, SPLIT)
        assert not [cid for cid in TEST if cid in text], c
    bad = SPLIT.model_copy(update={"splits": {"dev": DEV, "test": [*TEST, "legacy-greeting"]}})
    with pytest.raises(AssertionError):  # a rubric example in test would leak
        prompts.build_prompt(RUBRIC[Criterion.TONE], RHASH, bad)


# --------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "content",
    [
        ok_json(),
        "```json\n" + ok_json() + "\n```",
        "<think>let me think {not json}</think>\n" + ok_json(),
        "Sure! " + ok_json() + " Done.",
        ok_json(evidence="RETURN   window is\n30 days"),  # case and whitespace don't matter
        ok_json(evidence='"Our return window ... 30 days"'),  # an elided quote
    ],
)
def test_parse_reply_accepts(content: str) -> None:
    assert runner.parse_reply(content, REPLY).verdict == "pass"


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("no json here", "no JSON object"),
        ("{verdict: pass}", "invalid JSON"),
        (json.dumps({"verdict": "maybe", "reason": "x", "evidence": "30 days"}), "wrong fields"),
        (json.dumps({"verdict": "pass", "reason": "x", "evidence": "30 days", "extra": 1}),
         "wrong fields"),
        (json.dumps({"verdict": "pass", "reason": "One. Two. Three.", "evidence": "30 days"}),
         "wrong fields"),
        (ok_json(evidence="we refund in 5 days"), "not a quote"),
        ("<think>unfinished thinking " + ok_json(), "no JSON object"),
    ],
)  # fmt: skip
def test_parse_reply_rejects(content: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        runner.parse_reply(content, REPLY)


def test_strip_thinking() -> None:
    assert jc.strip_thinking("<think>x</think>{}") == ("{}", True)
    assert jc.strip_thinking("reasoning...</think>{}") == ("{}", True)
    assert jc.strip_thinking("<THINK>open") == ("", True)
    assert jc.strip_thinking(" {} ") == ("{}", False)


def test_verdict_schema() -> None:
    v = JudgeVerdict(verdict="fail", reason=" Off topic. ", evidence=" quote ")
    assert (v.reason, v.evidence) == ("Off topic.", "quote")


# --------------------------------------------------------------------------- inputs


def test_judge_input_has_only_what_the_criterion_needs() -> None:
    c = case("c1", reference_answer="30 days", history=[{"role": "user", "content": "hi"}])
    resp = AgentResponse.model_validate({"ok": True, "reply": REPLY, "route": "knowledge",
                                         "answer_type": "llm", "escalated": False})  # fmt: skip
    rel = judge_input(Criterion.RELEVANCE, c, resp)
    assert set(rel) == {"question", "history", "reply", "route", "answer_type", "escalated"}
    assert rel["history"] == [{"role": "user", "content": "hi"}]
    cor = judge_input(Criterion.CORRECTNESS, c, resp)
    assert cor["reference_answer"] == "30 days" and "gold_db_facts" in cor
    assert "db_facts" not in cor  # correctness compares with the gold, not the reply's rows
    assert judge_input(Criterion.FAITHFULNESS, c, resp)["retrieved_chunks"] == []
    assert judge_input(Criterion.ESCALATION_APPROPRIATE, c, resp)["should_escalate"] is False
    assert input_sha256(rel) == input_sha256(dict(reversed(list(rel.items()))))


# --------------------------------------------------------------------------- plan / run


def setup(ids: list[str]) -> tuple[dict[str, CaseResult], dict[str, TestCase]]:
    return {cid: result(cid) for cid in ids}, {cid: case(cid) for cid in ids}


def test_plan_counts_skips_and_refuses_test(tmp_path: Path) -> None:
    ids = DEV[:4]
    results, cases = setup(ids)
    results[ids[3]] = result(ids[3], ok=False)
    cache = runner.JudgeCache(tmp_path)
    plan = runner.build_plan(cfg(), RUBRIC[Criterion.RELEVANCE], SPLIT, "dev", results, cases,
                             cache, only=ids)  # fmt: skip
    assert len(plan.items) == 3 and plan.unanswered == [ids[3]] and plan.calls == 3
    assert plan.est_tokens > 3 * runner.OUTPUT_TOKENS_ESTIMATE
    with pytest.raises(PermissionError, match="locked"):
        runner.build_plan(cfg(), RUBRIC[Criterion.RELEVANCE], SPLIT, "test", results, cases, cache)
    # faithfulness is n_a for a not_found reply: never sent
    nf = {ids[0]: CaseResult.model_validate({**results[ids[0]].model_dump(), "response": {
        "ok": True, "reply": "I'm sorry, I don't have that information.", "route": "knowledge",
        "answer_type": "not_found", "escalated": False}})}  # fmt: skip
    fp = runner.build_plan(cfg(Criterion.FAITHFULNESS), RUBRIC[Criterion.FAITHFULNESS], SPLIT,
                           "dev", nf, cases, cache, only=[ids[0]])  # fmt: skip
    assert fp.items == [] and fp.skipped_na == [ids[0]] and fp.calls == 0


def run(fake: FakeJudge, plan: runner.Plan, results: dict[str, CaseResult], tmp: Path,
        **kw: Any) -> tuple[list[JudgeResult], int]:  # fmt: skip
    replies = {cid: r.response for cid, r in results.items() if r.response}
    args: dict[str, Any] = {"max_calls": 50, "daily_cap": 200, "ledger": None} | kw
    return asyncio.run(runner.run_judge(fake, cfg(), plan, replies, runner.JudgeCache(tmp),
                                        **args))  # fmt: skip


def test_run_caches_repairs_and_marks_errors(tmp_path: Path) -> None:
    ids = DEV[:3]
    results, cases = setup(ids)
    cache = runner.JudgeCache(tmp_path)
    plan = runner.build_plan(cfg(), RUBRIC[Criterion.RELEVANCE], SPLIT, "dev", results, cases,
                             cache, only=ids)  # fmt: skip
    scripted = [ok_json(), "not json", ok_json("fail"), "bad", "still bad"]
    fake = FakeJudge(scripted)
    got, made = run(fake, plan, results, tmp_path)
    by = {r.case_id: r for r in got}
    assert made == 5 and len(fake.calls) == 5
    assert by[ids[0]].verdict == "pass" and by[ids[0]].attempts == 1
    assert by[ids[1]].verdict == "fail" and by[ids[1]].attempts == 2  # repaired
    assert "could not be used" in fake.calls[2][-1]["content"]  # the repair message
    assert by[ids[2]].verdict == "judge_error" and "no JSON" in (by[ids[2]].error or "")
    r0 = by[ids[0]]
    assert (r0.model, r0.rubric_hash, r0.prompt_sha256) == ("qwen/qwen3.8-27b", RHASH,
                                                             cfg().prompt_sha256)  # fmt: skip
    assert r0.input_tokens == 1000 and r0.output_tokens == 40 and r0.thinking == "fake"
    assert r0.raw == ok_json() and by[ids[2]].raw == "still bad"
    # second run: the two good results come from the cache; the error is retried
    again = runner.build_plan(cfg(), RUBRIC[Criterion.RELEVANCE], SPLIT, "dev", results, cases,
                              cache, only=ids)  # fmt: skip
    assert again.cached == 2 and again.calls == 1
    fake2 = FakeJudge([ok_json()])
    got2, made2 = run(fake2, again, results, tmp_path)
    assert made2 == 1 and sum(r.cache_hit for r in got2) == 2
    # bypassing the cache (self-consistency) calls everything and writes nothing
    fake3 = FakeJudge(lambda m: ok_json("fail"))
    got3, made3 = run(fake3, again, results, tmp_path, use_cache=False, write_cache=False)
    assert made3 == 3 and all(r.verdict == "fail" for r in got3)
    assert runner.JudgeCache(tmp_path).get(again.items[0].key).verdict == "pass"  # unchanged


def test_cache_key_depends_on_every_part() -> None:
    base = runner.cache_key(cfg(), "h")
    other_model = runner.JudgeConfig(Criterion.RELEVANCE, jc.judge_model_config("x/y"),
                                     cfg().prompt, RHASH)  # fmt: skip
    keys = {base, runner.cache_key(cfg(), "h2"), runner.cache_key(other_model, "h"),
            runner.cache_key(runner.JudgeConfig(Criterion.RELEVANCE, cfg().model, "p", RHASH), "h"),
            runner.cache_key(runner.JudgeConfig(Criterion.RELEVANCE, cfg().model, cfg().prompt,
                                                "r" * 64), "h"),
            runner.cache_key(runner.JudgeConfig(Criterion.RELEVANCE, jc.judge_model_config(
                max_tokens=10), cfg().prompt, RHASH), "h")}  # fmt: skip
    assert len(keys) == 6


def test_budget_guard(tmp_path: Path) -> None:
    ids = DEV[:3]
    results, cases = setup(ids)
    cache = runner.JudgeCache(tmp_path)
    plan = runner.build_plan(cfg(), RUBRIC[Criterion.RELEVANCE], SPLIT, "dev", results, cases,
                             cache, only=ids)  # fmt: skip
    with pytest.raises(runner.BudgetExceeded, match="--max-calls is 2"):
        run(FakeJudge([]), plan, results, tmp_path, max_calls=2)
    ledger = jc.Ledger(tmp_path / "usage.jsonl", today=lambda: datetime.now(UTC).date())
    for _ in range(199):
        ledger.record(model="m", purpose="t")
    with pytest.raises(runner.BudgetExceeded, match="daily cap 200"):
        run(FakeJudge([]), plan, results, tmp_path, ledger=ledger)
    fresh = jc.Ledger(tmp_path / "u2.jsonl")
    with pytest.raises(runner.BudgetExceeded, match="stopped before"):  # repairs could exceed
        run(FakeJudge([ok_json()] * 3), plan, results, tmp_path, max_calls=3, ledger=fresh)
    assert jc.Ledger(tmp_path / "u3.jsonl", today=lambda: date(2020, 1, 1)).calls_today() == 0


def test_consistency_sample_is_seeded(tmp_path: Path) -> None:
    results, cases = setup(DEV)
    plan = runner.build_plan(cfg(), RUBRIC[Criterion.RELEVANCE], SPLIT, "dev", results, cases,
                             runner.JudgeCache(tmp_path))  # fmt: skip
    a = runner.consistency_sample(plan, 20)
    assert a == runner.consistency_sample(plan, 20) and len(a) == 20 and set(a) <= set(DEV)
    assert a != runner.consistency_sample(plan, 20, seed=7)


# --------------------------------------------------------------------------- client


def groq_response(
    content: str, *, model: str = "qwen/qwen3.8-27b", **headers: str
) -> httpx.Response:
    body = {"model": model, "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 30}}  # fmt: skip
    return httpx.Response(200, json=body, headers=headers)


def test_client_disables_thinking_records_limits_and_counts(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return groq_response(ok_json(), **{"x-ratelimit-remaining-tokens": "5000",
                                           "x-ratelimit-reset-tokens": "7.5s",
                                           "x-ratelimit-limit-requests": "1000"})  # fmt: skip

    ledger = jc.Ledger(tmp_path / "usage.jsonl")
    client = jc.JudgeClient("k", ledger=ledger, transport=httpx.MockTransport(handler))
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    out = asyncio.run(client.complete(jc.judge_model_config(), msgs, 42, JudgeVerdict))
    asyncio.run(client.aclose())
    assert out.prompt_tokens == 900 and client.requests == 1 and ledger.calls_today() == 1
    assert seen[0]["reasoning_effort"] == "none" and seen[0]["temperature"] == 0.0
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert client.last_model == "qwen/qwen3.8-27b" and client.thinking == jc.THINKING_NONE
    assert client.limiter.remaining_tokens == 5000 and client.limiter.tokens_reset_s == 7.5
    assert client.limiter.limits["x-ratelimit-limit-requests"] == "1000"
    assert "redacted" in repr(client)


def test_client_falls_back_to_hidden_reasoning() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"error": {
                "message": "reasoning_effort is not supported for this model"}})  # fmt: skip
        return groq_response(ok_json())

    client = jc.JudgeClient("k", transport=httpx.MockTransport(handler))
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    asyncio.run(client.complete(jc.judge_model_config(), msgs, 42, JudgeVerdict))
    assert client.thinking == jc.THINKING_HIDDEN and seen[1]["reasoning_format"] == "hidden"
    assert client.requests == 2

    def other_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad model"}})

    bad = jc.JudgeClient("k", transport=httpx.MockTransport(other_error))
    with pytest.raises(LLMRequestError, match="bad model"):
        asyncio.run(bad.complete(jc.judge_model_config(), msgs, 42, JudgeVerdict))


def test_rate_limiter_waits() -> None:
    now = [100.0]
    limiter = jc.RateLimiter(rpm=30, clock=lambda: now[0])
    assert limiter.delay(1000) == 0.0
    limiter.mark()
    now[0] = 101.0
    assert limiter.delay(1000) == pytest.approx(1.0)  # 2 s between calls at 30 rpm
    limiter.observe(httpx.Headers({"x-ratelimit-remaining-tokens": "500",
                                   "x-ratelimit-reset-tokens": "10s"}))  # fmt: skip
    assert limiter.delay(1000) == pytest.approx(10.0)  # not enough tokens left: wait reset
    limiter.observe(httpx.Headers({"x-ratelimit-remaining-tokens": "junk"}))
    assert limiter.remaining_tokens is None

    async def go() -> list[float]:
        slept: list[float] = []

        async def sleep(s: float) -> None:
            slept.append(s)

        def handler(request: httpx.Request) -> httpx.Response:
            return groq_response(ok_json())

        lim = jc.RateLimiter(rpm=30, clock=lambda: now[0])
        client = jc.JudgeClient("k", limiter=lim, sleep=sleep,
                                transport=httpx.MockTransport(handler))  # fmt: skip
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        await client.complete(jc.judge_model_config(), msgs, 42, JudgeVerdict)
        await client.complete(jc.judge_model_config(), msgs, 42, JudgeVerdict)
        await client.aclose()
        return slept

    assert asyncio.run(go()) == [pytest.approx(2.0)]


def test_parse_ok_without_json_model() -> None:
    client = jc.JudgeClient("k")
    resp = httpx.Response(200, content=b"not json")
    with pytest.raises(LLMRequestError):
        client._parse_ok(resp)
    assert client.last_model is None


# --------------------------------------------------------------------------- report


T0 = datetime(2026, 10, 15, tzinfo=UTC)


def jr(cid: str, verdict: str, reason: str = "r") -> JudgeResult:
    return JudgeResult(case_id=cid, criterion="relevance", verdict=verdict,  # type: ignore[arg-type]
                       reason=reason, evidence="e", model="qwen/qwen3.8-27b",
                       prompt_sha256="p" * 64, rubric_hash="r" * 64, input_sha256="i" * 64,
                       thinking="t", timestamp=T0,
                       error="no JSON" if verdict == "judge_error" else None)  # fmt: skip


def lab(cid: str, verdict: str) -> Label:
    return Label.model_validate({
        "case_id": cid, "run_id": "r", "labeler": "h", "timestamp": T0, "rubric_hash": "a" * 64,
        "response_sha256": "b" * 64, "seconds_spent": 1.0, "notes": "a | note",
        "verdicts": {c.value: "pass" for c in Criterion} | {"relevance": verdict},
    })  # fmt: skip


def test_report() -> None:
    results = [jr("a", "pass"), jr("b", "fail"), jr("c", "pass", "seems fine"),
               jr("d", "judge_error")]  # fmt: skip
    labels = {"a": lab("a", "pass"), "b": lab("b", "fail"), "c": lab("c", "fail"),
              "d": lab("d", "pass")}  # fmt: skip
    text = report.build_report(Criterion.RELEVANCE, results, labels, labels_file="l.jsonl",
                               consistency=[jr("a", "fail", "flip"), jr("b", "fail")])  # fmt: skip
    assert "judge_error: **1**" in text and "| c | fail | a \\| note | pass | seems fine |" in text
    assert "## Judge errors" in text and "| d | no JSON |" in text
    assert "**1 verdict flip(s)** (50.0%)" in text and "| a | pass | fail | flip |" in text
    clean = report.build_report(Criterion.RELEVANCE, [jr("a", "pass")], {"a": lab("a", "pass")},
                                labels_file="l")  # fmt: skip
    assert "None." in clean and "Not run." in clean
    assert "| n |" in report.build_report(Criterion.RELEVANCE, [], {}, labels_file="l")


# --------------------------------------------------------------------------- CLI


def test_cli_prompt_dry_run_and_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = tmp_path / "prompts"
    res = CliRunner().invoke(cli.app, ["judge", "prompt", "--criterion", "relevance",
                                       "--prompt-dir", str(pdir)])  # fmt: skip
    assert res.exit_code == 0 and (pdir / "relevance_v1.md").exists()
    ids = DEV[:5]
    runs = tmp_path / "runs"
    write_jsonl(runs / "r1" / "results.jsonl", [result(cid) for cid in ids])
    ds = tmp_path / "d.jsonl"
    write_jsonl(ds, [case(cid) for cid in ids])
    common = ["judge", "run", "--criterion", "relevance", "--run", "r1", "--runs-dir", str(runs),
              "--dataset", str(ds), "--prompt-dir", str(pdir), "--ledger",
              str(tmp_path / "usage.jsonl"), "--cache-dir", str(tmp_path / "cache")]  # fmt: skip
    dry = CliRunner().invoke(cli.app, [*common, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    flat_dry = " ".join(dry.output.split())
    assert "5 to judge" in flat_dry and "5 API call(s)" in flat_dry and "no call" in flat_dry
    smoke = CliRunner().invoke(cli.app, [*common, "--dry-run", "--limit", "2"])
    assert "2 to judge" in " ".join(smoke.output.split())
    cons = CliRunner().invoke(cli.app, [*common, "--dry-run", "--consistency", "3"])
    assert "3 to judge" in cons.output
    test = CliRunner().invoke(cli.app, [*common, "--split", "test", "--dry-run"])
    assert test.exit_code == 2 and "locked" in test.output
    capped = CliRunner().invoke(cli.app, [*common, "--max-calls", "1"])
    assert capped.exit_code == 2 and "--max-calls is 1" in capped.output

    def fake_client(key: str, **kw: Any) -> FakeJudge:
        f = FakeJudge(lambda m: ok_json(evidence="30 days"))
        f.limiter = jc.RateLimiter()  # type: ignore[attr-defined]
        f.limiter.limits = {"x-ratelimit-limit-tokens": "6000"}  # type: ignore[attr-defined]

        async def aclose() -> None:
            return None

        f.aclose = aclose  # type: ignore[attr-defined]
        return f

    monkeypatch.setattr(jc, "JudgeClient", fake_client)
    done = CliRunner().invoke(cli.app, [*common, "--max-calls", "20"])
    assert done.exit_code == 0, done.output
    flat = " ".join(done.output.split())  # rich wraps long lines
    assert "5 results, 5 API call(s), 0 judge_error" in flat and "rate limits" in flat
    out = list(read_jsonl(runs / "r1" / "judges" / "relevance_v1.jsonl", JudgeResult))
    assert len(out) == 5
    stale = pdir / "relevance_v1.md"
    stale.write_text(stale.read_text(encoding="utf-8") + "edit", encoding="utf-8")
    res = CliRunner().invoke(cli.app, common)
    assert res.exit_code == 2 and "out of date" in res.output
    bad = CliRunner().invoke(cli.app, ["judge", "prompt", "--criterion", "nope"])
    assert bad.exit_code == 2 and "cannot load the rubric" in bad.output
    gone = CliRunner().invoke(cli.app, [*common[:6], "--runs-dir", str(tmp_path / "x"),
                                        "--prompt-dir", str(pdir)])  # fmt: skip
    assert gone.exit_code == 2


def test_cli_agree(tmp_path: Path) -> None:
    labels = ROOT / "datasets" / "labels_v1.2.jsonl"
    runs = tmp_path / "runs"
    dev_labels = {lab_.case_id: lab_ for lab_ in read_jsonl(labels, Label) if lab_.case_id in DEV}
    rows = [jr(cid, dev_labels[cid].verdicts[Criterion.RELEVANCE].value) for cid in DEV]
    write_jsonl(runs / "r1" / "judges" / "relevance_v1.jsonl", rows)
    args = ["judge", "agree", "--criterion", "relevance", "--run", "r1", "--runs-dir", str(runs),
            "--out-dir", str(tmp_path / "reports")]  # fmt: skip
    res = CliRunner().invoke(cli.app, args)
    assert res.exit_code == 0, res.output and "agreement 100.0%" in res.output
    assert (tmp_path / "reports" / "relevance_dev_v1.md").exists()
    assert CliRunner().invoke(cli.app, [*args, "--split", "test"]).exit_code == 2
    missing = CliRunner().invoke(cli.app, [*args[:6], "--runs-dir", str(tmp_path / "none")])
    assert missing.exit_code == 2 and "cannot build the report" in missing.output


def test_refused_request_is_logged_counted_and_stops_the_run(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(429, json={"error": {"message": "Rate limit reached: TPM"}},
                                  headers={"retry-after": "1"})  # fmt: skip
        return groq_response(ok_json(), **{"x-ratelimit-limit-tokens": "8000"})

    async def nosleep(s: float) -> None:
        return None

    ledger = jc.Ledger(tmp_path / "usage.jsonl")
    client = jc.JudgeClient("k", ledger=ledger, max_retries=0, sleep=nosleep,
                            transport=httpx.MockTransport(handler))  # fmt: skip
    ids = DEV[:3]
    results, cases = setup(ids)
    cache = runner.JudgeCache(tmp_path / "c")
    plan = runner.build_plan(cfg(), RUBRIC[Criterion.RELEVANCE], SPLIT, "dev", results, cases,
                             cache, only=ids)  # fmt: skip
    replies = {cid: r.response for cid, r in results.items() if r.response}
    run_it = runner.run_judge(client, cfg(), plan, replies, cache, max_calls=10, daily_cap=200,
                              ledger=ledger, stop_on_refusal=True)  # fmt: skip
    with pytest.raises(runner.CallRefused, match=r"1 request\(s\) refused"):
        asyncio.run(run_it)
    rows = [json.loads(x) for x in (tmp_path / "usage.jsonl").read_text("utf-8").splitlines()]
    assert [r["status"] for r in rows] == [200, 429] and rows[1]["ok"] is False
    assert "Rate limit reached" in rows[1]["error"] and rows[0]["error"] is None
    assert client.requests == 2 and client.refused == 1
    assert client.limiter.limit_tokens == 8000
    assert cache.get(plan.items[0].key) is not None  # the good result was kept


def test_made_counts_http_requests_including_retries(tmp_path: Path) -> None:
    seq = iter([httpx.Response(503, json={"error": {"message": "busy"}}),
                groq_response(ok_json()), groq_response(ok_json())])  # fmt: skip

    async def nosleep(s: float) -> None:
        return None

    client = jc.JudgeClient("k", sleep=nosleep,
                            transport=httpx.MockTransport(lambda r: next(seq)))  # fmt: skip
    ids = DEV[:2]
    results, cases = setup(ids)
    cache = runner.JudgeCache(tmp_path)
    plan = runner.build_plan(cfg(), RUBRIC[Criterion.RELEVANCE], SPLIT, "dev", results, cases,
                             cache, only=ids)  # fmt: skip
    replies = {cid: r.response for cid, r in results.items() if r.response}
    got, made = asyncio.run(runner.run_judge(client, cfg(), plan, replies, cache, max_calls=10,
                                             daily_cap=200, ledger=None))  # fmt: skip
    assert made == 3 and client.refused == 1 and all(r.verdict == "pass" for r in got)


def test_token_window_paces_to_the_per_minute_limit() -> None:
    now = [0.0]
    lim = jc.RateLimiter(rpm=1000, clock=lambda: now[0])
    lim.observe(httpx.Headers({"x-ratelimit-limit-tokens": "8000"}))
    for t in (0.0, 5.0, 10.0):
        now[0] = t
        lim.mark(2500)
    now[0] = 12.0
    assert lim.delay(2500) == pytest.approx(48.0)  # 7500 used: wait for the first to expire
    now[0] = 61.0
    assert lim.delay(2500) == pytest.approx(0.0, abs=0.1)
    assert jc.judge_model_config().max_tokens == 300


def test_relevance_v2_adds_answer_available() -> None:
    from evalkit.judges.inputs import answer_available

    fields = {"ok": True, "reply": REPLY, "route": "knowledge", "answer_type": "not_found",
              "escalated": True}  # fmt: skip
    resp = AgentResponse.model_validate(fields)
    gold = case("g", expected_route="database", gold_db_facts={"price": 1.0})
    ref = case("r", reference_answer="30 days")
    oos = case("o", category="out_of_scope", reference_answer="Outside what we cover.")
    unans = case("u", reference_answer="Not available.", tags=["unanswerable"])
    plain = case("p")
    assert [answer_available(c) for c in (gold, ref, oos, unans, plain)] == [
        True, True, False, False, False]  # fmt: skip
    v1 = judge_input(Criterion.RELEVANCE, ref, resp)
    v2 = judge_input(Criterion.RELEVANCE, ref, resp, "v2")
    assert "answer_available" not in v1 and v2["answer_available"] is True
    assert "reference_answer" not in v2  # the flag, never the gold text
    t2 = prompts.build_prompt(RUBRIC[Criterion.RELEVANCE], RHASH, SPLIT, "v2")
    assert "`answer_available`" in t2 and "relevance_v2" in t2
    assert "`answer_available`" not in prompts.build_prompt(RUBRIC[Criterion.RELEVANCE], RHASH,
                                                            SPLIT, "v1")  # fmt: skip
    with pytest.raises(ValueError, match="unknown prompt version"):
        prompts.build_prompt(RUBRIC[Criterion.RELEVANCE], RHASH, SPLIT, "v9")
    c2 = runner.JudgeConfig(Criterion.RELEVANCE, jc.judge_model_config(), t2, RHASH, "v2")
    assert runner.cache_key(c2, "h") != runner.cache_key(cfg(), "h")


def test_cli_prompt_version(tmp_path: Path) -> None:
    pdir = tmp_path / "p"
    args = ["judge", "prompt", "--criterion", "relevance", "--prompt-dir", str(pdir)]
    ok = CliRunner().invoke(cli.app, [*args, "--prompt-version", "v2"])
    assert ok.exit_code == 0 and (pdir / "relevance_v2.md").exists()
    bad = CliRunner().invoke(cli.app, [*args, "--prompt-version", "v9"])
    assert bad.exit_code == 2 and "unknown prompt version" in bad.output


def test_report_compares_two_prompt_versions(tmp_path: Path) -> None:
    labels = {"a": lab("a", "fail"), "b": lab("b", "pass"), "c": lab("c", "pass")}
    old = [jr("a", "pass"), jr("b", "pass"), jr("c", "pass")]  # misses a
    new = [jr("a", "fail"), jr("b", "pass"), jr("c", "fail", "too strict")]  # fixes a, breaks c
    text = report.build_report(Criterion.RELEVANCE, new, labels, labels_file="l",
                               compare=("v1", old, "v2"))  # fmt: skip
    assert "## v1 vs v2 (same labels, same dev cases)" in text
    assert "| % agreement | 66.7% | 66.7% |" in text
    assert "**Fixed in v2 (1):** a" in text and "**Still disagreeing (0):** none" in text
    assert "| c | pass | fail | too strict |" in text
    same = report.comparison_lines(Criterion.RELEVANCE, "v1", old, "v1b", old, labels)
    assert "none" in same[-1]
    runs = tmp_path / "runs"
    real = {x.case_id: x for x in read_jsonl(ROOT / "datasets" / "labels_v1.2.jsonl", Label)}
    rows = [jr(cid, real[cid].verdicts[Criterion.RELEVANCE].value) for cid in DEV]
    for name in ("v1", "v2"):
        write_jsonl(runs / "r1" / "judges" / f"relevance_{name}.jsonl", rows)
    args = ["judge", "agree", "--criterion", "relevance", "--run", "r1", "--runs-dir", str(runs),
            "--out-dir", str(tmp_path / "rep"), "--prompt-version", "v2",
            "--compare-with", "v1"]  # fmt: skip
    res = CliRunner().invoke(cli.app, args)
    assert res.exit_code == 0, res.output
    assert "## v1 vs v2" in (tmp_path / "rep" / "relevance_dev_v2.md").read_text("utf-8")


# --------------------------------------------------------------------------- OpenAI


def test_openai_judge_payload_cost_and_counting(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return groq_response(ok_json(), model="gpt-5-mini-2025-08-07",
                             **{"x-ratelimit-remaining-tokens": "199000"})  # fmt: skip

    ledger = jc.Ledger(tmp_path / "usage.jsonl")
    client = jc.client_for("openai", "k", ledger=ledger, transport=httpx.MockTransport(handler))
    assert isinstance(client, jc.OpenAIJudgeClient) and client.url == jc.OPENAI_URL
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    asyncio.run(client.complete(jc.openai_judge_model_config(), msgs, 42, JudgeVerdict))
    body = seen[0]
    assert body["model"] == "gpt-5-mini" and "temperature" not in body
    assert body["reasoning_effort"] == "minimal" and body["max_completion_tokens"] == 2000
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert client.thinking == jc.THINKING_OPENAI and client.last_model == "gpt-5-mini-2025-08-07"
    assert client.requests == 1 and ledger.calls_today() == 1
    assert isinstance(jc.client_for("groq", "k"), jc.JudgeClient)
    assert jc.model_config_for("openai").name == "gpt-5-mini"
    assert jc.model_config_for("groq").name == jc.JUDGE_MODEL
    assert jc.estimate_cost_usd("gpt-5-mini", 1_000_000, 1_000_000) == pytest.approx(1.125)
    assert jc.estimate_cost_usd("qwen/qwen3.8-27b", 1, 1) is None

    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "quota",
                                                   "code": "insufficient_quota"}},
                              headers={"retry-after-ms": "500"})  # fmt: skip

    quota = jc.client_for("openai", "k", transport=httpx.MockTransport(limited), max_retries=0)
    from evalkit.generation.llm import QuotaExhausted

    with pytest.raises(QuotaExhausted):
        asyncio.run(quota.complete(jc.openai_judge_model_config(), msgs, 42, JudgeVerdict))

    # a plain rate-limit 429 (no retry-after-ms, no quota code) used to crash with a TypeError
    def plain(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "Rate limit reached"}},
                              headers={"retry-after": "1"})  # fmt: skip

    paced = jc.client_for("openai", "k", transport=httpx.MockTransport(plain), max_retries=0)
    resp = httpx.Response(429, json={"error": {"message": "Rate limit reached"}})
    assert paced.rate_limit_wait(resp, "Rate limit reached") is None
    assert not paced.is_quota_error(resp, "Rate limit reached")
    with pytest.raises(QuotaExhausted, match="still rate limited"):
        asyncio.run(paced.complete(jc.openai_judge_model_config(), msgs, 42, JudgeVerdict))

    # OpenAI's "no credits" 429 has no insufficient_quota code: still a quota/billing stop
    def broke(request: httpx.Request) -> httpx.Response:
        text = "You have no credits remaining. Add credits to continue using the API."
        return httpx.Response(429, json={"error": {"message": text}})

    poor = jc.client_for("openai", "k", transport=httpx.MockTransport(broke), max_retries=0)
    with pytest.raises(QuotaExhausted, match="quota/billing exhausted"):
        asyncio.run(poor.complete(jc.openai_judge_model_config(), msgs, 42, JudgeVerdict))


def test_cached_results_and_judge_vs_judge(tmp_path: Path) -> None:
    ids = DEV[:3]
    results, cases = setup(ids)
    cache = runner.JudgeCache(tmp_path)
    plan = runner.build_plan(cfg(), RUBRIC[Criterion.RELEVANCE], SPLIT, "dev", results, cases,
                             cache, only=ids)  # fmt: skip
    run(FakeJudge([ok_json(), ok_json("fail"), "bad", "bad"]), plan, results, tmp_path)
    cached = runner.cached_results(plan, cache)
    assert [r.case_id for r in cached] == sorted(ids[:2])  # the judge_error isn't cached
    a = [jr("a", "pass"), jr("b", "fail"), jr("c", "pass"), jr("d", "judge_error")]
    b = [jr("a", "pass"), jr("b", "pass", "fine"), jr("c", "pass"), jr("e", "fail")]
    labels = {"b": lab("b", "fail")}
    lines = "\n".join(report.judge_vs_judge_lines("Qwen", a, "GPT", b, labels,
                                                  Criterion.RELEVANCE))  # fmt: skip
    assert "On the 3 cases both judged" in lines and "| b | fail | fail | pass | fine |" in lines
    same = report.judge_vs_judge_lines("Qwen", a[:1], "GPT", b[:1], labels, Criterion.RELEVANCE)
    assert same[-1] == "They agree on every case."


def test_cli_openai_dry_run_and_agree_vs_groq(tmp_path: Path) -> None:
    pdir = tmp_path / "prompts"
    for v in ("v2",):
        CliRunner().invoke(cli.app, ["judge", "prompt", "--criterion", "relevance",
                                     "--prompt-dir", str(pdir), "--prompt-version", v])  # fmt: skip
    ids = DEV[:4]
    runs = tmp_path / "runs"
    write_jsonl(runs / "r1" / "results.jsonl", [result(cid) for cid in ids])
    ds = tmp_path / "d.jsonl"
    write_jsonl(ds, [case(cid) for cid in ids])
    common = ["judge", "run", "--criterion", "relevance", "--run", "r1", "--runs-dir", str(runs),
              "--dataset", str(ds), "--prompt-dir", str(pdir), "--prompt-version", "v2",
              "--ledger", str(tmp_path / "u.jsonl"),
              "--cache-dir", str(tmp_path / "c")]  # fmt: skip
    dry = CliRunner().invoke(cli.app, [*common, "--provider", "openai", "--dry-run"])
    flat = " ".join(dry.output.split())
    assert dry.exit_code == 0, dry.output
    assert "with gpt-5-mini" in flat and "estimated cost: about $" in flat and "no call" in flat
    groq = " ".join(CliRunner().invoke(cli.app, [*common, "--dry-run"]).output.split())
    assert "Groq free tier" in groq
    bad = CliRunner().invoke(cli.app, [*common, "--provider", "anthropic", "--dry-run"])
    assert bad.exit_code == 2 and "unknown provider" in bad.output
    # OpenAI results on file, Qwen only in its cache: the report compares both
    real = {x.case_id: x for x in read_jsonl(ROOT / "datasets" / "labels_v1.2.jsonl", Label)}
    rows = [jr(cid, real[cid].verdicts[Criterion.RELEVANCE].value) for cid in DEV]
    write_jsonl(runs / "r1" / "judges" / "relevance_v2_openai.jsonl", rows)
    args = ["judge", "agree", "--criterion", "relevance", "--run", "r1", "--runs-dir", str(runs),
            "--out-dir", str(tmp_path / "rep"), "--prompt-version", "v2", "--provider", "openai",
            "--vs-provider", "groq", "--dataset", str(ds), "--prompt-dir", str(pdir),
            "--cache-dir", str(tmp_path / "c")]  # fmt: skip
    res = CliRunner().invoke(cli.app, args)
    assert res.exit_code == 0, res.output
    text = (tmp_path / "rep" / "relevance_dev_v2_openai.md").read_text("utf-8")
    assert "## Judge vs judge" in text and "On the 0 cases both judged" in text


# --------------------------------------------------------------------------- Day 17


def test_day17_inputs_per_criterion() -> None:
    c = case("c1", reference_answer="30 days", gold_db_facts={"price": 1.0})
    resp = AgentResponse.model_validate({"ok": True, "reply": REPLY, "route": "knowledge",
                                         "answer_type": "llm", "escalated": False})  # fmt: skip
    common = {"question", "history", "reply", "route", "answer_type", "escalated"}
    assert set(judge_input(Criterion.TONE, c, resp)) == common
    assert set(judge_input(Criterion.SAFETY, c, resp)) == common
    assert set(judge_input(Criterion.FAITHFULNESS, c, resp)) == common | {"retrieved_chunks",
                                                                          "db_facts"}  # fmt: skip
    cor = judge_input(Criterion.CORRECTNESS, c, resp)
    assert set(cor) == common | {"reference_answer", "gold_db_facts"}
    com = judge_input(Criterion.COMPLETENESS, c, resp)
    assert set(com) == common | {"answer_available"} and com["answer_available"] is True


def test_day17_prompts_are_current_and_explain_their_inputs() -> None:
    pdir = ROOT / "prompts" / "judges"
    notes = {Criterion.FAITHFULNESS: "`retrieved_chunks`", Criterion.CORRECTNESS:
             "`reference_answer`", Criterion.COMPLETENESS: "`answer_available`"}  # fmt: skip
    for crit in (Criterion.TONE, Criterion.COMPLETENESS, Criterion.CORRECTNESS,
                 Criterion.SAFETY, Criterion.FAITHFULNESS):  # fmt: skip
        fresh = prompts.build_prompt(RUBRIC[crit], RHASH, SPLIT, "v1")
        assert prompts.load_prompt(crit, pdir, "v1") == fresh, crit
        assert all(k in fresh for k in RUBRIC[crit].key_rules)
        if crit in notes:
            assert notes[crit] in fresh
        else:
            assert "The input also has" not in fresh


def test_answer_available_is_false_when_the_item_does_not_exist() -> None:
    from evalkit.judges.inputs import answer_available

    order = case("o", expected_route="database", gold_db_facts={"order_id": "X", "exists": False})
    product = case("p", expected_route="database",
                   gold_db_facts={"product_name": "Y", "product_exists": False})  # fmt: skip
    real = case("r", expected_route="database", gold_db_facts={"order_id": "Z", "status": "x"})
    assert [answer_available(c) for c in (order, product, real)] == [False, False, True]


def test_v2_prompts_inputs_and_judge_skips(tmp_path: Path) -> None:
    resp = AgentResponse.model_validate({"ok": True, "reply": REPLY, "route": "knowledge",
                                         "answer_type": "llm", "escalated": False})  # fmt: skip
    c = case("c1", reference_answer="30 days")
    assert "db_facts" in judge_input(Criterion.SAFETY, c, resp, "v2")
    assert "db_facts" not in judge_input(Criterion.SAFETY, c, resp, "v1")
    for crit, phrase in ((Criterion.TONE, "upset, worried or complaining"),
                         (Criterion.FAITHFULNESS, "An OFFER of escalation"),
                         (Criterion.SAFETY, "matches `db_facts` is real")):  # fmt: skip
        v2 = prompts.build_prompt(RUBRIC[crit], RHASH, SPLIT, "v2")
        assert "## Clarifications (judge prompt v2)" in v2 and phrase in v2
        v1 = prompts.build_prompt(RUBRIC[crit], RHASH, SPLIT, "v1")
        assert "## Clarifications" not in v1
    # correctness v2 never sends a case without a reference answer or gold facts
    ids = DEV[:2]
    results = {cid: result(cid) for cid in ids}
    cases = {ids[0]: case(ids[0], reference_answer="30 days"), ids[1]: case(ids[1])}
    t2 = prompts.build_prompt(RUBRIC[Criterion.CORRECTNESS], RHASH, SPLIT, "v2")
    c2 = runner.JudgeConfig(Criterion.CORRECTNESS, jc.judge_model_config(), t2, RHASH, "v2")
    plan = runner.build_plan(c2, RUBRIC[Criterion.CORRECTNESS], SPLIT, "dev", results, cases,
                             runner.JudgeCache(tmp_path), only=ids)  # fmt: skip
    assert [i.case_id for i in plan.items] == [ids[0]] and plan.skipped_na == [ids[1]]
    c1 = runner.JudgeConfig(Criterion.CORRECTNESS, jc.judge_model_config(),
                            prompts.build_prompt(RUBRIC[Criterion.CORRECTNESS], RHASH, SPLIT),
                            RHASH)  # fmt: skip
    plan1 = runner.build_plan(c1, RUBRIC[Criterion.CORRECTNESS], SPLIT, "dev", results, cases,
                              runner.JudgeCache(tmp_path), only=ids)  # fmt: skip
    assert len(plan1.items) == 2  # v1 sends both (kept as it was)


def test_every_committed_prompt_matches_a_fresh_build() -> None:
    pdir = ROOT / "prompts" / "judges"
    for crit in Criterion:
        for version in prompts.VERSIONS:
            path = prompts.prompt_path(crit, pdir, version)
            if not path.exists():
                continue
            text = prompts.load_prompt(crit, pdir, version)
            if crit is Criterion.RELEVANCE and version == "v1":
                assert prompts.prompt_sha256(text) == RELEVANCE_V1_SHA  # frozen
                continue
            assert text == prompts.build_prompt(RUBRIC[crit], RHASH, SPLIT, version), path


# --------------------------------------------------------------------------- v3 / frozen


def test_answer_status_is_three_valued_and_v3_inputs() -> None:
    from evalkit.judges.inputs import answer_status, input_fields

    gone = case("g", expected_route="database", gold_db_facts={"order_id": "X", "exists": False})
    known = case("k", reference_answer="30 days")
    oos = case("o", category="out_of_scope", reference_answer="We only cover GlowCare.")
    unknown = case("u")
    assert [answer_status(c) for c in (gone, known, oos, unknown)] == [False, True, None, None]
    fields = {"ok": True, "reply": REPLY, "route": "knowledge", "answer_type": "not_found",
              "escalated": True}  # fmt: skip
    resp = AgentResponse.model_validate(fields)
    assert judge_input(Criterion.RELEVANCE, unknown, resp, "v3")["answer_available"] is None
    assert judge_input(Criterion.RELEVANCE, unknown, resp, "v2")["answer_available"] is False
    assert judge_input(Criterion.COMPLETENESS, gone, resp, "v3")["answer_available"] is False
    s3 = judge_input(Criterion.SAFETY, known, resp, "v3")
    assert "db_facts" in s3 and "retrieved_chunks" in s3
    assert input_fields(Criterion.COMPLETENESS, "v3")[-1] == "answer_available (3-valued)"
    for crit in (Criterion.RELEVANCE, Criterion.COMPLETENESS):
        text = prompts.build_prompt(RUBRIC[crit], RHASH, SPLIT, "v3")
        assert "one of three values" in text and "null when it isn't known" in text
    safety3 = prompts.build_prompt(RUBRIC[Criterion.SAFETY], RHASH, SPLIT, "v3")
    assert "`retrieved_chunks`" in safety3 and "matches `db_facts` or `retrieved_chunks`" in safety3


def test_frozen_prompts_match_frozen_json() -> None:
    """Fails if a frozen prompt file (or its inputs) changes without updating FROZEN.json:
    rerun `evalkit judge freeze` only for a deliberate, documented change."""
    from evalkit.judges import frozen

    pdir = ROOT / "prompts" / "judges"
    recorded = frozen.read_frozen(pdir)
    assert recorded is not None, "run `evalkit judge freeze`"
    assert set(recorded["criteria"]) == {c.value for c in frozen.FINAL_VERSIONS}
    for name, item in recorded["criteria"].items():
        crit = Criterion(name)
        assert item["version"] == frozen.FINAL_VERSIONS[crit]
        assert frozen.entry(crit, item["version"], pdir) == item, f"{name}: FROZEN.json is stale"
        assert item["rubric_hash"] == RHASH
    assert recorded["primary_judge"] == jc.OPENAI_JUDGE_MODEL
    assert recorded["cross_family_check"]["model"] == jc.JUDGE_MODEL
    assert frozen.frozen_version(Criterion.RELEVANCE, pdir) == "v3"
    assert frozen.frozen_version(Criterion.ESCALATION_APPROPRIATE, pdir) is None


def test_cli_freeze_guard_and_only(tmp_path: Path) -> None:
    from evalkit.judges import frozen

    pdir = tmp_path / "p"
    build = ["judge", "prompt", "--prompt-dir", str(pdir)]
    for crit, version in frozen.FINAL_VERSIONS.items():
        args = [*build, "--criterion", crit.value, "--prompt-version", version]
        CliRunner().invoke(cli.app, args)
    CliRunner().invoke(cli.app, [*build, "--criterion", "relevance", "--prompt-version", "v2"])
    res = CliRunner().invoke(cli.app, ["judge", "freeze", "--prompt-dir", str(pdir)])
    assert res.exit_code == 0 and (pdir / "FROZEN.json").exists() and "relevance: v3" in res.output
    assert frozen.read_frozen(tmp_path) is None and frozen.frozen_version(
        Criterion.TONE, tmp_path) is None  # fmt: skip
    ids = DEV[:3]
    runs = tmp_path / "runs"
    write_jsonl(runs / "r1" / "results.jsonl", [result(cid) for cid in ids])
    ds = tmp_path / "d.jsonl"
    write_jsonl(ds, [case(cid) for cid in ids])
    common = ["judge", "run", "--criterion", "relevance", "--run", "r1", "--runs-dir", str(runs),
              "--dataset", str(ds), "--prompt-dir", str(pdir), "--dry-run",
              "--cache-dir", str(tmp_path / "c")]  # fmt: skip
    old = CliRunner().invoke(cli.app, [*common, "--prompt-version", "v2"])
    assert old.exit_code == 2 and "frozen at v3" in old.output
    ok = CliRunner().invoke(cli.app, [*common, "--prompt-version", "v2", "--allow-unfrozen"])
    assert ok.exit_code == 0, ok.output
    one = CliRunner().invoke(cli.app, [*common, "--prompt-version", "v3", "--only", ids[1]])
    assert "1 to judge" in " ".join(one.output.split())
    listing = tmp_path / "ids.txt"
    listing.write_text("\n".join(ids[:2]), encoding="utf-8")
    two = CliRunner().invoke(cli.app, [*common, "--prompt-version", "v3", "--only", f"@{listing}"])
    assert "2 to judge" in " ".join(two.output.split())


def test_report_splits_agreement_by_case_source() -> None:
    results = [jr("a", "pass"), jr("b", "fail"), jr("c", "pass"), jr("d", "fail")]
    labels = {"a": lab("a", "pass"), "b": lab("b", "pass"), "c": lab("c", "fail"),
              "d": lab("d", "fail")}  # fmt: skip
    sources = {"a": "legacy", "b": "known_bug", "c": "synthetic", "d": "synthetic"}
    text = report.build_report(Criterion.RELEVANCE, results, labels, labels_file="l",
                               sources=sources)  # fmt: skip
    assert "## By case source" in text
    assert "| non-OpenAI (legacy + known_bugs) | 2 | 50.0% |" in text
    assert "| OpenAI-generated (synthetic) | 2 | 50.0% |" in text


def test_bias_lines_flag_large_gaps_and_human_fails() -> None:
    results = [jr("a", "pass"), jr("b", "pass"), jr("c", "pass"), jr("d", "pass")]
    labels = {"a": lab("a", "pass"), "b": lab("b", "pass"), "c": lab("c", "fail"),
              "d": lab("d", "pass")}  # fmt: skip
    sources = {"a": "legacy", "b": "known_bug", "c": "synthetic", "d": "synthetic"}
    lines = report.bias_lines([(Criterion.RELEVANCE, "v3", results, "subset")], labels, sources)
    row = lines[-1]
    assert row.startswith("| relevance (subset) | v3 | 2 | 100.0% |") and "**large gap**" in row
    small = report.bias_lines([(Criterion.RELEVANCE, "v3", results[:2], "")], labels, sources)
    assert "too few cases" in small[-1]
    even = report.bias_lines([(Criterion.RELEVANCE, "v3", [jr("a", "pass"), jr("d", "pass")], "")],
                             labels, sources)  # fmt: skip
    assert even[-1].endswith("| +0.00 / +0.0 | – |")
    fails = report.human_fail_lines([Criterion.RELEVANCE], labels, sources)
    assert fails == ["| relevance | 0 / 2 | 1 / 2 |"]


def test_cli_bias(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    real = {x.case_id: x for x in read_jsonl(ROOT / "datasets" / "labels_v1.3.jsonl", Label)}
    for crit, name in ((Criterion.TONE, "tone_v2_openai"), (Criterion.RELEVANCE,
                       "relevance_v3_openai_subset")):  # fmt: skip
        rows = [jr(cid, real[cid].verdicts[crit].value) for cid in DEV[:20]]
        write_jsonl(runs / "r1" / "judges" / f"{name}.jsonl",
                    [r.model_copy(update={"criterion": crit.value}) for r in rows])  # fmt: skip
    out = tmp_path / "bias.md"
    res = CliRunner().invoke(cli.app, ["judge", "bias", "--run", "r1", "--runs-dir", str(runs),
                                       "--out", str(out)])  # fmt: skip
    assert res.exit_code == 0, res.output
    text = out.read_text(encoding="utf-8")
    assert "| tone | v2 |" in text and "relevance (subset, 20 of 96)" in text
    assert "## Human fails by source" in text and "no saved results; skipped" in res.output
    bad = CliRunner().invoke(cli.app, ["judge", "bias", "--labels", str(tmp_path / "x.jsonl")])
    assert bad.exit_code == 2
