"""Judging the synthetic negatives with the FROZEN judges, and their report (Day 18).

Each record is joined with its dev source (case + saved response) and its ``reply`` swapped in;
the judge input is built exactly as for a real case (the frozen prompt version's fields), so a
control's input equals the original dev input and reuses its cached verdict. Results are
scored per criterion and per edit type: **catch rate** (fails / negatives, Wilson 95% CI) and
**false-alarm rate** (fails / controls), with every missed negative listed.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from evalkit.judges.inputs import input_sha256, judge_input
from evalkit.judges.runner import Item, JudgeCache, JudgeConfig, Plan, cache_key
from evalkit.judges.schema import JudgeResult
from evalkit.schemas import AgentResponse, Criterion, LabelSample
from evalkit.synthetic.negatives import SyntheticRecord

Z95: Final = 1.959963984540054


def edited_response(record: SyntheticRecord, sample: LabelSample) -> AgentResponse:
    return sample.response.model_copy(update={"reply": record.reply})


def build_items(
    cfg: JudgeConfig,
    records: Sequence[SyntheticRecord],
    samples: Mapping[str, LabelSample],
    cache: JudgeCache,
) -> Plan:
    """A judge plan over the records of ``cfg.criterion`` (item ids are the record ids)."""
    items, cached, est = [], 0, 0
    for r in sorted(records, key=lambda r: r.id):
        if r.criterion is not cfg.criterion:
            continue
        sample = samples[r.source_case_id]
        data = judge_input(cfg.criterion, sample.case, edited_response(r, sample), cfg.version)
        h = input_sha256(data)
        item = Item(r.id, data, h, cache_key(cfg, h))
        items.append(item)
        if cache.get(item.key) is not None:
            cached += 1
        else:
            est += (len(cfg.prompt) + len(str(data))) // 4 + 150
    return Plan(items, [], [], cached, est)


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """The Wilson score interval for k successes out of n (None when n is 0)."""
    if n == 0:
        return None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class Rate:
    k: int
    n: int

    @property
    def value(self) -> float | None:
        return self.k / self.n if self.n else None

    def cell(self) -> str:
        if not self.n:
            return "–"
        ci = wilson(self.k, self.n)
        assert ci is not None
        share = f"{self.k}/{self.n} ({100 * self.k / self.n:.0f}%)"
        return f"{share} [{100 * ci[0]:.0f}, {100 * ci[1]:.0f}]"


def _cell(text: str | None) -> str:
    return (text or "–").replace("|", "\\|").replace("\n", " ").strip() or "–"


def report(
    records: Sequence[SyntheticRecord],
    results: Mapping[str, JudgeResult],
    versions: Mapping[Criterion, str],
    *,
    model: str,
) -> str:
    """The Markdown report: catch and false-alarm rates, every miss and false alarm."""
    by_id = {r.id: r for r in records}
    negatives = [r for r in records if r.kind == "negative"]
    controls = [r for r in records if r.kind == "control"]

    def caught(rs: Sequence[SyntheticRecord]) -> Rate:
        judged = [r for r in rs if r.id in results and results[r.id].verdict != "judge_error"]
        return Rate(sum(results[r.id].verdict == "fail" for r in judged), len(judged))

    errors = [i for i, res in results.items() if res.verdict == "judge_error"]
    lines = [
        "# Synthetic negatives v1: the frozen judges' catch rate and false alarms",
        "",
        f"- **Judge:** `{model}`, the **primary judge** (OpenAI; Qwen was dropped on Day 18 for "
        "free-tier rate limits, not quality), frozen prompts "
        f"({', '.join(f'{c.value} {v}' for c, v in versions.items())}). OpenAI also "
        "generated the synthetic test cases (Day 5).",
        f"- **Data:** `datasets/synthetic_negatives_v1.jsonl`: {len(negatives)} negatives "
        f"(template edits of dev replies hayyan labeled pass) and {len(controls)} controls (the "
        "same replies unedited). Synthetic, dev-only, never mixed with human labels.",
        f"- **judge_error:** {len(errors)}.",
        "- **Catch rate** = negatives the judge fails; **false-alarm rate** = controls the judge "
        "fails. Wilson 95% CIs in brackets (small n: wide).",
        "- **Template edits are easier than real failures**, so catch rates are an upper bound.",
        "",
        "## Per criterion",
        "",
        "| Criterion | Catch rate [95% CI] | False alarms on controls [95% CI] |",
        "|---|---|---|",
    ]
    for c in versions:
        neg = [r for r in negatives if r.criterion is c]
        ctl = [r for r in controls if r.criterion is c]
        if neg or ctl:
            lines.append(f"| {c.value} | {caught(neg).cell()} | {caught(ctl).cell()} |")
    lines += ["", "## Per edit type", "", "| Criterion | Edit type | Catch rate [95% CI] |",
              "|---|---|---|"]  # fmt: skip
    for c in versions:
        types = sorted({r.edit_type for r in negatives if r.criterion is c})
        for t in types:
            rs = [r for r in negatives if r.criterion is c and r.edit_type == t]
            lines.append(f"| {c.value} | {t} | {caught(rs).cell()} |")
    missed = [r for r in negatives if r.id in results and results[r.id].verdict == "pass"]
    lines += ["", f"## Missed negatives ({len(missed)})", ""]
    if missed:
        lines += ["| id | source case | edit | judge reason |", "|---|---|---|---|"]
        lines += [f"| {r.id} | {r.source_case_id} | {_cell(r.edit)} | "
                  f"{_cell(results[r.id].reason)} |" for r in missed]  # fmt: skip
    else:
        lines.append("None.")
    alarms = [r for r in controls if r.id in results and results[r.id].verdict == "fail"]
    lines += ["", f"## False alarms on controls ({len(alarms)})", ""]
    if alarms:
        lines += ["| id | source case | judge reason |", "|---|---|---|"]
        lines += [f"| {r.id} | {r.source_case_id} | {_cell(results[r.id].reason)} |"
                  for r in alarms]  # fmt: skip
    else:
        lines.append("None.")
    if errors:
        lines += ["", "## Judge errors", ""]
        lines += [f"- {i} ({by_id[i].criterion.value}): {_cell(results[i].error)}" for i in errors]
    return "\n".join(lines) + "\n"
