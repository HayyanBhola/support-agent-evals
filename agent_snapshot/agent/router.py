"""
agent/router.py

Decides WHERE the answer to a customer message must come from, before any
LLM is involved (Section 5 of the brief: RAG vs. database vs. clarify vs.
escalate).

WHY RULES AND NOT THE LLM: in Phase 4 a 1.5B model mixed up topics and
invented ingredients. Routing is the most important decision in the
pipeline (a price question answered from documents = a guessed price), so
it is done with plain, readable rules that give the same result every
time and can be tested one by one in tests/eval_cases.json.
When no rule matches, the message goes to the knowledge base, which is
the safe default: the worst case there is an honest "I don't have that".

Routes:
    "escalate"  -> customer wants a human, or reports a safety problem
    "guardrail" -> tries to override the rules / role-play: fixed reply
    "medical"   -> asks if a product treats/cures something, or for a diagnosis: fixed reply
    "greeting"  -> just hello / thanks
    "clarify"   -> we know WHAT they want but not WHICH product / order
    "database"  -> price, stock, ingredients of a known product, or an
                   order status with a real order ID
    "knowledge" -> everything else: policies, FAQ, product descriptions
"""

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# --- Product recognition ------------------------------------------------
# Extra names customers use for each product, keyed by product_id so a
# renamed product in the database doesn't break anything. The full product
# name from the database is always recognised as well.
PRODUCT_ALIASES = {
    "GC-P001": ["hydra balance", "hydra", "moisturizer", "moisturiser"],
    "GC-P002": ["barrier repair", "barrier cream", "repair cream"],
    "GC-P003": ["niacinamide serum", "serum"],
    "GC-P004": ["gentle daily cleanser", "gentle cleanser", "cleanser", "face wash"],
    "GC-P005": ["sunscreen", "spf 50", "spf", "sun cream", "sunblock"],
}

# --- Intent keywords ----------------------------------------------------
HUMAN_PATTERNS = [
    r"\bhuman\b", r"\breal person\b", r"\brepresentative\b", r"\blive agent\b",
    r"\b(talk|speak|chat) (to|with) (someone|somebody|a person|a team member|an agent|support staff)\b",
    r"\bcall me\b", r"\bmanager\b",
    # Roman Urdu: "insaan se baat karni hai", "kisi banday se baat karao"
    r"\b(insaan|insan|banday|bande|admi|aadmi|agent) se baat\b",
]
# A skin reaction is a safety concern (SYSTEM_RULES rule 9): a human
# handles it, never the bot.
SAFETY_PATTERNS = [
    r"\brash\b", r"\bburn(ing|s|ed)?\b", r"\ballergic\b", r"\ballergy\b",
    r"\bswell(ing|ed)?\b", r"\bhives\b", r"\bitch(ing|y)?\b", r"\bblister",
    r"\bbroke out\b", r"\bbreak(ing)? out\b", r"\bhospital\b", r"\bdoctor\b",
    # Roman Urdu: kharish (itching), jalan (burning), sojan (swelling), daane (bumps)
    r"\bkharish\b", r"\bjalan\b", r"\bsojan\b", r"\bdaan[ea]\b", r"\ballergy ho\b",
]
COMPLAINT_PATTERNS = [
    r"\bcomplain", r"\bterrible\b", r"\bawful\b", r"\bscam\b", r"\bunacceptable\b",
    r"\bworst\b", r"\bangry\b", r"\bfurious\b",
    r"\bbakwas\b", r"\bghatiya\b",  # Roman Urdu
]
GREETING_PATTERN = (
    r"^(hi|hello|hey|salam|assalam ?o ?alaikum|aoa|good (morning|afternoon|evening)|"
    r"thanks|thank you|thx|ok|okay|bye|shukriya|shukria|jazakallah|meherbani)[\s!.,?]*$"
)
PRICE_PATTERNS = [r"\bprice", r"\bcost", r"\bhow much\b", r"\$", r"\bexpensive\b", r"\bcheap",
                  # Roman Urdu: "kitne ka hai", "kitna hai", "qeemat"
                  r"\bkitn[aei]\b", r"\bqeemat\b", r"\bqimat\b"]
