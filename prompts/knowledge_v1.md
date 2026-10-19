<!-- prompt: knowledge_v1 -->
You write test cases for GlowCare's skincare support chatbot. You get one or two knowledge-base `chunks`. Write `count` different customer questions that these chunks answer, each with a reference answer and key facts.

Hard rules:
- The reference answer uses ONLY facts stated in the chunks. Every number, duration and policy term in it must appear in the chunk text. Add nothing from general knowledge.
- `key_facts` is a list of groups. Each group lists 1-3 alternative phrases; each phrase is copied EXACTLY, character for character, from the chunk text (2-6 words, for example "5-7 business days", "unopened"). A correct answer must contain at least one phrase of every group. Use 1-2 groups.
- The question must not contain any key-fact phrase.
- Questions must differ in what they ask, not only in wording.

`style`:
- plain: a natural, direct question (6-30 words).
- multihop: the question needs BOTH chunks; give one key-fact group from each chunk.
- conditional: a scenario with a condition ("If I opened it already, can I still...?") that the chunk's rule decides.
- casual: lowercase, slang, abbreviations and 1-3 misspellings, still clearly about the same thing.
- roman_urdu: the question in Urdu written in Latin letters (for example "Agar product khula hua ho to kya return ho sakta hai?"); the reference answer and key facts stay in English.

Reply with JSON only: {"cases": [{"question": "...", "reference_answer": "...", "key_facts": [["...", "..."]]}]}
