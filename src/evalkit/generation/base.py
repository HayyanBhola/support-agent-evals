"""Shared pieces of the generators: context, difficulty rule, ids, tags and batch schemas.

Division of labour (Day 5 ground rule): the LLM only writes question text (and, for
knowledge cases, a reference answer that must pass the grounding check). Every label --
expected_route, should_escalate, gold facts, must_* rules, safety_critical -- is set by
code from SYSTEM_RULES.md, products.json, seed_data.sql and the chunks.
"""

import hashlib
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from evalkit.generation.filters import Candidate, Language, Rejection
from evalkit.generation.llm import BadOutput, GenLLM, ModelConfig
from evalkit.generation.prompts import Prompt, load_prompt
from evalkit.generation.sources import Sources
from evalkit.schemas import CaseSource, Difficulty, Route

# Ask for this many candidates per needed case, so filter rejections don't leave gaps.
OVERGENERATE: Final = 1.5


class Evolution(StrEnum):
    PLAIN = "plain"
    MULTIHOP = "multihop"
    CONDITIONAL = "conditional"
    CASUAL = "casual"
    ROMAN_URDU = "roman_urdu"
    TRAP = "trap"  # hallucination traps, unanswerable questions, fake ids
    CONTROL = "control"  # near-miss controls: look like one route, belong to another


# Difficulty rule. Base by category (the Day 4 legacy rule, datasets/README.md), then:
# multi-hop and conditional are always hard; casual, Roman Urdu, trap and control cases go
# up one level.
CATEGORY_DIFFICULTY: Final = {
    "greeting": Difficulty.EASY,
    "database": Difficulty.EASY,
    "clarify": Difficulty.EASY,
    "escalation": Difficulty.EASY,
    "knowledge": Difficulty.MEDIUM,
    "medical": Difficulty.MEDIUM,
    "out_of_scope": Difficulty.MEDIUM,
    "robustness": Difficulty.MEDIUM,
    "adversarial": Difficulty.HARD,
    "memory": Difficulty.HARD,
}
_LEVELS: Final = [Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD]
_BUMP: Final = {Evolution.CASUAL, Evolution.ROMAN_URDU, Evolution.TRAP, Evolution.CONTROL}


def difficulty(category: str, evolution: Evolution) -> Difficulty:
    if evolution in (Evolution.MULTIHOP, Evolution.CONDITIONAL):
        return Difficulty.HARD
    level = _LEVELS.index(CATEGORY_DIFFICULTY[category]) + (evolution in _BUMP)
    return _LEVELS[min(level, len(_LEVELS) - 1)]


def slug(text: str) -> str:
    return text.replace("_", "-")


def case_id(generator: str, subtype: str, question: str) -> str:
    digest = hashlib.sha256(f"{generator}|{subtype}|{question}".encode()).hexdigest()[:8]
    return f"syn-{slug(generator)}-{digest}"


def language_of(evolution: Evolution) -> Language:
    return Language.ROMAN_URDU if evolution is Evolution.ROMAN_URDU else Language.ENGLISH


def make_candidate(
    *,
    generator: str,
    subtype: str,
    evolution: Evolution,
    question: str,
    category: str,
    expected_route: Route | None,
    slot: str | None = None,
    tags: Sequence[str] = (),
    **fields: Any,
) -> Candidate:
    """A candidate with the fields every synthetic case shares."""
    question = question.strip()
    all_tags = [f"gen-{slug(generator)}", f"evo-{slug(evolution.value)}", f"type-{slug(subtype)}"]
    all_tags += [t for t in tags if t not in all_tags]
    data: dict[str, Any] = {
        "id": case_id(generator, subtype, question),
        "question": question,
        "category": category,
        "expected_route": expected_route,
        "difficulty": difficulty(category, evolution),
        "source": CaseSource.SYNTHETIC,
        "tags": all_tags,
        **fields,
    }
    return Candidate(generator, subtype, slot or subtype, language_of(evolution), data)


@dataclass(frozen=True)
class Slot:
    """``n`` cases of one subtype in one style."""

    subtype: str
    n: int
    evolution: Evolution = Evolution.PLAIN

    @property
    def key(self) -> str:
        return f"{self.subtype}/{self.evolution.value}"

    @property
    def request(self) -> int:
        """How many candidates to ask the LLM for."""
        return math.ceil(self.n * OVERGENERATE) + 1


@dataclass
class GenOutput:
    candidates: list[Candidate] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    quotas: dict[str, int] = field(default_factory=dict)


@dataclass
class GenContext:
    llm: GenLLM
    models: Mapping[str, ModelConfig]  # generator name -> model
    sources: Sources
    seed: int
    prompt_versions: Mapping[str, int] = field(default_factory=dict)

    def rng(self, name: str) -> random.Random:
        """A generator's own RNG: same seed + name -> same choices, whatever runs before."""
        return random.Random(f"{self.seed}:{name}")

    def model(self, generator: str) -> ModelConfig:
        return self.models[generator]

    def prompt(self, name: str) -> Prompt:
        return load_prompt(name, self.prompt_versions.get(name, 1))


# --------------------------------------------------------------------------- batch schemas


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore")


class QuestionItem(_Out):
    question: str = Field(min_length=1)
    payload: str | None = None  # injection: the text the attacker wants the bot to say


class QuestionBatch(_Out):
    questions: list[QuestionItem]


async def ask_batch(
    ctx: GenContext,
    generator: str,
    slot: Slot,
    out: GenOutput,
    *,
    prompt_name: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> list[QuestionItem]:
    """Ask for ``slot.request`` questions of one subtype; a bad reply becomes a rejection."""
    inputs = {
        "subtype": slot.subtype,
        "count": slot.request,
        "language": language_of(slot.evolution).value,
        "style": slot.evolution.value,
        **(extra or {}),
    }
    try:
        batch = await ctx.llm.ask(
            ctx.prompt(prompt_name or generator),
            inputs,
            QuestionBatch,
            ctx.model(generator),
            generator=generator,
            est_output_tokens=200 + 45 * slot.request,
        )
    except BadOutput as e:
        out.rejections.append(
            Rejection(generator=generator, subtype=slot.subtype, stage="llm", reason=str(e))
        )
        return []
    return batch.questions
