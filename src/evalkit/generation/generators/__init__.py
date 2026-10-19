"""The generators in run order (safety routes first) and the targets they aim for."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal

from evalkit.generation.base import GenContext, GenOutput
from evalkit.generation.generators.conversation import (
    generate_clarify,
    generate_escalate,
    generate_greeting,
    generate_out_of_scope,
)
from evalkit.generation.generators.database import generate_database
from evalkit.generation.generators.knowledge import generate_knowledge
from evalkit.generation.generators.safety import generate_guardrail, generate_medical

Tier = Literal["large", "small"]


@dataclass(frozen=True)
class GeneratorSpec:
    name: str
    run: Callable[[GenContext], Awaitable[GenOutput]]
    tier: Tier  # small: database paraphrasing only (--paraphrase-model); large: the rest


GENERATORS: Final = (
    GeneratorSpec("guardrail", generate_guardrail, "large"),
    GeneratorSpec("medical", generate_medical, "large"),
    GeneratorSpec("knowledge", generate_knowledge, "large"),
    GeneratorSpec("escalate", generate_escalate, "large"),
    GeneratorSpec("database", generate_database, "small"),
    GeneratorSpec("greeting", generate_greeting, "large"),
    GeneratorSpec("clarify", generate_clarify, "large"),
    GeneratorSpec("out_of_scope", generate_out_of_scope, "large"),
)
GENERATOR_NAMES: Final = tuple(g.name for g in GENERATORS)

# Gap per route after Day 4 (datasets/README.md) and the Day 5 target (~1.5x the gap).
ROUTE_GAP: Final = {
    "knowledge": 50,
    "database": 25,
    "greeting": 25,
    "escalate": 15,
    "clarify": 17,
    "guardrail": 22,
    "medical": 20,
}
# Cases per generator (all subtypes, including off-route ones like controls and traps).
GENERATOR_TARGET: Final = {
    "guardrail": 45,  # 33 guardrail + 6 discount (route unchecked) + 6 fake price (database)
    "medical": 30,
    "knowledge": 75,  # 45 plain + 22 evolved + 8 unanswerable
    "escalate": 23,
    "database": 38,  # 34 database + 4 nonexistent products (route unchecked)
    "greeting": 38,  # 28 greetings + 10 near-miss controls
    "clarify": 26,
    "out_of_scope": 15,
}
