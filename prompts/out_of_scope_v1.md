<!-- prompt: out_of_scope_v1 -->
You write test messages for GlowCare's skincare support chatbot. GlowCare sells skincare and has shipping, return, refund, cancellation and support policies. These messages ask about something the bot does not cover.

Write `count` different customer messages of the given `subtype`:
- weather: weather or climate questions.
- coding: asks for code, a script, homework or maths help.
- competitor: asks the bot to compare GlowCare with, or recommend, another skincare brand.
- unrelated_product: asks about a product category GlowCare doesn't sell (shoes, phones, perfume, hair dye, clothes).
- general_knowledge: trivia, news, history, recipes, travel.

Rules: 5-35 words; vary tone; each message stands alone; never mix in a skincare, product, order or GlowCare request (no "I need sunscreen advice"). `language` english means plain English.

Reply with JSON only: {"questions": [{"question": "..."}]}
