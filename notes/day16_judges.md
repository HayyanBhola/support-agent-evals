# Day 16: LLM judge framework and the relevance judge

Groq only, **dev split only** (the test split was never loaded), no OpenAI or Gemini model as
judge. The agent repo and agent_snapshot/ were not touched. Nothing pushed.

## Model choice

**`qwen/qwen3.8-27b`** (Alibaba Cloud, on Groq), pinned: every result records the model id
the API returned, and the judge cache is keyed by it, so results stay reproducible from the
cache if Groq retires the model.

- **Why:** the judge must come from neither the agent's family (Gemini,
  `gemini-3.1-flash-lite`) nor the test-case generator's (OpenAI, `gpt-5-mini`): a
  same-family judge tends to favour its own family's output and to share its blind spots.
  On this Groq key the only general chat model outside both families is qwen3.8-27b; the
  rest are OpenAI's gpt-oss and Whisper, Meta's prompt-guard classifiers, Canopy's
  text-to-speech and SDAIA's allam-2-7b (7B, 4k context, too small). `qwen/qwen3.6-27b`, the
  preferred id, returns 404 on this key; qwen3.8-27b was the agreed first choice.
- **Context:** 131,072 tokens (a relevance call uses about 1,450).
- **Thinking:** `reasoning_effort: "none"` is accepted; 0 reasoning tokens on every call. The
  fallback (`reasoning_format: "hidden"`) and the `<think>` stripping were never needed.
- **Groq limits on this key:** 1,000 requests per day and **8,000 tokens per minute**. The
  token limit is the bottleneck: about 4 calls per minute.
- **Possible bias checks later (not run):** gpt-oss-120b (free on Groq) for the
  generator-family effect, Gemini for self-preference; both would be side experiments, never
  the scorecard number.

## Framework (src/evalkit/judges/)

- **Prompts from the rubric files, never hand-copied:** `evalkit judge prompt` builds
  `prompts/judges/relevance_v1.md` from `rubrics/relevance.md` (rubric v3): a fixed
  instruction block (judge this one criterion, ignore the others), the definition, Key
  rules, Pass if, Fail if, Edge cases, the rubric's own pass/fail examples (rubric examples
  only, checked against the test split), and the output format. A header comment records
  the rubric_hash and is not sent to the model. prompt_sha256 `f57930f2127b` is in every
  result; a test fails if the committed prompt drifts from the rubric, and `judge run`
  refuses a stale prompt.
- **Input:** question, history, reply, route, answer_type, escalated, plus only what the
  criterion needs (relevance: nothing more; chunks, DB facts, reference and gold are wired
  for the next judges).
- **Output:** strict JSON `{verdict, reason (<= 2 sentences), evidence}` validated with
  Pydantic; the evidence must be a quote from the reply (case and whitespace ignored, "..."
  allowed). One repair retry, then `judge_error`: a verdict is never guessed. Temperature 0.
- **Applicability first:** n_a cases are never sent (relevance applies to every reply).
- **Provenance per result:** case_id, criterion, verdict, reason, evidence, model id,
  prompt_sha256, rubric_hash, input_sha256, tokens in / out / reasoning, latency, attempts,
  cache hit, thinking mode, timestamp, and the raw reply.
- **Cache:** `.cache/judges/`, keyed by (model, prompt_sha256, rubric_hash, input hash,
  params); never overwritten; bypassed (read and write) for self-consistency.
- **Budget and rate:** `--dry-run`; `--max-calls` and a daily cap (200), both counting real
  HTTP requests (retries included) and checked before any client exists and before each
  case; a usage ledger (`reports/judges/usage.jsonl`, committed) with the status and error
  of every request. A rate limiter paces from the `x-ratelimit-*` headers and a 60-second
  token window. By default a refused request stops the run (no automatic retry).

## What happened

1. **Smoke test (3 dev cases):** clean JSON, all 3 verdicts matching the human labels. But
   **2 of the 5 HTTP requests were refused** (almost certainly 429 rate limits; the retries
   succeeded), and the call count showed 3 because it counted judge attempts. Fixes before
   the full run: the ledger records status and error; caps count real requests; max_tokens
   1024 -> 300 (Groq reserves it against the per-minute limit; the longest verdict was 80
   tokens); pacing to the per-minute token window; stop on the first refusal.
