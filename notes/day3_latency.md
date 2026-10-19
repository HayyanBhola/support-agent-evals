# Day 3: latency, concurrency and the eval_mode database check

Measured on 2026-10-13 against the local agent (`gemini/gemini-3.1-flash-lite`) at
`http://127.0.0.1:8000/chat`. All calls used `eval_mode: true`.

## Concurrency benchmark

`datasets/bench_knowledge_10.jsonl` (10 different knowledge questions; all routed to
knowledge), run with `--no-cache` at each concurrency, one run after another, each after an
unrecorded warm-up call. "Cases phase" is first case start to last case finish; "command" is
the whole `evalkit run` including preflight and warm-up.

| Concurrency | Run id | Cases phase | Command | p50 | p95 | Max | Retries | 429s / 5xx | Failures |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `20261013-130920-69a8` | 41.0 s | 47.0 s | 3.46 s | 6.70 s | 6.8 s | 0 | 0 | 0 |
| **2** | `20261013-131008-a6a0` | **22.2 s** | 35.1 s | **3.51 s** | 8.13 s | 10.7 s | 0 | 0 | 0 |
| 4 | `20261013-131043-9aaf` | 65.6 s | 73.3 s | 6.44 s | 34.37 s | 53.9 s | 0 | 0 | 0 |

p50/p95/max are per-call wall latency (non-cached), from `results.jsonl`.

**Findings**

- No 429s, no 5xx and no retries at any setting (every case: `attempts == 1`).
- The agent's own `latency_ms` matches our wall latency within ~50 ms in every run, so all
  the slowdown happens inside the agent (most likely its LLM calls queueing or being
  throttled upstream), not in our client, retries or backoff.
- Concurrency 2 halves the cases phase (41 s to 22 s) with an unchanged median (3.46 s to
  3.51 s). The p95 rise (6.7 s to 8.1 s) is one 10.7 s call, within the noise of n=10.
- Concurrency 4 is counter-productive: the cases phase is *slower* than sequential, the
  median doubles and the tail explodes (one call took 54 s). Rate limiting surfaces as
  latency, not as 429s, so the adapter's retry logic never sees it.

**Recommendation: default concurrency 2** (unchanged; already the CLI default). It is the
highest setting where per-call latency doesn't rise noticeably and there are no 429s.

Caveats: one run of 10 cases per setting, knowledge route only. Re-check if the agent's
model, hosting or Gemini quota changes; concurrency 3 was not measured.

## Other latency observations

- Cold start: the first warm-up of the day took **116.7 s** (run `20261013-125413-5582`).
  Later warm-ups took 3.6-10 s. The warm-up step exists exactly to absorb this.
- Warm knowledge calls took ~2-7 s today, much faster than the ~20 s measured on Day 1.
  Non-LLM routes (database, escalate, greeting) answer in 0.01-0.35 s.
- The 120 s read timeout leaves ample headroom: the slowest call in any run was 54 s, and
  only at concurrency 4.

## eval_mode saves nothing: database check (passed)

Done by the user in the agent's `glowcare` database (pgAdmin Query Tool or psql) with:

```sql
SELECT (SELECT count(*) FROM conversations) AS conversations,
       (SELECT count(*) FROM messages) AS messages,
       (SELECT count(*) FROM escalations) AS escalations;
```

| Snapshot | conversations | messages | escalations |
|---|---|---|---|
| Before | 21 | 122 | 11 |
| After | 21 | 122 | 11 |

Between the two snapshots: `evalkit run --dataset datasets/sample_5.jsonl --no-cache`
(run `20261013-130325-bde2`), i.e. 1 `/health`, 1 "hi" probe, 1 warm-up and 5 real case
calls, including "I want to talk to a human", which returned `escalated: true`. Counts
are identical, so eval_mode wrote no conversations, messages or escalations.

To repeat the check: take the counts, run a dataset with `--no-cache` (so every case
really calls the agent), take the counts again and compare.
