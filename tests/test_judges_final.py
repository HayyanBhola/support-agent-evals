"""The one final frozen-judge evaluation on test (Day 19): fake judge, temp split labels and a
temp access log; the real test labels are never read."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalkit import cli
from evalkit.calibration.loader import FINAL_ENV
from evalkit.io import write_jsonl
from evalkit.judges import client as jc
from evalkit.judges import final, frozen, runner
from evalkit.schemas import Criterion, Label
from tests.test_judges import (
    DEV,
    RUBRIC,
    SPLIT,
    SPLIT_FILE,
    TEST,
    FakeJudge,
    case,
    cfg,
    jr,
    lab,
    ok_json,
    result,
    setup,
)

ROOT = Path(__file__).resolve().parent.parent


def test_real_frozen_json_matches_the_prompt_files() -> None:
    assert frozen.frozen_problems(ROOT / "prompts" / "judges") == []


def test_real_test_split_is_used_and_final_refuses_to_run_again() -> None:
    record = frozen.used_record(ROOT / "prompts" / "judges")
    assert record is not None and record["report"] == "reports/judges/FINAL_test_v1.md"
    res = CliRunner().invoke(cli.app, ["judge", "final", "--dry-run"])
    assert res.exit_code == 2 and "already used" in res.output


def _frozen_dir(tmp_path: Path) -> Path:
    pdir = tmp_path / "p"
    for crit, version in frozen.FINAL_VERSIONS.items():
        res = CliRunner().invoke(cli.app, ["judge", "prompt", "--prompt-dir", str(pdir),
                                           "--criterion", crit.value,
                                           "--prompt-version", version])  # fmt: skip
        assert res.exit_code == 0, res.output
    frozen.write_frozen(pdir)
    return pdir


def test_frozen_problems_and_the_used_record(tmp_path: Path) -> None:
    assert "no FROZEN.json" in frozen.frozen_problems(tmp_path)[0]
    pdir = _frozen_dir(tmp_path)
    assert frozen.frozen_problems(pdir) == [] and frozen.used_record(pdir) is None
    tone = pdir / "tone_v2.md"
    tone.write_text(tone.read_text(encoding="utf-8") + "x", encoding="utf-8")
    assert frozen.frozen_problems(pdir) == ["tone v2: the prompt file or its inputs changed"]
    tone.unlink()
    assert "tone" in frozen.frozen_problems(pdir)[0]
    data = json.loads((pdir / "FROZEN.json").read_text(encoding="utf-8"))
    data["criteria"]["safety"]["version"] = "v1"
    del data["criteria"]["relevance"]
    (pdir / "FROZEN.json").write_text(json.dumps(data), encoding="utf-8")
    found = frozen.frozen_problems(pdir)
    assert any("not the six" in p for p in found) and any("frozen at v1" in p for p in found)
    with pytest.raises(ValueError):
        frozen.mark_test_used({"date": "d"}, tmp_path / "none")
    frozen.mark_test_used({"date": "d"}, pdir)
    assert frozen.used_record(pdir) == {"date": "d"}


def test_write_frozen_keeps_the_used_record(tmp_path: Path) -> None:
    pdir = _frozen_dir(tmp_path)
    frozen.mark_test_used({"date": "d"}, pdir)
    frozen.write_frozen(pdir)
    assert frozen.used_record(pdir) == {"date": "d"}


def test_plan_on_test_needs_final(tmp_path: Path) -> None:
    results, cases = setup(TEST[:3])
    cache = runner.JudgeCache(tmp_path)
    rubric = RUBRIC[Criterion.RELEVANCE]
    with pytest.raises(PermissionError, match="locked"):
        runner.build_plan(cfg(), rubric, SPLIT, "test", results, cases, cache, only=TEST[:3])
    with pytest.raises(PermissionError):
        runner.build_plan(cfg(), rubric, SPLIT, "train", results, cases, cache, final=True)
    plan = runner.build_plan(cfg(), rubric, SPLIT, "test", results, cases, cache, final=True)
    assert plan.calls == 3 and len(plan.unanswered) == len(TEST) - 3


def _label(cid: str, verdict: str = "pass") -> Label:
    data = lab(cid, verdict).model_dump()
    data["verdicts"] = {c.value: verdict for c in Criterion}
    return Label.model_validate(data)


def test_final_report_sections() -> None:
    labels = {"a": _label("a"), "b": _label("b", "fail"), "c": _label("c")}
    dev_labels = {"d": _label("d"), "e": _label("e", "fail")}
    test = [jr("a", "pass"), jr("b", "pass"), jr("c", "judge_error")]
    entries = [
        final.FinalEntry(Criterion.RELEVANCE, "v3", test, [jr("d", "pass"), jr("e", "fail")], 1),
        final.FinalEntry(Criterion.TONE, "v2", [], []),
    ]
    sources = {"a": "legacy", "b": "synthetic", "c": "synthetic"}
    text = final.build_final_report(entries, labels, dev_labels, sources,
                                    {"run_id": "r1", "calls": 3, "cost_usd": 0.01})  # fmt: skip
    for heading in ("## Results per criterion (test)", "## Dev vs test", "## Bias check by case "
                    "source (test)", "## Confusion matrices", "## Limitations"):  # fmt: skip
        assert heading in text
    assert "| relevance | v3 | 2 | 50.0% |" in text and "| 1 | 0 | 1 | 1 |" in text
    assert "| b | fail |" in text and "| c | no JSON |" in text
    assert "| relevance v3 | 2 | 100.0% |" in text and "Disagreements: none." in text
    assert "test split is **USED**" in text


def _fake_client(provider: str, key: str, **kw: Any) -> FakeJudge:
    f = FakeJudge(lambda m: ok_json(evidence="30 days"))
    f.last_model = "gpt-5-mini-2025-08-07"

    async def aclose() -> None:
        return None

    f.aclose = aclose  # type: ignore[attr-defined]
    return f


def test_cli_final_runs_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = _frozen_dir(tmp_path)
    ids = [*TEST, *DEV[:5]]
    runs = tmp_path / "runs"
    write_jsonl(runs / "r1" / "results.jsonl", [result(cid) for cid in ids])
    ds = tmp_path / "d.jsonl"
    write_jsonl(ds, [case(cid) for cid in ids])
    labels = tmp_path / "labels.jsonl"
    write_jsonl(labels, [_label(cid) for cid in [*TEST, *DEV]])
    log = tmp_path / "access.log"
    out = tmp_path / "FINAL.md"
    args = ["judge", "final", "--run", "r1", "--runs-dir", str(runs), "--dataset", str(ds),
            "--prompt-dir", str(pdir), "--labels", str(labels), "--split-file", str(SPLIT_FILE),
            "--ledger", str(tmp_path / "u.jsonl"), "--cache-dir", str(tmp_path / "c"),
            "--access-log", str(log), "--out", str(out)]  # fmt: skip
    monkeypatch.delenv(FINAL_ENV, raising=False)
    dry = CliRunner().invoke(cli.app, [*args, "--dry-run"])
    flat = " ".join(dry.output.split())
    assert dry.exit_code == 0, dry.output
    assert "FROZEN.json matches" in flat and "no test label loaded" in flat
    assert "correctness v2: 0 to judge, 64 n_a" in flat and not log.exists()
    no_env = CliRunner().invoke(cli.app, [*args, "--max-calls", "500", "--max-usd", "1"])
    assert no_env.exit_code == 2 and FINAL_ENV in no_env.output
    monkeypatch.setenv(FINAL_ENV, "1")
    pricey = CliRunner().invoke(cli.app, [*args, "--max-calls", "500", "--max-usd", "0"])
    assert pricey.exit_code == 2 and "--max-usd" in pricey.output
    capped = CliRunner().invoke(cli.app, [*args, "--max-calls", "3", "--max-usd", "1"])
    assert capped.exit_code == 2 and not log.exists()
    monkeypatch.setattr(jc, "client_for", _fake_client)
    res = CliRunner().invoke(cli.app, [*args, "--max-calls", "500", "--max-usd", "1",
                                       "--daily-cap", "1000"])  # fmt: skip
    assert res.exit_code == 0, res.output
    assert "now USED" in " ".join(res.output.split())
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1
    assert "Week 3 final frozen-judge evaluation" in log.read_text(encoding="utf-8")
    text = out.read_text(encoding="utf-8")
    assert "| relevance | v3 | 64 | 100.0% |" in text
    record = frozen.used_record(pdir)
    assert record is not None and record["run_id"] == "r1"
    saved = runs / "r1" / "judges" / final.RESULTS_DIR / "relevance_v3_openai.jsonl"
    assert len(saved.read_text(encoding="utf-8").splitlines()) == 64
    again = CliRunner().invoke(cli.app, [*args, "--dry-run"])
    assert again.exit_code == 2 and "already used" in again.output


def test_cli_final_aborts_on_a_changed_prompt(tmp_path: Path) -> None:
    pdir = _frozen_dir(tmp_path)
    safety = pdir / "safety_v3.md"
    safety.write_text(safety.read_text(encoding="utf-8") + "tuned", encoding="utf-8")
    res = CliRunner().invoke(cli.app, ["judge", "final", "--prompt-dir", str(pdir),
                                       "--dry-run"])  # fmt: skip
    assert res.exit_code == 2 and "aborted" in res.output and "safety v3" in res.output
    bad = CliRunner().invoke(cli.app, ["judge", "final", "--prompt-dir", str(pdir), "--dry-run",
                                       "--sample", str(tmp_path / "none.jsonl")])  # fmt: skip
    assert bad.exit_code == 2 and "cannot read the sample" in bad.output
