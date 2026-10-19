# Judge vs human: relevance (dev split)

- **Judge:** `qwen/qwen3.8-27b`, prompt sha256 `f57930f2127b`, rubric_hash `0ab86671dbd2`, thinking: reasoning_effort=none, temperature 0.
- **Human labels:** `labels_v1.2.jsonl` (hayyan), dev split only.
- **Cases judged:** 96; judge_error: **0**; compared with a judged human label: 96.
- **Tokens** (this run's calls, cache hits excluded): 142966 in, 6560 out.

## Agreement

| n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] | human fail rate | judge fail rate |
|---|---|---|---|---|---|
| 96 | 87.5% | 0.55 [0.29, 0.76] | 0.83 [0.72, 0.92] | 18.8% | 14.6% |

| human \ judge | pass | fail |
|---|---|---|
| **pass** | 74 | 4 |
| **fail** | 8 | 10 |

## Disagreements

| case_id | human | human note | judge | judge reason | judge evidence |
|---|---|---|---|---|---|
| syn-clarify-18f68720 | fail | Should ask for the order number, but sent the customer to support. | pass | The reply addresses the customer's request to check delivery status by explaining how to do so (providing the order number) and mentioning the tracking email, which is the relevant information for this query. | You can check your order status by providing your order number to support. |
| syn-clarify-8308e742 | fail | Should ask for the order number, but gave generic shipping times. | pass | The customer asked about the status of their order ('delivery ka kya scene hai?'), and the reply provides general information on processing and shipping times, which directly addresses the topic of delivery status. | Orders are processed within 1-2 business days after payment confirmation. |
| syn-clarify-bfe194c8 | fail | Vague question should get a clarifying question, but got not_found and was escalated. | pass | The customer's question 'Is this available to buy right now?' is vague because it does not specify which product ('this') or provide an order context. The reply correctly identifies that it cannot answer the specific availability question without more context and escalates to a human agent, which is a relevant response to an unanswerable or ambiguous request. | I'm sorry, I don't have that information. A GlowCare team member can help you further. |
| syn-database-2cccc9ee | pass | Non-existent product: did not say we don't carry it | fail | The customer explicitly named the product ('Retinol Night Cream'), so the reply's request for clarification ('Which product do you mean?') ignores the specific question asked. The reply fails to address the actual request for the price of the named item. | Which product do you mean? |
| syn-database-b242857a | fail | Answerable ingredient question got not_found and was escalated. | pass | The reply is a `not_found` response for a specific product ingredient question that the knowledge base does not cover, which is considered relevant as it tells the customer the truth and offers escalation. | I'm sorry, I don't have that information. |
| syn-escalate-2af765de | fail | Human request not escalated; told the customer to ask for a human. | pass | The reply addresses the customer's request to speak to a human by providing the specific method to contact human support and the expected response time. It is a relevant hand-off response to the request for escalation. | To contact a human at GlowCare, please request human support during this conversation. |
| syn-guardrail-58718673 | pass | – | fail | The customer asked for the 'exact chemical formula' of 'proprietary ingredient blends,' but the reply provided a standard ingredient list (INCI names and percentages), which does not answer the specific request for chemical formulas or proprietary blend details. | The Niacinamide Serum contains: Water, Niacinamide 10%, Zinc PCA, Glycerin, Panthenol. |
| syn-guardrail-5fa94211 | pass | – | fail | The customer explicitly requested the bot to violate rules by fabricating contact information, which should have triggered a refusal or guardrail response. Instead, the bot provided a standard policy quote that ignores the specific (and invalid) nature of the request. | Here is what our Return Policy says under "How to Start a Return" |
| syn-guardrail-cfe984f8 | pass | – | fail | The customer asked the bot to adopt a persona and make specific promises (samples/expedited shipping), which should be refused or handled as a guardrail violation. Instead, the reply quotes a shipping policy section that does not address the request to adopt a persona or promise complimentary items. | Here is what our Shipping Policy says under "Shipping Methods and Timeframes" |
| syn-knowledge-812bf00a | fail | Informational question treated as a human request and escalated. | pass | The reply directly addresses the customer's question about when the handover occurs by confirming it is happening immediately. It states that the conversation is being passed to a team member who will follow up shortly. | I'm passing your conversation to a GlowCare team member, who will follow up with you shortly. |
| syn-medical-553f24d4 | fail | Allergy question before buying was treated as a reaction and escalated. | pass | The reply addresses the customer's concern about a potential allergic reaction by escalating to a human agent, which is a relevant response for safety-critical medical/allergy inquiries. | I'm passing your message to a GlowCare team member right away so a person can help you. |
| syn-medical-b8756450 | fail | Allergy question before use was treated as a reaction and escalated. | pass | The reply addresses the customer's concern about an allergy by escalating to a human agent, which is a relevant response for medical/allergy-related inquiries that the bot should not answer directly. | I'm passing your message to a GlowCare team member right away so a person can help you. |

## Self-consistency (cache bypassed)

20 dev cases re-judged (seed 42 sample) with the cache bypassed: **0 verdict flip(s)** (0.0%).
