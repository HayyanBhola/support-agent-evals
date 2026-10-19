"""Synthetic negatives (Day 18): template edits, dev only, controls, and their report."""

import asyncio
import json
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalkit import cli
from evalkit.calibration.loader import load_labels
from evalkit.calibration.split import read_split
from evalkit.generation.llm import Completion, ModelConfig
from evalkit.io import read_jsonl
from evalkit.judges import client as jc
from evalkit.judges import prompts, runner
from evalkit.judges.frozen import FINAL_VERSIONS
from evalkit.judges.schema import JudgeResult
from evalkit.manifest import read_manifest
from evalkit.rubrics import load_rubrics, rubric_hash
from evalkit.schemas import Criterion, LabelSample, LabelValue
from evalkit.synthetic import evaluate, negatives

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "datasets"
SPLIT = read_split(DATASETS / "splits" / "split_v1.json")
SAMPLES = {s.case_id: s for s in read_jsonl(DATASETS / "label_sample_v1.jsonl", LabelSample)}
LABELS = load_labels("dev", split_path=DATASETS / "splits" / "split_v1.json",
                     labels_path=DATASETS / "labels_v1.3.jsonl")  # fmt: skip
RUBRICS = {r.criterion: r for r in load_rubrics(ROOT / "rubrics", None)}


def build(seed: int = 42) -> list[negatives.SyntheticRecord]:
    return negatives.make_negatives(SAMPLES, LABELS, SPLIT.ids("dev"), RUBRICS, FINAL_VERSIONS,
                                    seed=seed)  # fmt: skip


def test_negatives_are_dev_only_paired_and_reproducible() -> None:
    recs = build()
    assert recs == build() and recs != build(seed=7)
    test_ids = set(SPLIT.ids("test"))
    assert not {r.source_case_id for r in recs} & test_ids  # never a test case
    assert {r.source_case_id for r in recs} <= set(SPLIT.ids("dev"))
    negs = [r for r in recs if r.kind == "negative"]
    ctls = {r.pair: r for r in recs if r.kind == "control"}
    assert len(negs) == len(ctls) == 63 and all(r.synthetic for r in recs)
    for n in negs:
        c = ctls[n.pair]
        assert n.expected_verdict is LabelValue.FAIL and c.expected_verdict is LabelValue.PASS
        assert c.reply == n.original_reply == SAMPLES[n.source_case_id].response.reply
        assert n.reply != n.original_reply and n.source_case_id == c.source_case_id
    for crit in Criterion:
        sources = [r.source_case_id for r in negs if r.criterion is crit]
        assert len(sources) == len(set(sources))  # distinct within a criterion


def test_committed_file_matches_a_fresh_build() -> None:
    on_disk = list(read_jsonl(DATASETS / negatives.DATASET_FILE, negatives.SyntheticRecord))
    assert on_disk == build()
    entry = read_manifest(DATASETS / "MANIFEST.json").datasets[negatives.DATASET_FILE]
    assert entry.kind == "synthetic_negatives" and entry.metadata["synthetic"] is True
    assert entry.metadata["negatives"] == 63


def sample(reply: str, question: str = "q", **case: Any) -> LabelSample:
    base = SAMPLES["legacy-db-price-hydra"]
    return base.model_copy(update={
        "response": base.response.model_copy(update={"reply": reply, "escalated": False}),
        "case": base.case.model_copy(update={"question": question, **case}),
    })  # fmt: skip


def test_edit_templates() -> None:
    rng = random.Random(1)
    two = sample("First part. Second part.", "How and when?")
    assert negatives.edit_drop_part(two, rng) == (
        "First part.",
        "dropped the last part: Second part.",
    )
    assert negatives.edit_drop_part(sample("Only one part.", "How and when?"), rng) is None
    assert negatives.edit_wrong_price(sample("It costs $24.99."), rng)[0] == "It costs $12.50."
    assert negatives.edit_wrong_price(sample("No price."), rng) is None
    assert negatives.edit_safe_for_you(sample("Ok.", "I'm allergic to nuts, can I use it?"), rng)
    assert negatives.edit_safe_for_you(sample("Ok.", "What does it cost?"), rng) is None
    handoff = negatives.edit_claimed_handoff(sample("Here you go."), rng)
    assert handoff is not None and negatives.CLAIMED_HANDOFF in handoff[0]
    escalated = sample("x").model_copy(update={"response": sample("x").response.model_copy(
        update={"escalated": True})})  # fmt: skip
    assert negatives.edit_claimed_handoff(escalated, rng) is None
    claim = negatives.edit_fake_claim(sample("A. B."), rng)
    assert claim is not None and claim[0].startswith("A. ") and claim[0].endswith(" B.")
    every = " ".join(k for _, k in negatives.FAKE_CLAIMS)
    assert negatives.edit_fake_claim(sample(every), rng) is None  # all claims "supported"
    for fn, text in ((negatives.edit_rude, negatives.RUDE), (negatives.edit_dismissive,
                     negatives.DISMISSIVE), (negatives.edit_role_play, negatives.ROLE_PLAY),
                     (negatives.edit_medical_claim, negatives.MEDICAL_CLAIM)):  # fmt: skip
        got = fn(sample("Reply."), rng)
        assert got is not None and text in got[0]
    gold = sample("It has Water and Glycerin.", gold_db_facts={"ingredients": ["Glycerin"]})
    assert negatives.edit_gold_ingredient(gold, rng)[0] == "It has Water and Retinol."
    assert negatives.edit_gold_ingredient(sample("x", gold_db_facts={}), rng) is None
    no_match = sample("Nothing here.", gold_db_facts={"ingredients": ["Glycerin"]})
    assert negatives.edit_gold_ingredient(no_match, rng) is None
    ref = sample("Returns within 30 days.", reference_answer="You have 30 days.")
    assert negatives.edit_reference_number(ref, rng)[0] == "Returns within 60 days."
    big = sample("Wait 120 hours.", reference_answer="120 hours")
    assert negatives.edit_reference_number(big, rng)[0] == "Wait 60 hours."
    assert negatives.edit_reference_number(sample("x", reference_answer=None), rng) is None
    assert negatives.edit_reference_number(sample("7", reference_answer="8"), rng) is None
    priced = sample("$5.00", gold_db_facts={"price": 5.0})
    assert negatives.edit_gold_price(priced, rng) is not None
    assert negatives.edit_gold_price(sample("$5.00", gold_db_facts={}), rng) is None


