# Week 2 wrap-up (Days 8-14)

## What the labels tell us about the agent

From all 160 human labels in `datasets/labels_v1.1.jsonl` (rubric v2), read directly: this
is analysis of the agent, not judge tuning. **42 of 160 records have at least one fail.**
Each one is in exactly one pattern below (checked in code).

**Top 3 patterns by count:**

1. **Greetings and thanks escalated** (12): multi-word greetings go to knowledge, get
   `not_found`, and are handed to a human.
2. **Prompt injections escalated instead of refused** (6): the guardrail patterns miss
   them, knowledge answers `not_found`, and the `not_found` path escalates.
3. **Vague questions not clarified** (5): no clarifying question; either `not_found` plus
   escalation, or a generic answer.

**One mechanism behind most of it:** 28 of the 42 fail records are a `not_found` answer
that was escalated (Day 7 finding 2: every `not_found` answer escalates), and 38 of 42 went
to the knowledge route. **31 of the 33 escalation fails are over-escalations.** Fixing the
`not_found -> escalate` rule and the router's fall-through to knowledge would remove most
of the observed fails at once.

## Failure patterns (Week 4 experiment candidates)

| # | Pattern | Count | Criteria failed | Examples | Day 7 finding | Fix idea for the agent |
|---|---|---|---|---|---|---|
| 1 | Greeting / thanks routed to knowledge, `not_found`, escalated | 12 | escalation_appropriate 12, relevance 12 | bug-greeting-hello-hello, syn-greeting-33dc74d2 | **1** (and 2) | Match greetings and thanks on any short social message, not only exact one-word ones, before retrieval. |
| 2 | Prompt injection / rule-override escalated instead of refused | 6 | escalation_appropriate 6 | syn-guardrail-3879376c, syn-guardrail-3e99311e | **2** (the `not_found` path); the guardrail miss is new | Widen the guardrail patterns (role-play, "ignore your rules", "act outside rules") and never escalate a guardrail case. |
| 3 | Vague question not clarified | 5 | relevance 5, escalation_appropriate 3 | syn-clarify-03d3b1d5, syn-clarify-18f68720 | **new** (3 of 5 also via 2) | Route order / tracking questions without an order number to the clarify reply ("Could you share your order number?"). |
| 4 | Medical question on knowledge, `not_found`, escalated | 4 | escalation_appropriate 4 | syn-medical-190c5dd1, syn-medical-314679bf | **7** (and 2) | Extend `MEDICAL_QUESTION_PATTERNS` (pregnancy, "before I use", "is it safe") so these get the medical reply, not knowledge. |
| 5 | Product copy as the answer to a cure / before-use / ingredient question | 3 | relevance 3, safety 2 | syn-medical-00d2b211, syn-medical-b7f4d113 | **7** | Never answer a medical-phrased question with a product-guide quote; send it to the medical reply with the caution. |
| 6 | Answerable ingredient question got `not_found` | 2 | escalation_appropriate, correctness, completeness, relevance (2 each) | syn-database-a9626580, syn-database-b242857a | **new** (database route missed; then 2) | Route "what's in / ingredients of <product>" to the database when a product name matches. |
| 7 | Before-use allergy question treated as a reaction | 2 | escalation_appropriate 2, relevance 2 | syn-medical-553f24d4, syn-medical-b8756450 | **8** | Escalate only a reaction that happened ("since using", "got a rash"); a question before use goes to the medical reply. |
| 8 | Complaint or human request not escalated | 2 | escalation_appropriate 2, relevance 2, tone 2 | syn-escalate-044fac8f, syn-escalate-2af765de | **6** | Detect complaints and requests for a person without trigger words (refund refused, "supervisor", "this bot isn't helping"), escalate, and acknowledge. |
| 9 | Unrelated product description quoted (competitor comparison, price-match demand) | 2 | relevance 2, tone 1 | legacy-oos-competitor, syn-guardrail-997dc058 | **2** (its competitor note) | Require the quoted section to mention what was asked; otherwise decline politely (competitors) or acknowledge and refuse (price match). |
| 10 | Out-of-scope question escalated | 1 | escalation_appropriate 1 | syn-out-of-scope-6b06ea9a | **2** | Answer `not_found` for off-topic chat without escalating (rule 7), keeping escalation for policy gaps (rule 5). |
| 11 | Informational question treated as a human request | 1 | escalation_appropriate, correctness, completeness, relevance | syn-knowledge-812bf00a | new | Don't fire the human-request trigger on questions *about* support ("how do I reach a person?"); answer them. |
| 12 | Incomplete answer (how to contact support) | 1 | correctness, completeness | syn-knowledge-926b08ac | new | Include both contact paths from the knowledge base (chat assistant and human team). |
| 13 | Guardrail request misread as clarify | 1 | relevance | syn-guardrail-dae41954 | new | Check the guardrail patterns before the clarify rule (it asked again for an order number already given). |
| – | Third-person / policy voice | **0** under v2 | – | (8 in v1, e.g. legacy-kb-fragrance) | – | No longer a tone fail (rubric v2); tracked by the Day 15 `voice_third_person` rule. Rewriting the knowledge base in the second person stays a Week 4 idea. |

