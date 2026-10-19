# Judge vs human: tone (dev split)

- **Judge:** `gpt-5-mini-2025-08-07`, prompt sha256 `17b4832b780a`, rubric_hash `1332a8be2395`, thinking: openai reasoning hidden, reasoning_effort=minimal, model default temperature (none sent).
- **Human labels:** `labels_v1.3.jsonl` (hayyan), dev split only.
- **Cases judged:** 96; judge_error: **0**; compared with a judged human label: 96.
- **Tokens** (this run's calls, cache hits excluded): 168658 in, 7270 out.

## Agreement

| n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] | human fail rate | judge fail rate |
|---|---|---|---|---|---|
| 96 | 83.3% | 0.09 [0.00, 0.28] | 0.80 [0.68, 0.89] | 1.0% | 17.7% |

| human \ judge | pass | fail |
|---|---|---|
| **pass** | 79 | 16 |
| **fail** | 0 | 1 |

## Disagreements

| case_id | human | human note | judge | judge reason | judge evidence |
|---|---|---|---|---|---|
| bug-greeting-hi-how-are-you | pass | Greeting got not_found and was escalated. | fail | The reply is polite but does not acknowledge an upset or worried customer; it also is not clearly concise for this simple greeting—more importantly, the customer wasn't upset, so acknowledgment wasn't required, but the reply is terse and mismatched to the greeting. It fails because it doesn't appropriately respond to a simple greeting (no greeting returned). | I'm sorry, I don't have that information. |
| legacy-kb-help-acne | pass | Medical question before use got not_found and was escalated. | fail | The customer asked about a health/skin concern (potentially worried); the reply is polite but fails to acknowledge the customer's concern before offering escalation. It is also very brief without empathy. | I'm sorry, I don't have that information. |
| legacy-med-pregnancy | pass | – | fail | Customer asked about safety during pregnancy (a potentially worried customer) and the reply gives a cold refusal and procedural guidance without acknowledging concern or empathy. The reply lacks an acknowledgment of the customer's possible worry and is not empathetic as required by the rubric. | I can't give medical advice or say that a product treats or cures a skin condition. |
| syn-clarify-8308e742 | pass | Should ask for the order number, but gave generic shipping times. | fail | Reply is concise but does not acknowledge a worried/upset customer; the user asked about their order in casual urgent tone and the response gives policy only with no acknowledgement. Per rules, an upset customer answered with a cold policy/procedure and no acknowledgement fails. | Orders are processed within 1-2 business days after payment confirmation. |
| syn-database-2cccc9ee | pass | Customer named the product; reply asked which product instead of saying we don't carry it. | fail | The reply is not concise and does not acknowledge any customer concern; it asks a clarifying question but lists many products unrelated to the customer's asked product, which is not concise. | Which product do you mean? We have: Hydra Balance Moisturizer, Barrier Repair Cream, Niacinamide Serum, Gentle Daily Cleanser, Lightweight Sunscreen SPF 50. |
| syn-greeting-368397f7 | pass | Greeting got not_found and was escalated. | fail | Customer greeted politely and not upset, so acknowledgement of upset/worried customer wasn't required, but the reply is too brief and doesn't include a simple friendly response to the greeting—it's not concise and appropriate to the user's neutral greeting. It also includes escalation but lacks a welcoming acknowledgment of the user's greeting. | I'm sorry, I don't have that information. |
| syn-greeting-b1a383ee | pass | Greeting got not_found and was escalated. | fail | Customer appears casual/greeting; assistant's reply is an apology for missing info but does not acknowledge any upset/worried state and is not concise for a simple greeting—it's unnecessary. It also escalated but gave no empathetic acknowledgment; missing acknowledgement when customer might be upset or worried fails the criterion. | I'm sorry, I don't have that information. |
| syn-greeting-f4bcd12f | pass | Greeting got not_found and was escalated. | fail | The reply is terse and lacks an acknowledgement of an upset or worried customer when escalation occurred; it does not politely address the customer's greeting or state empathy. The response is also minimal and doesn't acknowledge the user's tone. | I'm sorry, I don't have that information. A GlowCare team member can help you further. |
| syn-guardrail-58718673 | pass | – | fail | Reply does not acknowledge an upset or worried customer and is not framed politely; it bluntly lists ingredients without any acknowledgement. The rubric requires acknowledging upset/worried customers before giving information and being concise—this reply lacks acknowledgement. | The Niacinamide Serum contains: Water, Niacinamide 10%, Zinc PCA, Glycerin, Panthenol. |
| syn-guardrail-5fa94211 | pass | Quoted an unrelated policy instead of refusing the request | fail | The customer asked to act outside rules and is potentially upset; the reply is a cold policy quote with no acknowledgement or empathy, which fails the requirement to acknowledge an upset or worried customer before giving information. | Here is what our Return Policy says under "How to Start a Return":  Customers should contact support with their order number and reason for return. Support will issue a return authorization and instructions. |
| syn-knowledge-3f4d929c | pass | – | fail | Reply is concise and informative but lacks any acknowledgment of potential customer worry; when a customer asks about refunds they may be concerned and the reply contains no empathetic or reassuring phrase as required. | If a cancellation is successful before shipment, a full refund is issued automatically to the original payment method within 5-7 business days. |
| syn-knowledge-7bff2c0c | pass | – | fail | The customer asked in Hindi likely seeking advice; reply is factual but does not acknowledge any concern or show politeness; no empathetic phrase and it's minimal but lacks acknowledgement of an upset/worried customer when required. According to rubric, upset/worried customers must be acknowledged before info. | Hyaluronic acid is a humectant that helps draw and hold moisture in the skin, contributing to a plumper, hydrated appearance. |
| syn-knowledge-e115acb9 | pass | – | fail | The customer asked about cancellation and refund timing but may be worried; the reply gives policy information without any acknowledgment or empathy, which fails the requirement to acknowledge an upset or worried customer before providing information. | If a cancellation is successful before shipment, a full refund is issued automatically to the original payment method within 5-7 business days. |
| syn-medical-190c5dd1 | pass | Medical question before use got not_found and was escalated. | fail | Customer asked about acne scars (a worried/upset health/appearance concern) and the reply gives no acknowledgement or empathy before stating lack of information and escalation. The rubric requires acknowledging an upset or worried customer before giving information or escalation. | I'm sorry, I don't have that information. |
| syn-medical-82ffc8a9 | pass | – | fail | The customer asked about distinguishing PIH vs scars (a worried customer); the reply gives a cold refusal directing to a doctor without acknowledging concern and is not helpful—fails requirement to acknowledge an upset/worried customer. | I can't give medical advice or say that a product treats or cures a skin condition. |
| syn-medical-d858a37e | pass | Ingredient question not answered; quoted product copy instead. | fail | Customer expressed sensitivity; the reply only quoted product info without acknowledging the customer's concern—an upset/worried customer must be acknowledged before giving information (policy). | New users are advised to introduce it gradually (every other day) to assess tolerance. |

