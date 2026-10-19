# Day 25: agent fix v1 (Phase B): routing and escalation

**Status: done on dev; holdout measured offline (exact for routing and escalation); the live
holdout run is CLOSED as partial** (51/235 answered), stopped by repeated Gemini 503
"overloaded" and low memory on the machine. Decision on 2026-10-17: no further live holdout
runs. Report:
[reports/agent_fix_v1.md](../reports/agent_fix_v1.md). MLflow: experiment
`glowcare-support-agent`, run `agent-fix-v1`. No judges, no OpenAI: Gemini only, through the
agent.

## What changed in the agent

Branch `eval-fixes` of `rag-whatsapp-agent`, from `54f9ec6`. Local commits only; `main` and
the uncommitted vector-store files are untouched.

| Commit | Change |
|---|---|
| `f94ba77` | Temperature 0 for Gemini (Google recommends the default for Gemini 3; chosen for repeatable evals) |
| `33b240d` | Retries configurable: `LLM_RATE_LIMIT_RETRIES`, `LLM_OVERLOAD_RETRIES` (defaults unchanged). **The "before" agent.** |
| `4159e3d` | Fix A: "not found" **offers** a team member and no longer escalates; a short "yes" right after the offer escalates. `SYSTEM_RULES.md` rules 5 and 9 now agree ("not found" means offer a person; "low-confidence answer -> escalate" removed). Nothing else in that file changed. |
| `8f392b5` | Fixes B-E and R1-R4 as general rules in `agent/router.py`, plus 45 offline unit tests (`tests/test_router_rules_pytest.py`; 51 agent tests pass). **The "after" agent.** |

The fixes, all written as general rules:
- **A.** Offer a person on "not found".
- **B.** An allergy mentioned before use is an ingredient question, answered from the database.
- **C.** A question about the hand-off isn't a request for one.
- **D.** Human-request words, including supervisor, agent, real human, hand-off verbs, and frustration with the bot.
- **E.** Milder reaction words count when tied to using a product.
- **R1.** Small talk.
- **R2.** Manipulation, by intent.
- **R3.** Medical phrasings.
- **R4.** Softer ingredient words, order status without an id, and vague availability.

Regression check on the agent's own 118-case sheet: **0 route changes**. One draft rule broke
"I have red dry patches, which disease is it?"; it was fixed before the commit by tying
reaction words to product use.

## How it was run

`evalkit run` with: concurrency 1, 10 requests/min, at most 200 LLM calls per run, no retries
in evalkit, a clean stop on any failure, cache off. The agent ran with
`LLM_RATE_LIMIT_RETRIES=0` and `LLM_OVERLOAD_RETRIES=1` (one retry on a 503). Every run's
`meta.json` records the agent commit, branch and dirty state, plus the run settings. The dry
run (`evalkit agent-budget`) predicted the LLM calls exactly: 65 / 65 / 41 on dev.

| Run | Agent | Cases | Result |
|---|---|---|---|
| `20261015-212802-d8f4` before/dev #1 | `33b240d` | 96 | done, 65 LLM calls |
| `20261015-214735-83c2` before/dev #2 (noise) | `33b240d` | 96 | done, 65 |
| `20261015-220416-d3d9` after/dev | `8f392b5` | 96 | done, 41 |
| `20261016-111409-d556` after/holdout | `8f392b5` | 61 of 245 | **closed, partial** (51/235 answered): Gemini 503 stops at 11:19, 11:44, 12:05 and 12:28 UTC; a 13:11 resume hung for about 21 minutes and stopped on the same case; the auto-resume was then killed by Claude Code for low memory |
| before/holdout (temperature 0) | `33b240d` | – | not run (decision: skip the rest of the live holdout) |

## Results

**Noise:** the unchanged agent run twice on dev at temperature 0 gave **identical** replies (0
route, 0 escalation, 0 answer-type and 0 text changes in 96 cases). Any change on dev is the
fix.

