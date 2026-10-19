"""Offline v1 vs v2 router check (rules only, no LLM, no agent call), Day 26.

Runs the v1 router (agent commit 8f392b5, read with `git show`) and the router in the agent
repo's working tree on dev, the holdout (golden cases in neither dev nor test) and the agent's
own 118-case sheet; prints accuracy per route, escalation counts and every case that was right
in v1 and wrong in v2. Usage, from the repo root:

    uv run python scripts/compare_routers.py reports/router_v1_vs_v2.json
"""

import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "src")
from evalkit import budget  # noqa: E402
from evalkit.io import read_jsonl  # noqa: E402
from evalkit.schemas import TestCase  # noqa: E402

AGENT = Path("../rag-whatsapp-agent")
blob = subprocess.run(
    ["git", "show", "8f392b5:agent/router.py"], cwd=AGENT, capture_output=True, check=True
).stdout
v1_path = Path(tempfile.mkdtemp()) / "router_v1.py"
v1_path.write_bytes(blob)
v1 = budget.load_router(v1_path)
v2 = budget.load_router(AGENT / "agent" / "router.py")
prods = budget.load_products(Path("agent_snapshot/data/products/products.json"))
cases = {c.id: c for c in read_jsonl(Path("datasets/golden_v1.2.jsonl"), TestCase)}
split = json.loads(Path("datasets/splits/split_v1.json").read_text(encoding="utf-8"))["splits"]
out = {}


def check(name, items):
    """items: list of (id, question, expected_route, should_escalate or None)."""
    by = defaultdict(lambda: [0, 0, 0])
    regress, fixed = [], []
    esc = {"v1": [0, 0, 0], "v2": [0, 0, 0]}  # tp fp fn
    for cid, q, exp, should in items:
        a = v1.route_message(q, prods).route
        b = v2.route_message(q, prods).route
        if exp:
            by[exp][0] += a == exp
            by[exp][1] += b == exp
            by[exp][2] += 1
            if a == exp and b != exp:
                regress.append((cid, "route", exp, a, b, q))
            if a != exp and b == exp:
                fixed.append(cid)
        if should is not None:
            for tag, r in (("v1", a), ("v2", b)):
                e = r == "escalate"
                esc[tag][0] += should and e
                esc[tag][1] += (not should) and e
                esc[tag][2] += should and not e
            if (a == "escalate") == should and (b == "escalate") != should:
                regress.append((cid, "escalation", should, a, b, q))
    tot = [sum(v[i] for v in by.values()) for i in range(3)]
    print(
        f"\n===== {name}: routing v1 {tot[0]}/{tot[2]} ({100 * tot[0] / tot[2]:.1f}%) "
        f"-> v2 {tot[1]}/{tot[2]} ({100 * tot[1] / tot[2]:.1f}%)"
    )
    for r, (x, y, n) in sorted(by.items()):
        print(f"   {r:10s} v1 {x}/{n}  v2 {y}/{n}")
    for tag in ("v1", "v2"):
        tp, fp, fn = esc[tag]
        if tp + fp + fn:
            p = tp / (tp + fp) if tp + fp else float("nan")
            rc = tp / (tp + fn) if tp + fn else float("nan")
            print(f"   escalation {tag}: TP {tp} FP {fp} FN {fn} precision {p:.3f} recall {rc:.3f}")
    print(f"   fixed by v2: {len(fixed)}")
    print(f"   REGRESSIONS (right in v1, wrong in v2): {len(regress)}")
    for r in regress:
        print("     ", r[0], r[1], "expected", r[2], "| v1", r[3], "| v2", r[4], "|", r[5][:110])
    out[name] = {"routing": tot, "by": dict(by), "esc": esc, "regress": regress, "fixed": fixed}


def golden(ids):
    return [
        (
            i,
            cases[i].question,
            cases[i].expected_route.value if cases[i].expected_route else None,
            cases[i].should_escalate,
        )
        for i in ids
        if not cases[i].history
    ]


check("dev", golden(sorted(split["dev"])))
check("holdout", golden(budget.subset_ids(list(cases.values()), split, "holdout")))
sheet = json.loads((AGENT / "tests" / "eval_cases.json").read_text(encoding="utf-8"))
sheet = sheet["cases"] if isinstance(sheet, dict) else sheet
check(
    "agent sheet (118)",
    [(c["id"], c["question"], c.get("route"), None) for c in sheet if not c.get("history")],
)
summary = {
    k: {
        "routing": v["routing"],
        "by": v["by"],
        "esc": v["esc"],
        "fixed": v["fixed"],
        "regress": [list(map(str, r)) for r in v["regress"]],
    }
    for k, v in out.items()
}
Path(sys.argv[1]).write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8", newline="\n")
