---
id: tone
version: 2
verdicts: [pass, fail, n_a]
applies_by: actual
applies_when: route
applies_to:
  routes: [escalate, guardrail, medical, greeting, clarify, database, knowledge]
  answer_types: [llm, quoted_source, not_found, none]
---

# Tone

**Definition:** The reply is polite, acknowledges an upset or worried customer, is concise, and has no fake dialogue or role-play.

## Key rules

- Judge only: polite, acknowledges an upset or worried customer, concise, no fake dialogue or role-play.
- Voice is not tone (v2): "Customers should...", "the customer" or "the assistant" copied from policy text is not a fail on its own.
- An upset customer answered with a cold policy quote or a procedure and no acknowledgement = **fail**.
- Not concise: a whole guide or several sections when one part was asked = **fail**.
- Never n_a; tone is independent of whether the answer is right or the escalation was.

## Pass if

- Polite (warmth is welcome but not required).
- Concise: answers without dumping whole documents.
- Acknowledges an upset or worried customer before giving information.

## Fail if

- Answers an upset customer with a cold policy quote or a procedure and no acknowledgement.
- Contains fake dialogue, role-play or a persona.
- Not concise: quotes a whole guide or several sections when the question needed one part.

## Edge cases

- Voice is not judged under tone (v2, Day 12 decision; it was a fail in v1): third-person self-reference ("the assistant...") or policy voice ("Customers should...", "the customer") copied from the knowledge base is not a fail on its own. A cold policy quote to an upset customer still fails, for the missing acknowledgement.
- TODO Day 15: a deterministic rule `voice_third_person` in `src/evalkit/metrics/rules.py` ("customers should", "the customer", "the assistant", "the bot", ...) with tests, reported next to tone instead of judged by humans or the judge.
- Anchor for "not concise": `syn-knowledge-d6a153b3` ("Can your assistant tell me what's in a GlowCare product and how to apply it?") got the whole Ingredients Guide overview, every ingredient with its description, when one product's ingredients and usage were asked for = fail.
- `quoted_source` replies are judged like any other reply: a policy quote that answers a calm question politely and concisely passes, whatever its voice. Rewriting the knowledge base in the second person is still a Week 4 experiment idea.
- Fixed replies are judged like any other reply.
- Tone is independent of correctness: a wrong answer can have good tone.
- No fake dialogue or role-play was seen in the Day 7 run; the rule stays for future agents.
- Applies by the ACTUAL reply: judge the reply the agent gave (its route and answer_type), not the route the case expected.

## Pass examples

Copied verbatim from run `20261013-191847-f23b`.

### case_id: legacy-greeting

- **Question:** hello
- **Response:** route=greeting, answer_type=none, escalated=false

> Hi! I'm the GlowCare assistant. I can help with our products, prices, stock, your orders, and our shipping, return and refund policies. What can I help you with?

- **Why:** Friendly, introduces itself, says what it can help with, and asks a question.

### case_id: legacy-esc-safety

- **Question:** I got a rash after using the cleanser
- **Response:** route=escalate, answer_type=none, escalated=true

> I'm sorry you're dealing with this. Because it may be a reaction to a product, I'm passing your message to a GlowCare team member right away so a person can help you.

- **Why:** Acknowledges the customer ("I'm sorry you're dealing with this") and says clearly what happens next.

### case_id: syn-database-b855ce36

- **Question:** What's the status of my order GC99999?
- **Response:** route=database, answer_type=none, escalated=false

> I couldn't find an order with the number GC99999. Please double-check the number from your confirmation email, or I can connect you with a team member.

- **Why:** Polite, clear, and offers a next step.

### case_id: syn-knowledge-b3e584f1

- **Question:** How quickly does the chatbot reply when I ask a question?
- **Response:** route=knowledge, answer_type=llm, escalated=false

> The AI assistant responds instantly to questions.

- **Why:** Concise and civil, and the customer isn't upset. It talks about itself in the third person ("The AI assistant"), which is voice, not tone, in v2 (it was a fail example in v1).

## Fail examples

Copied verbatim from run `20261013-191847-f23b`.

Shortage: 2 real fail examples in v2. `syn-knowledge-b3e584f1` failed only for third-person voice, which is no longer a tone fail, so it moved to the pass examples. No labeled case is used as a replacement (labeled cases stay out of the rubric examples). The not-concise anchor `syn-knowledge-d6a153b3` is described under Edge cases.

### case_id: syn-escalate-e0c70db0

- **Question:** Spent money on your products and now support keeps closing my case without resolving anything. I want this taken seriously.
- **Response:** route=knowledge, answer_type=llm, escalated=false

> Since this is a complaint about a complex issue that the assistant cannot resolve, this case should be escalated to a human team member. A GlowCare team member will be able to assist you further, typically within one business day.

- **Why:** An upset customer gets a procedural answer ("this case should be escalated") with no acknowledgement. (Its "the assistant" is voice, not tone, in v2.)

### case_id: syn-escalate-8366bd0b

- **Question:** Customer service processed no refund after I followed the return instructions. Please reopen my case and issue the refund I’m owed.
- **Response:** route=knowledge, answer_type=quoted_source, escalated=false

> Here is what our Return Policy says under "How to Start a Return":
>
> Customers should contact support with their order number and reason for return. Support will issue a return authorization and instructions.

- **Why:** A refund complaint gets a raw policy quote with no acknowledgement. The fail is the missing acknowledgement, not the policy voice.