STOCK_PATTERNS = [
    r"\bin stock\b", r"\bout of stock\b", r"\bsold out\b", r"\brestock",
    r"\bstock\b", r"\bavailab",  # "available", "availability"
]
INGREDIENT_PATTERNS = [
    r"\bingredient", r"\bcontain", r"\bmade (of|with|from)\b",
    r"\bwhat('?s| is)? in\b", r"\bformula", r"\binci\b",  # "what's in" / "whats in"
    r"\b(has|have|got)\b.*\bin it\b", r"\bdoes it have\b",  # "has retinol in it?"
]
# Words that mean the question is about a POLICY, not a product. "How much
# is shipping?" contains "how much" but is a shipping-policy question.
POLICY_TOPIC_PATTERNS = [
    r"\bship", r"\bdeliver", r"\bexpress\b", r"\breturn", r"\brefund",
    r"\bcancel", r"\bexchange\b",
]
ORDER_ID_PATTERN = r"\bGC-?\d{5}\b"
# Attempts to change how the assistant works or make it play another role.
# These get a fixed reply: the assistant never follows instructions that
# arrive inside a customer message (SYSTEM_RULES stay in force).
MANIPULATION_PATTERNS = [
    r"\bignore (all |any |your |the )?(previous |prior |above )?(instructions|rules|prompt)",
    r"\b(system|hidden|initial) prompt\b", r"\bdeveloper mode\b", r"\bjailbreak",
    r"\b(reveal|show|print|repeat) (me )?(your|the) (rules|instructions|prompt)",
    r"\bpretend (you are|to be|you're)\b", r"\bact as (a|an)\b", r"\byou are now\b",
    r"\bfrom now on you\b", r"\bprescribe\b",
]
# Questions asking whether a product treats/cures a condition, or asking
# for a diagnosis. SYSTEM_RULES rule 6: never make a medical claim. The AI
# once answered "Will the sunscreen cure my acne?" with "Yes." (copied from
# an unrelated FAQ answer), so these never reach the AI: fixed reply.
MEDICAL_QUESTION_PATTERNS = [
    r"\b(cure|cures|curing|treat|treats|treating|heal|heals|healing|fix|fixes|get rid of|clear up|clears up)\b"
    r".*\b(acne|eczema|psoriasis|rosacea|dermatitis|melasma|pimples?|breakouts?|scars?|"
    r"infection|disease|condition|wrinkles|hyperpigmentation|spots)\b",
    r"\bdiagnos", r"\bwhich disease\b", r"\bwhat (disease|condition) (is|do i have)\b",
    r"\bis it (safe|ok|okay) (during|while|in) pregnan", r"\bmedicat(ion|e)\b",
]
MEDICAL_REPLY = ("I can't give medical advice or say that a product treats or cures a skin "
                 "condition. I can tell you what's in a product and what its ingredients "
                 "generally do. For a skin condition, a dermatologist or doctor is the right "
                 "person to ask.")
GUARDRAIL_REPLY = ("I can only help with GlowCare's products, prices, stock, your orders, and our "
                   "shipping, return and refund policies, and I can't change how I work or give "
                   "medical advice. What can I help you with?")
# Questions ABOUT how something works ("how do I get a tracking number?",
# "when will my order be processed?", "how quickly does a human reply?").
# They mention orders or humans but are answered from the policy documents:
# they are not a request to look up an order or to hand over to a person.
HOW_IT_WORKS_PATTERNS = [
    r"\bhow (do|can|would|should) (i|we|you)\b", r"\bhow (long|quickly|fast|soon)\b",
    r"\bbe processed\b", r"\bprocessing time\b", r"\btracking number\b",
    r"\bwhat (is|'s) the (process|policy)\b",
]
ORDER_STATUS_PATTERNS = [
    r"\bwhere('s| is) my (order|package|parcel)\b", r"\btrack", r"\border status\b",
    r"\bstatus of my order\b", r"\bmy order\b.*\b(shipped|arrive|deliver|status)",
    r"\bhas my order\b", r"\bwhen will my order\b",
    r"\bmera (order|parcel)\b", r"\border kahan\b", r"\bparcel kahan\b",  # Roman Urdu
]


