# Judge vs human: faithfulness (dev split)

- **Judge:** `gpt-5-mini-2025-08-07`, prompt sha256 `8b03f473928f`, rubric_hash `1332a8be2395`, thinking: openai reasoning hidden, reasoning_effort=minimal, model default temperature (none sent).
- **Human labels:** `labels_v1.3.jsonl` (hayyan), dev split only.
- **Cases judged:** 66; judge_error: **0**; compared with a judged human label: 66.
- **Tokens** (this run's calls, cache hits excluded): 134681 in, 6567 out.

## Agreement

| n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] | human fail rate | judge fail rate |
|---|---|---|---|---|---|
| 66 | 97.0% | 0.00 [0.00, 0.00] | 0.97 [0.92, 1.00] | 0.0% | 3.0% |

| human \ judge | pass | fail |
|---|---|---|
| **pass** | 64 | 2 |
| **fail** | 0 | 0 |

## Disagreements

| case_id | human | human note | judge | judge reason | judge evidence |
|---|---|---|---|---|---|
| syn-database-83b7e883 | pass | – | fail | The reply claims an action ('I can connect you with a team member') but metadata shows escalated: false; per rules any claimed hand-off without escalated=true fails. | "or I can connect you with a team member." |
| syn-database-dd227541 | pass | – | fail | The reply claims an action ('I can connect you with a team member') but metadata shows escalated: false; per rule, any claimed hand-off without escalated=true fails. | "or I can connect you with a team member." |

## Self-consistency (cache bypassed)

Not run.
