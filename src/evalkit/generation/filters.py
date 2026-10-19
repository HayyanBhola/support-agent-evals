"""Automatic quality filters, applied to every candidate before anything is saved.

In order (the first failure is the logged reason):

1. ``validation``: the candidate is a valid TestCase.
2. ``length``: the question has 3-60 words.
3. ``answer_in_question``: the question would already pass its own content checks
   (every must_include and must_include_any group), or states a gold value (price, stock).
4. ``duplicate``: exact duplicate of a question kept earlier by the same generator or of a
   question in an existing dataset, or near-duplicate (difflib ratio >= 0.9 after
   normalizing) of a kept question with the same gold_db_facts. Code-built cases that
   differ only in the entity ("order GC10241" vs "order GC10242") test different facts, so
   they are not near-duplicates; LLM-written questions have no gold facts and are all
   compared with each other.
5. ``language``: a Roman Urdu case really is Roman Urdu (Latin script, enough marker
   words). English cases are not language-checked.

Candidates that pass everything but arrive after their slot is full are rejected as
``surplus``. Rejected candidates are returned as ``Rejection`` records with the reason.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from evalkit.hashing import canonical_json
from evalkit.schemas import TestCase

MIN_WORDS: Final = 3
MAX_WORDS: Final = 60
NEAR_DUPLICATE_RATIO: Final = 0.9

# Common Roman Urdu function words and greetings. A Roman Urdu question needs at least one
# (two once it has 6+ words), at least one of them unambiguous.
ROMAN_URDU_MARKERS: Final = frozenset(
    {
        "aap", "abhi", "andar", "ap", "aoa", "assalam", "assalamualaikum", "bata", "batao",
        "bataein", "batayen", "bhai", "chahiye", "chahta", "chahti", "dein", "do", "gaya",
        "gayi", "hai", "hain", "haal", "hay", "ho", "hua", "hui", "jee", "ji", "ka", "kab",
        "kabhi", "kahan", "kaise", "kar", "karein", "karna", "karo", "kaun", "kaunsa",
        "kaunse", "kaunsi", "ke", "keemat", "kese", "ki", "kia", "kitna", "kitne", "kitni",
        "ko", "konsa", "konse", "kya", "kyun", "liye", "main", "maujood", "mein", "mera",
        "meri", "mere", "milega", "milegi", "mojood", "mujhe", "nahi", "nahin", "par", "pe",
        "qeemat", "raha", "rahi", "sakta", "sakte", "sakti", "salam", "se", "shukriya",
        "shukria", "tha", "thi", "theek", "tou", "wala", "wali", "walaikum", "wapas", "yaar",
        "yeh", "ye", "woh",
    }
)  # fmt: skip
# "do", "main", "par", "ho" are also English words; they only count with another marker.
_AMBIGUOUS_MARKERS: Final = frozenset({"do", "main", "par", "ho", "pe", "ye"})


class Language(StrEnum):
    ENGLISH = "english"
    ROMAN_URDU = "roman_urdu"


class Rejection(BaseModel):
    """One discarded candidate and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generator: str
    subtype: str
    stage: str
    reason: str
    question: str | None = None


@dataclass(frozen=True)
class Candidate:
    """A generated case before filtering. ``data`` becomes a TestCase if it passes."""

    generator: str
    subtype: str
    slot: str  # cases are kept per slot up to its quota, in generation order
    language: Language
    data: dict[str, Any]

    @property
    def question(self) -> str:
        return str(self.data.get("question", ""))

    def reject(self, stage: str, reason: str) -> Rejection:
        return Rejection(
            generator=self.generator,
            subtype=self.subtype,
            stage=stage,
            reason=reason,
            question=self.question or None,
        )


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def normalize_question(text: str) -> str:
    return " ".join(words(text))


def detect_language(text: str) -> Language:
    w = words(text)
    markers = [x for x in w if x in ROMAN_URDU_MARKERS]
    strong = [x for x in markers if x not in _AMBIGUOUS_MARKERS]
    needed = 2 if len(w) >= 6 else 1
    if strong and len(markers) >= needed:
        return Language.ROMAN_URDU
    return Language.ENGLISH


