"""
rag/retriever.py

The ONLY place in this project that searches the knowledge base (policies,
FAQ, product/ingredients guides). The agent (Phase 5) will call retrieve()
as a "tool" the same way it calls the database functions in database/db.py —
it never guesses policy or product-description content itself.

Per Section 8 of the brief, this file exposes exactly what was retrieved and
why, so every answer can be traced back to real source text.
"""

import chromadb
from chromadb.utils import embedding_functions

CHROMA_PERSIST_DIR = "rag/chroma_store"
COLLECTION_NAME = "glowcare_knowledge_base"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# How many chunks to retrieve per query. More chunks give the LLM more
# context but also more chance of irrelevant text diluting the answer.
# 3 is a reasonable starting point for short, focused documents like ours.
DEFAULT_TOP_K = 3

# Chroma returns a "distance" for each result (lower = more similar,
# since it's a distance metric, not a similarity score). Chunks beyond
# this distance are treated as not relevant, so the agent can honestly
# say "not found" instead of stretching a weak match into an answer —
# this is what SYSTEM_RULES.md rule 7 requires in practice.
MAX_RELEVANT_DISTANCE = 1.2


def _get_collection():
    """Opens the existing Chroma collection built by rag/ingest.py.
    Raises a clear error if ingest.py hasn't been run yet, rather than
    silently returning nothing — a silent empty result would look
    identical to 'no relevant documents found', which is a different,
    misleading situation."""
    client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
    embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL
    )
    try:
        return client.get_collection(name=COLLECTION_NAME, embedding_function=embedding_fn)
    except Exception as e:
        raise RuntimeError(
            "Knowledge base collection not found. Run `python rag/ingest.py` "
            "first to build it."
        ) from e


def retrieve(query: str, top_k: int = DEFAULT_TOP_K, verbose: bool = True):
    """Searches the knowledge base for chunks relevant to `query`.

    Returns a dict:
        {
            "query": the query string that was actually searched,
            "chunks": [
                {"id": ..., "text": ..., "source": ..., "section": ..., "distance": ...},
                ...
            ],
            "context": all chunk texts joined together, ready to hand to
                       an LLM as grounding context (Phase 4 will use this),
        }

    If verbose=True (the default), it also prints exactly what Section 8
    of the brief asks for: the query, which chunks were retrieved, their
    source documents, and their relevance scores — so you can see *why*
    the retriever picked what it picked, not just the final answer.

    Chunks beyond MAX_RELEVANT_DISTANCE are filtered out entirely rather
    than handed to the LLM — a weak match is closer to "nothing found"
    than to a real answer, and SYSTEM_RULES.md rule 7 says the system
    should say so rather than stretch a bad match into a confident reply.
    """
    collection = _get_collection()

    results = collection.query(query_texts=[query], n_results=top_k)

    raw_chunks = results["documents"][0]
    raw_metadatas = results["metadatas"][0]
    raw_distances = results["distances"][0]
    raw_ids = results["ids"][0]

    chunks = []
    for chunk_id, text, metadata, distance in zip(raw_ids, raw_chunks, raw_metadatas, raw_distances):
        if distance <= MAX_RELEVANT_DISTANCE:
            chunks.append({
                "id": chunk_id,  # "<file>-<chunk index>", stable across re-ingests
                "text": text,
                "source": metadata["source"],
                "section": metadata.get("section", metadata["source"]),
                "distance": round(distance, 4),
            })

    # Each chunk is numbered and labeled with its source file, so the LLM
    # can see which document and section every piece of text came from
    # (the chunk text itself already starts with "<Doc> > <Section>").
    context = "\n\n".join(
        f"[Chunk {i+1} | source: {c['source']}]\n{c['text']}"
        for i, c in enumerate(chunks)
    )

    if verbose:
        print(f"\n[RAG] Query: {query!r}")
        if not chunks:
            print("[RAG] No chunks within relevance threshold "
                  f"(MAX_RELEVANT_DISTANCE={MAX_RELEVANT_DISTANCE}). "
                  "Nothing relevant found — this should surface as "
                  "'information not available', not a guess.")
        for i, c in enumerate(chunks):
            print(f"[RAG] Chunk {i+1} (source: {c['source']}, "
                  f"distance: {c['distance']} — lower is more relevant)")
            preview = c["text"][:150].replace("\n", " ")
            print(f"        {preview}...")

    return {"query": query, "chunks": chunks, "context": context}


if __name__ == "__main__":
    # Manual smoke test — run `python rag/retriever.py` after ingest.py
    # to confirm retrieval actually works and returns sensible chunks.
    test_queries = [
        "Can I return a product after opening it?",
        "What ingredients are in the Hydra Balance Moisturizer?",
        "What is your policy on returning shoes?",  # deliberately irrelevant
    ]
    for q in test_queries:
        result = retrieve(q)
        print(f"[RAG] --> {len(result['chunks'])} relevant chunk(s) returned.\n")
