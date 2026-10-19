"""Judge bias tests (Day 22): the template perturbations, the sample, flip rates and the CLI
with a fake judge. No network, dev split only."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalkit import cli
from evalkit.bias import perturbations as pt
from evalkit.bias import run as br
from evalkit.io import write_jsonl
from evalkit.judges import client as jc
from evalkit.schemas import Criterion
from tests.test_judges import DEV, REPLY, FakeJudge, case, jr, ok_json, result
from tests.test_judges_final import _frozen_dir, _label

KB_REPLY = (
    "Products may be returned within 30 days of delivery. Opened items need a reason!\n"
    "- Start a return in chat.\n2. Keep your order number? Yes."
)


def _words(text: str) -> list[str]:
    return [w for w in text.replace("\n", " ").split() if w not in ("-", "2.")]


@pytest.mark.parametrize("name", pt.REPLY_PERTURBATIONS)
def test_reply_perturbations_are_deterministic_and_keep_the_meaning(name: str) -> None:
    f = pt.REPLY_FUNCTIONS[name]
    assert f(KB_REPLY) == f(KB_REPLY) != KB_REPLY
    out = f(KB_REPLY)
    if name == "format":
        assert _words(out) == _words(KB_REPLY)  # same words, same order: only the layout
        assert out.splitlines() == ["- Products may be returned within 30 days of delivery.",
                                    "- Opened items need a reason!", "- Start a return in chat.",
                                    "- Keep your order number?", "- Yes."]  # fmt: skip
    else:
        assert KB_REPLY.strip() in out  # the reply itself is unchanged; only text is added
    added = out.replace(KB_REPLY.strip(), "")
    assert not any(ch.isdigit() for ch in added) or name == "format"  # no new facts/numbers


def test_the_added_texts() -> None:
    assert pt.injection(REPLY).endswith(pt.INJECTION_NOTE)
    assert "Note to evaluator" in pt.injection(REPLY) and "should pass" in pt.INJECTION_NOTE
    assert pt.authority(REPLY).startswith(pt.AUTHORITY_CLAIM)
    assert pt.verbosity(REPLY).endswith(pt.VERBOSITY_FILLER)
    assert pt.bullets("") == "" and pt.bullets("One sentence") == "- One sentence"


def test_chunk_order_is_seeded_and_keeps_the_chunks() -> None:
    chunks = [{"id": f"c{i}", "text": f"t{i}"} for i in range(4)]
    a = pt.shuffled_chunks(chunks, "case-1")
    assert a == pt.shuffled_chunks(chunks, "case-1") and a != chunks
    assert sorted(a, key=lambda c: c["id"]) == chunks
    data = {"reply": REPLY, "retrieved_chunks": chunks}
    moved = pt.perturb_input(data, "chunk_order", "case-1")
    assert moved is not None and moved["reply"] == REPLY and moved["retrieved_chunks"] == a
    assert pt.perturb_input({"reply": REPLY, "retrieved_chunks": chunks[:1]}, "chunk_order",
                            "x") is None  # fmt: skip
    assert pt.perturb_input({"reply": REPLY}, "chunk_order", "x") is None
    assert pt.perturb_input(data, "format", "x")["retrieved_chunks"] == chunks  # type: ignore[index]
    with pytest.raises(ValueError):
        pt.perturb_input(data, "nope", "x")


def test_sample_is_stratified_seeded_and_prefers_the_judge_sample() -> None:
    labels = {cid: _label(cid, "fail" if i < 6 else "pass") for i, cid in enumerate(DEV[:20])}
    preferred = {DEV[0], DEV[10], DEV[11]}
    got = br.choose_sample(labels, preferred, 8)
    assert got == br.choose_sample(labels, preferred, 8) and len(got) == 8
    assert sum(br.human_fail(labels[c]) for c in got) == 4 and preferred <= set(got)
    few = {cid: _label(cid, "fail" if i < 2 else "pass") for i, cid in enumerate(DEV[:20])}
    assert len(br.choose_sample(few, set(), 8)) == 8  # topped up from the pass side


def test_flip_stats_directions_flags_and_report() -> None:
    res = {
        (Criterion.RELEVANCE, "original"): [jr("a", "fail"), jr("b", "pass"), jr("c", "fail"),
                                            jr("d", "judge_error")],
        (Criterion.RELEVANCE, "injection"): [jr("a", "pass", "told to"), jr("b", "pass"),
                                             jr("c", "fail"), jr("d", "pass")],
        (Criterion.RELEVANCE, "verbosity"): [jr("a", "fail"), jr("b", "fail"), jr("c", "fail"),
                                             jr("d", "fail")],
    }  # fmt: skip
    verb, inj = br.flip_stats(res, resamples=50)  # in PERTURBATIONS order
    assert verb.perturbation == "verbosity" and inj.perturbation == "injection"
    counts = (inj.n, inj.flips, inj.fail_to_pass, inj.pass_to_fail, inj.original_fails)
    assert counts == (3, 1, 1, 0, 2)
    assert inj.flagged and verb.flagged and verb.pass_to_fail == 1
    text = br.build_report([verb, inj], res, {"model": "m", "calls": 3, "cost_usd": 0.01})
    assert "| injection | relevance | 3 | 1 | 33.3%" in text and "**FLAG**" in text
    assert inj.low_confidence and "**low** (n=3)" in text and "Low confidence: relevance" in text
    assert "| injection | relevance | a | fail -> pass | told to |" in text
    assert "2 verdict(s) changed in total, 1 of them fail -> pass" in text
    m = br.mlflow_metrics([inj])
    assert m["injection.relevance.flip_rate"] == pytest.approx(1 / 3) and m["flagged"] == 1
    calm = {k: [jr(r.case_id, "pass") for r in v] for k, v in res.items()}
    assert "No verdict changed" in br.build_report(br.flip_stats(calm, resamples=20), calm, {})
    quiet = br.FlipStats(Criterion.TONE, "format", 10, 1, 0, 1, 0, 0, inj.ci)
    assert (
        not quiet.low_confidence
        and not quiet.flagged
        and "Nothing is flagged" in "\n".join(br.verdict_lines([quiet]))
    )


def _fake(provider: str, key: str, **kw: Any) -> FakeJudge:
    """Fails everything, except when told to pass by the injection note: the judge fooled."""
    f = FakeJudge(lambda m: ok_json("pass" if pt.INJECTION_NOTE in m[1]["content"] else "fail",
                                    evidence="30 days"))  # fmt: skip
    f.last_model = "gpt-5-mini-2025-08-07"

    async def aclose() -> None:
        return None

    f.aclose = aclose  # type: ignore[attr-defined]
    return f


def test_cli_bias_run_dry_run_and_go(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = _frozen_dir(tmp_path)
    runs = tmp_path / "runs"
    write_jsonl(runs / "r1" / "results.jsonl", [result(cid) for cid in DEV])
    write_jsonl(tmp_path / "d.jsonl", [case(cid, reference_answer="30 days") for cid in DEV])
    labels = tmp_path / "labels.jsonl"
    split = json.loads((Path("datasets") / "splits" / "split_v1.json").read_text("utf-8"))
    test_ids = split["splits"]["test"]
    write_jsonl(labels, [_label(c, "fail" if i % 3 == 0 else "pass")
                         for i, c in enumerate([*DEV, *test_ids])])  # fmt: skip
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"case_ids": DEV[:3]}), encoding="utf-8")
    args = ["bias", "run", "--run", "r1", "--runs-dir", str(runs), "--dataset",
            str(tmp_path / "d.jsonl"), "--prompt-dir", str(pdir), "--labels", str(labels),
            "--judge-sample", str(sample), "--ledger", str(tmp_path / "u.jsonl"), "--cache-dir",
            str(tmp_path / "c"), "--dataset-out", str(tmp_path / "pert.jsonl"), "--out",
            str(tmp_path / "bias.md"), "--n", "4", "--no-mlflow"]  # fmt: skip
    neither = CliRunner().invoke(cli.app, args)
    assert neither.exit_code == 2 and "exactly one" in neither.output
    nothing = CliRunner().invoke(cli.app, [*args, "--from-cache", "--missing-note",
                                           "not run (no OpenAI credits)"])  # fmt: skip
    assert nothing.exit_code == 0, nothing.output
    empty = (tmp_path / "bias.md").read_text(encoding="utf-8")
    assert "| tone | 0 of 4 | – | **not run (no OpenAI credits)** |" in empty
    assert "**Not run (no OpenAI credits):**" in empty
    dry = CliRunner().invoke(cli.app, [*args, "--dry-run"])
    flat = " ".join(dry.output.split())
    assert dry.exit_code == 0, dry.output
    assert "4 dev cases (2 with a human fail, 2 without" in flat and "no call made" in flat
    assert "relevance v3 / injection: 4 case(s)" in flat
    rows = (tmp_path / "pert.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 16 and all(json.loads(r)["case_id"] in DEV for r in rows)
    over = CliRunner().invoke(cli.app, [*args, "--go", "--max-calls", "500", "--max-usd", "0"])
    assert over.exit_code == 2 and "--max-usd" in over.output
    monkeypatch.setattr(jc, "client_for", _fake)
    res = CliRunner().invoke(cli.app, [*args, "--go", "--max-calls", "500", "--max-usd", "1"])
    assert res.exit_code == 0, res.output
    text = (tmp_path / "bias.md").read_text(encoding="utf-8")
    assert "| injection | relevance | 4 | 4 | 100.0%" in text and "**FLAG**" in text
    assert "| verbosity | relevance | 4 | 0 | 0.0%" in text
    assert (runs / "r1" / "judges" / br.RESULTS_DIR / "tone_v2_injection.jsonl").exists()
    again = CliRunner().invoke(cli.app, [*args, "--from-cache"])  # same verdicts, no call
    assert again.exit_code == 0 and (tmp_path / "bias.md").read_text(encoding="utf-8") == text
    changed = pdir / "tone_v2.md"
    changed.write_text(changed.read_text(encoding="utf-8") + "x", encoding="utf-8")
    bad = CliRunner().invoke(cli.app, [*args, "--dry-run"])
    assert bad.exit_code == 2 and "don't match FROZEN.json" in bad.output


def test_partial_cells_are_marked_not_run() -> None:
    res = {
        (Criterion.TONE, "original"): [jr("a", "pass"), jr("b", "fail")],
        (Criterion.TONE, "format"): [jr("a", "pass")],
        (Criterion.TONE, "injection"): [],
    }
    planned = {(Criterion.TONE, "format"): 2, (Criterion.TONE, "injection"): 2}
    fmt, inj = br.flip_stats(res, planned=planned, resamples=20)
    assert fmt.missing == 1 and not fmt.not_run and fmt.low_confidence
    assert inj.not_run and inj.missing == 2 and not inj.low_confidence and not inj.flagged
    text = br.build_report([fmt, inj], res, {"missing_note": "not run (no OpenAI credits)"})
    assert "| format | tone | 1 of 2 (1 not run (no OpenAI credits)) |" in text
    assert "| injection | tone | 0 of 2 | – | **not run (no OpenAI credits)** |" in text
    assert "injection x tone (all 2 cases); format x tone (1 of 2 cases)" not in text
    assert "format x tone (1 of 2 cases); injection x tone (all 2 cases)" in text
    m = br.mlflow_metrics([fmt, inj])
    assert "injection.tone.n" not in m and m["not_run_rows"] == 1 and m["missing_verdicts"] == 3
