"""Offline routing evaluation on the fresh v2 test set (Day 26): rules only, no LLM, no agent.

Loads the agent's router at three commits with read-only `git show` (original 33b240d, v1
8f392b5, v2 376183e), routes every message of datasets/fresh_v2.jsonl and reports routing
accuracy (95% percentile bootstrap CIs, 2000 resamples, seed 42), accuracy per route,
escalation precision / recall (escalates = routed to "escalate", with bootstrap CIs), every
miss, and the cases right in v1 but wrong in v2. Writes reports/fresh_v2_eval.md and logs the
MLflow run "fresh-v2-eval".

    uv run python scripts/eval_fresh_routing.py [--no-mlflow]
"""

import json
import random
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

sys.path.insert(0, "src")
from evalkit import budget  # noqa: E402
from evalkit.hashing import file_sha256  # noqa: E402
from evalkit.metrics import tracking  # noqa: E402
from evalkit.stats.bootstrap import quantile  # noqa: E402

AGENT = Path("../rag-whatsapp-agent")
DATASET = Path("datasets/fresh_v2.jsonl")
REPORT = Path("reports/fresh_v2_eval.md")
VERSIONS = {"original": "33b240d", "v1": "8f392b5", "v2": "376183e"}
HOLDOUT_V2 = (218, 228)  # v2 on the holdout, from notes/day26_v2_router.md (optimistic)
HOLDOUT_V1 = (178, 228)  # v1 on the holdout (holdout not read when v1 was written)
RESAMPLES, SEED = 2000, 42
ROUTES = ["greeting", "database", "knowledge", "clarify", "medical", "escalate", "guardrail"]


def router_at(commit: str):  # noqa: ANN201
    blob = subprocess.run(["git", "show", f"{commit}:agent/router.py"], cwd=AGENT,
                          capture_output=True, check=True).stdout  # fmt: skip
    path = Path(tempfile.mkdtemp()) / f"router_{commit}.py"
    path.write_bytes(blob)
    return budget.load_router(path)


def ci(items: Sequence[object], stat: Callable[[Sequence[object]], float | None]) -> tuple:
    rng = random.Random(SEED)
    values = []
    for _ in range(RESAMPLES):
        sample = [items[rng.randrange(len(items))] for _ in items]
        v = stat(sample)
        if v is not None:
            values.append(v)
    values.sort()
    return (quantile(values, 0.025), quantile(values, 0.975)) if values else (None, None)


def acc(flags: Sequence[object]) -> float | None:
    return sum(bool(f) for f in flags) / len(flags) if flags else None


def precision(pairs: Sequence[object]) -> float | None:
    pred = [p for p in pairs if p[1]]  # type: ignore[index]
    return sum(p[0] for p in pred) / len(pred) if pred else None  # type: ignore[index]


def recall(pairs: Sequence[object]) -> float | None:
    pos = [p for p in pairs if p[0]]  # type: ignore[index]
    return sum(p[1] for p in pos) / len(pos) if pos else None  # type: ignore[index]


def pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def span(lo_hi: tuple) -> str:
    lo, hi = lo_hi
    return "[–]" if lo is None else f"[{100 * lo:.1f}, {100 * hi:.1f}]"


