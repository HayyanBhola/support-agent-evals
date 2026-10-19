# Day 5: read timeout and a sequential latency check

## Read timeout is now configurable (default 180 s)

Day 4's slowest knowledge call took 116.8 s, ~3 s under the adapter's fixed 120 s read
timeout. Changes in evalkit:

- `READ_TIMEOUT_S` is now **180 s** (`src/evalkit/adapters/glowcare.py`).
- `evalkit run --read-timeout S` overrides it for one run; otherwise `AGENT_READ_TIMEOUT_S`
  from the environment / `.env` is used; otherwise 180 s. A non-positive or non-numeric
  `AGENT_READ_TIMEOUT_S` is a config error (exit 2), not a silent fallback.
- The value is part of the adapter settings, so it is in `config_hash`, and the resume
  command printed after Ctrl+C repeats `--read-timeout` when it was given.

## Sequential check: `bench_knowledge_10` at concurrency 1, `--no-cache`

Run **`20261013-144617-3f86`**, 2026-10-13 14:46-14:54 UTC, agent
`gemini/gemini-3.1-flash-lite` (health ok), read timeout 180 s.

| Run | Concurrency | n ok | p50 | p95 | Max | Timeouts |
|---|---|---|---|---|---|---|
| Day 3 `20261013-130920-69a8` (bench 10) | 1 | 10 | 3.46 s | 6.70 s | 6.8 s | 0 |
| Day 4 `20261013-140332-2240` (47 knowledge) | 2 | 47 | 6.73 s | 68.48 s | 116.8 s | 0 |
| **Day 5 `20261013-144617-3f86` (bench 10)** | **1** | **9** | **13.39 s** | **77.05 s** | **87.4 s** | **1 (> 180 s)** |

Per call (wall latency; the agent's own `latency_ms` matched within 20 ms on every answer):

| Case | Started (UTC) | Wall | Prompt / completion tokens |
|---|---|---|---|
| bench-shipping-time | 14:47:44 | 5.6 s | 940 / 37 |
| bench-shipping-cost | 14:47:50 | **timeout (180 s)** | - |
| bench-international | 14:50:50 | 13.4 s | 922 / 22 |
| bench-return-window | 14:51:03 | **87.4 s** | 933 / 12 |
| bench-return-opened | 14:52:31 | 21.5 s | 930 / 47 |
| bench-refund-time | 14:52:52 | 4.4 s | 925 / 39 |
| bench-damaged-item | 14:52:56 | **61.5 s** | 968 / 38 |
| bench-refund-method | 14:53:58 | 4.0 s | 932 / 14 |
| bench-return-shipping | 14:54:02 | 13.6 s | 942 / 42 |
| bench-wrong-item | 14:54:16 | 9.8 s | 990 / 37 |

The unrecorded warm-up call also took ~87 s (preflight started 14:46:16, first case 14:47:44),
although the agent was already running. All answers were `answer_type: llm` with ~1k prompt
tokens, so the questions were no heavier than on Day 3. evalkit did no retries (`attempts`
1 everywhere) and saw no 429s or 5xx.

## Verdict: not (only) rate limiting under a burst

**The Day 4 slowness does not look like a burst effect.** With one request at a time, and
on average one request every ~20 s, the tail is as bad as Day 4's at concurrency 2 (p95
77 s vs 68 s) and worse at the extreme (a > 180 s timeout vs 116.8 s). The median is 4x
Day 3's for the same 10 questions at the same concurrency, an hour and a half later.
Concurrency 1 does not keep the tail down, so lowering concurrency is not the fix.

The likely mechanism is inside the agent, which hides it from us. The agent's
`agent/llm_client.py` (agent repo at `54f9ec6`; this file is not in our snapshot) retries
Gemini itself:

- 429 -> sleeps 15, 30, 45 s; 500/502/503/504, timeouts and dropped connections -> sleeps
  5, 15, 30 s; at most 3 retries;
- every attempt has a 60 s request timeout (`call_llm(timeout=60)`).

So a single answer can take up to ~4 x 60 s + 90 s = 330 s before the agent gives up, and
the slow calls fit that pattern: 61.5 s is about one 60 s timeout plus a fast retry, and
87 s fits a few retry sleeps plus slow responses. The agent then still returns `ok: true`
with an `llm` answer and normal token counts, so neither `RunMeta` nor `CaseResult` can tell
"Gemini was slow" from "Gemini failed twice and we retried".

What it most likely is: Gemini returning transient 503 "high demand" errors or very slow
responses (the agent's comments record seeing exactly that in its Phase 10), and/or
per-minute 429s on the free tier. A burst would make 429s more likely, but today's
sequential run shows the problem without one. **To confirm which:** the agent prints
`[LLM] gemini rate limit hit, waiting ...` or `[LLM] gemini busy (error 503), waiting ...` to
its console. Its console output for 19:46-19:55 local time (UTC+5) would settle it.

## Consequences

- **180 s is still below the agent's worst case (~330 s).** A run can still record
  `timeout` failures that come from Gemini, not from answer quality. They are reported as
  failures (exit 1), never as wrong answers, and the affected cases can be rerun with
  `--resume` or `--ids`. If timeouts become frequent on the ~300-case run, raise
  `AGENT_READ_TIMEOUT_S` to ~330 for that run rather than hiding them.
- Keep the default concurrency at 2: concurrency 1 didn't help, and Day 3's finding that
  4 is counter-productive still stands.
- Agent-side suggestion (not ours to change): return the number of LLM attempts and the
  last upstream status in the eval_mode response, so evals can separate provider
  trouble from agent latency.
- Caveat: one run of 10 calls at one time of day. Day 3, Day 4 and Day 5 were all on
  2026-10-13, so time-of-day load on Gemini is a plausible confounder.
