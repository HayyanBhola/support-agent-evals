"""Knowledge generator: questions answerable only from the knowledge-base chunks.

* plain: every chunk gets one case (a few seeded extra chunks get two), so all 41 chunks and
  all 8 source files are covered; if no candidate for a chunk passes the grounding check,
  the chunk is asked once more with the failure reasons attached;
* evolved (~30%): multi-hop (two related chunks, both ids gold), conditional, casual /
  misspelled, Roman Urdu;
* unanswerable: plausible policy questions on topics no chunk mentions (checked against the
  whole knowledge base), with a "not available" reference.

The LLM writes question, reference_answer and key facts; the grounding check
(``evalkit.generation.grounding``) rejects any reference with a number, policy term or key
fact its chunk(s) don't contain. Labels by rule: answerable -> expected_route knowledge,
should_escalate false (rule 5: a grounded policy answer needs no hand-off); unanswerable ->
should_escalate null (rule 5 says "offer" escalation, rule 9's "low confidence" says create
one: the open product question from Day 4).
"""

import random
from collections.abc import Sequence
from typing import Final

from pydantic import Field

from evalkit.generation.base import (
    Evolution,
    GenContext,
    GenOutput,
    _Out,
    language_of,
    make_candidate,
)
from evalkit.generation.filters import Rejection
from evalkit.generation.grounding import check_grounding, normalize
from evalkit.generation.llm import BadOutput
from evalkit.importers.chunks import ChunkRecord
from evalkit.schemas import Route

GEN: Final = "knowledge"
PLAIN_EXTRA: Final = 4  # chunks that get a second plain case (45 plain in total)
EVOLVED: Final = {
    Evolution.MULTIHOP: 6,
    Evolution.CONDITIONAL: 6,
    Evolution.CASUAL: 5,
    Evolution.ROMAN_URDU: 5,
}
CANDIDATES_PER_CALL: Final = 2
UNANSWERABLE_N: Final = 8
# Sent (as an input field, so the prompt and every other cached call stay unchanged) when no
# plain candidate for a chunk passed the grounding check.
RETRY_FEEDBACK: Final = (
    "Every case in your previous reply failed the grounding check: {problems}. Copy each "
    "key-fact phrase character for character from the chunk text (2-6 words, not a whole "
    "line), and make the reference answer contain one phrase of every group verbatim."
)

# Related source files: multi-hop pairs are drawn from one family.
FAMILIES: Final = (
    ("return_policy.md", "refund_policy.md"),
    ("shipping_policy.md", "cancellation_policy.md"),
    ("product_guide.md", "ingredients_guide.md"),
    ("customer_support_policy.md", "faq.md"),
)

# (topic, words that would show the topic IS in the knowledge base)
UNANSWERABLE_TOPICS: Final = (
    ("price matching with other shops", ("price match",)),
    ("loyalty or reward points", ("loyalty", "reward", "points")),
    ("gift wrapping", ("gift wrap",)),
    ("gift cards", ("gift card",)),
    ("product warranty", ("warranty",)),
    ("picking up an order from a physical store", ("pickup", "pick up", "physical store")),
    ("subscriptions or auto-replenishment", ("subscription", "subscribe")),
    ("exchanging a product for a different one", ("exchange",)),
    ("restocking fees on returns", ("restocking",)),
    ("shelf life or expiry date after opening", ("shelf life", "expir")),
    ("shipping insurance for lost parcels", ("insurance",)),
)
UNANSWERABLE_REFERENCE: Final = (
    "GlowCare's policies don't cover this, so the information isn't available; the "
    "assistant should say so rather than guess, and can offer to pass the question to the "
    "support team."
)


class KnowledgeItem(_Out):
    question: str = Field(min_length=1)
    reference_answer: str = Field(min_length=1)
    key_facts: list[list[str]] = Field(min_length=1)


class KnowledgeBatch(_Out):
    cases: list[KnowledgeItem]


class TopicQuestion(_Out):
    topic: str
    question: str = Field(min_length=1)


class TopicBatch(_Out):
    questions: list[TopicQuestion]


async def generate_knowledge(ctx: GenContext) -> GenOutput:
    out = GenOutput()
    rng = ctx.rng(GEN)
    chunks = list(ctx.sources.chunks)

    extra = {c.id for c in rng.sample(chunks, PLAIN_EXTRA)}
    for chunk in chunks:
        quota = 2 if chunk.id in extra else 1
        slot = f"plain/{chunk.id}"
        problems = await _ask_cases(ctx, out, Evolution.PLAIN, [chunk], slot, quota)
        if problems is not None and not any(c.slot == slot for c in out.candidates):
            feedback = RETRY_FEEDBACK.format(problems=" | ".join(problems))
            await _ask_cases(ctx, out, Evolution.PLAIN, [chunk], slot, quota, feedback)

    for evolution, n in EVOLVED.items():
        for _ in range(n):
            picked = _pick_pair(rng, chunks) if evolution is Evolution.MULTIHOP else [
                rng.choice(chunks)
            ]  # fmt: skip
            await _ask_cases(ctx, out, evolution, picked, evolution.value, n)

    await _unanswerable(ctx, out)
    return out


