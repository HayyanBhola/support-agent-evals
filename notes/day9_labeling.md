# Day 9: the labeling tool

No LLM or agent calls, no Docker, no Argilla: a small local Streamlit app over
`evalkit.labeling`, working only from saved files. Nothing pushed.

## The sample: `datasets/label_sample_v1.jsonl` (160 records)

`evalkit label sample` (seed 42) draws from run `20261013-191847-f23b` joined by case_id to
**golden_v1.2** (its gold fields). Candidates: the 395 answered responses (skipped and
errored excluded) minus the 33 rubric example case_ids in `rubrics/examples_used.json`
(labeling those would leak the rubric's examples into judge evaluation) = **362**.

Allocation: knowledge / `llm` answers (where hallucinations happen) get a fixed oversampled
quota of 60; every other expected route gets at least 10 (route-not-checked at least 6);
the rest is shared in proportion to what's left; inside a route, answer_type strata get
proportional shares with at least one each.

| Expected route | Sampled | of | | Expected route | Sampled | of |
|---|---|---|---|---|---|---|
| knowledge | **64** (llm 60, not_found 2, quoted 1, none 1) | 107 | | guardrail | 14 | 35 |
| database | 19 | 79 | | escalate | 14 | 34 |
| greeting | 14 | 33 | | clarify | 14 | 31 |
| medical | 13 | 29 | | route not checked | 8 | 14 |

Knowledge came out at 64, not the ~76 I estimated in the plan: the at-least-one-per-
stratum floors in the other routes take budget from knowledge's non-LLM strata. The full
strata table is printed by the command and recorded in MANIFEST.json (with run_id,
dataset_version `golden_v1.2@1.2`, seed, rubric_hash, candidate count).

Each record is a `LabelSample` (new schema): case_id, run_id, stratum, the full TestCase,
the saved response, and the response's sha256.

## How to run the app

Install the optional group once (keeps the core install light), then start it in its own
terminal from the repo root:

```
uv sync --group labeling          # once
uv run --group labeling streamlit run app/labeler.py -- --labeler hayyan
```

(or `.\scripts\tasks.ps1 label -Labeler hayyan`, or `make label LABELER=hayyan`). The
labeler name is required; a second labeler (Day 12) uses their own name.

## How to label

Per record the app shows the question, history, the reply, actual route, answer_type,
escalated, the retrieved chunk texts, the DB facts from `details` (product rows or the order
record), the expected route, should_escalate, the reference answer and gold DB facts. **No
automated scores** (rule checks, route match) are shown, so you're not anchored.

- Seven criteria as pass / fail / n_a radios, in the judging order of rubrics/README.md
  (safety, escalation_appropriate, faithfulness, correctness, completeness, relevance,
  tone). The sidebar shows any criterion's definition, Pass if, Fail if and Edge cases, read
  live from the rubric files.
- Criteria the rubric marks n_a for this record (applies_by / applies_to / applies_when)
  are pre-set to n_a and greyed out; tick **override** to judge one anyway (saved in
  `overrides`, counted by `evalkit labels status`).
- A **note is required** whenever any verdict is fail (one short reason); it feeds the
  Day 14 failure patterns.
- **Save** appends one Label immediately and moves to the next unlabeled record. The
  current case_id is shown large right above the button ("Saving to: ...", and on the button
  itself), and every save confirms the case_id, all 7 saved verdicts and the note, so a save
  to the wrong record is obvious. Navigation: Back, Next unlabeled, jump to a case_id
  (spaces are stripped; an unknown or empty id shows a red error instead of silently
  staying on the current record), and a filter: unlabeled / labeled / labeled under an
  older rubric_hash / all. A progress bar shows done/160; a break reminder every 40
  records in a session. The app resumes where you left off after a restart.

## Storage and the time-per-record field

`datasets/labels/labels_raw_v1.jsonl` is append-only: one Label line per save, flushed and
fsynced, never edited; for each (case_id, labeler) the latest line wins. A line cut off by a
crash is dropped on the next open. Each Label (schema changed today, before any label data
existed) holds case_id, run_id, labeler, timestamp (UTC), the rubric_hash at labeling time,
the sha256 of the labeled response, a verdict for all 7 criteria, overrides, notes, and
**`seconds_spent`**: seconds from the moment the record was shown to the moment it was
saved (it includes reading time and any pause, so take medians, not means, and expect
outliers after breaks). A re-save records its own (usually short) time on its own row;
**first-pass time** is read from the earliest row per (case_id, labeler)
(`workflow.first_pass_timing`, printed by `evalkit labels status`), so corrections don't
replace it.

## When the rubric changes mid-labeling

Every label stores the rubric_hash it was given under. After you edit a rubric file (the
hash changes; `evalkit rubric check` prints the new one), earlier labels are **not** changed
or lost: they are flagged as "labeled under an older rubric_hash". Use that filter in the app
to re-check them (re-saving appends a new row with the new hash, which then wins), and
`evalkit labels status` shows how many remain. Exported labels keep their own hash, so labels
from different rubric versions are never silently mixed.

## CLI

- `evalkit labels status [--labeler NAME]`: done / remaining per expected route, overrides,
  labels under an older rubric_hash, first-pass time per record.
- `evalkit labels validate`: errors for a fail without a note, a label for a case not in the
  sample, or a changed response (sha mismatch); warnings when a verdict disagrees with
  applicability without an override. Every label has all 7 criteria by schema.
- `evalkit labels export` -> `datasets/labels_v1.jsonl` (latest per case per labeler,
  sorted) plus a MANIFEST entry (labelers, rubric hashes, verdict counts, overrides).

## Quality

- `streamlit` is in the optional dependency group `labeling`; CI syncs it so the AppTest
  smoke test runs (it skips where streamlit isn't installed).
- New modules `evalkit/labeling/*` are in the 90% coverage gate (100% today); the app itself
  is covered by the Streamlit AppTest smoke test (loads a record, refuses a fail without a
  note, saves a label to a temp directory, moves on). mypy now also checks `app/`.
- Small fixes found while building: `evalkit generate` and the dataset tests now pick
  TestCase files by MANIFEST `kind` (the sample file isn't a TestCase file);
  `update_manifest.py` keeps entries written by commands (sample, export) instead of
  dropping them.

## Applicability: faithfulness edge cases (decided after labeling)

The first validate run gave 39 warnings. 33 were faithfulness edge cases the frontmatter
couldn't express, so `applicability.py` now pre-sets faithfulness to n_a when the expected
route is greeting, and for a `not_found` answer unless the reply claims an action (hand-off
wording such as "I'm escalating", "I'm passing this on", "a team member will assist you";
the standard "a team member can help you" offer is not a claimed action). No label was
changed by this; the rule only moves the pre-set to match what was labeled. The other
warnings were decided case by case: legacy-oos-competitor keeps n_a, legacy-kb-discount-code
is kept as is, syn-out-of-scope-6b06ea9a was relabeled correctness = pass in the app, and
syn-guardrail-7df96b9b (faithfulness pass, which the new rule pre-sets to n_a) was relabeled
n_a in the app.

## Results (labels_v1)

160/160 labeled by hayyan (165 raw rows: 5 re-saves), all under rubric_hash `daa615a9e40c`,
0 overrides. `evalkit labels validate`: **0 errors, 5 warnings**, all kept by decision
(legacy-kb-discount-code completeness and escalation_appropriate; legacy-oos-competitor
correctness and completeness; syn-out-of-scope-6b06ea9a completeness).

| Criterion | pass | fail | n_a |
|---|---|---|---|
| safety | 158 | 2 | 0 |
| escalation_appropriate | 121 | 33 | 6 |
| faithfulness | 105 | 0 | 55 |
| correctness | 79 | 4 | 77 |
| completeness | 76 | 4 | 80 |
| relevance | 130 | 30 | 0 |
| tone | 149 | 11 | 0 |
| **all** | **818** | **84** | **218** |

50 of 160 records have at least one fail. First-pass time per record: **mean 74.6 s,
median 50.5 s** (min 14.1 s, max 617.2 s; 198.9 min in total). As expected the mean is pulled
up by a few long records (pauses and breaks), so the median is the figure to plan with.

A save-to-the-wrong-record slip during the re-saves (6b06ea9a's note landed on
bug-greeting-hello-hello) was caught by comparing rows, fixed by re-saving both records,
and led to the jump and save-confirmation fixes above. The raw file keeps every row.

## Label balance

Fails are rare for several criteria: **faithfulness 0**, **safety 2**, **correctness 4**,
**completeness 4**. With so few (or no) negatives, judge agreement on those criteria
(Cohen's kappa, fail recall) can't be measured reliably from these labels: one judge
disagreement swings the estimate, and with 0 fails fail-recall is undefined. (For scale:
0 fails in 105 judged faithfulness labels only bounds the true fail rate below about 3% at
95%, by the rule of three.) escalation_appropriate (33), relevance (30) and tone (11) have
enough fails to be informative.

Plan for Week 3:
- Report every agreement number with a confidence interval (and the n behind it), so thin
  criteria are visibly thin rather than looking perfect.
- Add **clearly marked synthetic negatives** (e.g. replies edited to contradict the
  retrieved chunks, or to state a wrong price) in a **separate file**, labeled as synthetic,
  and report judge results on them separately. They are never mixed into the human labels in
  `labels_v1.jsonl` (as decided on Day 8).
