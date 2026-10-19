# Judge vs human: completeness (dev split)

- **Judge:** `gpt-5-mini-2025-08-07`, prompt sha256 `5c37709768a5`, rubric_hash `1332a8be2395`, thinking: openai reasoning hidden, reasoning_effort=minimal, model default temperature (none sent).
- **Human labels:** `labels_v1.3.jsonl` (hayyan), dev split only.
- **Cases judged:** 50; judge_error: **0**; compared with a judged human label: 48.
- **Tokens** (this run's calls, cache hits excluded): 85176 in, 3838 out.

## Agreement

| n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] | human fail rate | judge fail rate |
|---|---|---|---|---|---|
| 48 | 95.8% | 0.48 [-0.03, 1.00] | 0.95 [0.88, 1.00] | 4.2% | 4.2% |

| human \ judge | pass | fail |
|---|---|---|
| **pass** | 45 | 1 |
| **fail** | 1 | 1 |

## Disagreements

| case_id | human | human note | judge | judge reason | judge evidence |
|---|---|---|---|---|---|
| syn-guardrail-55403122 | pass | – | fail | Single-part knowledge/database question expected the database answer but reply contradicts the known availability (answer_available=true) — the response gives a price that doesn't match the source; this is incorrect coverage per rule that knowledge/database questions must provide the correct answer. | The Barrier Repair Cream costs $32.50. |
| syn-knowledge-812bf00a | fail | Informational question treated as a human request and escalated. | pass | Single-part question was answered: the reply states the handoff is happening and a human will follow up shortly, which addresses 'when' the chatbot will hand over. | I'm passing your conversation to a GlowCare team member, who will follow up with you shortly. |

## Self-consistency (cache bypassed)

Not run.

## v2 vs v3 (same labels, same dev cases)

| | v2 | v3 |
|---|---|---|
| n | 48 | 48 |
| % agreement | 89.6% | 95.8% |
| Cohen's kappa [95% CI] | 0.24 [-0.07, 0.66] | 0.48 [-0.03, 1.00] |
| Gwet's AC1 [95% CI] | 0.88 [0.76, 0.98] | 0.95 [0.88, 1.00] |
| human pass / judge fail | 4 | 1 |
| human fail / judge pass | 1 | 1 |
| both pass / both fail | 42 / 1 | 45 / 1 |
| judge fail rate | 10.4% | 4.2% |

**Fixed in v3 (3):** legacy-db-ingredients-cleanser, legacy-db-price-hydra, legacy-rob-caps
**Still disagreeing (2):** syn-guardrail-55403122, syn-knowledge-812bf00a

**New in v3 (0):**
none
