"""What the labeling app shows for a record, extracted from the saved response (no scores).

Deliberately contains no automated checks (rule results, route match): the labeler must not
be anchored by them.
"""

from dataclasses import dataclass
from typing import Any

from evalkit.schemas import AgentResponse, DatabaseDetails, KnowledgeDetails, LabelSample, Route


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str


def chunks(response: AgentResponse) -> list[Chunk]:
    """The knowledge-base chunks the agent used (details.chunks_used), else those retrieved."""
    details = response.parsed_details
    if isinstance(details, KnowledgeDetails) and details.chunks_used:
        return [Chunk(c.id, c.text) for c in details.chunks_used]
    return [Chunk(c.id, c.text) for c in response.retrieved_chunks or []]


def db_facts(response: AgentResponse) -> list[dict[str, Any]]:
    """The database facts in ``details`` (database route), as plain dicts: product rows, or
    the order record of an order lookup (``{"order_id": ..., "status": ...}`` or
    ``{"order_id": ..., "found": false}``)."""
    details = response.parsed_details
    if isinstance(details, DatabaseDetails):
        return [p.model_dump(mode="json", exclude_none=True) for p in details.products]
    if response.route is Route.DATABASE and isinstance(details, dict):
        return [details]
    return []


def facts_table(sample: LabelSample) -> dict[str, str]:
    """The response metadata and case gold fields, as display strings."""
    r, c = sample.response, sample.case
    return {
        "actual route": r.route.value if r.route else "-",
        "answer_type": r.answer_type.value if r.answer_type else "-",
        "escalated": "-" if r.escalated is None else str(r.escalated).lower(),
        "expected_route": c.expected_route.value if c.expected_route else "(not checked)",
        "should_escalate": "-" if c.should_escalate is None else str(c.should_escalate).lower(),
    }


def escalation_facts(should_escalate: bool | None, escalated: bool | None) -> str:
    """Shown next to the escalation_appropriate radio: both values, side by side."""

    def word(v: bool | None) -> str:
        return "not set" if v is None else ("yes" if v else "no")

    return f"escalated: **{word(escalated)}** · should_escalate: **{word(should_escalate)}**"
