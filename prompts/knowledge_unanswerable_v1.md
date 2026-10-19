<!-- prompt: knowledge_unanswerable_v1 -->
You write test messages for GlowCare's skincare support chatbot. GlowCare sells skincare products online. For each topic in `topics`, write one realistic customer question about GlowCare's policy on that topic, as if the customer expects GlowCare to have one (for example "Do you offer gift wrapping for orders?").

Rules: one question per topic, in the same order; 6-30 words; vary wording; do not mention other companies. `language` english means plain English.

Reply with JSON only: {"questions": [{"topic": "...", "question": "..."}]}
