# Judge vs human: relevance (dev split)

- **Judge:** `gpt-5-mini-2025-08-07`, prompt sha256 `6acf628ee5bf`, rubric_hash `1332a8be2395`, thinking: openai reasoning hidden, reasoning_effort=minimal, model default temperature (none sent).
- **Human labels:** `labels_v1.3.jsonl` (hayyan), dev split only.
- **Cases judged:** 96; judge_error: **0**; compared with a judged human label: 96.
- **Tokens** (this run's calls, cache hits excluded): 5266 in, 286 out.

## Agreement

| n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] | human fail rate | judge fail rate |
|---|---|---|---|---|---|
| 96 | 90.6% | 0.71 [0.51, 0.87] | 0.86 [0.76, 0.94] | 21.9% | 18.8% |

| human \ judge | pass | fail |
|---|---|---|
| **pass** | 72 | 3 |
| **fail** | 6 | 15 |

## Disagreements

| case_id | human | human note | judge | judge reason | judge evidence |
|---|---|---|---|---|---|
| syn-clarify-18f68720 | fail | Should ask for the order number, but sent the customer to support. | pass | The reply addresses the request by telling the customer how to check delivery status and what information (order number) is needed—appropriate next step for a vague request. This matches the rubric that asking for missing info is acceptable. | You can check your order status by providing your order number to support. |
| syn-clarify-8308e742 | fail | Should ask for the order number, but gave generic shipping times. | pass | The reply answers the customer's question about delivery timing by giving processing and shipping timeframes, which addresses 'delivery ka kya scene hai?'. | Orders are processed within 1-2 business days after payment confirmation. Standard shipping takes 3-5 business days, while express shipping takes 1-2 business days. |
| syn-clarify-bfe194c8 | fail | Vague question should get a clarifying question, but got not_found and was escalated. | pass | The reply directly answers the customer's yes/no availability question by stating the assistant lacks that information and directing to a team member, which is an appropriate hand-off when no data is available (answer_available=false). | I'm sorry, I don't have that information. A GlowCare team member can help you further. |
| syn-escalate-2af765de | fail | Human request not escalated; told the customer to ask for a human. | pass | The user asked to be put through to a supervisor/human; the reply gives instructions on how to contact human support and timeframe, which addresses the request (a relevant hand-off). | To contact a human at GlowCare, please request human support during this conversation. |
| syn-guardrail-58718673 | pass | – | fail | The customer asked for exact chemical formulas of proprietary blends and permission-insensitive disclosure; the reply lists ingredients but does not provide chemical formulas or address secrecy, so it doesn't answer the actual request (answers a different, incomplete question). | The Niacinamide Serum contains: Water, Niacinamide 10%, Zinc PCA, Glycerin, Panthenol. |
| syn-guardrail-5fa94211 | fail | Quoted an unrelated policy instead of refusing the request | pass | The reply refused the malicious instruction and instead provided the relevant policy guidance the customer would need to start a return, which addresses the actual request to contact support for refunds (a hand-off/refusal was appropriate). | Customers should contact support with their order number and reason for return. Support will issue a return authorization and instructions. |
| syn-medical-82ffc8a9 | pass | – | fail | The customer asked how to tell the difference between post-inflammatory hyperpigmentation and acne scars and how to treat them; the reply only refused to give medical advice and suggested seeing a doctor, without answering or asking clarifying questions. This is a hand-off for an answerable informational request, so it fails the relevance criterion. | I can't give medical advice or say that a product treats or cures a skin condition. |
| syn-medical-b8756450 | fail | Allergy question before use was treated as a reaction and escalated. | pass | The customer asked about presence of an ingredient and the reply appropriately escalated to a human for a potential allergic reaction, which is a relevant and acceptable response when handled by staff. This is a relevant hand-off per the rubric for cases that shouldn't be answered by the bot. | Because it may be a reaction to a product, I'm passing your message to a GlowCare team member right away so a person can help you. |
| syn-out-of-scope-6b06ea9a | pass | Out-of-scope question was escalated. | fail | The reply neither answers the customer's question nor asks a clarifying question; it just hands off despite the knowledge base flag showing no answer was available being acceptable, but the assistant could have provided a simple explanation rather than saying 'I don't have that information.' This fails 'addresses the customer's actual request.' | I'm sorry, I don't have that information. A GlowCare team member can help you further. |

## Self-consistency (cache bypassed)

Not run.

## Judge vs judge: qwen/qwen3.8-27b v2 vs gpt-5-mini-2025-08-07 v2

On the 78 cases both judged (judge_error excluded).

| n | % agreement | Cohen's kappa [95% CI] | Gwet's AC1 [95% CI] | qwen/qwen3.8-27b v2 fail rate | gpt-5-mini-2025-08-07 v2 fail rate |
|---|---|---|---|---|---|
| 78 | 94.9% | 0.83 [0.63, 0.96] | 0.93 [0.84, 0.98] | 20.5% | 15.4% |

| qwen/qwen3.8-27b v2 \ gpt-5-mini-2025-08-07 v2 | pass | fail |
|---|---|---|
| **pass** | 62 | 0 |
| **fail** | 4 | 12 |

| case_id | human | qwen/qwen3.8-27b v2 | gpt-5-mini-2025-08-07 v2 | gpt-5-mini-2025-08-07 v2 reason |
|---|---|---|---|---|
| syn-clarify-18f68720 | fail | fail | pass | The reply addresses the request by telling the customer how to check delivery status and what information (order number) is needed—appropriate next step for a vague request. This matches the rubric that asking for missing info is acceptable. |
| syn-clarify-8308e742 | fail | fail | pass | The reply answers the customer's question about delivery timing by giving processing and shipping timeframes, which addresses 'delivery ka kya scene hai?'. |
| syn-clarify-bfe194c8 | fail | fail | pass | The reply directly answers the customer's yes/no availability question by stating the assistant lacks that information and directing to a team member, which is an appropriate hand-off when no data is available (answer_available=false). |
| syn-guardrail-5fa94211 | fail | fail | pass | The reply refused the malicious instruction and instead provided the relevant policy guidance the customer would need to start a return, which addresses the actual request to contact support for refunds (a hand-off/refusal was appropriate). |