2. **Full run:** 96 dev cases, **98 requests, 0 refused, 0 judge_error**, 2 repair retries
   (syn-greeting-b1a383ee, syn-knowledge-812bf00a; the first attempt's error isn't stored,
   see v2). About 24 minutes (20:15 to 20:39 UTC), paced by the token limit.
3. **Self-consistency:** 20 dev cases (seed 42) re-judged with the cache bypassed: **0 verdict
   flips**.

**Tokens and calls today:** 123 requests of the 200 cap (smoke 5, relevance 98, consistency
20); 176,343 input and 8,060 output tokens (Groq free tier, not billed). A relevance call
is about 1,450 tokens in and 70 out.

## Relevance on dev: judge vs hayyan (labels_v1.2)

`reports/judges/relevance_dev_v1.md`

| n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] | human fail rate | judge fail rate |
|---|---|---|---|---|---|
| 96 | **87.5%** | **0.55** [0.29, 0.76] | **0.83** [0.72, 0.92] | 18.8% | 14.6% |

Confusion (human x judge): pass/pass 74, pass/fail 4, fail/pass 8, fail/fail 10. The judge
is more lenient (14 fails vs 18). For scale, hayyan vs labeller_b on relevance (round 1, 40
pairs): 85.0%, kappa 0.53, AC1 0.78; the judge agrees with hayyan about as well as the
second human did, and below the 0.6 kappa line.

## Disagreement patterns (12)

No label was changed; the "label arguable" cases are listed for hayyan to decide.

**Judge wrong or under-informed (7, all judge pass / human fail):**

- **Vague questions not clarified (3):** syn-clarify-18f68720, syn-clarify-8308e742,
  syn-clarify-bfe194c8. The rubric's Pass if says a vague request should get the clarifying
  question, but it doesn't say that generic information (or not_found) instead is a fail;
  the judge accepts "related" information. A rubric gap: relevance_v2 needs it explicit.
- **Hand-off instead of an answer (3):** syn-medical-553f24d4, syn-medical-b8756450
  (allergy questions before use, escalated as reactions), syn-knowledge-812bf00a (a question
  about the hand-off treated as a hand-off request). The judge reads "a hand-off is relevant
  when the request should be handed off" generously: it assumes any medical-sounding
  question should be. It doesn't see should_escalate, by design (relevance isn't escalation).
- **not_found for an answerable question (1):** syn-database-b242857a. The judge can't know
  that the knowledge base or database has the answer: it sees no reference answer or gold
  facts, so it applies "not_found for an uncovered question = pass". An input gap.

**Label arguable (5):**

- **Guardrail requests answered with an unrelated policy quote (2, human pass / judge
  fail):** syn-guardrail-5fa94211, syn-guardrail-cfe984f8. The Key rule "a quoted policy
  section that doesn't answer the question = fail" supports the judge; in round 1 labeller_b left
  both n_a rather than passing them. Likely label fixes.
- **syn-guardrail-58718673 (human pass / judge fail):** a request for proprietary chemical
  formulas answered with the ingredient list. Arguable either way (the list is the honest
  partial answer; the request should arguably be refused).
- **syn-database-2cccc9ee (human pass / judge fail):** the "Which product do you mean?" menu
  for a named, non-existent product. hayyan failed it on correctness (Day 15); the judge says
  it ignores the named product, which is also a fair relevance reading.
- **syn-escalate-2af765de (human fail / judge pass):** "please request human support" in
  reply to "put me through to a supervisor". The Day 12 analysis already flagged this as a
  missed escalation counted again under relevance (group c).

## What to change in relevance_v2

1. **Rubric (relevance.md, then rebuild the prompt):** add Key rules or Fail if lines:
   "a vague request answered with generic information or not_found instead of the clarifying
   question = fail"; "a hand-off is relevant only when the request is for a human, a
   complaint, or a reaction that happened; handing off a question the bot should answer
   (e.g. an allergy question before use) doesn't answer it = fail".
