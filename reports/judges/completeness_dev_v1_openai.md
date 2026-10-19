# Judge vs human: completeness (dev split)

- **Judge:** `gpt-5-mini-2025-08-07`, prompt sha256 `803c19595465`, rubric_hash `1332a8be2395`, thinking: openai reasoning hidden, reasoning_effort=minimal, model default temperature (none sent).
- **Human labels:** `labels_v1.3.jsonl` (hayyan), dev split only.
- **Cases judged:** 50; judge_error: **0**; compared with a judged human label: 48.
- **Tokens** (this run's calls, cache hits excluded): 83426 in, 3854 out.

## Agreement

| n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] | human fail rate | judge fail rate |
|---|---|---|---|---|---|
| 48 | 91.7% | 0.29 [-0.06, 0.79] | 0.91 [0.80, 0.98] | 4.2% | 8.3% |

| human \ judge | pass | fail |
|---|---|---|
| **pass** | 43 | 3 |
| **fail** | 1 | 1 |

## Disagreements

| case_id | human | human note | judge | judge reason | judge evidence |
|---|---|---|---|---|---|
| syn-database-83b7e883 | pass | – | fail | The knowledge/database has the answer (answer_available true), but the reply returns a not-found response instead of providing the order status, which per rules is a fail. | I couldn't find an order with the number GC10250. |
| syn-database-dd227541 | pass | – | fail | The knowledge/database has the answer (answer_available true) but the reply returned 'not found' without providing the requested status — per rules this is a fail. | I couldn't find an order with the number GC10001. |
| syn-guardrail-55403122 | pass | – | fail | The reply contradicts the database expectation: the knowledge base has the answer but the response gives a conflicting price without acknowledging or explaining; also it does not cover the user's asked confirmation if $14 is correct. (Every part must be answered and KB questions must not be a fixed/incorrect reply.) | The Barrier Repair Cream costs $32.50. |
| syn-knowledge-812bf00a | fail | Informational question treated as a human request and escalated. | pass | The user asked when hand-off will occur; the reply states the hand-off is happening now and that a team member will follow up shortly, answering the timing. This covers the single-part question. (Every part answered.) | I'm passing your conversation to a GlowCare team member, who will follow up with you shortly. |

## Self-consistency (cache bypassed)

Not run.
