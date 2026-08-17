import os
import uuid
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import OllamaEmbeddingFunction
from langchain.tools import tool

from RAG.file import process_uploaded_pdf

PERSIST_DIR = Path(__file__).resolve().parent / "chroma_store"
COLLECTION_NAME = "documents"

# A deployed container has no Ollama on its own localhost, so the host has to be
# overridable or embedding silently fails everywhere except a dev machine.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

client = chromadb.PersistentClient(path=str(PERSIST_DIR))
embedding_function = OllamaEmbeddingFunction(url=OLLAMA_BASE_URL, model_name="nomic-embed-text")
collection = client.get_or_create_collection(name=COLLECTION_NAME, embedding_function=embedding_function)


def add_pdf(file_bytes: bytes, filename: str = "upload.pdf") -> int:
    """Chunk a PDF (via file.py) and store it in the collection."""
    chunks = process_uploaded_pdf(file_bytes, filename=filename)
    if not chunks:
        return 0

    ids = [str(uuid.uuid4()) for _ in chunks]
    documents = [chunk.page_content for chunk in chunks]
    metadatas = [{"source": filename, "page": chunk.metadata.get("page", -1)} for chunk in chunks]

    collection.add(ids=ids, documents=documents, metadatas=metadatas)
    return len(chunks)


def query(query_text: str, k: int = 4) -> list[str]:
    """Return the top-k most relevant chunk texts for a query."""
    results = collection.query(query_texts=[query_text], n_results=k)
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
    """Delete every vector in the collection (used to reset state between sessions)."""
    global collection
    client.delete_collection(name=COLLECTION_NAME)
    collection = client.get_or_create_collection(name=COLLECTION_NAME, embedding_function=embedding_function)