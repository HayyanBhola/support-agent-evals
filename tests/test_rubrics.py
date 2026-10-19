"""Rubric files: validation, examples, rubric_hash and `evalkit rubric check`."""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalkit import cli
from evalkit.io import write_jsonl
from evalkit.rubrics import (
    EXAMPLES_FILE,
    KEY_RULES_MAX,
    KEY_RULES_MIN,
    RubricError,
    criterion_files,
    example_ids,
    load_rubrics,
    parse_rubric,
    rubric_hash,
)
from evalkit.schemas import Criterion, TestCase

ROOT = Path(__file__).resolve().parent.parent
RUBRICS = ROOT / "rubrics"
DATASET = ROOT / "datasets" / "golden_v1.2.jsonl"


@pytest.fixture
def rubric_dir(tmp_path: Path) -> Path:
    """A copy of the real rubric folder to break one thing at a time."""
    d = tmp_path / "rubrics"
    shutil.copytree(RUBRICS, d)
    return d


def problems(rubric_dir: Path, dataset: Path | None = DATASET) -> list[str]:
    with pytest.raises(RubricError) as info:
        load_rubrics(rubric_dir, dataset)
    return info.value.problems


# --------------------------------------------------------------------------- the real files


def test_real_rubrics_are_valid() -> None:
    rubrics = load_rubrics(RUBRICS, DATASET)
    assert [r.criterion for r in rubrics] == list(Criterion)
    for r in rubrics:
        assert r.definition and len(r.pass_examples) >= 3
        assert len(r.fail_examples) >= 3 or "Fail examples" in r.shortages
        bumped = (Criterion.CORRECTNESS, Criterion.RELEVANCE)
        assert r.meta.version == (3 if r.criterion in bumped else 2)
        assert KEY_RULES_MIN <= len(r.key_rules) <= KEY_RULES_MAX
    listed = json.loads((RUBRICS / EXAMPLES_FILE).read_text(encoding="utf-8"))
    assert listed["case_ids"] == example_ids(rubrics)
    assert "Day 13" in listed["todo"]


def test_examples_quote_the_day7_replies_verbatim() -> None:
    from evalkit.route_report import load_run
    from evalkit.runner import RUNS_DIR

    run_dir = ROOT / RUNS_DIR
    if not (run_dir / "20261013-191847-f23b").exists():
        pytest.skip("Day 7 run not on this machine (runs/ is not in git)")
    _, results, _ = load_run(run_dir, "20261013-191847-f23b")
    replies = {r.case_id: r.response.reply for r in results if r.response}
    for path in criterion_files(RUBRICS):
        text = path.read_text(encoding="utf-8")
        rubric, _ = parse_rubric(path)
        assert rubric is not None
        for cid in (*rubric.pass_examples, *rubric.fail_examples):
            quoted = "\n".join(("> " + line if line else ">") for line in replies[cid].split("\n"))
            assert quoted in text, f"{path.name}: {cid} is not quoted verbatim"


# --------------------------------------------------------------------------- validation


def test_missing_section(rubric_dir: Path) -> None:
    path = rubric_dir / "tone.md"
    path.write_text(path.read_text(encoding="utf-8").replace("## Edge cases", "## Notes"),
                    encoding="utf-8")  # fmt: skip
    assert "tone.md: missing section '## Edge cases'" in problems(rubric_dir)


def test_section_without_rules_and_missing_definition(rubric_dir: Path) -> None:
    path = rubric_dir / "tone.md"
    text = path.read_text(encoding="utf-8")
    start = text.index("## Pass if")
    end = text.index("## Fail if")
    text = text[:start] + "## Pass if\n\nnothing here\n\n" + text[end:]
    path.write_text(text.replace("**Definition:**", "Definition:"), encoding="utf-8")
    found = problems(rubric_dir)
    assert "tone.md: section '## Pass if' has no '- ' rule" in found
    assert "tone.md: missing a one-line '**Definition:** ...'" in found


def test_key_rules_box_is_required_with_three_to_five_rules(rubric_dir: Path) -> None:
    path = rubric_dir / "tone.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("## Key rules", "## Summary"), encoding="utf-8")
    assert "tone.md: missing section '## Key rules'" in problems(rubric_dir)
    start, end = text.index("## Key rules"), text.index("## Pass if")

    def with_rules(n: int) -> str:
        box = ["## Key rules", "", *(f"- rule {i}" for i in range(n)), "", ""]
        return text[:start] + "\n".join(box) + text[end:]

    path.write_text(with_rules(6), encoding="utf-8")
    assert "tone.md: '## Key rules' has 6 rules; needs 3 to 5" in problems(rubric_dir)
    path.write_text(with_rules(2), encoding="utf-8")
    assert "tone.md: '## Key rules' has 2 rules; needs 3 to 5" in problems(rubric_dir)


