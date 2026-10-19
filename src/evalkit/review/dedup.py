"""Near-duplicate removal for synthetic cases (Day 6), lightweight: rapidfuzz, no embeddings.

A synthetic case is dropped when its normalised question has ``token_sort_ratio`` >=
THRESHOLD against a case that asserts the same thing (same ``expected_route`` and same
``gold_db_facts``) and is kept: any legacy or known-bug case (never dropped), or an earlier
synthetic case in file order.

Why not ``token_set_ratio``: it scores 100 whenever one question's words are a subset of
the other's ("Hydra Balance Moisturizer" vs any question naming it). Why the label gate:
"order GC10241" and "order GC10242" are near-identical text but test different facts.
Near-miss greeting controls are exempt: their greeting opener is what they test.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from rapidfuzz import fuzz

from evalkit.hashing import canonical_json
from evalkit.schemas import TestCase

THRESHOLD: Final = 80.0
EXEMPT_TAGS: Final = frozenset({"near_miss_control"})


def normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def label_key(case: TestCase) -> str:
    """What a case asserts, for "same test?" purposes."""
    route = case.expected_route.value if case.expected_route else None
    return canonical_json([route, case.gold_db_facts])


@dataclass(frozen=True)
class Drop:
    case: TestCase
    match: TestCase  # the kept case it duplicates
    score: float


def find_duplicates(
    synthetic: Sequence[TestCase], protected: Iterable[TestCase], threshold: float = THRESHOLD
) -> tuple[list[TestCase], list[Drop]]:
    """Split ``synthetic`` into kept cases and drops (both in input order)."""
    kept_by_label: dict[str, list[tuple[str, TestCase]]] = {}
    for case in protected:
        kept_by_label.setdefault(label_key(case), []).append((normalize(case.question), case))
    kept: list[TestCase] = []
    drops: list[Drop] = []
    for case in synthetic:
        norm = normalize(case.question)
        bucket = kept_by_label.setdefault(label_key(case), [])
        best: tuple[float, TestCase] | None = None
        if not EXEMPT_TAGS & set(case.tags):
            for other_norm, other in bucket:
                score = fuzz.token_sort_ratio(norm, other_norm)
                if score >= threshold and (best is None or score > best[0]):
                    best = (score, other)
        if best is None:
            kept.append(case)
            bucket.append((norm, case))
        else:
            drops.append(Drop(case, best[1], round(best[0], 1)))
    return kept, drops


def _tag(case: TestCase, prefix: str) -> str:
    return next((t.removeprefix(prefix) for t in case.tags if t.startswith(prefix)), "?")


def _route(case: TestCase) -> str:
    return case.expected_route.value if case.expected_route else "(not checked)"


def render_report(kept: Sequence[TestCase], drops: Sequence[Drop], targets: dict[str, int]) -> str:
    """Markdown: every drop with its match, drops per generator / route, routes vs targets."""
    lines = [
        f"Threshold: token_sort_ratio >= {THRESHOLD:g} against a kept case with the same "
        f"expected_route and gold_db_facts. Kept {len(kept)}, dropped {len(drops)}.",
        "",
        "| Dropped case | Generator | Route | Score | Duplicate of | Dropped question "
        "| Kept question |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| `{d.case.id}` | {_tag(d.case, 'gen-')} | {_route(d.case)} | {d.score:g} | "
        f"`{d.match.id}` | {d.case.question} | {d.match.question} |"
        for d in drops
    ]
    per_route_kept: dict[str, int] = {}
    for c in kept:
        per_route_kept[_route(c)] = per_route_kept.get(_route(c), 0) + 1
    per_route_drop: dict[str, int] = {}
    for d in drops:
        per_route_drop[_route(d.case)] = per_route_drop.get(_route(d.case), 0) + 1
    lines += ["", "| Route | Target | Dropped | Kept | vs target |", "|---|---|---|---|---|"]
    for route in [*targets, "(not checked)"]:
        n = per_route_kept.get(route, 0)
        target = targets.get(route)
        diff = "-" if target is None else ("ok" if n >= target else f"short by {target - n}")
        lines.append(
            f"| {route} | {'-' if target is None else target} | "
            f"{per_route_drop.get(route, 0)} | {n} | {diff} |"
        )
    return "\n".join(lines) + "\n"
