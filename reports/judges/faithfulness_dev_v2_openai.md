# Judge vs human: faithfulness (dev split)

- **Judge:** `gpt-5-mini-2025-08-07`, prompt sha256 `a47ebf15e7bc`, rubric_hash `1332a8be2395`, thinking: openai reasoning hidden, reasoning_effort=minimal, model default temperature (none sent).
- **Human labels:** `labels_v1.3.jsonl` (hayyan), dev split only.
- **Cases judged:** 66; judge_error: **0**; compared with a judged human label: 66.
- **Tokens** (this run's calls, cache hits excluded): 136800 in, 6320 out.

## Agreement

| n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] | human fail rate | judge fail rate |
|---|---|---|---|---|---|
| 66 | 98.5% | 0.00 [0.00, 0.00] | 0.98 [0.95, 1.00] | 0.0% | 1.5% |

| human \ judge | pass | fail |
|---|---|---|
| **pass** | 65 | 1 |
| **fail** | 0 | 0 |

## Disagreements

| case_id | human | human note | judge | judge reason | judge evidence |
|---|---|---|---|---|---|
| syn-database-83b7e883 | pass | – | fail | Reply claims an action offer ('I can connect you with a team member') while metadata shows escalated:false; wording implies a hand-off which per rules must match escalated true to pass. | "or I can connect you with a team member." |

## Self-consistency (cache bypassed)

Not run.

## v1 vs v2 (same labels, same dev cases)

| | v1 | v2 |
|---|---|---|
| n | 66 | 66 |
| % agreement | 97.0% | 98.5% |
| Cohen's kappa [95% CI] | 0.00 [0.00, 0.00] | 0.00 [0.00, 0.00] |
| Gwet's AC1 [95% CI] | 0.97 [0.92, 1.00] | 0.98 [0.95, 1.00] |
| human pass / judge fail | 2 | 1 |
| human fail / judge pass | 0 | 0 |
| both pass / both fail | 64 / 0 | 65 / 0 |
| judge fail rate | 3.0% | 1.5% |

**Fixed in v2 (1):** syn-database-dd227541
**Still disagreeing (1):** syn-database-83b7e883

**New in v2 (0):**
none
