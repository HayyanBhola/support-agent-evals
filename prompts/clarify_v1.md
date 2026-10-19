<!-- prompt: clarify_v1 -->
You write test messages for GlowCare's skincare support chatbot. Each message is too vague to answer: the bot has to ask which product or which order the customer means.

Write `count` different customer messages of the given `subtype`:
- order_without_id: asks about their order's status or delivery but gives no order number.
- price_without_product: asks how much something costs without saying which product.
- stock_without_product: asks whether something is in stock or available without saying which product.

Strict rules: never name a product and never use a product-type word (moisturizer, cream, serum, cleanser, face wash, sunscreen, SPF); say "it", "this", "that one", "your stuff", "the thing I saw"; never include an order number. 3-30 words. `style` casual means lowercase, slang or typos. `language` english means English; roman_urdu means Urdu in Latin letters (for example "Mera order kahan hai?").

Reply with JSON only: {"questions": [{"question": "..."}]}
