"""
agent/verifier.py

Checks every LLM-written answer IN CODE before a customer sees it.

WHY: prompt rules are requests, and a small model sometimes ignores them
(Phase 4: invented ingredients, fake dialogue, "try ordering from the UK").
These checks don't depend on the model behaving. If an answer fails any
check, the agent throws it away and uses a safe fallback instead: it quotes
the retrieved policy text directly, or says it doesn't know.

Each check returns a list of problems (empty list = passed), so the logs
show exactly WHY an answer was rejected.
"""

import re

from agent.router import find_products

# Phrases that mean the model invented a conversation or copied a document
# layout instead of answering.
# (An "A: ..." copied from the FAQ is fine and gets cleaned off. Any "Q:"
# means the model copied or invented a question instead of answering.)
DIALOGUE_PATTERNS = [
    r"^\s*\**(customer|agent|user|assistant|client|support)\**\s*:",
    r"\bQ:",
]

# Replies that don't answer anything ("Sure, please ask your question").
NON_ANSWER_PATTERNS = [
    r"\bask (me )?your question\b", r"\bhow (can|may) i (help|assist)\b",
    r"\bwhat would you like to know\b", r"\bgo ahead and ask\b",
]

# Common words ignored by the grounding check (they carry no facts).
STOPWORDS = set("""
about above after again also been before being below between both could does doing
during each from further have having here into itself just more most must once only
other over same should some such than that their them then there these they this
those through under until very were what when where which while will with would your
yours please thank thanks help sorry glowcare customer customers product products
""".split())

# At least this share of the answer's meaningful words must appear in the
# retrieved text. Lower = more lenient. Tune with tests/run_eval.py.
MIN_GROUNDED_SHARE = 0.6

# SYSTEM_RULES rule 6: no medical claims.
MEDICAL_PATTERNS = [
    r"\bcures?\b", r"\btreats?\b", r"\btreating\b", r"\bheals?\b",
    r"\bguarantee[sd]?\b", r"\bprescription\b", r"\bdiagnos", r"\beczema\b",
    r"\bpsoriasis\b", r"\brosacea\b", r"\bacne\b", r"\bdermatitis\b",
    # none of these conditions appear in GlowCare's documents, so an AI answer
    # that mentions one can't be backed by them: always rejected
]

# Openers small models add even when told not to. These are harmless, so
# they are removed instead of failing the whole answer.
PREAMBLE_PATTERN = re.compile(
    r"^\s*(sure|certainly|of course|absolutely|great question|hi there|hello|dear customer)"
    r"[^.!?\n]*[.!?:,]?\s*(here('s| is)[^:\n]*:\s*)?",
    flags=re.IGNORECASE,
)


def clean_answer(answer: str) -> str:
    """Removes leading "Sure! Here's the answer:" / "Hi there!" openers
    (up to 3 stacked ones, which small models sometimes produce)."""
    cleaned = answer.strip()
    for _ in range(3):
        new = PREAMBLE_PATTERN.sub("", cleaned, count=1).strip()
        new = re.sub(r"^\**A:\**\s*", "", new)  # FAQ-style "A: ..."
        if new == cleaned or not new:
            break
        cleaned = new
    return cleaned


def _numbers(text: str) -> set:
    """Stand-alone numbers in the text, e.g. '30', '4.99', '5-7' ->
    {'30','4.99','5','7'}. Numbers glued to letters, like 'B3' in
    'vitamin B3', are names rather than amounts and are skipped."""
    return set(re.findall(r"(?<![A-Za-z\d.])\d+(?:\.\d+)?(?![A-Za-z\d])", text))


def check_numbers(answer: str, context: str) -> list:
    """Every number in the answer (days, prices, percentages) must appear in
    the retrieved text. A number that isn't there was made up."""
    missing = sorted(_numbers(answer) - _numbers(context))
    return [f"number(s) not in the source text: {', '.join(missing)}"] if missing else []


