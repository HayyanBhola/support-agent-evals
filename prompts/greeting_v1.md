<!-- prompt: greeting_v1 -->
You write test messages for GlowCare's skincare support chatbot. These messages are pure social chat written BY THE CUSTOMER (never the assistant's own lines such as "How can I help you?"): they contain NO question or request about products, prices, stock, orders, policies or skin.

Write `count` different messages of the given `subtype`:
- small_talk: a greeting or opener ("hey there", "good morning!", "hi, how are you doing?").
- thanks: thanking the bot ("thanks a lot!", "thank you so much, that helped").
- closing: ending the chat ("ok bye", "that's all, have a nice day").
- roman_urdu_greeting: a greeting, thanks or goodbye in Urdu written in Latin letters ("Assalam o alaikum, kaise hain aap?", "bohat shukriya aap ka").

Rules: 3-15 words each (never fewer than 3 words); vary wording, punctuation and capitalisation. `style` casual means lowercase, slang or typos. `language` english means English; roman_urdu means Urdu in Latin letters.

Reply with JSON only: {"questions": [{"question": "..."}]}
