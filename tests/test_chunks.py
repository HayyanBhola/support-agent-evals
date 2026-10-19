import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evalkit.importers.chunks import (
    ChunkRecord,
    count_by_source,
    export_chunks,
    missing_ids,
    read_chunks,
)
from evalkit.io import read_jsonl

from .conftest import RESPONSES

ROOT = Path(__file__).resolve().parent.parent


class FakeCollection:
    """Mimics chromadb's Collection.get(include=[...]) return shape."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self.data = data
        self.includes: list[list[str]] = []

    def get(self, *, include: list[str]) -> Mapping[str, Any]:
        self.includes.append(include)
        return self.data


FAKE = FakeCollection(
    {
        "ids": ["faq.md-1", "faq.md-0", "shipping_policy.md-0"],
        "documents": ["Q: two?", "Q: one?", "Shipping takes 3-5 days."],
        "metadatas": [
            {"source": "faq.md", "section": "FAQ > Two", "chunk_index": 1},
            {"source": "faq.md", "section": "FAQ > One", "chunk_index": 0},
            None,
        ],
    }
)


def test_read_chunks_sorts_by_id_and_maps_metadata() -> None:
    chunks = read_chunks(FAKE)
    assert [c.id for c in chunks] == ["faq.md-0", "faq.md-1", "shipping_policy.md-0"]
    assert chunks[0] == ChunkRecord(
        id="faq.md-0", source="faq.md", section="FAQ > One", text="Q: one?"
    )
    assert chunks[2].source is None and chunks[2].section is None
    assert FAKE.includes[-1] == ["documents", "metadatas"]


def test_missing_documents_and_metadatas_are_tolerated() -> None:
    chunks = read_chunks(FakeCollection({"ids": ["a-0"], "documents": None, "metadatas": None}))
    assert chunks == [ChunkRecord(id="a-0", source=None, text="")]


def test_export_writes_sorted_jsonl(tmp_path: Path) -> None:
    out = tmp_path / "chunks.jsonl"
    written = export_chunks(FAKE, out)
    assert list(read_jsonl(out, ChunkRecord)) == written
    first = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert list(first) == ["id", "source", "section", "text"]


def test_count_and_missing() -> None:
    chunks = read_chunks(FAKE)
    assert count_by_source(chunks) == {"(none)": 1, "faq.md": 2}
    assert missing_ids(chunks, ["faq.md-0", "nope-1", "nope-0"]) == ["nope-0", "nope-1"]


def test_committed_export_covers_the_knowledge_fixture() -> None:
    chunks = list(read_jsonl(ROOT / "datasets" / "agent_chunks.jsonl", ChunkRecord))
    assert [c.id for c in chunks] == sorted(c.id for c in chunks)
    fixture = json.loads((RESPONSES / "knowledge.json").read_text(encoding="utf-8"))
    assert missing_ids(chunks, [c["id"] for c in fixture["retrieved_chunks"]]) == []
