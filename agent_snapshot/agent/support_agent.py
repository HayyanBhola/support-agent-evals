"""
agent/support_agent.py

THE entry point for a customer message (Phase 5). The chat UI (Phase 6)
and n8n (Phase 11) will call handle_message() and nothing else.

    customer message
          |
       router  (agent/router.py — plain rules, no LLM)
          |
    +-----------+-------------+-----------+-----------+
    |           |             |           |           |
 database   knowledge      clarify     escalate    greeting
 (exact     (RAG + LLM     (ask ONE    (hand to    (fixed
  facts,     + answer       question)   a human)    reply)
  no LLM)    checker)

WHY THE DATABASE PATH HAS NO LLM: a price, stock level, ingredient list or
order status is a single exact fact. Writing it into a fixed sentence in
code means it can't be misread, rounded or invented. The LLM is only used
where wording actually needs flexibility: answering from policy documents.
"""

from database import db
from agent.router import route_message, GUARDRAIL_REPLY, MEDICAL_REPLY
from agent.memory import apply_memory, rows_for_turn
from agent import responder

# How many earlier messages (customer + assistant) memory looks at.
MEMORY_MESSAGES = 6

# Loaded once per run (5 products; cheap). Used by the router to spot
# product names and by the answer checker to verify ingredient claims.
_products_cache = None


def get_products():
    global _products_cache
    if _products_cache is None:
        _products_cache = db.get_all_products()
    return _products_cache


# --- Fixed replies (no LLM) ---------------------------------------------

GREETING_REPLY = ("Hi! I'm the GlowCare assistant. I can help with our products, "
                  "prices, stock, your orders, and our shipping, return and refund "
                  "policies. What can I help you with?")
THANKS_REPLY = "You're welcome! Is there anything else I can help you with?"

ESCALATION_REPLIES = {
    "customer asked for a human":
        "Of course. I'm passing your conversation to a GlowCare team member, "
        "who will follow up with you shortly.",
    "possible skin reaction / safety concern":
        "I'm sorry you're dealing with this. Because it may be a reaction to a "
        "product, I'm passing your message to a GlowCare team member right away "
        "so a person can help you.",
    "complaint":
        "I'm sorry about your experience. I'm passing this to a GlowCare team "
        "member so a person can look into it and follow up with you.",
}


# How urgent each kind of hand-off is. The team works "high" first.
ESCALATION_PRIORITY = {
    "possible skin reaction / safety concern": "high",
    "customer asked for a human": "normal",
    "complaint": "normal",
    "information not available": "low",
}


def _format_ref(escalation_id: int) -> str:
    return f"ESC-{escalation_id}"


def _record_escalation(result: dict, conversation_id: int, verbose: bool):
    """Phase 7: saves the hand-off in the escalations table so a person
    actually follows up, and adds the reference number to the reply.

    - One open escalation per conversation: if one is already open, the
      customer gets the same reference instead of a duplicate record. A
      MORE urgent follow-up (e.g. a rash reported after asking for a human)
      upgrades the existing record's priority.
    - If the database write fails, the customer still gets their reply,
      just without a reference number: we never show a reference that
      wasn't actually saved."""
    reason = result["escalation_reason"]
    priority = ESCALATION_PRIORITY.get(reason, "normal")
    try:
        existing = db.get_open_escalation(conversation_id)
        if existing:
            escalation_id = existing["escalation_id"]
            if db.PRIORITY_RANK[priority] > db.PRIORITY_RANK.get(existing["priority"], 1):
                db.raise_escalation_priority(escalation_id, priority, reason, result["message"])
                result["answer"] += (f" I've marked your case {_format_ref(escalation_id)} "
                                     "as urgent for our team.")
            else:
                priority = existing["priority"]
                result["answer"] += (f" Your case is already with our team "
                                     f"(reference {_format_ref(escalation_id)}).")
            result["escalation_new"] = False
        else:
            escalation_id = db.create_escalation(conversation_id, reason, priority,
                                                 result["message"])
            result["answer"] += f" Your reference number is {_format_ref(escalation_id)}."
            result["escalation_new"] = True
        result["escalation_id"] = escalation_id
        result["escalation_priority"] = priority
        if verbose:
            state = "created" if result["escalation_new"] else "already open"
            print(f"[ESCALATION] {_format_ref(escalation_id)} {state} "
                  f"(priority {priority}, reason: {reason})")
    except Exception as e:
        result["escalation_error"] = f"{type(e).__name__}: {e}"
        # The hand-off wasn't recorded, so don't leave the customer waiting
        # for a follow-up that may never come.
        result["answer"] += (" If you don't hear from us soon, please contact "
                             "GlowCare support directly.")
        if verbose:
            print(f"[ESCALATION] FAILED to save: {result['escalation_error']}")


