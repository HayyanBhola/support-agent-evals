"""
agent/memory.py

Short-term conversation memory (Phase 8): lets a message use what was said
just before it in the same chat.

WHY PLAIN RULES, NOT "SEND THE WHOLE CHAT TO THE LLM":
Memory changes WHICH facts get looked up (which product, which question).
That decision goes through the router and database, never the LLM, so the
same guarantees as Phase 5 hold: prices, stock and ingredients still come
only from the database. Each rule below is small, readable and tested in
tests/test_memory.py.

The three situations it handles:

1. ANSWERING A CLARIFYING QUESTION
     Customer: How much does it cost?
     Assistant: Which product do you mean? ...
     Customer: Hydra Balance Moisturizer          -> price of Hydra Balance

2. "IT" / "THIS" / "THAT" REFERRING TO A PRODUCT MENTIONED EARLIER
     Customer: Tell me about the Niacinamide Serum
     Customer: How much is it?                    -> price of the serum
     Customer: Is it in stock?                    -> stock of the serum

3. "AND THE ...?" / "WHAT ABOUT ...?" FOLLOW-UPS
     Customer: How much is the serum?
     Customer: And the sunscreen?                 -> price of the sunscreen

Anything else is answered on its own, exactly as before Phase 8.
Memory never overrides escalation or greetings: "I got a rash" is always
an escalation, whatever came before it. And it never turns a POLICY
question into a product lookup: after "how much is the serum?", the
message "and the sunscreen, can I return it?" is a return-policy question.
"""

import re

from agent.router import Route, PRODUCT_ALIASES, POLICY_TOPIC_PATTERNS

# How many earlier assistant replies to search for "the product we were
# talking about". Kept small on purpose: after a few turns the customer has
# probably moved on, and guessing wrong is worse than asking again.
PRODUCT_LOOKBACK_TURNS = 3

PRONOUN_PATTERN = r"\b(it|its|it's|this|that|these|those|them|they|one)\b"
FOLLOW_UP_PATTERN = r"^\s*(and|what about|how about|also|same for|and what about)\b"
# Words that can surround a bare product name without changing its meaning,
# e.g. "the Hydra Balance Moisturizer please" or "for the serum".
FILLER_WORDS = {"the", "a", "an", "for", "of", "please", "pls", "one", "my", "i", "mean",
                "want", "about", "that", "this", "is", "it", "its", "ok", "okay",
                "and", "what", "how", "also", "same"}

PRODUCT_INTENTS = ("price", "stock", "ingredients")


def turn_info(row: dict):
    """Pulls the routing summary out of a logged ASSISTANT message.
    Returns {"route", "intents", "product_ids", "order_id"} or None."""
    if not row or row.get("sender") != "assistant":
        return None
    details = row.get("details") or {}
    return details.get("route_info")


def _assistant_turns(recent_turns: list) -> list:
    """Route summaries of the earlier assistant replies, most recent first."""
    infos = [turn_info(r) for r in recent_turns]
    return [i for i in reversed(infos) if i]


def _topic_changed(info: dict) -> bool:
    """True if this earlier reply was about something OTHER than a product:
    an order, a policy/document question, or a hand-off to a human. After
    one of those, "it" can't safely be taken to mean an older product.
    Greetings and "which product?" questions don't change the topic."""
    if info.get("route") in ("greeting", "guardrail", "medical"):
        return False
    if info.get("route") == "clarify":
        return "order_status" in info.get("intents", [])
    return True  # database-order, knowledge, escalate (product turns are handled before this)


def _is_bare_product_reply(message: str, products: list) -> bool:
    """True if the message is basically just a product name, like
    "Hydra Balance Moisturizer" or "the serum please"."""
    text = message.lower()
    for p in products:
        for name in [p["name"].lower()] + PRODUCT_ALIASES.get(p["product_id"], []):
            text = re.sub(r"\b" + re.escape(name) + r"\b", " ", text)
    leftover = [w for w in re.findall(r"[a-z']+", text) if w not in FILLER_WORDS]
    return not leftover


