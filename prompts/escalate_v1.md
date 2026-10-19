<!-- prompt: escalate_v1 -->
You write test messages for GlowCare's skincare support chatbot. These messages must be handed to a human agent.

Write `count` different customer messages of the given `subtype`:
- human_request: explicitly asks to talk to a human, a real person, an agent or a manager.
- complaint: an angry or upset complaint about service, a late order, rude staff or the bot itself.
- refund_dispute: disputes a refund decision, a refused return or a charge, and wants it resolved.
- skin_reaction: reports a reaction that ALREADY happened after using a GlowCare product (rash, burning, swelling, hives, redness).

Rules: vary tone, length (5-45 words) and situation; do not include order ids; each message stands alone. `language` english means plain English; roman_urdu means Urdu written in Latin letters (for example "Mujhe kisi insaan se baat karni hai").

Reply with JSON only: {"questions": [{"question": "..."}]}
