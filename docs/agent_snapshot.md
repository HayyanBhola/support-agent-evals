# Agent snapshot

`agent_snapshot/` is a **read-only copy** of the GlowCare agent files that the evals rely on:
the legacy test sheet (`tests/eval_cases.json`, `tests/run_eval.py`), the routing and
answering code (`agent/router.py`, `support_agent.py`, `responder.py`, `verifier.py`,
`memory.py`), the API (`api/server.py`), the knowledge-base documents (`data/`), the database
schema and seed data, the RAG code and Chroma store, `SYSTEM_RULES.md` and `requirements.txt`.

Current snapshot: agent commit **`54f9ec6323c5d15a51ca1c43784c2c86c5e6e8e5`**, copied
2026-10-13 (see `agent_snapshot/SNAPSHOT_INFO.txt`; the script writes that file with a BOM).

## Rules

- Only `scripts/snapshot_agent.ps1` writes `agent_snapshot/`. It deletes and re-creates the
  folder on every run, so nothing generated may live inside it.
- Tests and scripts read `agent_snapshot/` only, **never the live agent repo**, and never
  depend on the agent running (except tests marked `live`).
- `agent_snapshot/rag/chroma_store/` is gitignored (binary, re-created by the script);
  everything else in the snapshot is committed. It was secret-scanned on 2026-10-13: the only
  personal data is three fictional `@example.com` customers in `database/seed_data.sql`.

## Generated from the snapshot (outside it)

| File | Made by | Contents |
|---|---|---|
| `datasets/agent_chunks.jsonl` | `scripts/export_chunks.py` | every knowledge-base chunk: `id`, `source`, `section`, `text`, sorted by id (41 chunks at `54f9ec6`) |
| `datasets/legacy_v1.jsonl` | `scripts/import_legacy.py` | the 118 legacy cases as TestCases |

## Refreshing

```powershell
.\scripts\snapshot_agent.ps1                                 # you run this; Claude never does
uv run --group snapshot python scripts/export_chunks.py       # re-export the chunks
uv run python scripts/import_legacy.py                        # re-import the legacy cases
git diff --stat agent_snapshot datasets                       # review what changed
```

`export_chunks.py` needs chromadb **1.5.9**, the version that actually wrote the store (the
agent's own `requirements.txt` says 0.5.23, which cannot open it; see
`notes/known_issues_from_agent.md`). It is in the optional `snapshot` dependency group, so
normal installs and CI don't pull it in. The script opens a temporary copy of the store,
because a chromadb client may write to its sqlite file.
