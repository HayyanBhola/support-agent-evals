# Data contracts

All models live in `src/evalkit/schemas.py`. JSON Schema for each one is in `docs/schemas/`;
regenerate with `uv run python scripts/export_schemas.py` (a test fails if they are stale).

**Two families.** Our own records (TestCase, CaseResult, Label, MetricResult, RunMeta) are
strict: unknown fields are rejected, bools and ints are never coerced (`"true"` is not `True`),
timestamps must carry a timezone, and each carries `schema_version: 1`. The agent's response
models are tolerant: unknown fields are kept, so a new agent field never breaks a run.

**Files.** Records are stored as JSONL via `evalkit.io`: `read_jsonl` streams and reports
`path:line` on any bad line; `write_jsonl` writes UTF-8 with LF newlines atomically.
`evalkit.hashing.stable_hash` (sha256 of canonical JSON) backs cache keys and `config_hash`.

## Enums

| Enum | Values |
|---|---|
| `Route` | escalate, guardrail, medical, greeting, clarify, database, knowledge |
| `ResponseSource` | database, knowledge_base, fixed_reply |
| `AnswerType` | llm, quoted_source, not_found |
| `Difficulty` | easy, medium, hard |
| `CaseSource` | legacy, synthetic, human, production |
| `Criterion` | faithfulness, correctness, relevance, completeness, escalation_appropriate, safety, tone |
| `LabelValue` | pass, fail, n_a |
| `FailureType` | timeout, connection, http_error, agent_error, skipped, parse_error |

## TestCase

One evaluation case: a question and everything we expect of the answer. Frozen.

- Required: `id` and `category` (lowercase slugs), `question` (stripped, non-empty),
  `expected_route`, `source`. `difficulty` defaults to `medium`.
- `expected_route` must be present but may be `null`: the route is then not checked and the
  case is left out of route accuracy (two legacy cases work this way, as the legacy sheet
  only checks `route` when a case has one).
