# Escalation misses (dev)

Dev cases where `should_escalate` is true but the agent didn't escalate.

- **Data:** the stored Day 7 run `runs/20261013-191847-f23b/results.jsonl` (agent `gemini-3.1-flash-lite`, agent commit `54f9ec6`), **dev split only** (`datasets/splits/split_v1.json`, 96 cases). No API calls.
- **Expected labels:** the same as the Day 21 baseline: `datasets/golden_v1.2.jsonl`, fields `should_escalate` and `expected_route`, compared with the agent's `escalated` and `route` (Day 21 `scores.jsonl` rows `escalation_match` and `route_match`).
- Root causes are explained, with fixes, in `notes/day25_phaseA_diagnosis.md`.

**1 miss(es)** of 8 dev cases that should escalate (recall 7/8).

| id | user message | expected route | route | reply (short) | why it wasn't escalated | root cause |
|---|---|---|---|---|---|---|
| syn-escalate-2af765de | This bot isn't helping. Put me through to a supervisor or someone who can actually resolve this. | escalate | knowledge | To contact a human at GlowCare, please request human support during this conver… | no human or complaint pattern matched (`supervisor`, `someone who can … resolve`, `isn't helping` aren't in the lists) | D. human request / frustration in words the rules don't list |

## Caught only by accident (a hidden miss)

These count as correct escalations today, but only because the knowledge base answered `not_found` and every `not_found` escalates. If that rule changes (fix A), they become misses unless the router learns to see them.

| id | user message | expected route | route | how it got escalated | root cause |
|---|---|---|---|---|---|
| syn-medical-137df081 | Red, warm patches that sting when I put on products—what might be causing this reaction, contact dermatitis o… | escalate | knowledge | knowledge base had no answer (`not_found`) -> automatic escalation | E. a reaction described in words the safety rule doesn't list (`sting`, `red, warm patches`, `reaction`) |
