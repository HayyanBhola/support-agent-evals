"""The judge-vs-human report (Day 16): ``evalkit judge agree`` ->
``reports/judges/<criterion>_dev_v1.md``.

The judge's verdicts on the dev split against the human labels (labels_v1.2): n, percent
agreement, Cohen's kappa and Gwet's AC1 with bootstrap CIs, the confusion matrix, the
judge_error count, every disagreement (human verdict + note, judge verdict + reason +
evidence), and, if it was run, judge self-consistency (verdict flips with the cache
bypassed).
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from evalkit.calibration.agreement import Confusion, Pair, cohen_kappa, gwet_ac1
from evalkit.calibration.compare import MetricVsLabels, metric_vs_labels
from evalkit.judges.schema import JudgeResult
from evalkit.schemas import Criterion, Label, MetricResult
from evalkit.stats.bootstrap import percentile_ci

REPORT_DIR: Final = Path("reports") / "judges"


def as_metric_rows(results: Sequence[JudgeResult]) -> list[MetricResult]:
    """Judge verdicts as pass/fail rows (judge_error left out)."""
    return [
        MetricResult(case_id=r.case_id, metric=f"judge_{r.criterion}", passed=r.verdict == "pass")
        for r in results
        if r.verdict != "judge_error"
    ]


def _cell(text: str | None) -> str:
    return (text or "–").replace("|", "\\|").replace("\n", " ").strip() or "–"


def _f(x: float | None) -> str:
    return "–" if x is None else f"{x:.2f}"


def agreement(
    results: Sequence[JudgeResult], labels: Mapping[str, Label], criterion: Criterion
) -> MetricVsLabels:
    rows = as_metric_rows(results)
    return metric_vs_labels(rows, labels, f"judge_{criterion.value}", criterion)


def build_report(
    criterion: Criterion,
    results: Sequence[JudgeResult],
    labels: Mapping[str, Label],
    *,
    labels_file: str,
    consistency: Sequence[JudgeResult] = (),
    compare: tuple[str, Sequence[JudgeResult], str] | None = None,
    versus: tuple[str, Sequence[JudgeResult], str] | None = None,
    sources: Mapping[str, str] | None = None,
) -> str:
    a = agreement(results, labels, criterion)
    m = a.confusion
    by_id = {r.case_id: r for r in results}
    errors = [r for r in results if r.verdict == "judge_error"]
    first = results[0] if results else None
    tokens_in = sum(r.input_tokens for r in results if not r.cache_hit)
    tokens_out = sum(r.output_tokens for r in results if not r.cache_hit)
    # GPT-5 models accept only their default temperature, so none was sent
    openai = bool(first and "openai" in first.thinking)
    temperature = "model default temperature (none sent)" if openai else "temperature 0"
    lines = [
        f"# Judge vs human: {criterion.value} (dev split)",
        "",
        f"- **Judge:** `{first.model if first else '–'}`, prompt sha256 "
        f"`{first.prompt_sha256[:12] if first else '–'}`, rubric_hash "
        f"`{first.rubric_hash[:12] if first else '–'}`, thinking: "
        f"{first.thinking if first else '–'}, {temperature}.",
        f"- **Human labels:** `{labels_file}` (hayyan), dev split only.",
        f"- **Cases judged:** {len(results)}; judge_error: **{len(errors)}**; compared with a "
        f"judged human label: {m.n}.",
        f"- **Tokens** (this run's calls, cache hits excluded): {tokens_in} in, {tokens_out} out.",
        "",
        "## Agreement",
        "",
        "| n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] | human fail rate | "
        "judge fail rate |",
        "|---|---|---|---|---|---|",
        f"| {m.n} | {_pct(m.percent_agreement())} | {_f(a.kappa.value)} "
        f"[{_f(a.kappa_ci.low)}, {_f(a.kappa_ci.high)}] | {_f(a.ac1.value)} "
        f"[{_f(a.ac1_ci.low)}, {_f(a.ac1_ci.high)}] | {_pct(m.prevalence()[0])} | "
        f"{_pct(m.prevalence()[1])} |",
        "",
        "| human \\ judge | pass | fail |",
        "|---|---|---|",
        f"| **pass** | {m.pass_pass} | {m.pass_fail} |",
        f"| **fail** | {m.fail_pass} | {m.fail_fail} |",
        "",
        "## Disagreements",
        "",
    ]
    if not a.disagreements:
        lines.append("None.")
    else:
        lines += [
            "| case_id | human | human note | judge | judge reason | judge evidence |",
            "|---|---|---|---|---|---|",
        ]
        for cid, human, judge in a.disagreements:
            r = by_id[cid]
            lines.append(
                f"| {cid} | {human} | {_cell(labels[cid].notes)} | {judge} | {_cell(r.reason)} "
                f"| {_cell(r.evidence)} |"
            )
    if errors:
        lines += ["", "## Judge errors", "", "| case_id | error |", "|---|---|"]
        lines += [f"| {r.case_id} | {_cell(r.error)} |" for r in errors]
    if sources is not None:
        lines += ["", *source_split_lines(results, labels, criterion, sources)]
    lines += ["", *consistency_lines(results, consistency)]
    if compare is not None:
        old_name, old, new_name = compare
        lines += ["", *comparison_lines(criterion, old_name, old, new_name, results, labels)]
    if versus is not None:
        other_name, other, this_name = versus
        lines += ["", *judge_vs_judge_lines(other_name, other, this_name, results, labels,
                                             criterion)]  # fmt: skip
    return "\n".join(lines) + "\n"


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def consistency_lines(results: Sequence[JudgeResult], again: Sequence[JudgeResult]) -> list[str]:
    lines = ["## Self-consistency (cache bypassed)", ""]
    if not again:
        return [*lines, "Not run."]
    first = {r.case_id: r for r in results}
    pairs = [(first[r.case_id], r) for r in again if r.case_id in first]
    flips = [(a, b) for a, b in pairs if a.verdict != b.verdict]
    lines.append(
        f"{len(pairs)} dev cases re-judged (seed 42 sample) with the cache bypassed: "
        f"**{len(flips)} verdict flip(s)** ({_pct(len(flips) / len(pairs) if pairs else None)})."
    )
    if flips:
        lines += ["", "| case_id | first | again | reason (again) |", "|---|---|---|---|"]
        lines += [f"| {a.case_id} | {a.verdict} | {b.verdict} | {_cell(b.reason)} |"
                  for a, b in flips]  # fmt: skip
    return lines


def comparison_lines(
    criterion: Criterion,
    old_name: str,
    old: Sequence[JudgeResult],
    new_name: str,
    new: Sequence[JudgeResult],
    labels: Mapping[str, Label],
) -> list[str]:
    """Two prompt versions against the same labels: metrics side by side, then which
    disagreements were fixed and which are new."""
    a_old, a_new = agreement(old, labels, criterion), agreement(new, labels, criterion)
    lines = [
        f"## {old_name} vs {new_name} (same labels, same dev cases)",
        "",
        f"| | {old_name} | {new_name} |",
        "|---|---|---|",
    ]
    old_cells, new_cells = _metric_cells(a_old), _metric_cells(a_new)
    lines += [f"| {title} | {old_cells[title]} | {new_cells[title]} |" for title in old_cells]
    old_dis = {cid: (h, j) for cid, h, j in a_old.disagreements}
    new_dis = {cid: (h, j) for cid, h, j in a_new.disagreements}
    new_by = {r.case_id: r for r in new}
    fixed = sorted(old_dis.keys() - new_dis.keys())
    appeared = sorted(new_dis.keys() - old_dis.keys())
    kept = sorted(old_dis.keys() & new_dis.keys())
    lines += ["", f"**Fixed in {new_name} ({len(fixed)}):** " + (", ".join(fixed) or "none")]
    lines += [f"**Still disagreeing ({len(kept)}):** " + (", ".join(kept) or "none")]
    lines += ["", f"**New in {new_name} ({len(appeared)}):**"]
    if appeared:
        lines += ["", "| case_id | human | judge | judge reason |", "|---|---|---|---|"]
        lines += [f"| {cid} | {new_dis[cid][0]} | {new_dis[cid][1]} | "
                  f"{_cell(new_by[cid].reason)} |" for cid in appeared]  # fmt: skip
    else:
        lines.append("none")
    return lines


def _metric_cells(a: MetricVsLabels) -> dict[str, str]:
    m = a.confusion
    return {
        "n": str(m.n),
        "% agreement": _pct(m.percent_agreement()),
        "Cohen's kappa [95% CI]": f"{_f(a.kappa.value)} [{_f(a.kappa_ci.low)}, "
        f"{_f(a.kappa_ci.high)}]",
        "Gwet's AC1 [95% CI]": f"{_f(a.ac1.value)} [{_f(a.ac1_ci.low)}, {_f(a.ac1_ci.high)}]",
        "human pass / judge fail": str(m.pass_fail),
        "human fail / judge pass": str(m.fail_pass),
        "both pass / both fail": f"{m.pass_pass} / {m.fail_fail}",
        "judge fail rate": _pct(m.prevalence()[1]),
    }


def judge_vs_judge_lines(
    name_a: str,
    a: Sequence[JudgeResult],
    name_b: str,
    b: Sequence[JudgeResult],
    labels: Mapping[str, Label],
    criterion: Criterion,
) -> list[str]:
    """Two judges on the cases both judged (no judge_error): agreement, kappa, AC1, and
    every case where they differ, with the human verdict."""
    by_a = {r.case_id: r for r in a if r.verdict != "judge_error"}
    by_b = {r.case_id: r for r in b if r.verdict != "judge_error"}
    common = sorted(by_a.keys() & by_b.keys())
    pairs = [(by_a[c].verdict == "fail", by_b[c].verdict == "fail") for c in common]
    m = Confusion.from_pairs(pairs)
    kappa, ac1 = cohen_kappa(m), gwet_ac1(m)

    def k(sample: Sequence[Pair]) -> float | None:
        return cohen_kappa(Confusion.from_pairs(sample)).value

    def g(sample: Sequence[Pair]) -> float | None:
        return gwet_ac1(Confusion.from_pairs(sample)).value

    kci, gci = percentile_ci(pairs, k), percentile_ci(pairs, g)
    lines = [
        f"## Judge vs judge: {name_a} vs {name_b}",
        "",
        f"On the {len(common)} cases both judged (judge_error excluded).",
        "",
        "| n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] | "
        f"{name_a} fail rate | {name_b} fail rate |",
        "|---|---|---|---|---|---|",
        f"| {m.n} | {_pct(m.percent_agreement())} | {_f(kappa.value)} [{_f(kci.low)}, "
        f"{_f(kci.high)}] | {_f(ac1.value)} [{_f(gci.low)}, {_f(gci.high)}] | "
        f"{_pct(m.prevalence()[0])} | {_pct(m.prevalence()[1])} |",
        "",
        rf"| {name_a} \ {name_b} | pass | fail |",
        "|---|---|---|",
        f"| **pass** | {m.pass_pass} | {m.pass_fail} |",
        f"| **fail** | {m.fail_pass} | {m.fail_fail} |",
        "",
    ]
    differ = [c for c in common if by_a[c].verdict != by_b[c].verdict]
    if not differ:
        return [*lines, "They agree on every case."]
    lines += [
        f"| case_id | human | {name_a} | {name_b} | {name_b} reason |",
        "|---|---|---|---|---|",
    ]
    for c in differ:
        human = labels[c].verdicts[criterion].value if c in labels else "–"
        lines.append(
            f"| {c} | {human} | {by_a[c].verdict} | {by_b[c].verdict} | {_cell(by_b[c].reason)} |"
        )
    return lines


NON_OPENAI: Final = ("legacy", "known_bug")  # SOURCE_LABELS values for legacy and human cases


def source_split_lines(
    results: Sequence[JudgeResult],
    labels: Mapping[str, Label],
    criterion: Criterion,
    sources: Mapping[str, str],
) -> list[str]:
    """Judge vs human by where the case came from: non-OpenAI (legacy + known bugs) vs
    OpenAI-generated (synthetic), since OpenAI wrote the synthetic cases (Day 5)."""

    def of(wanted: tuple[str, ...]) -> list[JudgeResult]:
        return [r for r in results if sources.get(r.case_id) in wanted]

    groups = {
        "non-OpenAI (legacy + known_bugs)": of(NON_OPENAI),
        "OpenAI-generated (synthetic)": of(("synthetic",)),
    }
    lines = [
        "## By case source",
        "",
        "| Source | n | % agreement | Gwet's AC1 [95% CI] | Cohen's kappa | human fails | "
        "judge fails |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, rs in groups.items():
        a = agreement(rs, labels, criterion)
        m = a.confusion
        lines.append(
            f"| {name} | {m.n} | {_pct(m.percent_agreement())} | {_f(a.ac1.value)} "
            f"[{_f(a.ac1_ci.low)}, {_f(a.ac1_ci.high)}] | {_f(a.kappa.value)} | {m.a_fails} | "
            f"{m.b_fails} |"
        )
    return lines


GAP_AC1: Final = 0.15  # flag a criterion whose AC1 differs by more than this between sources
GAP_AGREEMENT: Final = 0.10  # ... or whose % agreement differs by more than 10 points


def bias_lines(
    entries: Sequence[tuple[Criterion, str, Sequence[JudgeResult], str]],
    labels: Mapping[str, Label],
    sources: Mapping[str, str],
) -> list[str]:
    """Judge vs human per criterion, split by case source, with a flag on large gaps.
    ``entries``: (criterion, prompt version, results, note on coverage)."""
    lines = [
        "| Criterion | Prompt | non-OpenAI n | non-OpenAI % agreement | non-OpenAI AC1 | "
        "synthetic n | synthetic % agreement | synthetic AC1 | Gap (AC1 / pts) | Flag |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for criterion, version, results, note in entries:
        cells = []
        stats = []
        for wanted in (NON_OPENAI, ("synthetic",)):
            rs = [r for r in results if sources.get(r.case_id) in wanted]
            a = agreement(rs, labels, criterion)
            m = a.confusion
            stats.append((a.ac1.value, m.percent_agreement(), m.n))
            cells += [str(m.n), _pct(m.percent_agreement()), _f(a.ac1.value)]
        (ac1_a, agr_a, _), (ac1_b, agr_b, _) = stats
        if None in (ac1_a, ac1_b, agr_a, agr_b):
            gap, flag = "–", "too few cases"
        else:
            assert ac1_a is not None and ac1_b is not None
            assert agr_a is not None and agr_b is not None
            d_ac1, d_agr = ac1_a - ac1_b, agr_a - agr_b
            gap = f"{d_ac1:+.2f} / {100 * d_agr:+.1f}"
            big = abs(d_ac1) > GAP_AC1 or abs(d_agr) > GAP_AGREEMENT
            flag = "**large gap**" if big else "–"
        name = f"{criterion.value}{f' ({note})' if note else ''}"
        lines.append(f"| {name} | {version} | " + " | ".join(cells) + f" | {gap} | {flag} |")
    return lines


def human_fail_lines(
    criteria: Sequence[Criterion], labels: Mapping[str, Label], sources: Mapping[str, str]
) -> list[str]:
    """Human fails / judged labels per criterion, non-OpenAI vs synthetic."""
    out = []
    for c in criteria:
        cells = []
        for wanted in (NON_OPENAI, ("synthetic",)):
            mine = [lab for cid, lab in labels.items() if sources.get(cid) in wanted]
            judged = [lab for lab in mine if lab.verdicts[c].value != "n_a"]
            fails = sum(lab.verdicts[c].value == "fail" for lab in judged)
            cells.append(f"{fails} / {len(judged)}")
        out.append(f"| {c.value} | {cells[0]} | {cells[1]} |")
    return out
