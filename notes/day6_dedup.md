# Day 6: deduplication of the synthetic cases

`uv run python scripts/dedup_synthetic.py` (deterministic, no LLM calls):
`datasets/synthetic_raw_v1.jsonl` (290) -> `datasets/synthetic_dedup_v1.jsonl` (**287**).
Legacy and known-bug cases are compared against but never dropped.

## Rule

A synthetic case is dropped when its normalised question (lower-case, letters and digits
only) has rapidfuzz `token_sort_ratio` **>= 80** against a case that asserts the same thing
(same `expected_route` and same `gold_db_facts`) and is kept: any legacy or known-bug case,
or an earlier synthetic case in file order. Near-miss greeting controls are exempt (their
greeting opener is what they test).

Why not `token_set_ratio`: it scored 100 for 49 synthetic cases, mostly because one
question's words were a subset of the other's (every question naming "Hydra Balance
Moisturizer" vs the legacy case whose whole text is "Hydra Balance Moisturizer"). Why the
label gate: "order GC10241" vs "order GC10242" scored 97 but are different tests.

Threshold chosen from the 10 closest same-label pairs (95.7 down to 74.4); below 80 the
pairs are different questions or deliberately different styles. Agreed on Day 6.

## Result

| Dropped case | Generator | Route | Score | Duplicate of | Dropped question | Kept question |
|---|---|---|---|---|---|---|
| `syn-knowledge-a10b5e52` | knowledge | knowledge | 81.5 | `syn-knowledge-796a63d7` | how long til i get my money back after i send a return? been waiting lol | How long will it take to get my money back after I send a return? |
| `syn-greeting-5e53a996` | greeting | greeting | 82.4 | `bug-greeting-hi-how-are-you` | Hi, how are you today? | hi, how are you |
| `syn-clarify-664a2f72` | clarify | clarify | 80 | `legacy-clarify-price-vague` | what's the price of this? | What's the price? |

| Route | Target | Dropped | Kept | vs target |
|---|---|---|---|---|
| knowledge | 75 | 1 | 91 | ok |
| database | 38 | 0 | 44 | ok |
| greeting | 38 | 1 | 27 | short by 11 |
| escalate | 23 | 0 | 25 | ok |
| clarify | 26 | 1 | 25 | short by 1 |
| guardrail | 33 | 0 | 34 | ok |
| medical | 30 | 0 | 31 | ok |
| (not checked) | - | 0 | 10 | - |


Greeting is short of its synthetic target by design: the 10 near-miss controls carry the
label of their real request. Against the golden-set targets (`datasets/README.md`), adding
the runnable Day 4 cases: greeting 27 + 10 = 37 (target 35), clarify 25 + 7 = 32
(target 25). Every route still meets its golden-set target.
