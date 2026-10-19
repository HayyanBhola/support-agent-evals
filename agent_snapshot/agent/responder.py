"""
agent/responder.py

Combines three things into one grounded answer, per Section 8 of the
brief ("Documents -> ... -> Retriever -> Relevant chunks -> LLM ->
Grounded response"):

1. rag.retriever.retrieve() — finds relevant chunks from the knowledge base
2. SYSTEM_RULES.md — the guardrails, loaded fresh every call so an edit
   to that file changes behavior without touching this code
3. agent.llm_client.call_llm() (OpenAI or Ollama, set in .env) — turns (rules + context + question)
   into a natural-language answer

Since Phase 5 this file only handles questions the router sent to the
knowledge base (policies, FAQ, product descriptions). Prices, stock,
ingredient lists and orders never reach it: agent/support_agent.py answers
those straight from the database.

Every LLM answer is checked by agent/verifier.py before it is returned. If
a check fails, the LLM answer is thrown away and the customer gets the
retrieved policy text itself (quoted), or an honest "I don't have that".
"""

from rag.retriever import retrieve
from agent.llm_client import call_llm, LLM_PROVIDER, MODEL_NAME, LLMUnavailableError
from agent.verifier import clean_answer, verify

SYSTEM_RULES_PATH = "SYSTEM_RULES.md"


def _load_system_rules() -> str:
    """Reads SYSTEM_RULES.md fresh on every call. Not cached on purpose —
    Section 13's guardrails should take effect the moment the file is
    edited, with no need to restart anything."""
    with open(SYSTEM_RULES_PATH, "r", encoding="utf-8") as f:
        return f.read()


# The exact reply used whenever the knowledge base has no answer. Fixed
# text (not LLM-generated) so an "I don't know" can never turn into a guess.
NOT_FOUND_REPLY = (
    "I'm sorry, I don't have that information. "
    "A GlowCare team member can help you further."
)


def _build_prompt(question: str, context: str) -> str:
    """Builds the actual text sent to the LLM. Kept as a plain, readable
    string (not a templating library) so it's easy to see and change
    exactly what the model is being told.

    WHY THIS SHAPE (learned in Phase 4 testing with a 1.5B model):
    - SHORT: small models lose track of long rule lists, so there are
      only 4 instructions.
    - Rule 3 exists because the model once listed ceramides and zinc PCA
      as Hydra Balance ingredients: it read them from the general
      Ingredients Guide and assumed they belonged to the product.
    - QUESTION LAST: small models pay most attention to the end of the
      prompt, so the question sits right before "Answer:" instead of at
      the top, where it got buried under the context.
    - LABELED EXCERPTS: every excerpt starts with its section title
      (e.g. "Shipping Policy > Shipping Destinations"), which helps the
      model tell relevant excerpts from irrelevant ones.
    - Rule 1 asks for excerpts that CONTAIN the answer, not ones whose
      title matches. An earlier "match the title" wording made the model
      refuse the return question, because "Condition for Returns" didn't
      literally match "return after opening".
    - Rule 1's "without using the customer's exact words" line exists
      because gpt-4o-mini refused "Do you sell products in the UK?" even
      though the shipping-destinations excerpt settles it (no
      international shipping). The example uses France, not the UK, so
      the test case still checks that the rule generalises.
    """
    return (
        f"Knowledge base excerpts (some may be irrelevant):\n\n"
        f"{context}\n\n"
        f"Instructions:\n"
        f"1. Use only the excerpts that contain the answer to the "
        f"customer's question. An excerpt can answer a question without "
        f"using the customer's exact words (e.g. a rule about which "
        f"countries we ship to answers \"do you sell in France?\"). "
        f"Ignore excerpts about other topics.\n"
        f"2. Answer in 1-3 plain sentences using only facts from those "
        f"excerpts. No greeting, no preamble, no dialogue, no advice that "
        f"isn't in the excerpts.\n"
        f"3. Only say a product contains an ingredient or has a feature if "
        f"an excerpt about THAT product says so. The Ingredients Guide "
        f"describes ingredients used across all products — it never says "
        f"which product contains them.\n"
        f"4. Only if NONE of the excerpts contain the answer, reply with "
        f"exactly: \"{NOT_FOUND_REPLY}\"\n\n"
        f"Customer question: {question}\n"
        f"Answer:"
    )


# Quoting a chunk is only safe if it's a strong match. Above this distance
# the best chunk is probably about something else, so the agent says it
# doesn't know instead. Tune with tests/run_eval.py.
QUOTE_MAX_DISTANCE = 0.75
# Stricter bar for overruling the model when it said "I don't know".
REFUSAL_OVERRIDE_DISTANCE = 0.5


def _quote_best_chunk(chunks: list):
    """The fallback when an LLM answer fails the checks: show the customer
    the most relevant policy text word for word. Can't be a hallucination,
    because it IS the source. Returns None if no chunk is a strong match."""
    if not chunks or chunks[0]["distance"] > QUOTE_MAX_DISTANCE:
        return None
    lines = chunks[0]["text"].splitlines()
    title, body = lines[0], "\n".join(lines[1:]).strip()
    section = title.split(" > ")[-1]
    document = title.split(" > ")[0].replace("GlowCare ", "")
    if body.startswith("**Q:"):  # FAQ chunk: show just the answer part
        answer_part = body.split("\n", 1)[-1].strip()
        answer_part = answer_part[2:].strip() if answer_part.startswith("A:") else answer_part
        return f"From our FAQ: {answer_part}"
    return f'Here is what our {document} says under "{section}":\n\n{body}'


