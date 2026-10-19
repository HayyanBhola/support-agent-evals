# Judge vs human: correctness (dev split)

- **Judge:** `gpt-5-mini-2025-08-07`, prompt sha256 `364d1599a1af`, rubric_hash `1332a8be2395`, thinking: openai reasoning hidden, reasoning_effort=minimal, model default temperature (none sent).
- **Human labels:** `labels_v1.3.jsonl` (hayyan), dev split only.
- **Cases judged:** 51; judge_error: **0**; compared with a judged human label: 50.
- **Tokens** (this run's calls, cache hits excluded): 88260 in, 4068 out.

## Agreement

| n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] | human fail rate | judge fail rate |
|---|---|---|---|---|---|
| 50 | 94.0% | 0.64 [0.00, 1.00] | 0.93 [0.83, 1.00] | 6.0% | 12.0% |

| human \ judge | pass | fail |
|---|---|---|
| **pass** | 44 | 3 |
| **fail** | 0 | 3 |

## Disagreements

| case_id | human | human note | judge | judge reason | judge evidence |
|---|---|---|---|---|---|
| legacy-db-ingredients-cleanser | pass | – | fail | The case has gold_db_facts empty and no reference_answer, so expected verdict is n_a; however the reply provided specific ingredient list (a knowledge/database answer) when no gold facts or reference exist — per rubric, when no reference/gold and not a greeting, judge as fail for giving database info without gold. | The Gentle Daily Cleanser contains: Water, Coco-Glucoside, Glycerin, Panthenol, Allantoin. |
| legacy-rob-caps | pass | – | fail | There is no reference_answer or gold_db_facts provided, so this case expects n_a; the reply gave a specific price (a database answer) even though no database facts were supplied—must be n_a per rubric. | The Barrier Repair Cream costs $32.50. |
| syn-knowledge-c1182cf0 | pass | – | fail | Reply adds a specific 2-hour free cancellation window and 'not yet entered processing' condition that are not present in the reference_answer; this contradicts or extends the central fact. The rubric: 'The reply contradicts the reference_answer or leaves out its central fact' -> fail. | Orders can be cancelled free of charge within 2 hours of being placed, as long as the order has not yet entered processing. |

## Self-consistency (cache bypassed)

Not run.
