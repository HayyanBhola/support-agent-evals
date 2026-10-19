"""Claude's first-pass review CSV, the human audit file and apply_review's CSV mode."""

import csv
import importlib.util
from pathlib import Path
from typing import Any

import pytest

from evalkit.io import read_jsonl, write_jsonl
from evalkit.review.apply import (
    AI_TAG,
    FIXED_TAG,
    HUMAN_TAG,
    ReviewError,
    apply_audit,
    apply_review,
    read_decisions_csv,
    render_log,
    render_method,
    review_tags,
)
from evalkit.review.audit import (
    VERDICT_PROMPT,
    AuditEntry,
    AuditError,
    parse_audit,
    render_audit,
)
from evalkit.schemas import TestCase

from .test_review import sample_cases

ROOT = Path(__file__).resolve().parent.parent
COLUMNS = ["id", "decision", "fixed_expected_route", "fixed_should_escalate",
           "fixed_reference_answer", "note", "reviewer"]  # fmt: skip


def decisions() -> list[dict[str, str]]:
    base = dict.fromkeys(COLUMNS, "") | {"reviewer": "claude", "note": "rule 5: fine"}
    return [
        base | {"id": "syn-k1", "decision": "keep"},
        base | {"id": "syn-medical-642a3f44", "decision": "fix",
                "fixed_expected_route": "escalate", "fixed_should_escalate": "true",
                "note": "rule 9: reaction already happened"},
        base | {"id": "syn-d1", "decision": "keep", "note": "rule 1: price from the database"},
        base | {"id": "syn-g1", "decision": "delete", "note": "duplicate in meaning"},
    ]  # fmt: skip


def write_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return path


def audit_text(rows: list[dict[str, str]], audited: list[str]) -> str:
    by_id = {c.id: c for c in sample_cases()}
    entries = [AuditEntry.from_decision(by_id[r["id"]], r) for r in rows if r["id"] in audited]
    return render_audit(entries, seed=6, total=len(rows))


def fill(text: str, case_id: str, verdict: str, **override: str) -> str:
    """Fill one entry's verdict (and override lines) in a rendered audit."""
    head, _, rest = text.partition(f". {case_id} (")
    block, sep, tail = rest.partition("\n## ")
    block = block.replace(f"MY VERDICT: {VERDICT_PROMPT}", f"MY VERDICT: {verdict}")
    for key, value in override.items():
        block = block.replace(f"OVERRIDE {key}:", f"OVERRIDE {key}: {value}")
    return head + f". {case_id} (" + block + sep + tail


# --------------------------------------------------------------------------- audit file


def test_render_audit_shows_question_old_label_decision_and_reason() -> None:
    text = audit_text(decisions(), ["syn-medical-642a3f44", "syn-k1"])
    assert "every fix and delete (1)** and **1 randomly sampled keeps** (seed 6): 2" in text
    assert "## 1. syn-k1 (keep)" in text and "## 2. syn-medical-642a3f44 (fix)" in text
    assert "- **Question:** White bumps after the moisturizer, what are they?" in text
    assert "- **Old label:** expected_route=medical, should_escalate=false" in text
    assert "fix (fixed_expected_route=escalate; fixed_should_escalate=true)" in text
    assert "- **Claude's reason:** rule 9: reaction already happened" in text
    assert text.count(f"MY VERDICT: {VERDICT_PROMPT}") == 2


def test_parse_audit_agree_and_disagree() -> None:
    text = audit_text(decisions(), ["syn-medical-642a3f44", "syn-k1", "syn-g1"])
    text = fill(text, "syn-k1", "agree")
    text = fill(text, "syn-g1", "Disagree", decision="keep", note="not a duplicate")
    text = fill(text, "syn-medical-642a3f44", "disagree", decision="Fix",
                fixed_should_escalate="(unset)", note="rule 9 but leave it unchecked")  # fmt: skip
    verdicts = parse_audit(text)
    assert verdicts["syn-k1"].agree
    assert verdicts["syn-g1"].override == {"decision": "keep", "note": "not a duplicate"}
    assert verdicts["syn-medical-642a3f44"].override["decision"] == "fix"


@pytest.mark.parametrize(
    ("verdict", "override", "message"),
    [
        (VERDICT_PROMPT, {}, "MY VERDICT is not filled in"),
        ("maybe", {}, "MY VERDICT is not filled in"),
        ("agree", {"note": "x"}, "verdict is agree but OVERRIDE lines are filled"),
        ("disagree", {"note": "x"}, "disagree needs OVERRIDE decision"),
        ("disagree", {"decision": "keep"}, "disagree needs an OVERRIDE note"),
    ],
)
def test_parse_audit_refuses(verdict: str, override: dict[str, str], message: str) -> None:
    text = fill(audit_text(decisions(), ["syn-k1"]), "syn-k1", verdict, **override)
    with pytest.raises(AuditError, match=message):
        parse_audit(text)


