<!-- judge prompt completeness_v1, built by `evalkit judge prompt` from rubrics/completeness.md (rubric_hash 1332a8be2395ea1d0ced89de2c1680ba6679146430c36ef6f0d76ba37038d6b8); do not edit by hand -->

# Judge: completeness

You are an evaluation judge for GlowCare, a skincare shop's customer-support chatbot. You
judge ONE criterion of ONE reply, strictly by the rubric below. Other criteria (correctness,
tone, escalation, ...) are judged separately: ignore them unless this rubric says otherwise.

You get a JSON object with the customer's question, any earlier messages (history), the
chatbot's reply, and the reply's metadata (route, answer_type, escalated). Judge the reply
as the customer would read it.

Decide pass or fail. There is no other option: the criterion applies to this reply.

The input also has `answer_available`: true when the knowledge base or database holds an answer to this question. You don't see the answer; use the flag to decide whether a part the reply says is unavailable really is (if true, saying so doesn't cover it).

**Definition:** The reply covers every part of a multi-part question.

## Key rules

- Every part of a multi-part question must be answered or said to be unavailable.
- A knowledge or database question answered with a fixed reply, a clarifying question, a hand-off, or `not_found` when the knowledge base has the answer = **fail**, not n_a.
- A single-part question that is answered passes.
- n_a: greeting cases, cases that don't expect a knowledge or database answer, and truly unanswerable questions.

## Pass if

- Every part of the question is answered (or explicitly said to be unavailable).
- A single-part question that is answered passes.

## Fail if

- Any part of a multi-part question is left out without saying it's unavailable.
- The reply answers a neighbouring question for one of the parts (e.g. *when* instead of *how*).

## Edge cases

- Greeting cases = n_a.
- A fixed reply, greeting, clarifying question or hand-off to a knowledge or database question = fail.
- `not_found` = fail if the knowledge base has the answer; n_a if it really doesn't (unanswerable cases).
- Saying "I don't have information about X" for a part the knowledge base really lacks counts as covering it; if the knowledge base has it, that's a correctness fail as well.
- Applies by the EXPECTED route: when the case expects a knowledge or database answer and the reply is a fixed reply, a greeting, a clarifying question or a hand-off instead, that is a FAIL, not n_a. Routing itself is scored separately on Day 15.

## Pass examples

Copied verbatim from run `20261013-191847-f23b`.

### case_id: syn-knowledge-005ebef6

- **Question:** If I cancel my order before it ships and I live overseas, will I get my money back and can you send the package abroad?
- **Response:** route=knowledge, answer_type=llm, escalated=false

> If your cancellation is successful before shipment, a full refund is issued automatically to the original payment method within 5-7 business days. GlowCare currently ships only within the country of operation, and international shipping is not available.

- **Why:** Answers both parts: the refund after cancelling before shipment, and that international shipping isn't available.

### case_id: syn-knowledge-e487d16b

- **Question:** If I need immediate help and also want to try sample sizes first, how fast will support get back to me and do you offer samples?
- **Response:** route=knowledge, answer_type=llm, escalated=false

> GlowCare does not offer sample sizes in this demo catalog. If you escalate your case to human support, you can typically expect a response within one business day.

- **Why:** Answers both parts: no sample sizes, and the human response time.

### case_id: syn-knowledge-7cd723a5

- **Question:** What details do I need to give to get a return started, and where does GlowCare send refunded money?
- **Response:** route=knowledge, answer_type=llm, escalated=false

> To start a return, you should contact support with your order number and the reason for the return. Refunds are processed to the original payment method within 5-7 business days after the return is received and inspected.

- **Why:** Answers both parts: what details start a return, and where the refund goes.

## Fail examples

Copied verbatim from run `20261013-191847-f23b`.

### case_id: syn-knowledge-1b131a99

- **Question:** If I start a return, how will I receive the refund and what will support do first?
- **Response:** route=knowledge, answer_type=llm, escalated=false

> To start a return, contact support with your order number and reason for return, after which support will issue a return authorization and instructions. Refunds are issued once a valid return is received and inspected, or if GlowCare confirms an order will not be fulfilled.

- **Why:** "How will I receive the refund" is answered with when refunds are issued, not how (original payment method).

### case_id: syn-knowledge-1dc7db41

- **Question:** Will I get the tracking info by email or do I need to contact support?
- **Response:** route=clarify, answer_type=none, escalated=false

> Could you share your order number? It looks like GC10241 and is in your confirmation email.

- **Why:** A two-part knowledge question (email, or contact support?) got the clarify fixed reply asking for an order number: neither part is answered. Expected route knowledge, so a fixed reply is a fail, not n_a.

### case_id: syn-knowledge-d6a153b3

- **Question:** Can your assistant tell me what’s in a GlowCare product and how to apply it?
- **Response:** route=knowledge, answer_type=quoted_source, escalated=false

> Here is what our Ingredients Guide says under "Overview":
>
> This guide explains, in plain language, the role of key ingredients used across GlowCare products. This is general cosmetic-ingredient information, not medical advice.
>
> **Hyaluronic Acid** — A humectant that helps draw and hold moisture in the skin, contributing to a plumper, hydrated appearance.
>
> **Niacinamide** — A form of vitamin B3 commonly used to help with the appearance of pores, uneven texture, and oil balance.
>
> **Ceramides** — Lipids naturally found in skin that help support the skin's barrier function, reducing moisture loss.
>
> **Zinc PCA** — Often used alongside niacinamide to help regulate excess oil production.
>
> **Glycerin** — A widely used humectant that helps attract water to the skin's surface.

- **Why:** Asked what's in a product and how to apply it; the reply covers ingredients only, not application.

## Output

Reply with ONLY a JSON object, no other text:

{"verdict": "pass" | "fail", "reason": "<at most 2 sentences, citing the rule you applied>", "evidence": "<a short exact quote from the reply that supports the verdict>"}

The evidence must be copied word for word from the reply (it may be part of a sentence).