def language_problem(text: str, language: Language) -> str | None:
    if any(ord(ch) > 0x2FF and not 0x2000 <= ord(ch) <= 0x206F for ch in text):
        return "uses a non-Latin script"
    detected = detect_language(text)
    if detected is not language:
        return f"expected {language.value}, looks like {detected.value}"
    return None


def answer_in_question(case: TestCase) -> str | None:
    """Why the question gives its own answer away, or None."""
    q = case.question.casefold()
    checks = bool(case.must_include or case.must_include_any)
    if (
        checks
        and all(s.casefold() in q for s in case.must_include)
        and all(any(s.casefold() in q for s in group) for group in case.must_include_any)
    ):
        return "the question alone passes its must_include checks"
    for key in ("price", "stock_quantity"):
        value = case.gold_db_facts.get(key)
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            # a whole number: not part of a longer number or a word ("10", "4.2", "0b")
            and re.search(rf"(?<![\w.]){re.escape(f'{value:g}')}(?!\w|\.\d)", q)
        ):
            return f"the question states the gold {key} ({value:g})"
    return None


def first_error(e: ValidationError) -> str:
    err = e.errors()[0]
    where = ".".join(str(p) for p in err["loc"]) or "case"
    return f"{where}: {err['msg']}"


@dataclass
class Deduper:
    """Exact and near-duplicate detection within one generator.

    ``group`` is what a case asserts as gold facts: near-duplicates only count within a group.
    """

    existing: frozenset[str] = frozenset()  # normalized questions from other datasets
    seen: dict[str, list[str]] = field(default_factory=dict)

    def problem(self, question: str, group: str = "") -> str | None:
        norm = normalize_question(question)
        if norm in self.existing:
            return "exact duplicate of a question in an existing dataset"
        if any(norm in kept for kept in self.seen.values()):
            return "exact duplicate of a kept question"
        for prev in self.seen.get(group, []):
            if SequenceMatcher(None, norm, prev).ratio() >= NEAR_DUPLICATE_RATIO:
                return f"near-duplicate of a kept question: {prev!r}"
        return None

    def add(self, question: str, group: str = "") -> None:
        self.seen.setdefault(group, []).append(normalize_question(question))


@dataclass(frozen=True)
class Kept:
    case: TestCase
    candidate: Candidate


def apply_filters(
    candidates: Sequence[Candidate],
    quotas: dict[str, int],
    existing_questions: Iterable[str] = (),
) -> tuple[list[Kept], list[Rejection]]:
    """Filter ``candidates`` (in order) and keep at most ``quotas[slot]`` per slot.

    Candidates beyond a full slot are rejected with stage ``surplus``: they passed every
    filter but were not needed.
    """
    dedup = Deduper(existing=frozenset(normalize_question(q) for q in existing_questions))
    kept: list[Kept] = []
    rejected: list[Rejection] = []
    filled: dict[str, int] = {}
    for cand in candidates:
        try:
            case = TestCase.model_validate(cand.data)
        except ValidationError as e:
            rejected.append(cand.reject("validation", first_error(e)))
            continue
        n = len(words(case.question))
        if not MIN_WORDS <= n <= MAX_WORDS:
            rejected.append(cand.reject("length", f"{n} words (allowed {MIN_WORDS}-{MAX_WORDS})"))
            continue
        problem = answer_in_question(case)
        if problem:
            rejected.append(cand.reject("answer_in_question", problem))
            continue
        group = canonical_json(case.gold_db_facts)
        problem = dedup.problem(case.question, group)
        if problem:
            rejected.append(cand.reject("duplicate", problem))
            continue
        if cand.language is Language.ROMAN_URDU:
            problem = language_problem(case.question, cand.language)
            if problem:
                rejected.append(cand.reject("language", problem))
                continue
        if filled.get(cand.slot, 0) >= quotas.get(cand.slot, 0):
            rejected.append(cand.reject("surplus", f"slot {cand.slot!r} already full"))
            continue
        dedup.add(case.question, group)
        filled[cand.slot] = filled.get(cand.slot, 0) + 1
        kept.append(Kept(case, cand))
    return kept, rejected
