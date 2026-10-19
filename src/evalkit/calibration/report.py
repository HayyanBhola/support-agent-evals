"""The human agreement report (Day 12): ``evalkit agreement report --a A --b B``.

Reads raw label files (every ``datasets/labels/labels_raw*.jsonl`` by default), takes each
labeler's latest label per case (by timestamp, across files), compares the cases both
labeled, and writes Markdown: a header (labels files, matched cases, the set and its fail
enrichment, both labelers' rubric_hash, response checks, labeling sessions), a per-criterion
table with flags, the confusion matrices, and every disagreement with both notes.

The same report serves a second labeler (``--b <name>``) and a blind self-relabel
(``--b hayyan-r2``; then a gap of under SELF_GAP_DAYS between the sessions is warned about).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from evalkit.calibration.agreement import (
    MIN_FAIL_PAIRS,
    THRESHOLD,
    CriterionAgreement,
    agreement,
    match,
)
from evalkit.io import read_jsonl
from evalkit.manifest import MANIFEST_NAME, read_manifest
from evalkit.schemas import Criterion, Label, LabelSample
from evalkit.stats.bootstrap import RESAMPLES, SEED, Interval

DEFAULT_OUT: Final = Path("reports") / "human_agreement.md"
SELF_OUT: Final = Path("reports") / "self_consistency.md"
RAW_GLOB: Final = "labels_raw*.jsonl"
SELF_GAP_DAYS: Final = 3
AC1_NOTE: Final = (
    "AC1 is reported next to kappa because fails are rare here: when one verdict dominates, "
    "kappa can be low or even negative despite high agreement (the kappa paradox), while "
    "AC1 stays close to the observed agreement."
)


def latest_labels(files: Iterable[Path]) -> dict[str, dict[str, Label]]:
    """labeler -> case_id -> that labeler's latest label (by timestamp) across ``files``."""
    out: dict[str, dict[str, Label]] = {}
    for path in files:
        for label in read_jsonl(path, Label):
            mine = out.setdefault(label.labeler, {})
            prev = mine.get(label.case_id)
            if prev is None or label.timestamp >= prev.timestamp:
                mine[label.case_id] = label
    return out


@dataclass(frozen=True)
class SetInfo:
    name: str
    size: int
    enriched: int | None  # records picked because the first labeler gave a fail


def identify_set(
    case_ids: set[str], datasets_dir: Path, sample: Path | None = None
) -> SetInfo | None:
    """The set the compared cases come from: ``sample`` if given, else the smallest
    label-sample file in the manifest that contains all of them."""
    base = sample.parent if sample else datasets_dir
    manifest_path = base / MANIFEST_NAME
    if not manifest_path.exists():
        return None
    entries = read_manifest(manifest_path).datasets
    names = [sample.name] if sample else sorted(entries)
    best: SetInfo | None = None
    for name in names:
        entry = entries.get(name)
        path = base / name
        if entry is None or entry.kind != "label_samples" or not path.exists():
            continue
        ids = {s.case_id for s in read_jsonl(path, LabelSample)}
        if case_ids <= ids and (best is None or len(ids) < best.size):
            best = SetInfo(name, len(ids), entry.metadata.get("enriched"))
    return best


def _f(x: float | None, digits: int = 2) -> str:
    return "–" if x is None else f"{x:.{digits}f}"


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def _ci(ci: Interval) -> str:
    if not ci.defined:
        return ""
    note = "" if ci.used == ci.resamples else f", {ci.used}/{ci.resamples} resamples"
    return f" [{_f(ci.low)}, {_f(ci.high)}{note}]"


