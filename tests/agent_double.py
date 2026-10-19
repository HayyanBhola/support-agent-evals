"""A scriptable fake GlowCare agent on httpx.MockTransport (no network, no real agent)."""

import asyncio
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from evalkit.adapters.glowcare import GlowCareAdapter
from evalkit.schemas import CaseSource, Route, TestCase

from .conftest import RESPONSES

SENTINEL_KEY = "sk-test-SENTINEL-KEY-9f8e7d6c5b4a"
AGENT_URL = "http://agent.test/chat"
HEALTH = {"status": "ok", "database": "ok", "llm": "gemini/test-model", "api_key_required": True}


def fixture(name: str, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((RESPONSES / f"{name}.json").read_text(encoding="utf-8"))
    return data | overrides


@dataclass
class Reply:
    """One scripted HTTP reply (built fresh for every request)."""

    status: int = 200
    json: Any = None
    text: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def build(self) -> httpx.Response:
        if self.text is not None:
            return httpx.Response(self.status, text=self.text, headers=self.headers)
        return httpx.Response(self.status, json=self.json, headers=self.headers)


Step = Reply | Callable[[], BaseException]


def ok(name: str = "knowledge", **overrides: Any) -> Reply:
    return Reply(json=fixture(name, **overrides))


def raises(exc_type: type[BaseException], msg: str = "simulated") -> Callable[[], BaseException]:
    return lambda: exc_type(msg)


class FakeAgent:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.scripts: dict[str, list[Step]] = {"hi": [ok("greeting")]}
        self.default: Step = ok("knowledge")
        self.health: Step = Reply(json=HEALTH)
        self.messages: list[str] = []
        self.bodies: list[dict[str, Any]] = []
        self.api_keys: list[str | None] = []
        self.sleeps: list[float] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.health_calls = 0

    def on(self, message: str, *steps: Step) -> "FakeAgent":
        """Reply to ``message`` with ``steps`` in order; the last one repeats."""
        self.scripts[message] = list(steps)
        return self

    def calls(self, message: str) -> int:
        return Counter(self.messages)[message]

    @staticmethod
    def _play(step: Step) -> httpx.Response:
        if isinstance(step, Reply):
            return step.build()
        raise step()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.api_keys.append(request.headers.get("x-api-key"))
        if request.url.path == "/health":
            self.health_calls += 1
            return self._play(self.health)
        body = json.loads(request.content)
        self.bodies.append(body)
        message = body["message"]
        seen = self.calls(message)
        self.messages.append(message)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            steps = self.scripts.get(message, [self.default])
            return self._play(steps[min(seen, len(steps) - 1)])
        finally:
            self.in_flight -= 1

    async def fake_sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def adapter(self, **kwargs: Any) -> GlowCareAdapter:
        return GlowCareAdapter(
            AGENT_URL,
            SENTINEL_KEY,
            transport=httpx.MockTransport(self.handler),
            sleep=self.fake_sleep,
            **kwargs,
        )


def make_case(case_id: str, question: str, route: str = "knowledge", **over: Any) -> TestCase:
    data: dict[str, Any] = {
        "id": case_id,
        "question": question,
        "category": "test",
        "expected_route": Route(route),
        "should_escalate": route == "escalate",
        "source": CaseSource.HUMAN,
    }
    if route == "database":
        data["must_include"] = ["24.99"]
    return TestCase.model_validate(data | over)