def test_v2_tone_no_longer_judges_voice() -> None:
    tone = {r.criterion: r for r in load_rubrics(RUBRICS, DATASET)}[Criterion.TONE]
    rules = tone.sections["Fail if"]
    assert "third person" not in rules and "Customers should" not in rules
    assert "syn-knowledge-b3e584f1" in tone.pass_examples
    assert "syn-knowledge-b3e584f1" not in tone.fail_examples
    assert any("Voice is not tone" in k for k in tone.key_rules)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("version: 2", "version: 0", "version must be at least 1"),
        ("verdicts: [pass, fail, n_a]", "verdicts: [pass, fail]", "exactly [pass, fail, n_a]"),
        ("id: tone", "id: vibes", "id: Input should be"),
        ("  routes: [", "  routes: [teleport, ", "applies_to.routes"),
        ("answer_types: [", "answer_types: [magic, ", "applies_to.answer_types"),
        ("version: 2", "version: 2\ncolour: blue", "colour: Extra inputs"),
        ("applies_by: actual", "applies_by: sometimes", "applies_by: Input should be"),
        ("applies_by: actual\n", "", "applies_by: Field required"),
        ("verdicts: [pass, fail, n_a]", "verdicts: [pass, fail, n_a", "not valid YAML"),
    ],
)
def test_bad_frontmatter(rubric_dir: Path, old: str, new: str, message: str) -> None:
    path = rubric_dir / "tone.md"
    path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
    assert any(message in p for p in problems(rubric_dir)), problems(rubric_dir)


def test_frontmatter_missing_or_mismatched(rubric_dir: Path) -> None:
    tone = rubric_dir / "tone.md"
    tone.write_text(tone.read_text(encoding="utf-8").split("---\n", 2)[2], encoding="utf-8")
    safety = rubric_dir / "safety.md"
    safety.write_text(safety.read_text(encoding="utf-8").replace("id: safety", "id: tone"),
                      encoding="utf-8")  # fmt: skip
    found = problems(rubric_dir)
    assert "tone.md: missing YAML frontmatter (--- ... ---) at the top" in found
    assert "safety.md: id 'tone' doesn't match the file name" in found


def test_empty_applies_to(rubric_dir: Path) -> None:
    path = rubric_dir / "tone.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "routes: [escalate, guardrail, medical, greeting, clarify, database, knowledge]",
        "routes: []",
    )
    path.write_text(text, encoding="utf-8")
    assert any("must not be empty" in p for p in problems(rubric_dir))


def test_unknown_case_id(rubric_dir: Path) -> None:
    path = rubric_dir / "relevance.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace("### case_id: syn-knowledge-726eea6d", "### case_id: syn-made-up-1")
    path.write_text(text, encoding="utf-8")
    found = problems(rubric_dir)
    assert "relevance.md: example case_id 'syn-made-up-1' is not in the dataset" in found
    assert any(EXAMPLES_FILE in p and "out of date" in p for p in found)


def test_too_few_examples_needs_a_shortage_note(rubric_dir: Path) -> None:
    path = rubric_dir / "faithfulness.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("Shortage:", "Note:"), encoding="utf-8"
    )
    assert any("'## Fail examples' has 2 example(s)" in p for p in problems(rubric_dir))


def test_repeated_example(rubric_dir: Path) -> None:
    path = rubric_dir / "tone.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace("### case_id: legacy-esc-safety", "### case_id: legacy-greeting")
    path.write_text(text, encoding="utf-8")
    assert any("repeats ['legacy-greeting']" in p for p in problems(rubric_dir))


def test_missing_rubric_and_examples_file(rubric_dir: Path) -> None:
    (rubric_dir / "tone.md").unlink()
    (rubric_dir / EXAMPLES_FILE).unlink()
    found = problems(rubric_dir)
    assert "tone.md: missing (one file per criterion)" in found
    assert f"{EXAMPLES_FILE}: missing" in found


def test_unreadable_examples_file(rubric_dir: Path) -> None:
    (rubric_dir / EXAMPLES_FILE).write_text("{}", encoding="utf-8")
    assert any("unreadable" in p for p in problems(rubric_dir))


