# Day 13: dev/test split of the human labels

No LLM or agent calls, no label changed, no DVC (MANIFEST.json + local git tags). Nothing
pushed.

## What was built

- **`evalkit split make`** (`evalkit.calibration.split`) -> `datasets/splits/split_v1.json`:
  the 160 labeled case_ids of `labels_v1.1.jsonl` split into **dev 96 / test 64** (seed
  42). Case_ids only, no label copies: the split is keyed by case_id, so labels can be
  re-versioned (v1.2, v2...) without re-splitting. The file records the seed, method,
  labels file + sha256 + version (1.1), rubric_hash (v2, with 8 labels current and 152
  carried forward), the counts per stratum and criterion, the rare-fail coverage, and the
  agreement metadata below. MANIFEST entry `splits/split_v1.json` (new kind `splits`;
  records = case_ids). Local tag **`labels-v1.1-split-v1`**.
- **`evalkit.calibration.loader.load_labels`**: dev loads freely; **test is locked**.
- **`evalkit.calibration.leakage`**: `few_shot_examples` (dev and rubric examples only) and
  **`evalkit split check`**, also a CI step.
- `split` task in `scripts/tasks.ps1` and the Makefile; reproduction steps in
  `datasets/README.md`.

## How the split was made

1. **Rare-fail placement.** The rare-fail criteria are those with at least one but fewer
   than 20 fails (the Day 12 rule, computed from the labels, so faithfulness joins as soon
   as it gets a fail): safety (2), tone (3), correctness (4), completeness (4). Fewest
   fails first, each gets one fail case in test and one in dev if it doesn't have one
   there yet. Correctness and completeness fail on the same 4 cases.
2. **Stratified fill** by (expected route, has any fail): test gets 64 places, and each
   stratum gets as close to 40% of its cases as the step-1 placements allow (a case
   already placed in test counts toward its stratum's share; plain largest remainder
   would have added the share on top and put 3 of the 3 `(none)|fail` cases in test).
3. **No rubric example** (`rubrics/examples_used.json`, 33 ids) is in either split:
   `make_split` refuses one, and a test checks the committed file.

## Counts

| Expected route | dev (with a fail) | test (with a fail) |
|---|---|---|
| knowledge | 39 (3) | 25 (1) |
| database | 11 (1) | 8 (1) |
| clarify | 9 (3) | 5 (2) |
| medical | 9 (5) | 4 (2) |
| escalate | 8 (1) | 6 (1) |
| greeting | 8 (7) | 6 (5) |
| guardrail | 8 (4) | 6 (3) |
| route not checked | 4 (1) | 4 (2) |
| **all** | **96 (25)** | **64 (17)** |

Every stratum is within one case of its 40% share in test. The largest gap is
`(none)|fail` (2 of 3 in test, target 1.2), forced by the rare placements: the safety fail
and a tone fail placed in test (syn-medical-b7f4d113, syn-guardrail-997dc058) both have a
null expected route.

| Criterion | dev pass / fail / n_a | test pass / fail / n_a |
|---|---|---|
| safety | 95 / 1 / 0 | 63 / 1 / 0 |
| escalation_appropriate | 74 / 20 / 2 | 47 / 13 / 4 |
| faithfulness | 66 / 0 / 30 | 39 / 0 / 25 |
| correctness | 48 / 2 / 46 | 31 / 2 / 31 |
| completeness | 46 / 2 / 48 | 30 / 2 / 32 |
| relevance | 78 / 18 / 0 | 52 / 12 / 0 |
| tone | 95 / 1 / 0 | 62 / 2 / 0 |

## Rare-fail coverage

| Criterion | fails | dev | test | |
|---|---|---|---|---|
| safety | 2 | 1 | 1 | in both |
| tone | 3 | 1 | 2 | in both |
| correctness | 4 | 2 | 2 | in both |
| completeness | 4 | 2 | 2 | in both (same cases as correctness) |
| faithfulness | 0 | 0 | 0 | no fails |

## Agreement metadata (for reporting only)

The split file records which cases are in the labeller_b overlap set and, per case, the
criteria where hayyan and labeller_b disagree, both for round 1 (`labels_v1` vs labeller_b, the
official 51 disagreements) and current (`labels_v1.1` vs labeller_b, the post-hoc 45). Later
reports can show judge scores on agreed cases separately.

| | overlap cases | disagree (round 1) | agreed (round 1) | disagree (current) | agreed (current) |
|---|---|---|---|---|---|
| dev | 27 | 13 | 14 | 9 | 18 |
| test | 23 | 10 | 13 | 9 | 14 |

## How the test lock works

`load_labels("test")` raises `TestSplitLocked` unless **all three** hold:

- the call passes `final=True`;
- the call passes a non-empty `reason`;
- the environment has `EVALKIT_FINAL_TEST=1`.

One switch is in the code and one in the shell, so a test load can't happen by accident or
from a stray notebook cell. Every successful test load appends a tab-separated line to
**`reports/test_access.log`**: UTC time, git commit, caller (module:function:line) and the
reason. The log is committed, so any peek at the test labels shows up in git. Dev loads
never touch it. Today the log doesn't exist: the test split has not been loaded.

## Leakage rules for Week 3

1. **Develop on dev only.** Judge prompts, rubric wording, thresholds and any tuning are
   iterated against `load_labels("dev")`. Test is loaded once per reported evaluation,
   with a reason, and the access log line is committed with the results.
2. **Examples come only from dev or the rubric examples**, through
   `leakage.few_shot_examples` (it checks the result against the test split). Never copy
   a case into a prompt by hand.
3. **`evalkit split check` must pass** (CI runs it): no file under `prompts/` or
   `src/evalkit/judges/` may contain a test case_id, or a test reply or question. Texts
   count when they have at least 40 characters and are unique to test (not a dev reply or
   question, not quoted in the rubrics), compared case-insensitively with whitespace
   collapsed. The shared fixed replies (the not_found text, greetings) are allowed. Today:
   11 files, 64 case_ids, 27 replies and 40 questions checked, no hit.
4. **Rubric changes after seeing test results** are a new rubric version (versions.json)
   and need a fresh look at dev first; never adjust a rubric to fix a test score.
5. **New labels** (v1.2, a second labeler, hayyan-r2) reuse split_v1 by case_id; the split
   is only re-made (split_v2) if the labeled set itself changes.

## Limitation

Fails are rare: in **test**, safety has 1 fail, tone 2, correctness 2, completeness 2, and
faithfulness none (dev: 1, 1, 2, 2, 0). Judge scores on those criteria (agreement with the
human labels, fail recall) will rest on one or two fail cases and have **very wide
confidence intervals**, or be undefined (faithfulness). They will be reported with their
CIs and n, never as a single number. As decided on Day 8 and repeated in the Day 9 Label
balance section, **clearly marked synthetic negatives** go in a **separate file** (e.g.
replies edited to contradict the retrieved chunks, or to state a wrong price), are reported
separately, and are never mixed into the human labels or this split.
