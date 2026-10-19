# Day 19: the final frozen-judge evaluation on the test split

**The test split is now USED.** It was loaded once, for this evaluation, and the frozen judge
prompts can't be tuned against it any more: **any future judge prompt change needs a new test
set.** `prompts/judges/FROZEN.json` records this under `test_split_used` (date, commit,
reason, run, report, calls, cost). `evalkit judge freeze` keeps that record, and
`evalkit judge final` refuses to run again.

Full report: [reports/judges/FINAL_test_v1.md](../reports/judges/FINAL_test_v1.md).

## First: relevance v3 completed on dev

The OpenAI relevance v3 results covered 65 of 96 dev cases. The other 31 cost 31 calls
(about $0.01; the 65 were cache hits). On all 96 dev cases v3 scores **86.5%, kappa 0.61,
AC1 0.79 [0.67, 0.89]**, against v2's 90.6% / 0.71 / 0.86. The bias check
(`reports/judges/bias_by_source_dev.md`) now uses the full file: non-OpenAI 100.0% / 1.00,
synthetic 80.9% / 0.68, still the only dev flag. v3 stayed frozen (Day 17 decision).

## How the run was guarded

`evalkit judge final` (new; `src/evalkit/judges/final.py`) does these steps in order:

1. Refuses if FROZEN.json already records a test use.
2. Checks **FROZEN.json against the prompt files** (version, prompt_sha256, rubric_hash, input
   fields, judge skips) and runs the **leakage check** (no test case_id, reply or question in
   `prompts/` or `src/evalkit/judges/`). It aborts if either fails. Both passed.
3. Builds the plan for the 6 frozen prompts on the 64 test cases. The plan needs only the
   cases and the agent's replies, no labels (`build_plan(..., final=True)`; without
   `final` the test split is still refused).
4. Dry run: 282 calls, about $0.18 (approved: $0.50 / 500 calls, OpenAI gpt-5-mini only,
   frozen prompts only).
5. Judges everything and saves the results to
   `runs/20261013-191847-f23b/judges/final_test/`. It would have stopped on a refusal, the
   budget, or more than 5 judge_errors, **before any test label was loaded**.
6. **Only then** loads the test labels, once: `load_labels("test", final=True, reason="Week
   3 final frozen-judge evaluation")` with `EVALKIT_FINAL_TEST=1`. That is the one line in
   `reports/test_access.log` (2026-10-15T15:18:34Z, commit `6e30712`).
7. Writes the report and marks the test split as used.

Actual cost: **290 API calls** (8 repair retries), **0 judge_error**, about **$0.09**.

## Results: dev vs test (OpenAI gpt-5-mini, frozen prompts, labels_v1.3)

| Criterion | dev n / % / AC1 | test n / % / AC1 [95% CI] | test kappa | human / judge fails (test) |
|---|---|---|---|---|
| relevance v3 | 96 / 86.5% / 0.79 | 64 / 85.9% / **0.78** [0.63, 0.91] | 0.60 | 12 / 17 |
| completeness v3 | 48 / 95.8% / 0.95 | 32 / 93.8% / **0.93** [0.81, 1.00] | 0.47 | 2 / 2 |
| safety v3 | 96 / 95.8% / 0.96 | 64 / 100.0% / **1.00** | 1.00 | 1 / 1 |
| tone v2 | 96 / 83.3% / 0.80 | 64 / 84.4% / **0.81** [0.67, 0.92] | 0.25 | 2 / 12 |
| faithfulness v2 | 66 / 98.5% / 0.98 | 39 / 100.0% / **1.00** | undefined | 0 / 0 |
| correctness v2 | 32 / 100.0% / 1.00 | 18 / 88.9% / **0.85** [0.60, 1.00] | 0.61 | 2 / 4 |

**Test came out about the same as dev, not lower:** within ±0.04 AC1 on five criteria.
Correctness is the exception, with 2 disagreements on n=18. Dev tuning doesn't seem to have
inflated the dev numbers much. With these few fails, though, the CIs are too wide to tell
small differences apart.

## What it says

- **Relevance:** AC1 0.78. 7 judge-strict and 2 judge-lenient disagreements, the same
  patterns as dev: hand-offs on medical / guardrail questions read as "not addressing", and
  the clarify-rule misreading (syn-clarify-fb1bc373).
- **Tone is over-strict:** 12 judge fails vs 2. All 10 disagreements (and all 16 on dev) are
  human pass / judge fail; 9 cite the "acknowledge an upset customer" rule. Its high AC1
  comes from the many agreed passes; kappa is 0.25.
- **Correctness:** two judge fails for leaving out a reference fact (syn-knowledge-6c46c07c,
  syn-knowledge-e3aee33b). Both are hayyan passes.
- **Safety and faithfulness:** perfect agreement, but only 1 and 0 human fails. Fail recall
  comes from the synthetic negatives (14/15, 20/20).

## Bias split on test

Four criteria are flagged (relevance, completeness, tone, correctness; dev flagged only
relevance). **The direction is stricter, not more lenient:** of the 22 disagreements on
synthetic (OpenAI-generated) cases, 19 are human pass / judge fail and 3 human fail / judge
pass. Self-preference would show up as leniency on its own family's cases, and that is not
what the data shows. The synthetic cases are the adversarial ones (guardrail, medical,
clarify) and hold nearly all the human fails, so agreement is lower there for any judge. The
Day 18 evidence of leniency (4 dev relevance cases vs Qwen) stays weak; on test, 2 lenient
relevance cases repeat the dev patterns.

## Open for hayyan (nothing relabeled)

- The 2 correctness disagreements: is the omitted fact central?
- syn-knowledge-926b08ac is labeled relevance pass and completeness fail, with the same note.
  Possibly inconsistent.
- Tone: accept the over-strict judge with a caveat in reporting, or plan a v3 tone prompt.
  **A v3 can't be validated on this test split any more: it needs a new test set.**

## Limitations (also in the report)

- Few human fails outside relevance.
- OpenAI generated the synthetic cases and is the judge.
- Tone misses role-play breaks (2/5 caught in the synthetic negatives).
- One labeler.
- One agent run.
- Correctness judged only where there is a reference.
