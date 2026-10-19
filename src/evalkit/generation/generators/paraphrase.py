"""Code-built cases whose question the LLM only rewords (database, controls, fake prices).

Each ``Template`` carries its finished labels; the LLM gets the template message and the
strings it must keep verbatim (product name, order id, ...). A variant missing one of them
is rejected. The template text itself is the last candidate (tagged
``paraphrase_fallback``), so a slot is still filled when every variant fails.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import Field

from evalkit.generation.base import Evolution, GenContext, GenOutput, _Out, language_of
from evalkit.generation.filters import Candidate, Rejection
from evalkit.generation.llm import BadOutput

VARIANTS: Final = 2
ITEMS_PER_CALL: Final = 8
FALLBACK_TAG: Final = "paraphrase_fallback"


@dataclass(frozen=True)
class Template:
    subtype: str
    slot: str  # unique per template; quota 1
    evolution: Evolution
    text: str
    must_keep: tuple[str, ...]
    build: Callable[[str, bool], Candidate]  # (question, is_fallback) -> candidate


class ParaphraseItem(_Out):
    index: int
    variants: list[str] = Field(default_factory=list)


class ParaphraseBatch(_Out):
    paraphrases: list[ParaphraseItem]


def missing_keep(text: str, must_keep: Sequence[str]) -> list[str]:
    return [k for k in must_keep if k.casefold() not in text.casefold()]


async def paraphrase(
    ctx: GenContext, generator: str, templates: Sequence[Template], out: GenOutput
) -> None:
    """Add VARIANTS reworded candidates per template, then the template as a fallback."""
    for start in range(0, len(templates), ITEMS_PER_CALL):
        group = templates[start : start + ITEMS_PER_CALL]
        inputs = {
            "variants": VARIANTS,
            "items": [
                {
                    "index": i,
                    "message": t.text,
                    "must_keep": list(t.must_keep),
                    "language": language_of(t.evolution).value,
                    "style": t.evolution.value,
                }
                for i, t in enumerate(group)
            ],
        }
        try:
            batch = await ctx.llm.ask(
                ctx.prompt("paraphrase"),
                inputs,
                ParaphraseBatch,
                ctx.model(generator),
                generator=generator,
                est_output_tokens=150 + 60 * VARIANTS * len(group),
            )
            by_index = {p.index: p.variants for p in batch.paraphrases}
        except BadOutput as e:
            out.rejections.append(
                Rejection(generator=generator, subtype="paraphrase", stage="llm", reason=str(e))
            )
            by_index = {}
        for i, t in enumerate(group):
            out.quotas[t.slot] = 1
            for variant in by_index.get(i, [])[:VARIANTS]:
                cand = t.build(variant, False)
                lost = missing_keep(variant, t.must_keep)
                if lost:
                    out.rejections.append(cand.reject("paraphrase", f"lost {lost!r}"))
                else:
                    out.candidates.append(cand)
            out.candidates.append(t.build(t.text, True))