def apply_memory(route: Route, message: str, products: list, recent_turns: list):
    """Returns (route, memory_note). memory_note is a plain-English
    explanation for the logs when memory changed the route, else None."""
    if not recent_turns or route.route in ("escalate", "greeting", "guardrail", "medical"):
        return route, None
    # Shipping / returns / refunds / cancelling are policy questions, answered
    # from the documents, never a repeat of an earlier price or stock lookup.
    if any(re.search(p, message, flags=re.IGNORECASE) for p in POLICY_TOPIC_PATTERNS):
        return route, None

    earlier = _assistant_turns(recent_turns)
    if not earlier:
        return route, None
    last = earlier[0]
    last_product_intents = [i for i in last.get("intents", []) if i in PRODUCT_INTENTS]

    # Rule 1: the customer is answering "Which product do you mean?"
    if (last.get("route") == "clarify" and last_product_intents
            and route.product_ids and not [i for i in route.intents if i in PRODUCT_INTENTS]):
        return (Route("database", f"{'/'.join(last_product_intents)} question about a known product",
                      intents=last_product_intents, product_ids=route.product_ids),
                "answered the earlier clarifying question: used its "
                f"{'/'.join(last_product_intents)} question with the product named now")

    # Rule 2: price/stock/ingredients question with no product, but "it"/"this"
    # (or nothing at all, like "how much?") pointing at a recent product.
    asked = [i for i in route.intents if i in PRODUCT_INTENTS]
    points_back = re.search(PRONOUN_PATTERN, message, flags=re.IGNORECASE) or route.route == "clarify"
    if asked and not route.product_ids and points_back:
        for info in earlier[:PRODUCT_LOOKBACK_TURNS]:
            if info.get("product_ids"):
                return (Route("database", f"{'/'.join(asked)} question about a known product",
                              intents=asked, product_ids=info["product_ids"]),
                        f"used the product from earlier in the chat ({', '.join(info['product_ids'])})")
            if _topic_changed(info):
                # e.g. cleanser -> "where is my order?" -> "how much is it?":
                # "it" no longer clearly means the cleanser, so don't guess.
                break

    # Rule 3: "And the sunscreen?" after a product question -> same question,
    # new product.
    if (route.product_ids and not asked and last.get("route") == "database"
            and last_product_intents
            and (re.search(FOLLOW_UP_PATTERN, message, flags=re.IGNORECASE)
                 or _is_bare_product_reply(message, products))):
        return (Route("database", f"{'/'.join(last_product_intents)} question about a known product",
                      intents=last_product_intents, product_ids=route.product_ids),
                f"follow-up: repeated the previous {'/'.join(last_product_intents)} "
                "question for the product named now")

    return route, None


def rows_for_turn(message: str, result: dict) -> list:
    """The two message rows (customer + assistant) that one exchange is
    logged as. The SAME shape is used for the database log and for
    in-memory history (tests), so memory reads both the same way."""
    assistant_details = {
        "route_info": {
            "route": result["route"],
            "intents": result.get("intents", []),
            "product_ids": result.get("product_ids", []),
            "order_id": result.get("order_id"),
        },
        "reason": result.get("reason"),
        "memory": result.get("memory"),
        "escalation_reason": result.get("escalation_reason"),
        # DB rows used, or document chunks + AI answer + checker result
        "audit": result.get("details"),
    }
    kb = result.get("details") if result.get("source") == "knowledge_base" else None
    return [
        {"sender": "customer", "content": message, "intent": None, "source": None,
         "answer_type": None, "details": None, "escalation_id": None},
        {"sender": "assistant", "content": result["answer"], "intent": result["route"],
         "source": result.get("source"),
         "answer_type": (kb or {}).get("answer_type") if kb else None,
         "details": assistant_details, "escalation_id": result.get("escalation_id")},
    ]
