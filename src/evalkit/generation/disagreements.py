"""Router disagreement report: a review aid, never a labeller.

Runs the snapshot's ``agent/router.py`` offline over generated cases and lists every case
whose predicted route differs from ``expected_route``, grouped by generator. Each one is
either a label error or a real agent bug; a human decides. Labels are never changed here.
Cases with ``expected_route: null`` are listed separately with the router's prediction.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from evalkit.schemas import TestCase

GEN_TAG = "gen-"
TYPE_TAG = "type-"


@dataclass(frozen=True)
class RouterCheck:
    case_id: str
    generator: str
    subtype: str
    question: str
    expected: str | None
    predicted: str

    @property
    def agrees(self) -> bool:
        return self.expected == self.predicted


def _tag(case: TestCase, prefix: str) -> str:
    return next((t.removeprefix(prefix) for t in case.tags if t.startswith(prefix)), "?")


def check_routes(cases: Iterable[TestCase], predict: Callable[[str], str]) -> list[RouterCheck]:
    """The router's prediction for every case without history."""
    return [
        RouterCheck(
            case_id=c.id,
            generator=_tag(c, GEN_TAG),
            subtype=_tag(c, TYPE_TAG),
            question=c.question,
            expected=c.expected_route.value if c.expected_route else None,
            predicted=predict(c.question),
        )
        for c in cases
        if not c.history
    ]


def disagreements(checks: Iterable[RouterCheck]) -> dict[str, list[RouterCheck]]:
    """Checked routes the router gets wrong, grouped by generator (in first-seen order)."""
    groups: dict[str, list[RouterCheck]] = {}
    for c in checks:
        if c.expected is not None and not c.agrees:
            groups.setdefault(c.generator, []).append(c)
    return groups


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(checks: list[RouterCheck]) -> str:
    checked = [c for c in checks if c.expected is not None]
    groups = disagreements(checks)
    n_dis = sum(len(v) for v in groups.values())
    lines = [
        f"Snapshot router vs expected_route: **{len(checked) - n_dis}/{len(checked)} agree**, "
        f"{n_dis} disagree.",
        "",
        "| Generator | Checked | Disagree |",
        "|---|---|---|",
    ]
    per_gen: dict[str, int] = {}
    for c in checked:
        per_gen[c.generator] = per_gen.get(c.generator, 0) + 1
    for gen, n in per_gen.items():
        lines.append(f"| {gen} | {n} | {len(groups.get(gen, []))} |")
    for gen, rows in groups.items():
        lines += [
            "",
            f"#### {gen} ({len(rows)})",
            "",
            "| Case | Type | Expected | Router | Question |",
        ]
        lines.append("|---|---|---|---|---|")
        lines += [
            f"| `{r.case_id}` | {r.subtype} | {r.expected} | {r.predicted} | {_cell(r.question)} |"
            for r in rows
        ]
    unchecked = [c for c in checks if c.expected is None]
    if unchecked:
        lines += ["", f"#### Route not checked ({len(unchecked)}): router prediction only", ""]
        lines += ["| Case | Type | Router | Question |", "|---|---|---|---|"]
        lines += [
            f"| `{r.case_id}` | {r.subtype} | {r.predicted} | {_cell(r.question)} |"
            for r in unchecked
        ]
    return "\n".join(lines) + "\n"
