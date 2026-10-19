# Known issues from the agent

Issues seen in the GlowCare agent (`rag-whatsapp-agent`). Each one must become a test case.

## Greeting variants are misrouted

These messages should route to `greeting`, but they route to `knowledge` and `escalate` instead:

| Message         | Expected route | Observed route          |
|-----------------|----------------|-------------------------|
| `hi again`      | greeting       | knowledge and escalate  |
| `hello again`   | greeting       | knowledge and escalate  |
| `hey there`     | greeting       | knowledge and escalate  |
| `hello hello`   | greeting       | knowledge and escalate  |

**Action:** add all four messages to the routing dataset with `expected_route: greeting`.

## `requirements.txt` pins the wrong chromadb version

The agent's `requirements.txt` pins `chromadb==0.5.23`, but its Chroma store
(`rag/chroma_store`) was written by chromadb **1.5.9** (the store has the 1.x
`collections.schema_str` column and the `sysdb 00010-collection-schema` migration).
chromadb 0.5.23 fails to open it (`KeyError: '_type'`), so a fresh install from
`requirements.txt` cannot read the knowledge base. Found on 2026-10-13 at agent commit
`54f9ec6`.

**Fix later (agent repo):** pin `chromadb==1.5.9` in `requirements.txt`.

## The agent runs from the global Python, not a virtual environment

The agent runs from the global Python 3.13 install instead of a project virtual
environment. That is how `requirements.txt` drifted from what is actually installed: packages
were upgraded globally and the file was never regenerated.

**Fix later (agent repo):** create a venv for the agent, install into it, and regenerate
`requirements.txt` from it (e.g. `pip freeze`, trimmed to direct dependencies).
