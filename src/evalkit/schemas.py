"""Data contracts shared by every evalkit module.

Two kinds of models live here:

* Our own persisted records (TestCase, CaseResult, Label, MetricResult, RunMeta). They are
  strict: unknown fields are rejected, bools and ints are not coerced, and each one carries
  ``schema_version`` so files written today can be detected and migrated later.
* The agent's response (AgentResponse and its parts). The agent is a separate project, so
  these are tolerant: unknown fields are kept and exposed via ``unknown_fields``.

Computed values on AgentResponse are plain properties, not ``computed_field``s, so they are
never serialized and a dumped response re-parses without picking up "unknown" fields.
"""

from contextlib import suppress
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    model_validator,
)

SCHEMA_VERSION: Final = 1

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]*$")]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,40}$")]
GoldFactValue = StrictStr | StrictBool | StrictInt | StrictFloat | list[StrictStr]
NonEmptyGroup = Annotated[list[NonEmptyStr], Field(min_length=1)]


# --------------------------------------------------------------------------- enums


class Route(StrEnum):
    ESCALATE = "escalate"
    GUARDRAIL = "guardrail"
    MEDICAL = "medical"
    GREETING = "greeting"
    CLARIFY = "clarify"
    DATABASE = "database"
    KNOWLEDGE = "knowledge"


class ResponseSource(StrEnum):
    DATABASE = "database"
    KNOWLEDGE_BASE = "knowledge_base"
    FIXED_REPLY = "fixed_reply"


class AnswerType(StrEnum):
    LLM = "llm"
    QUOTED_SOURCE = "quoted_source"
    NOT_FOUND = "not_found"


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class CaseSource(StrEnum):
    LEGACY = "legacy"
    SYNTHETIC = "synthetic"
    HUMAN = "human"
    PRODUCTION = "production"


class Criterion(StrEnum):
    FAITHFULNESS = "faithfulness"
    CORRECTNESS = "correctness"
    RELEVANCE = "relevance"
    COMPLETENESS = "completeness"
    ESCALATION_APPROPRIATE = "escalation_appropriate"
    SAFETY = "safety"
    TONE = "tone"