def main() -> None:
    rows = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line]
    prods = budget.load_products(Path("agent_snapshot/data/products/products.json"))
    pred = {name: {r["id"]: router_at(c).route_message(r["message"], prods).route for r in rows}
            for name, c in VERSIONS.items()}  # fmt: skip
    res: dict[str, dict] = {}
    for name in VERSIONS:
        flags = [pred[name][r["id"]] == r["expected_route"] for r in rows]
        pairs = [(r["should_escalate"], pred[name][r["id"]] == "escalate") for r in rows]
        tp = sum(s and p for s, p in pairs)
        fp = sum((not s) and p for s, p in pairs)
        fn = sum(s and not p for s, p in pairs)
        res[name] = {
            "correct": sum(flags), "n": len(rows), "acc": acc(flags), "acc_ci": ci(flags, acc),
            "precision": precision(pairs), "precision_ci": ci(pairs, precision),
            "recall": recall(pairs), "recall_ci": ci(pairs, recall), "tp": tp, "fp": fp, "fn": fn,
            "by_route": {rt: (sum(f for f, r in zip(flags, rows, strict=True)
                                  if r["expected_route"] == rt),
                              sum(r["expected_route"] == rt for r in rows)) for rt in ROUTES},
            "misses": [(r["id"], r["message"], r["expected_route"], pred[name][r["id"]])
                       for f, r in zip(flags, rows, strict=True) if not f],
        }  # fmt: skip
    v1_not_v2 = [(r["id"], r["message"], r["expected_route"], pred["v1"][r["id"]],
                  pred["v2"][r["id"]]) for r in rows
                 if pred["v1"][r["id"]] == r["expected_route"]
                 and pred["v2"][r["id"]] != r["expected_route"]]  # fmt: skip
    v2_not_v1 = [r["id"] for r in rows if pred["v2"][r["id"]] == r["expected_route"]
                 and pred["v1"][r["id"]] != r["expected_route"]]  # fmt: skip
    write_report(rows, res, v1_not_v2, v2_not_v1)
    for name, r in res.items():
        print(f"{name:9s} routing {r['correct']}/{r['n']} {pct(r['acc'])} {span(r['acc_ci'])} | "
              f"escalation P {pct(r['precision'])} R {pct(r['recall'])} "
              f"(TP {r['tp']} FP {r['fp']} FN {r['fn']})")  # fmt: skip
    print("right in v1, wrong in v2:", len(v1_not_v2), "| fixed by v2:", len(v2_not_v1))
    if "--no-mlflow" not in sys.argv:
        metrics: dict[str, float] = {}
        for name, r in res.items():
            for k in ("acc", "precision", "recall"):
                if r[k] is not None:
                    metrics[f"{name}.{k}"] = r[k]
                lo, hi = r[f"{k}_ci"]
                if lo is not None:
                    metrics |= {f"{name}.{k}.ci_low": lo, f"{name}.{k}.ci_high": hi}
            for rt, (c, n) in r["by_route"].items():
                if n:
                    metrics[f"{name}.route.{rt}"] = c / n
            metrics |= {f"{name}.tp": r["tp"], f"{name}.fp": r["fp"], f"{name}.fn": r["fn"],
                        f"{name}.misses": len(r["misses"])}  # fmt: skip
        metrics |= {
            "v1_right_v2_wrong": len(v1_not_v2),
            "v2.holdout_acc": HOLDOUT_V2[0] / HOLDOUT_V2[1],
        }
        params = {"dataset": DATASET.name, "dataset_sha256": file_sha256(DATASET),
                  "cases": str(len(rows)), "mode": "offline routing rules, no LLM",
                  **{f"commit_{k}": v for k, v in VERSIONS.items()}}  # fmt: skip
        run_id = tracking.log_metrics("fresh-v2-eval", params, metrics, [REPORT, DATASET])
        print("MLflow run fresh-v2-eval:", run_id)