def check_dialogue(answer: str) -> list:
    for p in DIALOGUE_PATTERNS:
        if re.search(p, answer, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL):
            return ["answer contains invented dialogue or Q:/A: formatting"]
    return []


def check_non_answer(answer: str) -> list:
    for p in NON_ANSWER_PATTERNS:
        if re.search(p, answer, flags=re.IGNORECASE):
            return ["answer doesn't answer the question"]
    return []


def _content_words(text: str) -> list:
    return [w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in STOPWORDS]


def grounded_share(answer: str, context: str) -> float:
    """Share (0.0-1.0) of the answer's meaningful words that also appear in
    the retrieved text, compared by their first 5 letters. 1.0 = every
    word is backed by the source. Used by check_grounding and by
    tests/tune_checker.py."""
    answer_words = _content_words(answer)
    if not answer_words:
        return 1.0
    context_stems = {w[:5] for w in _content_words(context)}
    return sum(w[:5] in context_stems for w in answer_words) / len(answer_words)


def check_grounding(answer: str, context: str) -> list:
    """Most meaningful words in the answer must come from the retrieved
    text. Words are compared by their first 5 letters, so 'returning'
    matches 'return' and 'experienced' matches 'experience'.
    Catches answers that drift into invented advice."""
    answer_words = _content_words(answer)
    if not answer_words:
        return []
    context_stems = {w[:5] for w in _content_words(context)}
    share = grounded_share(answer, context)
    if share < MIN_GROUNDED_SHARE:
        unsupported = sorted({w for w in answer_words if w[:5] not in context_stems})
        return [f"only {share:.0%} of the answer is backed by the source text "
                f"(unsupported words: {', '.join(unsupported[:8])})"]
    return []


def check_medical(answer: str) -> list:
    hits = [p for p in MEDICAL_PATTERNS if re.search(p, answer, flags=re.IGNORECASE)]
    return ["answer makes a medical claim (SYSTEM_RULES rule 6)"] if hits else []


def _ingredient_vocabulary(products: list) -> set:
    """Every ingredient word we know about, lower-case, from the database
    ingredient lists. 'Ceramide NP' also adds 'ceramide' and 'ceramides',
    so the check catches the everyday spelling too."""
    vocab = set()
    for p in products:
        for ing in p.get("ingredients") or []:
            base = re.sub(r"\s*\d+%?", "", ing.lower()).strip()  # "niacinamide 10%" -> "niacinamide"
            vocab.add(base)
            first = base.split()[0]
            if first not in {"water", "fatty", "shea", "zinc", "titanium"}:
                vocab.add(first)
                vocab.add(first + "s")
    return {v for v in vocab if len(v) > 3 and v != "water"}


def check_product_ingredients(answer: str, products: list) -> list:
    """If the answer names a product AND an ingredient, that ingredient must
    be in that product's real ingredient list in the database.
    This is the exact Phase 4 bug: "Hydra Balance contains ceramides"."""
    mentioned = find_products(answer, products)
    if len(mentioned) != 1:
        # With 0 products nothing is claimed; with 2+ we can't tell which
        # ingredient belongs to which, so the answer is checked leniently.
        return []
    product = next(p for p in products if p["product_id"] == mentioned[0])
    real = " | ".join(i.lower() for i in (product.get("ingredients") or []))

    problems = []
    for word in sorted(_ingredient_vocabulary(products)):
        if re.search(r"\b" + re.escape(word) + r"\b", answer, flags=re.IGNORECASE):
            stem = word.rstrip("s")
            if stem not in real:
                problems.append(f"claims {product['name']} contains '{word}', "
                                f"which is not in its ingredient list")
    return problems


def verify(answer: str, context: str, products: list) -> list:
    """Runs every check. Returns all problems found ([] = safe to send)."""
    if not answer or not answer.strip():
        return ["empty answer"]
    return (check_dialogue(answer)
            + check_non_answer(answer)
            + check_medical(answer)
            + check_grounding(answer, context)
            + check_numbers(answer, context)
            + check_product_ingredients(answer, products))
