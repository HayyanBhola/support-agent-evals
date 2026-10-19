# Agent fix v1: before vs after (routing and escalation)

Deterministic metrics only (no judges, no OpenAI). Expected labels: `golden_v1.2.jsonl` fields `expected_route` and `should_escalate`. **Holdout** = the golden cases in neither dev nor the locked test split: the fixes were written from dev errors, so **holdout is the honest number**. 95% percentile bootstrap CIs over cases (2000 resamples, seed 42); differences are paired (same cases).

## Runs and versions

| Run | Subset | Kind | Agent commit | Branch | Code dirty | evalkit | Cases | LLM replies (*) | Started (UTC) |
|---|---|---|---|---|---|---|---|---|---|
| `20261015-212802-d8f4` | dev before | live | `33b240d` | HEAD | False | `dddf0e8` | 96 | 64 | 2026-10-15 21:28 |
| `20261015-214735-83c2` | dev before 2 | live | `33b240d` | HEAD | False | `d2aa2a1` | 96 | 64 | 2026-10-15 21:47 |
| `20261015-220416-d3d9` | dev after | live | `8f392b5` | eval-fixes | False | `d2aa2a1` | 96 | 40 | 2026-10-15 22:04 |
| `20261013-191847-f23b` | holdout before | live | `54f9ec6 (Day 7)` | – | – | `5123801` | 245 | 111 | 2026-10-13 19:18 |
| `offline-holdout-after` | holdout after | **offline** (rules only) | `8f392b5` | eval-fixes | False | `d2aa2a1` | 235 | 0 | – (no agent call) |
| `20261016-111409-d556` | holdout after live | live, **partial** | `8f392b5` | eval-fixes | False | `19ca580` | 61 | 12 | 2026-10-16 11:14 |

(*) Saved replies that used the LLM; each live run also made one warm-up call. Cases include the 10 chat-history cases that are always skipped (holdout).

