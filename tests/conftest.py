from pathlib import Path
from typing import Any

import pytest
from typer import rich_utils

from evalkit.schemas import AgentResponse

FIXTURES = Path(__file__).parent / "fixtures"
RESPONSES = FIXTURES / "responses"
ROUTE_FIXTURES = [
    "knowledge",
    "database",
    "greeting",
    "clarify",
    "escalate",
    "guardrail",
    "medical",
]


@pytest.fixture(autouse=True)
def plain_typer_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Typer forces ANSI colours in its usage/error panels when GITHUB_ACTIONS, FORCE_COLOR or
    PY_COLORS is set (read once at import), which breaks message checks in CI; keep them plain."""
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", False)


def load_response(name: str) -> AgentResponse:
    return AgentResponse.model_validate_json((RESPONSES / f"{name}.json").read_bytes())


@pytest.fixture
def case_data() -> dict[str, Any]:
    """A minimal valid TestCase payload; tests override fields to break one rule at a time."""
    return {
        "id": "shipping-policy-01",
        "question": "What is your shipping policy?",
        "category": "policy",
        "expected_route": "knowledge",
        "must_include": ["shipping"],
        "source": "human",
    }
