"""
rag/ingest.py

Loads every document under data/policies, data/faq, and data/docs, splits
each into chunks, embeds those chunks with a local Hugging Face model, and
stores them in Chroma (a local vector database — just files on disk, no
server, no API key).

Run this once to build the knowledge base:
    python rag/ingest.py

Re-run it any time a document under data/ changes — it wipes and rebuilds
the collection from scratch each time, so it's always safe to re-run.

WHY THIS EXISTS: Section 6/8 of the brief says the agent must actually
retrieve from these documents, not have answers baked into Python. This
script is what turns plain text files into something a retriever can search
by *meaning*, not just exact keyword matching.
"""

import os
import re
import glob
import chromadb
from chromadb.utils import embedding_functions

# --- Configuration -----------------------------------------------------

# Where the source documents live. Every .md file under these three
# folders becomes part of the knowledge base.
DOC_FOLDERS = [
    "data/policies",
    "data/faq",
    "data/docs",
]

# Where Chroma stores its data on disk. This is just a folder — Chroma is
# not a server you run separately, it's a Python library that persists to
# files, which is why this is "free/local" with nothing to install beyond
# the pip package.
CHROMA_PERSIST_DIR = "rag/chroma_store"
COLLECTION_NAME = "glowcare_knowledge_base"

# The embedding model. all-MiniLM-L6-v2 is small (~80MB), runs on CPU in
# a fraction of a second per chunk, and needs no API key — it downloads
# once from Hugging Face the first time this script runs, then is cached
# locally. This is the "local Hugging Face embedding model" from the brief.
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# WHY CHUNK AT ALL: an embedding model turns text into one vector that
# represents its overall meaning. A whole policy document covers too many
# distinct facts to be represented well by a single vector — a question
# about "opened product returns" would get diluted by the rest of the
# document. Splitting into smaller chunks means each vector represents
# one focused idea, so retrieval can find the *specific* relevant part.
#
# Max characters per chunk. Chunks now follow headings and paragraphs
# (see chunk_text), so no overlap is needed — nothing is cut mid-sentence.
CHUNK_SIZE = 800


def load_documents():
    """Reads every .md file from the configured folders.
    Returns a list of dicts: {"source": filename, "text": full file content}."""
    documents = []
    for folder in DOC_FOLDERS:
        for path in sorted(glob.glob(os.path.join(folder, "*.md"))):
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            documents.append({"source": os.path.basename(path), "text": text})
    return documents


def chunk_text(text: str, source: str = "", chunk_size: int = CHUNK_SIZE):
    """Splits a markdown document into chunks that follow its STRUCTURE,
    not a blind character count.

    WHY (learned in Phase 4 testing): the old sliding-window splitter cut
    the shipping policy mid-sentence, so the chunk holding "International
    shipping is not available" lost its "## Shipping Destinations" heading
    and started with "s demo." The embedding for that chunk no longer
    looked like an answer to "Do you sell in the UK?", and the LLM picked
    an unrelated FAQ chunk instead.

    Rules:
    1. Split the document at every "## " heading -> one section each.
    2. In the FAQ, every "**Q: ...**" + answer pair is its own chunk, so an
       animal-testing answer can never ride along with a shipping question.
    3. A section longer than chunk_size is split on blank lines
       (paragraphs), packing whole paragraphs together — never mid-sentence.
    4. Every chunk is prefixed with "<Document title> > <Section heading>"
       so both the embedding model and the LLM know what the chunk is about.
    """
    lines = text.strip().splitlines()
    doc_title = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("# ") else source
    doc_title = re.sub(r"\s*\(FICTIONAL DEMO DOCUMENT\)", "", doc_title)

    # Step 1: split into (heading, body) sections on "## " lines.
    sections = []
    heading, body = "Overview", []
    for line in lines[1:] if lines and lines[0].startswith("# ") else lines:
        if line.startswith("## "):
            if "\n".join(body).strip():
                sections.append((heading, "\n".join(body).strip()))
            heading, body = line[3:].strip(), []
        else:
            body.append(line)
    if "\n".join(body).strip():
        sections.append((heading, "\n".join(body).strip()))

    chunks = []
    for heading, body in sections:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]

        # Step 2: FAQ-style Q/A pairs are always their own chunk.
        if any(p.startswith("**Q:") for p in paragraphs):
            for p in paragraphs:
                question = re.match(r"\*\*Q:\s*(.*?)\*\*", p)
                label = question.group(1) if question else heading
                chunks.append(f"{doc_title} > {label}\n{p}")
            continue

        # Step 3: pack whole paragraphs up to chunk_size.
        current = ""
        for p in paragraphs:
            if current and len(current) + len(p) + 2 > chunk_size:
                chunks.append(f"{doc_title} > {heading}\n{current}")
                current = p
            else:
                current = f"{current}\n\n{p}" if current else p
        if current:
            chunks.append(f"{doc_title} > {heading}\n{current}")
    return chunks


def build_knowledge_base():
    """The main ingestion pipeline: load -> chunk -> embed -> store.
    Wipes any existing collection first so this is always safe to re-run
    after editing a document."""
    print(f"Loading documents from: {DOC_FOLDERS}")
    documents = load_documents()
    print(f"Found {len(documents)} documents.")

    # This is the Chroma client, pointed at a local folder. No network
    # calls, no server process — this line just opens/creates that folder.
    client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)

    # Chroma can compute embeddings for us if we hand it an embedding
    # function. SentenceTransformerEmbeddingFunction wraps the Hugging
    # Face model above so we never touch OpenAI or any paid API here.
    embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL
    )

    # Delete any old collection so re-running this script doesn't leave
    # stale chunks from a previous version of a document.
    try:
        client.delete_collection(COLLECTION_NAME)
        print(f"Deleted existing collection '{COLLECTION_NAME}' to rebuild fresh.")
    except Exception:
        pass  # collection didn't exist yet — nothing to delete

    collection = client.create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_fn,
    )

    all_chunks = []
    all_ids = []
    all_metadatas = []

    for doc in documents:
        chunks = chunk_text(doc["text"], source=doc["source"])
        print(f"  {doc['source']}: {len(chunks)} chunks")
        for i, chunk in enumerate(chunks):
            all_chunks.append(chunk)
            all_ids.append(f"{doc['source']}-{i}")
            # Metadata travels with each chunk and comes back at query
            # time — this is how the retriever can tell you *which
            # document* an answer's supporting chunk came from, which
            # Section 8 of the brief requires for debug logging.
            all_metadatas.append({
                "source": doc["source"],
                "chunk_index": i,
                # First line of every chunk is "<Doc title> > <Section>" —
                # stored so the retriever can label context for the LLM.
                "section": chunk.splitlines()[0],
            })

    # add() is where the actual embedding computation happens — Chroma
    # calls embedding_fn on each chunk internally, then stores the
    # resulting vectors alongside the text and metadata.
    collection.add(documents=all_chunks, ids=all_ids, metadatas=all_metadatas)

    print(f"\nDone. {len(all_chunks)} chunks embedded and stored in "
          f"'{CHROMA_PERSIST_DIR}' under collection '{COLLECTION_NAME}'.")
    return collection


if __name__ == "__main__":
    build_knowledge_base()
