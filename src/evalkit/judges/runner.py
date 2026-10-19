"""Running a judge over a split of a saved run (Day 16).

For each case of the split: applicability (``labeling.applicability``) is decided first and
n_a cases are never sent; the judge input is built for the criterion; the disk cache is
checked (keyed by model, prompt_sha256, rubric_hash, input hash and params); otherwise the
judge is called at temperature 0. Its reply is parsed into ``JudgeVerdict`` after stripping
any thinking text; invalid JSON or a schema violation gets **one repair retry**, then the
case is a ``judge_error`` (a verdict is never guessed).

Budget guard: ``plan`` counts the calls (cache misses) and estimates tokens without calling;
``run_judge`` refuses to start if the calls could exceed ``max_calls`` or the daily cap, and
stops cleanly if repairs would push past them.
"""

import hashlib
import json
import random
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from evalkit.calibration.split import SplitFile, SplitName
from evalkit.generation.llm import Completion, GenerationError, LLMClient, ModelConfig
from evalkit.judges.client import Ledger, strip_thinking
from evalkit.judges.inputs import JUDGE_SKIPS, input_sha256, judge_input, to_json
from evalkit.judges.prompts import prompt_sha256, system_text
from evalkit.judges.schema import JudgeResult, JudgeVerdict
from evalkit.labeling.applicability import is_judged
from evalkit.rubrics import Rubric
from evalkit.schemas import AgentResponse, CaseResult, Criterion, TestCase

CACHE_DIR: Final = Path(".cache") / "judges"
SEED: Final = 42
OUTPUT_TOKENS_ESTIMATE: Final = 150
CALLS_PER_CASE_MAX: Final = 2  # the call and its repair retry
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
REPAIR: Final = (
    "Your reply could not be used: {error}. Reply again with ONLY the JSON object "
    '{{"verdict": "pass" | "fail", "reason": "...", "evidence": "..."}}, where evidence is '
    "copied word for word from the chatbot's reply."
)


class BudgetExceeded(Exception):
    """The calls needed would exceed --max-calls or the daily cap."""


class CallRefused(Exception):
    """The provider refused a request (stop_on_refusal): the run stops there."""


def norm(text: str) -> str:
    return " ".join(text.split()).casefold().strip("\"'“”‘’ ")


def parse_reply(content: str, reply: str) -> JudgeVerdict:
    """The verdict in ``content`` (thinking stripped, fences allowed); raises ValueError."""
    text, _ = strip_thinking(content)
    match = _JSON_OBJECT.search(text)
    if match is None:
        raise ValueError("no JSON object in the reply")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON ({e.msg})") from e
    try:
        verdict = JudgeVerdict.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, x['loc'])) or 'object'}: {x['msg']}"
                             for x in e.errors())  # fmt: skip
        raise ValueError(f"wrong fields ({problems})") from e
    pieces = [norm(p) for p in re.split(r"\.\.\.|…", verdict.evidence)]
    pieces = [p for p in pieces if p]
    if not pieces or not all(p in norm(reply) for p in pieces):
        raise ValueError("evidence is not a quote from the reply")
    return verdict


# --------------------------------------------------------------------------- cache


@dataclass(frozen=True)
class JudgeConfig:
    criterion: Criterion
    model: ModelConfig
    prompt: str  # the prompt file's text
    rubric_hash: str
    version: str = "v1"  # the prompt version (decides the input fields too)

    @property
    def prompt_sha256(self) -> str:
        return prompt_sha256(self.prompt)

    def params(self) -> dict[str, Any]:
        return {"seed": SEED, **self.model.params()}


def cache_key(cfg: JudgeConfig, input_hash: str) -> str:
    parts = [cfg.model.name, cfg.prompt_sha256, cfg.rubric_hash, input_hash, cfg.params()]
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()


class JudgeCache:
    def __init__(self, root: Path = CACHE_DIR) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> JudgeResult | None:
        path = self._path(key)
        if not path.exists():
            return None
        return JudgeResult.model_validate_json(path.read_text(encoding="utf-8"))

    def put(self, key: str, result: JudgeResult) -> None:
        path = self._path(key)
        if path.exists():
            return  # never overwrite a cached result
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=1) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- items


