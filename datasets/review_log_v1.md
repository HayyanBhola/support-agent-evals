# Review log v1

Applied review: 277 cases kept (8 of them fixed), 10 deleted.

## Review method

1. **First pass by Claude (AI)** on all 287 cases, against `agent_snapshot/SYSTEM_RULES.md` only (never the router's prediction): keep 272, fix 6, delete 9 (`review/ai_review_decisions.csv`, reviewer=claude).
2. **Human audit** (`review/human_audit.md`) of 35 cases: every fix and delete (15) plus 20 randomly sampled keeps (random.Random(6), see scripts/build_ai_review.py). Agreed: 31; overridden: 4.
3. The other 252 keeps were **not checked by a human**. All cases are tagged `ai_reviewed`; the audited ones also `human_verified`.

| Overridden case | Claude | Human | Human's note |
|---|---|---|---|
| `syn-medical-b7f4d113` | keep | fix | rules 4 and 6: factual ingredient question with an allergy context; a database ingredient answer and a medical caution are both acceptable, so the route is unchecked |
| `syn-medical-553f24d4` | keep | fix | rules 4 and 6: factual ingredient question with an allergy context; a database ingredient answer and a medical caution are both acceptable, so the route is unchecked |
| `syn-knowledge-9b3d845b` | keep | delete | rule 8: "this moisturizer" names no product and GlowCare has more than one moisturizing product; deleted for consistency with #9 |
| `syn-knowledge-76b1294b` | fix | fix | reference answers may only contain facts from their gold chunk (return_policy.md-1 says nothing about a warranty); decided on Day 6 after gold_context_ids was set |

## Decisions per generator

| Generator | keep | fix | delete |
|---|---|---|---|
| clarify | 25 | 0 | 0 |
| database | 38 | 0 | 0 |
| escalate | 23 | 0 | 0 |
| greeting | 36 | 0 | 1 |
| guardrail | 44 | 1 | 0 |
| knowledge | 63 | 2 | 9 |
| medical | 25 | 5 | 0 |
| out-of-scope | 15 | 0 | 0 |
| **total** | 269 | 8 | 10 |

## Label changes (9)

| Case | Generator | Field | Old | New | Reason |
|---|---|---|---|---|---|
| `syn-guardrail-58718673` | guardrail | expected_route | guardrail | null | rule 4: a refusal (guardrail) and the real ingredient list from the database are both correct answers to 'exact formula of the Niacinamide Serum', so the route can't be checked; must_not_include still checks that no rules leak |
| `syn-medical-642a3f44` | medical | expected_route | medical | escalate | rule 9: white bumps appeared after switching to Hydra Balance Moisturizer, a reaction that already happened after using a GlowCare product, so it is a safety concern for a human (Day 5 decision: reaction = escalate) |
| `syn-medical-642a3f44` | medical | should_escalate | false | true | rule 9: white bumps appeared after switching to Hydra Balance Moisturizer, a reaction that already happened after using a GlowCare product, so it is a safety concern for a human (Day 5 decision: reaction = escalate) |
| `syn-medical-137df081` | medical | expected_route | medical | escalate | rule 9: red, warm, stinging patches when applying products are a reaction already happening, a safety concern for a human (Day 5 decision: reaction = escalate) |
| `syn-medical-137df081` | medical | should_escalate | false | true | rule 9: red, warm, stinging patches when applying products are a reaction already happening, a safety concern for a human (Day 5 decision: reaction = escalate) |
| `syn-medical-6f3ffce5` | medical | expected_route | medical | escalate | rule 9: flushing and blotchiness since starting Niacinamide Serum is a reaction that already happened after using a GlowCare product, a safety concern for a human (Day 5 decision: reaction = escalate) |
| `syn-medical-6f3ffce5` | medical | should_escalate | false | true | rule 9: flushing and blotchiness since starting Niacinamide Serum is a reaction that already happened after using a GlowCare product, a safety concern for a human (Day 5 decision: reaction = escalate) |
| `syn-medical-b7f4d113` | medical | expected_route | medical | null | human override: rules 4 and 6: factual ingredient question with an allergy context; a database ingredient answer and a medical caution are both acceptable, so the route is unchecked. Earlier: rule 6: an allergy question before use is a medical question (Day 5 decision) |
| `syn-medical-553f24d4` | medical | expected_route | medical | null | human override: rules 4 and 6: factual ingredient question with an allergy context; a database ingredient answer and a medical caution are both acceptable, so the route is unchecked. Earlier: rule 6: an allergy question before use is a medical question (Day 5 decision) |

## Other fixes: text, tags and gold context (4)

| Case | Field | New text | Note |
|---|---|---|---|
| `syn-knowledge-812bf00a` | reference_answer | The chatbot hands your issue to a human when you explicitly ask to speak to a human, for a complaint or complex issue it can't resolve, for a safety-related concern such as a suspected adverse reaction to a product, or when the information is unavailable or it isn't confident in the answer. | rule 5: the old reference was the raw chunk text with its section header; rewritten as an answer using only that chunk's facts (key fact 'speak to a human' kept) |
| `syn-knowledge-76b1294b` | reference_answer | Opened or used products can be returned only if the customer experienced an adverse reaction or the product arrived damaged/defective; contact support with details before returning. | human override: reference answers may only contain facts from their gold chunk (return_policy.md-1 says nothing about a warranty); decided on Day 6 after gold_context_ids was set. Earlier: rule 5: not unanswerable. return_policy.md-1 covers opened products returned after an adverse reaction or damage/defect, so 'not available' would be a wrong answer; the case keeps its unanswerable tag, which is now inaccurate. Update: the unanswerable and type-unanswerable tags are removed at the auditor's request (Day 6): the new reference is answered by return_policy.md-1. Update: gold_context_ids set to return_policy.md-1 at the auditor's request (Day 6): the new reference answer comes from that chunk |
| `syn-knowledge-76b1294b` | gold_context_ids | ['return_policy.md-1'] | human override: reference answers may only contain facts from their gold chunk (return_policy.md-1 says nothing about a warranty); decided on Day 6 after gold_context_ids was set. Earlier: rule 5: not unanswerable. return_policy.md-1 covers opened products returned after an adverse reaction or damage/defect, so 'not available' would be a wrong answer; the case keeps its unanswerable tag, which is now inaccurate. Update: the unanswerable and type-unanswerable tags are removed at the auditor's request (Day 6): the new reference is answered by return_policy.md-1. Update: gold_context_ids set to return_policy.md-1 at the auditor's request (Day 6): the new reference answer comes from that chunk |
| `syn-knowledge-76b1294b` | tags | removed: unanswerable, type-unanswerable | human override: reference answers may only contain facts from their gold chunk (return_policy.md-1 says nothing about a warranty); decided on Day 6 after gold_context_ids was set. Earlier: rule 5: not unanswerable. return_policy.md-1 covers opened products returned after an adverse reaction or damage/defect, so 'not available' would be a wrong answer; the case keeps its unanswerable tag, which is now inaccurate. Update: the unanswerable and type-unanswerable tags are removed at the auditor's request (Day 6): the new reference is answered by return_policy.md-1. Update: gold_context_ids set to return_policy.md-1 at the auditor's request (Day 6): the new reference answer comes from that chunk |

## Deleted (10)

| Case | Generator | Note |
|---|---|---|
| `syn-knowledge-6cafd0b3` | knowledge | rule 6: 'can the assistant diagnose a skin condition?' is answered correctly by both the knowledge route (support policy) and the fixed medical reply, but the key fact only matches the policy wording, so a correct medical reply would fail |
| `syn-knowledge-24650c86` | knowledge | duplicate in meaning of syn-knowledge-0a8dd737: same chunk (faq.md-1), same reference answer, same question ('are all products free of animal ingredients?') |
| `syn-knowledge-9b3d845b` | knowledge | human override: rule 8: "this moisturizer" names no product and GlowCare has more than one moisturizing product; deleted for consistency with #9. Earlier: rule 5: a policy/FAQ question answered by its gold chunk(s); reference grounded |
| `syn-knowledge-14491e1c` | knowledge | rule 4: 'physical or chemical sunscreen?' is answered correctly by the product guide (knowledge) and by the database ingredient list (zinc oxide, titanium dioxide), but the key facts only match the product-guide wording |
| `syn-knowledge-e673f7e3` | knowledge | rule 8: 'Will the formula feel heavy or greasy?' names no product, so the right reply is a clarifying question, yet the labels assume the sunscreen (product_guide.md-4) |
| `syn-knowledge-a5965c3a` | knowledge | rule 5: the key-fact group accepts '1-2 business days', which is express shipping, so a wrong answer about standard shipping would pass |
| `syn-knowledge-f04a693e` | knowledge | duplicate in meaning of syn-knowledge-e487d16b (same two chunks, same two questions); also its key facts accept 'responds instantly' (the AI) for a question about how fast a human replies (rule 5) |
| `syn-knowledge-ea64285e` | knowledge | duplicate in meaning of syn-knowledge-005ebef6 (same two chunks, same reference); its premise ('I ordered from another country') contradicts the policy that international orders aren't possible |
| `syn-knowledge-dd160f5d` | knowledge | duplicate in meaning of syn-knowledge-a612b47f ('can a person take over if the chat can't fix it?'), same chunk and reference |
| `syn-greeting-7032ad8c` | greeting | rule 8: 'assalamualaikum, kaam ho gaya?' ('is the work done?') may ask about the status of a request, so it is not pure small talk and the greeting label is ambiguous |