| | Dev before -> after (live) | **Holdout before -> after (honest)** |
|---|---|---|
| Routing accuracy | 69.6% -> **96.7%** (+27.2 pts [+18.5, +37.0]) | 72.8% -> **78.1%** (+5.3 pts [+2.6, +8.3]) |
| Escalation precision | 26.9% -> **100%** | 27.2% -> **92.0%** (+64.8 pts [+52.7, +77.2]) |
| Escalation recall | 87.5% -> **100%** | 78.6% -> **82.1%** (+3.6 pts [-7.7, +16.1]) |
| False alarms / misses | 19 / 1 -> 0 / 0 | 59 / 6 -> 2 / 5 |

Holdout "before" is the Day 7 run (agent `54f9ec6`, default temperature). On dev, Day 7 and the
temperature-0 baseline had identical routes and escalations. Holdout "after" is **offline**:
the fixed router's rules on all 235 answerable holdout cases. That is exact for routing and
escalation, since after the fix neither depends on Gemini. **Cross-check:** on the 51 holdout
cases the live run did answer, the live agent matched the offline rules on every route and
every escalation (0 differences).

- **Dev is optimistic** (the rules were written from dev errors). **Holdout is the honest
  number:** a clear, significant win on escalation precision (false alarms 59 -> 2), a modest
  but significant routing gain, and recall about the same.
- **One regression on holdout:** syn-escalate-6c9ab4a2 ("Your chatbot gave me wrong advice and
  now my skin is worse. Nobody is responding…") used to be escalated **only** through the
  "not found" path. With fix A it isn't. This is the hidden-miss mechanism Phase A predicted:
  a complaint in words the rules don't list.
- **Still weak on holdout:** greetings 5/22, guardrail 13/22, medical 7/18. There are 5
  escalation misses, all complaints without trigger words ("support keeps closing my case",
  "got generic responses that ignored my problem"). The 2 false alarms: a role-play request
  containing "manager", and "itchy patches" (an existing safety word).
- **Side effect:** 15 of 39 dev knowledge answers that kept their route have new wording,
  because the edited rules 5/9 and the new "not found" reply are part of Gemini's prompt. They
  read as rephrasings but **weren't quality-scored** (no judges in this phase).

## Limitations

- The live holdout is partial (Gemini 503s). The holdout numbers are offline, which is exact
  for routing and escalation but not for answer content.
- Holdout "before" uses the Day 7 run (default temperature). The temperature-0 before/holdout
  run is still to do.
- Dev noise was measured, holdout noise wasn't (one run per side). Dev showed none at
  temperature 0.
- The keyword rules fit dev better than holdout (greeting/guardrail/medical). More holdout
  gains need rules written from a different sample. Holdout was not used to write any rule.
- Labels: one labeler. The 4 "allergy before use" labels say medical, but the decision was
  database; those cases count as routing errors until the labels change.
- Answer quality wasn't judged.

## Gemini calls

- Oct 15: 171.
- Oct 16, so far: about 20 (most of them 503s), out of the 240/day budget.

## The partial live holdout run (closed)

Run `20261016-111409-d556` is closed as **partial (51/235 answered)**, stopped by repeated
Gemini 503 and low memory. The **offline holdout numbers matched all 51 live cases** (0 route
and 0 escalation differences), so the offline holdout numbers stand.

**Case `legacy-oos-weather` failed on every attempt that reached it** (2 of 2). Both times it
was the 503 fallback, and the second attempt hung for about 21 minutes first. This may be a
case-specific issue (an off-topic weather question on the knowledge path); **not
investigated**.

## If the live holdout is ever finished (not planned)

1. Let the auto-resume finish after/holdout, or resume by hand (server on `eval-fixes`):
   `uv run evalkit run --dataset datasets/golden_v1.2.jsonl --ids @datasets/splits/agent_holdout_ids.txt --resume 20261016-111409-d556 --no-cache --concurrency 1 --max-rpm 10 --max-llm-calls 200 --max-retries 0 --stop-on-failure --agent-repo ../rag-whatsapp-agent`
2. Switch the server to `33b240d` and run before/holdout (about 112 calls).
3. Rebuild the report with live holdout runs:
   `uv run evalkit agent-fix-report --dev-before 20261015-212802-d8f4 --dev-before-2 20261015-214735-83c2 --dev-after 20261015-220416-d3d9 --holdout-before <before/holdout run> --holdout-after 20261016-111409-d556`