def test_sources_come_only_from_the_dev_ids_given() -> None:
    spec = negatives.EditSpec(Criterion.TONE, "rude", 3, negatives.edit_rude)
    few = SPLIT.ids("dev")[:5]
    recs = negatives.make_negatives(SAMPLES, LABELS, few, RUBRICS, FINAL_VERSIONS, specs=[spec])
    assert recs and {r.source_case_id for r in recs} <= set(few)
    assert negatives.make_negatives(SAMPLES, LABELS, [], RUBRICS, FINAL_VERSIONS,
                                    specs=[spec]) == []  # fmt: skip


def test_wilson_and_rate() -> None:
    assert evaluate.wilson(0, 0) is None
    lo, hi = evaluate.wilson(10, 10) or (0, 0)
    assert hi == pytest.approx(1.0) and lo == pytest.approx(0.722, abs=0.001)
    lo, hi = evaluate.wilson(5, 10) or (0, 0)
    assert lo == pytest.approx(0.237, abs=0.001) and hi == pytest.approx(0.763, abs=0.001)
    assert evaluate.Rate(0, 0).cell() == "–" and evaluate.Rate(0, 0).value is None
    assert evaluate.Rate(3, 4).cell().startswith("3/4 (75%)")


T0 = datetime(2026, 10, 15, tzinfo=UTC)


def result(rid: str, verdict: str) -> JudgeResult:
    return JudgeResult(case_id=rid, criterion="tone", verdict=verdict, reason="r | x",  # type: ignore[arg-type]
                       evidence="e", model="gpt-5-mini", prompt_sha256="p" * 64,
                       rubric_hash="r" * 64, input_sha256="i" * 64, thinking="t", timestamp=T0,
                       error="bad" if verdict == "judge_error" else None)  # fmt: skip


def test_report_counts_catches_misses_and_false_alarms() -> None:
    tone = {r.id: r for r in build() if r.criterion is Criterion.TONE}
    pairs = ["tone-rude-01", "tone-rude-02", "tone-rude-03"]
    recs = [tone[f"{k}-{p}"] for p in pairs for k in ("neg", "ctl")]
    results = {
        "neg-tone-rude-01": result("neg-tone-rude-01", "pass"),  # missed
        "neg-tone-rude-02": result("neg-tone-rude-02", "fail"),
        "neg-tone-rude-03": result("neg-tone-rude-03", "judge_error"),
        "ctl-tone-rude-01": result("ctl-tone-rude-01", "fail"),  # false alarm
        "ctl-tone-rude-02": result("ctl-tone-rude-02", "pass"),
        "ctl-tone-rude-03": result("ctl-tone-rude-03", "pass"),
    }
    text = evaluate.report(recs, results, {Criterion.TONE: "v2"}, model="gpt-5-mini")
    assert "## Missed negatives (1)" in text and "r \\| x" in text
    assert "| tone | 1/2 (50%)" in text and "## False alarms on controls (1)" in text
    assert "## Judge errors" in text and "the **primary judge**" in text
    empty = evaluate.report([], {}, {Criterion.TONE: "v2"}, model="m")
    assert "None." in empty


class Fake:
    def __init__(self) -> None:
        self.thinking = "fake"
        self.last_model = "gpt-5-mini"

    async def complete(
        self, model: ModelConfig, messages: Any, seed: int, schema: Any
    ) -> Completion:
        reply = json.loads(messages[1]["content"])["reply"]
        word = reply.split()[0]
        return Completion(content=json.dumps({"verdict": "fail", "reason": "Edited.",
                                              "evidence": word}), prompt_tokens=10)  # fmt: skip


