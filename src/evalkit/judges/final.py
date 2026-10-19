"""The one final frozen-judge evaluation on the locked test split (Day 19).

``evalkit judge final`` runs the six frozen judge prompts (OpenAI gpt-5-mini) on the 64 test
cases, then loads the test labels **once** (``load_labels("test", final=True, ...)``, logged to
``reports/test_access.log``) and writes ``reports/judges/FINAL_test_v1.md``: per criterion n,
% agreement, kappa and AC1 with 95% CIs, judge fails vs human fails, the confusion matrix and
every disagreement; dev vs test side by side; the source split on test; and the limitations.
Afterwards FROZEN.json records the test split as used: a later prompt change needs a new test
set, and the command refuses to run again.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from evalkit.judges.report import (
    _cell,
    _f,
    _pct,
    agreement,
    bias_lines,
    human_fail_lines,
)
from evalkit.judges.schema import JudgeResult
from evalkit.schemas import Criterion, Label

FINAL_REASON: Final = "Week 3 final frozen-judge evaluation"
RESULTS_DIR: Final = "final_test"  # runs/<run_id>/judges/final_test/
REPORT_FILE: Final = "FINAL_test_v1.md"
MAX_JUDGE_ERRORS: Final = 5  # more than this: stop before the test labels are loaded


@dataclass(frozen=True)
class FinalEntry:
    criterion: Criterion
    version: str
    test: Sequence[JudgeResult]
    dev: Sequence[JudgeResult]  # the same frozen prompt on dev (empty if not on file)
    skipped: int = 0  # n_a by applicability or a judge skip: never sent


def _ci(value: float | None, low: float | None, high: float | None) -> str:
    return f"{_f(value)} [{_f(low)}, {_f(high)}]"


def summary_lines(entries: Sequence[FinalEntry], labels: Mapping[str, Label]) -> list[str]:
    lines = [
        "| Criterion | Prompt | n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] "
        "| human fails | judge fails | judge_error | n_a (not sent) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for e in entries:
        a = agreement(e.test, labels, e.criterion)
        m = a.confusion
        errors = sum(r.verdict == "judge_error" for r in e.test)
        lines.append(
            f"| {e.criterion.value} | {e.version} | {m.n} | {_pct(m.percent_agreement())} | "
            f"{_ci(a.kappa.value, a.kappa_ci.low, a.kappa_ci.high)} | "
            f"{_ci(a.ac1.value, a.ac1_ci.low, a.ac1_ci.high)} | {m.a_fails} | {m.b_fails} | "
            f"{errors} | {e.skipped} |"
        )
    return lines


def dev_vs_test_lines(
    entries: Sequence[FinalEntry], dev_labels: Mapping[str, Label], labels: Mapping[str, Label]
) -> list[str]:
    lines = [
        "| Criterion | dev n | dev % agreement | dev AC1 [95% CI] | test n | test % agreement "
        "| test AC1 [95% CI] | AC1 test - dev | human fails dev / test |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for e in entries:
        d = agreement(e.dev, dev_labels, e.criterion)
        t = agreement(e.test, labels, e.criterion)
        if d.ac1.value is None or t.ac1.value is None:
            change = "–"
        else:
            change = f"{t.ac1.value - d.ac1.value:+.2f}"
        lines.append(
            f"| {e.criterion.value} {e.version} | {d.confusion.n} | "
            f"{_pct(d.confusion.percent_agreement())} | "
            f"{_ci(d.ac1.value, d.ac1_ci.low, d.ac1_ci.high)} | {t.confusion.n} | "
            f"{_pct(t.confusion.percent_agreement())} | "
            f"{_ci(t.ac1.value, t.ac1_ci.low, t.ac1_ci.high)} | {change} | "
            f"{d.confusion.a_fails} / {t.confusion.a_fails} |"
        )
    return lines


def criterion_lines(e: FinalEntry, labels: Mapping[str, Label]) -> list[str]:
    a = agreement(e.test, labels, e.criterion)
    m = a.confusion
    by_id = {r.case_id: r for r in e.test}
    lines = [
        f"### {e.criterion.value} ({e.version})",
        "",
        "| human \\ judge | pass | fail |",
        "|---|---|---|",
        f"| **pass** | {m.pass_pass} | {m.pass_fail} |",
        f"| **fail** | {m.fail_pass} | {m.fail_fail} |",
        "",
    ]
    if not a.disagreements:
        lines.append("Disagreements: none.")
    else:
        lines += [
            f"Disagreements ({len(a.disagreements)}):",
            "",
            "| case_id | human | human note | judge | judge reason | judge evidence |",
            "|---|---|---|---|---|---|",
        ]
        for cid, human, judge in a.disagreements:
            r = by_id[cid]
            lines.append(
                f"| {cid} | {human} | {_cell(labels[cid].notes)} | {judge} | {_cell(r.reason)} "
                f"| {_cell(r.evidence)} |"
            )
    errors = [r for r in e.test if r.verdict == "judge_error"]
    if errors:
        lines += ["", "| judge_error case_id | error |", "|---|---|"]
        lines += [f"| {r.case_id} | {_cell(r.error)} |" for r in errors]
    return lines


def limitations_lines(entries: Sequence[FinalEntry], labels: Mapping[str, Label]) -> list[str]:
    fails = {e.criterion: agreement(e.test, labels, e.criterion).confusion.a_fails
             for e in entries}  # fmt: skip
    few = ", ".join(f"{c.value} {n}" for c, n in fails.items() if c != Criterion.RELEVANCE)
    return [
        f"- **Few human fails outside relevance** (test: {few}). With so few fails, % agreement "
        "and AC1 mostly measure agreement on passes, kappa is unstable or undefined, and the CIs "
        "are wide; these numbers can't show how well a judge **catches** failures. The synthetic "
        "negatives (`reports/judges/synthetic_negatives_v1.md`, dev-derived) are the fail-recall "
        "evidence: faithfulness 20/20, correctness 11/11, safety 14/15, tone 12/15, "
        "completeness 1/2 (an upper bound: template edits are easier than real failures).",
        "- **OpenAI also generated the synthetic test cases** (Day 5) and gpt-5-mini is the "
        "judge. The source split above shows whether agreement differs on its own family's "
        "cases; on dev only relevance was flagged, and a non-OpenAI judge (Qwen v1) showed the "
        "same gap, so most of it is case difficulty. Qwen agreed more on 4 synthetic relevance "
        "cases on dev: weak evidence of OpenAI leniency, not tested further on test.",
        "- **Tone has a role-play blind spot:** in the synthetic negatives the tone judge caught "
        "only 2 of 5 role-play / persona breaks. A test pass on tone does not rule them out.",
        "- **One labeler.** The reference is hayyan's labels (labels_v1.3); human-human "
        "agreement on 50 overlap records was κ 0.87 (escalation) and 78-90% (tone), so a judge "
        "can't be expected to agree with one person more than two people agree.",
        "- **One agent run, one judge setting:** the Day 7 run of `gemini-3.1-flash-lite`, "
        "gpt-5-mini at reasoning_effort=minimal with its default temperature (none sent). The "
        "numbers say how the judges score this agent's replies, not any agent's.",
        "- **Correctness is judged only on cases with a reference answer or gold facts** (the "
        "rest are n_a for the judge), so its n is small.",
        "- **The prompts were tuned on dev** (v1 -> v3 rounds). The test numbers are the honest "
        "estimate; a lower test score is the expected cost of that tuning.",
    ]


def build_final_report(
    entries: Sequence[FinalEntry],
    labels: Mapping[str, Label],
    dev_labels: Mapping[str, Label],
    sources: Mapping[str, str],
    meta: Mapping[str, Any],
) -> str:
    first = next((r for e in entries for r in e.test), None)
    model = first.model if first else meta.get("model", "–")
    prompts = ", ".join(f"{e.criterion.value} {e.version} (`{e.test[0].prompt_sha256[:12]}`)"
                        if e.test else f"{e.criterion.value} {e.version}"
                        for e in entries)  # fmt: skip
    bias_entries = [(e.criterion, e.version, e.test, "") for e in entries]
    lines = [
        "# FINAL: frozen judges vs human labels on the test split (v1)",
        "",
        "**One-time evaluation.** After this run the test split is **USED** (recorded in "
        "`prompts/judges/FROZEN.json`): any change to a judge prompt needs a new test set.",
        "",
        f"- **Judge:** `{model}` (OpenAI), reasoning_effort=minimal, model default temperature "
        "(none sent); the primary judge since Day 18.",
        f"- **Frozen prompts** (FROZEN.json verified before the run): {prompts}; rubric v4 "
        f"(`{first.rubric_hash[:12] if first else '–'}`).",
        f"- **Agent run:** `{meta.get('run_id', '–')}` (Day 7, `gemini-3.1-flash-lite`).",
        f"- **Human labels:** `{meta.get('labels_file', '–')}` (hayyan), test split "
        f"({meta.get('n_test', 64)} cases), loaded once with final=True, reason "
        f'"{meta.get("reason", FINAL_REASON)}" (`reports/test_access.log`).',
        f"- **Cost:** {meta.get('calls', 0)} API call(s), {meta.get('cached', 0)} from the cache; "
        f"about ${meta.get('cost_usd', 0.0):.3f} at approximate list prices "
        f"(pre-approved up to ${meta.get('max_usd', 0.0):.2f} / {meta.get('max_calls', 0)} "
        "calls).",
        f"- **Date (UTC):** {meta.get('date', '–')}; code at commit `{meta.get('commit', '–')}`.",
        "",
        "## Results per criterion (test)",
        "",
        "Human = hayyan, judge = gpt-5-mini; n counts cases both judged (judge_error and n_a "
        "left out). CIs: percentile bootstrap, 95%.",
        "",
        *summary_lines(entries, labels),
        "",
        "## Dev vs test (same frozen prompts)",
        "",
        "Dev: the same judge and prompts vs labels_v1.3 on the 96 dev cases. The prompts were "
        "written while reading dev disagreements, so test is expected to be a bit lower.",
        "",
        *dev_vs_test_lines(entries, dev_labels, labels),
        "",
        "## Bias check by case source (test)",
        "",
        "**non-OpenAI** = legacy + known_bugs cases; **synthetic** = OpenAI-generated (Day 5). "
        "Flag: an AC1 gap above 0.15 or an agreement gap above 10 points.",
        "",
        *bias_lines(bias_entries, labels, sources),
        "",
        "| Criterion | non-OpenAI: human fails / judged | synthetic: human fails / judged |",
        "|---|---|---|",
        *human_fail_lines([e.criterion for e in entries], labels, sources),
        "",
        "## Confusion matrices and every disagreement",
        "",
    ]
    for e in entries:
        lines += [*criterion_lines(e, labels), ""]
    lines += ["## Limitations", "", *limitations_lines(entries, labels), ""]
    return "\n".join(lines)