@dataclass(frozen=True)
class Item:
    case_id: str
    data: dict[str, Any]
    input_hash: str
    key: str


@dataclass(frozen=True)
class Plan:
    items: list[Item]  # applicable, answered cases of the split
    skipped_na: list[str]  # n_a by applicability: never sent
    unanswered: list[str]  # no ok response in the run
    cached: int
    est_tokens: int  # for the cache misses

    @property
    def calls(self) -> int:
        return len(self.items) - self.cached


def build_plan(
    cfg: JudgeConfig,
    rubric: Rubric,
    split: SplitFile,
    split_name: str,
    results: Mapping[str, CaseResult],
    cases: Mapping[str, TestCase],
    cache: JudgeCache,
    *,
    use_cache: bool = True,
    only: Sequence[str] | None = None,
    final: bool = False,
) -> Plan:
    """``final=True`` (``evalkit judge final`` only) allows the test split: the plan needs
    the cases and the agent's replies, never the test labels."""
    if split_name not in ("dev", "test") or (split_name == "test" and not final):
        raise PermissionError("judges run on the dev split only; the test split is locked")
    name: SplitName = "test" if split_name == "test" else "dev"
    ids = split.ids(name) if only is None else only
    return plan_cases(cfg, rubric, ids, results, cases, cache, use_cache=use_cache)


def plan_cases(
    cfg: JudgeConfig,
    rubric: Rubric,
    ids: Sequence[str],
    results: Mapping[str, CaseResult],
    cases: Mapping[str, TestCase],
    cache: JudgeCache,
    *,
    use_cache: bool = True,
) -> Plan:
    """The plan for exactly ``ids`` (no split check): ``build_plan`` for calibration, and
    ``evalkit score-full``, which scores the agent on every case of a dataset."""
    ids = sorted(ids)
    system = system_text(cfg.prompt)
    items, skipped, unanswered, cached, est = [], [], [], 0, 0
    for cid in ids:
        r = results.get(cid)
        resp = r.response if r else None
        if resp is None or not resp.ok:
            unanswered.append(cid)
            continue
        skip = JUDGE_SKIPS.get((cfg.criterion, cfg.version))
        if not is_judged(rubric, cases[cid], resp) or (skip is not None and skip(cases[cid])):
            skipped.append(cid)
            continue
        data = judge_input(cfg.criterion, cases[cid], resp, cfg.version)
        h = input_sha256(data)
        item = Item(cid, data, h, cache_key(cfg, h))
        items.append(item)
        if use_cache and cache.get(item.key) is not None:
            cached += 1
        else:
            est += (len(system) + len(to_json(data))) // 4 + OUTPUT_TOKENS_ESTIMATE
    return Plan(items, skipped, unanswered, cached, est)


def consistency_sample(plan: Plan, n: int, seed: int = SEED) -> list[str]:
    ids = sorted(i.case_id for i in plan.items)
    return sorted(random.Random(seed).sample(ids, min(n, len(ids))))


# --------------------------------------------------------------------------- run


def _messages(system: str, data: dict[str, Any]) -> list[dict[str, str]]:
    return [{"role": "system", "content": system}, {"role": "user", "content": to_json(data)}]


async def judge_one(
    client: LLMClient, cfg: JudgeConfig, item: Item, reply: str, thinking: str
) -> tuple[JudgeResult, int]:
    """One case: the call, one repair retry if needed. Returns the result and the calls used."""
    messages = _messages(system_text(cfg.prompt), item.data)
    tokens_in = tokens_out = tokens_reason = 0
    error: str | None = None
    verdict: JudgeVerdict | None = None
    model_id = cfg.model.name
    raw: str | None = None
    start = time.perf_counter()
    calls = 0
    for attempt in (1, 2):
        calls += 1
        try:
            completion: Completion = await client.complete(cfg.model, messages, SEED, JudgeVerdict)
        except GenerationError as e:
            error = f"provider error: {e}"
            break
        tokens_in += completion.prompt_tokens
        tokens_out += completion.completion_tokens
        tokens_reason += completion.reasoning_tokens
        raw = completion.content
        model_id = getattr(client, "last_model", None) or model_id
        try:
            verdict = parse_reply(completion.content, reply)
            error = None
            break
        except ValueError as e:
            error = str(e)
            if attempt == 1:
                messages = [*messages, {"role": "assistant", "content": completion.content},
                            {"role": "user", "content": REPAIR.format(error=e)}]  # fmt: skip
    result = JudgeResult(
        case_id=item.case_id,
        criterion=cfg.criterion.value,
        verdict=verdict.verdict if verdict else "judge_error",
        reason=verdict.reason if verdict else None,
        evidence=verdict.evidence if verdict else None,
        error=None if verdict else error,
        model=model_id,
        prompt_sha256=cfg.prompt_sha256,
        rubric_hash=cfg.rubric_hash,
        input_sha256=item.input_hash,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        reasoning_tokens=tokens_reason,
        latency_ms=round((time.perf_counter() - start) * 1000, 1),
        attempts=calls,
        cache_hit=False,
        thinking=getattr(client, "thinking", thinking),
        raw=raw,
        timestamp=datetime.now(UTC),
    )
    return result, calls