def _money(amount, currency):
    return f"${amount:.2f}" if currency == "USD" else f"{amount:.2f} {currency}"


def _answer_from_database(route) -> dict:
    """Builds the reply from exact database values. Every sentence is a
    fixed template, so the facts in the reply are exactly the facts in
    the database. Returns {"answer": ..., "facts": ...}."""
    if "order_status" in route.intents:
        order = db.get_order_status(route.order_id)
        if order is None:
            return {
                "answer": (f"I couldn't find an order with the number {route.order_id}. "
                           "Please double-check the number from your confirmation email, "
                           "or I can connect you with a team member."),
                "facts": {"order_id": route.order_id, "found": False},
            }
        items = ", ".join(f"{i['quantity']} x {i['product_name']}" for i in order["items"])
        return {
            "answer": (f"Order {order['order_id']} is currently {order['status']}. "
                       f"It contains: {items}."),
            "facts": order,
        }

    sentences, facts = [], []
    for pid in route.product_ids:
        product = db.get_product_by_id(pid)
        if product is None:
            continue
        facts.append({k: product.get(k) for k in
                      ("product_id", "name", "price", "currency",
                       "stock_quantity", "availability", "ingredients")})
        name = product["name"]
        if "price" in route.intents:
            sentences.append(f"The {name} costs {_money(float(product['price']), product['currency'])}.")
        if "stock" in route.intents:
            if product["availability"] == "in_stock" and product["stock_quantity"] > 0:
                left = product["stock_quantity"]
                extra = f" (only {left} left)" if left < 10 else ""
                sentences.append(f"The {name} is in stock{extra}.")
            else:
                sentences.append(f"The {name} is currently out of stock.")
        if "ingredients" in route.intents:
            ingredients = product.get("ingredients") or []
            if ingredients:
                sentences.append(f"The {name} contains: {', '.join(ingredients)}.")
            else:
                sentences.append(f"I don't have the ingredient list for the {name}. "
                                 "A GlowCare team member can help you further.")
    return {"answer": " ".join(sentences) or responder.NOT_FOUND_REPLY, "facts": facts}


def _load_history(conversation_id, history, verbose):
    """Earlier messages of this chat: passed in directly (tests), or read
    from the database log. A failed read just means no memory this time."""
    if history is not None:
        return history[-MEMORY_MESSAGES:]
    if conversation_id is None:
        return []
    try:
        return db.get_recent_turns(conversation_id, MEMORY_MESSAGES)
    except Exception as e:
        if verbose:
            print(f"[MEMORY] couldn't load earlier messages: {type(e).__name__}: {e}")
        return []


def _log_turn(conversation_id, message, result, verbose):
    """Saves the customer's message and the reply (with its audit trail) to
    the messages table. A failed write never breaks the chat; it's noted
    in the result instead."""
    try:
        for row in rows_for_turn(message, result):
            db.log_message(conversation_id, **row)
    except Exception as e:
        result["logging_error"] = f"{type(e).__name__}: {e}"
        if verbose:
            print(f"[LOG] FAILED to save messages: {result['logging_error']}")


