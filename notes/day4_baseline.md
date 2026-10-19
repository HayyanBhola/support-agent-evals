# Day 4 baseline: legacy + known-bug cases

| | |
|---|---|
| Run id | **`20261013-140332-2240`** |
| Dataset | `datasets/day4_baseline.jsonl` (128 cases = legacy_v1 118 + known_bugs_v1 10) |
| Command | `evalkit run --dataset datasets/day4_baseline.jsonl --concurrency 2 --refresh` |
| Agent | `gemini/gemini-3.1-flash-lite`; snapshot taken the same day at `54f9ec6`, and every route matched it (see below) |
| evalkit | `692c683`, dirty (the route report was not committed yet) |
| Date | 2026-10-13 |

Reproduce the breakdown with `evalkit routes 20261013-140332-2240`.

## Outcomes

| ok | failed | skipped | cache hits | retries |
|---|---|---|---|---|
| 118 | 0 | 10 | 0 | 0 |

The 10 skipped cases are the legacy `memory` cases (`needs_history`): eval_mode can't replay
history yet.

## Route accuracy: 95% (110/116)

116 = 118 ok responses minus the 2 legacy cases whose route is not checked.

| Category | Accuracy | | Expected route | Accuracy |
|---|---|---|---|---|
| adversarial | 100% (8/8) | | clarify | 100% (7/7) |
| clarify | 100% (7/7) | | database | 100% (38/38) |
| database | 100% (24/24) | | escalate | 100% (14/14) |
| escalation | 100% (12/12) | | greeting | **40% (4/10)** |
| greeting | **25% (2/8)** | | guardrail | 100% (3/3) |
| knowledge | 100% (35/35) | | knowledge | 100% (39/39) |
| medical | 100% (4/4) | | medical | 100% (5/5) |
| out_of_scope | 100% (4/4) | | | |
| robustness | 100% (14/14) | | | |

**Every wrong route, by expected route.** All six are the known bugs, routed to knowledge,
answered "I don't have that information" (`not_found`) and **escalated to a human**:

| Expected | Actual | Case | Message | Escalated |
|---|---|---|---|---|
| greeting | knowledge | `bug-greeting-hi-again` | hi again | yes |
| greeting | knowledge | `bug-greeting-hello-again` | hello again | yes |
| greeting | knowledge | `bug-greeting-hey-there` | hey there | yes |
| greeting | knowledge | `bug-greeting-hello-hello` | hello hello | yes |
| greeting | knowledge | `bug-greeting-hi-how-are-you` | hi, how are you | yes |
| greeting | knowledge | `bug-greeting-thanks-a-lot` | thanks a lot! | yes |

The 4 controls (`known_bug_control`) all got their expected route (2 knowledge, 2 database).

## Legacy mismatches: none

All 106 legacy cases that have a route and ran got it right (100% in every legacy
category), in line with the legacy sheet's own 118/118. So there is nothing to attribute to
either a mapping error or an agent change: the live agent behaves like the snapshot, and the
offline router cross-check (`tests/test_datasets.py`) predicted exactly these routes. The two
legacy cases without a route (`kb-help-acne`, `adv-discount-demand`) went to knowledge,
as the snapshot's router predicts, and both escalated (`not_found`).

Not checked yet: the 10 history cases, and the content rules (`must_include`, ...) of all
cases. Route is the only thing scored today.

## Escalation

Knowledge answers escalated exactly when `answer_type` was `not_found` (14 of 47), matching
`support_agent.py`. Answer types on knowledge: llm 31, not_found 14, quoted_source 2. In
16 of them the LLM was called but its answer was replaced (`checker_replaced`).

`should_escalate` matched in 16 of 22 cases that set it; the 6 misses are the known bugs
(expected false, got true).

### Out-of-scope questions (possible product issue)

| Case | Message | answer_type | Escalated |
|---|---|---|---|
| `legacy-oos-weather` | What's the weather in Karachi today? | not_found | **yes** |
| `legacy-oos-poem` | Write me a poem about the sea | not_found | **yes** |
| `legacy-oos-shoes` | What is your policy on returning shoes? | not_found | **yes** |
| `legacy-oos-competitor` | Is CeraVe better than GlowCare? | quoted_source | no |

**3 of 4 out-of-scope questions escalate to a human.** The agent treats "not in the
knowledge base" as "a human must follow up", so off-topic chat (weather, poems) and, through
the greeting bug, small talk like "hi again" create low-priority escalations a person has
to close. With real traffic this could flood the support queue with tickets nobody needs.
Worth a product decision in the agent: escalate on `not_found` only for on-topic questions
(e.g. return policy for a product type we don't sell), and answer off-topic ones with a
fixed "I can only help with GlowCare..." reply without a hand-off.

Also notable: the competitor question got a word-for-word quote of the Barrier Repair Cream
product-guide section, which doesn't answer it. It passed the legacy checks (nothing
forbidden), but it's the kind of irrelevant answer the Day 5+ judges should catch.

## Latency (non-cached, concurrency 2)

| Route | n | p50 | p95 |
|---|---|---|---|
| knowledge | 47 | 6.73 s | **68.48 s** |
| database | 38 | 0.09 s | 0.22 s |
| escalate / clarify / greeting / guardrail / medical | 33 | 0.01-0.02 s | 0.02 s |

Knowledge calls were far slower than in the Day 3 benchmark (p50 3.5 s): over 47 LLM calls
the agent's Gemini calls slowed down, with the slowest at **116.8 s**
(`legacy-kb-shipping-express`), only ~3 s under the adapter's 120 s read timeout. No
retries, no 429s. Before running the full ~300-case set, either raise the read timeout
(e.g. 180 s) or check whether concurrency 1 keeps the tail down; a timeout would be recorded
as a failure that isn't the agent's answer quality.