def check_budget(calls: int, max_calls: int, ledger: Ledger | None, daily_cap: int) -> None:
    """Refuse to start when the first attempts alone would exceed a cap (repairs are
    checked as they happen: run_judge stops before a call and its repair could exceed)."""
    if calls > max_calls:
        raise BudgetExceeded(f"{calls} calls needed, --max-calls is {max_calls}")
    if ledger is not None and ledger.calls_today() + calls > daily_cap:
        raise BudgetExceeded(
            f"{calls} calls needed, {ledger.calls_today()} already made today, daily cap "
            f"{daily_cap}"
        )


async def run_judge(
    client: LLMClient,
    cfg: JudgeConfig,
    plan: Plan,
    replies: Mapping[str, AgentResponse],
    cache: JudgeCache,
    *,
    max_calls: int,
    daily_cap: int,
    ledger: Ledger | None,
    use_cache: bool = True,
    write_cache: bool = True,
    thinking: str = "n/a",
    stop_on_refusal: bool = False,
) -> tuple[list[JudgeResult], int]:
    """Judge every item of ``plan``; returns the results (sorted) and the calls made. Calls
    are HTTP requests to the provider (``client.requests``, retries included) when the
    client counts them, else judge attempts."""
    check_budget(plan.calls if use_cache else len(plan.items), max_calls, ledger, daily_cap)
    results: list[JudgeResult] = []
    made = 0
    start_requests = getattr(client, "requests", None)
    start_refused = getattr(client, "refused", 0)
    for item in plan.items:
        hit = cache.get(item.key) if use_cache else None
        if hit is not None:
            # the cached verdict is for the same input; it is reported under this item's id
            results.append(hit.model_copy(update={"cache_hit": True, "case_id": item.case_id}))
            continue
        today = ledger.calls_today() if ledger is not None else 0
        if made + CALLS_PER_CASE_MAX > max_calls or today + CALLS_PER_CASE_MAX > daily_cap:
            raise BudgetExceeded(
                f"stopped before {item.case_id}: {made} calls made, a call and its repair "
                f"could exceed --max-calls {max_calls} or the daily cap {daily_cap}"
            )
        result, calls = await judge_one(client, cfg, item, replies[item.case_id].reply, thinking)
        requests = getattr(client, "requests", None)
        if requests is not None and start_requests is not None:
            made = requests - start_requests  # real HTTP requests, retries included
        else:
            made += calls
        refused = getattr(client, "refused", 0) - start_refused
        if stop_on_refusal and refused:
            if write_cache and result.verdict != "judge_error":
                cache.put(item.key, result)
            raise CallRefused(
                f"stopped after {item.case_id}: {refused} request(s) refused by the provider "
                f"({made} made; see the usage ledger for the status and error)"
            )
        if write_cache and result.verdict != "judge_error":
            cache.put(item.key, result)
        results.append(result)
    return sorted(results, key=lambda r: r.case_id), made


def cached_results(plan: Plan, cache: JudgeCache) -> list[JudgeResult]:
    """The plan's results already in the cache (no call): e.g. a run that stopped early."""
    found = [cache.get(item.key) for item in plan.items]
    return sorted((r for r in found if r is not None), key=lambda r: r.case_id)
