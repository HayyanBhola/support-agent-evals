"""Plan agent runs under a free-tier LLM budget (Day 25), without calling anything.

The GlowCare agent calls its LLM (Gemini) at most once per message, and only on the knowledge
route: routing is plain rules and the embeddings are a local model. So the calls a run will
make can be predicted offline: run the agent's router (its rules file, loaded read-only) on
every case, and count the cases it sends to knowledge. A knowledge case whose earlier reply
answered without the LLM (no relevant chunks) is counted as 0; any other knowledge case as 1.
Each run adds one warm-up call. A 503 can add at most one retry per call inside the agent.

``plan_days`` packs the runs, in order, into days that stay under the daily budget.
"""

import importlib.util
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from evalkit.schemas import CaseResult, TestCase

WARMUP_CALLS: Final = 1


def load_router(path: Path) -> ModuleType:
    """The agent's router module from its file (no bytecode is written next to it)."""
    spec = importlib.util.spec_from_file_location(f"agent_router_{abs(hash(path))}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load a router from {path}")
    module = importlib.util.module_from_spec(spec)
    before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = before
    return module


def load_products(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["products"] if isinstance(data, dict) else data
    return [{"product_id": p["product_id"], "name": p.get("name") or p["product_name"]}
            for p in rows]  # fmt: skip


def subset_ids(
    cases: Sequence[TestCase], split: Mapping[str, Sequence[str]], name: str
) -> list[str]:
    """``dev`` (the split's dev ids) or ``holdout`` (every case in neither dev nor test)."""
    if name == "dev":
        return sorted(split["dev"])
    if name == "holdout":
        used = set(split["dev"]) | set(split["test"])
        return sorted(c.id for c in cases if c.id not in used)
    raise ValueError(f"unknown subset {name!r}: dev or holdout")


def predict_routes(router: ModuleType, products: list[dict[str, Any]],
                   cases: Iterable[TestCase]) -> dict[str, str]:  # fmt: skip
    """The route the router gives each case's question (history cases are left out: the
    runner skips them)."""
    return {c.id: router.route_message(c.question, products).route for c in cases if not c.history}


def llm_calls(predicted: Mapping[str, str], earlier: Mapping[str, CaseResult]) -> dict[str, int]:
    """Predicted LLM calls per case: 1 for knowledge, unless the earlier run's knowledge
    reply answered without the LLM (no relevant chunks, so the same question skips it)."""
    out = {}
    for cid, route in predicted.items():
        resp = earlier[cid].response if cid in earlier else None
        fallback = (
            resp is not None and isinstance(resp.details, dict) and resp.details.get("llm_error")
        )
        known_no_llm = (resp is not None and resp.route is not None
                        and resp.route.value == "knowledge" and resp.prompt_tokens is None
                        and not fallback)  # fmt: skip
        out[cid] = int(route == "knowledge" and not known_no_llm)
    return out


@dataclass(frozen=True)
class PlannedRun:
    name: str
    cases: int
    calls: int  # predicted LLM calls, warm-up included
    routes: Counter[str]

    def minutes(self, max_rpm: float) -> float:
        return (self.cases + WARMUP_CALLS + 1) / max_rpm  # + the preflight probe


def plan_days(runs: Sequence[PlannedRun], daily_budget: int) -> list[list[PlannedRun]]:
    """Runs in order, packed into days whose predicted calls stay within the budget (a run
    bigger than a day gets a day of its own and must be split with --resume)."""
    days: list[list[PlannedRun]] = [[]]
    for run in runs:
        if days[-1] and sum(r.calls for r in days[-1]) + run.calls > daily_budget:
            days.append([])
        days[-1].append(run)
    return days