def test_parse_audit_structural_problems() -> None:
    with pytest.raises(AuditError, match="no audit entries"):
        parse_audit("# nothing here\n")
    text = fill(audit_text(decisions(), ["syn-k1"]), "syn-k1", "agree")
    entry = "\n## " + text.split("\n## ", 1)[1]
    with pytest.raises(AuditError, match="audited twice"):
        parse_audit(text + entry)
    with pytest.raises(AuditError, match="unknown OVERRIDE field"):
        parse_audit(text.replace("MY VERDICT: agree", "MY VERDICT: disagree\nOVERRIDE color: red"))


# --------------------------------------------------------------------------- CSV + apply


def test_read_decisions_csv(tmp_path: Path) -> None:
    rows = read_decisions_csv(write_csv(tmp_path / "d.csv", decisions()))
    assert rows[1]["notes"] == "rule 9: reaction already happened" and "note" not in rows[1]
    bad = [r | {"reviewer": ""} for r in decisions()]
    with pytest.raises(ReviewError, match="no reviewer"):
        read_decisions_csv(write_csv(tmp_path / "bad.csv", bad))
    no_col = [{k: v for k, v in r.items() if k != "reviewer"} for r in decisions()]
    with pytest.raises(ReviewError, match="missing column"):
        read_decisions_csv(write_csv(tmp_path / "nocol.csv", no_col))


def test_apply_audit_overrides_tags_and_method(tmp_path: Path) -> None:
    ai_rows = read_decisions_csv(write_csv(tmp_path / "d.csv", decisions()))
    text = audit_text(decisions(), ["syn-medical-642a3f44", "syn-g1", "syn-d1"])
    text = fill(text, "syn-medical-642a3f44", "agree")
    text = fill(text, "syn-g1", "disagree", decision="keep", note="it is a fine greeting")
    text = fill(text, "syn-d1", "disagree", decision="fix",
                fixed_expected_route="(none)", note="rule 1: route not checkable")  # fmt: skip
    outcome = apply_audit(ai_rows, parse_audit(text))
    assert outcome.agreed == 1 and [o.case_id for o in outcome.overrides] == ["syn-d1", "syn-g1"]
    result = apply_review(sample_cases(), outcome.rows, extra_tags=review_tags(outcome))
    tags = {c.id: c.tags for c in result.cases}
    assert set(tags) == {"syn-k1", "syn-medical-642a3f44", "syn-d1", "syn-g1"}
    assert AI_TAG in tags["syn-k1"] and HUMAN_TAG not in tags["syn-k1"]
    assert {AI_TAG, HUMAN_TAG, FIXED_TAG} <= set(tags["syn-medical-642a3f44"])
    assert HUMAN_TAG in tags["syn-g1"]
    d1 = next(c for c in result.cases if c.id == "syn-d1")
    assert d1.expected_route is None
    log = render_log(result, render_method(ai_rows, outcome, "seed 6"))
    assert "## Review method" in log
    assert "First pass by Claude (AI)** on all 4 cases" in log
    assert "keep 2, fix 1, delete 1" in log
    assert "of 3 cases: every fix and delete (2) plus 1 randomly sampled keeps (seed 6)" in log
    assert "Agreed: 1; overridden: 2." in log
    assert "The other 1 keeps were **not checked by a human**" in log
    assert "| `syn-g1` | delete | keep | it is a fine greeting |" in log
    assert "human override: rule 1: route not checkable" in log


def test_apply_audit_override_to_delete_clears_fixes(tmp_path: Path) -> None:
    ai_rows = read_decisions_csv(write_csv(tmp_path / "d.csv", decisions()))
    text = audit_text(decisions(), ["syn-medical-642a3f44"])
    text = fill(text, "syn-medical-642a3f44", "disagree", decision="delete", note="unrealistic")
    outcome = apply_audit(ai_rows, parse_audit(text))
    med = next(r for r in outcome.rows if r["id"] == "syn-medical-642a3f44")
    assert med["decision"] == "delete" and med["fixed_expected_route"] == ""
    assert med["reviewer"] == "human"


def test_apply_audit_rejects_unknown_ids(tmp_path: Path) -> None:
    ai_rows = read_decisions_csv(write_csv(tmp_path / "d.csv", decisions()))
    text = fill(audit_text(decisions(), ["syn-k1"]), "syn-k1", "agree")
    with pytest.raises(AuditError, match="syn-zzz: audited but not in the decisions"):
        apply_audit(ai_rows, parse_audit(text.replace("syn-k1 (keep)", "syn-zzz (keep)")))