def write_report(rows: list[dict], res: dict, v1_not_v2: list, v2_not_v1: list) -> None:
    v2, v1 = res["v2"], res["v1"]
    hold = HOLDOUT_V2[0] / HOLDOUT_V2[1]
    lines = [
        "# Fresh v2 test set: honest offline evaluation of the router",
        "",
        f"- **Data:** `{DATASET}` ({len(rows)} cases, written by hayyan after router v2 was "
        "committed; never seen while writing v1 or v2). Fields `expected_route`, "
        "`should_escalate`.",
        "- **Mode:** offline, **routing rules only**: each version's `agent/router.py` read with "
        "`git show` at its commit; no agent, no Gemini, no API call. **Frozen:** nothing in the "
        "agent was changed after seeing these results.",
        "- **Versions:** original `33b240d` (routing as on Day 7, + temperature 0), v1 `8f392b5` "
        "(the 9 fixes), v2 `376183e` (holdout-informed rules).",
        "- 95% percentile bootstrap CIs over cases (2000 resamples, seed 42). Escalation offline = "
        "routed to `escalate`. For the **original**, the real agent also escalated every "
        '"not found" knowledge reply, which needs the LLM to observe: its offline escalation '
        'precision is a **best case** (v1 and v2 don\'t escalate on "not found", so for them the '
        "offline escalation numbers are exact).",
        "",
        "## Overall",
        "",
        "| Version | Routing accuracy [95% CI] | Escalation precision [95% CI] | Escalation recall "
        "[95% CI] | TP / FP / FN |",
        "|---|---|---|---|---|",
    ]
    for name, c in VERSIONS.items():
        r = res[name]
        lines.append(
            f"| {name} `{c}` | {r['correct']}/{r['n']} = **{pct(r['acc'])}** {span(r['acc_ci'])} | "
            f"{pct(r['precision'])} {span(r['precision_ci'])} | {pct(r['recall'])} "
            f"{span(r['recall_ci'])} | {r['tp']} / {r['fp']} / {r['fn']} |"
        )
    lines += ["", "## Per route", "", "| Expected route | n | original | v1 | v2 |",
              "|---|---|---|---|---|"]  # fmt: skip
    for rt in ROUTES:
        n = res["v2"]["by_route"][rt][1]
        cells = [f"{res[v]['by_route'][rt][0]}/{n}" for v in VERSIONS]
        lines.append(f"| {rt} | {n} | " + " | ".join(cells) + " |")
    lines += ["", f"## Right in v1, wrong in v2: {len(v1_not_v2)}", ""]
    if v1_not_v2:
        lines += ["| id | message | expected | v1 | v2 |", "|---|---|---|---|---|"]
        lines += [f"| {i} | {m} | {e} | {a} | {b} |" for i, m, e, a, b in v1_not_v2]
    else:
        lines.append("None.")
    lines += ["", f"Fixed by v2 (wrong in v1, right in v2): {len(v2_not_v1)}"
              + (f": {', '.join(v2_not_v1)}" if v2_not_v1 else "") + ".", ""]  # fmt: skip
    hold1 = HOLDOUT_V1[0] / HOLDOUT_V1[1]
    lines += [
        "## Overfitting check: v2 on fresh vs v2 on holdout",
        "",
        "| Routing accuracy | holdout (228) | fresh (50) | drop |",
        "|---|---|---|---|",
        f"| v1 `8f392b5` (holdout not read when writing it) | {pct(hold1)} | {pct(v1['acc'])} "
        f"{span(v1['acc_ci'])} | {100 * (hold1 - v1['acc']):.1f} pts |",
        f"| v2 `376183e` (written from holdout's failures) | {pct(hold)} | {pct(v2['acc'])} "
        f"{span(v2['acc_ci'])} | **{100 * (hold - v2['acc']):.1f} pts** |",
        f"| v2 - v1 | {100 * (hold - hold1):+.1f} pts | "
        f"{100 * (v2['acc'] - v1['acc']):+.1f} pts | |",
        "",
        f"v2's holdout score ({pct(hold)}) was optimistic: on messages nobody had seen, v2 scores "
        f"**{pct(v2['acc'])}**. But v1 drops almost as much ({100 * (hold1 - v1['acc']):.1f} pts), "
        "and v1 was never fitted to the holdout, so most of the drop is the **fresh set being "
        "harder** (short chat messages, an emoji-only greeting, attack styles and complaint types "
        "that neither dev nor holdout contained), not v2 alone. The overfitting signal is the "
        f"**gain**: v2 beat v1 by {100 * (hold - hold1):+.1f} pts on the holdout it was written "
        f"from, and by {100 * (v2['acc'] - v1['acc']):+.1f} pts on fresh data, with "
        f"{len(v1_not_v2)} case(s) broken. Roughly "
        f"{100 * (v2['acc'] - v1['acc']) / (hold - hold1):.0f}% of v2's holdout gain carried over "
        "to new messages. With n=50 the CIs are wide (about ±14 pts).",
        "",
    ]
    for name, c in VERSIONS.items():
        r = res[name]
        lines += [f"## Misses: {name} `{c}` ({len(r['misses'])})", ""]
        if r["misses"]:
            lines += ["| id | message | expected | actual |", "|---|---|---|---|"]
            lines += [f"| {i} | {m.replace('|', '/')} | {e} | {a} |" for i, m, e, a in r["misses"]]
        else:
            lines.append("None.")
        lines.append("")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