def answer_question(question: str, products: list = None, verbose: bool = True) -> dict:
    """The full pipeline for one question. Returns a dict with everything
    Section 8 (logging/auditability) asks to be visible:
        {
            "question": ...,
            "chunks_used": [...],       # from the retriever
            "prompt_sent_to_llm": ...,  # exact text the model saw
            "llm_answer": ...,          # what the model wrote (before checks)
            "problems": [...],          # why the checks rejected it, if they did
            "answer": ...,              # what the customer actually gets
            "answer_type": ...,         # llm | quoted_source | not_found
            "llm_error": ...,           # set only if the AI service was down
        }
    `products` (from db.get_all_products()) lets the checker catch wrong
    product/ingredient claims; without it that one check is skipped.
    """
    products = products or []
    retrieval = retrieve(question, verbose=verbose)
    context = retrieval["context"]
    chunks_used = retrieval["chunks"]

    # Nothing relevant retrieved -> don't even ask the LLM. Returning the
    # fixed reply guarantees SYSTEM_RULES rule 7 ("say so plainly")
    # instead of hoping a small model follows it.
    if not chunks_used:
        if verbose:
            print(f"[LLM] Skipped — no relevant chunks. Answer: {NOT_FOUND_REPLY}\n")
        return {
            "question": question,
            "chunks_used": [],
            "prompt_sent_to_llm": None,
            "llm_answer": None,
            "problems": [],
            "answer": NOT_FOUND_REPLY,
            "answer_type": "not_found",
            "llm_error": None,
        }

    system_rules = _load_system_rules()
    prompt = _build_prompt(question, context)

    if verbose:
        print(f"\n[LLM] Sending prompt (system rules + {len(chunks_used)} "
              f"chunk(s) of context) to {LLM_PROVIDER} ({MODEL_NAME})...")

    try:
        llm_answer = call_llm(prompt=prompt, system_prompt=system_rules)
    except LLMUnavailableError as e:
        # The AI service is overloaded or unreachable even after retrying
        # (Phase 10: Gemini "503 high demand"). The customer still gets an
        # answer: the best-matching policy text, quoted word for word, or an
        # honest "I don't have that" (which also flags a human to follow up).
        # Same safe fallback as a rejected AI answer; nothing is made up.
        quoted = _quote_best_chunk(chunks_used)
        answer = quoted or NOT_FOUND_REPLY
        answer_type = "quoted_source" if quoted else "not_found"
        problems = [f"AI service unavailable, answered from the documents instead ({e})"]
        if verbose:
            print(f"[LLM] UNAVAILABLE: {e}")
            print(f"[CHECK] Using fallback: {answer_type}")
            print(f"[ANSWER] {answer}\n")
        return {
            "question": question,
            "chunks_used": chunks_used,
            "prompt_sent_to_llm": prompt,
            "llm_answer": None,
            "problems": problems,
            "answer": answer,
            "answer_type": answer_type,
            "llm_error": str(e),
        }

    answer = clean_answer(llm_answer)
    if verbose:
        print(f"[LLM] Raw answer: {llm_answer}")

    # The model said "I don't know". Usually right — but in Phase 4 it
    # refused the return question even though the top chunk was a near-
    # perfect match. So if the best chunk is a VERY strong match, quote it
    # instead of refusing.
    if NOT_FOUND_REPLY.lower()[:30] in answer.lower():
        problems = []
        strong = chunks_used[0]["distance"] <= REFUSAL_OVERRIDE_DISTANCE
        quoted = _quote_best_chunk(chunks_used) if strong else None
        if quoted:
            problems = ["model refused, but the top chunk is a very strong match"]
        answer_type = "quoted_source" if quoted else "not_found"
        answer = quoted or NOT_FOUND_REPLY
    else:
        problems = verify(answer, context, products)
        if not problems:
            answer_type = "llm"
        else:
            quoted = _quote_best_chunk(chunks_used)
            answer_type = "quoted_source" if quoted else "not_found"
            answer = quoted or NOT_FOUND_REPLY

    if verbose:
        if problems:
            print(f"[CHECK] LLM answer REJECTED: {'; '.join(problems)}")
            print(f"[CHECK] Using fallback: {answer_type}")
        else:
            print("[CHECK] Passed all checks.")
        print(f"[ANSWER] {answer}\n")

    return {
        "question": question,
        "chunks_used": chunks_used,
        "prompt_sent_to_llm": prompt,
        "llm_answer": llm_answer,
        "problems": problems,
        "answer": answer,
        "answer_type": answer_type,
        "llm_error": None,
    }


if __name__ == "__main__":
    # Knowledge-base-only smoke test. For the full agent (database, routing,
    # escalation) run:  python -m agent.support_agent
    test_questions = [
        "Can I return a product after opening it?",
        "Do you sell products in the UK?",
        "What is your policy on returning shoes?",  # should not guess
    ]
    for q in test_questions:
        print("=" * 70)
        answer_question(q)