- Expectations: `should_escalate` (`None` = don't check), `reference_answer`,
  `gold_context_ids`, `gold_db_facts` (values: str, int, float, bool or list of str, e.g.
  ingredients), `must_include` (all must appear), `must_include_any` (a list of groups; **each**
  group needs at least one match, the legacy sheet's format), `must_not_include`.
- `history` is a list of prior `Turn`s (`role`: user/assistant, `content`). The agent's API
  only accepts `message` today, so the adapter cannot send history yet.
- Rules: every case must assert something (`reference_answer`, any `must_*`, gold facts or
  context ids, or `should_escalate` set to True/False, which makes route-focused cases like
  "hi again" valid); `expected_route=escalate` forbids `should_escalate=False`; database cases
  need `gold_db_facts` or `must_include`; `must_include` and `must_not_include` must not
  overlap (case-insensitive). Errors name the case id.

## AgentResponse

The JSON body of `POST /chat`. Only `ok` and `reply` are required; everything else is
Optional because some route, or the error branch, sends it as null or omits it.

- `route` is null on errors; `source`, `answer_type` and `memory` are absent on errors;
  `intents` is a list on success and null on errors; `error` exists only on errors.
- `conversation_id`, `memory` and `escalation` are `Any | None`: they were null in every
  captured fixture, so their real types are unknown.
- `answer_type` was `llm` on knowledge and null on every other route. **`quoted_source` and
  `not_found` are only covered by constructed test responses so far**, not real captures.
- `unknown_fields`: fields the agent sent that the model doesn't define (warn on these).
- `llm_called`: `prompt_tokens > 0`. Never derived from `model`, which is set on every route.
- `total_tokens`, `is_error` (`ok` is false), `checker_replaced` (LLM was called but the
  answer was replaced with a quote or not-found).
- `parsed_details`: `KnowledgeDetails` on knowledge, `DatabaseDetails` on database, otherwise
  (or if the shape doesn't fit) the raw `details` value, which stays available as-is.
- Escalation metrics must use `escalated`, not `escalation`: in eval_mode the escalate route
  returns `escalated: true` with `escalation: null`.
- These computed values are properties, never serialized.

## RetrievedChunk and ChunkUsed

Knowledge-base chunks: `retrieved_chunks[]` (`id`, `text`, `score`, `source`) and
`details.chunks_used[]` (adds `section`; the score is called `distance`). Both are Chroma
distances: **lower = more relevant**.

## KnowledgeDetails

`details` of a knowledge response: `question`, `chunks_used`, `prompt_sent_to_llm`,
`llm_answer`, `problems`, `answer`, `answer_type`, `llm_error`. Useful for faithfulness
judging (the exact prompt and the chunks the LLM saw).

## DatabaseDetails and ProductRecord

The agent sends database `details` as a bare list of products; `DatabaseDetails` wraps it as
`products`. Each `ProductRecord` has `product_id`, `name`, `price`, `currency`,
`stock_quantity`, `availability` and `ingredients`. Compare against `gold_db_facts`.

## CaseResult

The outcome of one case in one run: `run_id`, `case_id`, `response`, `failure_type`,
`error_message`, `attempts`, `cache_hit`, `wall_latency_ms`, `started_at`, `finished_at`.

- Exactly one of `response` / `failure_type` is set, **except** `agent_error`: the agent
  answered `ok: false`, so the response is kept (with its `request_id`, latency and `error`)
  and `failure_type` is `agent_error`.
- A response with `ok: false` must be marked `agent_error`, and `agent_error` needs one.
- `finished_at` cannot precede `started_at`.

## Label

One labeling of one response in one run, from a human or a judge: a `verdicts` map with a
verdict (`pass` / `fail` / `n_a`) for **every** `Criterion` (all seven required). One Label
per save; the labels file is append-only and the latest row per (`case_id`, `labeler`) wins.
`labeler` is free text such as `human:hayyan` or `judge:groq/qwen@v1`. `rubric_hash`
(sha256) is `evalkit.rubrics.rubric_hash()` of the rubric the verdicts were given under;
`response_sha256` is `evalkit.hashing.stable_hash` of the labeled AgentResponse (so a label
can't silently attach to a different reply). `overrides` lists criteria the rubric marked
n_a but the labeler judged anyway; `seconds_spent` is the time on the record; also
`timestamp` (UTC) and optional `notes` (the labeling app requires one when any verdict is
`fail`). Frozen.

**Change log:**
- Day 8 (2026-10-14): `rubric_hash` added as a required field, keeping `schema_version: 1`,
  because no Label data existed yet.
- Day 9 (2026-10-14): Label changed from one row per criterion (`criterion`, `value`) to one
  row per labeled response (`verdicts` for all criteria, plus `response_sha256`,
  `overrides`, `seconds_spent`), keeping `schema_version: 1`: it was changed **before any
  Label data existed**, so nothing needed migrating. Any change to a persisted record after
  data exists needs a schema_version bump.

## LabelSample and SampleStratum

One record of a labeling sample (`datasets/label_sample_v1.jsonl`): `case_id`, `run_id`,
`stratum` (`expected_route`, `answer_type`; either may be null), the full `case`
(TestCase, with the gold fields of the dataset version sampled from), the saved `response`
(AgentResponse) and its `response_sha256`. `case.id` must equal `case_id`. Frozen.

**Change log:** added on Day 9 (2026-10-14) with `schema_version: 1`, **before any sample
data existed**.

## MetricResult

One metric for one case: `case_id`, `metric` (slug), `value` (number or null), `passed`
(bool or null), `details` (free dict). At least one of `value` / `passed` must be set.

## RunMeta and RunCounts

Everything needed to reproduce and compare a run: `run_id`, `git_sha` (null outside git),
`git_dirty`, `dataset_path`, `dataset_sha256`, `dataset_version`, `judge_versions`,
`config_hash`, `agent_model`, `agent_health` (the agent's `/health` body, may be empty),
`concurrency` (at least 1), `started_at`, `finished_at` (null while running), and `counts`
(`total`, `ok`, `failed`, `cache_hits`; `ok + failed == total`, `cache_hits <= total`).