@dataclass
class Route:
    route: str                      # escalate | greeting | clarify | database | knowledge
    reason: str                     # plain-English "why", printed in the logs
    intents: list = field(default_factory=list)      # price / stock / ingredients / order_status
    product_ids: list = field(default_factory=list)  # e.g. ["GC-P001"]
    order_id: str = None            # e.g. "GC10241"
    clarify_question: str = None


def _any(patterns, text):
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


# How similar a misspelled word must be to a product name to count
# ("sunscren" vs "sunscreen" = 0.94). High on purpose: a wrong product is
# worse than asking "which product?".
FUZZY_MIN_SIMILARITY = 0.85


def _fuzzy_products(text: str, products: list) -> list:
    """Typo-tolerant fallback, used only when no product name matched
    exactly: compares each 1-3 word piece of the message with every product
    name and alias, e.g. "gentle cleansr" -> Gentle Daily Cleanser,
    "niacinamid serum" -> Niacinamide Serum. Names shorter than 5 letters
    ("spf") are never fuzzy-matched."""
    words = re.findall(r"[a-z0-9']+", text.lower())
    pieces = {" ".join(words[i:i + n]) for n in (1, 2, 3) for i in range(len(words) - n + 1)}
    best = {}
    for p in products:
        names = [p["name"].lower()] + PRODUCT_ALIASES.get(p["product_id"], [])
        for name in names:
            if len(name) < 5:
                continue
            for piece in pieces:
                if abs(len(piece) - len(name)) > 3:
                    continue
                score = SequenceMatcher(None, piece, name).ratio()
                if score >= FUZZY_MIN_SIMILARITY and score > best.get(p["product_id"], 0):
                    best[p["product_id"]] = score
    return sorted(best, key=lambda pid: -best[pid])


def find_products(text: str, products: list, fuzzy: bool = False) -> list:
    """Returns the product_ids mentioned in `text`, in order of first mention.
    `products` is db.get_all_products(): [{"product_id", "name", ...}, ...].
    Longer names are matched first, so "niacinamide serum" wins over the
    ingredient word "niacinamide" on its own (which is NOT a product).
    fuzzy=True (the router) also accepts small typos when nothing matched
    exactly; the answer checker keeps it off so it never over-reads."""
    candidates = []
    for p in products:
        names = [p["name"].lower()] + PRODUCT_ALIASES.get(p["product_id"], [])
        for n in names:
            candidates.append((n, p["product_id"]))
    candidates.sort(key=lambda c: len(c[0]), reverse=True)

    lowered = text.lower()
    found = []  # (position, product_id)
    for name, pid in candidates:
        m = re.search(r"\b" + re.escape(name) + r"\b", lowered)
        if m and pid not in [f[1] for f in found]:
            found.append((m.start(), pid))
            # blank out the match so a shorter alias can't match inside it
            lowered = lowered[:m.start()] + " " * len(name) + lowered[m.end():]
    if not found and fuzzy:
        return _fuzzy_products(text, products)
    return [pid for _, pid in sorted(found)]


