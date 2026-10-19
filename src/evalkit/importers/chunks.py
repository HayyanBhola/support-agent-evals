"""Export the agent's knowledge-base chunks (Chroma collection) to JSONL.

The chromadb import lives in scripts/export_chunks.py; this module only needs an object
with a chromadb-style ``get()``, so it is testable without chromadb installed.
"""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from evalkit.io import write_jsonl

COLLECTION_NAME = "glowcare_knowledge_base"


class Collection(Protocol):
    def get(self, *, include: list[str]) -> Mapping[str, Any]: ...


class ChunkRecord(BaseModel):
    """One knowledge-base chunk, as the agent's retriever sees it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    source: str | None
    section: str | None = None
    text: str


def read_chunks(collection: Collection) -> list[ChunkRecord]:
    """All chunks in the collection, sorted by id."""
    data = collection.get(include=["documents", "metadatas"])
    ids: Sequence[str] = data["ids"]
    documents: Sequence[str | None] = data.get("documents") or [None] * len(ids)
    metadatas: Sequence[Mapping[str, Any] | None] = data.get("metadatas") or [None] * len(ids)
    records = []
    for chunk_id, text, meta in zip(ids, documents, metadatas, strict=True):
        meta = meta or {}
        records.append(
            ChunkRecord(
                id=chunk_id,
                source=meta.get("source"),
                section=meta.get("section"),
                text=text or "",
            )
        )
    return sorted(records, key=lambda r: r.id)


def export_chunks(collection: Collection, out_path: Path) -> list[ChunkRecord]:
    """Write every chunk to ``out_path`` as JSONL (sorted by id); return them."""
    records = read_chunks(collection)
    write_jsonl(out_path, records)
    return records


def count_by_source(chunks: Iterable[ChunkRecord]) -> dict[str, int]:
    return dict(sorted(Counter(c.source or "(none)" for c in chunks).items()))


def missing_ids(chunks: Iterable[ChunkRecord], wanted: Iterable[str]) -> list[str]:
    """Ids in ``wanted`` that are not in ``chunks``."""
    have = {c.id for c in chunks}
    return sorted(set(wanted) - have)
