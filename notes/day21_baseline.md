# Day 21: the official BEFORE scorecard of the agent

The agent as it stands, before any Week 4 change. It scores the saved Day 7 run
`20261013-191847-f23b` (`gemini/gemini-3.1-flash-lite`) against `golden_v1.2` (405 cases).
No agent calls were made.

- **Deterministic metrics: all 405 cases.** 395 were answered; the 10 cases with chat
  history were skipped by the run, since eval_mode can't replay history.
- **LLM judge scores: only the 120-case judge sample** (`datasets/judge_sample_v1.json`),
  not all 405. The judge is OpenAI `gpt-5-mini` on the six frozen prompts (FROZEN.json
  sha256 `cfb3fe3d1677`). `judge final` stays locked: this run scores the agent and reads no
  human label.
- **Cost:** 316 judge calls (231 verdicts reused from the cache), about **$0.10**, 0
  judge_error, 0 refusals (approved: 400 calls / $0.20).
- **Files:** `runs/<id>/scores.jsonl` (2,538 rows: one per case per metric or judge),
  `runs/<id>/summary.json` (rates, 95% bootstrap CIs and breakdowns). The agent's own
  `runs/<id>/results.jsonl` is not touched. **MLflow** run `c3b2e3e5a10e…` in `./mlruns`
  (gitignored).

Commands:

```powershell
uv run evalkit score-full 20261013-191847-f23b --judge-sample datasets/judge_sample_v1.json --max-calls 400 --max-usd 0.20
uv run evalkit report --run 20261013-191847-f23b [--by route|source|difficulty]
uv run evalkit compare <run_a> <run_b>       # Week 4: deltas with paired bootstrap CIs
uv run mlflow ui --backend-store-uri sqlite:///mlruns/mlflow.db
```

## The judge sample

The sample is 120 of the 395 answered cases. It is seeded (seed 42) and stratified
proportionally by expected route × source, so each route and source keeps its share of the
golden set (`evalkit judge sample-cases`; the file lists the strata and is never
overwritten). 52 of the 120 are labeled dev/test cases, and their verdicts came from the
cache. Because it is a sample, **judge rates carry sampling error on top of judge error**,
and the per-route judge n is small (5-37).

## Headline (95% percentile bootstrap CIs over cases, 2000 resamples, seed 42)

| Metric | Cases | Passed | Rate [95% CI] |
|---|---|---|---|
| route_match | all (380 with an expected route) | 274/380 | **72.1%** [67.6, 76.6] |
| escalation_match | all (should_escalate set) | 187/285 | 65.6% [60.0, 71.2] |
| escalation **recall** | | 34/42 | **81.0%** [68.9, 91.9] |
| escalation precision | | 34/124 | 27.4% [19.8, 35.6] |
| db_facts | all (gold facts) | 44/51 | 86.3% [76.5, 94.1] |
| voice_third_person | all | 372/395 | 94.2% [91.9, 96.5] |
| content_rules (weak signal) | all | 247/275 | 89.8% [85.8, 93.5] |
| judge_relevance | sample | 89/120 | **74.2%** [66.6, 81.7] |
| judge_completeness | sample (knowledge/database) | 49/62 | 79.0% [69.4, 88.7] |
| judge_safety | sample | 114/120 | 95.0% [90.8, 98.3] |
| judge_tone | sample | 103/120 | 85.8% [79.2, 91.7] |
| judge_faithfulness | sample (KB/DB answers) | 74/75 | 98.7% [96.0, 100.0] |
| judge_correctness | sample (with a reference) | 32/41 | 78.0% [65.9, 90.2] |

The deterministic numbers reproduce Day 7 and Day 15 exactly: routing 72%, escalation
recall 81.0%, precision 27.4%.

## By expected route (pass rate, n)

| Metric | clarify | database | escalate | greeting | guardrail | knowledge | medical | (none) |
|---|---|---|---|---|---|---|---|---|
| route_match | 69% (32) | 95% (82) | 71% (42) | **11%** (36) | **28%** (36) | 98% (121) | **39%** (31) | – |
| escalation_match | 76% (25) | 93% (46) | 81% (42) | **0%** (32) | 45% (33) | 77% (78) | 54% (26) | 67% (3) |
| judge_relevance* | 100% (10) | 88% (25) | 83% (12) | **9%** (11) | 91% (11) | 81% (37) | 56% (9) | 20% (5) |
| judge_safety* | 90% (10) | 100% (25) | 83% (12) | 100% (11) | 100% (11) | 100% (37) | 89% (9) | 60% (5) |
| judge_tone* | 100% (10) | 96% (25) | 67% (12) | 82% (11) | 91% (11) | 92% (37) | 56% (9) | 60% (5) |
| judge_completeness* | – | 84% (25) | – | – | – | 76% (37) | – | – |
| judge_correctness* | – | 93% (14) | – | – | – | 76% (25) | – | 0% (2) |

\* judge sample. "(none)" = legacy cases with no expected route (15). CIs per cell are in
`summary.json`.

By source: legacy cases pass nearly everything (route 100%, judge relevance 88%); synthetic
cases hold the hard ones (route 62%, judge relevance 70%, completeness 71%). By difficulty,
judged relevance is easy 76% / medium 70% / hard 82%, all within each other's CIs.

**What the agent gets wrong (the Week 4 targets):** greetings and thank-yous (route 11%,
escalation 0%, relevance 9%: they get not_found and are escalated); guardrail and medical
routing (28%, 39%); over-escalation (precision 27%: 90 extra escalations against 8 missed);
and completeness / correctness on knowledge answers (76% each).

## Limitations

- **Judge scores cover the 120-case sample only**, not all 405. Per-route judge numbers rest
  on 5-37 cases, and the CIs show it.
- **Judge error is not removed.** On the test split (Day 19), tone was over-strict (12 judge
  fails vs 2 human) and relevance and correctness were somewhat stricter than hayyan. So
  judge pass rates for tone, relevance and correctness are probably **lower bounds**. Tone
  also misses role-play breaks (2/5 caught in the synthetic negatives).
- **OpenAI generated the synthetic cases** (68% of golden_v1.2) and is the judge. On test the
  judge was stricter, not more lenient, on its own family's cases (Day 19).
- **One run, one model:** no repeat runs, so run-to-run agent variance isn't in the CIs. They
  are bootstrap CIs over cases only.
- **Chat-history cases (10) are not scored**, by the metrics or the judges.
- `content_rules` is a substring check: a weak signal, not correctness.
- Some cases are counted in both the dev/test calibration and this baseline (52 of the 120
  sampled). That's fine for scoring the agent, but the scorecard is not an independent judge
  check.
- **Week 4 comparisons must use the same dataset, rubric, FROZEN.json and judge sample.**
  `evalkit compare` warns when the dataset, rubric or FROZEN hashes differ.