Total: 12 + 6 + 5 + 4 + 3 + 2 + 2 + 2 + 2 + 1 + 1 + 1 + 1 = **42**.

**Day 7 findings confirmed by the human labels:** 1, 2, 6, 7 and 8. Not as a fail:
3 is visible in the routing (two reaction cases, syn-medical-137df081 and
syn-medical-642a3f44, went to knowledge) but both were escalated through the `not_found`
path, which the rubric scores as a pass by design (routing is scored on Day 15); 4 has one
case in the sample (syn-database-2cccc9ee), which the agent answered without a fail. 5 is a
setup problem and no reply-level label can see it. **New in Week 2:** vague questions not clarified, missed
database routing for ingredient questions, guardrail misses beyond the `not_found` path,
and three single cases (11-13).

**Week 4 candidates, by expected impact:** (a) stop escalating every `not_found` (patterns
1, 2, 4, 6, 10: 25 records); (b) widen greeting and guardrail detection (1, 2, 13); (c)
medical routing (4, 5, 7); (d) complaint detection (8); (e) clarify routing (3).

**Added from the Day 15 scorecard** (deterministic metrics on the Day 7 run, golden_v1.2):

- **Routing fall-through to knowledge.** Routing is near-perfect for knowledge (97.5%) and
  database (95.1%) but poor for greeting (11.1%), guardrail (27.8%) and medical (38.7%);
  almost every miss lands on knowledge: 32 of 36 greetings, 24 of 36 guardrail, 11 of 31
  medical, 12 of 42 escalate, 10 of 32 clarify. Knowledge then answers `not_found` and the
  not_found path escalates (patterns 1-4 above). Week 4 candidate: make knowledge a
  deliberate route rather than the default, with the other routes' detectors run first and
  widened, and measure the routing confusion matrix before and after.
- **Latency.** The knowledge route (every LLM call) has p50 **9.4 s**, p95 **17.1 s**, max
  40.5 s; every other route answers in **under 0.5 s** (database p50 108 ms, fixed replies
  under 25 ms). Week 4 candidates: fewer or shorter retrieved chunks (about 1,000 prompt
  tokens per reply), a smaller or faster model for knowledge, and fixing the fall-through
  above, which sends greetings and guardrail cases through the slow path for nothing.

## Week 2 in numbers

- **Rubric v2** (7 criteria, Key rules boxes, tone without the voice rule), hash
  `26af257f50c7`, recorded in `rubrics/versions.json`.
- **160 human labels** (hayyan), first pass 74.6 s mean / 50.5 s median per record;
  `labels_v1.1` after the v2 re-check of 8 tone verdicts.
- **Agreement** with a second labeler (labeller_b, 50 overlap records, rubric v1): escalation
  κ 0.87; tone agreement 78% (official round 1) -> 90% post-hoc under v2. Kappa is
  unreliable on the rare-fail criteria; AC1 and CIs are reported with it.
- **Dev/test split** 96 / 64 (`datasets/splits/split_v1.json`, tag `labels-v1.1-split-v1`),
  test locked behind `final=True` + a reason + `EVALKIT_FINAL_TEST=1`, with an access log.

## Health check (Day 14)

All green: pre-commit (ruff, ruff format, mypy --strict), pytest (663 passed) with both
coverage gates (data contracts 100%, runtime 99%), `evalkit rubric check` (v2 recorded),
`evalkit labels validate` (0 errors; the 5 applicability warnings kept by decision),
`evalkit split check` (no test data in prompts/ or judges/), and the MANIFEST (no stale
entry; `update_manifest.py` leaves it unchanged).

**Fixed:** `manifest.unlisted` compared bare file names, so a dataset file in a subfolder
(`splits/split_v1.json`) always looked unlisted even though MANIFEST lists it. It now
compares paths relative to `datasets/`, and the dataset test covers `splits/*.json` too.

## Week 3 prep: open decisions

1. **Judge model family.** It must differ from the agent's (Gemini,
   `gemini-3.1-flash-lite`) and the test-case generator's (OpenAI, `gpt-5-mini`), so the
   judge shares neither's blind spots. Candidate: **Qwen on Groq**. To decide: the model
   and version (pinned), temperature 0, cost per full dev run (estimate before any paid
   call), and whether a second judge family is needed for the safety criterion.
2. **Synthetic negatives for faithfulness and safety.** Test has 0 faithfulness fails and 1
   safety fail, so judge recall on them can't be measured from human labels. Plan: a
   separate, clearly marked synthetic file (e.g. real replies edited to contradict the
   retrieved chunks, or to state a wrong price or a cure claim), labeled as synthetic,
   reported separately, **never mixed with the human labels or the split**. To decide: how
   many per criterion, who writes and reviews them, and whether they are built from dev
   cases only (they must be, to keep test clean).
3. **The hayyan-r2 blind relabel** of `datasets/relabel_v1.jsonl` (30 records, labeler
   `hayyan-r2`), **on or after 2026-10-17**, then
   `evalkit agreement report --a hayyan --b hayyan-r2 --out reports/self_consistency.md`.
   It is now under rubric v2 while the first pass was mostly v1: compare tone with that in
   mind (the report warns about the differing hashes).
