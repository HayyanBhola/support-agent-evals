# Judge bias check: agreement by case source (dev split)

- **Judge:** `gpt-5-mini-2025-08-07` on the frozen prompts; saved results only, no new judge calls.
- **Human labels:** `labels_v1.3.jsonl` (hayyan), dev split.
- **Why:** OpenAI generated the synthetic cases (Day 5) and is also the judge, so it might score its own family's cases differently. **non-OpenAI** = legacy + known_bugs cases; **synthetic** = OpenAI-generated.
- **Flag:** an AC1 gap above 0.15 or an agreement gap above 10 points (gap = non-OpenAI minus synthetic). With few cases or few fails per side, a gap can be noise: read it with n.

| Criterion | Prompt | non-OpenAI n | non-OpenAI % agreement | non-OpenAI AC1 | synthetic n | synthetic % agreement | synthetic AC1 | Gap (AC1 / pts) | Flag |
|---|---|---|---|---|---|---|---|---|---|
| relevance | v3 | 28 | 100.0% | 1.00 | 68 | 80.9% | 0.68 | +0.32 / +19.1 | **large gap** |
| completeness | v3 | 17 | 100.0% | 1.00 | 31 | 93.5% | 0.93 | +0.07 / +6.5 | – |
| safety | v3 | 28 | 92.9% | 0.92 | 68 | 97.1% | 0.97 | -0.05 / -4.2 | – |
| tone | v2 | 28 | 89.3% | 0.88 | 68 | 80.9% | 0.76 | +0.12 / +8.4 | – |
| faithfulness | v2 | 21 | 100.0% | 1.00 | 45 | 97.8% | 0.98 | +0.02 / +2.2 | – |
| correctness | v2 | 0 | – | – | 32 | 100.0% | 1.00 | – | too few cases |

## Human fails by source (the confound)

Where the human fails sit decides what each side can show: a side with no fails can only show agreement on passes.

| Criterion | non-OpenAI: human fails / judged | synthetic: human fails / judged |
|---|---|---|
| relevance | 3 / 28 | 18 / 68 |
| completeness | 0 / 17 | 2 / 31 |
| safety | 0 / 28 | 1 / 68 |
| tone | 0 / 28 | 1 / 68 |
| faithfulness | 0 / 21 | 0 / 45 |
| correctness | 0 / 17 | 3 / 33 |
