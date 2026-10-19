# Judge vs human: correctness (dev split)

- **Judge:** `gpt-5-mini-2025-08-07`, prompt sha256 `85ce000ff61d`, rubric_hash `1332a8be2395`, thinking: openai reasoning hidden, reasoning_effort=minimal, model default temperature (none sent).
- **Human labels:** `labels_v1.3.jsonl` (hayyan), dev split only.
- **Cases judged:** 32; judge_error: **0**; compared with a judged human label: 32.
- **Tokens** (this run's calls, cache hits excluded): 55977 in, 2631 out.

## Agreement

| n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] | human fail rate | judge fail rate |
|---|---|---|---|---|---|
| 32 | 100.0% | 1.00 [1.00, 1.00] | 1.00 [1.00, 1.00] | 9.4% | 9.4% |

| human \ judge | pass | fail |
|---|---|---|
| **pass** | 29 | 0 |
| **fail** | 0 | 3 |

## Disagreements

None.

## Self-consistency (cache bypassed)

Not run.

## v1 vs v2 (same labels, same dev cases)

| | v1 | v2 |
|---|---|---|
| n | 50 | 32 |
| % agreement | 94.0% | 100.0% |
| Cohen's kappa [95% CI] | 0.64 [0.00, 1.00] | 1.00 [1.00, 1.00] |
| Gwet's AC1 [95% CI] | 0.93 [0.83, 1.00] | 1.00 [1.00, 1.00] |
| human pass / judge fail | 3 | 0 |
| human fail / judge pass | 0 | 0 |
| both pass / both fail | 44 / 3 | 29 / 3 |
| judge fail rate | 12.0% | 9.4% |

**Fixed in v2 (3):** legacy-db-ingredients-cleanser, legacy-rob-caps, syn-knowledge-c1182cf0
**Still disagreeing (0):** none

**New in v2 (0):**
none