class LabelValue(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    N_A = "n_a"


class FailureType(StrEnum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    HTTP_ERROR = "http_error"
    AGENT_ERROR = "agent_error"
    SKIPPED = "skipped"
    PARSE_ERROR = "parse_error"


# --------------------------------------------------------------------------- base classes


class _Record(BaseModel):
    """Base for our own models: unknown fields are an error."""

    model_config = ConfigDict(extra="forbid")


class _AgentPart(BaseModel):
    """Base for parts of the agent's response: unknown fields are kept."""

    model_config = ConfigDict(extra="allow")


# --------------------------------------------------------------------------- test cases


class Turn(_Record):
    """One prior message in a multi-turn case."""

    model_config = ConfigDict(frozen=True)

    role: Literal["user", "assistant"]
    content: NonEmptyStr


class TestCase(_Record):
    """One evaluation case: a question plus everything we expect of the answer."""

    __test__ = False  # not a pytest test class

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    id: Slug
    question: NonEmptyStr
    category: Slug
    expected_route: Route | None  # required; null = route not checked (legacy cases do this)
    should_escalate: StrictBool | None = None
    history: list[Turn] = []
    reference_answer: NonEmptyStr | None = None
    gold_context_ids: list[NonEmptyStr] = []
    gold_db_facts: dict[NonEmptyStr, GoldFactValue] = {}
    must_include: list[NonEmptyStr] = []
    must_include_any: list[NonEmptyGroup] = []
    must_not_include: list[NonEmptyStr] = []
    safety_critical: StrictBool = False
    difficulty: Difficulty = Difficulty.MEDIUM
    source: CaseSource
    tags: list[Slug] = []

    @model_validator(mode="after")
    def _check_rules(self) -> "TestCase":
        asserts_something = (
            self.reference_answer is not None
            or self.must_include
            or self.must_include_any
            or self.must_not_include
            or self.gold_db_facts
            or self.gold_context_ids
            or self.should_escalate is not None
        )
        if not asserts_something:
            raise ValueError(
                f"case {self.id!r} asserts nothing: set at least one of reference_answer, "
                "must_include, must_include_any, must_not_include, gold_db_facts, "
                "gold_context_ids or should_escalate"
            )
        if self.expected_route is Route.ESCALATE and self.should_escalate is False:
            raise ValueError(
                f"case {self.id!r}: expected_route is 'escalate' but should_escalate is False"
            )
        if self.expected_route is Route.DATABASE and not (self.gold_db_facts or self.must_include):
            raise ValueError(f"case {self.id!r}: database cases need gold_db_facts or must_include")
        overlap = {s.casefold() for s in self.must_include} & {
            s.casefold() for s in self.must_not_include
        }
        if overlap:
            raise ValueError(
                f"case {self.id!r}: must_include and must_not_include overlap "
                f"(case-insensitive): {sorted(overlap)}"
            )
        return self


# --------------------------------------------------------------------------- agent response


class RetrievedChunk(_AgentPart):
    """A knowledge-base chunk the agent retrieved.

    ``score`` is a Chroma distance: LOWER score = MORE relevant.
    """

    id: str
    text: str
    score: float
    source: str | None = None


class ChunkUsed(_AgentPart):
    """A chunk that went into the LLM prompt (``details.chunks_used``).

    ``distance`` is a Chroma distance: LOWER = MORE relevant.
    """

    id: str
    text: str
    source: str | None = None
    section: str | None = None
    distance: float | None = None


class KnowledgeDetails(_AgentPart):
    """``details`` of a knowledge-route response."""

    question: str | None = None
    chunks_used: list[ChunkUsed] = []
    prompt_sent_to_llm: str | None = None
    llm_answer: str | None = None
    problems: list[Any] = []
    answer: str | None = None
    answer_type: AnswerType | None = None
    llm_error: str | None = None


class ProductRecord(_AgentPart):
    """One product row from a database-route response."""

    product_id: str
    name: str
    price: float | None = None
    currency: str | None = None
    stock_quantity: int | None = None
    availability: str | None = None
    ingredients: list[str] = []


class DatabaseDetails(_AgentPart):
    """``details`` of a database-route response. The agent sends a bare list of products."""

    products: list[ProductRecord]


class AgentResponse(_AgentPart):
    """The JSON body returned by the agent's ``POST /chat``.

    Only ``ok`` and ``reply`` are required; every other field is Optional because at least
    one route (or the error branch) sends it as null or omits it.
    """

    ok: bool
    reply: str
    conversation_id: Any | None = None
    new_chat: bool | None = None
    route: Route | None = None
    source: ResponseSource | None = None
    answer_type: AnswerType | None = None
    escalated: bool | None = None
    escalation: Any | None = None
    memory: Any | None = None
    error: str | None = None
    request_id: str | None = None
    eval_mode: bool | None = None
    model: str | None = None
    latency_ms: float | None = None
    reason: str | None = None
    intents: list[str] | None = None
    details: dict[str, Any] | list[Any] | None = None
    retrieved_chunks: list[RetrievedChunk] | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def unknown_fields(self) -> dict[str, Any]:
        """Fields the agent sent that this model does not know about."""
        return dict(self.model_extra or {})

    @property
    def llm_called(self) -> bool:
        """True if the agent spent prompt tokens. Never inferred from ``model``."""
        return (self.prompt_tokens or 0) > 0

    @property
    def total_tokens(self) -> int:
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)

    @property
    def is_error(self) -> bool:
        return not self.ok

    @property
    def checker_replaced(self) -> bool:
        """The LLM answered but the agent's checker replaced it with a quote or not-found."""
        return self.llm_called and self.answer_type in (
            AnswerType.QUOTED_SOURCE,
            AnswerType.NOT_FOUND,
        )

    @property
    def parsed_details(
        self,
    ) -> KnowledgeDetails | DatabaseDetails | dict[str, Any] | list[Any] | None:
        """``details`` parsed by route; the raw value if it does not fit the typed model."""
        with suppress(ValidationError):
            if self.route is Route.KNOWLEDGE and isinstance(self.details, dict):
                return KnowledgeDetails.model_validate(self.details)
            if self.route is Route.DATABASE and isinstance(self.details, list):
                return DatabaseDetails.model_validate({"products": self.details})
        return self.details


# --------------------------------------------------------------------------- results


class CaseResult(_Record):
    """The outcome of sending one TestCase to the agent in one run.

    Exactly one of ``response`` / ``failure_type`` is set, except ``agent_error``: the agent
    answered with ``ok: false``, so the response is kept (with ``is_error`` true) as well.
    """

    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: Slug
    case_id: Slug
    response: AgentResponse | None = None
    failure_type: FailureType | None = None
    error_message: str | None = None
    attempts: StrictInt = Field(ge=0)
    cache_hit: StrictBool = False
    wall_latency_ms: NonNegativeFloat | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime

    @model_validator(mode="after")
    def _check_outcome(self) -> "CaseResult":
        if self.failure_type is FailureType.AGENT_ERROR:
            if self.response is None or not self.response.is_error:
                raise ValueError("failure_type 'agent_error' needs a response with ok=false")
        elif (self.response is None) == (self.failure_type is None):
            raise ValueError("exactly one of response or failure_type must be set")
        elif self.response is not None and self.response.is_error:
            raise ValueError("a response with ok=false must have failure_type 'agent_error'")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        return self


class Label(_Record):
    """One labeling of one response: a verdict on every criterion, from a human or a judge.

    One Label per save; the labels file is append-only and, per (case_id, labeler), the
    latest row wins. ``overrides`` lists criteria the rubric's applicability marked n_a but
    the labeler judged anyway.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    case_id: Slug
    run_id: Slug
    labeler: NonEmptyStr
    timestamp: AwareDatetime
    rubric_hash: Sha256Hex  # evalkit.rubrics.rubric_hash() of the rubric the verdicts used
    response_sha256: Sha256Hex  # stable_hash of the labeled response (catches a changed reply)
    verdicts: dict[Criterion, LabelValue]
    overrides: list[Criterion] = []
    notes: str | None = None
    seconds_spent: NonNegativeFloat

    @model_validator(mode="after")
    def _check_verdicts(self) -> "Label":
        missing = [c.value for c in Criterion if c not in self.verdicts]
        if missing:
            raise ValueError(f"label for {self.case_id!r} has no verdict for {missing}")
        if len(set(self.overrides)) != len(self.overrides):
            raise ValueError("overrides lists a criterion twice")
        return self

    @property
    def fails(self) -> list[Criterion]:
        return [c for c in Criterion if self.verdicts[c] is LabelValue.FAIL]


class SampleStratum(_Record):
    """The stratum a LabelSample was drawn from."""

    model_config = ConfigDict(frozen=True)

    expected_route: Route | None
    answer_type: AnswerType | None  # None: the response has no answer_type (fixed replies, db)


class LabelSample(_Record):
    """One record to label: a case (with its gold fields) and the saved response to judge."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    case_id: Slug
    run_id: Slug
    stratum: SampleStratum
    case: TestCase
    response: AgentResponse
    response_sha256: Sha256Hex

    @model_validator(mode="after")
    def _check_ids(self) -> "LabelSample":
        if self.case.id != self.case_id:
            raise ValueError(f"case_id {self.case_id!r} != case.id {self.case.id!r}")
        return self


class MetricResult(_Record):
    """One metric computed for one case. At least one of value / passed is set."""

    schema_version: Literal[1] = SCHEMA_VERSION
    case_id: Slug
    metric: Slug
    value: StrictFloat | StrictInt | None = None
    passed: StrictBool | None = None
    details: dict[str, Any] = {}

    @model_validator(mode="after")
    def _check_has_result(self) -> "MetricResult":
        if self.value is None and self.passed is None:
            raise ValueError(f"metric {self.metric!r}: set value, passed, or both")
        return self


class RunCounts(_Record):
    total: NonNegativeInt
    ok: NonNegativeInt
    failed: NonNegativeInt
    cache_hits: NonNegativeInt

    @model_validator(mode="after")
    def _check_totals(self) -> "RunCounts":
        if self.ok + self.failed != self.total:
            raise ValueError(f"ok ({self.ok}) + failed ({self.failed}) != total ({self.total})")
        if self.cache_hits > self.total:
            raise ValueError(f"cache_hits ({self.cache_hits}) > total ({self.total})")
        return self


class RunMeta(_Record):
    """Everything needed to reproduce and compare a run."""

    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: Slug
    git_sha: GitSha | None
    git_dirty: StrictBool
    dataset_path: NonEmptyStr
    dataset_sha256: Sha256Hex
    dataset_version: str | None = None
    judge_versions: dict[str, str] = {}
    config_hash: Sha256Hex
    agent_model: str | None = None
    agent_health: dict[str, Any] = {}
    # the agent's code: {"commit", "branch", "dirty_code"} from --agent-repo (Day 25)
    agent_repo: dict[str, Any] = {}
    # pacing and stop settings (max_rpm, max_llm_calls, stop_on_failure, max_retries)
    run_settings: dict[str, Any] = {}
    concurrency: StrictInt = Field(ge=1)
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    counts: RunCounts

    @model_validator(mode="after")
    def _check_times(self) -> "RunMeta":
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        return self
