"""Vector storage for uploaded PDFs.

The collection is opened lazily. Building it eagerly would mean constructing the
embedding function at import time, and with LLM_PROVIDER=openai that needs an API
key -- so a missing key would take down every part of the app that merely imports
this module, including the parts that never touch a document.
"""

import uuid
from pathlib import Path

import chromadb
from langchain.tools import tool

import providers
from RAG.file import process_uploaded_pdf

PERSIST_DIR = Path(__file__).resolve().parent / "chroma_store"

_client = None
_collection = None


def client():
    """The on-disk Chroma client. Cheap, and needs no provider configuration."""
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=str(PERSIST_DIR))
    return _client


def collection():
    """
    The collection for the active embedding model.

    Its name carries the model (see providers.collection_name), so switching
    provider switches collection rather than colliding with vectors of a
    different width.
    """
    global _collection
    if _collection is None:
        _collection = client().get_or_create_collection(
            name=providers.collection_name(),
            embedding_function=providers.get_embedding_function(),
        )
    return _collection


def add_pdf(file_bytes: bytes, filename: str = "upload.pdf") -> int:
    """Chunk a PDF (via file.py) and store it in the collection."""
    chunks = process_uploaded_pdf(file_bytes, filename=filename)
    if not chunks:
        return 0

    ids = [str(uuid.uuid4()) for _ in chunks]
    documents = [chunk.page_content for chunk in chunks]
    metadatas = [{"source": filename, "page": chunk.metadata.get("page", -1)} for chunk in chunks]

    collection().add(ids=ids, documents=documents, metadatas=metadatas)
    return len(chunks)


def query(query_text: str, k: int = 4) -> list[str]:
    """Return the top-k most relevant chunk texts for a query."""
    results = collection().query(query_texts=[query_text], n_results=k)
    return results["documents"][0] if results["documents"] else []


@tool
def rag_search(question: str) -> str:
    """
    Search previously uploaded PDF documents for information relevant to the question.
    """
    results = query(question)
    if not results:
        return "No relevant information found in the uploaded documents."
    return "\n\n".join(results)


def clear_all() -> None:
    """
    Drop the collection. Used to reset state between sessions.

    Deletes rather than deletes-and-recreates: recreating needs the embedding
    function, and this runs at startup, where an unconfigured provider would
    turn a routine cleanup into a boot failure. The next caller of collection()
    creates it.
    """
    global _collection
    try:
        client().delete_collection(name=providers.collection_name())
    except Exception:
        # Nothing to delete on a cold start, and the exception type for "no such
        # collection" has moved between Chroma releases.
        pass
    _collection = None


def list_documents() -> list[dict]:
    """
    Every indexed file, with the number of chunks it contributed.

    Chroma stores chunks, not files, so the file-level view has to be rebuilt by
    grouping on the `source` metadata add_pdf writes. Without this the only
    record of what is indexed lives in the browser tab that uploaded it, and a
    refresh loses it.
    """
    stored = collection().get(include=["metadatas"])
    counts: dict[str, int] = {}
    for meta in stored.get("metadatas") or []:
        source = (meta or {}).get("source")
        if source:
            counts[source] = counts.get(source, 0) + 1
    return [{"name": name, "chunks": count} for name, count in sorted(counts.items())]


def delete_document(source: str) -> int:
    """
    Remove one file's chunks. Returns how many were removed, 0 if unknown.

    The count is read before deleting because Chroma's delete reports nothing --
    and the caller needs to tell "removed it" from "no such file" to answer with
    the right status code.
    """
    target = collection()
    ids = (target.get(where={"source": source}, include=[]) or {}).get("ids") or []
    if ids:
        target.delete(where={"source": source})
    return len(ids)