def load_script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_apply_review_script_csv_mode(tmp_path: Path, capsys: Any) -> None:
    dataset = tmp_path / "cases.jsonl"
    write_jsonl(dataset, sample_cases())
    csv_path = write_csv(tmp_path / "d.csv", decisions())
    audit = tmp_path / "audit.md"
    raw = audit_text(decisions(), ["syn-medical-642a3f44", "syn-g1"])
    audit.write_text(raw, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    script = load_script("apply_review")
    args = ["x.xlsx", str(dataset), "--decisions", str(csv_path), "--audit", str(audit)]
    assert script.main(args, out_dir=out) == 1  # verdicts not filled yet
    assert "MY VERDICT is not filled in" in capsys.readouterr().out
    assert script.main(args[:4], out_dir=out) == 1
    assert "--decisions needs --audit" in capsys.readouterr().out
    assert list(out.iterdir()) == []
    audit.write_text(fill(fill(raw, "syn-medical-642a3f44", "agree"), "syn-g1", "agree"),
                     encoding="utf-8")  # fmt: skip
    assert script.main(args, out_dir=out) == 0
    cases = list(read_jsonl(out / "synthetic_reviewed_v1.jsonl", TestCase))
    assert [c.id for c in cases] == ["syn-k1", "syn-medical-642a3f44", "syn-d1"]
    log = (out / "review_log_v1.md").read_text(encoding="utf-8")
    assert "## Review method" in log and "Agreed: 2; overridden: 0." in log


# --------------------------------------------------------------------------- the real files


def test_committed_ai_review_matches_the_dataset() -> None:
    rows = read_decisions_csv(ROOT / "review" / "ai_review_decisions.csv")
    dataset = list(read_jsonl(ROOT / "datasets" / "synthetic_dedup_v1.jsonl", TestCase))
    assert [r["id"] for r in rows] == [c.id for c in dataset]
    assert {r["reviewer"] for r in rows} == {"claude"}
    assert all(r["notes"] for r in rows)
    changed = [r for r in rows if r["decision"] != "keep"]
    text = (ROOT / "review" / "human_audit.md").read_text(encoding="utf-8")
    for r in changed:
        assert f". {r['id']} ({r['decision']})" in text
    assert text.count("MY VERDICT:") == len(changed) + 20
    fixes = [r for r in rows if r["decision"] == "fix"]
    fixed_ids = {r["id"] for r in fixes}
    result = apply_review([c for c in dataset if c.id in fixed_ids], fixes)
    assert len(result.cases) == len(fixes)  # every fix validates


def test_remove_tags_on_fix_rows(tmp_path: Path) -> None:
    rows = decisions()
    rows[0] |= {"decision": "fix", "remove_tags": "evo-plain | gen-knowledge",
                "fixed_reference_answer": "Within 30 days of delivery."}  # fmt: skip
    for r in rows[1:]:
        r["remove_tags"] = ""
    ai_rows = read_decisions_csv(write_csv(tmp_path / "d.csv", rows))
    result = apply_review(sample_cases(), ai_rows)
    k1 = next(c for c in result.cases if c.id == "syn-k1")
    assert "evo-plain" not in k1.tags and "gen-knowledge" not in k1.tags
    assert FIXED_TAG in k1.tags
    log = render_log(result)
    assert "| `syn-k1` | tags | removed: evo-plain, gen-knowledge |" in log
    rows[0]["remove_tags"] = "not-a-tag"
    with pytest.raises(ReviewError, match=r"remove_tags \['not-a-tag'\] not on the case"):
        apply_review(sample_cases(), read_decisions_csv(write_csv(tmp_path / "e.csv", rows)))
    rows[0] |= {"decision": "keep", "fixed_reference_answer": "", "remove_tags": "evo-plain"}
    with pytest.raises(ReviewError, match="remove_tags filled but decision is 'keep'"):
        apply_review(sample_cases(), read_decisions_csv(write_csv(tmp_path / "f.csv", rows)))


def test_fixed_gold_context_ids(tmp_path: Path) -> None:
    rows = decisions()
    for r in rows:
        r["fixed_gold_context_ids"] = ""
    rows[0] |= {"decision": "fix", "fixed_gold_context_ids": "return_policy.md-1 | faq.md-0"}
    ai_rows = read_decisions_csv(write_csv(tmp_path / "d.csv", rows))
    known = frozenset({"return_policy.md-1", "faq.md-0"})
    result = apply_review(sample_cases(), ai_rows, chunk_ids=known)
    k1 = next(c for c in result.cases if c.id == "syn-k1")
    assert k1.gold_context_ids == ["return_policy.md-1", "faq.md-0"]
    assert "| `syn-k1` | gold_context_ids |" in render_log(result)
    with pytest.raises(ReviewError, match=r"\['faq.md-0'\] are not chunk ids"):
        apply_review(sample_cases(), ai_rows, chunk_ids=frozenset({"return_policy.md-1"}))


def test_override_note_keeps_the_earlier_note(tmp_path: Path) -> None:
    ai_rows = read_decisions_csv(write_csv(tmp_path / "d.csv", decisions()))
    text = audit_text(decisions(), ["syn-medical-642a3f44"])
    text = fill(text, "syn-medical-642a3f44", "disagree", decision="fix",
                fixed_should_escalate="(unset)", note="leave escalation unchecked")  # fmt: skip
    med = next(r for r in apply_audit(ai_rows, parse_audit(text)).rows
               if r["id"] == "syn-medical-642a3f44")  # fmt: skip
    assert med["notes"] == (
        "human override: leave escalation unchecked. Earlier: rule 9: reaction already happened"
    )