def test_build_items_and_cache_hits_take_the_item_id(tmp_path: Path) -> None:
    recs = [r for r in build() if r.criterion is Criterion.TONE][:2]  # a control and a negative
    text = prompts.build_prompt(RUBRICS[Criterion.TONE], rubric_hash(ROOT / "rubrics"), SPLIT,
                                "v2")  # fmt: skip
    cfg = runner.JudgeConfig(Criterion.TONE, jc.openai_judge_model_config(), text,
                             rubric_hash(ROOT / "rubrics"), "v2")  # fmt: skip
    cache = runner.JudgeCache(tmp_path)
    plan = evaluate.build_items(cfg, recs, SAMPLES, cache)
    assert sorted(i.case_id for i in plan.items) == sorted(r.id for r in recs)
    assert evaluate.build_items(cfg, recs, SAMPLES, cache).calls == 2
    other = runner.JudgeConfig(Criterion.SAFETY, cfg.model, text, cfg.rubric_hash, "v2")
    assert evaluate.build_items(other, recs, SAMPLES, cache).items == []
    replies = {r.id: evaluate.edited_response(r, SAMPLES[r.source_case_id]) for r in recs}
    got, made = asyncio.run(runner.run_judge(Fake(), cfg, plan, replies, cache, max_calls=10,
                                             daily_cap=200, ledger=None))  # fmt: skip
    assert made == 2 and {r.case_id for r in got} == {r.id for r in recs}
    # the same input under another id: the cached verdict is reported under the new id
    twin = [r.model_copy(update={"id": f"twin-{r.id}"}) for r in recs]
    again = evaluate.build_items(cfg, twin, SAMPLES, cache)
    assert again.cached == 2
    got2, made2 = asyncio.run(runner.run_judge(Fake(), cfg, again, replies, cache, max_calls=10,
                                               daily_cap=200, ledger=None))  # fmt: skip
    assert made2 == 0 and {r.case_id for r in got2} == {r.id for r in twin}


def test_cli_negatives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "negs.jsonl"
    for name in ("label_sample_v1.jsonl", "labels_v1.3.jsonl"):
        (tmp_path / name).write_bytes((DATASETS / name).read_bytes())
    res = CliRunner().invoke(cli.app, ["negatives", "make", "--out", str(out), "--sample",
                                       str(tmp_path / "label_sample_v1.jsonl"), "--labels",
                                       str(tmp_path / "labels_v1.3.jsonl")])  # fmt: skip
    assert res.exit_code == 0, res.output
    assert "63 negatives + 63 controls" in res.output
    assert read_manifest(tmp_path / "MANIFEST.json").datasets["negs.jsonl"].kind == (
        "synthetic_negatives"
    )
    runs = tmp_path / "runs"
    (runs / "r1").mkdir(parents=True)
    (runs / "r1" / "results.jsonl").write_bytes(
        (ROOT / "datasets" / "day4_baseline.jsonl").read_bytes()[:0]
    )  # empty results file
    common = ["negatives", "judge", "--provider", "openai", "--data", str(out), "--run", "r1",
              "--runs-dir", str(runs), "--cache-dir", str(tmp_path / "c"),
              "--ledger", str(tmp_path / "u.jsonl")]  # fmt: skip
    dry = CliRunner().invoke(cli.app, [*common, "--dry-run"])
    flat = " ".join(dry.output.split())
    assert dry.exit_code == 0, dry.output
    assert "total: 126 API call(s) with gpt-5-mini" in flat and "no call" in flat
    capped = CliRunner().invoke(cli.app, [*common, "--max-calls", "5"])
    assert capped.exit_code == 2 and "--max-calls is 5" in capped.output

    def fake_client(provider: str, key: str, **kw: Any) -> Fake:
        f = Fake()

        async def aclose() -> None:
            return None

        f.aclose = aclose  # type: ignore[attr-defined]
        return f

    monkeypatch.setattr(jc, "client_for", fake_client)
    done = CliRunner().invoke(cli.app, [*common, "--max-calls", "200", "--daily-cap", "500"])
    assert done.exit_code == 0, done.output
    rep = tmp_path / "rep.md"
    res = CliRunner().invoke(cli.app, ["negatives", "report", "--provider", "openai", "--data",
                                       str(out), "--run", "r1", "--runs-dir", str(runs),
                                       "--out", str(rep)])  # fmt: skip
    assert res.exit_code == 0, res.output
    text = rep.read_text(encoding="utf-8")
    assert "| faithfulness | 20/20 (100%)" in text  # the fake fails everything
    assert "False alarms on controls (63)" in text
    bad = CliRunner().invoke(cli.app, ["negatives", "judge", "--provider", "openai", "--data",
                                       str(tmp_path / "missing.jsonl")])  # fmt: skip
    assert bad.exit_code == 2
    missing = ["negatives", "report", "--provider", "openai", "--data", str(out),
               "--runs-dir", str(tmp_path / "x")]  # fmt: skip
    assert CliRunner().invoke(cli.app, missing).exit_code == 2
