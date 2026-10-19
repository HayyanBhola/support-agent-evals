"""Before/after report of an agent fix (Day 25): ``evalkit agent-fix-report``.

Deterministic metrics only (no judges): routing accuracy and escalation (precision, recall,
match) for runs of the agent before and after a change, per subset (dev, and the holdout
outside dev/test). Each subset is reported apart, with 95% percentile bootstrap CIs and
paired differences (after - before, resampling cases). Also: the regressions (cases right
before and wrong after), run-to-run noise from two runs of the unchanged agent, and the
provenance (agent commits, evalkit commit, run settings, LLM calls).
"""

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from evalkit.metrics.routing import ESCALATION_MATCH, ROUTE_MATCH, EscalationSummary
from evalkit.metrics.scorecard import rate, score_run
from evalkit.runner import used_llm
from evalkit.schemas import CaseResult, MetricResult, RunMeta, TestCase
from evalkit.stats.bootstrap import RESAMPLES, SEED, Interval, quantile

REPORT_FILE: Final = "agent_fix_v1.md"


@dataclass(frozen=True)
class RunData:
    run_id: str
    meta: RunMeta
    results: list[CaseResult]
    commit_note: str = ""  # shown when meta.agent_repo has no commit (e.g. the Day 7 run)
    mode: str = "live"  # live | offline (routing rules only) | partial

    def only(self, ids: set[str]) -> "RunData":
        """The same run restricted to these case ids."""
        kept = [r for r in self.results if r.case_id in ids]
        return RunData(self.run_id, self.meta, kept, self.commit_note, self.mode)

    def rows(self, cases: Mapping[str, TestCase]) -> list[MetricResult]:
        return score_run(self.results, cases)

    @property
    def llm_calls(self) -> int:
        return sum(used_llm(r.response) for r in self.results)

    @property
    def agent_commit(self) -> str:
        commit = self.meta.agent_repo.get("commit")
        return str(commit)[:7] if commit else (self.commit_note or "unknown")


def offline_run(run_id: str, routes: Mapping[str, str], template: RunMeta,
                commit: str) -> RunData:  # fmt: skip
    """A run made from the router's rules alone (no agent call, no LLM): each case gets its
    predicted route and escalates exactly when the route is escalate. Exact for routing and
    escalation once "not found" no longer escalates (the fixed agent); replies are empty."""
    t = template.started_at

    def one(cid: str, route: str) -> CaseResult:
        response = {"ok": True, "reply": "(offline: routing rules only)", "route": route,
                    "escalated": route == "escalate"}  # fmt: skip
        return CaseResult.model_validate({"run_id": run_id, "case_id": cid, "attempts": 0,
                                          "cache_hit": False, "started_at": t,
                                          "finished_at": t, "response": response})  # fmt: skip

    results = [one(cid, route) for cid, route in sorted(routes.items())]
    repo = {"commit": commit, "branch": "eval-fixes", "dirty_code": False,
            "mode": "offline rules"}  # fmt: skip
    meta = template.model_copy(update={"run_id": run_id, "agent_repo": repo})
    return RunData(run_id, meta, results, mode="offline")


def agreement(live: RunData, offline: RunData) -> dict[str, Any]:
    """Live replies vs offline predictions on the cases the live run answered."""
    off = {r.case_id: r.response for r in offline.results}
    pairs = [(r.case_id, r.response, off[r.case_id]) for r in live.results
             if r.response is not None and r.case_id in off]  # fmt: skip
    route = [c for c, a, b in pairs if a.route != b.route]  # type: ignore[union-attr]
    esc = [c for c, a, b in pairs if a.escalated != b.escalated]  # type: ignore[union-attr]
    return {"n": len(pairs), "route_differs": route, "escalation_differs": esc}


def _by(rows: Sequence[MetricResult], metric: str) -> dict[str, MetricResult]:
    return {r.case_id: r for r in rows if r.metric == metric}


