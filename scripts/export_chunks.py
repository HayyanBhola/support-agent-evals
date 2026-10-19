"""Export agent_snapshot/rag/chroma_store -> datasets/agent_chunks.jsonl.

Usage: uv run --group snapshot python scripts/export_chunks.py

Opens a temporary COPY of the store, because a chromadb client may write to its sqlite
file, and agent_snapshot/ must stay a pure copy that only scripts/snapshot_agent.ps1 writes.
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

import chromadb
from chromadb.config import Settings

from evalkit.importers.chunks import COLLECTION_NAME, count_by_source, export_chunks, missing_ids

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "agent_snapshot" / "rag" / "chroma_store"
OUT = ROOT / "datasets" / "agent_chunks.jsonl"
FIXTURE = ROOT / "tests" / "fixtures" / "responses" / "knowledge.json"


def main() -> int:
    if not STORE.is_dir():
        print(f"missing {STORE}; run scripts/snapshot_agent.ps1 first")
        return 2
    # chromadb may keep the copied sqlite file open on Windows; don't fail on cleanup.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "chroma_store"
        shutil.copytree(STORE, copy)
        client = chromadb.PersistentClient(
            path=str(copy), settings=Settings(anonymized_telemetry=False)
        )
        chunks = export_chunks(client.get_collection(COLLECTION_NAME), OUT)
        del client

    print(f"wrote {len(chunks)} chunks to {OUT.relative_to(ROOT)}")
    for source, n in count_by_source(chunks).items():
        print(f"  {source:32} {n}")

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    wanted = [c["id"] for c in fixture["retrieved_chunks"]]
    missing = missing_ids(chunks, wanted)
    print(f"fixture chunk ids {wanted}: " + (f"MISSING {missing}" if missing else "all present"))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