def _cell(text: str | None) -> str:
    return (text or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip() or "–"


def _hashes(labels: Iterable[Label]) -> list[str]:
    return sorted({lab.rubric_hash for lab in labels})


def _span(labels: Sequence[Label]) -> tuple[datetime, datetime]:
    times = [lab.timestamp for lab in labels]
    return min(times), max(times)


def header_lines(
    a_name: str,
    b_name: str,
    pairs: Sequence[tuple[Label, Label]],
    *,
    a_total: int,
    b_total: int,
    sources: Sequence[str],
    set_info: SetInfo | None,
) -> list[str]:
    a_labels, b_labels = [a for a, _ in pairs], [b for _, b in pairs]
    lines = [
        f"- **Labels:** {', '.join(f'`{s}`' for s in sources)}",
        f"- **Matched cases:** {len(pairs)} (labeled by {a_name}: {a_total}, by {b_name}: "
        f"{b_total}; only cases both labeled are compared)",
    ]
    if set_info is None:
        lines.append("- **Set:** not identified, so its fail enrichment is unknown.")
    elif set_info.enriched:
        lines.append(
            f"- **Set:** `{set_info.name}` ({set_info.size} records), **enriched for fails**: "
            f"{set_info.enriched} of {set_info.size} records were picked because {a_name} gave "
            "at least one fail. The agreement figures and fail prevalences describe this test "
            "set, not population fail rates."
        )
    else:
        lines.append(f"- **Set:** `{set_info.name}` ({set_info.size} records), not enriched.")
    ha, hb = _hashes(a_labels), _hashes(b_labels)
    lines.append(
        f"- **rubric_hash:** {a_name} {', '.join(f'`{h[:12]}`' for h in ha)}; "
        f"{b_name} {', '.join(f'`{h[:12]}`' for h in hb)}"
    )
    if ha != hb:
        lines.append(
            "- ⚠ **The labelers used different rubric versions.** Disagreements may come from "
            "the rubric change, not the labelers."
        )
    changed = [a.case_id for a, b in pairs if a.response_sha256 != b.response_sha256]
    if changed:
        lines.append(
            f"- ⚠ **{len(changed)} case(s) were labeled on different responses** "
            f"(response_sha256 differs): {', '.join(changed)}"
        )
    else:
        lines.append("- **Responses:** every matched case was labeled on the same response.")
    a_start, a_end = _span(a_labels)
    b_start, b_end = _span(b_labels)
    gap = (b_start - a_end).total_seconds() / 86400
    lines.append(
        f"- **Sessions:** {a_name} {a_start:%Y-%m-%d} to {a_end:%Y-%m-%d}; {b_name} "
        f"{b_start:%Y-%m-%d} to {b_end:%Y-%m-%d} ({b_name} started {gap:.1f} days after "
        f"{a_name}'s last matched label)"
    )
    if b_name.startswith(f"{a_name}-") and gap < SELF_GAP_DAYS:
        lines.append(
            f"- ⚠ **Self-relabel less than {SELF_GAP_DAYS} days after the first pass**: "
            "memory may inflate self-consistency."
        )
    return lines


def summary_table(results: Sequence[CriterionAgreement]) -> list[str]:
    lines = [
        "| Criterion | Both judged | Applicability agreement | % agreement | Cohen's kappa "
        "[95% CI] | Gwet's AC1 [95% CI] | Fail prevalence A / B | Flags |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        m = r.confusion
        pa, pb = m.prevalence()
        kappa = _f(r.kappa.value) + _ci(r.kappa_ci) if r.kappa.value is not None else "undefined"
        flags = "; ".join(r.flags()) or "–"
        lines.append(
            f"| {r.criterion.value} | {m.n} | {_pct(r.applicability.agreement())} | "
            f"{_pct(m.percent_agreement())} | {kappa} | {_f(r.ac1.value)}{_ci(r.ac1_ci)} | "
            f"{_pct(pa)} / {_pct(pb)} | {flags} |"
        )
    return lines


def detail_lines(results: Sequence[CriterionAgreement], a_name: str, b_name: str) -> list[str]:
    lines: list[str] = []
    for r in results:
        m, ap = r.confusion, r.applicability
        lines += [
            f"### {r.criterion.value}",
            "",
            f"| {a_name} \\ {b_name} | pass | fail |",
            "|---|---|---|",
            f"| **pass** | {m.pass_pass} | {m.pass_fail} |",
            f"| **fail** | {m.fail_pass} | {m.fail_fail} |",
            "",
            f"Applicability: both n_a {ap.both_na}, both judged {ap.both_judged}, "
            f"{a_name} n_a / {b_name} judged {ap.a_na_b_judged}, "
            f"{a_name} judged / {b_name} n_a {ap.a_judged_b_na}.",
        ]
        if r.kappa.reason:
            lines.append(f"Kappa: {r.kappa.reason}.")
        lines.append("")
    return lines


def disagreement_lines(
    pairs: Sequence[tuple[Label, Label]], order: Sequence[Criterion], a_name: str, b_name: str
) -> list[str]:
    rows = [(c, a, b) for c in order for a, b in pairs if a.verdicts[c] is not b.verdicts[c]]
    if not rows:
        return ["No disagreements."]
    lines = [
        f"| case_id | criterion | {a_name} | {b_name} | {a_name} note | {b_name} note |",
        "|---|---|---|---|---|---|",
    ]
    for c, a, b in rows:
        lines.append(
            f"| {a.case_id} | {c.value} | {a.verdicts[c].value} | {b.verdicts[c].value} | "
            f"{_cell(a.notes)} | {_cell(b.notes)} |"
        )
    return lines


def build_report(
    a_name: str,
    b_name: str,
    labels: Mapping[str, Mapping[str, Label]],
    *,
    order: Sequence[Criterion] = tuple(Criterion),
    sources: Sequence[str] = (),
    datasets_dir: Path = Path("datasets"),
    sample: Path | None = None,
    resamples: int = RESAMPLES,
    seed: int = SEED,
    title: str | None = None,
    note: str | None = None,
) -> tuple[str, list[CriterionAgreement]]:
    """The Markdown report and the per-criterion results; ValueError if nothing matches."""
    for name in (a_name, b_name):
        if name not in labels:
            known = ", ".join(sorted(labels)) or "none"
            raise ValueError(f"no labels by {name!r} (labelers found: {known})")
    a, b = labels[a_name], labels[b_name]
    pairs = match(a, b)
    if not pairs:
        raise ValueError(f"{a_name!r} and {b_name!r} have no case in common")
    results = agreement(a, b, order, resamples=resamples, seed=seed)
    set_info = identify_set({x.case_id for x, _ in pairs}, datasets_dir, sample)
    flagged = [r.criterion.value for r in results if r.flags()]
    lines = [
        f"# {title or 'Human agreement'}: {a_name} vs {b_name}",
        "",
        *([f"> {note}", ""] if note else []),
        *header_lines(
            a_name,
            b_name,
            pairs,
            a_total=len(a),
            b_total=len(b),
            sources=sources,
            set_info=set_info,
        ),  # fmt: skip
        f"- **Method:** pairs where both judged (pass/fail); 95% percentile bootstrap CIs over "
        f"those pairs ({resamples} resamples, seed {seed}). Flag: kappa (AC1 when kappa is "
        f"undefined) below {THRESHOLD}, or fewer than {MIN_FAIL_PAIRS} pairs with a fail.",
        "",
        "## Per criterion",
        "",
        *summary_table(results),
        "",
        AC1_NOTE,
        "",
        f"Flagged: {', '.join(flagged) if flagged else 'none'}.",
        "",
        "## Confusion matrices (pairs where both judged)",
        "",
        *detail_lines(results, a_name, b_name),
        "## Disagreements",
        "",
        "Every case and criterion where the verdicts differ (n_a vs pass/fail included). Each "
        "is discussed; where one shows an unclear rule, the rubric is clarified.",
        "",
        *disagreement_lines(pairs, order, a_name, b_name),
        "",
    ]
    return "\n".join(lines), results
