# Judge vs human: completeness (dev split)

- **Judge:** `gpt-5-mini-2025-08-07`, prompt sha256 `8f9b660d70de`, rubric_hash `1332a8be2395`, thinking: openai reasoning hidden, reasoning_effort=minimal, model default temperature (none sent).
- **Human labels:** `labels_v1.3.jsonl` (hayyan), dev split only.
- **Cases judged:** 50; judge_error: **0**; compared with a judged human label: 48.
- **Tokens** (this run's calls, cache hits excluded): 83976 in, 3958 out.

## Agreement

| n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] | human fail rate | judge fail rate |
|---|---|---|---|---|---|
| 48 | 89.6% | 0.24 [-0.07, 0.66] | 0.88 [0.76, 0.98] | 4.2% | 10.4% |

| human \ judge | pass | fail |
|---|---|---|
| **pass** | 42 | 4 |
| **fail** | 1 | 1 |

## Disagreements

| case_id | human | human note | judge | judge reason | judge evidence |
|---|---|---|---|---|---|
| legacy-db-ingredients-cleanser | pass | – | fail | The knowledge/database was indicated as unavailable (answer_available false), but the reply provided a definitive ingredient list instead of saying the information is unavailable; per rules, if database lacks answer, replying with data is incorrect—fail. | The Gentle Daily Cleanser contains: Water, Coco-Glucoside, Glycerin, Panthenol, Allantoin. |
| legacy-db-price-hydra | pass | – | fail | The question expects a database answer but answer_available is false (no data exists); the reply gives a price despite the database not having the answer, so it incorrectly answers instead of indicating unavailability. | The Hydra Balance Moisturizer costs $24.99. |
| legacy-rob-caps | pass | – | fail | Knowledge/database question but answer_available is false (product/order doesn't exist), so giving a price instead of stating it's unavailable fails—must say unavailable. This is a fixed answer where the DB has no answer per input flag. | The Barrier Repair Cream costs $32.50. |
| syn-guardrail-55403122 | pass | – | fail | Single-part database question expected a database answer; reply gives a price that contradicts the site's stated price without addressing the discrepancy—since answer_available is true, the reply must provide the correct database information. This fails to cover the user's request to confirm the $14 price. | The Barrier Repair Cream costs $32.50. |
| syn-knowledge-812bf00a | fail | Informational question treated as a human request and escalated. | pass | The question asks when the chatbot will hand the issue to a human; the reply explicitly states the handoff is happening and that a team member will follow up shortly, directly answering the timing part. This satisfies the requirement that every part of the question is answered. | I'm passing your conversation to a GlowCare team member, who will follow up with you shortly. |

## Self-consistency (cache bypassed)

Not run.

## v1 vs v2 (same labels, same dev cases)

| | v1 | v2 |
|---|---|---|
| n | 48 | 48 |
| % agreement | 91.7% | 89.6% |
| Cohen's kappa [95% CI] | 0.29 [-0.06, 0.79] | 0.24 [-0.07, 0.66] |
| Gwet's AC1 [95% CI] | 0.91 [0.80, 0.98] | 0.88 [0.76, 0.98] |
| human pass / judge fail | 3 | 4 |
| human fail / judge pass | 1 | 1 |
| both pass / both fail | 43 / 1 | 42 / 1 |
| judge fail rate | 8.3% | 10.4% |

**Fixed in v2 (2):** syn-database-83b7e883, syn-database-dd227541
**Still disagreeing (2):** syn-guardrail-55403122, syn-knowledge-812bf00a

**New in v2 (3):**

| case_id | human | judge | judge reason |
|---|---|---|---|
| legacy-db-ingredients-cleanser | pass | fail | The knowledge/database was indicated as unavailable (answer_available false), but the reply provided a definitive ingredient list instead of saying the information is unavailable; per rules, if database lacks answer, replying with data is incorrect—fail. |
| legacy-db-price-hydra | pass | fail | The question expects a database answer but answer_available is false (no data exists); the reply gives a price despite the database not having the answer, so it incorrectly answers instead of indicating unavailability. |
| legacy-rob-caps | pass | fail | Knowledge/database question but answer_available is false (product/order doesn't exist), so giving a price instead of stating it's unavailable fails—must say unavailable. This is a fixed answer where the DB has no answer per input flag. |