def test_dataset_check_can_be_skipped(rubric_dir: Path, tmp_path: Path) -> None:
    write_jsonl(tmp_path / "tiny.jsonl", [TestCase.model_validate(
        {"id": "x", "question": "q", "category": "c", "expected_route": None,
         "should_escalate": False, "source": "human"})])  # fmt: skip
    assert any("not in the dataset" in p for p in problems(rubric_dir, tmp_path / "tiny.jsonl"))
    assert len(load_rubrics(rubric_dir, None)) == len(Criterion)


# --------------------------------------------------------------------------- hash


def test_rubric_hash_is_stable_and_sensitive(rubric_dir: Path) -> None:
    h = rubric_hash(rubric_dir)
    assert len(h) == 64 and h == rubric_hash(rubric_dir) == rubric_hash(RUBRICS)
    # CRLF checkouts hash the same
    tone = rubric_dir / "tone.md"
    tone.write_bytes(tone.read_bytes().replace(b"\n", b"\r\n"))
    assert rubric_hash(rubric_dir) == h
    # README and examples_used.json are not part of the hash
    (rubric_dir / "README.md").write_text("changed", encoding="utf-8")
    assert rubric_hash(rubric_dir) == h
    tone.write_text(tone.read_text(encoding="utf-8") + "\n- one more rule\n", encoding="utf-8")
    assert rubric_hash(rubric_dir) != h


# --------------------------------------------------------------------------- CLI


def test_cli_rubric_check(rubric_dir: Path) -> None:
    result = CliRunner().invoke(cli.app, ["rubric", "check", "--dir", str(rubric_dir),
                                          "--dataset", str(DATASET)])  # fmt: skip
    assert result.exit_code == 0, result.output
    assert f"rubric_hash: {rubric_hash(rubric_dir)}" in result.output
    assert "faithfulness" in result.output and "33 example case_ids" in result.output
    (rubric_dir / "tone.md").unlink()
    bad = CliRunner().invoke(cli.app, ["rubric", "check", "--dir", str(rubric_dir),
                                       "--dataset", str(DATASET)])  # fmt: skip
    assert bad.exit_code == 2 and "rubric check failed" in bad.output


def test_applies_by_follows_the_agreed_hybrid_rule(rubric_dir: Path) -> None:
    from evalkit.rubrics import AGREED_APPLIES_BY

    by = {r.criterion.value: r.meta.applies_by for r in load_rubrics(RUBRICS, DATASET)}
    assert by == {c.value: v for c, v in AGREED_APPLIES_BY.items()}
    assert {k for k, v in by.items() if v == "expected"} == {
        "correctness",
        "completeness",
        "escalation_appropriate",
    }
    path = rubric_dir / "correctness.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("applies_by: expected", "applies_by: actual"),
        encoding="utf-8",
    )
    expected_problem = (
        "correctness.md: applies_by is 'actual', but the agreed rule for correctness is 'expected'"
    )
    assert expected_problem in problems(rubric_dir)


def test_applies_when_has_gold_only_for_correctness(rubric_dir: Path) -> None:
    from evalkit.rubrics import AGREED_APPLIES_WHEN

    by = {r.criterion.value: r.meta.applies_when for r in load_rubrics(RUBRICS, DATASET)}
    assert by == {c.value: v for c, v in AGREED_APPLIES_WHEN.items()}
    assert [k for k, v in by.items() if v == "has_gold"] == ["correctness"]
    path = rubric_dir / "correctness.md"
    text = path.read_text(encoding="utf-8").replace("applies_when: has_gold", "applies_when: route")
    path.write_text(text, encoding="utf-8")
    assert (
        "correctness.md: applies_when is 'route', but the agreed rule for correctness is "
        "'has_gold'" in problems(rubric_dir)
    )
    path.write_text(
        text.replace("applies_when: route", "applies_when: sometimes"), encoding="utf-8"
    )
    assert any("applies_when: Input should be" in p for p in problems(rubric_dir))


def test_judging_order(rubric_dir: Path) -> None:
    from evalkit.rubrics import judging_order

    order = judging_order(RUBRICS)
    assert order[0] is Criterion.SAFETY and order[-1] is Criterion.TONE and len(order) == 7
    readme = rubric_dir / "README.md"
    text = readme.read_text(encoding="utf-8")
    readme.write_text(text.replace("7. **tone**", "7. **vibes**"), encoding="utf-8")
    with pytest.raises(RubricError, match="unknown criterion"):
        judging_order(rubric_dir)
    readme.write_text(text.replace("7. **tone**", "7. **safety**"), encoding="utf-8")
    with pytest.raises(RubricError, match="every criterion once"):
        judging_order(rubric_dir)
    readme.write_text(text.replace("## (c) Judging order", "## Order"), encoding="utf-8")
    with pytest.raises(RubricError, match="missing section"):
        judging_order(rubric_dir)