def _pick_pair(rng: random.Random, chunks: Sequence[ChunkRecord]) -> list[ChunkRecord]:
    family = rng.choice(FAMILIES)
    pool = [c for c in chunks if c.source in family]
    first = rng.choice(pool)
    # prefer the other file of the family, so the hop crosses documents
    others = [c for c in pool if c.source != first.source] or [c for c in pool if c != first]
    return sorted([first, rng.choice(others)], key=lambda c: c.id)


async def _ask_cases(
    ctx: GenContext,
    out: GenOutput,
    evolution: Evolution,
    chunks: list[ChunkRecord],
    slot: str,
    quota: int,
    feedback: str | None = None,
) -> list[str] | None:
    """Add grounded candidates for ``slot``; returns the grounding problems seen (None if
    the call failed)."""
    out.quotas[slot] = quota
    count = quota + 1 if evolution is Evolution.PLAIN else CANDIDATES_PER_CALL
    inputs: dict[str, object] = {
        "style": evolution.value,
        "language": language_of(evolution).value,
        "count": count,
        "chunks": [{"id": c.id, "text": c.text} for c in chunks],
    }
    if feedback is not None:
        inputs["previous_attempt_failed"] = feedback
    try:
        batch = await ctx.llm.ask(
            ctx.prompt(GEN),
            inputs,
            KnowledgeBatch,
            ctx.model(GEN),
            generator=GEN,
            est_output_tokens=250 + 140 * count,
        )
    except BadOutput as e:
        out.rejections.append(
            Rejection(generator=GEN, subtype=evolution.value, stage="llm", reason=str(e))
        )
        return None
    seen: list[str] = []
    for item in batch.cases[:count]:
        key_facts = [[f.strip() for f in g if f.strip()] for g in item.key_facts]
        key_facts = [g for g in key_facts if g]
        cand = make_candidate(
            generator=GEN,
            subtype=evolution.value,
            evolution=evolution,
            question=item.question,
            category="knowledge",
            expected_route=Route.KNOWLEDGE,
            slot=slot,
            should_escalate=False,
            reference_answer=item.reference_answer.strip(),
            gold_context_ids=[c.id for c in chunks],
            must_include_any=key_facts,
            tags=[
                f"src-{c.source.removesuffix('.md').replace('_', '-')}" for c in chunks if c.source
            ],
        )
        problems = check_grounding(item.reference_answer, key_facts, [c.text for c in chunks])
        if not key_facts:
            problems.append("no key facts")
        if evolution is Evolution.MULTIHOP:
            problems += [
                f"multi-hop answer uses no key fact from {c.id}"
                for c in chunks
                if not any(normalize(f) in normalize(c.text) for g in key_facts for f in g)
            ]
        if problems:
            out.rejections.append(cand.reject("grounding", "; ".join(problems)))
            seen += problems
        else:
            out.candidates.append(cand)
    return seen


async def _unanswerable(ctx: GenContext, out: GenOutput) -> None:
    kb = ctx.sources.kb_text
    topics = []
    for topic, words in UNANSWERABLE_TOPICS:
        if any(w in kb for w in words):
            out.rejections.append(
                Rejection(
                    generator=GEN,
                    subtype="unanswerable",
                    stage="precondition",
                    reason=f"topic {topic!r} is in the knowledge base",
                )
            )
        else:
            topics.append(topic)
    out.quotas["unanswerable"] = UNANSWERABLE_N
    try:
        batch = await ctx.llm.ask(
            ctx.prompt("knowledge_unanswerable"),
            {"topics": topics, "language": "english"},
            TopicBatch,
            ctx.model(GEN),
            generator=GEN,
            est_output_tokens=200 + 50 * len(topics),
        )
    except BadOutput as e:
        out.rejections.append(
            Rejection(generator=GEN, subtype="unanswerable", stage="llm", reason=str(e))
        )
        return
    for item in batch.questions:
        out.candidates.append(
            make_candidate(
                generator=GEN,
                subtype="unanswerable",
                evolution=Evolution.TRAP,
                question=item.question,
                category="knowledge",
                expected_route=Route.KNOWLEDGE,
                slot="unanswerable",
                reference_answer=UNANSWERABLE_REFERENCE,
                tags=["unanswerable"],
            )
        )
