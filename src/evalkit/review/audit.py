"""The human audit of Claude's first-pass review: render it as Markdown, parse it back.

Each entry shows the question, the old label, Claude's decision and reason, then:

    MY VERDICT: agree / disagree

The auditor replaces that with ``agree`` or ``disagree``. On ``disagree`` the
``OVERRIDE ...:`` lines say what to do instead: ``decision`` (keep / fix / delete) and
``note`` are required; blank fixed_* lines mean "unchanged". Parsing refuses an entry
whose verdict is still unfilled.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from evalkit.review.workbook import DECISIONS
from evalkit.schemas import TestCase

VERDICT_PROMPT: Final = "agree / disagree"
OVERRIDE_FIELDS: Final = (
    "decision",
    "fixed_expected_route",
    "fixed_should_escalate",
    "fixed_reference_answer",
    "note",
)
_HEADER = re.compile(r"^## \d+\. (\S+)", re.MULTILINE)
_VERDICT = re.compile(r"^MY VERDICT:(.*)$", re.MULTILINE)
_OVERRIDE = re.compile(r"^OVERRIDE (\w+):(.*)$", re.MULTILINE)


class AuditError(Exception):
    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__(
            f"{len(problems)} audit problem(s):\n" + "\n".join(f"- {p}" for p in problems)
        )
        self.problems = list(problems)


def _bool(value: bool | None) -> str:
    return "null" if value is None else str(value).lower()


@dataclass(frozen=True)
class AuditEntry:
    case_id: str
    question: str
    old_label: str
    decision: str
    change: str  # what a fix changes ("" for keep/delete)
    reason: str

    @classmethod
    def from_decision(cls, case: TestCase, row: Mapping[str, str]) -> "AuditEntry":
        route = case.expected_route.value if case.expected_route else "(none)"
        old = (
            f"expected_route={route}, should_escalate={_bool(case.should_escalate)}, "
            f"safety_critical={_bool(case.safety_critical)}"
        )
        if case.reference_answer:
            old += f"; reference_answer: {case.reference_answer}"
        change = "; ".join(
            f"{key}={row[key]}"
            for key in ("fixed_expected_route", "fixed_should_escalate", "fixed_reference_answer")
            if row.get(key)
        )
        return cls(case.id, case.question, old, row["decision"], change, row["note"])


def _one_line(text: str) -> str:
    return " ".join(text.split())


def render_audit(entries: Sequence[AuditEntry], *, seed: int, total: int) -> str:
    n_changed = sum(e.decision != "keep" for e in entries)
    lines = [
        "# Human audit of Claude's first-pass review",
        "",
        f"Claude reviewed all {total} synthetic cases against agent_snapshot/SYSTEM_RULES.md "
        "(never against the router). This file has **every fix and delete "
        f"({n_changed})** and **{len(entries) - n_changed} randomly sampled keeps** (seed "
        f"{seed}): {len(entries)} entries.",
        "",
        "For each entry, replace `agree / disagree` after MY VERDICT with `agree` or "
        "`disagree`. If you disagree, fill the OVERRIDE lines: `decision` (keep / fix / "
        "delete) and `note` (cite the SYSTEM_RULES rule) are required; a blank fixed_* "
        "line means unchanged; fixed_expected_route takes a route or `(none)`, "
        "fixed_should_escalate takes true / false / `(unset)`. apply_review.py refuses to "
        "run while any verdict is unfilled.",
    ]
    for n, e in enumerate(entries, start=1):
        decision = e.decision + (f" ({_one_line(e.change)})" if e.change else "")
        lines += [
            "",
            f"## {n}. {e.case_id} ({e.decision})",
            "",
            f"- **Question:** {_one_line(e.question)}",
            f"- **Old label:** {_one_line(e.old_label)}",
            f"- **Claude's decision:** {decision}",
            f"- **Claude's reason:** {_one_line(e.reason)}",
            "",
            f"MY VERDICT: {VERDICT_PROMPT}",
            *(f"OVERRIDE {name}:" for name in OVERRIDE_FIELDS),
        ]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Verdict:
    case_id: str
    agree: bool
    override: dict[str, str] = field(default_factory=dict)


def parse_audit(text: str) -> dict[str, Verdict]:
    """Verdict per audited case id; raises AuditError listing every problem."""
    problems: list[str] = []
    verdicts: dict[str, Verdict] = {}
    headers = list(_HEADER.finditer(text))
    if not headers:
        raise AuditError(["no audit entries found"])
    for i, h in enumerate(headers):
        cid = h.group(1)
        body = text[h.end() : headers[i + 1].start() if i + 1 < len(headers) else len(text)]
        if cid in verdicts:
            problems.append(f"{cid}: audited twice")
            continue
        m = _VERDICT.search(body)
        value = m.group(1).strip().lower() if m else ""
        if value not in ("agree", "disagree"):
            problems.append(
                f"{cid}: MY VERDICT is not filled in (agree or disagree), got {value!r}"
            )
            continue
        override = {k: v.strip() for k, v in _OVERRIDE.findall(body) if v.strip()}
        unknown = set(override) - set(OVERRIDE_FIELDS)
        if unknown:
            problems.append(f"{cid}: unknown OVERRIDE field(s) {sorted(unknown)}")
            continue
        if value == "agree":
            if override:
                problems.append(f"{cid}: verdict is agree but OVERRIDE lines are filled")
                continue
            verdicts[cid] = Verdict(cid, True)
            continue
        decision = override.get("decision", "").lower()
        if decision not in DECISIONS:
            problems.append(f"{cid}: disagree needs OVERRIDE decision: keep, fix or delete")
            continue
        if not override.get("note"):
            problems.append(f"{cid}: disagree needs an OVERRIDE note citing the rule")
            continue
        verdicts[cid] = Verdict(cid, False, override | {"decision": decision})
    if problems:
        raise AuditError(problems)
    return verdicts
