# Day 15: deterministic metrics (no LLM)

No LLM or agent calls. The saved Day 7 run `20261013-191847-f23b` is scored against
**golden_v1.2**. The locked test split was not loaded; the human comparison uses dev only.
Nothing pushed.

## What was built

`evalkit score <run_id> [--dataset] [--price-in --price-out]` runs every metric below on a
saved run and writes `runs/<id>/metrics.jsonl` (one `MetricResult` per case per metric,
sorted) and `runs/<id>/scorecard.md` (rates with 95% bootstrap CIs, breakdowns by expected
route and by dataset source). Deterministic and cache-free: two runs give byte-identical
files. Task: `.\scripts\tasks.ps1 score [-RunId ...]` / `make score RUN=...`. CI runs the
tests only (`runs/` isn't in git), with hand-made inputs for every metric.

| Metric | Module | What it measures | Applies to |
|---|---|---|---|
| `route_match` | metrics/routing.py | the reply's route equals the expected route; confusion matrix | cases with an expected route (380 answered) |
| `escalation_match` | metrics/routing.py | `escalated` equals `should_escalate`; recall (first), precision, F1 | should_escalate set (285) |
| `content_rules` | metrics/rules.py | the agent's substring rules (must_include / _any / must_not_include), same semantics as `run_eval.py` | cases with rules (275) |
| `voice_third_person` | metrics/voice.py | the reply talks about the customer or itself in the third person | every answered reply (395) |
| `db_facts` | metrics/database.py | the reply against gold_db_facts: ingredients, contains, stock, order status, price (±0.01, USD), claimed price, not-found, different product | cases with gold facts (51) |
| `latency_ms`, `tokens` | metrics/ops.py | agent latency; prompt + completion tokens of LLM replies; p50 / p95 / max per route | answered replies |

Checks of the checks:

- **Routing regression test:** the run on golden_v1.2 gives exactly 274/380 routes (72%),
  TP 34 / FN 8 / FP 90 / TN 153: recall 81.0%, precision 27.4%, F1 0.410.
- **Parity with the agent's own checker:** `run_eval.py`'s checks are ported into the test
  (not imported) and run on the agent's `eval_cases.json`; on all 97 answered legacy replies
  with rules, `content_rules` gives the same pass/fail, with no exception.
- **voice_third_person** was developed on non-test replies only and tested on the 5 Day 12
  tone cases in dev plus hand-made replies; the 3 in test were left untouched.
  `rubrics/voice_allowlist.txt` removes "I'm the GlowCare assistant", "customer support",
  "customer service", "customer care" before matching.
- **db_facts** was developed looking at non-test cases only (2 of its 7 fails are on test
  cases and were not inspected).
- **Cost:** tokens only; cost is computed only with `--price-in` / `--price-out` (USD per 1M
  tokens). There is no default price.

## Scorecard headline (Day 7 run, golden_v1.2)

| Metric | Passed | Rate | 95% CI |
|---|---|---|---|
| route_match | 274/380 | 72.1% | [67.6, 76.6] |
| escalation_match | 187/285 | 65.6% | [60.0, 71.2] |
| db_facts | 44/51 | 86.3% | [76.5, 94.1] |
| voice_third_person (clean) | 372/395 | 94.2% | [91.9, 96.5] |
| content_rules (weak signal) | 247/275 | 89.8% | [85.8, 93.5] |

- **Escalation:** recall **81.0%** (34/42) [68.9, 91.9]; precision **27.4%** (34/124)
  [19.8, 35.6]; F1 0.410 [0.314, 0.503]. 8 missed, 90 extra.
- **Routing** is near-perfect for knowledge (97.5%) and database (95.1%) and poor for
  greeting (11.1%), guardrail (27.8%) and medical (38.7%): almost every miss falls through
  to knowledge (32 of 36 greetings, 24 of 36 guardrail, 11 of 31 medical).
- **By source:** legacy routes 106/106 (the agent was built on that sheet); synthetic 62.1%;
  known bugs 40.0% (by design).
- **db_facts** fails: not_found 4, and one each for price, contains, ingredients and
  same_product (the Vitamin C -> Niacinamide case, flagged as a different product).
- **voice:** 23 of 395 replies flagged ("customers" 10, "users" 5, "the assistant" 3,
  "the customer" 3, "the AI assistant" 2).
- **Latency (agent):** p50 6.0 s, p95 14.4 s over all replies; knowledge p50 9.4 s, p95
  17.1 s, max 40.5 s; database p50 108 ms; fixed replies (escalate, greeting, guardrail,
  medical, clarify) under 25 ms.
- **Tokens:** 219 LLM replies (all knowledge), 218,989 tokens (213,016 prompt, 5,973
  completion), about 1,000 per reply. Cost not computed (no price configured).

## Sanity check against the human labels (dev split only)

**Escalation** (`escalation_match` vs hayyan's `escalation_appropriate`, dev, pairs where
both apply): **73 of 73 agree**, κ 1.0 (20 fails, 53 passes on both sides). The other 23
dev cases have no pair (the label is n_a or should_escalate is null). Expected: every
escalation label already matched escalated vs should_escalate (Day 12), so on cases with
should_escalate set the deterministic check can replace the human or LLM judgment.

**Voice** (`voice_third_person`, dev): all **5 of 5** Day 12 tone cases in dev flag. It
flags 7 of the 96 dev replies in total; the 2 others (syn-medical-00d2b211,
syn-medical-d858a37e) quote product copy that says "new users are advised...". Both were
tone **pass** under v1, so they are cases the human pass didn't call out, not false
positives by the rule's definition: customer voice copied from the knowledge base.

## What these metrics can't measure (why the LLM judges are still needed)

- **Faithfulness:** whether each claim is supported by the retrieved chunks needs reading
  both and comparing meaning; paraphrase defeats substring checks. Only db_facts covers the
  narrow case of structured database facts.
- **Correctness in substance:** content_rules is a weak signal (Day 8: 22 of 85 correctly
  routed LLM knowledge answers fail it, mostly correct paraphrases); judging a free-text
  answer against the reference answer needs a judge.
- **Relevance:** whether a reply addresses the question asked (a quoted policy section on
  the wrong topic, a product description for a comparison) is semantic.
- **Tone:** politeness, acknowledging an upset customer, concision are judgments; only the
  voice part became a rule.
- **Safety** beyond structure: an implied medical claim ("product copy that reads as yes")
  can't be matched by patterns.
- **Escalation when should_escalate is null** (the rubric's "rules don't decide" cases).

## Surprises

1. **Exact parity on legacy-adv-fake-price.** Expected to differ: the agent's phrase is
   `"costs $1 "` with a trailing space, which never matched "$19.99". The Day 8 bug was our
   import stripping that space; golden_v1.1 restored the agent's intent.
2. **db_facts fails 3 "Which product do you mean?" replies** to non-existent products
   (expected route null): the reply lists the real catalog instead of saying the product
   doesn't exist. By the agreed rule (pass only if it says it can't be found) that is a fail;
   a human passed one of them (syn-database-2cccc9ee, Week 2). Nothing is invented, so this
   is a strictness question for the rule, worth a decision before the judges.
3. **content_rules never caught delivered forbidden content:** all 28 fails are missing
   phrases; no reply contained a forbidden phrase outside the fixed not_found text.
4. **voice flags "new users are advised"** in product copy, which the human pass under v1
   didn't treat as a voice problem (above).
5. **Every LLM call is on the knowledge route**; the 40.5 s max and 17 s p95 there dominate
   the latency profile; every other route answers in under half a second.

## Decisions on the surprises (Day 15)

1. **Non-existent products: the strict rule stays.** The reply must say we don't carry the
   product; listing the catalog alone fails. hayyan relabeled **syn-database-2cccc9ee**
   ("how much does the Retinol Night Cream cost?" -> "Which product do you mean? We have:
   ...") correctness pass -> **fail** in the app (2026-10-14 19:21 UTC, under rubric v2; note
   "Non-existent product: did not say we don't carry it"). It is the only non-existent-product
   case among the 160 labels, and it is in dev, so the test split is unaffected.
   - **correctness.md Key rules** gained: "A product that doesn't exist: the reply must say
     we don't carry it; offering the catalog alone is a fail." That changes the hash:
     **rubric v3 = `0ab86671dbd25d6384032d44e6fc1b90eec5623b19504209b176f2b0c4b9f6ab`**,
     recorded in `rubrics/versions.json` as a **wording clarification** (correctness.md at
     `version: 3`; the correctness Pass if already said "says it can't be found"). Its re-check
     list is empty: no other label is affected. All 160 labels are carried forward (v1 -> v2
     -> v3); `labels status`: 0 to re-check.
   - **`datasets/labels_v1.2.jsonl`** (MANIFEST version 1.2, changelog in the entry): exactly
     one verdict differs from v1.1 (that one). correctness: 78 pass / 5 fail / 77 n_a (dev now
     3 fails, test 2); records with any fail: 43. `labels_v1.jsonl` and `labels_v1.1.jsonl`
     are unchanged. `split_v1.json` keeps its case_ids and still records v1.1 as the labels it
     was made from; pass `labels_path=datasets/labels_v1.2.jsonl` to `load_labels` to use the
     new version (the split is keyed by case_id, so no re-split).
   - db_facts already applies this rule (its `not_found` check), so the 3 "Which product do
     you mean?" replies stay fails.
2. **Voice: "new users are advised" stays flagged.** `voice_third_person` is a separate
   deterministic metric, not tone: it reports customer or self voice copied from the
   knowledge base, wherever it appears, and is never folded into the tone verdict (rubric v2
   took voice out of tone). The 2 dev replies it flags beyond the Day 12 cases are therefore
   correct flags, not disagreements with the tone labels.