2. **Input:** give the relevance judge the reference answer and gold facts (or a "the
   knowledge base covers this" flag), so not_found can be judged against coverage.
3. **Labels (hayyan's decision):** syn-guardrail-5fa94211 and syn-guardrail-cfe984f8 look
   like relevance fails under the existing Key rule; 2cccc9ee, 58718673 and 2af765de need a
   ruling.
4. **Framework:** store the first attempt's error when a repair retry is used.
5. Re-run on dev with v2 (cache keys change with the prompt), compare with v1 on the same 96
   cases, and only then consider a held-out check on test.

## Second-opinion judge: OpenAI gpt-5-mini (Day 16, later)

**Qwen (`qwen/qwen3.8-27b`) stays the primary judge.** OpenAI's `gpt-5-mini` is added as a
**second-opinion judge only**, because OpenAI generated the synthetic test cases (Day 5): a
judge from the generator's family may favour the cases and wordings it would produce itself,
so its numbers never replace or mix with Qwen's.

- **Kept apart:** its own client (`OpenAIJudgeClient`: strict JSON-schema output), its own
  results file (`runs/<id>/judges/relevance_v2_openai.jsonl`) and report
  (`reports/judges/relevance_dev_v2_openai.md`), and its own cache keys (the model id is in
  the key). Same `relevance_v2` prompt (sha256 `6acf628ee5bf`) and the same verdict schema.
- **Settings:** GPT-5 models accept only their default temperature, so none is sent (the
  lowest the model allows); `reasoning_effort: "minimal"`; `max_completion_tokens` 2000
  (hidden reasoning included). Dev split only.
- **Dry run (96 dev cases):** 96 calls (up to 192 with repairs; capped at 110), about 178k
  input and 38k output tokens, **estimated $0.06** at approximate list prices (worst case
  with every repair and the full output budget: $0.43).
- **Judge vs judge:** `judge agree --provider openai --vs-provider groq` adds Qwen v2 vs
  OpenAI v2 on the cases both judged; Qwen's verdicts come from its results file, or from its
  cache while its run is unfinished (no call).
- **Run (cost approved: about $0.06, at most $0.43):** 96 dev cases, **96 calls, 0 refused,
  0 judge_error, 0 repairs**; model `gpt-5-mini-2025-08-07`; 168,370 input and 8,242 output
  tokens (0 reasoning tokens reported), **about $0.03** at approximate list prices. OpenAI's
  limits on this key (2M tokens per minute) never slowed it down.

### OpenAI v2 vs hayyan (labels_v1.3, dev)

`reports/judges/relevance_dev_v2_openai.md`

| n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] | human fail rate | judge fail rate |
|---|---|---|---|---|---|
| 96 | 90.6% | 0.71 [0.51, 0.87] | 0.86 [0.76, 0.94] | 21.9% | 18.8% |

9 disagreements:

- **Too lenient (6, human fail / judge pass):** the 3 vague clarify cases (syn-clarify-
  18f68720, -8308e742, -bfe194c8: it accepts generic information or not_found where the v4
  rule wants the clarifying question); syn-escalate-2af765de ("please request human
  support"); syn-guardrail-5fa94211 (reads the policy quote as a refusal); syn-medical-
  b8756450 (accepts the reaction hand-off for a before-use question).
- **Too strict (3, human pass / judge fail):** syn-guardrail-58718673 (the ingredient list
  for a "proprietary formulas" request; hayyan kept it pass); syn-medical-82ffc8a9 (it calls
  the medical refusal a hand-off of an answerable question, against the v4 rule that medical
  questions may be refused); syn-out-of-scope-6b06ea9a (it fails an honest not_found
  although `answer_available` was false). The last two are the judge misreading the rubric.

### Judge vs judge: Qwen v2 vs OpenAI v2

On the **81** dev cases both have judged (Qwen's v2 run resumes after 00:00 UTC; its 81
verdicts come from its cache): **95.1% agreement, kappa 0.84 [0.65, 0.96], AC1 0.93
[0.85, 0.98]**. All 4 differences are Qwen fail / OpenAI pass (the 3 clarify cases and
syn-guardrail-5fa94211), and **hayyan's label is fail on all 4**: where the two judges
differ, Qwen agrees with the human. OpenAI's 90.6% / kappa 0.71 above is on all 96;
Qwen v2's own agreement with hayyan is pending its last 15 cases.

**Caveats.** Both judges use the v2 prompt, whose rules were written after reading the v1
disagreements on these same dev cases, so both numbers are optimistic for unseen data (see
"v1 -> v2", to come). OpenAI stays a second opinion: it generated the synthetic cases, and
its numbers never go into the scorecard or replace Qwen's.
