"""What a judge returns (``JudgeVerdict``) and what we record per case (``JudgeResult``)."""

import re
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_REASON_SENTENCES: Final = 2
MAX_REASON_CHARS: Final = 400
MAX_EVIDENCE_CHARS: Final = 300
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


class JudgeVerdict(BaseModel):
    """The strict JSON a judge must reply with."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["pass", "fail"]
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)
    evidence: str = Field(min_length=1, max_length=MAX_EVIDENCE_CHARS)

    @field_validator("reason")
    @classmethod
    def _short(cls, v: str) -> str:
        if len(_SENTENCE_END.findall(v.strip())) > MAX_REASON_SENTENCES:
            raise ValueError(f"reason must be at most {MAX_REASON_SENTENCES} sentences")
        return v.strip()

    @field_validator("evidence")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class JudgeResult(BaseModel):
    """One judge call's outcome for one case and criterion, with its provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    criterion: str
    verdict: Literal["pass", "fail", "judge_error"]
    reason: str | None = None
    evidence: str | None = None
    error: str | None = None  # why it is a judge_error
    model: str  # the exact model id the API reported
    prompt_sha256: str
    rubric_hash: str
    input_sha256: str
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: float = 0.0
    attempts: int = 1  # 2 when the repair retry was used
    cache_hit: bool = False
    thinking: str  # how thinking output was disabled or stripped
    raw: str | None = None  # the model's last reply exactly as received (for audit)
    timestamp: datetime