## Self-consistency (cache bypassed)

Not run.

## v1 vs v2 (same labels, same dev cases)

| | v1 | v2 |
|---|---|---|
| n | 96 | 96 |
| % agreement | 55.2% | 83.3% |
| Cohen's kappa [95% CI] | 0.02 [0.00, 0.08] | 0.09 [0.00, 0.28] |
| Gwet's AC1 [95% CI] | 0.30 [0.08, 0.50] | 0.80 [0.68, 0.89] |
| human pass / judge fail | 43 | 16 |
| human fail / judge pass | 0 | 0 |
| both pass / both fail | 52 / 1 | 79 / 1 |
| judge fail rate | 45.8% | 17.7% |

**Fixed in v2 (28):** bug-greeting-hello-hello, legacy-clarify-order, legacy-db-price-hydra, legacy-kb-cancel-shipped, legacy-kb-processing, legacy-kb-refund-cash, legacy-kb-refund-shipping-fee, legacy-oos-competitor, syn-database-ad97bede, syn-greeting-b0f9f241, syn-greeting-c3e3eecf, syn-guardrail-3e99311e, syn-guardrail-55403122, syn-knowledge-0b6223ff, syn-knowledge-136f40c4, syn-knowledge-2e7c3ae7, syn-knowledge-53d700de, syn-knowledge-5ee7e9e7, syn-knowledge-6ad24673, syn-knowledge-8ebe887b, syn-knowledge-9a81c4df, syn-knowledge-a612b47f, syn-knowledge-c4420daa, syn-knowledge-fdd5b5a1, syn-medical-00d2b211, syn-medical-137df081, syn-medical-19b81765, syn-out-of-scope-6b06ea9a
**Still disagreeing (15):** bug-greeting-hi-how-are-you, legacy-kb-help-acne, legacy-med-pregnancy, syn-clarify-8308e742, syn-database-2cccc9ee, syn-greeting-368397f7, syn-greeting-b1a383ee, syn-greeting-f4bcd12f, syn-guardrail-58718673, syn-guardrail-5fa94211, syn-knowledge-7bff2c0c, syn-knowledge-e115acb9, syn-medical-190c5dd1, syn-medical-82ffc8a9, syn-medical-d858a37e

**New in v2 (1):**

| case_id | human | judge | judge reason |
|---|---|---|---|
| syn-knowledge-3f4d929c | pass | fail | Reply is concise and informative but lacks any acknowledgment of potential customer worry; when a customer asks about refunds they may be concerned and the reply contains no empathetic or reassuring phrase as required. |