def paired_ci(
    ids: Sequence[str],
    stat: Callable[[Sequence[str]], float | None],
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> Interval:
    """Percentile bootstrap over case ids of a statistic computed on both runs' rows."""
    if not ids:
        return Interval(None, None, 0, resamples)
    rng = random.Random(seed)
    values = []
    for _ in range(resamples):
        sample = [ids[rng.randrange(len(ids))] for _ in ids]
        v = stat(sample)
        if v is not None:
            values.append(v)
    values.sort()
    if not values:
        return Interval(None, None, 0, resamples)
    return Interval(quantile(values, 0.025), quantile(values, 0.975), len(values), resamples)


def _esc(rows: Mapping[str, MetricResult], ids: Sequence[str]) -> EscalationSummary:
    return EscalationSummary.from_rows([rows[i] for i in ids if i in rows])


@dataclass(frozen=True)
class Comparison:
    """One subset: before vs after on the cases both runs scored."""

    name: str
    before: RunData
    after: RunData
    cases: Mapping[str, TestCase]
    resamples: int = RESAMPLES
    seed: int = SEED

    def _rows(self) -> tuple[list[MetricResult], list[MetricResult]]:
        return self.before.rows(self.cases), self.after.rows(self.cases)

    def metric_lines(self) -> list[str]:
        rb, ra = self._rows()
        kw = {"resamples": self.resamples, "seed": self.seed}
        lines = ["| Metric | Before [95% CI] | After [95% CI] | After - before [95% CI, paired] |",
                 "|---|---|---|---|"]  # fmt: skip
        for metric, title in ((ROUTE_MATCH, "Routing accuracy"),
                              (ESCALATION_MATCH, "Escalation decision correct")):  # fmt: skip
            b, a = _by(rb, metric), _by(ra, metric)
            ids = sorted(b.keys() & a.keys())
            rbef = rate([b[i] for i in ids], **kw)
            raft = rate([a[i] for i in ids], **kw)

            def diff(sample: Sequence[str], b: Any = b, a: Any = a) -> float | None:
                return sum(bool(a[i].passed) - bool(b[i].passed) for i in sample) / len(sample)

            d = diff(ids) if ids else None
            lines.append(f"| {title} (n={len(ids)}) | {_rate(rbef.passed, rbef.n, rbef.ci)} | "
                         f"{_rate(raft.passed, raft.n, raft.ci)} | "
                         f"{_pts(d, paired_ci(ids, diff, **kw))} |")  # fmt: skip
        b, a = _by(rb, ESCALATION_MATCH), _by(ra, ESCALATION_MATCH)
        ids = sorted(b.keys() & a.keys())
        stats: list[tuple[str, Callable[[EscalationSummary], float | None]]] = [
            ("Escalation precision", lambda s: s.precision),
            ("Escalation recall", lambda s: s.recall),
        ]
        for title, f in stats:

            def one(
                rows: Mapping[str, MetricResult], f: Any = f
            ) -> Callable[[Sequence[str]], float | None]:
                return lambda sample: f(_esc(rows, sample))

            def delta(sample: Sequence[str], f: Any = f) -> float | None:
                x, y = f(_esc(b, sample)), f(_esc(a, sample))
                return None if x is None or y is None else y - x

            sb, sa = _esc(b, ids), _esc(a, ids)
            lines.append(
                f"| {title} | {_frac(f(sb), sb, title)} {_ci(paired_ci(ids, one(b), **kw))} | "
                f"{_frac(f(sa), sa, title)} {_ci(paired_ci(ids, one(a), **kw))} | "
                f"{_pts(delta(ids), paired_ci(ids, delta, **kw))} |"
            )
        sb, sa = _esc(b, ids), _esc(a, ids)
        lines += ["", f"Escalation counts: before TP {sb.tp} / FP {sb.fp} / FN {sb.fn} / TN "
                  f"{sb.tn}; after TP {sa.tp} / FP {sa.fp} / FN {sa.fn} / TN {sa.tn}."]  # fmt: skip
        return lines

    def route_lines(self) -> list[str]:
        rb, ra = self._rows()
        b, a = _by(rb, ROUTE_MATCH), _by(ra, ROUTE_MATCH)
        groups: dict[str, list[str]] = {}
        for cid in sorted(b.keys() & a.keys()):
            route = self.cases[cid].expected_route
            groups.setdefault(route.value if route else "(none)", []).append(cid)
        lines = ["| Expected route | n | Before | After |", "|---|---|---|---|"]
        for g, ids in sorted(groups.items()):
            pb, pa = sum(bool(b[i].passed) for i in ids), sum(bool(a[i].passed) for i in ids)
            lines.append(f"| {g} | {len(ids)} | {pb}/{len(ids)} | {pa}/{len(ids)} |")
        return lines

    def regressions(self) -> list[tuple[str, str, str, str]]:
        """(case_id, metric, before detail, after detail) for cases right before, wrong after."""
        rb, ra = self._rows()
        out = []
        for metric in (ROUTE_MATCH, ESCALATION_MATCH):
            b, a = _by(rb, metric), _by(ra, metric)
            for cid in sorted(b.keys() & a.keys()):
                if b[cid].passed and not a[cid].passed:
                    out.append((cid, metric, _detail(b[cid]), _detail(a[cid])))
        return out

    def metrics(self) -> dict[str, float]:
        """Flat numbers for MLflow, prefixed with the subset name."""
        rb, ra = self._rows()
        out: dict[str, float] = {}
        for tag, rows in (("before", rb), ("after", ra)):
            for metric in (ROUTE_MATCH, ESCALATION_MATCH):
                r = rate(list(_by(rows, metric).values()), resamples=self.resamples,
                         seed=self.seed)  # fmt: skip
                if r.rate is not None:
                    out[f"{self.name}.{tag}.{metric}"] = r.rate
            s = _esc(_by(rows, ESCALATION_MATCH), sorted(_by(rows, ESCALATION_MATCH)))
            for k, v in (("precision", s.precision), ("recall", s.recall), ("f1", s.f1)):
                if v is not None:
                    out[f"{self.name}.{tag}.escalation_{k}"] = v
            out |= {
                f"{self.name}.{tag}.escalation_fp": s.fp,
                f"{self.name}.{tag}.escalation_fn": s.fn,
            }
        out[f"{self.name}.regressions"] = len(self.regressions())
        out[f"{self.name}.before.llm_calls"] = self.before.llm_calls
        out[f"{self.name}.after.llm_calls"] = self.after.llm_calls
        return out


def _detail(r: MetricResult) -> str:
    d = r.details
    if r.metric == ROUTE_MATCH:
        return f"expected {d.get('expected')}, got {d.get('actual')}"
    return f"expected escalate={d.get('expected')}, got {d.get('actual')}"


def _ci(ci: Interval) -> str:
    if ci.low is None or ci.high is None:
        return "[–]"
    return f"[{100 * ci.low:.1f}, {100 * ci.high:.1f}]"


def _rate(passed: int, n: int, ci: Interval) -> str:
    return f"{passed}/{n} = {100 * passed / n:.1f}% {_ci(ci)}" if n else "–"


def _frac(v: float | None, s: EscalationSummary, title: str) -> str:
    if v is None:
        return "–"
    num, den = (s.tp, s.tp + s.fp) if "precision" in title else (s.tp, s.tp + s.fn)
    return f"{num}/{den} = {100 * v:.1f}%"


def _pts(d: float | None, ci: Interval) -> str:
    if d is None:
        return "–"
    sure = ci.low is not None and ci.high is not None and (ci.low > 0 or ci.high < 0)
    span = ("" if ci.low is None or ci.high is None
            else f" [{100 * ci.low:+.1f}, {100 * ci.high:+.1f}]")  # fmt: skip
    return f"**{100 * d:+.1f} pts**{span}" + (" (CI excludes 0)" if sure else "")


def noise(a: RunData, b: RunData) -> dict[str, Any]:
    """Two runs of the same agent and settings: how many replies differ at all."""
    ra = {r.case_id: r.response for r in a.results}
    rb = {r.case_id: r.response for r in b.results}
    ids = sorted(ra.keys() & rb.keys())

    def differ(field: str) -> list[str]:
        return [i for i in ids if ra[i] is not None and rb[i] is not None
                and getattr(ra[i], field) != getattr(rb[i], field)]  # fmt: skip

    knowledge = [i for i in ids if ra[i] is not None and ra[i].route is not None  # type: ignore[union-attr]
                 and ra[i].route.value == "knowledge"]  # type: ignore[union-attr]  # fmt: skip
    return {"n": len(ids), "route": differ("route"), "escalated": differ("escalated"),
            "answer_type": differ("answer_type"), "reply": differ("reply"),
            "knowledge": len(knowledge)}  # fmt: skip