def handle_message(message: str, conversation_id: int = None, verbose: bool = True,
                   history: list = None) -> dict:
    """Answers one customer message. Returns everything needed to audit the
    reply (Section 14 of the brief):
        {
          "message":  the customer's text,
          "route":    escalate | greeting | clarify | database | knowledge,
          "reason":   why the router chose that route,
          "answer":   what the customer sees,
          "source":   database | knowledge_base | fixed_reply,
          "escalate": True if a human should take over,
          "escalation_id": the saved escalation's id (only when conversation_id
                           was given and escalate is True),
          "memory":   plain-English note when earlier messages changed the route,
          "details":  DB facts, or retrieved chunks + LLM answer + check results,
        }
    conversation_id: the chat this message belongs to (db.create_conversation()).
    With it, earlier messages are used as memory and this exchange is logged.
    Without it, nothing is read from or written to the database: that's how
    the test sheet runs, so test runs never create fake chats or escalations.
    history: earlier message rows to use as memory instead of the database
    (for tests); see agent.memory.rows_for_turn for the row format.
    """
    products = get_products()
    earlier = _load_history(conversation_id, history, verbose)
    route = route_message(message, products)
    route, memory_note = apply_memory(route, message, products, earlier)
    if verbose:
        print(f"\n[ROUTER] {message!r} -> {route.route} ({route.reason})"
              + (f" products={route.product_ids}" if route.product_ids else "")
              + (f" order={route.order_id}" if route.order_id else ""))
        if memory_note:
            print(f"[MEMORY] {memory_note}")

    result = {"message": message, "route": route.route, "reason": route.reason,
              "intents": route.intents, "product_ids": route.product_ids,
              "order_id": route.order_id, "memory": memory_note,
              "escalate": False, "escalation_reason": None, "escalation_id": None,
              "details": None}

    if route.route == "escalate":
        result.update(answer=ESCALATION_REPLIES[route.reason], source="fixed_reply",
                      escalate=True, escalation_reason=route.reason)

    elif route.route == "guardrail":
        result.update(answer=GUARDRAIL_REPLY, source="fixed_reply")

    elif route.route == "medical":
        result.update(answer=MEDICAL_REPLY, source="fixed_reply")

    elif route.route == "greeting":
        is_thanks = any(w in message.lower() for w in
                        ("thank", "thx", "shukri", "jazakallah", "meherbani"))
        result.update(answer=THANKS_REPLY if is_thanks else GREETING_REPLY,
                      source="fixed_reply")

    elif route.route == "clarify":
        result.update(answer=route.clarify_question, source="fixed_reply")

    elif route.route == "database":
        db_result = _answer_from_database(route)
        result.update(answer=db_result["answer"], source="database",
                      details=db_result["facts"])
        if verbose:
            print(f"[DB] {db_result['facts']}")

    else:  # knowledge
        kb = responder.answer_question(message, products=products, verbose=verbose)
        not_found = kb["answer_type"] == "not_found"
        result.update(answer=kb["answer"], source="knowledge_base", details=kb,
                      # support policy: if the info isn't available, a human follows up
                      escalate=not_found,
                      escalation_reason="information not available" if not_found else None)

    if result["escalate"] and conversation_id is not None:
        _record_escalation(result, conversation_id, verbose)

    if conversation_id is not None:
        _log_turn(conversation_id, message, result, verbose)

    if verbose:
        print(f"[REPLY] ({result['source']}{', ESCALATE' if result['escalate'] else ''}) "
              f"{result['answer']}")
    return result


if __name__ == "__main__":
    # Quick demo: one question per route. For the full pass/fail check run:
    #     python -m tests.run_eval
    demo = [
        "What ingredients are in the Hydra Balance Moisturizer?",
        "How much is the Niacinamide Serum and is it in stock?",
        "Where is my order GC10241?",
        "Can I return a product after opening it?",
        "Do you sell products in the UK?",
        "How much does it cost?",
        "I got a rash after using the cleanser",
    ]
    for q in demo:
        print("=" * 70)
        handle_message(q)  # no conversation_id: nothing is saved
