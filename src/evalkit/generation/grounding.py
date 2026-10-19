"""Automatic grounding check for generated knowledge cases.

A reference answer passes only if everything checkable in it comes from its source chunk(s):

* every number (digits, or a number word such as "five") appears in the chunk text;
* every policy term it uses (from POLICY_TERMS: "business day", "store credit", "final
  sale", ...) appears in the chunk text, in any listed form;
* every key fact (each option of each must_include_any group) appears verbatim in the chunk
  text, and the reference contains at least one option of every group.

Matching is case-insensitive with whitespace collapsed. The check is deliberately strict: a
true answer phrased with a term from a different chunk is rejected, and the rejection is
logged rather than the term guessed.
"""

import re
from collections.abc import Iterable, Sequence
from typing import Final

NUMBER_WORDS: Final = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "fourteen": "14",
    "fifteen": "15",
    "twenty": "20",
    "thirty": "30",
    "forty": "40",
    "fifty": "50",
    "sixty": "60",
    "ninety": "90",
    "hundred": "100",
}

# Each entry is one policy idea in all the forms we accept. If the reference uses any form,
# the chunk must contain one of them. Terms that appear in no chunk at all (store credit,
# restocking fee, warranty, ...) are here so an invented policy is caught.
POLICY_TERMS: Final[tuple[tuple[str, ...], ...]] = (
    ("business day", "business days"),
    ("hour", "hours", "2-hour"),
    ("week", "weeks"),
    ("month", "months"),
    ("year", "years"),
    ("full refund",),
    ("partial refund",),
    ("store credit",),
    ("cash refund", "cash refunds", "cash"),
    ("exchange", "exchanges", "exchanged"),
    ("restocking fee", "restocking"),
    ("warranty", "guarantee", "guaranteed"),
    ("gift card", "gift cards", "gift wrap", "gift wrapping"),
    ("loyalty", "reward points", "points"),
    ("discount", "discounts", "coupon", "promo code", "promo"),
    ("free shipping", "free on orders"),
    ("standard shipping",),
    ("express shipping", "express"),
    ("international", "internationally", "overseas", "abroad"),
    ("tracking number", "tracking"),
    ("proof of purchase", "receipt"),
    ("original packaging", "packaging"),
    ("original payment method",),
    ("final sale", "clearance", "sale"),
    ("unopened",),
    ("opened", "used"),
    ("damaged", "defective"),
    ("adverse reaction", "reaction"),
    ("wrong item",),
    ("return authorization",),
    ("inspected", "inspection"),
    ("free of charge",),
    ("non-refundable",),
    ("shipping fee", "shipping fees"),
    ("weekend", "weekends"),
    ("public holiday", "public holidays", "holiday", "holidays"),
    ("courier", "courier issues"),
    ("vegan",),
    ("animal", "animals", "animal-derived"),
    ("fragrance", "fragrance-free"),
    ("sample", "samples", "sample sizes"),
    ("dermatologist", "doctor"),
    ("self-service",),
    ("payment confirmation",),
    ("bank", "card issuer"),
    ("cancel", "cancelled", "canceled", "cancellation", "cancelling"),
    ("human support team", "human support", "human"),
    ("email", "e-mail"),
    ("prepaid label", "return label", "shipping label"),
    ("cruelty-free",),
    ("sulfate-free",),
    ("oil-free",),
    ("mineral",),
    ("broad-spectrum",),
    ("every other day",),
)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def numbers_in(text: str) -> set[str]:
    """Numbers as normalized strings: "5-7 business days" -> {"5", "7"}; "$4.99" -> {"4.99"};
    "five" -> {"5"}; "10.0" -> {"10"}."""
    found = set()
    for raw in re.findall(r"\d+(?:\.\d+)?", text):
        found.add(raw.rstrip("0").rstrip(".") if "." in raw else raw.lstrip("0") or "0")
    for word in re.findall(r"[a-z]+", text.lower()):
        if word in NUMBER_WORDS:
            found.add(NUMBER_WORDS[word])
    return found


def _has(text: str, phrase: str) -> bool:
    return re.search(r"(?<![\w-])" + re.escape(phrase) + r"(?![\w-])", text) is not None


def terms_in(text: str) -> list[tuple[str, ...]]:
    """The POLICY_TERMS entries that ``text`` uses (in any form)."""
    t = normalize(text)
    return [forms for forms in POLICY_TERMS if any(_has(t, f) for f in forms)]


def check_grounding(
    reference: str, key_facts: Sequence[Sequence[str]], chunk_texts: Iterable[str]
) -> list[str]:
    """Problems that make ``reference`` ungrounded in ``chunk_texts``; empty = grounded."""
    source = normalize(" ".join(chunk_texts))
    ref = normalize(reference)
    problems = []
    missing_numbers = sorted(numbers_in(ref) - numbers_in(source))
    if missing_numbers:
        problems.append(f"numbers not in the chunk: {', '.join(missing_numbers)}")
    for forms in terms_in(ref):
        if not any(_has(source, f) for f in forms):
            problems.append(f"policy term not in the chunk: {forms[0]!r}")
    for group in key_facts:
        for option in group:
            if normalize(option) not in source:
                problems.append(f"key fact not in the chunk: {option!r}")
        if not any(normalize(option) in ref for option in group):
            problems.append(f"reference states none of the key facts {list(group)!r}")
    return problems