def route_message(message: str, products: list) -> Route:
    text = message.strip()

    # 1. Escalation always wins: a person asking for a human, or a possible
    #    safety issue, must never be handled by the bot alone.
    #    Safety is checked FIRST: "I want a human, I got a rash" must be
    #    treated as a high-priority safety case, not a normal human request.
    if _any(SAFETY_PATTERNS, text):
        return Route("escalate", "possible skin reaction / safety concern")
    # "How quickly does a human reply?" is a question about support, not a
    # request for one (but "how can I contact a human?" still escalates:
    # the customer is reaching for a person, so give them one).
    asks_about_process = (_any(HOW_IT_WORKS_PATTERNS, text)
                          and not re.search(r"\b(contact|reach|talk|speak)\b", text, re.IGNORECASE))
    if _any(HUMAN_PATTERNS, text) and not asks_about_process:
        return Route("escalate", "customer asked for a human")
    if _any(COMPLAINT_PATTERNS, text):
        return Route("escalate", "complaint")

    # 1b. Attempts to override the rules or make the assistant role-play
    #     get a fixed reply. (Checked AFTER escalation, so "ignore your rules,
    #     I have a rash" still reaches a human.)
    if _any(MANIPULATION_PATTERNS, text):
        return Route("guardrail", "attempt to change the assistant's rules or role")

    # 1c. Medical questions (treat / cure / diagnose): fixed, safe reply.
    if _any(MEDICAL_QUESTION_PATTERNS, text):
        return Route("medical", "asks for a medical claim or diagnosis (SYSTEM_RULES rule 6)")

    # 2. Small talk.
    if re.match(GREETING_PATTERN, text, flags=re.IGNORECASE):
        return Route("greeting", "greeting / small talk")

    # 3. Orders: only the database knows order status.
    order_match = re.search(ORDER_ID_PATTERN, text, flags=re.IGNORECASE)
    if order_match:
        order_id = order_match.group(0).upper().replace("-", "")
        return Route("database", "order ID found", intents=["order_status"], order_id=order_id)
    if _any(ORDER_STATUS_PATTERNS, text) and not _any(HOW_IT_WORKS_PATTERNS, text):
        return Route("clarify", "order status asked without an order ID",
                     intents=["order_status"],
                     clarify_question="Could you share your order number? "
                                      "It looks like GC10241 and is in your confirmation email.")

    # 4. Product facts: price / stock / ingredients come only from the database.
    product_ids = find_products(text, products)
    intents = []
    is_policy_question = _any(POLICY_TOPIC_PATTERNS, text)
    if _any(PRICE_PATTERNS, text) and not is_policy_question:
        intents.append("price")
    # "available" alone is a stock word only when the question isn't about a
    # policy: "is the serum available for international shipping?" is a
    # shipping question. Clear stock words ("in stock", "sold out") always count.
    strong_stock_patterns = [p for p in STOCK_PATTERNS if p != r"\bavailab"]
    if _any(strong_stock_patterns, text) or (_any(STOCK_PATTERNS, text) and not is_policy_question):
        intents.append("stock")
    if _any(INGREDIENT_PATTERNS, text):
        intents.append("ingredients")
    # Typo-tolerant product matching only for price/stock/ingredient
    # questions ("sunscren in stock?"), so ordinary words like "cleanse" in
    # "how do I cleanse my face?" are never mistaken for a product.
    if intents and not product_ids:
        product_ids = find_products(text, products, fuzzy=True)

    if intents and product_ids:
        return Route("database", f"{'/'.join(intents)} question about a known product",
                     intents=intents, product_ids=product_ids)

    # A price or stock question with no product can't be answered honestly
    # and shouldn't be sent to the documents (they have no prices), so ask.
    # ("available" alone is left out: "is express shipping available?" is a
    # policy question.)
    strong_stock = _any([p for p in STOCK_PATTERNS if p != r"\bavailab"], text)
    if "price" in intents or strong_stock:
        return Route("clarify", "price/stock question without a product",
                     intents=intents,
                     clarify_question="Which product do you mean? We have: "
                                      + ", ".join(p["name"] for p in products) + ".")

    # 5. Everything else: policies, FAQ, product descriptions, ingredient
    #    explanations ("what does niacinamide do?").
    return Route("knowledge", "general question -> knowledge base",
                 intents=intents, product_ids=product_ids)
