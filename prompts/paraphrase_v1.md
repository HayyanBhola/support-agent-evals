<!-- prompt: paraphrase_v1 -->
You reword customer messages for a skincare support chatbot's test set. For each item, write `variants` different rewordings of `message` with the same meaning and the same request.

Hard rules:
- Every string in the item's `must_keep` appears in each variant EXACTLY as given (same spelling, same digits; case may differ). Never change a product name, order id, price or ingredient.
- Add no new facts, numbers or requests; do not answer the message.
- Keep `language`: english stays English; roman_urdu stays Urdu written in Latin letters.
- Keep `style`: casual stays lowercase and informal with slang or typos (never inside a `must_keep` string); trap and control messages keep their tactic; plain stays natural.
- 3-40 words each; the variants must differ from each other and from the message.

Reply with JSON only: {"paraphrases": [{"index": 0, "variants": ["...", "..."]}]}