- **Agent before:** `33b240d` (unchanged routing + temperature 0 + configurable retries); **after:** `8f392b5` (the 9 fixes). Agent LLM: `gemini/gemini-3.1-flash-lite`, temperature 0. `SYSTEM_RULES.md` (the LLM's system prompt) sha256 before `469b1bb57007`, after `235e2203683c`.
- **Run settings:** concurrency 1, 10.0 requests/min, at most 200 LLM calls per run, stop on the first failure, no retries in evalkit; agent started with LLM_RATE_LIMIT_RETRIES=0, LLM_OVERLOAD_RETRIES=1. Cache off (every reply fresh).
- **Dataset:** `golden_v1.2.jsonl` sha256 `4fc4668dc8f5`.

## Noise: the unchanged agent twice on dev

Two runs of `33b240d` on the same 96 dev cases: route changed on **0**, escalation on **0**, answer type on **0**, reply text on **0** of 64 knowledge replies. At temperature 0 the agent was fully repeatable, so **any change below is the fix, not noise** (for dev; holdout was run once per side).

## Dev (optimistic: the fixes were written from these errors)

| Metric | Before [95% CI] | After [95% CI] | After - before [95% CI, paired] |
|---|---|---|---|
| Routing accuracy (n=92) | 64/92 = 69.6% [59.8, 78.3] | 89/92 = 96.7% [92.4, 100.0] | **+27.2 pts** [+18.5, +37.0] (CI excludes 0) |
| Escalation decision correct (n=73) | 53/73 = 72.6% [63.0, 82.2] | 73/73 = 100.0% [100.0, 100.0] | **+27.4 pts** [+17.8, +37.0] (CI excludes 0) |
| Escalation precision | 7/26 = 26.9% [10.0, 44.4] | 8/8 = 100.0% [100.0, 100.0] | **+73.1 pts** [+55.6, +90.0] (CI excludes 0) |
| Escalation recall | 7/8 = 87.5% [60.0, 100.0] | 8/8 = 100.0% [100.0, 100.0] | **+12.5 pts** [+0.0, +40.0] |

Escalation counts: before TP 7 / FP 19 / FN 1 / TN 46; after TP 8 / FP 0 / FN 0 / TN 65.

Routing by expected route:

| Expected route | n | Before | After |
|---|---|---|---|
| clarify | 9 | 6/9 | 9/9 |
| database | 11 | 9/11 | 11/11 |
| escalate | 8 | 6/8 | 8/8 |
| greeting | 8 | 1/8 | 8/8 |
| guardrail | 8 | 1/8 | 8/8 |
| knowledge | 39 | 38/39 | 39/39 |
| medical | 9 | 3/9 | 6/9 |

**Regressions** (right before, wrong after): 0


## Holdout (the honest number)

- **Before** = run `20261013-191847-f23b` restricted to the holdout cases (agent 54f9ec6 (Day 7), default temperature). On dev, that run and the temperature-0 baseline had identical routes and escalations (see Noise), so it stands in for the unchanged agent here.
- **After = OFFLINE**: the fixed router's rules run on every holdout case, no agent call. After the fix, routing and escalation don't depend on the LLM ("not found" no longer escalates), so these numbers are exact for routing and escalation; they say nothing about the wording of knowledge answers.
- **The live holdout run is PARTIAL** (`20261016-111409-d556`, stopped by Gemini 503 "overloaded", resumable): see the cross-check at the end.

| Metric | Before [95% CI] | After [95% CI] | After - before [95% CI, paired] |
|---|---|---|---|
| Routing accuracy (n=228) | 166/228 = 72.8% [67.1, 78.5] | 178/228 = 78.1% [72.4, 83.3] | **+5.3 pts** [+2.6, +8.3] (CI excludes 0) |
| Escalation decision correct (n=172) | 107/172 = 62.2% [55.2, 69.2] | 165/172 = 95.9% [93.0, 98.8] | **+33.7 pts** [+26.2, +41.3] (CI excludes 0) |
| Escalation precision | 22/81 = 27.2% [17.5, 37.6] | 23/25 = 92.0% [80.0, 100.0] | **+64.8 pts** [+52.7, +77.2] (CI excludes 0) |
| Escalation recall | 22/28 = 78.6% [62.5, 93.3] | 23/28 = 82.1% [66.7, 95.8] | **+3.6 pts** [-7.7, +16.1] |

Escalation counts: before TP 22 / FP 59 / FN 6 / TN 85; after TP 23 / FP 2 / FN 5 / TN 142.

Routing by expected route:

| Expected route | n | Before | After |
|---|---|---|---|
| clarify | 18 | 13/18 | 13/18 |
| database | 63 | 62/63 | 62/63 |
| escalate | 28 | 20/28 | 23/28 |
| greeting | 22 | 2/22 | 5/22 |
| guardrail | 22 | 7/22 | 13/22 |
| knowledge | 57 | 55/57 | 55/57 |
| medical | 18 | 7/18 | 7/18 |

**Regressions** (right before, wrong after): 1

| case_id | metric | before | after | question |
|---|---|---|---|---|
| syn-escalate-6c9ab4a2 | escalation_match | expected escalate=True, got True | expected escalate=True, got False | Your chatbot gave me wrong advice and now my skin is worse. Nobody is responding to fix th |

## Live holdout run (partial)

Run `20261016-111409-d556` (agent `8f392b5`): 51 cases answered of 61 recorded so far (the rest skipped: chat history); stopped cleanly: LLM unavailable (no retry) on legacy-oos-weather; it will be re-run on resume. It is not used for the numbers above.

Cross-check against the offline rules on those 51 cases: route differs on **0**, escalation on **0** (the offline numbers match the live agent).

**Closed:** partial (51/235 answered), stopped by repeated Gemini 503 + low memory; offline holdout numbers matched all 51 live cases. No further live holdout runs (decision on 2026-10-17). Case legacy-oos-weather failed on every attempt that reached it (2 of 2, both times the 503 fallback; the second attempt hung for about 21 minutes first): possibly a case-specific issue, not investigated.
